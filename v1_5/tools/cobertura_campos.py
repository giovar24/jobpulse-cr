"""Resumen final de cobertura de campos tras el pase IA."""
from jobpulse_v15.db import Database

db = Database()
db.initialize()
with db.connection() as conn:
    def q(sql):
        return conn.execute(sql).fetchone()[0]

    total = q("SELECT COUNT(*) FROM jobs")
    checks = [
        ("work_mode resuelto (no unknown/vacio)",
         "SELECT COUNT(*) FROM jobs WHERE work_mode IN ('remote','hybrid','onsite')"),
        ("work_mode = unknown (legitimo)",
         "SELECT COUNT(*) FROM jobs WHERE work_mode = 'unknown'"),
        ("con city", "SELECT COUNT(*) FROM jobs WHERE COALESCE(city,'') != ''"),
        ("con region", "SELECT COUNT(*) FROM jobs WHERE COALESCE(region,'') != ''"),
        ("con date_published", "SELECT COUNT(*) FROM jobs WHERE COALESCE(date_published,'') != ''"),
        ("con employment_type", "SELECT COUNT(*) FROM jobs WHERE COALESCE(employment_type,'') != ''"),
        ("con salary_min", "SELECT COUNT(*) FROM jobs WHERE salary_min IS NOT NULL"),
        ("con skills (no vacia)",
         "SELECT COUNT(*) FROM jobs WHERE skills IS NOT NULL"
         " AND skills NOT IN ('null', '[]') AND TRIM(skills) != '[]'"),
        ("con languages (no vacio)",
         "SELECT COUNT(*) FROM jobs WHERE languages IS NOT NULL"
         " AND languages NOT IN ('null', '{}') AND TRIM(languages) != '{}'"),
    ]
    print(f"TOTAL JOBS: {total}\n")
    for nombre, sql in checks:
        n = q(sql)
        print(f"  {nombre}: {n} ({n * 100 // total}%)")
