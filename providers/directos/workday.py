"""Workday (myworkdayjobs.com): proveedor #6, barrido de tenants curados.

Los portales Workday exponen la API CXS (publica, la misma que usa el
navegador):
    POST https://{tenant}.wd{n}.myworkdayjobs.com/wday/cxs/{tenant}/{Sitio}/jobs
        {"appliedFacets": {}, "limit": 20, "offset": N, "searchText": "..."}
La LISTA no trae descripcion (limitacion del ATS: sin pay ni depto tampoco);
el DETALLE si: GET .../cxs/{tenant}/{Sitio}/job/{externalPath tras /job/}
devuelve `jobPostingInfo` con `jobDescription` (HTML), `startDate` (fecha
exacta ISO) y `externalUrl`. El gate (core/gate.py) semanticamente requiere
descripcion, asi que el detalle se trae para cada vacante CR.

Filosofia igual que ANE/CINDE: el barrido trae TODO lo que pida "Costa Rica"
por tenant (la lista curada es la fuente de empresa; `workday_cr.py` la
mantiene actualizada) y el gate decide si queda. Ventaja frente a los
agregadores: empresas grandes que NO publican en LinkedIn/Indeed (HPE,
Edwards, TD Synnex, Stryker...), fecha de publicacion exacta y portal propio
(sin muros de login ni 403).

Fuentes de fallos por tenant (403/429/422 en ~0.5% del directorio total) se
saltean con warning; el resto sigue.

Campos de entrada para cada tenant: token "wd{n}/{tenant}/{Sitio}" (el mismo
formato del directorio data/empresas/workday_cr.json).
"""
import asyncio
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, List

import httpx
from bs4 import BeautifulSoup

from providers.base import BaseProvider, JobOffer

logger = logging.getLogger("jobpulse.workday")

# Vacantes CR por tenant al momento de curar (1,362 tableros sondeados, 51 con CR).
# (empresa, "cluster/tenant/Sitio"); fallback si falta data/empresas/workday_cr.json.
TENANT_FALLBACK = [
    ("Hewlett Packard Enterprise", "wd5/hpe/Jobsathpe"),
    ("TD SYNNEX", "wd5/synnex/tdsynnexcareers"),
    ("Edwards Lifesciences", "wd5/edwards/edwardscareers"),
    ("Thermo Fisher Scientific", "wd5/thermofisher/ThermoFisherCareers"),
    ("Abbott", "wd5/abbott/abbottcareers"),
    ("Citi", "wd5/citi/2"),
    ("Johnson & Johnson", "wd5/jj/JJ"),
    ("Stryker", "wd1/stryker/StrykerCareers"),
    ("Huntsman", "wd1/huntsman/Huntsman"),
    ("Four Seasons", "wd3/fourseasons/search"),
    ("Unisys", "wd5/unisys/External"),
    ("Roche", "wd3/roche/roche-ext"),
    ("Clario", "wd1/clarioclinical/clarioclinical_careers"),
    ("Goddard Enterprises", "wd5/goddardenterprisesltd/goddardenteterprisesltd"),
    ("Cardinal Health", "wd1/cardinalhealth/EXT"),
    ("TransPerfect", "wd5/transperfect/transperfect"),
    ("Insulet", "wd5/insulet/insuletcareers"),
    ("Workday", "wd5/workday/Workday"),
    ("Kraft Heinz", "wd1/heinz/KraftHeinz_Careers"),
    ("Medtronic", "wd1/medtronic/MedtronicCareers"),
    ("Johnson Controls", "wd5/jci/JCI"),
    ("Sysco", "wd5/sysco/syscocareers"),
    ("AstraZeneca", "wd3/astrazeneca/Careers"),
    ("Cisco", "wd5/cisco/Cisco_Careers"),
    ("Intel", "wd1/intel/External"),
    ("Ingram Micro", "wd5/ingrammicro/IngramMicro"),
    ("Applied Materials", "wd1/amat/External"),
    ("Blackbaud", "wd1/blackbaud/ExternalCareers"),
    ("Kimberly-Clark", "wd1/kimberlyclark/GLOBAL"),
    ("Syniverse", "wd1/syniverse/SyniverseCareers"),
    ("GSK", "wd3/gsknch/GSKCareers"),
    ("Safeguard Global", "wd3/safeguardglobal/External_Careers"),
    ("Kyndryl", "wd5/kyndryl/KyndrylProfessionalCareers"),
    ("3M", "wd1/3m/Search"),
    ("Autodesk", "wd1/autodesk/Ext"),
    ("Fortrea", "wd1/fortrea/Fortrea"),
    ("Mastercard", "wd1/mastercard/CorporateCareers"),
    ("Solenis", "wd1/solenis/Solenis"),
    ("Accenture", "wd103/accenture/AccentureCareers"),
    ("ABB", "wd3/abb/external_career_page"),
    ("HP Inc.", "wd5/hp/ExternalCareerSite"),
    ("LanguageLine", "wd5/languageline/LLS_Careers"),
    ("Skechers", "wd5/skechers/One-career-site"),
    ("TransUnion", "wd5/transunion/TransUnion"),
    ("Aptiv", "wd5/aptiv/aptiv_careers"),
    ("GSK", "wd5/gsk/GSKCareers"),
    ("MSD", "wd5/msd/SearchJobs"),
    ("Smith+Nephew", "wd5/smithnephew/External"),
    ("ING", "wd3/ing/ICSGBLCOR"),
]

_TOKEN_RE = re.compile(r"costa\s+rica", re.IGNORECASE)
_REQ_ID_RE = re.compile(r"([A-Za-z]{2}\d{6,})[_\-]?\w*$")  # p.ej. _JR0287833

PAGE_SIZE = 20


class WorkdayProvider(BaseProvider):
    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self._tenants = self._cargar_tenants()

    # ------------------------------------------------------------ tenants
    def _cargar_tenants(self) -> List[tuple]:
        """Lista curada: data/empresas/workday_cr.json (si existe) o fallback.

        El nombre amigable se toma de TENANT_FALLBACK cuando el token coincide;
        para tenants nuevos se usa el token del directorio (revisable luego).
        """
        friendly = {t: n for n, t in TENANT_FALLBACK}
        path = Path(__file__).resolve().parent.parent.parent / "data" / "empresas" / "workday_cr.json"
        out = []
        if path.exists():
            try:
                hits = __import__("json").loads(
                    path.read_text(encoding="utf-8"))
                for h in hits:
                    tok = h.get("token") or ""
                    if not re.fullmatch(r"wd[\w-]+/[^/]+/[^/]+", tok):
                        continue
                    nombre = friendly.get(tok) \
                        or " ".join(w.capitalize() for w in tok.split("/")[1].split("-"))
                    out.append((nombre, tok))
                logger.info("Workday: %d tenants desde workday_cr.json", len(out))
            except Exception as e:
                logger.warning("workday_cr.json ilegible (%s): uso fallback", e)
        if not out:
            out = list(TENANT_FALLBACK)
        return out

    # ------------------------------------------------------------ fetch
    async def fetch(self, search_term: str, location: str,
                    pages: int) -> List[JobOffer]:
        """Barrido de TODOS los tenants (ignora `search_term`: decide el gate).

        Se llama UNA vez por corrida, no por tema de busqueda.
        `pages` = tope de paginas (20/registro) POR TENANT con searchText CR.
        """
        return await asyncio.to_thread(
            self._sweep, max(1, int(pages or 1)))

    # ------------------------------------------------------------ barrido
    def _sweep(self, pages: int) -> List[JobOffer]:
        offers: List[JobOffer] = []
        with httpx.Client(timeout=25.0, follow_redirects=True) as client:
            for nombre, token in self._tenants:
                offers.extend(self._tenant(client, nombre, token, pages))
        logger.info("Workday: %d vacantes CR totales (antes del gate).",
                    len(offers))
        return offers

    # ------------------------------------------------------------ tenant
    def _tenant(self, client, nombre: str, token: str,
                pages: int) -> List[JobOffer]:
        cluster, tenant, site = token.split("/")
        origin = f"https://{tenant}.{cluster}.myworkdayjobs.com"
        api = f"{origin}/wday/cxs/{tenant}/{site}"

        # --- 1. listado paginado con searchText CR --------------------
        postings: Dict[str, Any] = {}  # externalPath -> posting
        for page in range(pages):
            try:
                r = client.post(
                    f"{api}/jobs",
                    json={"appliedFacets": {}, "limit": PAGE_SIZE,
                          "offset": page * PAGE_SIZE,
                          "searchText": "Costa Rica"},
                    headers={"Content-Type": "application/json"},
                )
                if not (200 <= r.status_code < 300):
                    logger.warning("Workday list HTTP %s en %s", r.status_code, token)
                    return []
                data = r.json()
            except Exception as e:
                logger.warning("Workday list fallo en %s: %s", token, e)
                return []
            items = data.get("jobPostings") or []
            if not items:
                break
            for it in items:
                ep = it.get("externalPath") or ""
                if ep:
                    postings[ep] = it
            if len(items) < PAGE_SIZE:
                break

        # --- 2. CR real en el texto + detalle (descripcion completa) ---
        ofertas: List[JobOffer] = []
        vistos = set()
        for ep, it in postings.items():
            texto = " ".join([
                it.get("locationsText") or "", it.get("title") or "",
            ])
            if not _TOKEN_RE.search(texto):
                continue
            detalle = self._detalle(client, api, ep)
            jr_id = self._job_id(ep, detalle)
            if jr_id in vistos:
                continue
            vistos.add(jr_id)

            info = (detalle or {}).get("jobPostingInfo") or {}
            titulo = info.get("title") or it.get("title") or ""
            if not titulo:
                continue
            link = info.get("externalUrl") or f"{origin}/{site}{ep}"
            desc = self._limpiar_html(info.get("jobDescription") or "")
            fecha = info.get("startDate") \
                or self._fecha_posted(info.get("postedOn") or it.get("postedOn") or "") \
                or None
            ofertas.append(JobOffer(
                source="workday",
                search_term="barrido-workday",
                job_id=jr_id,
                title=self.clean_text(titulo),
                company=self.clean_text(nombre),
                location=self.clean_text(info.get("location") or it.get("locationsText") or ""),
                link=link,
                date_published=fecha,
                description=desc,
                requirements="",
                salary="",
            ))
            time.sleep(0.15)  # amable con los portales propios
        if ofertas:
            logger.info("Workday %s: %d vacantes CR", token, len(ofertas))
        return ofertas

    # ------------------------------------------------------------ helpers
    def _detalle(self, client, api: str, ep: str) -> dict:
        """GET .../job/{externalPath tras /job/} -> jobPostingInfo."""
        token = (ep or "").split("/job/")[-1]
        if not token:
            return {}
        try:
            r = client.get(f"{api}/job/{token}")
            if not (200 <= r.status_code < 300):
                return {}
            return r.json() or {}
        except Exception:
            return {}

    def _job_id(self, ep: str, detalle: dict | None) -> str:
        """ID estable: requisito tipo JR0287833 de la ruta; fallback a path."""
        info = (detalle or {}).get("jobPostingInfo") or {}
        jid = str(info.get("jobPostingId") or "").strip()
        if jid:
            return f"workday-{jid}"
        m = _REQ_ID_RE.search((ep or "").split("/")[-1])
        if m and m.group(1):
            return f"workday-{m.group(1)}"
        return f"workday-{abs(hash(ep)) % 10**12}"

    @staticmethod
    def _fecha_posted(txt: str) -> str:
        """'Posted 7 Days Ago' -> ISO via core.dates (respeta postedOn EN)."""
        if not txt:
            return ""
        from core.dates import parse_date
        iso = parse_date(txt)
        try:
            from datetime import date as _d
            _d.fromisoformat(iso[:10])
            return iso[:10]
        except ValueError:
            return ""

    @staticmethod
    def _limpiar_html(html: str) -> str:
        if not html:
            return ""
        return BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
