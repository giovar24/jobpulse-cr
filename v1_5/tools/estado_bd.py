"""Consulta rapida del estado de la BD."""
from jobpulse_v15.db import Database

db = Database()
db.initialize()
with db.connection() as conn:
    total = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    con_desc = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE description_raw IS NOT NULL"
        " AND LENGTH(description_raw) > 200"
    ).fetchone()[0]
    print("total jobs:", total)
    print("con descripcion >200 chars:", con_desc)
    print()
    print("tablas:", [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()])
    print()
    rows = conn.execute(
        """
        SELECT p.name, COUNT(*) n
        FROM jobs j
        JOIN job_source_records r ON r.job_id = j.job_id
        JOIN source_sites s ON s.site_id = r.site_id
        JOIN source_platforms p ON p.platform_id = s.platform_id
        GROUP BY p.name ORDER BY n DESC
        """
    ).fetchall()
    for r in rows:
        print(f"  {r[0]}: {r[1]}")
    print()
    # jobs sin city/region/work_mode
    faltan_city = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE city IS NULL OR TRIM(COALESCE(city,''))=''"
    ).fetchone()[0]
    faltan_wm = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE work_mode IS NULL OR TRIM(COALESCE(work_mode,''))=''"
    ).fetchone()[0]
    print("faltan city:", faltan_city, "| faltan work_mode:", faltan_wm)
