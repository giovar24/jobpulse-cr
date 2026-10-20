"""SmartRecruiters (api.smartrecruiters.com): proveedor #11.

API publica documentada del vendor (sin key):

    GET https://api.smartrecruiters.com/v1/companies/{cid}/postings
        ?q=Costa%20Rica&limit=100&offset=0
    -> {"totalFound": N, "content": [{id, name, releasedDate, location{city,
        region, country, address}, postingUrl, applyUrl, function,
        experienceLevel, typeOfEmployment, customField...}]}

- `q` busca server-side (p.ej. `q=Costa Rica` encontro la CR de Bosch aunque
  el titulo no menciona el pais -> busca en los datos del posting).
- Detalle (descripcion por secciones HTML):
    GET .../postings/{id} -> jobAd.sections = {titulo: {title, text(HTML)}}
- Fecha: releasedDate ISO completo (usar [:10]).
- ids decodificados (vendor case-sensitive y con guion): westerndigital y
  BoschGroup (ceil, probados 2026-10-19). "western-digital"/"bosch" dan 0.

Fuente de companias: data/empresas/smartrecruiters_companies.json
(fallback constante). Nota: Western Digital tenia CR en el barrido del
directorio pero hoy el q no devuelve CR; BoschGroup tiene 1 (Heredia).
"""
import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Dict, List

import httpx
from bs4 import BeautifulSoup

from providers.base import BaseProvider, JobOffer

logger = logging.getLogger("jobpulse.smartrecruiters")

API = "https://api.smartrecruiters.com/v1/companies/{cid}/postings"
PAGE_SIZE = 100

# (empresa, cid interno); fuente de verdad:
# data/empresas/smartrecruiters_companies.json
COMPANY_FALLBACK = [
    ("Western Digital", "westerndigital"),
    ("Bosch", "BoschGroup"),
]


class SmartRecruitersProvider(BaseProvider):
    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self._companies = self._cargar_companies()

    # ------------------------------------------------------------ companias
    def _cargar_companies(self) -> List[tuple]:
        path = (Path(__file__).resolve().parent.parent.parent /
                "data" / "empresas" / "smartrecruiters_companies.json")
        out = []
        if path.exists():
            try:
                for c in json.loads(path.read_text(encoding="utf-8")):
                    if c.get("cid") and c.get("empresa"):
                        out.append((c["empresa"], c["cid"]))
            except Exception as e:
                logger.warning("smartrecruiters_companies.json ilegible (%s): "
                               "uso fallback", e)
        if not out:
            out = list(COMPANY_FALLBACK)
        return out

    # ------------------------------------------------------------ fetch
    async def fetch(self, search_term: str, location: str,
                    pages: int) -> List[JobOffer]:
        return await asyncio.to_thread(self._sweep, max(1, int(pages or 10)))

    # ------------------------------------------------------------ barrido
    def _sweep(self, pages: int) -> List[JobOffer]:
        offers: List[JobOffer] = []
        with httpx.Client(timeout=40.0, follow_redirects=True) as client:
            for nombre, cid in self._companies:
                offers.extend(self._company(client, nombre, cid, pages))
        logger.info("SmartRecruiters: %d vacantes CR totales (antes del gate).",
                    len(offers))
        return offers

    # ------------------------------------------------------------ company
    def _company(self, client, nombre: str, cid: str, pages: int) -> List[JobOffer]:
        vacas: List[JobOffer] = []
        vistos = set()
        offset = 0
        for page in range(pages):
            try:
                r = client.get(API.format(cid=cid), params={
                    "q": "Costa Rica", "limit": PAGE_SIZE, "offset": offset})
            except Exception as e:
                logger.warning("SR %s fallo GET (off=%s): %s", cid, offset, e)
                return vacas
            if not (200 <= r.status_code < 300):
                logger.warning("SR %s HTTP %s", cid, r.status_code)
                return vacas
            try:
                data = r.json()
            except ValueError:
                logger.warning("SR %s no-JSON", cid)
                return vacas
            items = data.get("content") or []
            nuevos = [j for j in items if str(j.get("id")) not in vistos]
            if not nuevos:
                break
            for j in nuevos:
                vistos.add(str(j.get("id")))
                o = self._oferta(client, nombre, cid, j)
                if o:
                    vacas.append(o)
            offset += PAGE_SIZE
            if len(items) < PAGE_SIZE:
                break
        if vacas:
            logger.info("SR %s: %d vacantes CR", cid, len(vacas))
        return vacas

    # ------------------------------------------------------------ oferta
    def _oferta(self, client, nombre: str, cid: str, j: dict) -> JobOffer | None:
        import re
        jid = str(j.get("id") or "")
        blob = str(j.get("name") or "") + " " + str(j.get("location") or "") + \
               " " + str(j.get("customField") or "")
        if not jid or not re.search(r"costa\s*rica", blob, re.IGNORECASE):
            return None
        loc = j.get("location") or {}
        city = loc.get("city") or ""
        region = loc.get("region") or ""
        pais = (loc.get("country") or "").upper()
        ubic = ", ".join(x for x in (city, region) if x) or "Costa Rica"
        if pais and pais != "CR":
            ubic = f"{ubic} ({pais})"
        # detalle con las secciones de descripcion
        desc = ""
        try:
            r = client.get(f"{API.format(cid=cid)}/{jid}", timeout=40.0)
            if 200 <= r.status_code < 300:
                ja = (r.json().get("jobAd") or {}).get("sections") or {}
                partes = []
                for sec in ja.values():
                    tit = sec.get("title") or ""
                    html = sec.get("text") or ""
                    if not html:
                        continue
                    texto = BeautifulSoup(html, "html.parser").get_text(
                        " ", strip=True)
                    partes.append((f"{tit}: {texto}" if tit else texto))
                desc = " ".join(partes)
        except Exception as e:
            logger.warning("SR detail fallo %s/%s: %s", cid, jid, e)
        if not (desc or j.get("name")):
            return None
        fecha = (j.get("releasedDate") or "")[:10] or None
        tipo = ((j.get("typeOfEmployment") or {}).get("label")
                if isinstance(j.get("typeOfEmployment"), dict) else None)
        func = ((j.get("function") or {}).get("label")
                if isinstance(j.get("function"), dict) else None)
        extras = [x for x in (func, tipo) if x]
        desc_full = f"{desc} [{' | '.join(extras)}]" if extras else desc
        link = j.get("postingUrl") or j.get("applyUrl") or \
               f"https://jobs.smartrecruiters.com/{cid}/{jid}"
        return JobOffer(
            source="smartrecruiters",
            search_term="barrido-sr",
            job_id=f"sr-{cid}-{jid}",
            title=self.clean_text(j.get("name") or ""),
            company=self.clean_text(nombre),
            location=self.clean_text(ubic),
            link=link,
            date_published=fecha,
            description=self.clean_text(desc_full),
            requirements="",
            salary="",
        )
