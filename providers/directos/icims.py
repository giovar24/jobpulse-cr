"""iCIMS (icims.com): proveedor #10, boards de Teleperformance y DocuSign.

Patron decodificado (2026-10-19, verificado con TP y DocuSign):
- Listado: `GET https://{host}/jobs/search?search=true&in_iframe=1`
  -> HTML server-side con `<li class="iCIMS_JobCardItem">` por vacante:
     - ubicacion en "Job Locations" (p.ej. "MY-Kuala Lumpur-Kuala Lumpur")
     - fecha de post en `title="M/D/YYYY H:MM AM/PM"` (tabla "Posted Date")
     - titulo en el href `.../jobs/{id}/{slug}/job?in_iframe=1` (class iCIMS_Anchor)
- Paginacion: `&pr=N` (N=1,2,...): 50 vacantes NUEVAS por pagina. OJO: el
  param `pageNum` NO pagina (devuelve siempre la misma pag).
- NO hay filtro keyword por GET (el form es POST con `hashed=` por pagina):
  se barre paginado y se filtra "costa rica" del lado del proveedor.
- Detalle: GET {href}/job?in_iframe=1 -> HTML con la descripcion
  (contenedor iCIMS_JobDescription o equivalente).
- El filtro keyword por GET (`&in_0=...`, `&searchKeyword=...`) no filtra
  (probado y descartado).

Boards: DocuSign redirige a los boards regionales pero los "root"/us/emea
responden 200 SIN filas (su front se mudo a SPA propia); el que vale es el
latam + tp global. setup en data/empresas/icims_boards.json (fallback const).
"""
import asyncio
import html as _html
import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

import httpx
from bs4 import BeautifulSoup

from providers.base import BaseProvider, JobOffer

logger = logging.getLogger("jobpulse.icims")

PAGE_SIZE = 50
_RE_CR = re.compile(r"\bcosta\s*rica\b", re.I)

# (empresa, host completo de board); fuente de verdad:
# data/empresas/icims_boards.json  (fallback si falta)
BOARD_FALLBACK = [
    ("Teleperformance", "careers-teleperformance.icims.com"),
    ("Teleperformance USA", "careersus-teleperformance.icims.com"),
    ("Teleperformance (global EN)", "careerseng-teleperformance.icims.com"),
    ("DocuSign LatAm", "latamcareers-docusign.icims.com"),
    ("DocuSign USA", "uscareers-docusign.icims.com"),
    ("DocuSign EMEA", "emeacareers-docusign.icims.com"),
]

_RE_FILA = re.compile(r'<li class="iCIMS_JobCardItem">(.*?)</li>', re.S)
_RE_HREF = re.compile(r'href="([^"]*/jobs/(\d+)/[^"]*/job[^"]*)"')
_RE_LOC = re.compile(r'Job Locations\s*</span>\s*<span[^>]*>([^<]+)', re.S)
_RE_FEC = re.compile(r'title="(\d{1,2}/\d{1,2}/\d{4}[^"]*)"')
_RE_TIT = re.compile(r"<h3[^>]*>([^<]+)</h3>")


def _fecha_iso(v: str) -> str | None:
    try:
        return datetime.strptime(v.strip()[:20], "%m/%d/%Y %I:%M %p").strftime("%Y-%m-%d")
    except ValueError:
        try:
            return datetime.strptime(v.strip()[:10], "%m/%d/%Y").strftime("%Y-%m-%d")
        except ValueError:
            return None


class IcimsProvider(BaseProvider):
    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self._boards = self._cargar_boards()

    # ------------------------------------------------------------ boards
    def _cargar_boards(self) -> List[tuple]:
        path = Path(__file__).resolve().parent.parent.parent / "data" / "empresas" / "icims_boards.json"
        out = []
        if path.exists():
            try:
                for b in json.loads(path.read_text(encoding="utf-8")):
                    if b.get("host") and b.get("empresa"):
                        out.append((b["empresa"], b["host"]))
            except Exception as e:
                logger.warning("icims_boards.json ilegible (%s): uso fallback", e)
        if not out:
            out = list(BOARD_FALLBACK)
        return out

    # ------------------------------------------------------------ fetch
    async def fetch(self, search_term: str, location: str,
                    pages: int) -> List[JobOffer]:
        return await asyncio.to_thread(self._sweep, max(1, int(pages or 8)))

    # ------------------------------------------------------------ barrido
    def _sweep(self, pages: int) -> List[JobOffer]:
        offers: List[JobOffer] = []
        with httpx.Client(timeout=30.0, follow_redirects=True) as client:
            for nombre, host in self._boards:
                offers.extend(self._board(client, nombre, host, pages))
        logger.info("iCIMS: %d vacantes CR totales (antes del gate).", len(offers))
        return offers

    # ------------------------------------------------------------ board
    def _board(self, client, nombre: str, host: str, pages: int) -> List[JobOffer]:
        base = f"https://{host}"
        vacas: List[JobOffer] = []
        vistos = set()
        for pr in range(pages):
            url = base + "/jobs/search?search=true&in_iframe=1"
            if pr:
                url += f"&pr={pr}"
            try:
                r = client.get(url)
            except Exception as e:
                logger.warning("iCIMS %s fallo GET (pr=%s): %s", host, pr, e)
                return vacas
            if not (200 <= r.status_code < 300):
                logger.warning("iCIMS %s HTTP %s", host, r.status_code)
                return vacas
            nuevos = []
            for blob in _RE_FILA.findall(r.text):
                href_m = _RE_HREF.search(blob)
                if not href_m:
                    continue
                href, jid = href_m.group(1), href_m.group(2)
                if jid in vistos:
                    continue
                vistos.add(jid)
                loc = _RE_LOC.search(blob)
                fec = _RE_FEC.search(blob)
                tit = _RE_TIT.search(blob)
                nuevos.append({
                    "jid": jid, "href": href,
                    "loc": (_html.unescape(loc.group(1)).strip() if loc else ""),
                    "fecha": (fec.group(1) if fec else ""),
                    "titulo": (" ".join(_html.unescape(tit.group(1)).split())
                               if tit else ""),
                })
            if not nuevos:
                break
            for row in nuevos:
                blob = f"{row['titulo']} {row['loc']}"
                if not _RE_CR.search(blob):
                    continue
                o = self._oferta(nombre, host, row, client)
                if o:
                    vacas.append(o)
                    time.sleep(0.15)
            if len(nuevos) < PAGE_SIZE:
                break
            time.sleep(0.2)
        if vacas:
            logger.info("iCIMS %s: %d vacantes CR", host, len(vacas))
        else:
            logger.info("iCIMS %s: barre %d pag sin CR", host, min(pages, pr + 1))
        return vacas

    # ------------------------------------------------------------ detalle
    def _oferta(self, nombre: str, host: str, row: dict,
                client: httpx.Client) -> JobOffer | None:
        href = row["href"]
        if href.startswith("/"):
            href = f"https://{host}{href}"
        desc = ""
        loc_det = ""
        try:
            r = client.get(href)
            if 200 <= r.status_code < 300:
                t = r.text
                m = (re.search(r'class="iCIMS_JobDescription"[^>]*>(.*?)<div class=', t, re.S)
                     or re.search(r'id="iCIMS_JobDescription"[^>]*>(.*?)</div>', t, re.S)
                     or re.search(r"(?is)<div class=\"ck-?editor[^\"]*\">(.*?)</div>", t))
                if m:
                    txt = BeautifulSoup(_html.unescape(m.group(1)),
                                        "html.parser").get_text(" ", strip=True)
                    desc = txt
                ld = re.search(r"(?i)Job Locations?\s*</span>\s*</span>\s*<[^>]*>\s*([^<]{3,120})", t) \
                     or re.search(r"(?i)Job Locations?\s*<[^>]*>\s*<[^>]*>([^<]{3,120})", t)
                if ld:
                    loc_det = _html.unescape(ld.group(1)).strip()
        except Exception as e:
            logger.warning("iCIMS detail fallo %s: %s", href, e)
        if not (desc or row["titulo"]):
            return None
        fecha = _fecha_iso(row.get("fecha") or "")
        ubic = loc_det or row.get("loc") or "Costa Rica"
        return JobOffer(
            source="icims",
            search_term="barrido-icims",
            job_id=f"icims-{host.split('-')[-1].split('.')[0]}-{row['jid']}",
            title=self.clean_text(row.get("titulo") or ""),
            company=self.clean_text(nombre),
            location=self.clean_text(ubic),
            link=href,
            date_published=fecha,
            description=self.clean_text(desc),
            requirements="",
            salary="",
        )
