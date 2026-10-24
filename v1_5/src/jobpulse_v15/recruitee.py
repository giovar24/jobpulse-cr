"""Adaptador para la API publica de tableros Recruitee."""

from html import unescape
from html.parser import HTMLParser
import json
import re
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


_BOARD_TOKEN = re.compile(r"^[A-Za-z0-9_-]+$")
_HTML_BLOCKS = {"br", "div", "li", "p", "section", "tr", "ul", "ol", "h1", "h2", "h3"}


class RecruiteeFetchError(RuntimeError):
    """La API Recruitee devolvio una respuesta no valida o un error HTTP."""


class RecruiteeOffers(list[dict[str, Any]]):
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


class RecruiteeFetcher:
    """Consulta un tablero Recruitee y adapta sus vacantes al formato v1.5."""

    def __init__(self, *, timeout: float = 20.0) -> None:
        if timeout <= 0:
            raise ValueError("timeout debe ser mayor que cero.")
        self.timeout = timeout

    def fetch(
        self,
        site: dict[str, Any],
        *,
        skip_source_job_ids: set[str] | None = None,
    ) -> RecruiteeOffers:
        token = self._board_token(site.get("base_url"))
        config = site.get("site_config") or {}
        if not isinstance(config, dict):
            raise ValueError("La configuracion Recruitee debe ser un objeto.")
        country_query = config.get("country_query") or "Costa Rica"
        if not isinstance(country_query, str) or not country_query.strip():
            raise ValueError("country_query debe contener texto.")

        url = f"https://{token}.recruitee.com/api/offers/"
        payload = self._request_json(url)
        postings = payload.get("offers")
        if not isinstance(postings, list):
            raise RecruiteeFetchError(
                "La respuesta Recruitee no contiene offers."
            )

        offers = []
        skipped = 0
        seen_ids: set[str] = set()
        skipped_ids = skip_source_job_ids or set()
        for index, posting in enumerate(postings):
            if not isinstance(posting, dict):
                raise RecruiteeFetchError(
                    f"El anuncio Recruitee {index} no es un objeto."
                )
            source_job_id = self._required_text(posting.get("id"), index, "id")
            if source_job_id in seen_ids:
                continue
            title = self._optional_text(posting.get("position"))
            if title is None:
                raise RecruiteeFetchError(
                    f"El anuncio Recruitee {source_job_id!r} no informa titulo."
                )
            seen_ids.add(source_job_id)

            location = self._optional_text(posting.get("location"))
            locations = posting.get("locations") or []
            if not isinstance(locations, list) or any(
                not isinstance(item, str) for item in locations
            ):
                raise RecruiteeFetchError(
                    f"El anuncio Recruitee {source_job_id!r} tiene locations invalidos."
                )
            location_names = [
                value for item in locations if (value := self._optional_text(item))
            ]
            searchable = " ".join((title, location, *location_names))
            if country_query.casefold() not in searchable.casefold():
                continue
            if source_job_id in skipped_ids:
                skipped += 1
                continue

            location_raw = location or "; ".join(location_names)
            if country_query.casefold() not in (location_raw or "").casefold():
                location_raw = f"{country_query} (multi-loc)"

            description = self._html_to_text(
                self._optional_text(posting.get("description")) or ""
            )
            requirements = self._html_to_text(
                self._optional_text(posting.get("requirements")) or ""
            )
            combined = " ".join(
                part for part in (description, requirements) if part
            )

            published = (
                self._optional_text(posting.get("published_at"))
                or self._optional_text(posting.get("created_at"))
            )
            offers.append(
                {
                    "source_job_id": source_job_id,
                    "source_url": self._optional_text(posting.get("careers_url")),
                    "source_updated_at": published,
                    "job": {
                        "job_id": f"recruitee-{token}-{source_job_id}",
                        "title": title,
                        "company": (
                            self._optional_text(site.get("company_name"))
                            or self._optional_text(site.get("site_name"))
                        ),
                        "location_raw": location_raw or None,
                        "country": self._optional_text(site.get("country")),
                        "work_mode": "unknown",
                        "employment_type": self._optional_text(
                            posting.get("employment_type")
                        ),
                        "date_published": published[:10] if published else None,
                        "posting_language": "undetermined",
                        "posting_language_origin": "unknown",
                        "description_raw": combined or None,
                    },
                    "raw_payload": posting,
                }
            )
        return RecruiteeOffers(offers, jobs_skipped=skipped)

    @staticmethod
    def _board_token(value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("La URL Recruitee debe ser texto.")
        parsed = urlsplit(value)
        hostname = (parsed.hostname or "").casefold()
        if (
            parsed.scheme.casefold() != "https"
            or not hostname.endswith(".recruitee.com")
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("La URL debe apuntar a un tablero HTTPS de Recruitee.")
        token = hostname[: -len(".recruitee.com")]
        if not _BOARD_TOKEN.fullmatch(token) or token.casefold() == "www":
            raise ValueError(
                "La URL Recruitee no contiene un token de tablero valido."
            )
        return token

    def _request_json(self, url: str) -> dict[str, Any]:
        request = Request(
            url,
            headers={"Accept": "application/json", "User-Agent": "JobPulse-CR/1.5"},
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise RecruiteeFetchError(
                f"Recruitee respondio HTTP {exc.code}."
            ) from exc
        except (
            URLError,
            TimeoutError,
            OSError,
            UnicodeDecodeError,
            json.JSONDecodeError,
        ) as exc:
            raise RecruiteeFetchError(
                f"No se pudo consultar Recruitee: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise RecruiteeFetchError("La respuesta Recruitee debe ser un objeto.")
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
            raise RecruiteeFetchError(
                f"El anuncio Recruitee {index} no informa un {field} valido."
            )
        text = str(value).strip()
        if not text:
            raise RecruiteeFetchError(
                f"El anuncio Recruitee {index} no informa un {field} valido."
            )
        return text
