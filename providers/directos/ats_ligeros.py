"""ATS ligeros del directorio (Ashby / Lever / Recruitee / Workable): #14.

Todos exponen API publica sin key; los tokens venienen del barrido del
directorio (otros_ats_cr.json, 8 plataformas con CR pre-scrapeado, de ahi el
nombres "ligeros": 1-2 GET por tablero):

- **Ashby**   GET https://api.ashbyhq.com/posting-api/job-board/{tok}
              -> {"jobs":[{id,title,location,secondaryLocations,publishedAt,
                          jobUrl,descriptionHtml,descriptionPlain...
                          (isRemote, workplaceType = REMOTE/HYBRID)}]}
- **Lever**   GET https://api.lever.co/v0/postings/{tok}?mode=json  (o api.eu)
              -> lista [{text,country ("CR"),createdAt epoch, hostedUrl,
                         description(tx), others]} (NOTA eu: boards que
                         vienen de jobs.eu.lever.co usan api.eu.lever.co)
- **Recruitee** GET https://{biz}.recruitee.com/api/offers/
              -> {"offers":[{position, location, published_at (str utc),
                            careers_url, requirements}]}
- **Workable** POST https://apply.workable.com/api/v1/accounts/{tok}/jobs
              con headers `X-Workable-Shortcode: {tok}` (PAGINADO de 10/pag
              con el header `X-Workable-Page: {cursor}` de la resp
              `nextPage`) -> results [{id, title, location{countryCode CR},
              published, department...}]; detail para desc:
              GET .../jobs/{shortcode} ( contiene description 5-10 KB).

Filtro CR por campo exacto: ashby (location/secondaryLocations/title),
lever (country), recruitee (location/title), workable (countryCode==CR
o "Costa Rica" en locations).
"""
import asyncio
import html as _html
import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import httpx
from bs4 import BeautifulSoup

from providers.base import BaseProvider, JobOffer

logger = logging.getLogger("jobpulse.ats_ligeros")

_PLATS = ("workable", "ashby", "lever", "recruitee")

# fallback si el JSON es ilegible (solo tableros con CR confirmado)
FALLBACK = [
    ("ashby", "jbs-dev", "JBS"),
    ("ashby", "greenlight-workforce-solutions", "Greenlight Workforce"),
    ("ashby", "owner", "Owner"),
    ("lever", "westernacher", "Westernacher"),
    ("lever", "sysdig", "Sysdig"),
    ("lever", "omnidian", "Omnidian"),
    ("recruitee", "pur", "Pur"),
    ("recruitee", "transperfect", "TransPerfect"),
    ("recruitee", "primeworks", "PrimeWorks"),
    ("workable", "huzzle", "Huzzle"),
]

_RE_CR = re.compile(r"\bcosta\s*rica\b", re.I)


def _txt(v) -> str:
    return " ".join(_html.unescape(re.sub(r"<[^>]+>", " ", str(v or ""))).split())


class AtsLigerosProvider(BaseProvider):
    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self._boards = self._cargar_boards()
        self._wk_paused = False

    # ------------------------------------------------------------ boards
    def _cargar_boards(self) -> List[tuple]:
        path = (Path(__file__).resolve().parent.parent.parent /
                "data" / "empresas" / "otros_ats_cr.json")
        out: List[tuple] = []
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                xs = data if isinstance(data, list) else (data.get("boards") or [])
                for x in xs:
                    p = (x.get("platform") or "").strip()
                    tok = (x.get("token") or x.get("company") or "").strip()
                    if p in _PLATS and tok:
                        nombre = (x.get("company") or tok).strip()
                        out.append((p, tok, nombre))
            except Exception as e:
                logger.warning("otros_ats_cr.json ilegible (%s): uso fallback", e)
        if not out:
            out = [(p, t, n) for (p, t, n) in FALLBACK]
        logger.info("ATS ligeros: %d tableros desde JSON.", len(out))
        return out

    # ------------------------------------------------------------ fetch
    async def fetch(self, search_term: str, location: str,
                    pages: int) -> List[JobOffer]:
        return await asyncio.to_thread(self._sweep, max(1, int(pages or 6)))

    def _sweep(self, pages: int) -> List[JobOffer]:
        offers: List[JobOffer] = []
        self._wk_paused = False
        with httpx.Client(timeout=40.0, follow_redirects=True) as client:
            for p in _PLATS:
                boards = [b for b in self._boards if b[0] == p]
                corredor = getattr(self, f"_{p}")
                for i, (tok, nombre) in enumerate([(b[1], b[2]) for b in boards]):
                    if p == "workable" and self._wk_paused:
                        logger.warning("Workable en pausa por 429: se salta %s", tok)
                        continue
                    if p == "workable":
                        time.sleep(1.2 if i else 0)
                    try:
                        offers.extend(corredor(client, tok, nombre, pages))
                    except Exception as e:
                        logger.warning("ATS-l %s/%s fallo: %s", p, tok, e)
        logger.info("ATS ligeros: %d vacantes CR totales (antes del gate).",
                    len(offers))
        return offers

    # ------------------------------------------------------------ ashby
    def _ashby(self, client, tok: str, nombre: str, pages: int) -> List[JobOffer]:
        vacas: List[JobOffer] = []
        r = client.get(f"https://api.ashbyhq.com/posting-api/job-board/{tok}")
        if not (200 <= r.status_code < 300):
            logger.warning("Ashby %s HTTP %s", tok, r.status_code)
            return vacas
        for j in (r.json().get("jobs") or []):
            blobloc = " ".join(x for x in (str(j.get("location")), json.dumps(
                j.get("secondaryLocations") or "")) if x).lower()
            if not (_RE_CR.search(str(j.get("title") or "")) or
                    _RE_CR.search(blobloc)):
                continue
            desc = _txt(j.get("descriptionHtml")) or _txt(j.get("descriptionPlain"))
            loc = str(j.get("location") or "")
            if not _RE_CR.search(loc):
                # CR aparece en secundarias/others -> marcar como CR (multi)
                loc = "Costa Rica (multi-loc)"
            vacas.append(JobOffer(
                source="ats_ligeros", search_term="barrido-ats-ligeros",
                job_id=f"ashby-{tok}-{j.get('id')}",
                title=self.clean_text(j.get("title")),
                company=self.clean_text(nombre),
                location=self.clean_text(loc),
                link=j.get("jobUrl") or j.get("applyUrl"),
                date_published=(j.get("publishedAt") or "")[:10] or None,
                description=self.clean_text(desc),
                requirements="", salary=""))
        if vacas:
            logger.info("Ashby %s: %d CR", tok, len(vacas))
        return vacas

    # ------------------------------------------------------------ lever
    def _lever(self, client, tok: str, nombre: str, pages: int) -> List[JobOffer]:
        vacas: List[JobOffer] = []
        pos_h = "https://api.eu.lever.co/v0/postings/{tok}?mode=json"
        r = client.get(pos_h.format(tok=tok))
        if not (200 <= r.status_code < 300):
            # fallback .com
            r = client.get(f"https://api.lever.co/v0/postings/{tok}?mode=json")
        if not (200 <= r.status_code < 300):
            logger.warning("Lever %s HTTP %s", tok, r.status_code)
            return vacas
        for j in (r.json() or []):
            ctry = str(j.get("country") or "").upper()
            txt = _txt(j.get("text")) + " " + json.dumps(j.get("lists") or "",
                                                        ensure_ascii=False)
            if not (ctry == "CR" or _RE_CR.search(txt)):
                continue
            ep = j.get("createdAt")
            fecha = datetime.fromtimestamp(int(ep) / 1000, tz=timezone.utc).strftime(
                "%Y-%m-%d") if ep else None
            desc = _txt(j.get("description")) + " " + _txt(j.get("additional"))
            loc = "Costa Rica"
            vacas.append(JobOffer(
                source="ats_ligeros", search_term="barrido-ats-ligeros",
                job_id=f"lever-{tok}-{j.get('id')}",
                title=self.clean_text(j.get("text")),
                company=self.clean_text(nombre),
                location=self.clean_text(loc),
                link=j.get("hostedUrl") or j.get("applyUrl"),
                date_published=fecha,
                description=self.clean_text(desc),
                requirements="", salary=""))
        if vacas:
            logger.info("Lever %s: %d CR", tok, len(vacas))
        return vacas

    # ------------------------------------------------------------ recruitee
    def _recruitee(self, client, tok: str, nombre: str, pages: int) -> List[JobOffer]:
        vacas: List[JobOffer] = []
        r = client.get(f"https://{tok}.recruitee.com/api/offers/")
        if not (200 <= r.status_code < 300):
            logger.warning("Recruitee %s HTTP %s", tok, r.status_code)
            return vacas
        for o in ((r.json() or {}).get("offers") or []):
            blob = str(o.get("position") or "") + " " + str(o.get("location") or "") + \
                   " " + json.dumps(o.get("locations") or "", ensure_ascii=False)
            if not _RE_CR.search(blob):
                continue
            pub = (str(o.get("published_at") or o.get("created_at") or ""))[:10] or None
            desc = _txt(o.get("description")) or _txt(o.get("requirements"))
            vacas.append(JobOffer(
                source="ats_ligeros", search_term="barrido-ats-ligeros",
                job_id=f"recruitee-{tok}-{o.get('id')}",
                title=self.clean_text(str(o.get("position") or "")),
                company=self.clean_text(nombre),
                location=self.clean_text(str(o.get("location") or "Costa Rica")),
                link=o.get("careers_url"),
                date_published=pub,
                description=self.clean_text(desc + " " + _txt(o.get("requirements"))),
                requirements="", salary=""))
        if vacas:
            logger.info("Recruitee %s: %d CR", tok, len(vacas))
        return vacas

    # ------------------------------------------------------------ workable
    def _workable(self, client, tok: str, nombre: str, pages: int) -> List[JobOffer]:
        vacas: List[JobOffer] = []
        jobs: List[dict] = []
        cur: str | None = None
        for page in range(max(2, pages)):
            headers = {"X-Workable-Shortcode": tok, "User-Agent":
                       "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124"}
            if cur:
                headers["X-Workable-Page"] = str(cur)
            try:
                r = client.post(f"https://apply.workable.com/api/v1/accounts/{tok}/jobs",
                                headers=headers, timeout=40.0)
            except Exception as e:
                logger.warning("Workable %s fallo GET pag %s: %s", tok, page, e)
                return vacas
            time.sleep(0.4)
            if r.status_code == 429:
                # retry unico tras cooldown (throttle por IP)
                logger.warning("Workable %s HTTP 429: cooldown 120s...", tok)
                time.sleep(120)
                try:
                    r = client.post(f"https://apply.workable.com/api/v1/accounts/{tok}/jobs",
                                    headers=headers, timeout=40.0)
                except Exception as e:
                    logger.warning("Workable %s fallo retry: %s", tok, e)
                    self._wk_paused = True
                    return vacas
                if r.status_code == 429:
                    logger.warning("Workable %s sigue 429: pauso workable", tok)
                    self._wk_paused = True
                    return vacas
            if not (200 <= r.status_code < 300):
                logger.warning("Workable %s HTTP %s", tok, r.status_code)
                return vacas
            d = r.json()
            res = (d.get("results") or [])
            jobs.extend(res)
            nueva = d.get("nextPage")
            if not nueva or not res:
                break
            if cur == nueva:  # cursor no avanza -> parar
                break
            cur = nueva
        for j in jobs:
            locs = j.get("locations") or []
            ctry = ((j.get("location") or {}).get("countryCode") or "").upper()
            blob = json.dumps(locs, ensure_ascii=False)
            if not (ctry == "CR" or _RE_CR.search(blob) or
                    _RE_CR.search(str(j.get("title") or ""))):
                continue
            # detail con la descripcion
            desc = ""
            try:
                rd = client.get(f"https://apply.workable.com/api/v1/accounts/{tok}/jobs/{j.get('shortcode')}",
                                timeout=40.0)
                if 200 <= rd.status_code < 300:
                    desc = _txt((rd.json() or {}).get("description"))
            except Exception as e:
                logger.warning("Workable detail %s/%s: %s", tok, j.get("shortcode"), e)
            depts = "; ".join(j.get("department") or [])
            vacas.append(JobOffer(
                source="ats_ligeros", search_term="barrido-ats-ligeros",
                job_id=f"workable-{tok}-{j.get('id')}",
                title=self.clean_text(j.get("title")),
                company=self.clean_text(nombre),
                location=self.clean_text("Costa Rica" +
                                         (f" | {depts}" if depts else "")),
                link=f"https://apply.workable.com/{tok}/j/{j.get('shortcode')}",
                date_published=(j.get("published") or "")[:10] or None,
                description=self.clean_text(desc),
                requirements="", salary=""))
        if vacas:
            logger.info("Workable %s: %d CR", tok, len(vacas))
        return vacas
