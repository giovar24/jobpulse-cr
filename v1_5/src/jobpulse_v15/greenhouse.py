"""Adaptador para la API publica de tableros Greenhouse."""

from html import unescape
from html.parser import HTMLParser
import json
import re
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen


_BOARD_TOKEN = re.compile(r"^[A-Za-z0-9_-]+$")
_HTML_BLOCKS = {"br", "div", "li", "p", "section", "tr", "ul", "ol", "h1", "h2", "h3"}


class GreenhouseFetchError(RuntimeError):
    """La API Greenhouse devolvio una respuesta no valida o un error HTTP."""


class GreenhouseOffers(list[dict[str, Any]]):
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


class GreenhouseFetcher:
    """Consulta un tablero Greenhouse y lo adapta al formato de ingesta v1.5."""

    def __init__(self, *, timeout: float = 20.0) -> None:
        if timeout <= 0:
            raise ValueError("timeout debe ser mayor que cero.")
        self.timeout = timeout

    def fetch(
        self,
        site: dict[str, Any],
        *,
        skip_source_job_ids: set[str] | None = None,
    ) -> GreenhouseOffers:
        token = self._board_token(site.get("base_url"))
        config = site.get("site_config") or {}
        if not isinstance(config, dict):
            raise ValueError("La configuracion Greenhouse debe ser un objeto.")
        country_query = config.get("country_query") or "Costa Rica"
        if not isinstance(country_query, str) or not country_query.strip():
            raise ValueError("country_query debe contener texto.")

        url = (
            "https://boards-api.greenhouse.io/v1/boards/"
            f"{quote(token, safe='')}/jobs?content=true"
        )
        response = self._request_json(url)
        postings = response.get("jobs")
        if not isinstance(postings, list):
            raise GreenhouseFetchError("La respuesta Greenhouse no contiene jobs.")

        offers = []
        skipped = 0
        seen_ids: set[str] = set()
        skipped_ids = skip_source_job_ids or set()
        for index, posting in enumerate(postings):
            if not isinstance(posting, dict):
                raise GreenhouseFetchError(
                    f"El anuncio Greenhouse {index} no es un objeto."
                )
            source_job_id = self._required_text(posting.get("id"), index, "id")
            if source_job_id in seen_ids:
                continue
            title = self._optional_text(posting.get("title"))
            if title is None:
                raise GreenhouseFetchError(
                    f"El anuncio Greenhouse {source_job_id!r} no informa titulo."
                )
            seen_ids.add(source_job_id)

            location_data = posting.get("location")
            location = (
                self._optional_text(location_data.get("name"))
                if isinstance(location_data, dict)
                else None
            )
            offices = posting.get("offices") or []
            if not isinstance(offices, list) or any(
                not isinstance(office, dict) for office in offices
            ):
                raise GreenhouseFetchError(
                    f"El anuncio Greenhouse {source_job_id!r} tiene offices invalidos."
                )
            office_names = [
                value
                for office in offices
                if (value := self._optional_text(office.get("name")))
            ]
            location_raw = location or "; ".join(office_names)
            searchable_location = " ".join((location_raw, *office_names))
            if country_query.casefold() not in searchable_location.casefold():
                continue
            if source_job_id in skipped_ids:
                skipped += 1
                continue

            content = self._optional_text(posting.get("content")) or ""
            parser = _TextParser()
            parser.feed(unescape(content))
            description = parser.text()
            departments = posting.get("departments") or []
            if not isinstance(departments, list) or any(
                not isinstance(department, dict) for department in departments
            ):
                raise GreenhouseFetchError(
                    f"El anuncio Greenhouse {source_job_id!r} tiene departments invalidos."
                )
            department_names = [
                value
                for department in departments
                if (value := self._optional_text(department.get("name")))
            ]
            if department_names:
                department_text = "Departamento: " + "; ".join(department_names)
                description = " ".join(
                    part for part in (description, department_text) if part
                )

            published = self._optional_text(posting.get("first_published"))
            if published is None:
                published = self._optional_text(posting.get("updated_at"))
            job: dict[str, Any] = {
                "job_id": f"greenhouse-{token}-{source_job_id}",
                "title": title,
                "company": (
                    self._optional_text(site.get("company_name"))
                    or self._optional_text(site.get("site_name"))
                ),
                "location_raw": location_raw or None,
                "country": self._optional_text(site.get("country")),
                "work_mode": "unknown",
                "date_published": published[:10] if published else None,
                "posting_language": "undetermined",
                "posting_language_origin": "unknown",
                "description_raw": description or None,
            }
            offers.append(
                {
                    "source_job_id": source_job_id,
                    "source_url": self._optional_text(posting.get("absolute_url")),
                    "source_updated_at": self._optional_text(posting.get("updated_at")),
                    "job": job,
                    "raw_payload": posting,
                }
            )
        return GreenhouseOffers(offers, jobs_skipped=skipped)

    @staticmethod
    def _board_token(value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("La URL Greenhouse debe ser texto.")
        parsed = urlsplit(value)
        if (
            parsed.scheme.casefold() != "https"
            or parsed.hostname is None
            or parsed.hostname.casefold()
            not in {"boards.greenhouse.io", "job-boards.greenhouse.io"}
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("La URL debe apuntar a un tablero HTTPS de Greenhouse.")
        parts = [part for part in parsed.path.split("/") if part]
        if not parts or not _BOARD_TOKEN.fullmatch(parts[0]):
            raise ValueError("La URL Greenhouse no contiene un token de tablero valido.")
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
            raise GreenhouseFetchError(f"Greenhouse respondio HTTP {exc.code}.") from exc
        except (URLError, TimeoutError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GreenhouseFetchError(f"No se pudo consultar Greenhouse: {exc}") from exc
        if not isinstance(payload, dict):
            raise GreenhouseFetchError("La respuesta Greenhouse debe ser un objeto.")
        return payload

    @staticmethod
    def _optional_text(value: Any) -> str | None:
        return value.strip() if isinstance(value, str) and value.strip() else None

    @classmethod
    def _required_text(cls, value: Any, index: int, field: str) -> str:
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise GreenhouseFetchError(
                f"El anuncio Greenhouse {index} no informa un {field} valido."
            )
        text = str(value).strip()
        if not text:
            raise GreenhouseFetchError(
                f"El anuncio Greenhouse {index} no informa un {field} valido."
            )
        return text
