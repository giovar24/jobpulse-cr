"""Adaptador para la API publica de tableros Workable."""

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


class WorkableFetchError(RuntimeError):
    """La API Workable devolvio una respuesta no valida o un error HTTP."""


class WorkableOffers(list[dict[str, Any]]):
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


class WorkableFetcher:
    """Consulta un tablero Workable y adapta sus vacantes al formato v1.5."""

    def __init__(self, *, timeout: float = 20.0) -> None:
        if timeout <= 0:
            raise ValueError("timeout debe ser mayor que cero.")
        self.timeout = timeout

    def fetch(
        self,
        site: dict[str, Any],
        *,
        skip_source_job_ids: set[str] | None = None,
    ) -> WorkableOffers:
        token = self._board_token(site.get("base_url"))
        config = site.get("site_config") or {}
        if not isinstance(config, dict):
            raise ValueError("La configuracion Workable debe ser un objeto.")
        country_query = config.get("country_query") or "Costa Rica"
        if not isinstance(country_query, str) or not country_query.strip():
            raise ValueError("country_query debe contener texto.")

        url = (
            "https://apply.workable.com/api/v1/accounts/"
            f"{quote(token, safe='')}/jobs?details=true"
        )
        payload = self._request_json(url)
        postings = payload.get("jobs")
        if not isinstance(postings, list):
            raise WorkableFetchError("La respuesta Workable no contiene jobs.")

        offers = []
        skipped = 0
        seen_ids: set[str] = set()
        skipped_ids = skip_source_job_ids or set()
        for index, posting in enumerate(postings):
            if not isinstance(posting, dict):
                raise WorkableFetchError(
                    f"El anuncio Workable {index} no es un objeto."
                )
            source_job_id = self._required_text(posting.get("shortcode"), index, "id")
            if source_job_id in seen_ids:
                continue
            title = self._optional_text(posting.get("title"))
            if title is None:
                raise WorkableFetchError(
                    f"El anuncio Workable {source_job_id!r} no informa titulo."
                )
            seen_ids.add(source_job_id)

            location_data = posting.get("location")
            location = (
                self._optional_text(location_data.get("city"))
                if isinstance(location_data, dict)
                else None
            )
            country = (
                self._optional_text(location_data.get("country"))
                if isinstance(location_data, dict)
                else None
            )
            searchable = " ".join(part for part in (title, location, country) if part)
            if country_query.casefold() not in searchable.casefold():
                continue
            if source_job_id in skipped_ids:
                skipped += 1
                continue

            location_parts = [
                value
                for value in (
                    location,
                    self._optional_text(location_data.get("region"))
                    if isinstance(location_data, dict)
                    else None,
                    country,
                )
                if value
            ]
            location_raw = ", ".join(location_parts) or None

            description = self._html_to_text(
                self._optional_text(posting.get("description")) or ""
            )
            requirements = self._html_to_text(
                self._optional_text(posting.get("requirements")) or ""
            )
            combined = " ".join(
                part for part in (description, requirements) if part
            )

            published = self._optional_text(posting.get("published_on"))
            work_mode = "unknown"
            telecommuting = posting.get("telecommuting")
            if telecommuting is True:
                work_mode = "remote"

            job_url = (
                self._optional_text(posting.get("url"))
                or f"https://apply.workable.com/{token}/j/{source_job_id}/"
            )
            offers.append(
                {
                    "source_job_id": source_job_id,
                    "source_url": job_url,
                    "source_updated_at": published,
                    "job": {
                        "job_id": f"workable-{token}-{source_job_id}",
                        "title": title,
                        "company": (
                            self._optional_text(site.get("company_name"))
                            or self._optional_text(site.get("site_name"))
                        ),
                        "location_raw": location_raw,
                        "country": self._optional_text(site.get("country")),
                        "work_mode": work_mode,
                        "employment_type": self._optional_text(
                            posting.get("type")
                        ),
                        "date_published": (
                            published[:10]
                            if published
                            and not published[:10].isdigit()
                            else None
                        ),
                        "posting_language": "undetermined",
                        "posting_language_origin": "unknown",
                        "description_raw": combined or None,
                    },
                    "raw_payload": posting,
                }
            )
        return WorkableOffers(offers, jobs_skipped=skipped)

    @staticmethod
    def _board_token(value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("La URL Workable debe ser texto.")
        parsed = urlsplit(value)
        hostname = (parsed.hostname or "").casefold()
        if (
            parsed.scheme.casefold() != "https"
            or hostname != "apply.workable.com"
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("La URL debe apuntar a un tablero HTTPS de Workable.")
        parts = [part for part in parsed.path.split("/") if part]
        if not parts or not _BOARD_TOKEN.fullmatch(parts[0]):
            raise ValueError(
                "La URL Workable no contiene un token de tablero valido."
            )
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
            raise WorkableFetchError(
                f"Workable respondio HTTP {exc.code}."
            ) from exc
        except (
            URLError,
            TimeoutError,
            OSError,
            UnicodeDecodeError,
            json.JSONDecodeError,
        ) as exc:
            raise WorkableFetchError(
                f"No se pudo consultar Workable: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise WorkableFetchError("La respuesta Workable debe ser un objeto.")
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
            raise WorkableFetchError(
                f"El anuncio Workable {index} no informa un {field} valido."
            )
        text = str(value).strip()
        if not text:
            raise WorkableFetchError(
                f"El anuncio Workable {index} no informa un {field} valido."
            )
        return text
