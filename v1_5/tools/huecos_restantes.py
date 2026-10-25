# -*- coding: utf-8 -*-
"""Clasifica los huecos restantes: irrecuperables (desc. basura) vs recuperables.

Uso: python tools/huecos_restantes.py
"""
import sqlite3
import sys
from pathlib import Path

BD = Path(__file__).resolve().parent.parent / "data" / "jobpulse_v15.db"

BASURA = "LENGTH(COALESCE(description_raw,'')) < 200"


def filas(con, sql, args=()):
    return con.execute(sql, args).fetchall()


def main() -> int:
    con = sqlite3.connect(BD)
    huecos = {
        "city": "city IS NULL OR TRIM(COALESCE(city,''))=''",
        "region": "region IS NULL OR TRIM(COALESCE(region,''))=''",
        "work_mode 'unknown'": "work_mode='unknown'",
        "languages": "(languages IS NULL OR languages='' OR languages='{}')",
        "skills": "(skills IS NULL OR skills='' OR skills='[]')",
        "salary (con texto, sin parsear)":
            "salary_min IS NULL AND TRIM(COALESCE(salary_raw,''))<>''",
        "salary (sin texto en la fuente)":
            "salary_min IS NULL AND TRIM(COALESCE(salary_raw,''))=''",
    }
    print(f"{'hueco':<34}{'total':>7}{'de los cuales basura':>22}")
    print("-" * 63)
    for nombre, where in huecos.items():
        total, basura = filas(
            con, f"SELECT COUNT(*), SUM(CASE WHEN {BASURA} THEN 1 ELSE 0 END) "
                 f"FROM jobs WHERE {where}")[0]
        print(f"{nombre:<34}{total:>7}{basura or 0:>22}")

    print("\n-- Jobs con city faltante Y descripcion buena (recuperables en teoria) --")
    for jid, title, loc, n in filas(
            con, f"SELECT job_id, title, location_raw, LENGTH(description_raw) "
                 f"FROM jobs WHERE (city IS NULL OR TRIM(COALESCE(city,''))='') "
                 f"AND NOT {BASURA} ORDER BY job_id"):
        print(f"  [{jid}] {title[:52]} | loc={loc!r} | desc={n}")
    con.close()
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
