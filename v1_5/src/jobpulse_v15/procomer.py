"""Adaptador Procomer (talento.procomer.com) para v1.5 — versión probada.

API REST pública, sin autenticación, sin navegador.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger("jobpulse.procomer")


def _strip_html(text: str) -> str:
    if not text:
        return ""
    return BeautifulSoup(text, "html.parser").get_text(" ", strip=True)


class ProcomerFetchError(RuntimeError):
    """Error consultando Procomer."""


class ProcomerOffers(list[dict[str, Any]]):
    def __init__(self, offers: list[dict[str, Any]], *, jobs_skipped: int = 0) -> None:
        super().__init__(offers)
        self.jobs_skipped = jobs_skipped


class ProcomerFetcher:
    """Consulta la API de Procomer."""

    def __init__(
        self,
        *,
        timeout: float = 30.0,
        request_delay: float = 0.2,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout debe ser mayor que cero.")
        if request_delay < 0 or request_delay > 5:
            raise ValueError("request_delay debe estar entre 0 y 5 segundos.")
        self.timeout = timeout
        self.request_delay = request_delay
        self.api_url = "https://talento.procomer.com/api/candidate/jobs-available"

    async def fetch(
        self,
        site: dict[str, Any],
        *,
        pages: int = 10,
        search_text: str | None = None,
        skip_source_job_ids: set[str] | None = None,
    ) -> ProcomerOffers:
        if isinstance(pages, bool) or not isinstance(pages, int):
            raise ValueError("pages debe ser un entero.")
        if pages < 1 or pages > 50:
            raise ValueError("pages debe estar entre 1 y 50.")

        search_term = search_text or site.get("site_config", {}).get("search_text") or "Costa Rica"
        skipped_ids = skip_source_job_ids or set()
        offers = []
        skipped = 0
        seen = set()

        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            for page_num in range(1, pages + 1):
                logger.info("Procomer: página %d/%d", page_num, pages)
                try:
                    params = {"search": search_term, "page": page_num, "per_page": 16}
                    response = await client.get(self.api_url, params=params)
                except Exception as e:
                    logger.error("Procomer: error de red página %d: %s", page_num, e)
                    break

                # La API responde 202 (Accepted) con JSON válido
                if not (200 <= response.status_code < 300):
                    logger.warning("Procomer: HTTP %d en página %d", response.status_code, page_num)
                    break

                try:
                    data = response.json()
                except ValueError:
                    logger.warning("Procomer: JSON inválido en página %d", page_num)
                    break

                items = data.get("data", [])
                if not items:
                    logger.info("Procomer: sin más resultados en página %d", page_num)
                    break

                for item in items:
                    title = _strip_html(item.get("name", ""))
                    if not title:
                        continue

                    raw_id = str(item.get("id") or "")
                    if not raw_id or raw_id in seen:
                        continue
                    if raw_id in skipped_ids:
                        skipped += 1
                        continue
                    seen.add(raw_id)

                    org = item.get("organization") or {}
                    company = (
                        _strip_html(item.get("anonymous_name", ""))
                        if item.get("external_visibility") == "anonymous"
                        else ""
                    ) or _strip_html(org.get("name", "")) or "PROCOMER"

                    location_raw = _strip_html(item.get("provincia", ""))
                    date_pub = item.get("publish_date")

                    offers.append({
                        "source_job_id": raw_id,
                        "source_url": f"https://talento.procomer.com/interview/{raw_id}",
                        "source_updated_at": date_pub,
                        "job": {
                            "job_id": f"procomer-{raw_id}",
                            "title": title,
                            "company": company,
                            "location_raw": location_raw or None,
                            "country": "CR",
                            **self._normalize_cr_location(location_raw),
                            "work_mode": "unknown",
                            "employment_type": None,
                            "date_published": date_pub,
                            "expires_at": None,
                            "posting_language": "es",
                            "posting_language_origin": "source",
                            "description_raw": _strip_html(item.get("description", "")),
                            "requirements_raw": _strip_html(item.get("requirements", "")),
                            "salary_raw": item.get("salary") or "",
                        },
                        "raw_payload": item,
                    })

                if self.request_delay:
                    import asyncio
                    await asyncio.sleep(self.request_delay)

        logger.info("Procomer: %d ofertas extraídas, %d omitidas", len(offers), skipped)
        return ProcomerOffers(offers, jobs_skipped=skipped)

    @staticmethod
    def _normalize_cr_location(location: str) -> Dict[str, str | None]:
        if not location:
            return {"city": None, "region": None}
        loc = location.lower()
        provinces = {
            "alajuela": "Alajuela", "cartago": "Cartago", "guanacaste": "Guanacaste",
            "heredia": "Heredia", "limón": "Limón", "limon": "Limón",
            "puntarenas": "Puntarenas", "san josé": "San José", "san jose": "San José",
        }
        for key, prov in provinces.items():
            if key in loc:
                parts = [p.strip() for p in location.split(",")]
                if len(parts) >= 2 and parts[-1].lower() == key:
                    return {"city": parts[0], "region": prov}
                return {"city": None, "region": prov}
        return {"city": None, "region": None}