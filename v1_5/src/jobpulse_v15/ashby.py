"""Adaptador para la API publica de tableros Ashby."""

from html import unescape
from html.parser import HTMLParser
import json
import re
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen


_BOARD_TOKEN = re.compile(r"^[A-Za-z0-9_-]+$")
_BLOCK_TAGS = {"br", "div", "li", "p", "section", "tr", "ul", "ol", "h1", "h2", "h3"}


class AshbyFetchError(RuntimeError):
    """La API Ashby devolvio una respuesta no valida o un error HTTP."""


class AshbyOffers(list[dict[str, Any]]):
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
        if tag.casefold() in _BLOCK_TAGS:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in _BLOCK_TAGS:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def text(self) -> str:
        return " ".join(" ".join(self.parts).split())


class AshbyFetcher:
    """Consulta un tablero Ashby y adapta sus vacantes al formato v1.5."""

    def __init__(self, *, timeout: float = 20.0) -> None:
        if timeout <= 0:
            raise ValueError("timeout debe ser mayor que cero.")
        self.timeout = timeout

    def fetch(
        self,
        site: dict[str, Any],
        *,
        skip_source_job_ids: set[str] | None = None,
    ) -> AshbyOffers:
        token = self._board_token(site.get("base_url"))
        config = site.get("site_config") or {}
        if not isinstance(config, dict):
            raise ValueError("La configuracion Ashby debe ser un objeto.")
        country_query = config.get("country_query") or "Costa Rica"
        if not isinstance(country_query, str) or not country_query.strip():
            raise ValueError("country_query debe contener texto.")

        url = (
            "https://api.ashbyhq.com/posting-api/job-board/"
            f"{quote(token, safe='')}"
        )
        response = self._request_json(url)
        postings = response.get("jobs")
        if not isinstance(postings, list):
            raise AshbyFetchError("La respuesta Ashby no contiene jobs.")

        offers = []
        skipped = 0
        seen_ids: set[str] = set()
        skipped_ids = skip_source_job_ids or set()
        for index, posting in enumerate(postings):
            if not isinstance(posting, dict):
                raise AshbyFetchError(f"El anuncio Ashby {index} no es un objeto.")
            source_job_id = self._required_text(posting.get("id"), index, "id")
            if source_job_id in seen_ids:
                continue
            title = self._optional_text(posting.get("title"))
            if title is None:
                raise AshbyFetchError(
                    f"El anuncio Ashby {source_job_id!r} no informa titulo."
                )
            seen_ids.add(source_job_id)

            location = self._location_text(posting.get("location"))
            secondary_locations = posting.get("secondaryLocations") or []
            if not isinstance(secondary_locations, list):
                raise AshbyFetchError(
                    f"El anuncio Ashby {source_job_id!r} tiene secondaryLocations invalidos."
                )
            secondary_names = [
                value
                for item in secondary_locations
                if (
                    value := self._location_text(item)
                )
            ]
            searchable = " ".join((title, location, *secondary_names))
            if country_query.casefold() not in searchable.casefold():
                continue
            if source_job_id in skipped_ids:
                skipped += 1
                continue

            description = self._html_to_text(
                self._optional_text(posting.get("descriptionHtml")) or ""
            )
            if not description:
                description = self._plain_text(
                    posting.get("descriptionPlain")
                )
            if country_query.casefold() not in location.casefold():
                location = f"{country_query} (multi-loc)"

            workplace_type = self._optional_text(posting.get("workplaceType"))
            is_remote = posting.get("isRemote") is True
            normalized_workplace_type = (
                (workplace_type or "")
                .casefold()
                .replace("-", "")
                .replace("_", "")
                .replace(" ", "")
            )
            if is_remote or normalized_workplace_type == "remote":
                work_mode = "remote"
            elif normalized_workplace_type == "hybrid":
                work_mode = "hybrid"
            elif normalized_workplace_type in {"onsite", "inperson"}:
                work_mode = "onsite"
            else:
                work_mode = "unknown"
            published = self._optional_text(posting.get("publishedAt"))
            job_url = self._optional_text(posting.get("jobUrl")) or self._optional_text(
                posting.get("applyUrl")
            )
            offers.append(
                {
                    "source_job_id": source_job_id,
                    "source_url": job_url,
                    "source_updated_at": published,
                    "job": {
                        "job_id": f"ashby-{token}-{source_job_id}",
                        "title": title,
                        "company": (
                            self._optional_text(site.get("company_name"))
                            or self._optional_text(site.get("site_name"))
                        ),
                        "location_raw": location or None,
                        "country": self._optional_text(site.get("country")),
                        "work_mode": work_mode,
                        "employment_type": self._optional_text(
                            posting.get("employmentType")
                        ),
                        "date_published": published[:10] if published else None,
                        "posting_language": "undetermined",
                        "posting_language_origin": "unknown",
                        "description_raw": description or None,
                    },
                    "raw_payload": posting,
                }
            )
        return AshbyOffers(offers, jobs_skipped=skipped)

    @staticmethod
    def _board_token(value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("La URL Ashby debe ser texto.")
        parsed = urlsplit(value)
        if (
            parsed.scheme.casefold() != "https"
            or parsed.hostname is None
            or parsed.hostname.casefold() != "jobs.ashbyhq.com"
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("La URL debe apuntar a un tablero HTTPS de Ashby.")
        parts = [part for part in parsed.path.split("/") if part]
        if not parts or not _BOARD_TOKEN.fullmatch(parts[0]):
            raise ValueError("La URL Ashby no contiene un token de tablero valido.")
        return parts[0]

    def _request_json(self, url: str) -> dict[str, Any]:
        request = Request(
            url,
            headers={"Accept": "application/json", "User-Agent": "JobPulse-CR/1.5"},
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise AshbyFetchError(f"Ashby respondio HTTP {exc.code}.") from exc
        except (URLError, TimeoutError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AshbyFetchError(f"No se pudo consultar Ashby: {exc}") from exc
        if not isinstance(payload, dict):
            raise AshbyFetchError("La respuesta Ashby debe ser un objeto.")
        return payload

    @classmethod
    def _html_to_text(cls, value: str) -> str:
        parser = _TextParser()
        parser.feed(unescape(value))
        return parser.text()

    @staticmethod
    def _plain_text(value: Any) -> str:
        if not isinstance(value, str):
            return ""
        return " ".join(unescape(value).split())

    @staticmethod
    def _optional_text(value: Any) -> str | None:
        return value.strip() if isinstance(value, str) and value.strip() else None

    @classmethod
    def _location_text(cls, value: Any) -> str:
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, dict):
            for key in ("name", "location"):
                candidate = cls._optional_text(value.get(key))
                if candidate:
                    return candidate
        return ""

    @classmethod
    def _required_text(cls, value: Any, index: int, field: str) -> str:
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise AshbyFetchError(
                f"El anuncio Ashby {index} no informa un {field} valido."
            )
        text = str(value).strip()
        if not text:
            raise AshbyFetchError(
                f"El anuncio Ashby {index} no informa un {field} valido."
            )
        return text
