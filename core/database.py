import aiosqlite
import logging
from typing import List, Dict, Optional
from providers.base import JobOffer

logger = logging.getLogger("jobpulse.db")


class JobDatabase:
    def __init__(self, db_path: Optional[str] = None):
        if db_path is None:
            import os
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            self.db_path = os.path.join(base_dir, "data", "jobs.db")
        else:
            self.db_path = db_path

    async def init_db(self):
        async with aiosqlite.connect(self.db_path) as db:
            for tabla in ("jobs", "jobs_raw"):
                await db.execute(f"""
                    CREATE TABLE IF NOT EXISTS {tabla} (
                        job_id TEXT PRIMARY KEY,
                        source TEXT,
                        search_term TEXT,
                        title TEXT,
                        company TEXT,
                        location TEXT,
                        link TEXT,
                        date_published TEXT,
                        description TEXT,
                        requirements TEXT,
                        salary TEXT,
                        gate_score REAL,
                        gate_razon TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
            # Migracion: tablas creadas antes del gate no traen estas columnas.
            for tabla in ("jobs", "jobs_raw"):
                cursor = await db.execute(f"PRAGMA table_info({tabla})")
                existentes = {row[1] for row in await cursor.fetchall()}
                for col, tipo in (("gate_score", "REAL"), ("gate_razon", "TEXT")):
                    if col not in existentes:
                        await db.execute(f"ALTER TABLE {tabla} ADD COLUMN {col} {tipo}")
                        logger.info(f"Migracion: +{col} en {tabla}")
            await db.commit()

    async def clean_empty_jobs(self) -> int:
        """Delete placeholder rows with no title and no link."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "DELETE FROM jobs WHERE (title = '' OR title IS NULL) AND (link = '' OR link IS NULL)"
            )
            await db.commit()
            deleted = cursor.rowcount
            if deleted:
                logger.info(f"Removed {deleted} empty placeholder rows from DB")
            return deleted

    async def clear_jobs(self) -> int:
        """Elimina todos los registros de ambas tablas (corrida en limpio)."""
        async with aiosqlite.connect(self.db_path) as db:
            c1 = await db.execute("DELETE FROM jobs")
            c2 = await db.execute("DELETE FROM jobs_raw")
            await db.commit()
            deleted = c1.rowcount + c2.rowcount
            logger.info(f"Cleared {deleted} rows (jobs + jobs_raw) from DB")
            return deleted

    async def save_jobs(self, jobs: List[JobOffer], tabla: str = "jobs") -> List[JobOffer]:
        """Upsert jobs en `tabla` (jobs o jobs_raw); return only newly inserted."""
        new_jobs = []
        async with aiosqlite.connect(self.db_path) as db:
            for job in jobs:
                if not job.title or not job.link:
                    continue

                cursor = await db.execute(
                    f"SELECT 1 FROM {tabla} WHERE job_id = ?", (job.job_id,)
                )
                exists = await cursor.fetchone()

                if exists:
                    await db.execute(
                        f"""UPDATE {tabla} SET
                             title=CASE WHEN title = '' OR title IS NULL THEN ? ELSE title END,
                             company=CASE WHEN company = '' OR company IS NULL THEN ? ELSE company END,
                             location=CASE WHEN location = '' OR location IS NULL THEN ? ELSE location END,
                             link=CASE WHEN link = '' OR link IS NULL THEN ? ELSE link END,
                             date_published=CASE WHEN date_published = '' OR date_published IS NULL THEN ? ELSE date_published END,
                             description=CASE WHEN description = '' OR description IS NULL THEN ? ELSE description END,
                             requirements=CASE WHEN requirements = '' OR requirements IS NULL THEN ? ELSE requirements END,
                             salary=CASE WHEN salary = '' OR salary IS NULL THEN ? ELSE salary END,
                             gate_score=?, gate_razon=?
                           WHERE job_id = ?""",
                        (job.title, job.company, job.location, job.link,
                         job.date_published, job.description, job.requirements,
                         job.salary, job.gate_score, job.gate_razon, job.job_id)
                    )
                else:
                    try:
                        await db.execute(
                            f"""INSERT INTO {tabla} (job_id, source, search_term, title, company, location, link, date_published, description, requirements, salary, gate_score, gate_razon)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (job.job_id, job.source, job.search_term, job.title, job.company,
                             job.location, job.link, job.date_published, job.description,
                             job.requirements, job.salary, job.gate_score, job.gate_razon)
                        )
                        new_jobs.append(job)
                    except aiosqlite.IntegrityError:
                        continue
            await db.commit()
        return new_jobs

    async def save_raw_jobs(self, jobs: List[JobOffer]) -> List[JobOffer]:
        """Guarda lo crudo en la tabla de staging jobs_raw."""
        return await self.save_jobs(jobs, tabla="jobs_raw")

    async def get_raw_jobs(self) -> List[Dict]:
        """Devuelve todo lo crudo (staging) para poder re-filtrar sin re-scrapear."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM jobs_raw ORDER BY created_at DESC"
            )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def update_gate(self, job_id: str, score, razon: str, tabla: str = "jobs_raw"):
        """Persiste el resultado del gate sobre una fila (trazabilidad del descarte)."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                f"UPDATE {tabla} SET gate_score = ?, gate_razon = ? WHERE job_id = ?",
                (score, razon, job_id)
            )
            await db.commit()

    async def get_recent_jobs(self, hours: int = 24) -> List[Dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM jobs WHERE created_at >= datetime('now', ?)",
                (f'-{hours} hours',)
            )
            return await cursor.fetchall()

    async def get_all_jobs(self, days: Optional[int] = None) -> List[Dict]:
        """Devuelve todas las vacantes con datos suficientes para indexar.

        Si `days` se indica, limita a las creadas (DB) en los ultimos N dias.
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            query = (
                "SELECT job_id, source, search_term, title, company, location, link, "
                "date_published, description, requirements, salary, created_at "
                "FROM jobs "
                "WHERE title != '' AND title IS NOT NULL "
                "AND ((description != '' AND description IS NOT NULL) "
                "OR (requirements != '' AND requirements IS NOT NULL))"
            )
            params: list = []
            if days:
                query += " AND created_at >= datetime('now', ?)"
                params.append(f'-{days} days')
            query += " ORDER BY created_at DESC"
            cursor = await db.execute(query, params)
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]
