"""Buscador semantico interactivo sobre las vacantes de jobs.db.

Uso:
    python search.py "soporte tecnico bilingue"
    python search.py --top 5 --dias 7 --fuente indeed "analista de datos"
    python search.py --perfil             # ranking segun data/profile.json
    python search.py --perfil mi.json     # ranking segun otro archivo de perfil
    python search.py                      # modo interactivo

Filtros duros opcionales (--fuente, --dias) se aplican ANTES de ordenar
semanticamente, como recomienda la propuesta del buscador local.
"""
import argparse
import asyncio
import logging
import re
import sys

from dotenv import load_dotenv

from core.database import JobDatabase
from core.profile import CandidateProfile, DEFAULT_PROFILE_PATH
from core.search_engine import SemanticSearchEngine

load_dotenv()

if sys.stdout and sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

SOURCES = ["indeed", "linkedin", "ane", "procomer"]


def _safe(text):
    """CSV/consola: cola cualquier cosa no imprimible."""
    return (text or "").strip()


def fmt_job(row, with_desc=False, with_ats=False):
    lines = [
        f"{row['score']*100:5.1f}%  [{row['source']}] {row['title']}",
        f"        {_safe(row.get('company')) or '-'}" +
        (f" | {row['location']}" if row.get("location") else "") +
        (f" | pub: {row['date_published']}" if row.get("date_published") else ""),
        f"        {row['link']}",
    ]
    if with_desc:
        res = re.sub(r"\s+", " ", (row.get("description") or ""))[:200]
        lines.append(f"        > {res}{'...' if len(res) == 200 else ''}")
    if row.get("secciones"):
        brk = " | ".join(f"{k} {v*100:.0f}%" for k, v in row["secciones"].items())
        lines.insert(1, f"        ({brk})")
    if with_ats and row.get("ats"):
        a = row["ats"]
        lines.insert(1, f"        ATS {a['score']:5.1f}% [{a['verdict']:>14}]"
                        f" | combinado {row.get('combined', 0):5.1f}%")
        if a["missing"]:
            lines.insert(2, f"        keywords faltantes: {', '.join(a['missing'][:6])}")
    return "\n".join(lines)


def engine_for(engine, days, fuente):
    """Recarga el indice aplicando filtros duros sobre la DB."""
    db = JobDatabase()
    jobs = asyncio.run(db.get_all_jobs(days=days))
    if fuente:
        jobs = [j for j in jobs if (j.get("source") or "").lower() == fuente.lower()]
    engine.load(jobs)


def print_perfil(profile, engine, top):
    from core.ats import rank_with_ats
    result = engine.search_profile(profile, top_k=max(top, 20))
    if not result:
        print("  (sin resultados: ajusta los filtros o verifica la DB)\n")
        return
    ranked = rank_with_ats(profile.to_dict(), result, engine)
    print(f"\nMejores opciones para: {profile.nombre or profile.path.name}")
    print("(orden = 60% similitud semantica + 40% score ATS; ver core/ats.py)")
    for r in ranked[:top]:
        print(fmt_job(r, with_desc=True, with_ats=True))
    print()


def interactive(args):
    engine = SemanticSearchEngine(cache_name=args.cache)
    profile = CandidateProfile.load(args.perfil) if args.perfil else None
    filters = {"dias": args.dias, "fuente": args.fuente}
    if profile and not filters["dias"]:
        filters["dias"] = profile.filtros.get("dias")
    engine_for(engine, filters["dias"], filters["fuente"])
    print("\nBuscador semantico JobPulse CR (escribe 'salir' para terminar).")
    print("Comandos: 'dias N' | 'fuente X' | 'perfil' | 'salir'")
    if profile:
        print(f"Perfil activo: {profile.nombre or profile.path.name}\n")
    else:
        print("\n")
    while True:
        try:
            q = input("🔎 ").strip()
        except (KeyboardInterrupt, EOFError):
            break
        if not q:
            continue
        cmd = q.split(maxsplit=1)
        if cmd[0].lower() in ("salir", "exit", "quit"):
            break
        if cmd[0].lower() == "dias" and len(cmd) > 1 and cmd[1].strip().isdigit():
            filters["dias"] = int(cmd[1].strip())
            filters["fuente"] = None
            print(f"Filtro dias activado: {filters['dias']} (fuente liberada)")
            engine_for(engine, filters["dias"], filters["fuente"])
        elif cmd[0].lower() == "fuente":
            source = cmd[1].strip().lower() if len(cmd) > 1 else None
            if source and source not in SOURCES:
                print("Fuentes validas:", ", ".join(SOURCES))
                continue
            filters["fuente"] = source
            print(f"Filtro fuente activado: {source or 'todas'}")
            engine_for(engine, filters["dias"], filters["fuente"])
        elif cmd[0].lower() == "perfil":
            if not profile:
                profile = CandidateProfile.load(args.perfil or DEFAULT_PROFILE_PATH)
                if not profile:
                    print(f"No existe {DEFAULT_PROFILE_PATH}; copia data/profile.example.json.")
                    continue
                if profile.filtros.get("dias"):
                    filters["dias"] = profile.filtros["dias"]
                engine_for(engine, filters["dias"], filters["fuente"])
            print_perfil(profile, engine, args.top)
        else:
            results = engine.search(q, top_k=args.top)
            if not results:
                print("  (sin resultados: ajusta los filtros o verifica la DB)\n")
                continue
            for r in results:
                print(fmt_job(r, with_desc=True))
            print()


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logging.getLogger("sentence_transformers.SentenceTransformer").setLevel(logging.ERROR)

    p = argparse.ArgumentParser(description="Busqueda semantica local sobre las vacantes guardadas")
    p.add_argument("query", nargs="*", help="Consulta en lenguaje natural")
    p.add_argument("--top", type=int, default=10, help="Cantidad de resultados")
    p.add_argument("--dias", type=int, default=None, help="Solo vacantes de los últimos N dias")
    p.add_argument("--fuente", default=None, choices=SOURCES, help="Filtrar por portal")
    p.add_argument("--perfil", nargs="?", const=str(DEFAULT_PROFILE_PATH), default=None,
                   metavar="ARCHIVO",
                   help="Ranking por perfil del candidato (default: data/profile.json)")
    p.add_argument("--cache", default="embeddings_cache", help="Nombre base del cache de embeddings")
    args = p.parse_args()

    profile = CandidateProfile.load(args.perfil) if args.perfil else None
    query = " ".join(args.query).strip()
    if not query and not profile:
        interactive(args)
        return

    engine = SemanticSearchEngine(cache_name=args.cache)
    dias = args.dias
    fuente = args.fuente
    if profile and not dias:
        dias = profile.filtros.get("dias")
    engine_for(engine, dias, fuente)
    if profile and not query:
        print_perfil(profile, engine, args.top)
        return
    if profile:
        print(f"(nota: consulta libre activa; usa --perfil sin texto para el ranking del perfil)")
    for r in engine.search(query, top_k=args.top):
        print(fmt_job(r))


if __name__ == "__main__":
    main()
