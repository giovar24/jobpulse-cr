"""TP (Teleperformance) Umbraco Careers: proveedor #8.

TP es el mayor empleador de la lista de multinacionales pendientes para CR.
Su board real NO es jobs-tp.cr (solo un formulario WP sin vacantes) sino la
API Umbraco de tp.com, descubierta leyendo scripts/modules/careersFilter.js:

    GET https://www.tp.com/Umbraco/Api/Careers/GetCareersBase
        ?node=1780&country=Costa%20Rica&culture=en-us
        &pageSize=50&page=0&accumulated=true
    -> {"resultado": [{title, description(HTML), url, nodeId, country,
                       location, category, language, opportunityType,
                       opportunityModel, workFromHome, source, date}],
        "countFirst": N}

Helpers (por si se quieran listas de filtros):
    /Umbraco/Api/Careers/GetLocationList?country=...   - ubicaciones
    /Umbraco/Api/Careers/GetCategoryList?country=...   - categorias
    /Umbraco/Api/Careers/GetLanguageList               - idiomas

- JSON publico, sin cloudflare (httpx directo), paginado 50/pag.
- `source` puede ser "Umbraco" (evergreen: cuidado, fechas viejas) o "icims"
  (requisiciones RFH frescas; el url viene absoluto de *.icims.com - agregar
  ?mode=job&iis=Teleperformance.com si se quiere).
- El `node` correcto es el 2do argumento del constructor `careersFilter` que
  vive en el HTML de /en-us/careers/job-opportunities/ (el 1er arg es un
  hashCode que cambia). node=1780 verificado 2026-10-19.
- job_id estable: tp-{nodeId} (mas barato que el url, que se duplica por
  cultura: /es-cr/... y /.../... son el mismo posting).

Fechas: el campo `date` viene ISO ("2021-06-29T00:00:00"); los evergreen
traen anos viejos y el gate los deja fuera por ventana (comportamiento
correcto, igual que Workday).
"""
import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List

import httpx
from bs4 import BeautifulSoup

from providers.base import BaseProvider, JobOffer

logger = logging.getLogger("jobpulse.tp")

API = "https://www.tp.com/Umbraco/Api/Careers/GetCareersBase"
PAGE_SIZE = 50

# (empresa, node, culture); fuente de verdad: data/empresas/tp_umbraco_sites.json
# (generable agregando paises; fallback constante si falta)
SITE_FALLBACK = [
    ("Teleperformance Costa Rica", 1780, "en-us"),
]


class TpProvider(BaseProvider):
    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self._sites = self._cargar_sitios()

    # ------------------------------------------------------------ sitios
    def _cargar_sitios(self) -> List[tuple]:
        path = Path(__file__).resolve().parent.parent.parent / "data" / "empresas" / "tp_umbraco_sites.json"
        out = []
        if path.exists():
            try:
                for h in json.loads(path.read_text(encoding="utf-8")):
                    if (h.get("node") or 0) and h.get("empresa"):
                        out.append((h["empresa"], int(h["node"]),
                                    h.get("culture") or "en-us"))
            except Exception as e:
                logger.warning("tp_umbraco_sites.json ilegible (%s): uso fallback", e)
        if not out:
            out = list(SITE_FALLBACK)
        return out

    # ------------------------------------------------------------ fetch
    async def fetch(self, search_term: str, location: str,
                    pages: int) -> List[JobOffer]:
        return await asyncio.to_thread(self._sweep, max(1, int(pages or 1)))

    # ------------------------------------------------------------ barrido
    def _sweep(self, pages: int) -> List[JobOffer]:
        offers: List[JobOffer] = []
        with httpx.Client(timeout=30.0, follow_redirects=True) as client:
            for nombre, node, culture in self._sites:
                offers.extend(self._sitio(client, nombre, node, culture, pages))
        logger.info("TP Umbraco: %d vacantes CR totales (antes del gate).", len(offers))
        return offers

    # ------------------------------------------------------------ sitio
    def _sitio(self, client, nombre: str, node: int, culture: str,
               pages: int) -> List[JobOffer]:
        vacas: List[JobOffer] = []
        vistos = set()
        for page in range(pages):
            r = None
            for intento in range(2):
                try:
                    r = client.get(API, params={
                        "node": node, "country": "Costa Rica", "culture": culture,
                        "pageSize": PAGE_SIZE, "page": page, "accumulated": "true",
                    })
                    break
                except Exception as e:
                    logger.warning("TP list fallo (intento %d): %s", intento + 1, e)
                    time.sleep(1.0)
            if r is None or not (200 <= r.status_code < 300):
                if r is not None:
                    logger.warning("TP API HTTP %s (node=%s pag=%s)",
                                   r.status_code, node, page)
                return vacas
            try:
                data = r.json()
            except ValueError:
                logger.warning("TP API no-JSON en pag %s", page)
                return vacas
            items = data.get("resultado") or []
            nuevos = []
            for x in items:
                k = str(x.get("nodeId") or x.get("url") or x.get("title"))
                if k in vistos:
                    continue
                vistos.add(k)
                nuevos.append(x)
            if not nuevos:
                break
            for x in nuevos:
                vistos.add(str(x.get("nodeId") or x.get("url") or x.get("title")))
                o = self._oferta(nombre, x)
                if o:
                    vacas.append(o)
            if len(items) < PAGE_SIZE:
                break
            time.sleep(0.2)
        if vacas:
            logger.info("TP %s: %d vacantes CR", nombre, len(vacas))
        return vacas

    # ------------------------------------------------------------ oferta
    def _oferta(self, nombre: str, x: dict) -> JobOffer | None:
        titulo = (x.get("title") or "").strip()
        if not titulo:
            return None
        node_id = str(x.get("nodeId") or "")
        url = x.get("url") or ""
        if not url:
            return None
        link = url if url.startswith("http") else f"https://www.tp.com{url}"
        if (x.get("source") or "").lower() == "icims" and "iis=" not in link:
            sep = "&" if "?" in link else "?"
            link = f"{link}{sep}mode=job&iis=Teleperformance.com"
        desc_html = x.get("description") or ""
        desc = ""
        if desc_html:
            desc = BeautifulSoup(desc_html, "html.parser").get_text(" ", strip=True)
        fecha = (x.get("date") or "")[:10] or None
        ubic = "Costa Rica"
        loc = (x.get("location") or "").strip()
        if loc and "costa rica" not in loc.lower():
            ubic = f"{loc}, Costa Rica"
        modelo = (x.get("opportunityModel") or "").strip()
        tipo = (x.get("opportunityType") or "").strip()
        extras = [e for e in (tipo, modelo, x.get("category") or "",
                              (f"{x.get('language') or ''} (idioma demanda)"
                               if x.get("language") else "")) if e]
        desc_full = f"{desc} [{' | '.join(extras)}]" if extras else desc
        return JobOffer(
            source="tp",
            search_term="barrido-tp",
            job_id=f"tp-{node_id}" if node_id else f"tp-{abs(hash(url))}",
            title=self.clean_text(titulo),
            company=self.clean_text(nombre),
            location=self.clean_text(ubic),
            link=link,
            date_published=fecha,
            description=self.clean_text(desc_full),
            requirements="",
            salary="",
        )
