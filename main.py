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
from providers.procomer import ProcomerProvider
from providers.linkedin import LinkedInProvider
from providers.indeed import IndeedProvider
from providers.ane import AneProvider
from providers.base import JobOffer

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


async def refiltrar(db, profile, engine, dias, min_score):
    """Re-aplica el gate sobre jobs_raw SIN re-scrapear (valor de la 2da tabla).

    La tabla curada `jobs` se RECONSTRUYE: lo que aprobo una corrida anterior
    pero ya no pasa los criterios actuales sale de `jobs` (nunca de jobs_raw).
    """
    raw = await db.get_raw_jobs()
    if not raw:
        logger.warning("jobs_raw vacia: corre una corrida normal primero.")
        return
    await db.clear_curated()
    offers = [dict_a_offer(r) for r in raw]
    logger.info("Re-filtrando %d vacantes crudas (sin scraping)..." % len(offers))
    newly, _, _ = await _aplicar_gate(db, profile, engine, offers, dias, min_score)
    _reportes(newly)


async def main(fresh: bool = False, searches: list = None, sin_expand: bool = False,
               dias: int = DEFAULT_DIAS, min_score: float = DEFAULT_MIN_SCORE,
               re_filtrar: bool = False):
    # Perfil del candidato: fuente unica de verdad de las tematicas.
    from core.profile import CandidateProfile
    profile = CandidateProfile.load()

    db = JobDatabase()
    await db.init_db()

    # --- Modo re-filtrar: sin navegador, reevalua el crudo ya guardado ---
    if re_filtrar:
        engine, _ = _motor(profile, sin_expand, [])
        await refiltrar(db, profile, engine, dias, min_score)
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
    port = browser_mgr.start_brave()

    if not port:
        logger.error("Could not start Brave browser. Exiting.")
        return

    # Todo lo crudo de los 4 portales; el gate despues decide que pasa a jobs.
    raw_offers: list = []
    try:
        # --- 1. Portales por termino (keyword) ---
        providers = [ProcomerProvider({}), LinkedInProvider({}), IndeedProvider({})]
        for search in searches:
            term = search["puesto"]
            loc = search["ubicacion"]
            logger.info(f"Searching for '{term}' in {loc}...")

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
                    raw_offers.extend(valid)

        # --- 2. ANE por barrido de fecha (UNA sola vez, ignora termino) ---
        ane = AneProvider({
            "username": os.getenv("ANE_USER"),
            "password": os.getenv("ANE_PASSWORD"),
        })
        try:
            ane_jobs = await ane.fetch(
                "barrido-fecha", "Costa Rica", pages=ANE_PAGES, port=port, dias=dias
            )
            valid_ane = [j for j in ane_jobs if j.title and j.link]
            logger.info(f"ANE barrido: {len(valid_ane)} vacantes crudas")
            raw_offers.extend(valid_ane)
        except Exception as e:
            logger.error(f"ANE barrido error: {e}")
    finally:
        browser_mgr.stop_brave()

    # --- 3. Gate (Fecha Y relevancia) -> staging + curado ---
    newly, _, _ = await _aplicar_gate(db, profile, engine, raw_offers, dias, min_score)
    _reportes(newly)


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
    args = parser.parse_args()
    asyncio.run(main(
        fresh=args.fresh, sin_expand=args.sin_expand,
        dias=args.dias, min_score=args.score_min, re_filtrar=args.refiltrar,
    ))
