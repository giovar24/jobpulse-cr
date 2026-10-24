"""Adaptador CINDE Jobs (cindejobs.com) para v1.5 — versión probada del proyecto principal.

API GraphQL pública (https://api.cindejobs.com/graphql), sin navegador ni credenciales.
Barrido completo del tablero activo (el gate filtra por relevancia semántica).
El API NO expone fecha de publicación (verificado por introspección del esquema).
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger("jobpulse.cinde")

API_URL = "https://api.cindejobs.com/graphql"
BASE_URL = "https://cindejobs.com"
_JOB_DETAIL = "{base}/en/fair/{company_slug}/{slug}"

# Query GraphQL probada (introspección 2026-10-20)
_QUERY = """
query Offers($limit: Int!, $page: Int!) {
  viewJobOffers(limit: $limit, page: $page) {
    jobOfferId
    name
    nameEs
    slug
    slugEs
    companySlug
    companyName
    officeName
    wfhEs
    description
    descriptionEs
    features
    featuresEs
    technicalSkillsRequired
    technicalSkillsRequiredEs
    recruitmentProcessEs
    academicLevelRequiredEs
    workExperience
    languagesEs
    positionCategoryNameEs
    positionLevelNameEs
    vacancy
  }
}
"""

# Campos estructurados que dan contexto al embedding (categoría, nivel, experiencia, modalidad, vacantes, idiomas)
_CONTEXTO = (
    ("positionCategoryNameEs", "Categoría"),
    ("positionLevelNameEs", "Nivel"),
    ("workExperience", "Experiencia"),
    ("academicLevelRequiredEs", "Formación"),
    ("languagesEs", "Idiomas"),
    ("wfhEs", "Modalidad"),
)


def _strip_html(text: str) -> str:
    if not text:
        return ""
    return BeautifulSoup(text, "html.parser").get_text(" ", strip=True)


def _fmt_experiencia(v: str) -> str:
    """Traduce el código de `workExperience` a texto legible.
    Valores observados: 0, 1_2, 3_5, 5_8. Si cambia el vocabulario, pasa tal cual.
    """
    v = (v or "").strip()
    m = re.fullmatch(r"(\d+)_(\d+)", v)
    if m:
        return f"de {m.group(1)} a {m.group(2)} años"
    if v == "0":
        return "sin experiencia"
    return v


class CindeFetchError(RuntimeError):
    """Error consultando CINDE Jobs."""


class CindeOffers(list[dict[str, Any]]):
    def __init__(self, offers: list[dict[str, Any]], *, jobs_skipped: int = 0) -> None:
        super().__init__(offers)
        self.jobs_skipped = jobs_skipped


class CindeFetcher:
    """Consulta la API GraphQL de CINDE Jobs."""

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
        self.per_page = 100

    async def fetch(
        self,
        site: dict[str, Any],
        *,
        pages: int = 10,
        search_text: str | None = None,
        skip_source_job_ids: set[str] | None = None,
    ) -> CindeOffers:
        if isinstance(pages, bool) or not isinstance(pages, int):
            raise ValueError("pages debe ser un entero.")
        if pages < 1 or pages > 20:
            raise ValueError("pages debe estar entre 1 y 20.")

        query = search_text or site.get("site_config", {}).get("search_text") or "Costa Rica"
        skipped_ids = skip_source_job_ids or set()
        offers = []
        skipped = 0
        seen = set()

        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            for page_num in range(1, pages + 1):
                logger.info("CINDE: página %d/%d", page_num, pages)
                try:
                    response = await client.post(
                        API_URL,
                        headers={
                            "Content-Type": "application/json",
                            "User-Agent": "JobPulseCR/1.0 (local; personal)"
                        },
                        json={"query": _QUERY, "variables": {"limit": self.per_page, "page": page_num}},
                    )
                except Exception as e:
                    logger.error("CINDE: error de red página %d: %s", page_num, e)
                    break

                if not (200 <= response.status_code < 300):
                    logger.warning("CINDE: HTTP %d en página %d", response.status_code, page_num)
                    break

                try:
                    data = response.json()
                except ValueError:
                    logger.warning("CINDE: JSON inválido en página %d", page_num)
                    break

                items = (data.get("data") or {}).get("viewJobOffers") or []
                if not items:
                    logger.info("CINDE: sin más resultados en página %d", page_num)
                    break

                for item in items:
                    raw_id = str(item.get("jobOfferId") or item.get("slug") or "")
                    if not raw_id:
                        continue
                    job_id = f"cinde-{raw_id}"
                    if job_id in seen:
                        continue
                    if job_id in skipped_ids:
                        skipped += 1
                        continue
                    seen.add(job_id)

                    title = _strip_html(item.get("nameEs") or item.get("name") or "")
                    if not title:
                        continue

                    # Descripción: versión ES si existe, si no EN
                    description = _strip_html(item.get("descriptionEs") or item.get("description") or "")

                    # Contexto estructurado -> requirements (texto que se vectoriza)
                    ctx = []
                    for campo, et in _CONTEXTO:
                        v = _strip_html(item.get(campo) or "")
                        if campo == "workExperience":
                            v = _fmt_experiencia(v)
                        if v:
                            ctx.append(f"{et}: {v}")
                    vac = item.get("vacancy")
                    if isinstance(vac, int) and vac > 1:
                        ctx.append(f"Vacantes: {vac}")
                    requirements = ". ".join(ctx)

                    # Sector real de la fuente (para filtro por sector)
                    sector = self._normalizar_sector_fuente(item.get("positionCategoryNameEs"))

                    company_slug = item.get("companySlug") or ""
                    slug = item.get("slug") or ""
                    link = _JOB_DETAIL.format(base=BASE_URL, company_slug=company_slug, slug=slug)

                    offers.append({
                        "source_job_id": raw_id,
                        "source_url": link,
                        "source_updated_at": None,
                        "job": {
                            "job_id": job_id,
                            "title": title,
                            "company": _strip_html(item.get("companyName") or ""),
                            "location_raw": _strip_html(item.get("officeName") or ""),
                            "country": "CR",
                            **self._normalize_cr_location(_strip_html(item.get("officeName") or "")),
                            "work_mode": self._normalize_work_mode(item.get("wfhEs")),
                            "employment_type": None,
                            "date_published": None,  # API no expone fecha
                            "expires_at": None,
                            "posting_language": "es",
                            "posting_language_origin": "source",
                            "description_raw": description or None,
                            "requirements_raw": requirements or None,
                            "salary_raw": None,
                            "sector": sector,
                        },
                        "raw_payload": item,
                    })

                if self.request_delay:
                    import asyncio
                    await asyncio.sleep(self.request_delay)

        logger.info("CINDE: %d ofertas extraídas, %d omitidas", len(offers), skipped)
        return CindeOffers(offers, jobs_skipped=skipped)

    @staticmethod
    def _normalize_work_mode(wfh: str | None) -> str:
        if not wfh:
            return "unknown"
        w = wfh.strip().lower()
        if w in ("remoto", "remote", "teletrabajo", "100% remoto"):
            return "remote"
        if w in ("híbrido", "hibrido", "hybrid", "mixto", "semipresencial"):
            return "hybrid"
        if w in ("presencial", "onsite", "oficina", "en sitio"):
            return "onsite"
        return "unknown"

    @staticmethod
    def _normalizar_sector_fuente(category: str | None) -> str | None:
        if not category:
            return None
        cat = category.strip().lower()
        mapping = {
            "tecnología": "Tecnología",
            "tecnologia": "Tecnología",
            "it": "Tecnología",
            "software": "Tecnología",
            "desarrollo": "Tecnología",
            "informática": "Tecnología",
            "informatica": "Tecnología",
            "ventas": "Ventas",
            "comercial": "Ventas",
            "marketing": "Marketing",
            "finanzas": "Finanzas",
            "contabilidad": "Finanzas",
            "recursos humanos": "Recursos Humanos",
            "rrhh": "Recursos Humanos",
            "administración": "Administración",
            "administracion": "Administración",
            "atención al cliente": "Atención al Cliente",
            "atencion al cliente": "Atención al Cliente",
            "customer service": "Atención al Cliente",
            "ingeniería": "Ingeniería",
            "ingenieria": "Ingeniería",
            "manufactura": "Manufactura",
            "logística": "Logística",
            "logistica": "Logística",
            "salud": "Salud",
            "educación": "Educación",
            "educacion": "Educación",
        }
        for k, v in mapping.items():
            if k in cat:
                return v
        return category.strip().title()

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