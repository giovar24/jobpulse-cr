"""Auditoria de datos: totales por fuente y huecos en columnas criticas."""
import sqlite3

conn = sqlite3.connect(r"D:\Proyectos\jobpulse_cr\v1_5\data\jobpulse_v15.db")
conn.row_factory = sqlite3.Row
cur = conn.cursor()

# Mapa job -> fuente via job_source_records
cur.execute("""
    SELECT r.site_id AS fuente, COUNT(*) AS jobs,
           SUM(CASE WHEN j.description_raw IS NULL OR LENGTH(TRIM(j.description_raw)) < 100 THEN 1 ELSE 0 END) AS sin_desc,
           SUM(CASE WHEN j.date_published IS NULL OR j.date_published = '' THEN 1 ELSE 0 END) AS sin_fecha,
           SUM(CASE WHEN j.city IS NULL OR TRIM(j.city) = '' OR LOWER(j.city) = 'unknown' THEN 1 ELSE 0 END) AS sin_ciudad,
           SUM(CASE WHEN j.region IS NULL OR TRIM(j.region) = '' OR LOWER(j.region) = 'unknown' THEN 1 ELSE 0 END) AS sin_region,
           SUM(CASE WHEN j.work_mode IS NULL OR j.work_mode = '' OR j.work_mode = 'unknown' THEN 1 ELSE 0 END) AS sin_work_mode,
           SUM(CASE WHEN j.employment_type IS NULL OR j.employment_type = '' THEN 1 ELSE 0 END) AS sin_tipo,
           SUM(CASE WHEN j.salary_raw IS NULL OR TRIM(j.salary_raw) = '' THEN 1 ELSE 0 END) AS sin_salario,
           SUM(CASE WHEN j.skills IS NULL OR TRIM(j.skills) = '' THEN 1 ELSE 0 END) AS sin_skills,
           SUM(CASE WHEN j.languages IS NULL OR TRIM(j.languages) = '' THEN 1 ELSE 0 END) AS sin_idiomas
    FROM jobs j
    JOIN job_source_records r ON r.job_id = j.job_id
    GROUP BY r.site_id
    ORDER BY r.site_id
""")

cols = [d[0] for d in cur.description]
print(" | ".join(cols))
print("-" * 130)
for row in cur.fetchall():
    print(" | ".join(str(row[c]) for c in cols))

# Total general
cur.execute("SELECT COUNT(*) FROM jobs")
print(f"\nTOTAL jobs: {cur.fetchone()[0]}")

# Ejemplo de vacantes sin descripcion
cur.execute("""
    SELECT j.job_id, j.title, r.site_id, LENGTH(COALESCE(j.description_raw,'')) AS len
    FROM jobs j JOIN job_source_records r ON r.job_id = j.job_id
    WHERE LENGTH(COALESCE(j.description_raw,'')) < 100
    LIMIT 8
""")
filas = cur.fetchall()
if filas:
    print("\nEjemplos con descripcion corta/vacia:")
    for f in filas:
        print(f"  {f['site_id']:12s} len={f['len']:5d} {f['job_id'][:26]:26s} {f['title'][:40]}")

conn.close()
