"""Puerta de relevancia (gate): decide que vacantes scrapeadas pasan al ranking.

Filosofia (decision del usuario):
  1. El scraper junta TODO lo crudo de los 5 portales (Procomer, LinkedIn,
     Indeed, ANE, CINDE Jobs) en la tabla `jobs_raw`.
  2. El gate aplica "Fecha Y relevancia Y dominio" con la IA local
     (embeddings) sobre el perfil del candidato y deja lo que sirve en
     `jobs` (la tabla que leen search.py y app.py).
  3. Lo descartado NO se borra: queda en `jobs_raw` con su `gate_razon`, y es
     re-filtrable sin re-scrapear (cambiar umbral, ventana o perfil y
     re-ejecutar el modo --refiltrar).

El buscador de ANE esta roto (devuelve 0 incluso a mano), por eso su provider
barre por fecha; aqui es donde, por fin, se filtra por contenido de verdad.

Criterio Fecha Y relevancia Y dominio:
  - Fecha dura: date_published dentro de la ventana (dias).
  - Relevancia: score semantico perfil<->vacante >= min_score.
  - Dominio del titulo (veto lexico): el titulo debe contener un token del
    dominio (soporte / atencion al cliente / TI...). Medido con datos reales,
    el embedding del TITULO no separa oficios de soporte ("Mecanico
    Automotriz" obtiene title-score 0.37, mas alto que su propio
    desc-score 0.30): la senal util es lexica, no semantica. Escape: score
    >= ESCAPE_SCORE pasa sin importar el titulo (protege titulos creativos
    con descripcion muy relevante).

Un perfil ausente degrada con seguridad a "solo fecha" (no hay criterio de
relevancia sin perfil). Una fecha no parseable no descarta por fecha: deja que
la IA decida (beneficio de la duda).
"""
import logging
import re
import unicodedata
from datetime import date, timedelta
from typing import List, Optional, Tuple

from core.dates import parse_date
from providers.base import JobOffer

logger = logging.getLogger("jobpulse.gate")

# Coseno semantico normalizado (0-1) del score del perfil. Los buenos matches
# reales rondan 0.35-0.5; la basura (operarios, bodegas...) cae por debajo.
DEFAULT_MIN_SCORE = 0.30
DEFAULT_DIAS = 30

# Score al que una vacante pasa aunque su titulo no tenga tokens del dominio.
ESCAPE_SCORE = 0.45

# Tokens del dominio soporte / atencion al cliente (ES+EN). Se aplica sobre el
# titulo sin acentos y en minuscula. Tokens de ROL ("tecnico", "analista",
# "representante"...) NO cuentan: "Tecnico de Aire Acondicionado" debe caer.
_TITLE_TOKENS = re.compile(
    r"(soporte|support|help\s*desk|service\s*desk|helpdesk|"
    r"mesa\s+de\s+(ayuda|asistencia)|customer|cliente|informatic|"
    r"biling|asistencia\s+tecnica|\bti\b|\bit\b|sistemas|redes)",
    re.IGNORECASE)


def _deacc(s: str) -> str:
    s = unicodedata.normalize("NFD", s or "")
    return "".join(c for c in s if unicodedata.category(c) != "Mn")


def titulo_en_dominio(title: Optional[str]) -> bool:
    """True si el titulo contiene un token del dominio (veto lexico)."""
    return bool(_TITLE_TOKENS.search(_deacc(title).lower()))


def es_reciente(date_published: Optional[str], dias: int) -> bool:
    """True si la fecha esta dentro de la ultima ventana (dias)."""
    iso = parse_date(date_published or "")
    try:
        d = date.fromisoformat(iso[:10])
    except (ValueError, TypeError):
        return True  # sin fecha parseable: que decida la IA
    return d >= date.today() - timedelta(days=dias)


def _a_dict(o: JobOffer) -> dict:
    return {
        "job_id": o.job_id, "source": o.source, "search_term": o.search_term,
        "title": o.title, "company": o.company, "location": o.location,
        "link": o.link, "date_published": o.date_published,
        "description": o.description, "requirements": o.requirements,
        "salary": o.salary,
    }


def gate_jobs(offers: List[JobOffer], profile, engine,
              dias: int = DEFAULT_DIAS,
              min_score: float = DEFAULT_MIN_SCORE) -> Tuple[List[JobOffer], List[JobOffer]]:
    """Aplica "Fecha Y relevancia" a las vacantes crudas.

    Cada JobOffer recibe `gate_score` y `gate_razon` (tambien los descartados,
    para trazabilidad en jobs_raw). Devuelve (aprobados, descartados).
    """
    if not offers:
        return [], []

    # --- 1. Filtro de fecha (duro) ---
    recientes: List[JobOffer] = []
    viejos: List[JobOffer] = []
    for o in offers:
        if es_reciente(o.date_published, dias):
            recientes.append(o)
        else:
            o.gate_score = None
            o.gate_razon = f"fuera de ventana ({dias} dias)"
            viejos.append(o)

    if not recientes:
        logger.info("Gate: 0/%d vacantes en ventana de %d dias.", len(offers), dias)
        return [], viejos

    # --- 2. Sin perfil: solo fecha (no hay criterio de relevancia) ---
    if profile is None:
        for o in recientes:
            o.gate_score = None
            o.gate_razon = "sin perfil: solo filtro de fecha"
        logger.info("Gate: %d/%d recientes pasan (sin perfil, solo fecha).",
                    len(recientes), len(offers))
        return recientes, viejos

    # --- 3. Filtro semantico (IA local) sobre los que pasaron fecha ---
    dicts = [_a_dict(o) for o in recientes]
    engine.load(dicts)
    scored = engine.search_profile(profile, top_k=len(dicts))
    score_por_id = {s.get("job_id"): s for s in scored}

    aprobados: List[JobOffer] = []
    descartados: List[JobOffer] = list(viejos)
    for o, d in zip(recientes, dicts):
        s = score_por_id.get(d["job_id"])
        sc = float(s["score"]) if s and s.get("score") is not None else 0.0
        o.gate_score = round(sc, 4)
        if sc < min_score:
            o.gate_razon = f"baja relevancia ({sc:.2f} < {min_score})"
            descartados.append(o)
        elif not titulo_en_dominio(o.title) and sc < ESCAPE_SCORE:
            o.gate_razon = f"titulo fuera de dominio (score {sc:.2f} < {ESCAPE_SCORE})"
            descartados.append(o)
        else:
            aprobados.append(o)

    logger.info(
        "Gate: %d crudas -> %d en ventana (%d dias) -> %d aprobadas "
        "(score >= %.2f); %d descartadas.",
        len(offers), len(recientes), dias, len(aprobados), min_score, len(descartados),
    )
    return aprobados, descartados
