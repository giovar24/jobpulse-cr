"""Corre el scrape de fuentes indicadas por argv, igual que la UI.

Uso: python run_scrapes.py cinde procomer [ane indeed linkedin]
"""
import sys

from jobpulse_v15.ane import AneFetcher
from jobpulse_v15.cinde import CindeFetcher
from jobpulse_v15.db import Database
from jobpulse_v15.indeed import IndeedFetcher
from jobpulse_v15.ingestion import IngestionService
from jobpulse_v15.linkedin import LinkedInFetcher
from jobpulse_v15.procomer import ProcomerFetcher
from jobpulse_v15.repository import Repository

FETCHERS = {
    "ane": (AneFetcher, "ane-cr", 9222),
    "cinde": (CindeFetcher, "cinde-cr", None),
    "procomer": (ProcomerFetcher, "procomer-cr", None),
    "indeed": (IndeedFetcher, "indeed-cr", 9222),
    "linkedin": (LinkedInFetcher, "linkedin-cr", 9222),
}

db = Database()
db.initialize()
repo = Repository(db)
service = IngestionService(repo)

fuentes = sys.argv[1:] or list(FETCHERS)

for fuente in fuentes:
    clase, site_id, port = FETCHERS[fuente]
    print(f"\n=== {fuente} ({site_id}) ===", flush=True)
    site = repo.get_site(site_id)
    if not site:
        print(f"  ERROR: sitio {site_id} no existe en BD")
        continue
    if not site.get("enabled"):
        print(f"  ERROR: sitio {site_id} esta pausado")
        continue

    cfg = site.get("site_config") or {}
    pages = cfg.get("pages", 2)
    fetcher = clase()

    try:
        if port is not None:
            kwargs = {"port": port}
            if fuente == "ane":
                kwargs["dias"] = 30
            result = service.fetch_and_import(
                site_id,
                lambda: fetcher.fetch(site, pages=pages, **kwargs),
                run_kind=fuente,
            )
        else:
            result = service.fetch_and_import(
                site_id,
                lambda: fetcher.fetch(site, pages=pages),
                run_kind=fuente,
            )
        print(
            f"  OK -> encontradas={result.get('jobs_found', 0)}, "
            f"validas={result.get('jobs_valid', 0)}, "
            f"nuevas={result.get('jobs_new', 0)}, "
            f"duplicadas={result.get('jobs_duplicate', 0)}"
        )
    except Exception as exc:
        print(f"  ERROR: {type(exc).__name__}: {exc}")
