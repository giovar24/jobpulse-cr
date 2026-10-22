"""Puerta de relevancia (gate): decide que vacantes scrapeadas pasan al ranking.

Filosofia (decision del usuario):
  1. El scraper junta TODO lo crudo de los 5 portales (Procomer, LinkedIn,
     Indeed, ANE, CINDE Jobs) en la tabla `jobs_raw`.
  2. El gate aplica "Fecha Y relevancia Y dominio" con la IA local
     (embeddings) sobre el perfil del candidato y deja lo que sirve en
     `jobs` (la tabla que leen search.py y app.py).
  3. Lo descartado NO se borra: queda en `jobs_raw` con su `gate_razon`, y es
     re-filtrable sin re-scrapear (cambiar umbral, ventana o perfil y
     re-ejecutar el modo --refiltrar). EXCEPCION: lo ya VENCIDO (fecha de
     vencimiento pasada, o publicacion fuera de la ventana cuando el portal no
     publica vencimiento) lo elimina `JobDatabase.purge_expired` al cerrar la
     corrida: una vacante vencida no se conserva ni se re-filtra.

El buscador de ANE esta roto (devuelve 0 incluso a mano), por eso su provider
barre por fecha; aqui es donde, por fin, se filtra por contenido de verdad.

Criterio Fecha Y relevancia Y dominio:
  - Fecha dura: date_published dentro de la ventana (dias) Y, si el portal
    publica fecha de vencimiento (`expires_at`, ej. ANE), que no haya pasado.
    Las vencidas se descartan (y luego `purge_expired` las borra de la base).
  - Relevancia: score semantico perfil<->vacante >= min_score. El umbral se
    calibra POR MODELO de embeddings (`_CALIB_POR_MODELO`): la escala del
    coseno cambia con el modelo y un 0.30 de mpnet no es el mismo 0.30 de
    EmbeddingGemma.
  - Dominio del titulo (veto lexico): el titulo debe contener un token del
    dominio (soporte / atencion al cliente / TI general). Medido con datos reales,
    el embedding del TITULO no separa oficios de soporte ("Mecanico
    Automotriz" obtiene title-score 0.37, mas alto que su propio
    desc-score 0.30): la senal util es lexica, no semantica. Escape: score
    >= escape pasa sin importar el titulo (protege titulos creativos con
    descripcion muy relevante); tambien calibrado por modelo.

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

# Coseno semantico normalizado (0-1) del score del perfil. La ESCALA depende
# del modelo de embeddings, asi que los umbrales se calibran POR MODELO.
# Medido con datos reales (1171 crudas + muestra estratificada vs IA externa,
# informes/calidad/calibracion_gate.md, 2026-10-22):
#   - EmbeddingGemma-300m: dominio p50=0.26, no-dominio p50=0.20; el 0.30
#     heredado de mpnet descartaba ~164 vacantes de dominio validas.
#   - mpnet (fallback): los buenos matches reales rondan 0.35-0.50.
_CALIB_POR_MODELO = {
    "google/embeddinggemma-300m": (0.26, 0.32),
    "paraphrase-multilingual-mpnet": (0.30, 0.45),
}

# Calibracion del modelo activo por defecto (EmbeddingGemma); mpnet conserva
# la suya via _CALIB_POR_MODELO.
DEFAULT_MIN_SCORE, ESCAPE_SCORE = _CALIB_POR_MODELO["google/embeddinggemma-300m"]
DEFAULT_DIAS = 30


def calibracion(modelo: str = "") -> Tuple[float, float]:
    """(min_score, escape_score) calibrados para el modelo resuelto del motor.

    Para un modelo desconocido se usa la escala del modelo configurado por
    defecto (EmbeddingGemma). Si se agrega otro modelo, medir su escala y
    sumar la fila a _CALIB_POR_MODELO en lugar de reusar esta.
    """
    for clave, cal in _CALIB_POR_MODELO.items():
        if clave in (modelo or ""):
            return cal
    return DEFAULT_MIN_SCORE, ESCAPE_SCORE

# Tokens del dominio soporte / atencion al cliente y TI general (ES+EN). Se
# aplica sobre el titulo sin acentos y en minuscula. Tokens de ROL ("tecnico",
# "analista", "representante"...) NO cuentan: "Tecnico de Aire Acondicionado"
# debe caer.
# Los tokens TI van SIEMPRE con \b (palabra completa): "software" no debe
# matchear "softwarez" ni "data" matchear "database" por accidente de subcadena.
# "engineer"/"ingenier" quedan FUERA a proposito: son de rol, no de dominio, y
# arrastrarian oficios ("Ingeniero Mecanico", "Ingeniero Industrial").
# "client"/"itsm"/"moderador|moderat" entran por calibracion con IA externa
# (2026-10-22): "Client Services Representative" (AnswerHero), "Administracion
# funcional de plataformas ITSM" (GBM) y el objetivo del perfil "Moderador de
# Contenido" caian todos fuera del dominio.
_TITLE_TOKENS = re.compile(
    r"(soporte|support|help\s*desk|service\s*desk|helpdesk|"
    r"mesa\s+de\s+(ayuda|asistencia)|customer|cliente|client\b|"
    r"informatic|\bitsm\b|moderador|moderat|biling|"
    r"asistencia\s+tecnica|\bti\b|\bit\b|sistemas|redes|"
    r"\b(software|developer|desarrollador\w*|architect|cloud|data|"
    r"full\s*stack|devops|sysadmin|dba|qa)\b)",
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


def es_vigente(expires_at: Optional[str]) -> bool:
    """False solo si `expires_at` es una fecha ISO ya pasada.

    Sin fecha de vencimiento ('' / None / texto raro) => True: el portal no
    la publica o no se pudo parsear, y no se puede afirmar que vencio.
    """
    try:
        return date.fromisoformat((expires_at or "")[:10]) >= date.today()
    except (ValueError, TypeError):
        return True


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
              min_score: Optional[float] = None) -> Tuple[List[JobOffer], List[JobOffer]]:
    """Aplica "Fecha Y relevancia" a las vacantes crudas.

    Cada JobOffer recibe `gate_score` y `gate_razon` (tambien los descartados,
    para trazabilidad en jobs_raw). Devuelve (aprobados, descartados).

    `min_score` None = usar la calibracion del modelo resuelto del motor
    (`calibracion(engine.model_name)`); explicito (p. ej. --score-min) manda.
    """
    if not offers:
        return [], []

    # --- 1. Filtro de fecha (duro) ---
    recientes: List[JobOffer] = []
    viejos: List[JobOffer] = []
    for o in offers:
        if not es_vigente(o.expires_at):
            o.gate_score = None
            o.gate_razon = f"vacante vencida (vence {o.expires_at})"
            viejos.append(o)
        elif es_reciente(o.date_published, dias):
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
    engine.load(dicts)   # load() resuelve el modelo real (gated -> fallback)
    # Umbrales calibrados para el modelo EFECTIVO; --score-min explicito manda.
    min_cal, esc_cal = calibracion(getattr(engine, "model_name", ""))
    if min_score is None:
        min_score = min_cal
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
        elif not titulo_en_dominio(o.title) and sc < esc_cal:
            o.gate_razon = f"titulo fuera de dominio (score {sc:.2f} < {esc_cal})"
            descartados.append(o)
        else:
            aprobados.append(o)

    logger.info(
        "Gate: %d crudas -> %d vigentes/en ventana (%d dias) -> %d aprobadas "
        "(score >= %.2f); %d descartadas.",
        len(offers), len(recientes), dias, len(aprobados), min_score, len(descartados),
    )
    return aprobados, descartados
