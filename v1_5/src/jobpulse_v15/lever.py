"""Adaptador para la API publica de tableros Lever."""

from html import unescape
from html.parser import HTMLParser
import json
import re
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen


_BOARD_TOKEN = re.compile(r"^[A-Za-z0-9_-]+$")
_HTML_BLOCKS = {"br", "div", "li", "p", "section", "tr", "ul", "ol", "h1", "h2", "h3"}


class LeverFetchError(RuntimeError):
    """La API Lever devolvio una respuesta no valida o un error HTTP."""


class LeverOffers(list[dict[str, Any]]):
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


class LeverFetcher:
    """Consulta un tablero Lever y adapta sus vacantes al formato v1.5."""

    def __init__(self, *, timeout: float = 20.0) -> None:
        if timeout <= 0:
            raise ValueError("timeout debe ser mayor que cero.")
        self.timeout = timeout

    def fetch(
        self,
        site: dict[str, Any],
        *,
        skip_source_job_ids: set[str] | None = None,
    ) -> LeverOffers:
        token = self._board_token(site.get("base_url"))
        config = site.get("site_config") or {}
        if not isinstance(config, dict):
            raise ValueError("La configuracion Lever debe ser un objeto.")
        country_query = config.get("country_query") or "Costa Rica"
        if not isinstance(country_query, str) or not country_query.strip():
            raise ValueError("country_query debe contener texto.")

        url_eu = f"https://api.eu.lever.co/v0/postings/{quote(token, safe='')}?mode=json"
        url_com = f"https://api.lever.co/v0/postings/{quote(token, safe='')}?mode=json"

        postings = None
        try:
            postings = self._request_json(url_eu)
        except LeverFetchError:
            postings = self._request_json(url_com)

        if not isinstance(postings, list):
            raise LeverFetchError("La respuesta Lever no contiene una lista de postings.")

        offers = []
        skipped = 0
        seen_ids: set[str] = set()
        skipped_ids = skip_source_job_ids or set()

        for index, posting in enumerate(postings):
            if not isinstance(posting, dict):
                raise LeverFetchError(f"El anuncio Lever {index} no es un objeto.")

            source_job_id = self._required_text(posting.get("id"), index, "id")
            if source_job_id in seen_ids:
                continue
            title = self._optional_text(posting.get("text"))
            if title is None:
                raise LeverFetchError(
                    f"El anuncio Lever {source_job_id!r} no informa titulo."
                )
            seen_ids.add(source_job_id)

            country = self._optional_text(posting.get("country"))
            lists_text = " ".join(
                part
                for item in (posting.get("lists") or [])
                if isinstance(item, dict)
                if (part := self._optional_text(item.get("text")))
            )

            searchable_text = " ".join(
                part for part in (title, country, lists_text) if part
            )
            if country_query.casefold() not in searchable_text.casefold():
                continue
            if source_job_id in skipped_ids:
                skipped += 1
                continue

            description_text = self._html_to_text(self._optional_text(posting.get("description")) or "")
            additional_text = self._html_to_text(self._optional_text(posting.get("additional")) or "")
            description = " ".join(part for part in (description_text, additional_text) if part)

            location_raw = self._optional_text(posting.get("categories", {}).get("location")) or ""
            if country_query.casefold() not in location_raw.casefold():
                location_raw = f"{country_query} (multi-loc)"

            published_at_ep = posting.get("createdAt")
            published_at = (
                datetime.fromtimestamp(int(published_at_ep) / 1000, tz=timezone.utc)
                .strftime("%Y-%m-%d")
                if published_at_ep
                else None
            )

            job_url = self._optional_text(posting.get("hostedUrl")) or self._optional_text(
                posting.get("applyUrl")
            )

            offers.append(
                {
                    "source_job_id": source_job_id,
                    "source_url": job_url,
                    "source_updated_at": published_at, # Lever doesn't have an updated_at
                    "job": {
                        "job_id": f"lever-{token}-{source_job_id}",
                        "title": title,
                        "company": (
                            self._optional_text(site.get("company_name"))
                            or self._optional_text(site.get("site_name"))
                        ),
                        "location_raw": location_raw or None,
                        "country": self._optional_text(site.get("country")),
                        "work_mode": "unknown", # Lever API does not provide this
                        "employment_type": self._optional_text(posting.get("categories", {}).get("commitment")),
                        "date_published": published_at,
                        "posting_language": "undetermined",
                        "posting_language_origin": "unknown",
                        "description_raw": description or None,
                    },
                    "raw_payload": posting,
                }
            )
        return LeverOffers(offers, jobs_skipped=skipped)

    @staticmethod
    def _board_token(value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("La URL Lever debe ser texto.")
        parsed = urlsplit(value)
        if (
            parsed.scheme.casefold() != "https"
            or parsed.hostname is None
            or parsed.hostname.casefold() not in {"jobs.lever.co", "boards.eu.lever.co", "jobs.eu.lever.co"}
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("La URL debe apuntar a un tablero HTTPS de Lever.")
        parts = [part for part in parsed.path.split("/") if part]
        if not parts or not _BOARD_TOKEN.fullmatch(parts[0]):
            raise ValueError("La URL Lever no contiene un token de tablero valido.")
        return parts[0]

    def _request_json(self, url: str) -> list[dict[str, Any]]:
        request = Request(
            url,
            headers={"Accept": "application/json", "User-Agent": "JobPulse-CR/1.5"},
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise LeverFetchError(f"Lever respondio HTTP {exc.code}.") from exc
        except (URLError, TimeoutError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LeverFetchError(f"No se pudo consultar Lever: {exc}") from exc
        if not isinstance(payload, list): # Lever returns a list of postings
            raise LeverFetchError("La respuesta Lever debe ser una lista.")
        return payload

    @classmethod
    def _html_to_text(cls, value: str) -> str:
        parser = _TextParser()
        parser.feed(unescape(value))
        return parser.text()

    @staticmethod
    def _optional_text(value: Any) -> str | None:
        return value.strip() if isinstance(value, str) and value.strip() else None

    @classmethod
    def _required_text(cls, value: Any, index: int, field: str) -> str:
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise LeverFetchError(
                f"El anuncio Lever {index} no informa un {field} valido."
            )
        text = str(value).strip()
        if not text:
            raise LeverFetchError(
                f"El anuncio Lever {index} no informa un {field} valido."
            )
        return text
