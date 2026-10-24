# -*- coding: utf-8 -*-
"""jobpulse_v15.ats — Scoring tipo ATS (Applicant Tracking System).

Implementa evaluacion ponderada estilo ATS reales (Oracle AI Rating, Eightfold,
experimentos LiveCareer/ResumeWorded):

Componentes y pesos default (suman 1.0):
- Hard Skills / Keywords:       40%  (coincidencia de requisitos explicitos)
- Titulo del puesto:            30%  (similitud semantica via embeddings)
- Experiencia (anos vs min):    15%  (cobertura del minimo requerido)
- Educacion (nivel vs req):     7.5% (jerarquia: doctorado > master > bachelor > tecnico > bachiller)
- Idiomas (nivel explicito):    7.5% (coincidencia codigo + nivel >= requerido)

Umbrales de veredicto:
- >= 85  -> "Fuerte" (Strong match)
- 70-84  -> "Ajustable" (Good match, mejorar keywords)
- < 70   -> "Debil" / "Rechazo probable" (Weak match)

El score es 0-100. Componentes ausentes en la vacante se redistribuyen.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Tuple

from jobpulse_v15.search_engine import SemanticSearchEngine

logger = logging.getLogger("jobpulse.ats")


# ----------------------------------------------------------------------
# Pesos default (suman 1.0)
# ----------------------------------------------------------------------
DEFAULT_WEIGHTS = {
    "skills": 0.40,
    "title": 0.30,
    "experience": 0.15,
    "education": 0.075,
    "languages": 0.075,
}

# Veredicto thresholds
VEREDICTO_THRESHOLDS = {
    "fuerte": 85.0,
    "ajustable": 70.0,
    # <70 = debil
}

# Jerarquia educacion (mayor = mejor)
EDUCATION_HIERARCHY = {
    "doctorado": 6,
    "phd": 6,
    "doctorate": 6,
    "maestria": 5,
    "master": 5,
    "postgrado": 5,
    "posgrado": 5,
    "licenciatura": 4,
    "bachelor": 4,
    "grado": 4,
    "universitario": 4,
    "tecnologo": 3,
    "tecnico superior": 3,
    "tecnico": 2,
    "tecnico medio": 2,
    "bachillerato": 1,
    "bachiller": 1,
    "high school": 1,
    "secundaria": 0,
}

# Jerarquia idiomas (CEFR + coloquial)
LANGUAGE_HIERARCHY = {
    "nativo": 5,
    "native": 5,
    "c2": 5,
    "c1": 4,
    "avanzado": 4,
    "advanced": 4,
    "fluido": 4,
    "fluent": 4,
    "b2": 3,
    "intermedio alto": 3,
    "upper intermediate": 3,
    "b1": 2,
    "intermedio": 2,
    "intermediate": 2,
    "a2": 1,
    "basico": 1,
    "basic": 1,
    "a1": 1,
    "principiante": 1,
    "beginner": 1,
}


@dataclass
class ATSScore:
    """Resultado del scoring ATS."""
    total: float                    # 0-100
    veredicto: str                  # "fuerte" | "ajustable" | "debil"
    skills_pct: float               # 0-100
    title_pct: float                # 0-100
    experience_pct: float           # 0-100
    education_pct: float            # 0-100
    languages_pct: float            # 0-100
    missing_keywords: List[str]     # keywords de la vacante no cubiertas por el perfil
    details: Dict[str, Any]         # info extra por componente


# ----------------------------------------------------------------------
# Lexico de habilidades tecnicas (hard skills) - ES + EN
# ----------------------------------------------------------------------
SKILL_LEXICON: Set[str] = {
    # Lenguajes
    "python", "java", "javascript", "typescript", "c#", "csharp", "c++", "cpp",
    "go", "golang", "rust", "ruby", "php", "swift", "kotlin", "scala", "r",
    "matlab", "sql", "nosql", "bash", "powershell", "perl", "lua", "dart",
    # Frameworks web
    "react", "reactjs", "angular", "vue", "vuejs", "svelte", "nextjs", "next.js",
    "django", "flask", "fastapi", "spring", "spring boot", "express", "nestjs",
    "laravel", "symfony", "rails", "asp.net", ".net core", "blazor",
    # Cloud / DevOps
    "aws", "azure", "gcp", "google cloud", "docker", "kubernetes", "k8s",
    "terraform", "ansible", "jenkins", "gitlab ci", "github actions", "circleci",
    "argocd", "helm", "prometheus", "grafana", "elk", "datadog", "new relic",
    "ci/cd", "ci cd", "pipeline", "infrastructure as code", "iac",
    # Bases de datos
    "postgresql", "postgres", "mysql", "mariadb", "mongodb", "redis", "elasticsearch",
    "cassandra", "dynamodb", "oracle", "sql server", "sqlite", "neo4j", "firebase",
    "supabase", "planetscale",
    # Data / ML
    "pandas", "numpy", "scikit-learn", "sklearn", "tensorflow", "pytorch", "keras",
    "xgboost", "lightgbm", "catboost", "huggingface", "transformers", "langchain",
    "spark", "databricks", "snowflake", "bigquery", "redshift", "airflow", "kafka",
    # Testing / QA
    "pytest", "jest", "cypress", "playwright", "selenium", "junit", "testng",
    "postman", "newman", "k6", "locust", "sonarqube",
    # Otros
    "git", "github", "gitlab", "bitbucket", "jira", "confluence", "agile", "scrum",
    "kanban", "jira", "confluence", "linux", "windows", "macos", "unix",
    "api rest", "rest api", "graphql", "grpc", "websockets", "microservicios",
    "microservices", "event driven", "event-driven", "message queue", "rabbitmq",
    "sqoop", "hadoop", "hive", "pig", "tableau", "power bi", "looker", "metabase",
}


def _normalize_skill(s: str) -> str:
    """Normaliza skill para comparacion (lower, sin acentos, alfanumerico + espacios)."""
    import unicodedata
    s = unicodedata.normalize("NFD", s.lower())
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9\s\+\#\.]", " ", s).strip()


def _extract_skills_from_text(text: str) -> Set[str]:
    """Extrae skills del lexicon presentes en el texto (word boundary)."""
    if not text:
        return set()
    found = set()
    text_norm = _normalize_skill(text)
    for skill in SKILL_LEXICON:
        skill_norm = _normalize_skill(skill)
        # Word boundary para multi-palabra
        pattern = r"(^|\s)" + re.escape(skill_norm) + r"(\s|$|[.,;:])"
        if re.search(pattern, " " + text_norm + " "):
            found.add(skill)
    return found


# ----------------------------------------------------------------------
# Extraccion de requisitos de la vacante
# ----------------------------------------------------------------------
def job_keywords(job: Dict[str, Any]) -> Set[str]:
    """Hard skills de la oferta = coincidencias del lexicon en titulo+desc+reqs."""
    text = " ".join(filter(None, [
        job.get("title", ""),
        job.get("description_raw", ""),
        job.get("requirements_raw", ""),
    ]))
    return _extract_skills_from_text(text)


def _covered(profile_skills: Set[str], keyword: str) -> bool:
    """Cobertura multipalabra: 'Soporte Tecnico' cubre 'soporte'; 'Help Desk' cubre 'helpdesk'."""
    kw_norm = _normalize_skill(keyword)
    for ps in profile_skills:
        ps_norm = _normalize_skill(ps)
        if kw_norm in ps_norm or ps_norm in kw_norm:
            return True
    return False


def extract_years_required(job: Dict[str, Any]) -> Optional[int]:
    """Extrae anos de experiencia requeridos de la vacante (regex)."""
    text = " ".join(filter(None, [
        job.get("description_raw", ""),
        job.get("requirements_raw", ""),
    ]))
    if not text:
        return None
    # Patrones: "N anos", "N años", "N+ anos", "minimum N years", "N+ years"
    patterns = [
        r"(\d+)\s*\+\s*a[nn]os?",
        r"(\d+)\s*a[nn]os?",
        r"minimum\s+(\d+)\s*years?",
        r"at least\s+(\d+)\s*years?",
        r"(\d+)\s*\+\s*years?",
        r"(\d+)\s*years?\s*(experience|experiencia)",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                continue
    return None


def extract_education_required(job: Dict[str, Any]) -> Optional[str]:
    """Extrae nivel educativo requerido (normalizado a clave de EDUCATION_HIERARCHY)."""
    text = " ".join(filter(None, [
        job.get("description_raw", ""),
        job.get("requirements_raw", ""),
    ])).lower()
    if not text:
        return None
    # Buscar nivel mas alto mencionado
    found_level = None
    found_val = -1
    for level, val in EDUCATION_HIERARCHY.items():
        # Word boundary aproximado
        if re.search(rf"\b{re.escape(level)}\b", text):
            if val > found_val:
                found_val = val
                found_level = level
    return found_level


def extract_languages_required(job: Dict[str, Any]) -> Dict[str, str]:
    """Extrae idiomas requeridos con nivel: {'ingles': 'b2', 'frances': 'a2'}."""
    text = " ".join(filter(None, [
        job.get("description_raw", ""),
        job.get("requirements_raw", ""),
    ])).lower()
    if not text:
        return {}

    # Patrones: "ingles b2", "english c1", "nivel b2 ingles", "fluido en ingles"
    lang_pattern = re.compile(
        r"\b(ingles|english|espanol|spanish|frances|french|aleman|german|portugues|portuguese|italiano|italian|chino|chinese|japones|japanese)\b"
        r".{0,30}?"
        r"\b(nativo|native|c2|c1|avanzado|advanced|fluido|fluent|b2|intermedio alto|upper intermediate|b1|intermedio|intermediate|a2|basico|basic|a1|principiante|beginner)\b"
    )
    found = {}
    for m in lang_pattern.finditer(text):
        lang_raw = m.group(1)
        level_raw = m.group(2)
        # Normalizar idioma
        lang_map = {
            "ingles": "ingles", "english": "ingles",
            "espanol": "espanol", "spanish": "espanol",
            "frances": "frances", "french": "frances",
            "aleman": "aleman", "german": "aleman",
            "portugues": "portugues", "portuguese": "portugues",
            "italiano": "italiano", "italian": "italiano",
            "chino": "chino", "chinese": "chino",
            "japones": "japones", "japanese": "japones",
        }
        lang = lang_map.get(lang_raw, lang_raw)
        level = level_raw
        if lang not in found or LANGUAGE_HIERARCHY.get(level, 0) > LANGUAGE_HIERARCHY.get(found[lang], 0):
            found[lang] = level
    return found


def title_semantic_match(profile_titles: List[str], job_title: str, engine: SemanticSearchEngine) -> float:
    """Similitud coseno entre titulos del perfil y titulo de la vacante (0-1)."""
    if not profile_titles or not job_title:
        return 0.0
    # Embedding del titulo de la vacante
    job_emb = engine._model.encode([engine._get_doc_prompt() + job_title], convert_to_numpy=True, normalize_embeddings=True)[0]
    # Embeddings de titulos del perfil
    profile_embs = engine._model.encode([engine._get_query_prompt() + t for t in profile_titles], convert_to_numpy=True, normalize_embeddings=True)
    # Max similitud
    sims = profile_embs @ job_emb
    return float(np.max(sims))


def title_semantic_match_simple(profile_titles: List[str], job_title: str) -> float:
    """Fallback simple sin embeddings: overlap de tokens normalizados."""
    if not profile_titles or not job_title:
        return 0.0
    job_tokens = set(_normalize_skill(job_title).split())
    best = 0.0
    for pt in profile_titles:
        pt_tokens = set(_normalize_skill(pt).split())
        if not job_tokens or not pt_tokens:
            continue
        overlap = len(job_tokens & pt_tokens) / len(job_tokens | pt_tokens)
        best = max(best, overlap)
    return best


# ----------------------------------------------------------------------
# Scoring principal
# ----------------------------------------------------------------------
def ats_score(
    profile: Dict[str, Any],
    job: Dict[str, Any],
    engine: Optional[SemanticSearchEngine] = None,
    weights: Optional[Dict[str, float]] = None,
) -> ATSScore:
    """
    Calcula score ATS 0-100 con desglose por componente.

    profile: dict con claves opcionales:
        - skills: List[str]           (hard skills del candidato)
        - titulos: List[str]          (titulos objetivo / historicos)
        - experiencia_anios: int      (anos de experiencia total)
        - educacion_nivel: str        (clave de EDUCATION_HIERARCHY)
        - idiomas: Dict[str, str]     # {'ingles': 'c1', 'frances': 'b2'}
        - pesos_ats: Dict[str, float] # override de DEFAULT_WEIGHTS

    job: dict normalizado de la vacante (title, description_raw, requirements_raw, etc.)

    engine: SemanticSearchEngine para similitud de titulo (opcional; si None usa fallback simple)
    weights: override de pesos (skills, title, experience, education, languages)
    """
    w = weights or profile.get("pesos_ats", DEFAULT_WEIGHTS).copy()
    # Normalizar pesos de componentes presentes
    active = {k: v for k, v in w.items() if v > 0}
    total_w = sum(active.values()) or 1.0
    w = {k: v / total_w for k, v in active.items()}

    # 1. SKILLS (40%)
    job_kws = job_keywords(job)
    profile_skills = set(profile.get("skills", []))
    if job_kws:
        covered = sum(1 for kw in job_kws if _covered(profile_skills, kw))
        skills_pct = 100.0 * covered / len(job_kws)
        missing = [kw for kw in job_kws if not _covered(profile_skills, kw)]
    else:
        skills_pct = 100.0  # sin requisitos tecnicos = full
        missing = []

    # 2. TITLE (30%)
    profile_titles = profile.get("titulos", [])
    job_title = job.get("title", "")
    if engine and profile_titles and job_title:
        title_pct = 100.0 * title_semantic_match(profile_titles, job_title, engine)
    else:
        title_pct = 100.0 * title_semantic_match_simple(profile_titles, job_title)

    # 3. EXPERIENCE (15%)
    req_years = extract_years_required(job)
    cand_years = profile.get("experiencia_anios")
    if req_years is not None and cand_years is not None:
        if cand_years >= req_years:
            experience_pct = 100.0
        else:
            experience_pct = max(0.0, 100.0 * cand_years / req_years)
    elif req_years is None:
        experience_pct = 100.0  # no especifica requisito
    else:
        experience_pct = 50.0   # vacante pide exp pero perfil no la tiene

    # 4. EDUCATION (7.5%)
    req_edu = extract_education_required(job)
    cand_edu = profile.get("educacion_nivel")
    if req_edu and cand_edu:
        req_val = EDUCATION_HIERARCHY.get(req_edu, 0)
        cand_val = EDUCATION_HIERARCHY.get(cand_edu, 0)
        if cand_val >= req_val:
            education_pct = 100.0
        else:
            education_pct = max(0.0, 100.0 * cand_val / max(req_val, 1))
    elif req_edu is None:
        education_pct = 100.0
    else:
        education_pct = 0.0

    # 5. LANGUAGES (7.5%)
    req_langs = extract_languages_required(job)
    cand_langs = profile.get("idiomas", {})
    if req_langs:
        lang_scores = []
        for lang, req_level in req_langs.items():
            cand_level = cand_langs.get(lang, "")
            req_val = LANGUAGE_HIERARCHY.get(req_level, 0)
            cand_val = LANGUAGE_HIERARCHY.get(cand_level, 0)
            if cand_val >= req_val:
                lang_scores.append(100.0)
            else:
                lang_scores.append(max(0.0, 100.0 * cand_val / max(req_val, 1)))
        languages_pct = sum(lang_scores) / len(lang_scores) if lang_scores else 100.0
    else:
        languages_pct = 100.0

    # Total ponderado
    total = (
        w.get("skills", 0) * skills_pct +
        w.get("title", 0) * title_pct +
        w.get("experience", 0) * experience_pct +
        w.get("education", 0) * education_pct +
        w.get("languages", 0) * languages_pct
    )

    # Veredicto
    if total >= VEREDICTO_THRESHOLDS["fuerte"]:
        veredicto = "fuerte"
    elif total >= VEREDICTO_THRESHOLDS["ajustable"]:
        veredicto = "ajustable"
    else:
        veredicto = "debil"

    return ATSScore(
        total=round(total, 1),
        veredicto=veredicto,
        skills_pct=round(skills_pct, 1),
        title_pct=round(title_pct, 1),
        experience_pct=round(experience_pct, 1),
        education_pct=round(education_pct, 1),
        languages_pct=round(languages_pct, 1),
        missing_keywords=sorted(missing),
        details={
            "job_keywords": sorted(job_kws),
            "profile_skills": sorted(profile_skills),
            "req_years": req_years,
            "cand_years": cand_years,
            "req_education": req_edu,
            "cand_education": cand_edu,
            "req_languages": req_langs,
            "cand_languages": cand_langs,
            "weights_used": w,
        }
    )


def rank_with_ats(
    jobs: List[Dict],
    profile: Dict[str, Any],
    engine: Optional[SemanticSearchEngine] = None,
    weights: Optional[Dict[str, float]] = None,
    semantic_weight: float = 0.6,
) -> List[Dict]:
    """
    Reordena jobs combinando score semantico (0-1) + ATS (0-100).

    Combinado = semantic_weight * (semantic_score * 100) + (1 - semantic_weight) * ats_score
    """
    for job in jobs:
        ats = ats_score(profile, job, engine, weights)
        job["ats_score"] = ats.total
        job["ats_veredicto"] = ats.veredicto
        job["ats_missing"] = ats.missing_keywords
        job["ats_skills_pct"] = ats.skills_pct
        job["ats_title_pct"] = ats.title_pct

    # Asumimos que job ya tiene 'semantic_score' (0-1) del search_engine
    for job in jobs:
        sem = job.get("semantic_score", 0.0) or 0.0
        ats = job.get("ats_score", 0.0) or 0.0
        job["combined_score"] = round(semantic_weight * (sem * 100) + (1 - semantic_weight) * ats, 1)

    jobs.sort(key=lambda j: -j.get("combined_score", 0))
    return jobs