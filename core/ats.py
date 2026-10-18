"""Capa ATS (Applicant Tracking System) para JobPulse CR.

Comportamiento basado en la investigacion de ATS reales (Oracle AI Rating,
experimentos LiveCareer, guias ATS 2025-2026):

  - Extraccion de requisitos de la vacante: anios, nivel de educacion,
    idiomas requeridos y keywords (hard skills) con su frecuencia.
  - Score 0-100 ponderado y configurable (los pesos deben sumar 1):
        skills 0.40 | titulo 0.30 | experiencia 0.15 | educacion 0.075 | idiomas 0.075
  - Componente semantico del titulo (embeddings, no solo string exacto:
    "Project Manager" ~= "Gestor de proyectos").
  - Brechas de palabras clave (keyword gaps): skills de la oferta que faltan
    en el perfil -> "75% de los CVs se rechazan por faltan keywords".
  - Umbrales clasicos: >=85 fuerte, 70-84 ajustable, 40-69 debil, <40 rechazo.
"""
import re
import unicodedata
from typing import Dict, List, Optional, Tuple

DEFAULT_WEIGHTS = {
    "skills": 0.40,
    "titulo": 0.30,
    "experiencia": 0.15,
    "educacion": 0.075,
    "idiomas": 0.075,
}

# Orden de estudios (jerarquia tipica de ATS).
_EDU_ORDER = {
    "ninguno": 0, "basico": 1, "bachiller": 2, "tecnico": 3,
    "tecnologo": 4, "universitario": 5, "maestria": 6,
}

_STOP = set("""
para como entre sobre desde hasta más mas sus ese esta estan tiene tienen
desde para ser sera trabajo empresa equipo area areas puestos puesto role
job work team empresa desarrollar busqueda busqueda herramientas herramienta
etc dias dia anual anos anios years year""".split())


def deaccent(text: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", text or "")
        if unicodedata.category(c) != "Mn"
    ).lower()


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", deaccent(text)).strip()


def extract_years_required(text: str) -> Optional[float]:
    """Mayor 'N anios de experiencia' mencionado en la vacante."""
    hits = [float(m) for m in re.findall(
        r"(\d{1,2})\s*\+?\s*(?:años|anos|years)(?:\s+de\s+experiencia)?",
        _norm(text), flags=re.IGNORECASE)]
    return max(hits) if hits else None


def extract_education_required(text: str) -> Optional[str]:
    """Solo dispara si hay contexto academico: 'nivel academico: tecnico',
    'titulo universitario', 'formacion tecnica'... (evita 'soporte tecnico N1')."""
    t = _norm(text)
    if re.search(r"maestr[íi]a|master'?s? degree|maestria", t, re.I):
        return "maestria"
    if re.search(r"universitario|licenciatura|ingenier[íi]a|bachelor|grado universitario"
                 r"|titulo profesional|carrera universitaria"
                 r"|(?<!high )\bdegree\b(?!\s*of)", t, re.I):   # "degree in X" (EN)
        return "universitario"
    if re.search(r"tecn[oó]logo|technologist", t, re.I):
        return "tecnologo"
    if re.search(r"nivel acad[ée]mico[^.]{0,60}t[eé]cnico|t[ií]tulo t[eé]cnico"
                 r"|grado t[eé]cnico|formaci[óo]n t[eé]cnica|technical degree"
                 r"|associate degree", t, re.I):
        return "tecnico"
    if re.search(r"bachiller(?:ato)?|high school|secundaria", t, re.I):
        return "bachiller"
    return None


# Idioma requerido en la vacante: nombre canonico ES + aliases EN/ES
_LANG_REQ = {
    "Ingles": ("ingles", "english"),
    "Portugues": ("portugues", "portuguese"),
    "Frances": ("frances", "french"),
    "Aleman": ("aleman", "german"),
}


def extract_languages_required(text: str) -> List[str]:
    t = _norm(text)
    out = []
    for canon, alias in _LANG_REQ.items():
        if any(re.search(rf"\b{a}\b", t) for a in alias):
            out.append(canon)
    return out


def job_keywords(job: Dict, lexicon: List[str]) -> List[str]:
    """Hard skills de la oferta: solo coincidencias del lexicon en el texto.

    (Los tokens del titulo no cuentan como keywords: palabras genericas como
    "agente"/"lider" las cubre mejor el componente semantico del titulo.)
    """
    text = _norm(" ".join([
        job.get("title") or "", job.get("description") or "",
        job.get("requirements") or "",
    ]))
    found = []
    for term in lexicon:
        pat = re.compile(r"(?<![\w-])" + re.escape(_norm(term)) + r"(?![\w-])")
        if pat.search(text) and term not in found:
            found.append(term)
    return found


def _covered(keyword: str, skills_norm: List[str]) -> bool:
    """True si alguna skill del perfil cubre la keyword (multipalabra o juntos:
    'Soporte Tecnico' cubre 'soporte'; 'Help Desk' cubre 'helpdesk')."""
    kw = _norm(keyword)
    kw_ns = kw.replace(" ", "")
    for s in skills_norm:
        if kw == s or kw_ns == s.replace(" ", ""):
            return True
        if re.search(r"(?<![\w-])" + re.escape(kw) + r"(?![\w-])", s):
            return True
    return False


def profile_skill_set(profile: Dict) -> set:
    return {_norm(s) for s in profile.get("skills", []) if s}


def profile_title_texts(profile: Dict) -> List[str]:
    return [t for t in profile.get("titulos", []) if t]


def title_semantic_match(profile: Dict, job: Dict, engine) -> Optional[float]:
    """Max similitud coseno entre los titulos del perfil y el titulo de la vacante."""
    titles = profile_title_texts(profile)
    job_title = (job.get("title") or "").strip()
    if not titles or not job_title or engine is None:
        return None
    from core.search_engine import QUERY_PROMPT, DOC_PROMPT
    p_vecs = engine.model.encode(
        [QUERY_PROMPT.format(text=t) for t in titles], normalize_embeddings=True)
    j_vec = engine.model.encode([DOC_PROMPT.format(text=job_title)], normalize_embeddings=True)[0]
    return float((p_vecs @ j_vec).max())


def experience_score(profile: Dict, required: Optional[float]) -> Optional[float]:
    years = float(profile.get("experiencia_anios") or 0)
    if required is None:
        return None  # la vacante no exige anios: componente neutral
    if required <= 0:
        return 1.0
    return max(0.0, min(1.0, years / required))


def education_score(profile: Dict, required: Optional[str]) -> Optional[float]:
    if not required:
        return None
    have = _norm(profile.get("educacion_nivel") or "")
    have_order = _EDU_ORDER.get(have, 0)
    req_order = _EDU_ORDER.get(required, 0)
    if have_order >= req_order:
        return 1.0
    # proporcional a la distancia (ej. tecnologo pidiendo universitario -> 0.8)
    return max(0.0, have_order / req_order) if req_order else None


def languages_score(profile: Dict, required: List[str]) -> Optional[float]:
    if not required:
        return None
    have = {_norm(i if isinstance(i, str) else i.get("idioma", ""))
            for i in profile.get("idiomas", [])}
    have |= {_norm(s) for s in profile.get("skills", [])}
    ok = sum(1 for r in required if _norm(r) in have or _norm(r).rstrip("s") in have)
    return ok / len(required)


def ats_score(profile: Dict, job: Dict, engine=None,
              lexicon: Optional[List[str]] = None,
              weights: Optional[Dict] = None) -> Dict:
    """Score ATS 0-100 + desglose + keywords faltantes + veredicto."""
    from core.cv_analysis import SKILL_LEXICON
    lex = lexicon or SKILL_LEXICON
    w = dict(DEFAULT_WEIGHTS)
    if isinstance(profile.get("pesos_ats"), dict):
        w.update({k: float(v) for k, v in profile["pesos_ats"].items()})

    jk = job_keywords(job, lex)
    # skills ordenadas de la mas larga a la mas corta (mejor cobertura de frases)
    skills_norm = sorted(profile_skill_set(profile), key=len, reverse=True)
    missing = [k for k in jk if not _covered(k, skills_norm)]
    skills = (len(jk) - len(missing)) / len(jk) if jk else None

    comps = {
        "skills": skills,
        "titulo": title_semantic_match(profile, job, engine),
        "experiencia": experience_score(profile, extract_years_required(
            " ".join([job.get("title") or "", job.get("description") or "",
                      job.get("requirements") or ""]))),
        "educacion": education_score(profile, extract_education_required(
            (job.get("description") or "") + " " + (job.get("requirements") or ""))),
        "idiomas": languages_score(profile, extract_languages_required(
            (job.get("description") or "") + " " + (job.get("requirements") or ""))),
    }

    num = den = 0.0
    presentes = []
    for k, v in comps.items():
        if v is None:
            continue
        num += w.get(k, DEFAULT_WEIGHTS[k]) * v
        den += w.get(k, DEFAULT_WEIGHTS[k])
        presentes.append(k)
    score = (num / den * 100) if den else 0.0

    if score >= 85:
        verdict = "Fuerte"
    elif score >= 70:
        verdict = "Ajustable"
    elif score >= 40:
        verdict = "Debil"
    else:
        verdict = "Rechazo probable"

    return {
        "score": round(score, 1),
        "components": {k: (round(v * 100) if v is not None else None)
                       for k, v in comps.items()},
        "presentes": presentes,
        "missing": missing[:8],
        "requisitos": {
            "anios": extract_years_required(
                " ".join([job.get("title") or "", job.get("description") or "",
                          job.get("requirements") or ""])),
            "idiomas": extract_languages_required(
                (job.get("description") or "") + " " + (job.get("requirements") or "")),
            "educacion": extract_education_required(
                (job.get("description") or "") + " " + (job.get("requirements") or "")),
        },
        "verdict": verdict,
    }


def rank_with_ats(profile: Dict, results: List[Dict], engine,
                  sem_weight: float = 0.6, ats_weight: float = 0.4) -> List[Dict]:
    """Combina el score semantico con el score ATS y reordena.

    El componente ATS usa solo pesos que existan (aun si la vacante no pide
    anos/educacion/idiomas, los pesos se renormalizan dentro de ats_score).
    """
    out = []
    for r in results:
        a = ats_score(profile, r, engine=engine)
        combined = sem_weight * r.get("score", 0) * 100 + ats_weight * a["score"]
        row = dict(r)
        row["ats"] = a
        row["ats_score"] = a["score"]
        row["combined"] = round(combined, 1)
        out.append(row)
    out.sort(key=lambda x: x["combined"], reverse=True)
    return out
