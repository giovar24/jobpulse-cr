"""CINDE Jobs (cindejobs.com): tablero publico de empleo de CINDE.

El sitio carga las ofertas con Apollo Client contra su API GraphQL publica
(https://api.cindejobs.com/graphql), asi que no hace falta navegador ni
credenciales: la consulta `viewJobOffers` paginada trae todo el tablero
activo (titulos, empresa, oficina, descripcion, requisito, lenguas...).

Como ANE, el barrido ignora el termino de busqueda y trae TODAS las
vacantes activas de una pasada: el gate (core/gate.py) es quien decide
por relevancia semantica contra el perfil.

El esquema no expone fecha de publicacion: verificado por introspeccion
(2026-10-20) del esquema GraphQL — `ViewJobOffer` expone 43 campos y NINGUNO
de fecha, y la pagina publica del detalle tampoco la muestra. Cada oferta
activa se considera vigente y la fecha queda vacia. El gate trata las fechas
no parseables como "beneficio de la duda" y solo filtra por relevancia.

Los campos estructurados (categoria, nivel, experiencia, modalidad, idiomas,
vacantes) van a `requirements`: son el contexto que el embedding usa para
separar "Remoto"/"Nivel profesional" de la descripcion general.
"""
import re

import httpx
from typing import List, Dict, Any
from bs4 import BeautifulSoup
from providers.base import BaseProvider, JobOffer


API_URL = "https://api.cindejobs.com/graphql"
BASE_URL = "https://cindejobs.com"

# Pagina de detalle publica confirmada del sitio: /en/fair/{company}/{slug}
_JOB_DETAIL = "{base}/en/fair/{company_slug}/{slug}"


def _strip_html(text: str) -> str:
    if not text:
        return ""
    return BeautifulSoup(text, "html.parser").get_text(" ", strip=True)


def _fmt_experiencia(v: str) -> str:
    """Traduce el codigo de `workExperience` a texto legible.

    El API devuelve codigos, no rangos: medido en las 19 ofertas activas
    (2026-10-20) los unicos valores eran 0, 1_2, 3_5 y 5_8. Sin traducir,
    el texto vectorizado se llenaba de "Experiencia: 1_2" (ruido que no
    aporta nada al embedding). Lo no reconocido pasa tal cual: si el API
    cambia el vocabulario no se pierde informacion.
    """
    v = (v or "").strip()
    m = re.fullmatch(r"(\d+)_(\d+)", v)
    if m:
        return f"de {m.group(1)} a {m.group(2)} años"
    if v == "0":
        return "sin experiencia"
    return v


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

# Campos estructurados del API que NO son la descripcion pero dan contexto al
# embedding (categoria, nivel, experiencia, modalidad, vacantes, idiomas).
# Antes iban a raw_data, que core/database.py NO persiste -> se tiraban.
_CONTEXTO = (
    ("positionCategoryNameEs", "Categoría"),
    ("positionLevelNameEs", "Nivel"),
    ("workExperience", "Experiencia"),
    ("academicLevelRequiredEs", "Formación"),
    ("languagesEs", "Idiomas"),
    ("wfhEs", "Modalidad"),
)


class CindeProvider(BaseProvider):
    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.per_page = 100

    async def fetch(self, search_term: str, location: str, pages: int) -> List[JobOffer]:
        offers: List[JobOffer] = []
        seen = set()
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            for page in range(1, pages + 1):
                response = await client.post(
                    API_URL,
                    headers={"Content-Type": "application/json", "User-Agent": "JobPulseCR/1.0 (local; personal)"},
                    json={"query": _QUERY, "variables": {"limit": self.per_page, "page": page}},
                )
                if not (200 <= response.status_code < 300):
                    break

                try:
                    data = response.json()
                except ValueError:
                    break

                items = (data.get("data") or {}).get("viewJobOffers") or []
                if not items:
                    break

                for item in items:
                    # job_id es PRIMARY KEY global: el prefijo evita colisionar
                    # con los ids numericos de Procomer/ANE.
                    raw_id = str(item.get("jobOfferId") or item.get("slug") or "")
                    if not raw_id:
                        continue
                    job_id = f"cinde-{raw_id}"
                    if job_id in seen:
                        continue
                    seen.add(job_id)

                    title = self.clean_text(item.get("nameEs") or item.get("name") or "")
                    if not title:
                        continue

                    # Descripcion unica: version ES si existe, si no EN.
                    # Nota: technicalSkillsRequired/features del API repiten el
                    # mismo HTML de la descripcion (datos crudos); no se
                    # duplican en requirements para no hinchar el texto.
                    description = _strip_html(item.get("descriptionEs") or item.get("description") or "")

                    # Contexto estructurado -> requirements. Va al texto que se
                    # vectoriza (core/search_engine.build_doc_text), asi que
                    # le da al gate senal que la descripcion sola no trae
                    # ("Remoto", "Nivel profesional", "Ingles").
                    ctx = []
                    for campo, et in _CONTEXTO:
                        v = _strip_html(item.get(campo) or "")
                        if campo == "workExperience":
                            v = _fmt_experiencia(v)   # '1_2' -> 'de 1 a 2 años'
                        if v:
                            ctx.append(f"{et}: {v}")
                    vac = item.get("vacancy")
                    if isinstance(vac, int) and vac > 1:
                        ctx.append(f"Vacantes: {vac}")
                    requirements = ". ".join(ctx)

                    # Sector real de la fuente (Categoría del API); se guarda
                    # en la columna sector y alimenta el filtro por sector.
                    from core.sectores import normalizar_sector_fuente
                    sector = normalizar_sector_fuente(item.get("positionCategoryNameEs"))

                    offers.append(JobOffer(
                        source="cinde",
                        search_term=search_term,
                        job_id=job_id,
                        title=title,
                        company=self.clean_text(item.get("companyName") or ""),
                        location=self.clean_text(item.get("officeName") or ""),
                        link=_JOB_DETAIL.format(
                            base=BASE_URL,
                            company_slug=item.get("companySlug") or "",
                            slug=item.get("slug") or "",
                        ),
                        # El API no expone fecha de publicacion (verificado por
                        # introspeccion del esquema: ViewJobOffer tiene 43
                        # campos y ninguno de fecha; la web publica tampoco).
                        date_published=None,
                        description=description,
                        requirements=requirements,
                        salary="",
                        sector=sector,
                    ))
        return offers
