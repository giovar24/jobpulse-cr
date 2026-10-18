import argparse
import asyncio
import logging
import os
import re
import socket
import pandas as pd
from datetime import datetime, timedelta
from dotenv import load_dotenv
from core.database import JobDatabase
from core.browser_manager import BraveManager
from providers.procomer import ProcomerProvider
from providers.linkedin import LinkedInProvider
from providers.indeed import IndeedProvider
from providers.ane import AneProvider

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("jobpulse")


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
        "Descripción": resume_text(j.description),
        "Requisitos": resume_text(j.requirements),
    }


async def main(fresh: bool = False, searches: list = None, sin_expand: bool = False):
    # Perfil del candidato: fuente unica de verdad de las tematicas.
    from core.profile import CandidateProfile
    profile = CandidateProfile.load()
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

    # Expansion semantica integrada: usa los titulos del perfil como vocabulario
    # preferente. Con perfil definido corre por defecto (--sin-expand la apaga).
    if profile and not sin_expand:
        from core.search_engine import SemanticSearchEngine
        logger.info("Expandiendo tematicas del perfil...")
        engine = SemanticSearchEngine()
        searches = engine.expand_searches(
            searches, top_n=2, vocab_es=profile.titulos, vocab_en=profile.titulos
        )

    db = JobDatabase()
    await db.init_db()
    if fresh:
        await db.clear_jobs()
        logger.info("Corrida en limpio: base de datos vaciada.")
    await db.clean_empty_jobs()

    providers = [
        ProcomerProvider({}),
        LinkedInProvider({}),
        IndeedProvider({}),
        AneProvider({
            "username": os.getenv("ANE_USER"),
            "password": os.getenv("ANE_PASSWORD"),
        }),
    ]

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

    try:
        all_new_jobs = []
        for search in searches:
            term = search["puesto"]
            loc = search["ubicacion"]
            logger.info(f"Searching for '{term}' in {loc}...")

            tasks = []
            for p in providers:
                provider_term = term
                if isinstance(p, (LinkedInProvider, IndeedProvider)):
                    provider_term = search.get("puesto_en", term)
                elif isinstance(p, AneProvider):
                    provider_term = search["puesto"]

                if isinstance(p, (LinkedInProvider, IndeedProvider, AneProvider)):
                    tasks.append(p.fetch(provider_term, loc, pages=2, port=port))
                else:
                    tasks.append(p.fetch(provider_term, loc, pages=2))

            results = await asyncio.gather(*tasks, return_exceptions=True)

            for i, res in enumerate(results):
                provider_name = providers[i].__class__.__name__
                if isinstance(res, Exception):
                    logger.error(f"Provider {provider_name} error: {res}")
                elif res is None:
                    logger.warning(f"Provider {provider_name} returned None")
                else:
                    valid = [j for j in res if j.title and j.link]
                    logger.info(f"Provider {provider_name} found {len(valid)} valid jobs")
                    newly_added = await db.save_jobs(valid)
                    logger.info(f"Provider {provider_name}: {len(newly_added)} are new")
                    all_new_jobs.extend(newly_added)

        stamp = datetime.now().strftime("%Y%m%d")
        if all_new_jobs:
            logger.info(f"Found {len(all_new_jobs)} new jobs today!")
            df = pd.DataFrame([job_row(j) for j in all_new_jobs])
            report_name = f"data/report_{stamp}.csv"
            df.to_csv(report_name, index=False, encoding="utf-8-sig")
            logger.info(f"Report generated: {report_name}")

            now = datetime.now()
            threshold = (now - timedelta(days=1)).strftime("%Y-%m-%d")
            today = now.strftime("%Y-%m-%d")
            # Solo fechas válidas dentro de las últimas 24 h (excluye vacías y futuras).
            recent = [j for j in all_new_jobs if threshold <= (j.date_published or "") <= today]
            if recent:
                df24 = pd.DataFrame([job_row(j) for j in recent])
                name24 = f"data/report_24h_{stamp}.csv"
                df24.to_csv(name24, index=False, encoding="utf-8-sig")
                logger.info(f"Publicadas en las últimas 24 h: {len(recent)} (guardadas en {name24})")
            else:
                logger.info("Sin publicaciones en las últimas 24 horas.")
        else:
            logger.info("No new jobs found.")

    finally:
        browser_mgr.stop_brave()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="JobPulse CR - scraper de empleo")
    parser.add_argument("--fresh", action="store_true", help="Vacia la base de datos antes de la corrida")
    parser.add_argument("--sin-expand", action="store_true",
                        help="Desactiva la expansion semantica de tematicas (default: activa con perfil)")
    args = parser.parse_args()
    asyncio.run(main(fresh=args.fresh, sin_expand=args.sin_expand))
