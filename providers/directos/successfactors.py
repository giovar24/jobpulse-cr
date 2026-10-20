"""SuccessFactors (SAP Career Section): proveedor #12.

Patron decodificado (2026-10-19, verificado con Kn+Fondever+EY):

Los portales SAP SuccessFactors "Career Section" (j2w) exponen feed RSS
publico sin key ni cookies:

    GET {base}/services/rss/job/?locale=en_US&keywords=(Costa%20Rica)
    -> XML <rss><channel><item>
        <title>con ubicacion incluida</title>
        <link>/job/{slug}/{id}/</link>
        <pubDate>RFC 2822 (Thu, 08 Oct 2026 ...)</pubDate>
        <description><![CDATA[ HTML completo (3-20 KB) ]]></description>

- El `keywords=` NO es estricto: devuelve tambien paises vecinos que tocan
  la query (K+N trae Santo Domingo DO, EY trae Buenos Aires) -> el provider
  filtra "costa rica"/"heredia"/"san jose"/"cartago"/CR del lado cliente
  sobre title+link+description.
- La descripcion ya viene inline en el RSS: no hace falta GET de detalle.
- Fecha: pubDate RFC 2822 -> ISO con email.utils.

Portales confirmados: Kuehne+Nagel (careers.kuehne-nagel.com), Foundever
(jobs.foundever.com), EY (careers.ey.com). Config en
data/empresas/successfactors_sites.json (fallback constante). Microsoft
(jobs.careers.microsoft.com) NO es j2w: es SPA con API propia deprecada a
404 -> queda fuera (bloqueado, front propio).
"""
import asyncio
import html as _html
import json
import logging
import re
import time
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import unquote

import httpx
from bs4 import BeautifulSoup

from providers.base import BaseProvider, JobOffer

logger = logging.getLogger("jobpulse.successfactors")

# (empresa, base de portal sans barra); fuente de verdad:
# data/empresas/successfactors_sites.json
SITES_FALLBACK = [
    ("Kuehne+Nagel", "https://careers.kuehne-nagel.com"),
    ("Foundever", "https://jobs.foundever.com"),
    ("EY", "https://careers.ey.com"),
]

_RE_ITEM = re.compile(r"<item>(.*?)</item>", re.S)
_RE_TIT = re.compile(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", re.S)
_RE_LINK = re.compile(r"<link>(.*?)</link>", re.S)
_RE_DESC = re.compile(r"<description>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</description>", re.S)
_RE_PUB = re.compile(r"<pubDate>(.*?)</pubDate>", re.S)
_RE_CR = re.compile(r"(?i)(\bcosta\s*rica\b|barreal|\bheredia\b|san\s+jos[eé]|"
                    r"cartago|alajuela|liberia|santa ana|escaz[uú]|moravia|curridabat|"
                    r"\(cr\)|\bcr[,)])")


def _fecha_iso(v: str) -> str | None:
    try:
        dt = parsedate_to_datetime(v.strip())
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return None


class SuccessFactorsProvider(BaseProvider):
    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self._sites = self._cargar_sites()

    # ------------------------------------------------------------ sites
    def _cargar_sites(self) -> List[tuple]:
        path = (Path(__file__).resolve().parent.parent.parent /
                "data" / "empresas" / "successfactors_sites.json")
        out = []
        if path.exists():
            try:
                for s in json.loads(path.read_text(encoding="utf-8")):
                    if s.get("base") and s.get("empresa"):
                        out.append((s["empresa"], s["base"]))
            except Exception as e:
                logger.warning("successfactors_sites.json ilegible (%s): uso fallback", e)
        if not out:
            out = list(SITES_FALLBACK)
        return out

    # ------------------------------------------------------------ fetch
    async def fetch(self, search_term: str, location: str,
                    pages: int) -> List[JobOffer]:
        return await asyncio.to_thread(self._sweep)

    # ------------------------------------------------------------ barrido
    def _sweep(self) -> List[JobOffer]:
        offers: List[JobOffer] = []
        with httpx.Client(timeout=40.0, follow_redirects=True) as client:
            for nombre, base in self._sites:
                offers.extend(self._site(client, nombre, base))
        logger.info("SuccessFactors: %d vacantes CR totales (antes del gate).",
                    len(offers))
        return offers

    # ------------------------------------------------------------ site
    def _site(self, client, nombre: str, base: str) -> List[JobOffer]:
        vacas: List[JobOffer] = []
        try:
            r = client.get(f"{base}/services/rss/job/", params={
                "locale": "en_US", "keywords": "(Costa Rica)"})
        except Exception as e:
            logger.warning("SF %s fallo RSS: %s", base, e)
            return vacas
        if not (200 <= r.status_code < 300):
            logger.warning("SF %s HTTP %s", base, r.status_code)
            return vacas
        t = r.text
        dominio = base.split("//")[-1].replace("www.", "")
        for blob in _RE_ITEM.findall(t):
            tit = _html.unescape((_RE_TIT.search(blob) or [None, ""])[1]).strip()
            link = (_RE_LINK.search(blob) or [None, ""])[1].strip()
            desc_html = _html.unescape((_RE_DESC.search(blob) or [None, ""])[1])
            pub = (_RE_PUB.search(blob) or [None, ""])[1].strip()
            fecha = _fecha_iso(pub)
            link = _html.unescape(link)
            slug = unquote(link)
            # filtro CR client-side (title + link decodificado + desc)
            blob_grp = f"{tit} {slug}"
            desc_txt = BeautifulSoup(desc_html, "html.parser").get_text(
                " ", strip=True) if desc_html else ""
            if not _RE_CR.search(blob_grp):
                # si el titulo no menciona, revisar el pie de la desc
                # (SAP pone "Location: XX" al final del HTML)
                cola = desc_txt[-400:]
                if not _RE_CR.search(cola):
                    continue
            jid_m = re.search(r"/(\d{6,20})/?", slug)
            jid = jid_m.group(1) if jid_m else str(abs(hash(tit)) % 10 ** 10)
            if not (tit or desc_txt):
                continue
            vacas.append(JobOffer(
                source="successfactors",
                search_term="barrido-sf",
                job_id=f"sf-{re.sub(r'[^a-z]', '', dominio)}-{jid}",
                title=self.clean_text(tit),
                company=self.clean_text(nombre),
                location=self.clean_text("Costa Rica"),
                link=link if link.startswith("http") else base + link,
                date_published=fecha,
                description=self.clean_text(desc_txt),
                requirements="",
                salary="",
            ))
        if vacas:
            logger.info("SF %s: %d vacantes CR", dominio, len(vacas))
        return vacas
