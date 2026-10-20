"""Barrido de empresas candidatas para multi-fuente de ATS (Workday, etc.).

Fases (todas independientes, unidas al final en el CSV de respaldo):

  1. cinde      -> GraphQL de cindejobs.com: viewCompanies + viewJobOffers
                   (empresa, sector, vacantes activas; sin navegador).
  2. procomer   -> talento.procomer.com jobs-available paginado; agrega
                   organizations con conteo de vacantes (sin navegador).
  3. zf         -> procomer.com/zonas-franca/ (directorio server-rendered):
                   empresas del regimen con cedula, actividad, provincia.
  4. ane        -> ane.cr listado /Puesto?Pagina=N sobre sesion de Brave
                   (login con .env); solo listado (empresa por tarjeta),
                   sin abrir cada detalle: rapido y poco intrusivo.
  5. merge      -> normaliza nombres, deduplica y escribe
                   data/empresas/empresas_candidatas.csv (respaldo).

Uso:
    python barrido_empresas.py            # todo (ane abre Brave)
    python barrido_empresas.py --no-ane   # solo HTTP, sin navegador
    python barrido_empresas.py --ane-pages 150
    python barrido_empresas.py --solo merge   # reconstruye CSV desde los JSON
"""
import argparse
import asyncio
import json
import logging
import os
import re
import sys
import time
import unicodedata
from pathlib import Path

import httpx
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.bootstrap import ensure_workspace

ensure_workspace()
if os.path.exists(Path(__file__).resolve().parent / ".env"):
    from dotenv import load_dotenv
    load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
OUT_DIR = BASE_DIR / "data" / "empresas"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("barrido")

UA_HEADERS = {"User-Agent": "JobPulseCR/1.0 (local; personal)"}

# ------------------------------------------------------------------ helpers

def deacc(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s or "")
                   if unicodedata.category(c) != "Mn")


def quita_juridica(s: str) -> str:
    """Quita sufijos legales y ruido para la clave de deduplicacion."""
    t = " ".join(deacc(s).upper().split())
    t = re.sub(r"\b(S\.?[AAL]\.?)\b", "SA", t)
    for suf in (
        "SOCIEDAD DE RESPONSABILIDAD LIMITADA", "SOCIEDAD RESPONSABILIDAD LIMITADA",
        "SOCIEDAD ANONIMA", "SOC ANONIMA", "SOCIEDAD SINDICADA",
        "LIMITADA", "SRL", "SAOCIV", "SOCIEDAD", "ANONIMA", "ANONIMO",
        "CORPORATION", "CORP", "COMPANY", "CO", "INC", "LIMITED", "LLC",
        "GMBH", "SARL", "SAPI", "PARTNERS", "INTERNACIONAL",
    ):
        t = re.sub(rf"(?:^|\b){re.escape(suf)}$", "", t).strip(" ,.-&")
    return t.strip()


def save_json(name: str, data):
    OUT_DIR.mkdir(exist_ok=True)
    (OUT_DIR / name).write_text(
        json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    log.info("Guardado %s (%d registros)", name, len(data))


def load_json(name: str):
    p = OUT_DIR / name
    if not p.exists():
        return []
    return json.loads(p.read_text(encoding="utf-8"))

# ------------------------------------------------------------------- 1 CINDE

CINDE_API = "https://api.cindejobs.com/graphql"

_CINDE_COMPANY_FIELDS = """
companySlug companyName workSectorEs totalJobOffers
"""
_CINDE_OFFER_FIELDS = """
jobOfferId slug companySlug companyName
"""


def _cindeGraphQL(client, query, variables):
    r = client.post(CINDE_API, headers=UA_HEADERS | {"Content-Type": "application/json"},
                    json={"query": query, "variables": variables}, timeout=30.0)
    if not (200 <= r.status_code < 300):
        raise RuntimeError(f"CINDE HTTP {r.status_code}: {r.text[:200]}")
    data = r.json()
    if data.get("errors"):
        raise RuntimeError(f"CINDE GraphQL errors: {str(data['errors'])[:300]}")
    return data["data"]


def fase_cinde():
    """Empresas de cindejobs: viewCompanies (si pagina) + todas las ofertas."""
    empresas, ofertas_vistas = [], {}
    with httpx.Client(headers=UA_HEADERS, timeout=30.0) as c:
        # --- viewCompanies: intento con limite alto (el API define limit, no page)
        try:
            d = _cindeGraphQL(c,
                "query ($limit: Int!) { viewCompanies(limit: $limit) { %s } }"
                % _CINDE_COMPANY_FIELDS, {"limit": 1000})
            comps = d.get("viewCompanies") or []
            for it in comps:
                empresas.append({
                    "slug": it.get("companySlug") or "",
                    "nombre": (it.get("companyName") or "").strip(),
                    "sector": (it.get("workSectorEs") or "").strip(),
                    "vacantes_cinde": int(it.get("totalJobOffers") or 0),
                    "via": "directorio",
                })
            log.info("CINDE viewCompanies: %d (limit 1000)", len(empresas))
        except Exception as e:
            log.warning("CINDE viewCompanies fallo (%s); sigo por ofertas.", e)

        # --- ofertas paginadas: cubre empresas sin ficha propia de directorio
        # (limit alto alarga la pag 1 y vacia las demas: usar el tope real, 16)
        page = 1
        while page <= 60:
            try:
                d = _cindeGraphQL(c,
                    "query ($limit: Int!, $page: Int!) { viewJobOffers(limit: $limit, page: $page) { %s } }"
                    % _CINDE_OFFER_FIELDS, {"limit": 16, "page": page})
            except Exception as e:
                log.warning("CINDE ofertas pag %d fallo: %s", page, e)
                break
            items = d.get("viewJobOffers") or []
            if not items:
                break
            for it in items:
                slug = it.get("companySlug") or ""
                if slug and slug not in ofertas_vistas:
                    ofertas_vistas[slug] = (it.get("companyName") or "").strip()
            page += 1
            time.sleep(0.4)
        log.info("CINDE ofertas: %d empresas con vacantes (pag %d)", len(ofertas_vistas), page - 1)

    conocidos = {e["slug"]: e for e in empresas}
    for slug, nombre in ofertas_vistas.items():
        if slug in conocidos:
            continue
        empresas.append({
            "slug": slug, "nombre": nombre, "sector": "",
            "vacantes_cinde": 0, "via": "ofertas",
        })
    save_json("cinde_empresas.json", empresas)
    return empresas

# ---------------------------------------------------------------- 2 PROCOMER

PROCOMER_API = "https://talento.procomer.com/api/candidate/jobs-available"


def fase_procomer():
    """Organizaciones del tablero talento.procomer.com con conteo de vacantes.

    jobs-available no trae el nombre de la organizacion (solo id/logo); el
    nombre se resuelve con GET /api/candidate/organizations/{id} (2xx,
    tipicamente 201) por org_id UNICO (una peticion por empresa, no por job).
    """
    ids = {}
    anon = 0
    page = 1
    with httpx.Client(headers=UA_HEADERS, timeout=30.0) as c:
        while page <= 60:
            r = c.get(PROCOMER_API, params={"search": "", "page": page, "per_page": 16})
            if not (200 <= r.status_code < 300):
                log.warning("Procomer HTTP %s en pag %d; corto.", r.status_code, page)
                break
            try:
                data = r.json()
            except ValueError:
                break
            items = data.get("data") or []
            if not items:
                break
            for it in items:
                if it.get("external_visibility") == "anonymous":
                    anon += 1
                    continue
                oid = (it.get("organization") or {}).get("id") or it.get("organization_id")
                if not oid:
                    anon += 1
                    continue
                ids.setdefault(int(oid), 0)
                ids[int(oid)] += 1
            if page >= (data.get("last_page") or page):
                break
            page += 1
            time.sleep(0.4)

        empresas = []
        for i, (oid, n) in enumerate(sorted(ids.items(), key=lambda kv: -kv[1]), 1):
            nombre, legal = None, None
            try:
                r2 = c.get(f"https://talento.procomer.com/api/candidate/organizations/{oid}")
                d2 = r2.json()
                o2 = d2.get("organization") or d2
                nombre = (o2.get("name") or "").strip()
                legal = (o2.get("legal_name") or "").strip()
            except Exception as e:
                log.warning("Procomer org %d fallo: %s", oid, e)
            if not nombre:
                continue
            empresas.append({
                "nombre": nombre,
                "legal_name": legal,
                "org_id": oid,
                "vacantes_procomer": n,
            })
            if i <= 200:
                time.sleep(0.3)
    log.info("Procomer: %d organizaciones resueltas (%d vacantes anonimas fuera)",
             len(empresas), anon)
    empresas.sort(key=lambda e: -e["vacantes_procomer"])
    save_json("procomer_empresas.json", empresas)
    return empresas

# ------------------------------------------------------------- 3 Zona franca

ZF_URL = "https://procomer.com/zonas-franca/"


def fase_zonasfrancas():
    """Directorio completo de empresas de regimen (HTML server-rendered)."""
    with httpx.Client(headers=UA_HEADERS, follow_redirects=True, timeout=60.0) as c:
        r = c.get(ZF_URL)
        r.raise_for_status()
        html = r.text
    soup = BeautifulSoup(html, "html.parser")
    if len(html) < 50_000:
        log.warning("ZF: HTML chico (%d); puede ser dinamico.", len(html))
    registros = []
    for h3 in soup.find_all("h3"):
        nombre = " ".join(h3.get_text(" ", strip=True).split())
        if not nombre:
            continue
        caja = h3.find_parent(["article", "div"])
        if caja is None:
            continue
        texto = caja.get_text(" ", strip=True)
        m_ced = re.search(r"C[ée]dula\s*:\s*([0-9\-\s]+?)(?:Actividad:|$)", texto)
        m_act = re.search(r"Actividad\s*:\s*([^:]*?)(?:Provincia:|$)", texto)
        m_prov = re.search(r"Provincia\s*:\s*([A-Za-z-zA-Z]+)", texto)
        m_ubi = re.search(r"Ubicaci[óo]n\s*:\s*([^:]+?)((?:De Servicios|De Exportaci[óo]n|Inciso|Administradora|Comercial|Procesadora)[^:]*$|$)", texto) \
            or re.search(r"Ubicaci[óo]n\s*:\s*([^:]+?)(?=[A-Z][a-z]+ [A-Z][a-z]*(?: [A-Z][a-z]*)?\s|$)", texto)
        registros.append({
            "nombre": nombre,
            "cedula": " ".join((m_ced.group(1) or "").split()) if m_ced else "",
            "actividad": (m_act.group(1) or "").strip() if m_act else "",
            "provincia": (m_prov.group(1) or "").strip() if m_prov else "",
            "ubicacion": (m_ubi.group(1) or "").strip() if m_ubi else "",
        })
    # quita duplicados de la misma cedula (bloques anidados)
    vistas, limpio = set(), []
    for it in registros:
        k = it["cedula"] or it["nombre"]
        if k in vistas:
            continue
        vistas.add(k)
        limpio.append(it)
    save_json("zonasfrancas.json", limpio)
    log.info("Zonas francas: %d empresas parseadas", len(limpio))
    return limpio

# --------------------------------------------------------------------- 4 ANE

def fase_ane(pages: int = 120):
    """Listado /Puesto?Pagina=N con sesion de ANE en Brave; solo empresas."""
    from core.browser_manager import BraveManager
    from providers.boards.ane import AneProvider

    cfg = {}
    if os.getenv("BRAVE_BIN"):
        cfg["brave_bin"] = os.getenv("BRAVE_BIN")
    if os.getenv("BRAVE_PERFIL"):
        cfg["brave_perfil"] = os.getenv("BRAVE_PERFIL")

    # mismo truco de main.py: ane.cr no siempre resuelve; fuerzamos la IP
    from main import resolve_first  # reutiliza el helper (sip, esta en main.py)
    aip = resolve_first(["www.ane.cr", "brete.cr", "ane.cr"])
    if aip:
        cfg["host_resolver_rules"] = f"MAP www.ane.cr {aip}"

    mgr = BraveManager(cfg)
    port = mgr.start_brave()
    if not port:
        log.error("No se pudo abrir Brave; fase ANE abortada.")
        return []
    empresas = {}
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(f"http://localhost:{port}")
            ctx = browser.contexts[0]
            ane = AneProvider({
                "username": os.getenv("ANE_USER"),
                "password": os.getenv("ANE_PASSWORD"),
            })
            if not ane._asegurar_sesion(ctx):
                log.error("ANE: sin sesion (credenciales .env fallaron).")
                return []
            log.info("ANE: sesion ok; barriendo %d paginas de listado...", pages)
            for pagina in range(1, pages + 1):
                html = ane._obtener(ctx, f"https://www.ane.cr/Puesto?Pagina={pagina}")
                cards = ane._parse_listado(html) if html else []
                if not cards:
                    # 2 paginas vacias seguidas = fin del listado
                    html2 = ane._obtener(ctx, f"https://www.ane.cr/Puesto?Pagina={pagina+1}")
                    if not ane._parse_listado(html2 or ""):
                        log.info("Fin del listado ANE en pag %d", pagina)
                        break
                    cards = ane._parse_listado(html2)
                    pagina += 1
                for ccard in cards:
                    nom = ccard.get("empresa") or ""
                    if not nom:
                        continue
                    e = empresas.setdefault(nom, {"vacantes_ane": 0, "ejemplo_puesto": ""})
                    e["vacantes_ane"] += 1
                    if not e["ejemplo_puesto"]:
                        e["ejemplo_puesto"] = ccard.get("titulo") or ""
                if pagina % 10 == 0:
                    log.info("ANE pag %d: %d empresas hasta ahora", pagina, len(empresas))
                time.sleep(0.5)
    finally:
        mgr.stop_brave()

    empresas = [{
        "nombre": k,
        "vacantes_ane": v["vacantes_ane"],
        "ejemplo_puesto": v["ejemplo_puesto"],
    } for k, v in sorted(empresas.items(), key=lambda kv: -kv[1]["vacantes_ane"])]
    save_json("ane_empresas.json", empresas)
    log.info("ANE: %d empresas unicas", len(empresas))
    return empresas

# ------------------------------------------------------------------ 5 MERGE

SEED_GRANDES = [
    # marcas conocidas con operaciones grandes en CR (verificar caso a caso)
    "intel", "amazon", "microsoft", "ibm", "hp", "hewlett packard", "hewlett-packard",
    "dell", "abbott", "boston scientific", "medtronic", "philips", "bayer",
    "syngenta", "roche", "pfizer", "siemens", "bosch", "continental", "toyota",
    "volkswagen", "vibram", "st Jude", "hologic", "belt equipment",
    "equifax", "sykes", "foundever", "concentrix", "teleperformance", "ibex",
    "genpact", "accenture", "deloitte", "ey", "pwc", "kpmg", "sutherland",
    "aws", "west po", "westpost", "wells fargo", "bank of america",
    "citi", "citibank", "hsbc", "standard chartered", "bofa", "learn", "wuxi",
    "shimadzu", "anchor", "pandora", "dole", "chiquita", "kaiser", "cigna",
    "moog", "durrutia", "proximity", "anders",
]


_RE_RECRUITER = re.compile(
    r"empleos\.\s*net|recluta|reclutam|bolsa de empleo|talenthunter|headhunt|"
    r"manpower|kelly services|random jobs|jobs\.|trabaj[ao]s\.|klee|"
    r"empleo_|bolsaNacional|ladyjobs|jobs4u|soluciones de empleo", re.I)
_RE_CONFIDENCIAL = re.compile(
    r"confidencial?|confiden|oculto|no revelada|company confidential|"
    r"central de emp|consultora de reclutamiento", re.I)


def tipo_empresa(nombre: str) -> str:
    if _RE_CONFIDENCIAL.search(" ".join(deacc(nombre).lower().split())):
        return "confidencial"
    if _RE_RECRUITER.search(deacc(nombre).lower()):
        return "recruiter"


def clasificar_tamano(nombre: str, vacantes_observed: int) -> str:
    n = " ".join(deacc(nombre).upper().split())
    for semilla in SEED_GRANDES:
        sem = " ".join(deacc(semilla).upper().split())
        if len(sem) >= 4 and re.search(rf"(?:^|\b){re.escape(sem)}", n):
            return "grande"
    if vacantes_observed >= 12:
        return "grande?"
    if vacantes_observed >= 4:
        return "mediana?"
    return "por_evaluar"


def fase_merge():
    cinde = load_json("cinde_empresas.json")
    procomer = load_json("procomer_empresas.json")
    zf = load_json("zonasfrancas.json")
    ane = load_json("ane_empresas.json")

    empresas = {}      # clave canonica -> registro fusionado
    por_razon = {}     # clave de razon social -> clave canonica (agrupa variantes)

    def _c(n):
        return quita_juridica(n) or " ".join(deacc(n).upper().split())

    def _merge_destino(clave):
        """Si la clave coincide con la razon social de otra entrada, se fusiona."""
        if clave in empresas:
            return clave
        if clave in por_razon:
            return por_razon[clave]
        return None

    def _reg(clave, nombre, fuente, razon_social=None):
        """Registra (o consolida) una empresa desde `fuente`."""
        destino = _merge_destino(clave)
        if destino is None:
            empresas[clave] = {
                "nombre": nombre, "clave": clave,
                "razon_social": "", "fuentes": set(),
                "vacantes_observadas": 0,
            }
            destino = clave
        r = empresas[destino]
        r["fuentes"].add(fuente)
        if razon_social:
            if not r["razon_social"]:
                r["razon_social"] = razon_social
            # guarda el tramo para futuras fusiones con el directorio legal
            por_razon.setdefault(_c(razon_social), destino)
        return r

    # --- CINDE ---------------------------------------------------------
    for it in cinde:
        if not it.get("nombre"):
            continue
        r = _reg(_c(it["nombre"]), it["nombre"], "cinde")
        r["slug_cinde"] = r.get("slug_cinde") or it.get("slug") or ""
        r["sector_cinde"] = r.get("sector_cinde") or it.get("sector") or ""
        v = int(it.get("vacantes_cinde") or 0)
        r["cinde_vacantes"] = max(int(r.get("cinde_vacantes") or 0), v)
        r["vacantes_observadas"] += v
    # --- PROCOMER ------------------------------------------------------
    for it in procomer:
        if not it.get("nombre"):
            continue
        razon = it.get("legal_name") or None
        r = _reg(_c(it["nombre"]), it["nombre"], "procomer", razon_social=razon)
        v = int(it.get("vacantes_procomer") or 0)
        r["vacantes_procomer"] = int(r.get("vacantes_procomer") or 0) + v
        r["vacantes_observadas"] += v
    # --- ZONA FRANCA (nombres legales: casa con legal_name de procomer) --
    for it in zf:
        nom = it.get("nombre") or ""
        legal = nom if not re.fullmatch(r"\d+ SOCIEDAD.+", " ".join(nom.split())) else ""
        if not legal:
            continue
        r = _reg(_c(legal), legal, "zonasfranca")
        r["cedula_zf"] = r.get("cedula_zf") or it.get("cedula") or ""
        r["actividad_zf"] = r.get("actividad_zf") or it.get("actividad") or ""
        r["provincia_zf"] = r.get("provincia_zf") or it.get("provincia") or ""
        r["ubicacion_zf"] = r.get("ubicacion_zf") or it.get("ubicacion") or ""
    # --- ANE (nombres comerciales de quien publica) ---------------------
    for it in ane:
        if not it.get("nombre"):
            continue
        r = _reg(_c(it["nombre"]), it["nombre"], "ane")
        v = int(it.get("vacantes_ane") or 0)
        r["vacantes_ane"] = int(r.get("vacantes_ane") or 0) + v
        r["vacantes_observadas"] += v

    # --- WORKDAY (directorio CXS sondeado con searchText "Costa Rica") ---
    path_wd = OUT_DIR / "workday_cr.json"
    if path_wd.exists():
        nuevas = 0
        for wd in json.loads(path_wd.read_text(encoding="utf-8")):
            tok = wd.get("token") or ""
            wsitio = wd["token"]  # "wd1/empresa/Sitio"
            parts = tok.split("/")
            empresa_token = parts[1] if len(parts) >= 2 else (wd.get("company") or "")
            nombre = wd.get("company") or empresa_token
            if not empresa_token:
                continue
            clave = _c(nombre)
            destino = _merge_destino(clave)
            if destino is None:
                r = _reg(clave, nombre, "workday_dir")
                nuevas += 1
            else:
                r = empresas[destino]
                r["fuentes"].add("workday_dir")
            r["ats"] = "workday"
            r["tenant_workday"] = wsitio
            r["api_endpoint"] = wd.get("api_url") or ""
            r["vacantes_workday_cr"] = int(wd.get("total_cr") or 0)
        if nuevas:
            log.info("Workday: %d empresas nuevas (no estaban en ANE/CINDE/Procomer/ZF)",
                     nuevas)

    # --- OTROS ATS (greenhouse/ashby/lever/workable/recruitee/rippling/personio) --
    path_ats = OUT_DIR / "otros_ats_cr.json"
    if path_ats.exists():
        nuevas = 0
        for oa in json.loads(path_ats.read_text(encoding="utf-8")):
            nombre = oa.get("company") or ""
            if not nombre:
                continue
            clave = _c(nombre)
            destino = _merge_destino(clave)
            if destino is None:
                r = _reg(clave, nombre, "otros_ats")
                nuevas += 1
            else:
                r = empresas[destino]
                r["fuentes"].add("otros_ats")
            r["ats"] = r.get("ats") or oa.get("platform")
            r["ats_token"] = f"{oa.get('platform')}::{(oa.get('token') or '')[:80]}"
            r["api_endpoint"] = oa.get("api_url") or ""
            r["vacantes_otros_ats_cr"] = int(oa.get("total_cr") or 0)
        if nuevas:
            log.info("Otros ATS: %d empresas nuevas (no estaban en las fuentes previas)",
                     nuevas)

    filas = []
    for r in sorted(empresas.values(), key=lambda x: -x["vacantes_observadas"]
                    - int(x.get("vacantes_workday_cr") or 0)):
        fuentes = sorted(r["fuentes"])
        filas.append({
            "empresa": r["nombre"],
            "razon_social": r.get("razon_social", ""),
            "tipo": tipo_empresa(r["nombre"]),
            "tamano_estimado": clasificar_tamano(
                r["nombre"],
                r["vacantes_observadas"] + int(r.get("vacantes_workday_cr") or 0)
                + int(r.get("vacantes_otros_ats_cr") or 0)),
            "fuentes": "|".join(fuentes),
            "vacantes_observadas": r["vacantes_observadas"]
                                   + int(r.get("vacantes_workday_cr") or 0),
            "vacantes_workday_cr": r.get("vacantes_workday_cr", ""),
            "vacantes_otros_ats_cr": r.get("vacantes_otros_ats_cr", ""),
            "sector": r.get("sector_cinde", ""),
            "cedula_zf": r.get("cedula_zf", ""),
            "actividad_zf": r.get("actividad_zf", ""),
            "provincia": r.get("provincia_zf", ""),
            "ubicacion": r.get("ubicacion_zf", ""),
            "slug_cinde": r.get("slug_cinde", ""),
            "link_cinde": (f"https://cindejobs.com/en/mojopage/{r['slug_cinde']}"
                           if r.get("slug_cinde") else ""),
            "ats": r.get("ats", ""),
            "ats_token": r.get("ats_token", ""),
            "tenant_workday": r.get("tenant_workday", ""),
            "api_endpoint": r.get("api_endpoint", ""),
            "notas": "",
        })

    OUT_DIR.mkdir(exist_ok=True)
    import csv
    path = OUT_DIR / "empresas_candidatas.csv"
    cols = ["empresa", "razon_social", "tipo", "tamano_estimado", "fuentes",
            "vacantes_observadas", "vacantes_workday_cr", "vacantes_otros_ats_cr",
            "sector",
            "cedula_zf", "actividad_zf", "provincia", "ubicacion", "slug_cinde",
            "link_cinde", "ats", "ats_token", "tenant_workday", "api_endpoint", "notas"]
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(filas)
    log.info("CSV: %s (%d empresas unicas; grandes %d, mediana? %d, por_evaluar %d; "
             "recruiter %d, confidencial %d)",
             path, len(filas),
             sum(1 for x in filas if x["tamano_estimado"] == "grande"),
             sum(1 for x in filas if x["tamano_estimado"] == "mediana?"),
             sum(1 for x in filas if x["tamano_estimado"] == "por_evaluar"),
             sum(1 for x in filas if x["tipo"] == "recruiter"),
             sum(1 for x in filas if x["tipo"] == "confidencial"))
    return filas


def main():
    ap = argparse.ArgumentParser(description="Barrido de empresas candidatas")
    ap.add_argument("--no-ane", action="store_true", help="no abre Brave; salta ANE")
    ap.add_argument("--ane-pages", type=int, default=120)
    ap.add_argument("--solo", choices=["merge", "ane"], help="ejecuta una sola fase (--solo ane corre solo el barrido de ANE)")
    args = ap.parse_args()
    args = ap.parse_args()

    if args.solo == "merge":
        fase_merge()
        return
    if args.solo == "ane":
        fase_ane(args.ane_pages)
        return

    if not args.no_ane:
        fase_ane(args.ane_pages)
    else:
        log.info("--no-ane: salto ANE")
    fase_cinde()
    fase_procomer()
    fase_zonasfrancas()
    fase_merge()


if __name__ == "__main__":
    main()
