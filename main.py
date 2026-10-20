import argparse
import asyncio
import logging
import os
import re
import socket
import pandas as pd
from datetime import datetime, timedelta
from dotenv import load_dotenv

from core.bootstrap import ensure_workspace
from core.database import JobDatabase
from core.browser_manager import BraveManager
from core.gate import gate_jobs, DEFAULT_DIAS, DEFAULT_MIN_SCORE
from providers.boards.procomer import ProcomerProvider
from providers.boards.linkedin import LinkedInProvider
from providers.boards.indeed import IndeedProvider
from providers.boards.ane import AneProvider
from providers.boards.cinde import CindeProvider
from providers.directos.workday import WorkdayProvider
from providers.base import JobOffer
from providers.source_groups import GROUP_DIRECTOS, source_group
from core.dedupe import dedupe_boards_vs_directos
from core.progreso import GRUPO_LABEL, iniciar_corrida
from core import credenciales as creds_store

ensure_workspace()   # crea data/, profile.json (generico) y .env si faltan
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("jobpulse")

# ANE barre por fecha (su buscador esta roto): tope de paginas de listado.
# El corte por fecha real lo pone la ventana (dias); esto es una red de seguridad.
ANE_PAGES = int(os.getenv("ANE_MAX_PAGES", "15"))

# CINDE Jobs barre todo el tablero activo (su API no busca bien en ES y son
# pocas vacantes): tope de paginas; cada pagina trae hasta 100 ofertas.
CINDE_PAGES = int(os.getenv("CINDE_MAX_PAGES", "10"))

# Workday: barrido de tenants curados con searchText "Costa Rica"; `pages`
# paginas de 20 por tenant (los hits CR casi nunca pasan de 20).
WORKDAY_PAGES = int(os.getenv("WORKDAY_MAX_PAGES", "3"))

# Phenom: barrido de sitios SSR con keywords "Costa Rica"; 10 paginas por sitio.
PHENOM_PAGES = int(os.getenv("PHENOM_MAX_PAGES", "3"))

# TP (Teleperformance): API Umbraco de tp.com filtrada country=Costa Rica;
# paginas de 50 (el cat CR actual es corto, 2 pag sobra).
TP_PAGES = int(os.getenv("TP_MAX_PAGES", "2"))

# Greenhouse: 1 GET por tablero (API board con content=true); los tokens
# vienen del barrido multi-ATS (otros_ats_cr.json). Pages se usa como reintentos.
GREENHOUSE_PAGES = int(os.getenv("GREENHOUSE_MAX_PAGES", "1"))

# iCIMS: listado paginado pr=N (50/pag) por board; sin filtro server-side
# de keyword -> se barre y se filtra CR del lado del proveedor.
ICIMS_PAGES = int(os.getenv("ICIMS_MAX_PAGES", "8"))

# SmartRecruiters: API publica q=Costa Rica por compania (pag de 100).
SR_PAGES = int(os.getenv("SR_MAX_PAGES", "10"))

# Rippling: boards con peloteo SSR; filtrado por registro (title+locations).
RIPPLING_PAGES = int(os.getenv("RIPPLING_MAX_PAGES", "1"))

# ATS ligeros: Ashby/Lever/Recruitee/Workable (1 GET por tablero; Workable
# paginado de 10 y con throttle propio -> AL_MAX_PAGES limita el paginado).
AL_PAGES = int(os.getenv("AL_MAX_PAGES", "6"))

# Orden de los barridos "de un solo tiro" (uno por proveedor, sin termino de
# busqueda). Lo usa la GUI (pestana Corrida) para la barra de progreso y para
# diferenciar JOB BOARDS (agregadores: Indeed/LinkedIn/ANE...) de PORTALES DE
# LA EMPRESA (ATS directo: Workday, iCIMS, Greenhouse...).
BARRIDOS = [
    ("ANE (barrido por fecha)", "boards"),
    ("CINDE Jobs", "boards"),
    ("Workday", "directos"),
    ("Phenom", "directos"),
    ("TP (Teleperformance)", "directos"),
    ("Greenhouse", "directos"),
    ("iCIMS", "directos"),
    ("SmartRecruiters", "directos"),
    ("SuccessFactors", "directos"),
    ("Rippling", "directos"),
    ("ATS ligeros (Ashby/Lever/Recruitee/Workable)", "directos"),
    ("Login sites (navegador del usuario)", "directos"),
]


def strip_html(text):
    if not text:
        return ""
    return re.sub(r"<[^>]+>", " ", text)


def resume_text(text, limit=200):
    clean = re.sub(r"\s+", " ", strip_html(text)).strip()
    return clean[:limit] + ("..." if len(clean) > limit else "")


def resolve_first(hosts):
    """Devuelve la IP del primer host que resuelva por DNS (o None)."""
    for h in hosts:
        try:
            return socket.gethostbyname(h)
        except OSError:
            continue
    return None


def job_row(j):
    return {
        "Fuente": j.source,
        "Grupo": GRUPO_LABEL.get(source_group(j.source), "Job board (agregador)"),
        "Puesto": j.title,
        "Empresa": j.company,
        "Ubicación": j.location,
        "Link": j.link,
        "Fecha": j.date_published or "",
        "Salario": j.salary or "",
        "Gate": (f"{j.gate_score:.2f}" if j.gate_score is not None else ""),
        "Descripción": resume_text(j.description),
        "Requisitos": resume_text(j.requirements),
    }


def dict_a_offer(d: dict) -> JobOffer:
    """Convierte una fila de jobs_raw (dict) en JobOffer para re-filtrar."""
    return JobOffer(
        source=d.get("source", ""), search_term=d.get("search_term", ""),
        job_id=d.get("job_id", ""), title=d.get("title", ""),
        company=d.get("company", ""), location=d.get("location", ""),
        link=d.get("link", ""), date_published=d.get("date_published"),
        description=d.get("description"), requirements=d.get("requirements"),
        salary=d.get("salary"),
    )


def _motor(profile, sin_expand: bool, searches: list):
    """Crea (si hay perfil) el motor semantico y expande las busquedas."""
    if not profile:
        return None, searches
    from core.search_engine import SemanticSearchEngine
    engine = SemanticSearchEngine()
    if not sin_expand:
        logger.info("Expandiendo tematicas del perfil...")
        searches = engine.expand_searches(
            searches, top_n=2, vocab_es=profile.titulos, vocab_en=profile.titulos
        )
    return engine, searches


async def _aplicar_gate(db, profile, engine, offers, dias, min_score):
    """Gate Fecha Y relevancia -> staging (jobs_raw) + curado (jobs)."""
    aprobados, descartados = gate_jobs(offers, profile, engine, dias, min_score)
    await db.save_raw_jobs(offers)                 # todo lo crudo + score/razon
    newly = await db.save_jobs(aprobados)          # solo aprobados -> tabla jobs
    logger.info(
        "Pipeline: %d crudas -> %d aprobadas -> %d nuevas en jobs (%d descartadas).",
        len(offers), len(aprobados), len(newly), len(descartados),
    )
    return newly, aprobados, descartados


def _reportes(newly):
    stamp = datetime.now().strftime("%Y%m%d")
    if not newly:
        logger.info("No new jobs found.")
        return
    logger.info(f"Found {len(newly)} new jobs today!")
    df = pd.DataFrame([job_row(j) for j in newly])
    report_name = f"data/report_{stamp}.csv"
    df.to_csv(report_name, index=False, encoding="utf-8-sig")
    logger.info(f"Report generated: {report_name}")

    now = datetime.now()
    threshold = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    today = now.strftime("%Y-%m-%d")
    # Solo fechas válidas dentro de las últimas 24 h (excluye vacías y futuras).
    recent = [j for j in newly if threshold <= (j.date_published or "") <= today]
    if recent:
        df24 = pd.DataFrame([job_row(j) for j in recent])
        name24 = f"data/report_24h_{stamp}.csv"
        df24.to_csv(name24, index=False, encoding="utf-8-sig")
        logger.info(f"Publicadas en las últimas 24 h: {len(recent)} (guardadas en {name24})")
    else:
        logger.info("Sin publicaciones en las últimas 24 horas.")


async def refiltrar(db, profile, engine, dias, min_score, prog=None):
    """Re-aplica el gate sobre jobs_raw SIN re-scrapear (valor de la 2da tabla).

    La tabla curada `jobs` se RECONSTRUYE: lo que aprobo una corrida anterior
    pero ya no pasa los criterios actuales sale de `jobs` (nunca de jobs_raw).
    """
    if prog:
        prog.fase("Re-filtrar (sin scraping)")
    raw = await db.get_raw_jobs()
    if not raw:
        logger.warning("jobs_raw vacia: corre una corrida normal primero.")
        return
    await db.clear_curated()
    offers = [dict_a_offer(r) for r in raw]
    # mismo orden de prioridad que la corrida normal: first directos, luego
    # boards (las copias de boards que ya existen en ATS se descartan).
    offers, n_dup = dedupe_boards_vs_directos(offers)
    logger.info("Re-filtrando %d vacantes crudas (dedupe boards<->directos "
                "descarto %d; sin scraping)..." % (len(offers), n_dup))
    if prog:
        prog.fase("Gate + reportes")
    newly, _, _ = await _aplicar_gate(db, profile, engine, offers, dias, min_score)
    _reportes(newly)
    if prog:
        prog.sumar("boards", sum(1 for o in offers if source_group(o.source) != GROUP_DIRECTOS))
        prog.sumar("directos", sum(1 for o in offers if source_group(o.source) == GROUP_DIRECTOS))
    return _resumen_progreso(prog, len(newly)) if prog else None


def _fases_totales(searches: list, re_filtrar: bool) -> int:
    """Numero EXACTO de fases (para la barra de la GUI).

    Se calcula sobre las busquedas ya EXPANDIDAS (el motor semantico puede
    duplicarlas), mas los barridos de un solo tiro, el dedupe y el gate.
    """
    if re_filtrar:
        return 2   # re-filtrar + reportes (sin scraping)
    return (1                           # fase 1: preparar (perfil + BD)
            + 1                         # fase 2: navegador Brave
            + len(searches)             # boards por termino (una fase c/u)
            + len(BARRIDOS)             # barridos de un solo tiro
            + 2)                        # dedupe cross-source + gate/reportes


_BARRIDO_GRUPO = {nombre: grupo for nombre, grupo in BARRIDOS}


def _barrido_cfg(nombre: str):
    """(nombre, grupo) para prog.fase(...) — grupo = boards|directos."""
    return nombre, _BARRIDO_GRUPO.get(nombre, "directos")


async def main(fresh: bool = False, searches: list = None, sin_expand: bool = False,
               dias: int = DEFAULT_DIAS, min_score: float = DEFAULT_MIN_SCORE,
               re_filtrar: bool = False, preflight: bool = False):
    """Entrada: bloquea corridas dobles y publica el estado de progreso ANTES
    de tocar la red, para que la GUI (pestana Corrida) muestre que paso."""
    prog = iniciar_corrida("preflight" if preflight else "corrida")
    if not prog:
        return                      # ya hay una corrida viva (PID en uso)
    try:
        resumen = await _corrida(prog, fresh, searches, sin_expand, dias,
                                 min_score, re_filtrar, preflight) or {}
        estado = resumen.pop("_estado",
                             "preflight" if preflight else "ok")
        prog.finalizar(estado, resumen or _resumen_progreso(prog, 0))
    except Exception as e:
        prog.finalizar("error", {"error": str(e)})
        logger.exception("Corrida abortada: %s", e)
        raise


def _resumen_progreso(prog, nuevas: int) -> dict:
    enc = prog._estado.get("encontradas", {})
    return {
        "nuevas": nuevas,
        "crudas_boards": enc.get("boards", 0),
        "crudas_directos": enc.get("directos", 0),
    }


def _preflight(prog, profile, searches: list) -> dict:
    """Dry-run: publica el plan de corrida + estado de credenciales/archivos,
    sin navegador ni requests. Validacion rapida del setup y de la GUI."""
    logger.info("PREFLIGHT: sin red ni navegador; solo reporte de setup.")
    prog.fase("Preflight: plan de corrida")
    for s in searches:
        logger.info("  busqueda: %s / %s", s["puesto"], s["ubicacion"])
    for nombre, grupo in BARRIDOS:
        prog.fase(nombre, grupo, "preflight (sin ejecutar)")
    prog.fase("Preflight: credenciales de login")
    lst = creds_store.lista_sitios_login()
    faltan = [s["empresa"] for s in lst if not s["tiene_credencial"]]
    for s in lst:
        logger.info("  login %-14s %-9s %s%s", s["empresa"],
                    "OBLIGATORIO" if s["obligatorio"] else "opcional",
                    "credencial ✓" if s["tiene_credencial"] else "SIN credencial",
                    f" — auto-login {'ON' if s.get('auto_login') else 'OFF'}")
    if faltan:
        logger.warning("  Faltan credenciales en: %s (python credenciales.py "
                       "--set \"Empresa\")", ", ".join(faltan))
    return {"plan_fases": len(BARRIDOS), "busquedas": len(searches),
            "login_total": len(lst), "login_faltan": faltan}


async def _corrida(prog, fresh: bool, searches: list, sin_expand: bool,
                   dias: int, min_score: float, re_filtrar: bool,
                   preflight: bool):
    # Perfil del candidato: fuente unica de verdad de las tematicas.
    from core.profile import CandidateProfile
    prog.fase("Preparación (perfil + base de datos)")
    profile = CandidateProfile.load()

    db = JobDatabase()
    await db.init_db()

    # --- Modo re-filtrar: sin navegador, reevalua el crudo ya guardado ---
    if re_filtrar:
        prog.total(_fases_totales(searches, re_filtrar))
        engine, _ = _motor(profile, sin_expand, [])
        await refiltrar(db, profile, engine, dias, min_score, prog)
        return

    if searches is None:
        if profile:
            searches = profile.to_searches()
            logger.info("Perfil: %s -> %d temas de busqueda",
                        profile.nombre or profile.path.name, len(searches))
        else:
            searches = [
                {"puesto": "Soporte", "puesto_en": "IT Support", "ubicacion": "Costa Rica"},
                {"puesto": "Soporte Técnico", "puesto_en": "Technical Support", "ubicacion": "Costa Rica"},
                {"puesto": "Mesa de Ayuda", "puesto_en": "Help Desk", "ubicacion": "Costa Rica"},
            ]

    # Motor semantico: expande busquedas y luego decide el gate (Fecha Y relevancia).
    engine, searches = _motor(profile, sin_expand, searches)
    # total de fases DESPUES de expandir: el motor puede duplicar las busquedas.
    prog.total(_fases_totales(searches, re_filtrar))

    # Preflight: publica el plan de corrida (y el estado de las credenciales)
    # SIN navegador ni red. Sirve para validar la GUI y el setup.
    if preflight:
        _preflight(prog, profile, searches)
        return

    if fresh:
        await db.clear_jobs()
        logger.info("Corrida en limpio: base de datos vaciada.")
    await db.clean_empty_jobs()

    browser_cfg = {}
    if os.getenv("BRAVE_BIN"):
        browser_cfg["brave_bin"] = os.getenv("BRAVE_BIN")
    if os.getenv("BRAVE_PERFIL"):
        browser_cfg["brave_perfil"] = os.getenv("BRAVE_PERFIL")

    # ANE: el ISP a veces no resuelve www.ane.cr; usamos un alias que si resuelva (brete.cr)
    # para obtener la IP y forzarla en el navegador.
    ane_ip = resolve_first(["www.ane.cr", "brete.cr", "ane.cr"])
    if ane_ip:
        browser_cfg["host_resolver_rules"] = f"MAP www.ane.cr {ane_ip}"
        logger.info(f"ANE host mapping: www.ane.cr -> {ane_ip}")

    browser_mgr = BraveManager(browser_cfg)
    prog.fase("Navegador (Brave + perfil del usuario)", "",
              "cierra y reabre Brave con depuracion CDP")
    port = browser_mgr.start_brave()

    if not port:
        logger.error("Could not start Brave browser. Exiting.")
        prog.nota("Brave no arranco: revisa BRAVE_BIN en .env")
        return {"_estado": "error", "motivo": "Brave no arranco",
                **_resumen_progreso(prog, 0)}

    # Todo lo crudo de los 5 portales; el gate despues decide que pasa a jobs.
    raw_offers: list = []
    try:
        # --- 1. Portales por termino (keyword) ---
        providers = [ProcomerProvider({}), LinkedInProvider({}), IndeedProvider({})]
        for idx, search in enumerate(searches, 1):
            term = search["puesto"]
            loc = search["ubicacion"]
            logger.info(f"Searching for '{term}' in {loc}...")
            prog.fase(f"Boards por término: {term}", "boards",
                      f"búsqueda {idx}/{len(searches)} · {loc}")

            tasks = []
            for p in providers:
                provider_term = search.get("puesto_en", term) \
                    if isinstance(p, (LinkedInProvider, IndeedProvider)) else term
                if isinstance(p, (LinkedInProvider, IndeedProvider)):
                    tasks.append(p.fetch(provider_term, loc, pages=2, port=port))
                else:
                    tasks.append(p.fetch(provider_term, loc, pages=2))

            results = await asyncio.gather(*tasks, return_exceptions=True)
            for i, res in enumerate(results):
                name = providers[i].__class__.__name__
                if isinstance(res, Exception):
                    logger.error(f"Provider {name} error: {res}")
                elif res is None:
                    logger.warning(f"Provider {name} returned None")
                else:
                    valid = [j for j in res if j.title and j.link]
                    logger.info(f"Provider {name} found {len(valid)} valid jobs")
                    prog.sumar("boards", len(valid))
                    raw_offers.extend(valid)

        # --- 2. ANE por barrido de fecha (UNA sola vez, ignora termino) ---
        # Credenciales: .env (ANE_USER/ANE_PASSWORD) o el almacén local
        # (data/empresas/credenciales.json, pestaña "Credenciales" / CLI).
        _cred_ane = creds_store.credencial("ANE") or {}
        prog.fase(*_barrido_cfg("ANE (barrido por fecha)"))
        ane = AneProvider({
            "username": _cred_ane.get("usuario"),
            "password": _cred_ane.get("password"),
        })
        try:
            ane_jobs = await ane.fetch(
                "barrido-fecha", "Costa Rica", pages=ANE_PAGES, port=port, dias=dias
            )
            valid_ane = [j for j in ane_jobs if j.title and j.link]
            logger.info(f"ANE barrido: {len(valid_ane)} vacantes crudas")
            prog.sumar("boards", len(valid_ane))
            raw_offers.extend(valid_ane)
        except Exception as e:
            logger.error(f"ANE barrido error: {e}")
        # --- 2b. CINDE Jobs: barrido completo UNA sola vez (API GraphQL
        # publica, sin navegador); el gate luego decide por relevancia ---
        prog.fase(*_barrido_cfg("CINDE Jobs"))
        cinde = CindeProvider({})
        try:
            cinde_jobs = await cinde.fetch(
                "barrido-todos", "Costa Rica", pages=CINDE_PAGES
            )
            valid_cinde = [j for j in cinde_jobs if j.title and j.link]
            logger.info(f"CINDE barrido: {len(valid_cinde)} vacantes crudas")
            prog.sumar("boards", len(valid_cinde))
            raw_offers.extend(valid_cinde)
        except Exception as e:
            logger.error(f"CINDE barrido error: {e}")
        # --- 2c. Workday: tenants curados (empresas grandes sin LinkedIn/Indeed) ---
        prog.fase(*_barrido_cfg("Workday"))
        try:
            wd = WorkdayProvider({})
            wd_jobs = await wd.fetch("barrido-workday", "Costa Rica",
                                     pages=WORKDAY_PAGES)
            valid_wd = [j for j in wd_jobs if j.title and j.link]
            logger.info(f"Workday barrido: {len(valid_wd)} vacantes crudas")
            prog.sumar("directos", len(valid_wd))
            raw_offers.extend(valid_wd)
        except Exception as e:
            logger.error(f"Workday barrido error: {e}")
        # --- 2d. Phenom: sitios SSR confirmados (P&G, Zimmer Biomet...) ---
        prog.fase(*_barrido_cfg("Phenom"))
        try:
            from providers.directos.phenom import PhenomProvider
            ph = PhenomProvider({})
            ph_jobs = await ph.fetch("barrido-phenom", "Costa Rica",
                                     pages=PHENOM_PAGES)
            valid_ph = [j for j in ph_jobs if j.title and j.link]
            logger.info(f"Phenom barrido: {len(valid_ph)} vacantes crudas")
            prog.sumar("directos", len(valid_ph))
            raw_offers.extend(valid_ph)
        except Exception as e:
            logger.error(f"Phenom barrido error: {e}")
        # --- 2e. TP Umbraco: el board de Teleperformance via tp.com ---
        prog.fase(*_barrido_cfg("TP (Teleperformance)"))
        try:
            from providers.directos.tp import TpProvider
            tp = TpProvider({})
            tp_jobs = await tp.fetch("barrido-tp", "Costa Rica", pages=TP_PAGES)
            valid_tp = [j for j in tp_jobs if j.title and j.link]
            logger.info(f"TP barrido: {len(valid_tp)} vacantes crudas")
            prog.sumar("directos", len(valid_tp))
            raw_offers.extend(valid_tp)
        except Exception as e:
            logger.error(f"TP barrido error: {e}")
        # --- 2f. Greenhouse: tokens con CR del barrido multi-ATS ---
        prog.fase(*_barrido_cfg("Greenhouse"))
        try:
            from providers.directos.greenhouse import GreenhouseProvider
            gh = GreenhouseProvider({})
            gh_jobs = await gh.fetch("barrido-greenhouse", "Costa Rica",
                                     pages=GREENHOUSE_PAGES)
            valid_gh = [j for j in gh_jobs if j.title and j.link]
            logger.info(f"Greenhouse barrido: {len(valid_gh)} vacantes crudas")
            prog.sumar("directos", len(valid_gh))
            raw_offers.extend(valid_gh)
        except Exception as e:
            logger.error(f"Greenhouse barrido error: {e}")
        # --- 2g. iCIMS: boards TP/DocuSign (listado paginado pr=N) ---
        prog.fase(*_barrido_cfg("iCIMS"))
        try:
            from providers.directos.icims import IcimsProvider
            ii = IcimsProvider({})
            ii_jobs = await ii.fetch("barrido-icims", "Costa Rica", pages=ICIMS_PAGES)
            valid_ii = [j for j in ii_jobs if j.title and j.link]
            logger.info(f"iCIMS barrido: {len(valid_ii)} vacantes crudas")
            prog.sumar("directos", len(valid_ii))
            raw_offers.extend(valid_ii)
        except Exception as e:
            logger.error(f"iCIMS barrido error: {e}")
        # --- 2h. SmartRecruiters: API publica q=Costa Rica por compania ---
        prog.fase(*_barrido_cfg("SmartRecruiters"))
        try:
            from providers.directos.smartrecruiters import SmartRecruitersProvider
            sr = SmartRecruitersProvider({})
            sr_jobs = await sr.fetch("barrido-sr", "Costa Rica", pages=SR_PAGES)
            valid_sr = [j for j in sr_jobs if j.title and j.link]
            logger.info(f"SmartRecruiters barrido: {len(valid_sr)} vacantes crudas")
            prog.sumar("directos", len(valid_sr))
            raw_offers.extend(valid_sr)
        except Exception as e:
            logger.error(f"SmartRecruiters barrido error: {e}")
        # --- 2i. SuccessFactors: RSS publico keywords=(Costa Rica) ---
        prog.fase(*_barrido_cfg("SuccessFactors"))
        try:
            from providers.directos.successfactors import SuccessFactorsProvider
            sf = SuccessFactorsProvider({})
            sf_jobs = await sf.fetch("barrido-sf", "Costa Rica", pages=1)
            valid_sf = [j for j in sf_jobs if j.title and j.link]
            logger.info(f"SuccessFactors barrido: {len(valid_sf)} vacantes crudas")
            prog.sumar("directos", len(valid_sf))
            raw_offers.extend(valid_sf)
        except Exception as e:
            logger.error(f"SuccessFactors barrido error: {e}")
        # --- 2j. Rippling: boards ats.rippling.com con CR real (scan 2026-10-19) ---
        prog.fase(*_barrido_cfg("Rippling"))
        try:
            from providers.directos.rippling import RipplingProvider
            rp = RipplingProvider({})
            rp_jobs = await rp.fetch("barrido-rippling", "Costa Rica", pages=RIPPLING_PAGES)
            valid_rp = [j for j in rp_jobs if j.title and j.link]
            logger.info(f"Rippling barrido: {len(valid_rp)} vacantes crudas")
            prog.sumar("directos", len(valid_rp))
            raw_offers.extend(valid_rp)
        except Exception as e:
            logger.error(f"Rippling barrido error: {e}")
        # --- 2k. ATS ligeros: Ashby/Lever/Recruitee/Workable (tokens JSON) ---
        prog.fase(*_barrido_cfg("ATS ligeros (Ashby/Lever/Recruitee/Workable)"))
        try:
            from providers.directos.ats_ligeros import AtsLigerosProvider
            al = AtsLigerosProvider({})
            al_jobs = await al.fetch("barrido-ats-ligeros", "Costa Rica", pages=AL_PAGES)
            valid_al = [j for j in al_jobs if j.title and j.link]
            logger.info(f"ATS ligeros barrido: {len(valid_al)} vacantes crudas")
            prog.sumar("directos", len(valid_al))
            raw_offers.extend(valid_al)
        except Exception as e:
            logger.error(f"ATS ligeros barrido error: {e}")
        # --- 2l. Sitios con sesion/Cloudflare: browser del usuario (cookies) ---
        prog.fase(*_barrido_cfg("Login sites (navegador del usuario)"))
        try:
            from providers.login.browser_sites import LoginSitesProvider
            ls = LoginSitesProvider({})
            ls_jobs = await ls.fetch("barrido-login-sites", "Costa Rica",
                                     pages=1, port=port)
            valid_ls = [j for j in ls_jobs if j.title and j.link]
            logger.info(f"Login-sites barrido: {len(valid_ls)} vacantes crudas")
            prog.sumar("directos", len(valid_ls))
            raw_offers.extend(valid_ls)
        except Exception as e:
            logger.error(f"Login-sites barrido error: {e}")
    finally:
        browser_mgr.stop_brave()

    # --- 2z. Dedupe cross-source: prioridad a providers DIRECTOS. Si el
    # puesto ya esta en un provider directo (ATS de la empresa), se descarta
    # la copia republicada en los job boards (LinkedIn/Indeed/ANE...). ---
    prog.fase("Dedupe cross-source (boards vs portales de empresa)")
    prev_total = len(raw_offers)
    raw_offers, n_dupes = dedupe_boards_vs_directos(raw_offers)
    logger.info(f"Dedupe: {prev_total} crudas -> {len(raw_offers)} deduplicadas "
                f"({n_dupes} copias de job boards descartadas por existir ya "
                f"en providers directos)")

    # --- 3. Gate (Fecha Y relevancia) -> staging + curado ---
    prog.fase("Gate (fecha + relevancia) y reportes")
    newly, _, _ = await _aplicar_gate(db, profile, engine, raw_offers, dias, min_score)
    _reportes(newly)
    return _resumen_progreso(prog, len(newly))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="JobPulse CR - scraper de empleo")
    parser.add_argument("--fresh", action="store_true", help="Vacia la base de datos antes de la corrida")
    parser.add_argument("--sin-expand", action="store_true",
                        help="Desactiva la expansion semantica de tematicas (default: activa con perfil)")
    parser.add_argument("--dias", type=int, default=DEFAULT_DIAS,
                        help=f"Ventana de fechas del gate (default: {DEFAULT_DIAS})")
    parser.add_argument("--score-min", type=float, default=DEFAULT_MIN_SCORE,
                        help=f"Umbral de relevancia semantica 0-1 (default: {DEFAULT_MIN_SCORE})")
    parser.add_argument("--refiltrar", action="store_true",
                        help="Reaplica el gate sobre jobs_raw sin re-scrapear")
    parser.add_argument("--preflight", action="store_true",
                        help="Dry-run: publica el plan de corrida y el estado de "
                             "credenciales en la GUI, sin navegador ni red")
    args = parser.parse_args()
    asyncio.run(main(
        fresh=args.fresh, sin_expand=args.sin_expand,
        dias=args.dias, min_score=args.score_min, re_filtrar=args.refiltrar,
        preflight=args.preflight,
    ))
