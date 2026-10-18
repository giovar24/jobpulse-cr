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
            await db.execute("""
                CREATE TABLE IF NOT EXISTS jobs (
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
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
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
        """Elimina todos los registros de la tabla jobs (corrida en limpio)."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("DELETE FROM jobs")
            await db.commit()
            deleted = cursor.rowcount
            logger.info(f"Cleared {deleted} rows from DB")
            return deleted

    async def save_jobs(self, jobs: List[JobOffer]) -> List[JobOffer]:
        """Upsert jobs; return only those that are newly inserted."""
        new_jobs = []
        async with aiosqlite.connect(self.db_path) as db:
            for job in jobs:
                if not job.title or not job.link:
                    continue

                cursor = await db.execute(
                    "SELECT 1 FROM jobs WHERE job_id = ?", (job.job_id,)
                )
                exists = await cursor.fetchone()

                if exists:
                    await db.execute(
                        """UPDATE jobs SET
                             title=CASE WHEN title = '' OR title IS NULL THEN ? ELSE title END,
                             company=CASE WHEN company = '' OR company IS NULL THEN ? ELSE company END,
                             location=CASE WHEN location = '' OR location IS NULL THEN ? ELSE location END,
                             link=CASE WHEN link = '' OR link IS NULL THEN ? ELSE link END,
                             date_published=CASE WHEN date_published = '' OR date_published IS NULL THEN ? ELSE date_published END,
                             description=CASE WHEN description = '' OR description IS NULL THEN ? ELSE description END,
                             requirements=CASE WHEN requirements = '' OR requirements IS NULL THEN ? ELSE requirements END,
                             salary=CASE WHEN salary = '' OR salary IS NULL THEN ? ELSE salary END
                           WHERE job_id = ?""",
                        (job.title, job.company, job.location, job.link,
                         job.date_published, job.description, job.requirements,
                         job.salary, job.job_id)
                    )
                else:
                    try:
                        await db.execute(
                            """INSERT INTO jobs (job_id, source, search_term, title, company, location, link, date_published, description, requirements, salary)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (job.job_id, job.source, job.search_term, job.title, job.company,
                             job.location, job.link, job.date_published, job.description,
                             job.requirements, job.salary)
                        )
                        new_jobs.append(job)
                    except aiosqlite.IntegrityError:
                        continue
            await db.commit()
        return new_jobs

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
