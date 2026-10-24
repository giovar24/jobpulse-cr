"""Adaptador para portales SAP SuccessFactors via su feed RSS publico."""

from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
import re
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlsplit
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET


_JOB_ID = re.compile(r"/(\d{6,20})/?")
_HTML_BLOCKS = {"br", "div", "li", "p", "section", "tr", "ul", "ol", "h1", "h2", "h3"}


class SuccessFactorsFetchError(RuntimeError):
    """El feed RSS de SuccessFactors devolvio una respuesta no valida."""


class SuccessFactorsOffers(list[dict[str, Any]]):
    """Ofertas filtradas con conteo de anuncios omitidos por estado manual."""

    def __init__(
        self, offers: list[dict[str, Any]], *, jobs_skipped: int = 0
    ) -> None:
        super().__init__(offers)
        self.jobs_skipped = jobs_skipped


class _TextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        _ = attrs
        if tag.casefold() in _HTML_BLOCKS:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in _HTML_BLOCKS:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def text(self) -> str:
        return " ".join(" ".join(self.parts).split())


class SuccessFactorsFetcher:
    """Consulta el feed RSS de un portal SuccessFactors y adapta sus vacantes."""

    def __init__(self, *, timeout: float = 40.0) -> None:
        if timeout <= 0:
            raise ValueError("timeout debe ser mayor que cero.")
        self.timeout = timeout

    def fetch(
        self,
        site: dict[str, Any],
        *,
        skip_source_job_ids: set[str] | None = None,
    ) -> SuccessFactorsOffers:
        base = self._portal_base(site.get("base_url"))
        config = site.get("site_config") or {}
        if not isinstance(config, dict):
            raise ValueError("La configuracion SuccessFactors debe ser un objeto.")
        country_query = config.get("country_query") or "Costa Rica"
        if not isinstance(country_query, str) or not country_query.strip():
            raise ValueError("country_query debe contener texto.")

        url = (
            f"{base}/services/rss/job/"
            f"?locale=en_US&keywords={quote(country_query)}"
        )
        response = self._request_text(url)
        try:
            root = ET.fromstring(response)
        except ET.ParseError as exc:
            raise SuccessFactorsFetchError(
                f"El feed de SuccessFactors no es XML valido: {exc}"
            ) from exc

        items = root.findall(".//item")
        offers = []
        skipped = 0
        seen_ids: set[str] = set()
        skipped_ids = skip_source_job_ids or set()
        country_fold = country_query.casefold()

        for index, item in enumerate(items):
            title = self._item_text(item, "title")
            if title is None:
                raise SuccessFactorsFetchError(
                    f"El anuncio RSS {index} de SuccessFactors no informa titulo."
                )
            link = self._item_text(item, "link")
            description_html = self._item_text(item, "description") or ""
            description = self._html_to_text(unescape(description_html))
            pub_date = self._item_text(item, "pubDate")

            slug = unquote(link or "")
            searchable_head = " ".join((title, slug))
            if country_fold not in searchable_head.casefold():
                tail = description[-400:]
                if country_fold not in tail.casefold():
                    continue

            source_job_id = self._extract_job_id(slug, title)
            if source_job_id in seen_ids:
                continue
            if source_job_id in skipped_ids:
                skipped += 1
                continue
            seen_ids.add(source_job_id)

            published = self._pub_date_to_iso(pub_date)
            if link and link.startswith("http"):
                source_url = link
            else:
                source_url = base + link if link else None
            offers.append(
                {
                    "source_job_id": source_job_id,
                    "source_url": source_url,
                    "source_updated_at": published,
                    "job": {
                        "job_id": f"successfactors-{source_job_id}",
                        "title": title,
                        "company": (
                            self._optional_text(site.get("company_name"))
                            or self._optional_text(site.get("site_name"))
                        ),
                        "location_raw": country_query,
                        "country": self._optional_text(site.get("country")),
                        "work_mode": "unknown",
                        "date_published": published,
                        "posting_language": "undetermined",
                        "posting_language_origin": "unknown",
                        "description_raw": description or None,
                    },
                    "raw_payload": {
                        "title": title,
                        "link": link,
                        "pubDate": pub_date,
                        "description": description_html,
                    },
                }
            )
        return SuccessFactorsOffers(offers, jobs_skipped=skipped)

    @staticmethod
    def _portal_base(value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("La URL SuccessFactors debe ser texto.")
        parsed = urlsplit(value.strip())
        if (
            parsed.scheme.casefold() != "https"
            or parsed.hostname is None
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError(
                "La URL debe ser un portal HTTPS de SuccessFactors."
            )
        base = value.strip().rstrip("/")
        return base

    def _request_text(self, url: str) -> str:
        request = Request(
            url,
            headers={
                "Accept": "application/rss+xml, application/xml",
                "User-Agent": "JobPulse-CR/1.5",
            },
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                return response.read().decode("utf-8")
        except HTTPError as exc:
            raise SuccessFactorsFetchError(
                f"SuccessFactors respondio HTTP {exc.code}."
            ) from exc
        except (URLError, TimeoutError, OSError, UnicodeDecodeError) as exc:
            raise SuccessFactorsFetchError(
                f"No se pudo consultar SuccessFactors: {exc}"
            ) from exc

    @staticmethod
    def _item_text(item: ET.Element, tag: str) -> str | None:
        node = item.find(tag)
        if node is None or node.text is None:
            return None
        return node.text.strip() or None

    @staticmethod
    def _extract_job_id(slug: str, title: str) -> str:
        match = _JOB_ID.search(slug)
        if match:
            return match.group(1)
        return str(abs(hash(title)) % 10**10)

    @staticmethod
    def _pub_date_to_iso(value: str | None) -> str | None:
        if not value:
            return None
        try:
            return parsedate_to_datetime(value).strftime("%Y-%m-%d")
        except (TypeError, ValueError):
            return None

    @classmethod
    def _html_to_text(cls, value: str) -> str:
        parser = _TextParser()
        parser.feed(unescape(value))
        return parser.text()

    @staticmethod
    def _optional_text(value: Any) -> str | None:
        return value.strip() if isinstance(value, str) and value.strip() else None
