# -*- coding: utf-8 -*-
"""jobpulse_v15.gate — Gate de relevancia: Fecha Y Relevancia semántica.

El gate decide que vacantes pasan de la tabla staging (jobs_raw) a la tabla
curada (jobs). Criterios:

1. FECHA: vacante dentro de ventana (default 30 dias) Y no vencida
2. RELEVANCIA: score semantico >= umbral (calibrado por modelo de embeddings)
3. DOMINIO: titulo contiene token del dominio objetivo (veto lexico)

El gate NO borra: lo descartado queda en jobs_raw con gate_score/gate_razon.
Se puede re-ejecutar con --refiltrar sin re-scrapear.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from jobpulse_v15.search_engine import SemanticSearchEngine

logger = logging.getLogger("jobpulse.gate")

# ----------------------------------------------------------------------
# Configuracion por modelo de embeddings
# ----------------------------------------------------------------------
# EmbeddingGemma-300m corre mas bajo (mediana ~0.26) vs mpnet (~0.30)
# Escape: score >= escape pasa sin veto de dominio (protege titulos creativos)
_CALIB_POR_MODELO = {
    "google/embeddinggemma-300m": {"min_score": 0.26, "escape": 0.32},
    "sentence-transformers/paraphrase-multilingual-mpnet-base-v2": {"min_score": 0.30, "escape": 0.45},
    # fallback generico
    "default": {"min_score": 0.28, "escape": 0.40},
}

DEFAULT_DIAS = 30
DEFAULT_MIN_SCORE = 0.28  # se resuelve automaticamente por modelo


def _calibracion(modelo: str) -> Dict[str, float]:
    """Resuelve umbrales para el modelo efectivo."""
    modelo_norm = modelo.lower().strip()
    for k, v in _CALIB_POR_MODELO.items():
        if k in modelo_norm:
            return v
    return _CALIB_POR_MODELO["default"]


# ----------------------------------------------------------------------
# Veto lexico de dominio (tokens que definen el dominio objetivo)
# ----------------------------------------------------------------------
# Tokens que SI cuentan como dominio (al menos uno debe aparecer en el titulo)
DOMINIO_TOKENS = {
    # Soporte / Help Desk / Service Desk
    "soporte", "support", "help desk", "service desk", "mesa de ayuda",
    "helpdesk", "service desk", "it support", "technical support",
    # Atencion al cliente
    "cliente", "customer", "atencion", "atención",
    # Bilingue
    "biling", "bilingue", "bilingual",
    # Asistencia tecnica
    "asistencia tecnica", "asistencia técnica", "technical assistance",
    # TI / IT / Sistemas / Redes
    "informatic", "informática", "it ", "ti ", "sistemas", "systems",
    "redes", "network", "infraestructura", "infrastructure",
    # Desarrollo (para perfiles tecnicos)
    "desarrollador", "developer", "programador", "ingeniero", "engineer",
    "analista", "analyst", "arquitecto", "architect",
}

# Tokens de ROL que NO cuentan solos (necesitan dominio)
ROL_TOKENS_IGNORADOS = {
    "tecnico", "technician", "tecnica", "technical",
    "analista", "analyst", "representante", "representative",
    "especialista", "specialist", "coordinador", "coordinator",
    "supervisor", "jefe", "lead", "manager", "gerente",
    "asistente", "assistant", "auxiliar", "auxiliary",
    "practicante", "intern", "junior", "senior", "semi-senior",
}

# Escape tokens (si aparecen, pasa veto aunque no haya token de dominio)
# Protege titulos creativos como "Cloud Support Engineer", "DevOps Support"
ESCAPE_TOKENS = {
    "cloud", "devops", "site reliability", "sre", "platform",
    "automation", "automatizacion", "automatización",
    "security", "seguridad", "ciberseguridad", "cybersecurity",
    "data", "datos", "analytics", "machine learning", "ml ", "ai ",
    "backend", "frontend", "fullstack", "full stack",
    "mobile", "movil", "móvil", "ios", "android",
}


def _normalizar_titulo(titulo: str) -> str:
    """Normaliza titulo para matching: lower, sin acentos, solo alfanumerico/espacios."""
    import unicodedata
    if not titulo:
        return ""
    t = unicodedata.normalize("NFD", titulo.lower())
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    # Conservar espacios, quitar puntuacion extra
    t = re.sub(r"[^a-z0-9\s]", " ", t)
    return " ".join(t.split())


def _contiene_token_dominio(titulo: str) -> bool:
    """True si el titulo normalizado contiene al menos un token de dominio."""
    t = _normalizar_titulo(titulo)
    for token in DOMINIO_TOKENS:
        if f" {token} " in f" {t} " or t.startswith(token + " ") or t.endswith(" " + token):
            return True
    return False


def _contiene_escape_token(titulo: str) -> bool:
    """True si el titulo contiene token de escape (protege titulos creativos)."""
    t = _normalizar_titulo(titulo)
    for token in ESCAPE_TOKENS:
        if f" {token} " in f" {t} " or t.startswith(token + " ") or t.endswith(" " + token):
            return True
    return False


def _es_titulo_rol_puro(titulo: str) -> bool:
    """True si el titulo SOLO tiene tokens de rol (sin token de dominio ni escape)."""
    t = _normalizar_titulo(titulo)
    tokens = set(t.split())
    # Si tiene token de dominio o escape -> NO es rol puro
    for token in DOMINIO_TOKENS | ESCAPE_TOKENS:
        if token in t:
            return False
    # Si solo tiene tokens de rol (y no esta vacio)
    if tokens and tokens.issubset(ROL_TOKENS_IGNORADOS | {"de", "del", "la", "el", "en", "para", "con", "y", "o"}):
        return True
    return False


def es_vigente(expires_at: Optional[str]) -> bool:
    """True si la vacante no ha vencido (expires_at >= hoy)."""
    if not expires_at:
        return True  # sin fecha de vencimiento = no se descarta por vigencia
    try:
        exp_date = date.fromisoformat(expires_at[:10])
        return exp_date >= date.today()
    except (ValueError, TypeError):
        return True  # fecha rara -> beneficio de la duda


def es_reciente(date_published: Optional[str], dias: int = DEFAULT_DIAS) -> bool:
    """True si date_published está dentro de la ventana (hoy - dias)."""
    if not date_published:
        return True  # sin fecha = beneficio de la duda (CINDE, etc.)
    try:
        pub_date = date.fromisoformat(date_published[:10])
        cutoff = date.today() - timedelta(days=dias)
        return pub_date >= cutoff
    except (ValueError, TypeError):
        return True  # fecha rara -> beneficio de la duda


def fecha_efectiva(job: Dict[str, Any]) -> Optional[str]:
    """
    Fecha efectiva para procesamiento: la que trae la pagina si es legible;
    si no (CINDE no publica), se INFIERE la del scrape (created_at).
    """
    pub = job.get("date_published")
    if pub and es_reciente(pub, 365):  # fecha razonable (ultimo año)
        return pub
    # Fallback: created_at del scrape
    created = job.get("created_at")
    if created:
        return created[:10]
    return None


@dataclass
class GateResult:
    """Resultado del gate para una vacante."""
    aprobada: bool
    gate_score: float
    gate_razon: str  # "ok" | "fuera de ventana" | "bajo score" | "veto dominio" | "vencida"
    dominio_ok: bool
    score: float
    min_score_usado: float
    escape_usado: float


def gate_job(
    job: Dict[str, Any],
    profile: Dict[str, Any],
    engine: SemanticSearchEngine,
    dias: int = DEFAULT_DIAS,
    min_score: Optional[float] = None,
    modelo: str = "",
) -> GateResult:
    """
    Evalua una vacante contra el gate (Fecha Y Relevancia Y Dominio).

    Retorna GateResult con aprobada=True/False y razon.
    """
    # 1. Vigencia (fecha de vencimiento del portal)
    if not es_vigente(job.get("expires_at")):
        return GateResult(
            aprobada=False, gate_score=0.0, gate_razon=f"vacante vencida (vence {job.get('expires_at', '?')})",
            dominio_ok=False, score=0.0, min_score_usado=0.0, escape_usado=0.0,
        )

    # 2. Fecha de publicacion (ventana)
    fecha_eff = fecha_efectiva(job)
    if not es_reciente(fecha_eff, dias):
        return GateResult(
            aprobada=False, gate_score=0.0, gate_razon=f"fuera de ventana ({dias} dias, fecha={fecha_eff})",
            dominio_ok=False, score=0.0, min_score_usado=0.0, escape_usado=0.0,
        )

    # 3. Relevancia semantica
    # Construir texto para embedding
    from jobpulse_v15.search_engine import SemanticSearchEngine
    doc_text = engine._build_doc_text(job)
    doc_prompt = engine._get_doc_prompt()
    job_emb = engine._model.encode([doc_prompt + doc_text], convert_to_numpy=True, normalize_embeddings=True)[0]

    # Embedding del perfil (promedio de titulos + skills + temas)
    profile_sections = []
    for section in ["titulos", "skills", "temas"]:
        terms = profile.get(section, [])
        if terms:
            profile_sections.extend(terms)

    if profile_sections:
        profile_embs = engine._model.encode(
            [engine._get_query_prompt() + t for t in profile_sections],
            convert_to_numpy=True, normalize_embeddings=True
        )
        profile_emb = np.mean(profile_embs, axis=0)
        profile_emb = profile_emb / (np.linalg.norm(profile_emb) + 1e-8)
        score = float(job_emb @ profile_emb)
    else:
        # Sin perfil -> score neutro bajo
        score = 0.15

    # 4. Calibracion por modelo
    modelo_eff = modelo or engine.model_name
    calib = _calibracion(modelo_eff)
    min_score_eff = min_score if min_score is not None else calib["min_score"]
    escape_score = calib["escape"]

    # 5. Veto lexico de dominio
    titulo = job.get("title", "")
    dominio_ok = _contiene_token_dominio(titulo)
    escape_ok = _contiene_escape_token(titulo)

    # Logica de veto:
    # - Si dominio_ok -> pasa
    # - Si escape_ok y score >= escape_score -> pasa (protege titulos creativos)
    # - Si score >= escape_score -> pasa (escape global)
    # - Sino -> veto dominio
    veto_dominio = not (dominio_ok or escape_ok or score >= escape_score)

    # 6. Decision final
    aprobada = (score >= min_score_eff) and not veto_dominio

    if veto_dominio:
        razon = f"veto dominio (titulo: '{titulo}', score={score:.3f}, min={min_score_eff:.2f})"
    elif score < min_score_eff:
        razon = f"bajo score ({score:.3f} < {min_score_eff:.2f})"
    else:
        razon = "ok"

    return GateResult(
        aprobada=aprobada,
        gate_score=round(score, 4),
        gate_razon=razon,
        dominio_ok=dominio_ok,
        score=round(score, 4),
        min_score_usado=min_score_eff,
        escape_usado=escape_score,
    )


def gate_jobs(
    jobs: List[Dict[str, Any]],
    profile: Dict[str, Any],
    engine: SemanticSearchEngine,
    dias: int = DEFAULT_DIAS,
    min_score: Optional[float] = None,
) -> Tuple[List[Dict], List[Dict]]:
    """
    Aplica gate a lista de jobs. Retorna (aprobados, descartados).
    Cada job recibe gate_score, gate_razon.
    """
    aprobados = []
    descartados = []

    for job in jobs:
        resultado = gate_job(job, profile, engine, dias, min_score)
        job["gate_score"] = resultado.gate_score
        job["gate_razon"] = resultado.gate_razon
        job["gate_dominio_ok"] = resultado.dominio_ok
        job["gate_min_score"] = resultado.min_score_usado
        job["gate_escape"] = resultado.escape_usado

        if resultado.aprobada:
            aprobados.append(job)
        else:
            descartados.append(job)

    logger.info("Gate: %d aprobados, %d descartados (min_score=%.2f, dias=%d)",
                len(aprobados), len(descartados),
                _calibracion("").get("min_score", 0.28), dias)
    return aprobados, descartados