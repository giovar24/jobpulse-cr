# -*- coding: utf-8 -*-
"""Cliente Phenom People "canvas" (SSR) para sitios tipo {host}/{lang}/search-results.

Patron descubierto (validado con Thermo Fisher y HPE):
- La pagina de resultados SSR embebe un JSON con las vacantes de la pagina:
  {"jobSeqNo": "...EXTERNALENUS", "title": "...", "multi_location": ["..."],
   "applyUrl": "...", "city": "...", ...} (10 por pagina).
- Paginacion por query `&from=N` (0,10,20,...); cuando la pagina devuelve 0
  jobSeqNo nuevos, se acabo.
- No requiere cookies ni tokens: el GET del SSR basta (el x-csrf-token solo se
  exige en los POST /widgets).

API:
    ph = PhenomSite("https://jobs.thermofisher.com")
    ph.descubrir()            -> config {refNum, locale_path, csrf}
    ph.buscar("Costa Rica", max_vacantes=50)
        -> [{"orange": ...}] con titulo/ubicaciones/applyUrl/job_id
"""
import json
import re
import time

import httpx

H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124",
     "Accept-Language": "en-US,en"}

RE_JS = re.compile(r'"jobSeqNo"\s*:\s*"([^"]+)"')
RE_TITULO = re.compile(r'"title"\s*:\s*"([^"]{3,200})"')
RE_LOC = re.compile(r'"multi_location"\s*:\s*\[([^\]]*)\]')
RE_APPLY = re.compile(r'"applyUrl"\s*:\s*"([^"]+)"')
RE_CITY = re.compile(r'"city"\s*:\s*"([^"]+)"')
RE_COUNTRY = re.compile(r'"country"\s*:\s*"([^"]+)"')
RE_STATE = re.compile(r'"state"\s*:\s*"([^"]+)"')
RE_JD = re.compile(r'"descri[^"]*"\s*:\s*"(.{3,200}?)"')

# orden de paths de idioma a probar
PATHS_STD = ["global/en", "us/en", "cr/en", "en", "cl/es", "es", "en-US",
             "mx/es", "co/es", "gb/en", "jobs", "global/en/search-results"]


def tiene_cr(v):
    """La vacante menciona Costa Rica en multi_location o cityStateCountry."""
    txt = (" ".join(str(x) for x in v["ubicaciones"]) + " "
           + str(v.get("lugar_txt") or "")).lower()
    return "costa rica" in txt or "costarica" in txt


def _json_str_arr(txt):
    """'{a,b}' -> [a,b] con escapes JSON respetados (aprox)."""
    try:
        return json.loads("[" + txt + "]")
    except ValueError:
        partes = [x.strip().strip('"') for x in txt.split(",")]
        return [p for p in partes if p]


class PhenomSite:
    def __init__(self, base, client=None):
        self.base = base.rstrip("/")
        self.client = client or httpx.Client(headers=H, follow_redirects=True,
                                             timeout=30.0)
        self.locale_path = None
        self.refNum = None
        self.csrf = None

    def _home(self):
        r = self.client.get(self.base + "/")
        r.raise_for_status()
        return r.text

    def descubrir(self):
        """Detecta el locales-path de search-results y el refNum del sitio."""
        home = self._home()
        m = re.search(r"CareerConnectResources/([A-Za-z0-9]+[A-Za-z0-9_]+)/", home)
        self.refNum = m.group(1) if m else None
        self.csrf = (re.search(r'"csrfToken"\s*:\s*"([a-f0-9]{32})"', home)
                     or [None]) and (re.search(r'"csrfToken"\s*:\s*"([a-f0-9]{32})"', home).group(1)
                                     if re.search(r'"csrfToken"\s*:\s*"([a-f0-9]{32})"', home) else None)
        # paths que aparecen en el propio home (locale-path p cand)
        cands = list(dict.fromkeys(
            re.findall(r'href="[^"]*/((?:[a-z]{2,3}(?:-[a-zA-Z]{2})?)/(?:[a-z]{2})(?:/[^"]*)?)"', home)))
        cands = [c for c in cands if re.match(r"^[a-z]{2,3}(-[a-zA-Z]{2})?/[a-z]{2}(?:-[A-Z]{2})?$", c)]
        for p in cands + PATHS_STD:
            try:
                r = self.client.get(f"{self.base}/{p}/search-results?keywords=Costa%20Rica")
            except Exception:
                continue
            if r.status_code == 200 and RE_JS.search(r.text):
                self.locale_path = p
                return {"refNum": self.refNum, "locale_path": p, "home": self.base,
                        "ok": True}
        return {"refNum": self.refNum, "locale_path": None, "home": self.base, "ok": False}

    def pagina(self, keywords, desde=0):
        """Una pagina (10 items) del SSR. Devuelve (vacantes, total)."""
        url = (f"{self.base}/{self.locale_path}/search-results"
               f"?keywords={keywords.replace(' ', '%20')}&from={desde}"
               if self.locale_path != "en"
               else f"{self.base}/search-results?keywords={keywords.replace(' ', '%20')}&from={desde}")
        r = self.client.get(url)
        if r.status_code != 200:
            return [], None
        t = r.text
        i = t.rfind('"jobs":[')
        if i == -1:
            return [], None
        try:
            jobs, _ = json.JSONDecoder().raw_decode(t, t.find("[", i))
        except ValueError:
            return [], None
        tot = re.search(r'"totalHits"\s*:\s*(\d+)', t)
        vacas = []
        for j in jobs:
            if not isinstance(j, dict):
                continue
            vacas.append({
                "job_id": f"phenom-{(self.refNum or self.base.split('//')[1].split('.')[0][:14]).upper()}-{j.get('jobSeqNo') or j.get('jobId') or ''}",
                "req_id": j.get("reqId"),
                "titulo": j.get("title") or j.get("jobTitle"),
                "ubicaciones": j.get("multi_location") or [],
                "ciudad": j.get("city"), "estado": j.get("state"),
                "pais": j.get("country"), "lugar_txt": j.get("cityStateCountry"),
                "apply_url": j.get("applyUrl"), "external_apply": j.get("externalApply"),
                "fecha": j.get("postedDate") or j.get("dateCreated"),
                "teaser": j.get("descriptionTeaser"),
            })
        return vacas, (int(tot.group(1)) if tot else None)

    def buscar(self, keywords="Costa Rica", max_vacantes=60, pausa=0.3):
        """Barre from=0,10,20,... y devuelve las vacantes unicas."""
        vacas, vistos, total = [], set(), 0
        for desde in range(0, max_vacantes, 10):
            pag, tot = self.pagina(keywords, desde)
            nuevos = [v for v in pag if v["job_id"] not in vistos]
            if tot and not total:
                total = tot
            if not nuevos:
                break
            for v in nuevos:
                vistos.add(v["job_id"])
                vacas.append(v)
            if len(pag) < 10:
                break
            time.sleep(pausa)
        return vacas, total


if __name__ == "__main__":
    ph = PhenomSite("https://careers.hpe.com")
    print(ph.descubrir())
    jobs, total = ph.buscar("Costa Rica", max_vacantes=40)
    for j in jobs[:6]:
        print(j["titulo"], "|", j["ubicaciones"][:2], "|", (j["apply_url"] or "")[:70])
    print("total:", total, "| paginadas:", len(jobs))
