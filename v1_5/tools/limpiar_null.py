"""Limpia cadenas 'null' legadas en skills/languages de la BD v1.5."""
from jobpulse_v15.db import Database

db = Database()
db.initialize()
with db.connection() as conn:
    s = conn.execute("SELECT COUNT(*) FROM jobs WHERE skills = 'null'").fetchone()[0]
    l = conn.execute("SELECT COUNT(*) FROM jobs WHERE languages = 'null'").fetchone()[0]
    print(f"antes: skills='null' en {s} jobs | languages='null' en {l} jobs")
    if s or l:
        conn.execute("UPDATE jobs SET skills = NULL WHERE skills = 'null'")
        conn.execute("UPDATE jobs SET languages = NULL WHERE languages = 'null'")
    s = conn.execute("SELECT COUNT(*) FROM jobs WHERE skills = 'null'").fetchone()[0]
    l = conn.execute("SELECT COUNT(*) FROM jobs WHERE languages = 'null'").fetchone()[0]
    print(f"despues: skills='null' en {s} jobs | languages='null' en {l} jobs")
    # estado antes del pase
    sin_skills = conn.execute("SELECT COUNT(*) FROM jobs WHERE skills IS NULL OR skills='[]'").fetchone()[0]
    print(f"jobs sin skills: {sin_skills}")
