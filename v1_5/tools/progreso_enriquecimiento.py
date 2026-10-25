"""Progreso del enriquecimiento IA: cuenta faltantes por campo (criticos + secundarios).

Uso: python tools/progreso_enriquecimiento.py
"""
import sqlite3
import sys
from pathlib import Path

BD = Path(__file__).resolve().parent.parent / "data" / "jobpulse_v15.db"

CAMPOS = [
    # criticos
    ("city", "description_raw IS NOT NULL AND TRIM(description_raw)<>''"),
    ("region", "description_raw IS NOT NULL AND TRIM(description_raw)<>''"),
    ("work_mode", "description_raw IS NOT NULL AND TRIM(description_raw)<>''"),
    ("date_published", "1=1"),
    # secundarios
    ("employment_type", "1=1"),
    ("salary_raw", "1=1"),
    ("skills", "1=1"),
    ("languages", "1=1"),
]


def main() -> int:
    if not BD.exists():
        print(f"ERROR: no existe {BD}")
        return 1
    con = sqlite3.connect(BD)
    con.row_factory = sqlite3.Row
    total = con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    print(f"BD: {BD.name} | jobs: {total}\n")
    print(f"{'campo':<18}{'faltantes':>10}{'% completo':>12}")
    print("-" * 40)
    for campo, filtro in CAMPOS:
        elegibles = con.execute(
            f"SELECT COUNT(*) FROM jobs WHERE {filtro}"
        ).fetchone()[0]
        if elegibles == 0:
            continue
        faltan = con.execute(
            f"""SELECT COUNT(*) FROM jobs
                WHERE {filtro} AND ({campo} IS NULL OR TRIM({campo})=''
                      OR LOWER({campo}) IN ('null','none','unknown'))"""
        ).fetchone()[0]
        pct = 100.0 * (elegibles - faltan) / elegibles
        print(f"{campo:<18}{faltan:>10}{pct:>11.1f}%")
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
