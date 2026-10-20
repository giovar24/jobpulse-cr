"""Phenom People "canvas": proveedor #7, barrido de sitios SSR confirmados.

Los portales Phenom (careers.{empresa}.com) renderizan server-side su pagina de
resultados: `{base}/{lang}/search-results?keywords=...&from=N` embebe un JSON
(`"jobs":[{...}]` + `"totalHits":N`) con las vacantes de la pagina (10) y el
detalle vive en `{base}/{lang}/job/{reqId}/{Slug}` (SSR tambien, con
`"description"` en HTML y `postedDate` ISO). No hay API privada que usar y no
requiere cookies ni tokens (el x-csrf-token solo lo piden los POST /widgets del
front SPA, no el SSR).

Filosofia igual que Workday: lista curada de sitios confirmados con presencia
CR (data/empresas/phenom_cr.json, generado con sondeo_phenom.py) y el gate
decide. Empresas que NO estan en el directorio Workday ni en los agregadores
(P&G, Zimmer Biomet...).

Nota de solapamiento: HPE y Thermo Fisher usan front Phenom pero el apply va a
su propio Workday -> ya cubiertos por providers/workday.py y se saltan aqui.

Sitios con variante "widgets" (CSS-P con POST /widgets: Cisco, Fiserv, UPS,
Snowflake) quedan pendientes hasta capturar su body exacto.
"""
import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, List

import httpx
from bs4 import BeautifulSoup

from providers.base import BaseProvider, JobOffer

logger = logging.getLogger("jobpulse.phenom")

PAGE_SIZE = 10  # el SSR trae 10 por pagina

# (empresa, base, locale_path); fuente de verdad: data/empresas/phenom_cr.json
SITE_FALLBACK = [
    ("Procter & Gamble (P&G)", "https://www.pgcareers.com", "cr/en"),
    ("Zimmer Biomet", "https://careers.zimmerbiomet.com", "us/en"),
    ("Thermo Fisher Scientific", "https://jobs.thermofisher.com", "global/en"),
    ("Hewlett Packard Enterprise", "https://careers.hpe.com", "us/en"),
]

# front Phenom cuyo apply real es Workday -> ya cubiertos por workday.py
SKIP_EN_WORKDAY = {"hpe", "thermofisher"}


def _marca_host(base: str) -> str:
    """'https://jobs.thermofisher.com' -> 'THERMOFISHER' (2do nivel)."""
    host = base.split("//")[-1].replace("www.", "")
    partes = host.split(".")
    if partes[0] in ("jobs", "careers") and len(partes) > 1:
        partes = partes[1:]
    return partes[0].upper() if partes else host.upper()

_RE_TOTAL = re.compile(r'"totalHits"\s*:\s*(\d+)')
_RE_CR = re.compile(r"costa\s+rica", re.IGNORECASE)


class PhenomProvider(BaseProvider):
    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self._sites = self._cargar_sitios()

    # ------------------------------------------------------------ sitios
    def _cargar_sitios(self) -> List[tuple]:
        """De data/empresas/phenom_cr.json: los con SSR ok y no-workday."""
        path = Path(__file__).resolve().parent.parent.parent / "data" / "empresas" / "phenom_cr.json"
        out = []
        if path.exists():
            try:
                hits = json.loads(path.read_text(encoding="utf-8"))
                for h in hits:
                    base = (h.get("base") or "").rstrip("/")
                    lp = h.get("locale_path") or ""
                    if not base or not lp or h.get("estado"):
                        continue
                    host = _marca_host(base).lower()
                    if host in SKIP_EN_WORKDAY:
                        continue
                    nombre = h.get("empresa") or host
                    out.append((nombre, base, lp))
            except Exception as e:
                logger.warning("phenom_cr.json ilegible (%s): uso fallback", e)
        if not out:
            out = [s for s in SITE_FALLBACK
                   if _marca_host(s[1]).lower() not in SKIP_EN_WORKDAY]
        return out

    # ------------------------------------------------------------ fetch
    async def fetch(self, search_term: str, location: str,
                    pages: int) -> List[JobOffer]:
        """Un barrido por corrida: keywords 'Costa Rica' por sitio, 10/pag."""
        return await asyncio.to_thread(self._sweep, max(1, int(pages or 1)))

    # ------------------------------------------------------------ barrido
    def _sweep(self, pages: int) -> List[JobOffer]:
        offers: List[JobOffer] = []
        with httpx.Client(timeout=30.0, follow_redirects=True) as client:
            for nombre, base, lp in self._sites:
                offers.extend(self._sitio(client, nombre, base, lp, pages))
        logger.info("Phenom: %d vacantes CR totales (antes del gate).", len(offers))
        return offers

    # ------------------------------------------------------------ sitio
    def _sitio(self, client, nombre: str, base: str, lp: str,
               pages: int) -> List[JobOffer]:
        from phenom import PhenomSite, tiene_cr  # reusa el cliente SSR

        ph = PhenomSite(base, client=client)
        ph.locale_path = lp
        host_tag = _marca_host(base)

        vacas: List[JobOffer] = []
        vistos = set()
        for page in range(pages):
            try:
                pag, total = ph.pagina("Costa Rica", page * PAGE_SIZE)
            except Exception as e:
                logger.warning("Phenom list fallo en %s: %s", base, e)
                return vacas
            nuevos = [v for v in pag if v["job_id"] not in vistos]
            if not nuevos:
                break
            for v in nuevos:
                vistos.add(v["job_id"])
                if not tiene_cr(v):
                    continue
                ofertas = self._detalle(client, nombre, base, lp, v, host_tag)
                if ofertas:
                    vacas.append(ofertas)
                    time.sleep(0.15)
            if total and (page + 1) * PAGE_SIZE >= total:
                break
            if len(pag) < PAGE_SIZE:
                break
        if vacas:
            logger.info("Phenom %s: %d vacantes CR", nombre, len(vacas))
        return vacas

    # ------------------------------------------------------------ detalle
    def _detalle(self, client, nombre, base, lp, v, host_tag) -> JobOffer | None:
        req = v.get("req_id") or ""
        if not req:
            return None
        slug = re.sub(r"[^a-zA-Z0-9]+", "-", (v.get("titulo") or "").strip()).strip("-")
        url = f"{base}/{lp}/job/{req}" + (f"/{slug}" if slug else "")
        desc_txt = ""
        fecha = v.get("fecha") or ""
        try:
            r = client.get(url)
            if 200 <= r.status_code < 300:
                t = r.text
                m = re.search(r'"description"\s*:\s*"', t)
                if m:
                    i = t.find('"', m.end() - 1)
                    try:
                        blob, _ = json.JSONDecoder().raw_decode(t, i)
                        desc_txt = BeautifulSoup(blob, "html.parser").get_text(" ", strip=True)
                    except ValueError:
                        pass
                p = re.search(r'"postedDate"\s*:\s*"([^"]+)"', t)
                if p:
                    fecha = p.group(1)
        except Exception as e:
            logger.warning("Phenom detail fallo %s: %s", url, e)
        if not (desc_txt or v.get("teaser")):
            return None
        return JobOffer(
            source="phenom",
            search_term="barrido-phenom",
            job_id=f"phenom-{host_tag}-{req}",
            title=self.clean_text(v.get("titulo") or ""),
            company=self.clean_text(nombre),
            location=self.clean_text(v.get("lugar_txt")
                                     or "; ".join(v.get("ubicaciones") or [])),
            link=url,
            date_published=(fecha[:10] if fecha else None),
            description=self.clean_text(desc_txt or v.get("teaser") or ""),
            requirements="",
            salary="",
        )
