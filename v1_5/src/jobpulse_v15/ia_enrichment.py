# -*- coding: utf-8 -*-
"""
jobpulse_v15.ia_enrichment — Enriquecimiento de jobs usando IA externa.

Enriquece SOLO campos críticos faltantes usando prompts simples y secuenciales:
- CRÍTICOS: city, region, work_mode, date_published
- SECUNDARIOS (opcionales): employment_type, salary, skills, languages

Uso:
    from jobpulse_v15.ia_enrichment import run_ia_enrichment
    import asyncio
    result = asyncio.run(run_ia_enrichment(limit=50))
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Dict, List, Optional

# Import IA client
try:
    from jobpulse_v15.ia_api import IAClient, IAError, IAModeloError, IACuotaError
    IA_AVAILABLE = True
except ImportError:
    IA_AVAILABLE = False
    IAClient = None
    IAError = Exception
    IAModeloError = Exception
    IACuotaError = Exception

logger = logging.getLogger("jobpulse.ia_enrich")


# ─── Schemas simples por campo ───

LOCATION_SCHEMA = {
    "type": "object",
    "properties": {
        "city": {"type": ["string", "null"]},
        "region": {"type": ["string", "null"]}
    },
    "required": ["city", "region"],
    "additionalProperties": False
}

WORK_MODE_SCHEMA = {
    "type": "object",
    "properties": {
        "work_mode": {"type": "string", "enum": ["remote", "hybrid", "onsite", "unknown"]}
    },
    "required": ["work_mode"],
    "additionalProperties": False
}

DATE_SCHEMA = {
    "type": "object",
    "properties": {
        "date_published": {"type": ["string", "null"]}
    },
    "required": ["date_published"],
    "additionalProperties": False
}

EMPLOYMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "employment_type": {"type": ["string", "null"], "enum": ["Full-time", "Part-time", "Contract", "Internship", None]}
    },
    "required": ["employment_type"],
    "additionalProperties": False
}

SALARY_SCHEMA = {
    "type": "object",
    "properties": {
        "salary_min": {"type": ["number", "null"]},
        "salary_max": {"type": ["number", "null"]},
        "salary_currency": {"type": ["string", "null"]},
        "salary_period": {"type": ["string", "null"]}
    },
    "required": ["salary_min", "salary_max", "salary_currency", "salary_period"],
    "additionalProperties": False
}

SKILLS_SCHEMA = {
    "type": "object",
    "properties": {
        "skills": {"type": "array", "items": {"type": "string"}}
    },
    "required": ["skills"],
    "additionalProperties": False
}

LANGUAGES_SCHEMA = {
    "type": "object",
    "properties": {
        "languages": {"type": "object", "additionalProperties": {"type": "string"}}
    },
    "required": ["languages"],
    "additionalProperties": False
}


# ─── Prompts simples y enfocados ───

LOCATION_PROMPT = """Eres experto en ubicaciones de Costa Rica.
IMPORTANTE: city = CANTÓN (ej: Escazú, Belén, San Carlos), region = PROVINCIA (ej: San José, Heredia, Alajuela). NO intercambiar.

Formatos comunes:
- "Provincia / Cantón / Distrito" → city=Cantón, region=Provincia
- "Empresa, Cantón Provincia" → city=Cantón, region=Provincia (filtrar nombre empresa)
- "Cantón, Provincia" → city=Cantón, region=Provincia
- "Solo Provincia" → city=null, region=Provincia
- "Remote" / "Virtual" → city=null, region=null

Ejemplos:
- "San José / Escazú / Escazú" → {"city": "Escazú", "region": "San José"}
- "HCLTech, Evolution Free Zone" → {"city": "Evolution Free Zone", "region": "Heredia"}
- "HCLTech, Belén Heredia" → {"city": "Belén", "region": "Heredia"}
- "Guanacaste" → {"city": null, "region": "Guanacaste"}
- "Remote" → {"city": null, "region": null}

Responde SOLO JSON: {"city": "...", "region": "..."}"""

WORK_MODE_PROMPT = """Detecta modalidad de trabajo EXPLÍCITA en el texto.
- "remote" / "100% remoto" / "teletrabajo" -> remote
- "híbrido" / "hybrid" / "mixto" / "semipresencial" -> hybrid  
- "presencial" / "onsite" / "oficina" / "en sitio" -> onsite
- Si no está explícito -> unknown

Responde SOLO JSON: {"work_mode": "..."}"""

DATE_PROMPT = """Extrae fecha de publicación EXPLÍCITA (ISO YYYY-MM-DD).
Formatos válidos: "2024-10-15", "15 octubre 2024", "15 de octubre de 2024".
NO uses fechas relativas ("hace 3 días", "2 weeks ago").
Si no hay fecha explícita -> null.

Responde SOLO JSON: {"date_published": "..."}"""

EMPLOYMENT_PROMPT = """Extrae tipo de empleo SOLO si está EXPLÍCITO:
- "Full-time" / "tiempo completo" -> Full-time
- "Part-time" / "medio tiempo" -> Part-time
- "Contract" / "contrato" / "freelance" -> Contract
- "Internship" / "pasantía" / "practicante" -> Internship
Si no está explícito -> null.

Responde SOLO JSON: {"employment_type": "..."}"""

SALARY_PROMPT = """Extrae salario SOLO si está EXPLÍCITO con números.
Responde: min, max, currency (CRC/USD), period (monthly/annual/hourly).
Si no hay salario explícito -> todos null.

Responde SOLO JSON: {"salary_min": ..., "salary_max": ..., "salary_currency": "...", "salary_period": "..."}"""

SKILLS_PROMPT = """Extrae lista de skills TÉCNICAS explícitas (Python, AWS, SQL, React, Docker, Kubernetes, Java, JavaScript, etc.).
NO incluyas soft skills ni requisitos genéricos.
Responde SOLO JSON: {"skills": ["...", "..."]}"""

LANGUAGES_PROMPT = """Extrae idiomas CON NIVEL CEFR explícito (A1, A2, B1, B2, C1, C2, nativo).
Ejemplo: "Inglés B2", "English C1", "Español nativo".
Formato: {"ingles": "B2", "frances": "A2"}.
Si no hay nivel explícito -> no incluir.

Responde SOLO JSON: {"languages": {...}}"""


async def _extract_field(
    client: IAClient,
    system_prompt: str,
    user_text: str,
    schema: Dict,
    field_name: str,
    max_tokens: int = 300
) -> Optional[Dict]:
    """Extrae un campo individual con reintentos (3 intentos, backoff)."""
    ultimo_error = None
    for attempt in range(3):
        try:
            resp = await client.chat_json(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_text}
                ],
                schema=schema,
                temperature=0.1,
                max_tokens=max_tokens
            )
            return resp
        except Exception as e:
            ultimo_error = e
            if attempt < 2:
                await asyncio.sleep(1.0 * (attempt + 1))  # 1s, 2s
    logger.warning(f"IA extract {field_name} fallo tras 3 intentos: {ultimo_error}")
    return None


async def enrich_job_fields(job: Dict[str, Any], client: IAClient) -> Dict[str, Any]:
    """Enriquece un job campo por campo (secuencial simple)."""
    filled = {}
    user_parts = []
    
    # Construir texto base una sola vez
    if job.get("title"):
        user_parts.append(f"TÍTULO: {job['title']}")
    if job.get("company"):
        user_parts.append(f"EMPRESA: {job['company']}")
    if job.get("location_raw"):
        user_parts.append(f"UBICACIÓN_RAW: {job['location_raw']}")
    if job.get("description_raw"):
        user_parts.append(f"DESCRIPCIÓN: {job['description_raw'][:2000]}")
    if job.get("requirements_raw"):
        user_parts.append(f"REQUISITOS: {job['requirements_raw'][:1500]}")
    if job.get("salary_raw"):
        user_parts.append(f"SALARIO: {job['salary_raw']}")
    
    full_text = "\n\n".join(user_parts)
    location_raw = job.get("location_raw", "")
    
    # 1. LOCATION (crítico)
    if job.get("city") is None and location_raw:
        location_text = f"Ubicación: {location_raw}"
        result = await _extract_field(client, LOCATION_PROMPT, location_text, LOCATION_SCHEMA, "location")
        if result and result.get("city") is not None:
            filled["city"] = result["city"]
        if result and result.get("region") is not None:
            filled["region"] = result["region"]
    
    # 2. WORK_MODE (crítico)
    if job.get("work_mode") in (None, "", "unknown"):
        result = await _extract_field(client, WORK_MODE_PROMPT, full_text, WORK_MODE_SCHEMA, "work_mode")
        if result and result.get("work_mode") != "unknown":
            filled["work_mode"] = result["work_mode"]
    
    # 3. DATE_PUBLISHED (crítico)
    if job.get("date_published") in (None, ""):
        result = await _extract_field(client, DATE_PROMPT, full_text, DATE_SCHEMA, "date_published")
        if result and result.get("date_published"):
            filled["date_published"] = result["date_published"]
    
    # 4. SECUNDARIOS (solo si el job no los tiene)
    
    # Employment type
    if not job.get("employment_type"):
        result = await _extract_field(client, EMPLOYMENT_PROMPT, full_text, EMPLOYMENT_SCHEMA, "employment_type", 150)
        if result and result.get("employment_type"):
            filled["employment_type"] = result["employment_type"]
    
    # Salary
    if job.get("salary_min") is None and job.get("salary_raw"):
        result = await _extract_field(client, SALARY_PROMPT, full_text, SALARY_SCHEMA, "salary", 200)
        if result and result.get("salary_min") is not None:
            filled["salary_min"] = result["salary_min"]
            filled["salary_max"] = result["salary_max"]
            filled["salary_currency"] = result["salary_currency"]
            filled["salary_period"] = result["salary_period"]
    
    # Skills
    # Skills 600 tokens: la lista puede ser larga y un JSON truncado
    # aborta el parseo (antes 300 truncaba y se perdian skills).
    if not job.get("skills") and (job.get("description_raw") or job.get("requirements_raw")):
        result = await _extract_field(client, SKILLS_PROMPT, full_text, SKILLS_SCHEMA, "skills", 600)
        if result and result.get("skills"):
            filled["skills"] = result["skills"]
    
    # Languages
    if not job.get("languages"):
        result = await _extract_field(client, LANGUAGES_PROMPT, full_text, LANGUAGES_SCHEMA, "languages", 200)
        if result and result.get("languages"):
            filled["languages"] = result["languages"]
    
    return filled


async def enrich_job(job: Dict[str, Any], client: IAClient) -> Optional[Dict[str, Any]]:
    """Enriquece un job campo por campo."""
    try:
        filled = await enrich_job_fields(job, client)
        return filled  # Return even empty dict for partial enrichment
    except Exception as e:
        logger.debug(f"IA enrich failed {job.get('job_id')}: {e}")
        return None


def needs_critical_enrichment(job: Dict[str, Any]) -> bool:
    """Verifica si el job necesita enriquecimiento CRÍTICO."""
    return (
        (job.get("city") is None and job.get("location_raw")) or
        job.get("work_mode") in (None, "", "unknown") or
        job.get("date_published") in (None, "")
    )


def needs_secondary_enrichment(job: Dict[str, Any]) -> bool:
    """Verifica si el job necesita campos SECUNDARIOS (solo pase completo)."""
    return (
        not job.get("employment_type") or
        (job.get("salary_min") is None and bool(job.get("salary_raw"))) or
        not job.get("skills") or
        not job.get("languages")
    )


async def verificar_servidor_ia(client: IAClient) -> Optional[str]:
    """Preflight: verifica que el servidor IA responde antes del pase.

    Devuelve None si responde, o un mensaje de error si no. Evita lanzar
    el pase completo cuando el backend esta apagado (mil fallas en fila).
    """
    try:
        await client.chat_json(
            messages=[
                {"role": "system", "content": "Responde SOLO JSON."},
                {"role": "user", "content": 'Responde: {"ok": true}'},
            ],
            schema={
                "type": "object",
                "properties": {"ok": {"type": "boolean"}},
                "required": ["ok"],
                "additionalProperties": False,
            },
            temperature=0.0,
            max_tokens=20,
        )
        return None
    except Exception as e:
        return (
            f"Servidor IA no responde en {getattr(client, 'base_url', '?')} "
            f"(modelo {getattr(client, 'modelo', '?')}): {e}. "
            "Arranca llama-server y reintenta."
        )


def get_ia_client() -> Optional[IAClient]:
    """Crea cliente IA si está configurado."""
    if not IA_AVAILABLE:
        return None
    try:
        return IAClient()
    except Exception as e:
        logger.warning(f"IA no configurada: {e}")
        return None


# Escaparate de guardado: campo IA -> (columna jobs, valor a escribir).
# skills/languages se serializan a JSON; el resto es texto/numero directo.
MAPEO_SAVE = {
    "city": ("city", lambda j: j.get("city")),
    "region": ("region", lambda j: j.get("region")),
    "work_mode": ("work_mode", lambda j: j.get("work_mode")),
    "date_published": ("date_published", lambda j: j.get("date_published")),
    "employment_type": ("employment_type", lambda j: j.get("employment_type")),
    "salary_min": ("salary_min", lambda j: j.get("salary_min")),
    "salary_max": ("salary_max", lambda j: j.get("salary_max")),
    "salary_currency": ("salary_currency", lambda j: j.get("salary_currency")),
    "salary_period": ("salary_period", lambda j: j.get("salary_period")),
    "skills": ("skills", lambda j: json.dumps(j.get("skills") or [], ensure_ascii=False)),
    "languages": ("languages", lambda j: json.dumps(j.get("languages") or {}, ensure_ascii=False)),
}


async def run_ia_enrichment(
    db_path: str = "data/jobpulse_v15.db",
    limit: int = 50,
    only_critical: bool = True
) -> Dict[str, Any]:
    """
    Pipeline principal: carga jobs sin campos críticos, enriquece campo por campo, guarda.
    """
    from dotenv import load_dotenv
    load_dotenv()  # asegura IA_* disponibles tambien desde UI/scripts

    if not IA_AVAILABLE:
        return {"error": "IA client no disponible (import failed)"}
    
    ia_client = get_ia_client()
    if not ia_client:
        return {"error": "IA no configurada (ver .env: IA_PROVEEDOR, IA_BASE_URL, IA_MODEL, IA_API_KEY)"}

    # Preflight: si el backend esta apagado, abortar de inmediato y no
    # generar una avalancha de fallos job por job.
    error_preflight = await verificar_servidor_ia(ia_client)
    if error_preflight:
        return {"error": error_preflight}
    
    from jobpulse_v15.db import Database
    from jobpulse_v15.repository import Repository
    
    db = Database(db_path)
    db.initialize()
    repo = Repository(db)
    
    # Cargar jobs que necesitan enriquecimiento
    with db.connection() as conn:
        conn.row_factory = __import__('sqlite3').Row
        cur = conn.cursor()
        
        if only_critical:
            cur.execute("""
                SELECT job_id FROM jobs
                WHERE (city IS NULL AND location_raw IS NOT NULL AND location_raw != '')
                   OR work_mode IS NULL OR work_mode = 'unknown' OR work_mode = ''
                   OR date_published IS NULL OR date_published = ''
                LIMIT ?
            """, (limit,))
        else:
            cur.execute("SELECT job_id FROM jobs LIMIT ?", (limit,))
        
        job_ids = [row[0] for row in cur.fetchall()]
    
    if not job_ids:
        return {"stats": {"total": 0, "enriched": 0, "skipped": 0, "failed": 0}, "message": "No hay jobs para enriquecer"}
    
    # Cargar jobs completos
    jobs = []
    for jid in job_ids:
        job = repo.get_job(jid)
        if not job:
            continue
        if only_critical:
            if needs_critical_enrichment(job):
                jobs.append(job)
        elif needs_critical_enrichment(job) or needs_secondary_enrichment(job):
            # Pase completo: tambien jobs que solo faltan secundarios.
            jobs.append(job)
    
    if not jobs:
        return {"stats": {"total": len(job_ids), "enriched": 0, "skipped": len(job_ids), "failed": 0}, "message": "Todos los jobs ya tienen campos críticos"}
    
    # Enriquecer en lotes
    stats = {"total": len(jobs), "enriched": 0, "skipped": 0, "failed": 0, "fields_filled": {"city": 0, "region": 0, "work_mode": 0, "date_published": 0, "employment_type": 0, "salary": 0, "skills": 0, "languages": 0}}
    
    batch_size = 3  # Lotes pequeños para modelo pequeño
    updated_jobs = []
    
    for i in range(0, len(jobs), batch_size):
        batch = jobs[i:i+batch_size]
        logger.info(f"IA lote {i // batch_size + 1}/{-(-len(jobs) // batch_size)}: {len(batch)} jobs")
        
        # Procesar en paralelo dentro del lote
        tasks = [enrich_job(job, ia_client) for job in batch]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        for job, filled in zip(batch, results):
            if isinstance(filled, Exception) or not filled:
                stats["failed"] += 1
                continue

            # Merge: solo llenar lo que falte y anotar SOLO los campos
            # realmente cambiados (lo que governa el UPDATE posterior).
            tocado = []
            for field, value in filled.items():
                if field not in MAPEO_SAVE or value in (None, "", [], {}):
                    continue
                if field == "work_mode":
                    vacio = job.get(field) in (None, "", "unknown")
                elif field in ("salary_min", "salary_max", "salary_currency", "salary_period"):
                    vacio = job.get(field) is None
                else:
                    vacio = job.get(field) in (None, "", [], {})
                if not vacio:
                    continue
                job[field] = value
                tocado.append(field)
            if not tocado:
                stats["skipped"] += 1  # la IA no devolvio nada nuevo
                continue
            job["_ia_fields_filled"] = tocado
            stats["enriched"] += 1
            for field in tocado:
                if field.startswith("salary"):
                    continue  # el grupo se cuenta 1 vez por job
                if field in stats["fields_filled"]:
                    stats["fields_filled"][field] += 1
            if any(f.startswith("salary") for f in tocado):
                stats["fields_filled"]["salary"] += 1
            updated_jobs.append(job)

    # Guardar en BD: SOLO las columnas tocadas (nunca sobreescribir
    # skills/languages con 'null' por campos que no se consultaron).
    db_updated = 0
    from jobpulse_v15.db import Database
    db = Database(db_path)
    for job in updated_jobs:
        tocado = job.get("_ia_fields_filled") or []
        columnas = [MAPEO_SAVE[f][0] for f in tocado]
        valores = [MAPEO_SAVE[f][1](job) for f in tocado]
        with db.connection() as conn:
            conn.execute(
                f"UPDATE jobs SET {', '.join(c + ' = ?' for c in columnas)}"
                " WHERE job_id = ?",
                (*valores, job["job_id"]),
            )
            if conn.total_changes > 0:
                db_updated += 1
    
    stats["db_updated"] = db_updated
    return {"stats": stats, "message": f"Enriquecidos {stats['enriched']}/{stats['total']}, BD actualizados: {db_updated}"}


if __name__ == "__main__":
    import asyncio
    from dotenv import load_dotenv
    load_dotenv()
    
    result = asyncio.run(run_ia_enrichment(limit=5))
    print(json.dumps(result, indent=2, ensure_ascii=False))