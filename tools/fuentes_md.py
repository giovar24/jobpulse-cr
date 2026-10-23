# -*- coding: utf-8 -*-
"""Genera FUENTES.md (raiz del proyecto): referencia de los portales que
entregan vacantes, con su aporte REAL medido desde la base.

Uso:
    python tools/fuentes_md.py            # escribe FUENTES.md en la raiz
    python tools/fuentes_md.py --db data/jobs.db

Conteo por fuente: `jobs_raw` (todo lo que entrego el scraping) y `jobs`
(candidatas aprobadas por el gate, sin duplicados marcados). Las fuentes
configuradas sin vacantes aun se listan aparte.
"""
import argparse
import io
import sqlite3
import sys
from datetime import date
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# Metadatos curados de cada fuente: (nombre publico, URL de referencia, grupo).
# Los ATS "por empresa" apuntan a su portal generico y al catalogo local de
# tableros confirmados con CR (data/empresas/*.json, regenerable).
META = {
    "linkedin": ("LinkedIn Jobs", "https://www.linkedin.com/jobs/", "Job board"),
    "indeed": ("Indeed Costa Rica", "https://cr.indeed.com/", "Job board"),
    "ane": ("ANE / Brete (MTSS)", "https://www.ane.cr/", "Estatal"),
    "cinde": ("CINDE Jobs", "https://cindejobs.com/", "Estatal"),
    "procomer": ("Procomer Talento (Zona Franca)", "https://talento.procomer.com/",
                 "Estatal"),
    "workday": ("Workday", "https://www.myworkdayjobs.com/", "ATS directo"),
    "phenom": ("Phenom", "https://www.phenompeople.com/", "ATS directo"),
    "tp": ("TP (Teleperformance)", "https://www.tp.com/", "ATS directo"),
    "greenhouse": ("Greenhouse", "https://job-boards.greenhouse.io/", "ATS directo"),
    "icims": ("iCIMS", "https://www.icims.com/", "ATS directo"),
    "smartrecruiters": ("SmartRecruiters", "https://careers.smartrecruiters.com/",
                        "ATS directo"),
    "successfactors": ("SAP SuccessFactors", "https://www.successfactors.com/",
                        "ATS directo"),
    "rippling": ("Rippling", "https://ats.rippling.com/", "ATS directo"),
    "ats_ligeros": ("Ashby / Lever / Recruitee / Workable", "https://jobs.ashbyhq.com/",
                    "ATS directo"),
    "login_sites": ("Login sites (navegador del usuario)", "(por empresa)", "ATS directo"),
}

# Notas de las fuentes cuyo tablero es POR EMPRESA (la URL generica no es la
# puerta de entrada; el catalogo local lista los tableros confirmados).
POR_EMPRESA = {
    "workday": "data/empresas/workday_cr.json",
    "phenom": "data/empresas/phenom_cr.json",
    "greenhouse": "data/empresas/otros_ats_cr.json",
    "icims": "data/empresas/icims_boards.json",
    "smartrecruiters": "data/empresas/smartrecruiters_companies.json",
    "successfactors": "data/empresas/successfactors_sites.json",
    "rippling": "data/empresas/rippling_boards.json",
    "ats_ligeros": "data/empresas/otros_ats_cr.json",
    "login_sites": "data/empresas/login_sites.json",
}

GRUPO_ORDEN = {"Estatal": 0, "Job board": 1, "ATS directo": 2}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Genera FUENTES.md con los portales y su aporte real")
    parser.add_argument("--db", default=str(BASE_DIR / "data" / "jobs.db"))
    parser.add_argument("--salida", default=str(BASE_DIR / "FUENTES.md"))
    args = parser.parse_args(argv)

    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    crudo = dict(con.execute("SELECT source, COUNT(*) FROM jobs_raw GROUP BY 1"))
    curada = dict(con.execute(
        "SELECT source, COUNT(*) FROM jobs WHERE TRIM(COALESCE(dup_of,''))='' "
        "GROUP BY 1"))
    con.close()
    total_raw = sum(crudo.values())
    total_cur = sum(curada.values())

    activas = sorted(
        (s for s in crudo if crudo.get(s, 0) > 0),
        key=lambda s: (GRUPO_ORDEN.get(META[s][2], 9), -crudo[s]))
    sin_resultados = sorted(s for s in META if s not in activas)

    hoy = date.today().isoformat()
    lineas = [
        "# FUENTES.md — Portales de vacantes y su aporte real",
        "",
        f"Referencia de los portales que entregan vacantes a JobPulse CR. "
        f"Aporte medido desde la base el **{hoy}**",
        f"({total_raw} crudas · {total_cur} candidatas aprobadas sin duplicados).",
        "",
        "> Regenerable: `python tools/fuentes_md.py` (lee `data/jobs.db`).",
        "",
        "## Portales con resultados",
        "",
        "| Portal | URL | Grupo | Crudo | Candidatas |",
        "|---|---|---|---:|---:|",
    ]
    for s in activas:
        nombre, url, grupo = META[s]
        nota = ""
        if s in POR_EMPRESA:
            nota = " — tableros por empresa"
        lineas.append(f"| [{nombre}]({url}){nota} | {url} | {grupo} | "
                      f"{crudo.get(s, 0)} | {curada.get(s, 0)} |")

    if sin_resultados:
        lineas += ["", "## Configurados, sin vacantes aún", ""]
        for s in sin_resultados:
            nombre, url, grupo = META[s]
            motivo = {
                "tp": "board TP/DocuSign CR sin vacantes activas por ahora",
                "icims": "boards TP/DocuSign archivados o sin CR activo",
                "login_sites": "sitios con login: requieren sesion manual",
            }.get(s, "sin vacantes CR al corte")
            enlace = url if url.startswith("(") else f"({url})"
            lineas.append(f"- **{nombre}** {enlace} — {motivo}.")

    lineas += [
        "",
        "## Fuentes por-empresa",
        "",
        "Varios ATS no tienen una unica pagina: el scraper lleva un catalogo "
        "local de tableros confirmados con presencia Costa Rica "
        "(regenerables, ver README):",
        "",
    ]
    for s, cat in POR_EMPRESA.items():
        if s in activas or s in sin_resultados:
            lineas.append(f"- **{META[s][0]}**: `{cat}`")

    lineas += [
        "",
        "## Notas",
        "",
        "- **Candidatas** = aprobadas por el gate (fecha + relevancia + "
        "dominio), sin duplicados marcados (`dup_of`).",
        "- **Crudo** = todo lo que entrego el scraping a `jobs_raw` "
        "(incluye descartadas por el gate y excluidas por perfil).",
        "- Los **job boards** (LinkedIn, Indeed) y los portales estatales "
        "republican puestos que ya estan en el ATS de la empresa: el dedupe "
        "conserva la version directa cuando ambas se ven.",
        "",
    ]
    salida = Path(args.salida)
    salida.write_text("\n".join(lineas), encoding="utf-8")
    print(f"FUENTES.md generado: {salida} ({len(activas)} activas, "
          f"{len(sin_resultados)} sin resultados, {total_raw} crudo, "
          f"{total_cur} candidatas)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
