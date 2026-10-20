"""CINDE Jobs (cindejobs.com): tablero publico de empleo de CINDE.

El sitio carga las ofertas con Apollo Client contra su API GraphQL publica
(https://api.cindejobs.com/graphql), asi que no hace falta navegador ni
credenciales: la consulta `viewJobOffers` paginada trae todo el tablero
activo (titulos, empresa, oficina, descripcion, requisito, lenguas...).

Como ANE, el barrido ignora el termino de busqueda y trae TODAS las
vacantes activas de una pasada: el gate (core/gate.py) es quien decide
por relevancia semantica contra el perfil.

El esquema no expone fecha de publicacion (tablero "abierto 24/7"): cada
oferta activa se considera vigente y la fecha queda vacia. El gate trata
las fechas no parseables como "beneficio de la duda" y solo filtra por
relevancia.
"""
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
  }
}
"""


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
                    details = [d for d in (
                        _strip_html(item.get("academicLevelRequiredEs") or ""),
                        _strip_html(item.get("languagesEs") or ""),
                    ) if d]

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
                        # El API no expone fecha de publicacion.
                        date_published=None,
                        description=description,
                        requirements="",
                        salary="",
                        raw_data={
                            **item,
                            # Complemento de la descripcion: modalidad y datos
                            # estructurados que no caben en otros campos.
                            "_adicional": " | ".join(details),
                        },
                    ))
        return offers
