# -*- coding: utf-8 -*-
"""
jobpulse_v15.cv_analysis — Extracción de perfil a partir de CV (PDF/DOCX).

Implementa Harvard/ATS-style profile extraction from CV documents:
- PDF text extraction via pdfplumber (multi-page, tables, headers/footers)
- DOCX text extraction via python-docx
- Post-procesing: normalización, detección de secciones, extracción de habilidades,
  educación, experiencia y idiomas
- Output: profile dict compatible con el usado en gate.py y ats.py
"""

from __future__ import annotations

import re
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("jobpulse.cv")

# Rutas
RAIZ = Path(__file__).resolve().parent.parent.parent

# ----------------------------------------------------------------------
# Expresiones regulares de ayuda
# ----------------------------------------------------------------------

# Patrones para detectar secciones en el CV
SECCION_PATTERNS = {
    "experiencia": re.compile(
        r"(?i)(experiencia laboral|experience|historial laboral|employment|work.*experience|experiencia\s+profesional)"
    ),
    "educacion": re.compile(
        r"(?i)(educación|education| formación|formation|estudios|academic|degree|certification)"
    ),
    "habilidades": re.compile(
        r"(?i)(habilidades|skills| competencias|technologies|tools|lenguajes|languages)"
    ),
    "idiomas": re.compile(
        r"(?i)(idiomas|languages|languages skills|idiomas conocidos|language)"
    ),
    "proyectos": re.compile(
        r"(?i)(proyectos|projects|personal projects)"
    ),
}

# Patrones para extraer años de experiencia
ANIO_EXP_PATTERNS = [
    r"(?i)(\d+)\s*años?\s*(de\s*)?experiencia",
    r"(?i)(\d+)\s*años?\s*trabajando",
    r"(?i)(\d+)\s*años?\s*en\s*el\s*área",
    r"(?i)(\d+)\s*-\s*(\d+)\s*años?\s*de\s*experiencia",
]

# Patrones para títulos de grado
TITULO_GRADO_PATTERNS = [
    r"(?i)(maestria|master|m\.?s\.?)",
    r"(?i)(doctorado|phd|doctorate)",
    r"(?i)(licenciatura|bachelor|b\.?a\.?|b\.?s\.?)",
    r"(?i)(tecnologo|technologist)",
    r"(?i)(tecnico|associate|aa|as)",
]

# Patrones para idiomas CEFR
PATRON_NIVEL_IDIOMA = re.compile(
    r"(?i)(\b(?:nativo|native|c2|c1|avanzado|advanced|fluido|fluent|b2|intermedio|upper intermediate|b1|a2|basico|beginner|a1)\b)"
)

# Palabras clave de habilidades tecnicas (mismo lexicon que ats.py)
SKILL_LEXICON = {
    "python", "java", "javascript", "typescript", "c#", "csharp", "c++", "cpp",
    "go", "golang", "rust", "ruby", "php", "swift", "kotlin", "scala", "r",
    "matlab", "sql", "nosql", "bash", "powershell", "perl", "lua", "dart",
    "react", "reactjs", "angular", "vue", "vuejs", "svelte", "nextjs", "next.js",
    "django", "flask", "fastapi", "spring", "spring boot", "express", "nestjs",
    "laravel", "symfony", "rails", "asp.net", ".net core", "blazor",
    "aws", "azure", "gcp", "google cloud", "docker", "kubernetes", "k8s",
    "terraform", "ansible", "jenkins", "gitlab ci", "github actions", "circleci",
    "argocd", "helm", "prometheus", "grafana", "elk", "datadog", "new relic",
    "ci/cd", "ci cd", "pipeline", "infrastructure as code", "iac",
    "postgresql", "postgres", "mysql", "mariadb", "mongodb", "redis", "elasticsearch",
    "cassandra", "dynamodb", "oracle", "sql server", "sqlite", "neo4j", "firebase",
    "supabase", "planetscale",
    "pandas", "numpy", "scikit-learn", "sklearn", "tensorflow", "pytorch", "keras",
    "xgboost", "lightgbm", "catboost", "huggingface", "transformers", "langchain",
    "spark", "databricks", "snowflake", "bigquery", "redshift", "airflow", "kafka",
    "pytest", "jest", "cypress", "playwright", "selenium", "junit", "testng",
    "postman", "newman", "k6", "locust", "sonarqube",
    "git", "github", "gitlab", "bitbucket", "jira", "confluence", "agile", "scrum", "kanban",
}


# ----------------------------------------------------------------------
# Normalización y utilidades
# ----------------------------------------------------------------------

def _normalize_skill(s: str) -> str:
    """Normaliza skill para comparacion (lower, sin acentos, alfanumerico + espacios)."""
    import unicodedata
    s = unicodedata.normalize("NFD", s.lower())
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9\s+#.]", " ", s).strip()


def _extract_skills_from_text(text: str) -> Set[str]:
    """Extrae skills del lexicon presentes en el texto (word boundary)."""
    if not text:
        return set()
    found = set()
    text_norm = _normalize_skill(text)
    for skill in SKILL_LEXICON:
        skill_norm = _normalize_skill(skill)
        # Word boundary: skill debe aparecer como palabra completa
        # Usamos grupos de captura explícitos en lugar de \\s para evitar problemas de escaping
        pattern = "(^|[^a-z0-9])" + re.escape(skill_norm) + "([^a-z0-9]|$)"
        if re.search(pattern, text_norm, flags=re.IGNORECASE):
            found.add(skill)
    return found


# ----------------------------------------------------------------------
# Extractor de PDF
# ----------------------------------------------------------------------


def _extract_text_pdf(filepath: Path) -> str:
    """Extrae todo el texto de un archivo PDF usando pdfplumber."""
    try:
        import pdfplumber
    except ImportError:
        logger.warning("pdfplumber no disponible; PDF no procesable")
        return ""

    full_text = []
    try:
        with pdfplumber.open(filepath) as pdf:
            for i, page in enumerate(pdf.pages):
                try:
                    text = page.extract_text()
                    if text:
                        full_text.append(text)
                except Exception as e:
                    logger.debug(f"No se pudo extraer texto de la página {i}: {e}")
    except Exception as e:
        logger.error(f"Error abriendo PDF {filepath}: {e}")
        return ""

    return "\n".join(full_text)


# ----------------------------------------------------------------------
# Extractor de DOCX
# ----------------------------------------------------------------------


def _extract_text_docx(filepath: Path) -> str:
    """Extrae todo el texto de un archivo DOCX usando python-docx."""
    try:
        from docx import Document
    except ImportError:
        logger.warning("python-docx no disponible; DOCX no procesable")
        return ""

    try:
        doc = Document(filepath)
        full_text = []
        for para in doc.paragraphs:
            if para.text:
                full_text.append(para.text)
        # También extraer texto de tablas
        for table in doc.tables:
            for row in table.rows:
                row_text = " ".join(cell.text for cell in row.cells if cell.text)
                if row_text:
                    full_text.append(row_text)
        return "\n".join(full_text)
    except Exception as e:
        logger.error(f"Error abriendo DOCX {filepath}: {e}")
        return ""


# ----------------------------------------------------------------------
# Limpieza y post-procesado
# ----------------------------------------------------------------------


def _limpiar_texto(texto: str) -> str:
    """Limpia el texto extraído: remueve líneas vacías excesivas, normaliza espacios."""
    # Reemplazar múltiples espacios/newlines por un solo espacio
    texto = re.sub(r"\n\s*\n", "\n", texto)
    texto = re.sub(r" +", " ", texto)
    return texto.strip()


def _dividir_en_lineas(texto: str) -> List[str]:
    """Divide el texto en líneas simples."""
    return [l.strip() for l in texto.split("\n") if l.strip()]


# ----------------------------------------------------------------------
# Extracción de secciones del CV
# ----------------------------------------------------------------------


def _detectar_secciones(lineas: List[str]) -> Dict[str, List[str]]:
    """Intenta detectar y separar secciones del CV (experiencia, educación, habilidades, etc.)."""
    secciones = {
        "experiencia": [],
        "educacion": [],
        "habilidades": [],
        "idiomas": [],
        "proyectos": [],
        "otros": [],
    }

    seccion_actual = "otros"
    for linea in lineas:
        baja = linea.lower()
        asignado = False

        for nombre, patron in SECCION_PATTERNS.items():
            if patron.search(linea):
                seccion_actual = nombre
                asignado = True
                break

        if not asignado:
            secciones[seccion_actual].append(linea)

    # Filtrar líneas muy cortas o ruido en "otros"
    min_len = 3
    secciones["otros"] = [
        l for l in secciones["otros"] if len(l) >= min_len and not re.match(r"^(cv|curriculum|curriculum vitae)$", l.lower())
    ]

    return secciones


# ----------------------------------------------------------------------
# Extracción de datos específicos
# ----------------------------------------------------------------------


def _extraer_anos_experiencia(texto: str) -> Optional[int]:
    """Intenta extraer años de experiencia del texto."""
    for patron in ANIO_EXP_PATTERNS:
        m = re.search(patron, texto)
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                continue
    # Búsqueda simple: "X años" o "X years"
    m = re.search(r"(?i)(\d+)\s*años?", texto)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            pass
    return None


def _extraer_educacion(nombre_archivo: str, texto: str) -> Optional[str]:
    """Extrae el nivel educativo y lo normaliza a clave de EDUCATION_HIERARCHY."""
    combinado = f"{nombre_archivo} {texto}".lower()

    for patron in TITULO_GRADO_PATTERNS:
        if re.search(patron, combinado):
            # Devolver la clave canonical
            for clave in [
                "maestria", "master", "doctorado", "phd", "doctorate",
                "licenciatura", "bachillerato", "bachiller", "tecnologo",
                "tecnico", "universitario", "grado",
            ]:
                if re.search(clave, combinado):
                    return clave
    return None


def _extraer_idiomas(texto: str) -> Dict[str, str]:
    """Extrae idiomas y niveles CEFR del texto."""
    encontrados: Dict[str, str] = {}

    for m in PATRON_NIVEL_IDIOMA.finditer(texto):
        level = m.group(1).lower()
        # Buscar el idioma cercano en el texto (palabra anterior o siguiente)
        start = max(0, m.start() - 30)
        end = min(len(texto), m.end() + 30)
        alrededor = texto[start:end]

        # Buscar nombres de idioma
        for lang_variants, lang_normalized in {
            "ingles": "ingles",
            "english": "ingles",
            "espanol": "espanol",
            "spanish": "espanol",
            "frances": "frances",
            "french": "frances",
            "aleman": "aleman",
            "german": "aleman",
            "portugues": "portugues",
            "portuguese": "portugues",
            "italiano": "italiano",
            "italian": "italiano",
            "chino": "chino",
            "chinese": "chino",
            "japones": "japones",
            "japanese": "japones",
        }.items():
            # Buscar la variante en el alrededor
            if re.search(rf"\b{re.escape(lang_variants)}\b", alrededor.lower()):
                if lang_normalized not in encontrados or LANGUAGE_HIERARCHY.get(level, 0) > LANGUAGE_HIERARCHY.get(encontrados[lang_normalized], 0):
                    encontrados[lang_normalized] = level

    return encontrados


# ----------------------------------------------------------------------
# Extracción de skills del CV
# ----------------------------------------------------------------------


def _extraer_habilidades(seccion_habilidades: List[str]) -> List[str]:
    """Extrae skills únicos de la sección de habilidades detectada."""
    habilidades_unicas: Set[str] = set()
    for linea in seccion_habilidades:
        # Buscar skills del lexicon en la línea
        found = _extract_skills_from_text(linea)
        habilidades_unicas.update(found)

    # Si no se encontraron skills del lexicon, hacer un fallback: tokenizar y filtrar
    if not habilidades_unicas:
        for linea in seccion_habilidades:
            tokens = re.findall(r"[a-zA-ZáéíóúÁÉÍÓÚñÑ]+", linea.lower())
            # Filtrar tokens muy comunes
            stop_words = {"el", "la", "de", "en", "y", "a", "por", "con", "para", "es", "son", "sus", "un", "una", "las", "los"}
            tokens = [t for t in tokens if t not in stop_words and len(t) >= 3]
            habilidades_unicas.update(tokens)

    return sorted(habilidades_unicas)


# ----------------------------------------------------------------------
# Motor principal: CV → profile dict
# ----------------------------------------------------------------------


def cv_a_perfil(filepath: Path, nombre: Optional[str] = None) -> Dict[str, Any]:
    """
    Convierte un CV (PDF o DOCX) en un profile dict compatible con gate.py/ats.py.

    Estructura de salida (igual que profile en gate/ats):
    {
        "skills": List[str],           # hard skills detectadas
        "titulos": List[str],          # títulos/grados detectados
        "experiencia_anios": Optional[int],  # años de experiencia (puede ser None)
        "educacion_nivel": Optional[str],    # clave EDUCATION_HIERARCHY o None
        "idiomas": Dict[str, str],       # {'ingles': 'c1', 'frances': 'b2'}
        "pesos_ats": Dict[str, float],     # pesos por defecto
    }

    Args:
        filepath: Ruta al archivo PDF o DOCX.
        nombre: Nombre opcional del candidato. Si None, se deriva del archivo.

    Returns:
        Dict perfil listo para usar en gate/ats.
    """
    if not filepath.exists():
        logger.error(f"Archivo no encontrado: {filepath}")
        return {
            "skills": [],
            "titulos": [],
            "experiencia_anios": None,
            "educacion_nivel": None,
            "idiomas": {},
            "pesos_ats": DEFAULT_WEIGHTS.copy(),
        }

    # Extraer texto
    texto = ""
    if filepath.suffix.lower() == ".pdf":
        texto = _extract_text_pdf(filepath)
    elif filepath.suffix.lower() == ".docx":
        texto = _extract_text_docx(filepath)
    else:
        logger.warning(f"Formato no soportado: {filepath.suffix}; retornando perfil vacío")
        return {
            "skills": [],
            "titulos": [],
            "experiencia_anios": None,
            "educacion_nivel": None,
            "idiomas": {},
            "pesos_ats": DEFAULT_WEIGHTS.copy(),
        }

    texto = _limpiar_texto(texto)
    lineas = _dividir_en_lineas(texto)

    if not texto.strip():
        logger.warning(f"CV vacío después de extracción: {filepath}")
        return {
            "skills": [],
            "titulos": [],
            "experiencia_anios": None,
            "educacion_nivel": None,
            "idiomas": {},
            "pesos_ats": DEFAULT_WEIGHTS.copy(),
        }

    # Detectar secciones
    secciones = _detectar_secciones(lineas)

    # 1. Habilidades
    habilidades = _extraer_habilidades(secciones.get("habilidades", []))
    # También buscar skills en experiencia y otros textos
    for sec_nombre in ["experiencia", "educacion", "proyectos"]:
        habilidades = list(set(habilidades + _extraer_habilidades(secciones.get(sec_nombre, []))))

    # 2. Títulos / grados
    educacion_raw = _extraer_educacion(filepath.name, texto)
    # Si no se detecta en el texto, buscar en las líneas
    titulos_detectados: List[str] = []
    if educacion_raw:
        titulos_detectados = [educacion_raw]
    else:
        # Buscar patrones de título en las líneas
        for linea in lineas:
            for patron in TITULO_GRADO_PATTERNS:
                if re.search(patron, linea):
                    titulos_detectados.append(linea.strip())
                    break
        # Si aún no tenemos, usar la educación extraída aunque no tuviera clave
        if not titulos_detectados and educacion_raw:
            titulos_detectados = [educacion_raw]

    # 3. Años de experiencia
    anos_exp = _extraer_anos_experiencia(texto)

    # 4. Idiomas
    idiomas = _extraer_idiomas(texto)

    # Asegurar que todos los idiomas detectados tengan nivel conocido
    for lang in list(idiomas.keys()):
        if idiomas[lang] not in LANGUAGE_HIERARCHY:
            # Asignar default básico
            idiomas[lang] = "b2"

    # 5. Peso ATS default
    pesos_ats = DEFAULT_WEIGHTS.copy()

    perfil = {
        "skills": habilidades[:100],  # límite razonable
        "titulos": titulos_detectados[:20],  # límite razonable
        "experiencia_anios":anos_exp,
        "educacion_nivel": educacion_raw,
        "idiomas": idiomas,
        "pesos_ats": pesos_ats,
    }

    logger.info(
        f"Perfil extraído de {filepath.name}: skills={len(habilidades)}, "
        f"titulos={len(titulos_detectados)}, exp={anos_exp}, "
        f"educacion={educacion_raw}, idiomas={idiomas}"
    )

    return perfil


# Helpers adicionales


def _alrededor(texto: str, termino: str) -> str:
    """Retorna contexto alrededor de un término en el texto."""
    idx = texto.lower().find(termino.lower())
    if idx == -1:
        return ""
    start = max(0, idx - 20)
    end = min(len(texto), idx + 40)
    return texto[start:end]


# Exportar pesos por defecto para uso externo
DEFAULT_WEIGHTS = {
    "skills": 0.40,
    "title": 0.30,
    "experience": 0.15,
    "education": 0.075,
    "languages": 0.075,
}

# Exportar EDUCATION_HIERARCHY y LANGUAGE_HIERARCHY para uso en otros módulos
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