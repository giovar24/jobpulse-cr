"""Rippling ATS (ats.rippling.com): proveedor #13.

Patron decodificado (2026-10-19, verificado con 8 boards):

Los boards de Rippling ats.rippling.com/{slug}/jobs renderizan el listado
SERVER-SIDE en el HTML (Next.js): cada job post trae un JSON embebido
"title" -> "url" -> department -> "locations":[{"name","countryCode"}].
No hace falta API interna ni JS: se parsea directo del HTML.

    "title":"Senior Software Engineer","url":"https://ats.rippling.com/
    commandlink/jobs/{uuid}","department":{"name":...},"locations":[
      {"name":"Costa Rica","countryCode":"CR",...}, ...]

- Filtro CR: countryCode == "CR" / "Costa Rica" en las locations del post.
  (el HTML tambien trae lists de "paises de hiring" para el dropdown -> NO
  filtrar por texto libre del HTML: hay que ir registro por registro,
  ligando title+locations del MISMO jobPost.)
- Detalle: GET /{slug}/jobs/{uuid} -> __NEXT_DATA__ JSON ->
  apiData.jobPost.description.{company, role} (HTML con <p>/<ul>) +
  createdOn (ISO con TZ), employmentType.label, workLocations, videojuegos
  department.name -> parsear con BS4.
- Fuente de scan del directorio completo (864 boards, veniam "Costa Rica"
  en HTML): data/empresas/rippling_scan_cr.json -> 10 tableros con CR real.
  OJO: varios jobs de commandlink/apex NO aparecen en el scan regex v1 del
  listado (los jobs siempre traen *+locations* embebido).

Sin provider de paginacion: boards de Rippling listan Tod(vac)as las
vacantes del board en una sola pagina.
"""

import asyncio
import html as _html
import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List

import httpx
from bs4 import BeautifulSoup

from providers.base import BaseProvider, JobOffer

logger = logging.getLogger("jobpulse.rippling")

# (empresa, slug); fuente de verdad:
# data/empresas/rippling_boards.json (scan 2026-10-19 con CR real)
BOARDS_FALLBACK = [
    ("CommandLink", "commandlink"),
    ("Aubrant Digital", "epic-software"),
    ("Havenly", "havenlybrands"),
    ("InAir", "inair"),
    ("Lisinski Law Firm", "lisinski-law-firm"),
    ("Pythian", "pythian"),
    ("Sparq", "sparq"),
    ("Venn", "venncareers"),
]

_RE_REG = re.compile(
    r'"(?:title|name)":"(?P<tit>[^"]{3,140})","url":"https://ats\.rippling\.com/'
    r'(?P<tok>[^/"]+)/jobs/(?P<uid>[0-9a-f-]{36})"(?P<blob>.{0,6000}?)\](?=<|"|,)', re.S)
_RE_LOC = re.compile(
    r'\{"name":"([^"]+)","country":"[^"]*","countryCode":"(\w{2})"')
_RE_REM = re.compile(r'"workplaceType":"REMOTE"')

_RE_NEXT = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)


class RipplingProvider(BaseProvider):
    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self._boards = self._cargar_boards()

    def _cargar_boards(self) -> List[tuple]:
        path = (Path(__file__).resolve().parent.parent.parent /
                "data" / "empresas" / "rippling_boards.json")
        out = []
        if path.exists():
            try:
                for b in json.loads(path.read_text(encoding="utf-8")):
                    if b.get("slug") and b.get("empresa"):
                        out.append((b["empresa"], b["slug"]))
            except Exception as e:
                logger.warning("rippling_boards.json ilegible (%s): uso fallback", e)
        if not out:
            out = list(BOARDS_FALLBACK)
        return out

    async def fetch(self, search_term: str, location: str,
                    pages: int) -> List[JobOffer]:
        return await asyncio.to_thread(self._sweep)

    def _sweep(self) -> List[JobOffer]:
        offers: List[JobOffer] = []
        with httpx.Client(timeout=40.0, follow_redirects=True) as client:
            for nombre, slug in self._boards:
                offers.extend(self._board(client, nombre, slug))
        logger.info("Rippling: %d vacantes CR totales (antes del gate).", len(offers))
        return offers

    # ------------------------------------------------------------ board
    def _board(self, client, nombre: str, slug: str) -> List[JobOffer]:
        vacas: List[JobOffer] = []
        try:
            r = client.get(f"https://ats.rippling.com/{slug}/jobs")
        except Exception as e:
            logger.warning("Rippling %s fallo GET: %s", slug, e)
            return vacas
        if not (200 <= r.status_code < 300):
            logger.warning("Rippling %s HTTP %s", slug, r.status_code)
            return vacas
        for m in _RE_REG.finditer(r.text):
            if m.group("tok") != slug:
                continue
            tit = _html.unescape(m.group("tit"))
            uid = m.group("uid")
            # locations del MISMO registro (hasta el "]")
            blob = m.group("blob")
            if blob.find('"url"') == 0:
                continue
            locs = [(_html.unescape(a), c) for a, c in _RE_LOC.findall(blob)]
            crs = [a for a, c in locs if c == "CR" or
                   re.search(r"(?i)costa\s*rica", a)]
            if not crs:
                continue
            remote = '"workplaceType":"REMOTE"' in blob
            ubic = "; ".join(dict.fromkeys(crs))
            if remote and len(locs) > len(crs):
                ubic += " (remote)"
            o = self._oferta(client, nombre, slug, uid, tit, ubic)
            if o:
                vacas.append(o)
        if vacas:
            logger.info("Rippling %s: %d vacantes CR", slug, len(vacas))
        return vacas

    # ------------------------------------------------------------ detalle
    def _oferta(self, client, nombre: str, slug: str, uid: str,
                titulo: str, ubic: str) -> JobOffer | None:
        url = f"https://ats.rippling.com/{slug}/jobs/{uid}"
        desc = ""
        fecha = None
        dept = ""
        try:
            r = client.get(url)
            if 200 <= r.status_code < 300:
                m = _RE_NEXT.search(r.text)
                if m:
                    try:
                        jp = (json.loads(m.group(1))["props"]["pageProps"]
                              ["apiData"]["jobPost"])
                    except Exception:
                        jp = {}
                    if jp.get("name"):
                        titulo = jp["name"]
                    ds = jp.get("description") or {}
                    partes = []
                    for k in ("role", "company"):
                        v = ds.get(k) or ""
                        if v:
                            partes.append(BeautifulSoup(v, "html.parser").get_text(
                                " ", strip=True))
                    desc = " ".join(partes)
                    dept = (jp.get("department") or {}).get("name") or ""
                    fecha = (jp.get("createdOn") or "")[:10] or None
                    et = ((jp.get("employmentType") or {}).get("label") or "")
                    if et:
                        desc = f"{desc} [{et}]"
        except Exception as e:
            logger.warning("Rippling detail %s: %s", uid, e)
        if not (desc or titulo):
            return None
        return JobOffer(
            source="rippling",
            search_term="barrido-rippling",
            job_id=f"rippling-{slug}-{uid[:8]}",
            title=self.clean_text(titulo),
            company=self.clean_text(nombre),
            location=self.clean_text(ubic) + (f" | {dept}" if dept else ""),
            link=url,
            date_published=fecha,
            description=self.clean_text(desc),
            requirements="",
            salary="",
        )
