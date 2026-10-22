import aiosqlite
import logging
from datetime import date, datetime, timedelta
from typing import List, Dict, Optional
from providers.base import JobOffer
from core.dedupe import clave_duplicado_exacto
from core.text import limpiar_texto

# Para el canonico de un duplicado exacto: gana el portal directo (ATS) sobre
# el agregador. Dentro del mismo grupo, gana la fila mas antigua.
_SOURCES_DIRECTOS = {
    "workday", "greenhouse", "icims", "smartrecruiters", "successfactors",
    "rippling", "phenom", "ats_ligeros", "tp", "login_sites",
}

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
                        expires_at TEXT,
                        description TEXT,
                        requirements TEXT,
                        salary TEXT,
                        gate_score REAL,
                        gate_razon TEXT,
                        ia_score REAL,
                        ia_razon TEXT,
                        ia_model TEXT,
                        ia_hash TEXT,
                        ia_ts TEXT,
                        dup_of TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
            # Migracion: tablas creadas antes del gate no traen estas columnas.
            for tabla in ("jobs", "jobs_raw"):
                cursor = await db.execute(f"PRAGMA table_info({tabla})")
                existentes = {row[1] for row in await cursor.fetchall()}
                for col, tipo in (("gate_score", "REAL"), ("gate_razon", "TEXT"),
                                  ("expires_at", "TEXT"), ("ia_score", "REAL"),
                                  ("ia_razon", "TEXT"), ("ia_model", "TEXT"),
                                  ("ia_hash", "TEXT"), ("ia_ts", "TEXT"),
                                  ("dup_of", "TEXT")):
                    if col not in existentes:
                        await db.execute(f"ALTER TABLE {tabla} ADD COLUMN {col} {tipo}")
                        logger.info(f"Migracion: +{col} en {tabla}")
            # Historial append-only: NUNCA se actualiza ni se borra. Cada
            # evaluacion del LLM queda como etiqueta para calibrar/destilar la
            # IA local (core/ia_calibracion.py).
            await db.execute("""
                CREATE TABLE IF NOT EXISTS ia_evals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    tipo TEXT NOT NULL,
                    proveedor TEXT,
                    modelo TEXT,
                    perfil_hash TEXT,
                    job_id TEXT,
                    job_hash TEXT,
                    score REAL,
                    veredicto TEXT,
                    razon TEXT,
                    payload TEXT,
                    tokens_in INTEGER DEFAULT 0,
                    tokens_out INTEGER DEFAULT 0
                )
            """)
            await db.execute("CREATE INDEX IF NOT EXISTS idx_ia_evals_job "
                             "ON ia_evals(job_id, ts)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_ia_evals_tipo "
                             "ON ia_evals(tipo, ts)")
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

    async def clear_curated(self) -> int:
        """Vacia SOLO la tabla curada (jobs); jobs_raw queda intacta.

        Se usa en --refiltrar: la tabla jobs es derivable de jobs_raw + los
        criterios actuales del gate, asi que se reconstruye desde cero."""
        async with aiosqlite.connect(self.db_path) as db:
            c = await db.execute("DELETE FROM jobs")
            await db.commit()
            logger.info(f"Cleared {c.rowcount} rows (solo tabla jobs; jobs_raw intacta)")
            return c.rowcount

    async def save_jobs(self, jobs: List[JobOffer], tabla: str = "jobs") -> List[JobOffer]:
        """Upsert jobs en `tabla` (jobs o jobs_raw); return only newly inserted."""
        new_jobs = []
        async with aiosqlite.connect(self.db_path) as db:
            for job in jobs:
                # Frontera única de limpieza: todo lo que se persiste (nuevo o
                # re-filtrado desde jobs_raw) pasa por la misma normalización.
                job.search_term = limpiar_texto(job.search_term)
                job.title = limpiar_texto(job.title)
                job.company = limpiar_texto(job.company)
                job.location = limpiar_texto(job.location)
                job.link = limpiar_texto(job.link)
                job.description = limpiar_texto(job.description,
                                                conservar_saltos=True)
                job.requirements = limpiar_texto(job.requirements,
                                                 conservar_saltos=True)
                job.salary = limpiar_texto(job.salary)
                job.date_published = limpiar_texto(job.date_published) or None
                job.expires_at = limpiar_texto(job.expires_at) or None
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
                             expires_at=CASE WHEN ? <> '' THEN ? ELSE expires_at END,
                             description=CASE WHEN description = '' OR description IS NULL THEN ? ELSE description END,
                             requirements=CASE WHEN requirements = '' OR requirements IS NULL THEN ? ELSE requirements END,
                             salary=CASE WHEN salary = '' OR salary IS NULL THEN ? ELSE salary END,
                             gate_score=?, gate_razon=?
                           WHERE job_id = ?""",
                        (job.title, job.company, job.location, job.link,
                         job.date_published, job.expires_at or "",
                         job.expires_at or "", job.description, job.requirements,
                         job.salary, job.gate_score, job.gate_razon, job.job_id)
                    )
                else:
                    try:
                        await db.execute(
                            f"""INSERT INTO {tabla} (job_id, source, search_term, title, company, location, link, date_published, expires_at, description, requirements, salary, gate_score, gate_razon)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (job.job_id, job.source, job.search_term, job.title, job.company,
                             job.location, job.link, job.date_published,
                             job.expires_at or "", job.description,
                             job.requirements, job.salary, job.gate_score, job.gate_razon)
                        )
                        new_jobs.append(job)
                    except aiosqlite.IntegrityError:
                        continue
            await db.commit()
        return new_jobs

    async def purge_expired(self, dias: int = 30) -> int:
        """Borra las vacantes vencidas de `jobs` y `jobs_raw`.

        - Si la fila trae `expires_at` (ej. ANE publica "Fecha de vencimiento
          de la vacante"), se compara ESA fecha con hoy: vencida = se borra.
        - Si no trae vencimiento (CINDE no publica fecha, otros portales no
          exponen cierre), se usa la ventana de publicacion (`dias`), igual
          que el gate.
        - Las filas sin ninguna fecha legible NO se tocan: sin dato no se
          puede afirmar que vencieron.
        """
        hoy = date.today().isoformat()
        corte = (date.today() - timedelta(days=dias)).isoformat()
        total = 0
        async with aiosqlite.connect(self.db_path) as db:
            for tabla in ("jobs", "jobs_raw"):
                cursor = await db.execute(
                    f"""DELETE FROM {tabla} WHERE
                        (expires_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
                         AND expires_at < ?)
                        OR (trim(coalesce(expires_at, '')) = ''
                            AND date_published GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
                            AND date_published < ?)""",
                    (hoy, corte))
                total += cursor.rowcount
            await db.commit()
        if total:
            logger.info("Vigencia: %d filas vencidas borradas (vence < %s; "
                        "sin vencimiento, publicada < %s).", total, hoy, corte)
        else:
            logger.info("Vigencia: 0 filas vencidas (vence < %s; sin "
                        "vencimiento, publicada < %s).", hoy, corte)
        return total

    async def marcar_duplicados_exactos(self, tablas=("jobs_raw", "jobs")) -> int:
        """Marca con `dup_of` los duplicados exactos de cada tabla.

        Clave: empresa + titulo + ubicacion conservadores (sin quitar numeros
        romanos ni ruido). Requiere ubicacion no vacia. El canonico es el portal
        directo y, dentro del mismo grupo, la fila mas antigua. No borra filas:
        las copias quedan marcadas y las vistas pueden ocultarlas.

        Idempotente: recalcula todas las marcas de las tablas indicadas.
        """
        total = 0
        async with aiosqlite.connect(self.db_path) as db:
            for tabla in tablas:
                cols = {r[1] for r in await (await db.execute(
                    f"PRAGMA table_info({tabla})")).fetchall()}
                if not cols:
                    continue
                if "dup_of" not in cols:
                    await db.execute(
                        f"ALTER TABLE {tabla} ADD COLUMN dup_of TEXT")
                filas = await (await db.execute(
                    f"SELECT job_id, company, title, location, source, "
                    f"created_at FROM {tabla}")).fetchall()
                grupos = {}
                for job_id, company, title, location, source, created in filas:
                    clave = clave_duplicado_exacto(company, title, location)
                    if not clave:
                        continue
                    grupos.setdefault(clave, []).append(
                        (job_id, source or "", created or ""))

                marcas = {}
                for items in grupos.values():
                    if len(items) < 2:
                        continue
                    items.sort(key=lambda x: (
                        0 if x[1] in _SOURCES_DIRECTOS else 1, x[2], x[0]))
                    canonico = items[0][0]
                    for job_id, _, _ in items[1:]:
                        marcas[job_id] = canonico

                await db.execute(f"UPDATE {tabla} SET dup_of = NULL")
                if marcas:
                    await db.executemany(
                        f"UPDATE {tabla} SET dup_of = ? WHERE job_id = ?",
                        [(canonico, job_id)
                         for job_id, canonico in marcas.items()])
                total += len(marcas)
                if marcas:
                    logger.info("Duplicados exactos: %d marcados en %s",
                                len(marcas), tabla)
            await db.commit()
        return total

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

    async def save_ia_latest(self, resultados: Dict[str, dict], modelo: str) -> int:
        """Guarda la ULTIMA evaluacion de IA en jobs_raw y (si existe) en jobs.

        `resultados` = {job_id: {"score","razon","hash"}}. El historial
        completo vive aparte en `ia_evals` (append-only).
        """
        if not resultados:
            return 0
        ts = datetime.now().isoformat(timespec="seconds")
        n = 0
        async with aiosqlite.connect(self.db_path) as db:
            for jid, r in resultados.items():
                for tabla in ("jobs_raw", "jobs"):
                    cur = await db.execute(
                        f"""UPDATE {tabla} SET ia_score=?, ia_razon=?, ia_model=?,
                             ia_hash=?, ia_ts=? WHERE job_id=?""",
                        (r.get("score"), r.get("razon"), modelo,
                         r.get("hash"), ts, jid))
                    n += cur.rowcount
            await db.commit()
        return n

    async def registrar_ia(self, registros: List[Dict]) -> int:
        """Inserta evaluaciones en el historial append-only `ia_evals`."""
        if not registros:
            return 0
        filas = [(
            r.get("ts") or datetime.now().isoformat(timespec="seconds"),
            r.get("tipo") or "vacante", r.get("proveedor"), r.get("modelo"),
            r.get("perfil_hash"), r.get("job_id"), r.get("job_hash"),
            r.get("score"), r.get("veredicto"), r.get("razon"),
            r.get("payload"), int(r.get("tokens_in") or 0),
            int(r.get("tokens_out") or 0),
        ) for r in registros]
        async with aiosqlite.connect(self.db_path) as db:
            await db.executemany(
                """INSERT INTO ia_evals (ts, tipo, proveedor, modelo, perfil_hash,
                     job_id, job_hash, score, veredicto, razon, payload,
                     tokens_in, tokens_out)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                filas)
            await db.commit()
        return len(filas)

    async def get_ia_evals(self, tipo: Optional[str] = None) -> List[Dict]:
        """Historial completo de evaluaciones IA (mas viejo primero)."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            q = "SELECT * FROM ia_evals"
            params: list = []
            if tipo:
                q += " WHERE tipo = ?"
                params.append(tipo)
            q += " ORDER BY ts ASC, id ASC"
            cursor = await db.execute(q, params)
            return [dict(r) for r in await cursor.fetchall()]

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
