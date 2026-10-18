"""Analizador de CV: extrae titulos, skills e idiomas de forma 100% local.

Pipeline:
  1. Extrae texto del CV (PDF con pypdf, DOCX con python-docx, TXT/MD directo).
  2. Detecta idiomas y nivel (regex sobre el texto).
  3. Skills: coincidencia literal contra un lexicon curado (ES+EN).
  4. Titulos: rankea vocabularios de puestos contra el encabezado del CV
     usando los embeddings del SemanticSearchEngine (sinonimos incluidos).
  5. Devuelve un dict listo para fusionar en data/profile.json.

Es el "contrato" esperado: cv -> dict -> ProfileWriter.
"""
import re
import unicodedata
from pathlib import Path
from typing import Dict, List, Optional

SKILL_LEXICON = [
    # --- Sistemas / productividad ---
    "Windows", "Linux", "macOS", "Office 365", "Word", "Excel", "PowerPoint",
    "Outlook", "Google Workspace", "SharePoint", "Teams", "Zoom",
    "OneDrive", "Google Drive",
    # --- Tecnico TI ---
    "Soporte Tecnico", "Mesa de Ayuda", "Help Desk", "Service Desk",
    "Active Directory", "Exchange", "VPN", "TCP/IP", "DNS", "DHCP",
    "Firewall", "Wireshark", "Impresoras", "Monitor de Impresion",
    "Hardware", "Instalacion de software", "Soporte Remoto",
    "ITIL", "SQL", "MySQL", "SQL Server", "PostgreSQL", "MongoDB",
    "Python", "JavaScript", "HTML", "CSS", "VBA", "Macros",
    "PowerShell", "Bash", "Git", "Docker", "AWS", "Azure",
    "Ciberseguridad", "Antivirus", "Copias de Seguridad", "Backup",
    "Redes", "Monitoreo",
    # --- Herramientas / negocio ---
    "Jira", "Zendesk", "Freshdesk", "HubSpot", "Salesforce", "Dynamics 365",
    "SAP", "Asana", "Trello", "Power BI", "Tableau",
    "Facturacion Electronica", "Facturacion", "Contabilidad", "Nomina",
    "Quickbooks", "Oracle", "Excel Avanzado", "Atencion al Cliente",
    "Ventas", "Logistica", "Inventario", "Bodega", "Conduccion",
    "Trabajo Remoto",
    # --- QA / Pruebas de software ---
    "QA", "Testing", "Pruebas de Software", "Pruebas Manuales",
    "Pruebas Automatizadas", "Pruebas de Regresion", "Casos de Prueba",
    "Bug Tracking", "Calidad de Software", "Gestion de Defectos",
    "Selenium", "Cypress", "Playwright", "Postman", "Pruebas de API",
    "ISTQB", "Cobertura de Pruebas", "Pruebas de Integracion",
]

# Cabeceras tipicas del encabezado de un CV.
_HEAD_PATTERNS = re.compile(
    r"^(perfil profesional|perfil|resumen ejecutivo|resumen|profesional profile|"
    r"objective|objetivo profesional|objetivo|acerca de mi|about me|about)\b",
    re.IGNORECASE,
)

_LANG_PATTERNS = [
    ("Ingles Avanzado", re.compile(r"ingl[eé]s[^.;\n]{0,40}?(avanzado|bilingü[eé]|fluido|c1|c2)", re.IGNORECASE)),
    ("Ingles Intermedio", re.compile(r"ingl[eé]s[^.;\n]{0,40}?(intermedio|conversacional|b1|b2)", re.IGNORECASE)),
    ("Ingles Basico", re.compile(r"ingl[eé]s[^.;\n]{0,40}?(básico|basico|a1|a2)", re.IGNORECASE)),
    ("Ingles", re.compile(r"\bingl[eé]s\b", re.IGNORECASE)),
    ("Portugues", re.compile(r"\bportugu[eé]s\b", re.IGNORECASE)),
    ("Frances", re.compile(r"\bfranc[eé]s\b", re.IGNORECASE)),
    ("Aleman", re.compile(r"\balem[aá]n\b", re.IGNORECASE)),
]


def extract_text(path: str) -> str:
    """Texto plano de un CV pdf / docx / txt / md."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"No existe el CV: {p}")
    suffix = p.suffix.lower()
    if suffix == ".pdf":
        from pypdf import PdfReader
        reader = PdfReader(str(p))
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    if suffix == ".docx":
        from docx import Document
        doc = Document(str(p))
        return "\n".join(par.text for par in doc.paragraphs)
    if suffix in (".txt", ".md"):
        return p.read_text(encoding="utf-8", errors="replace")
    raise ValueError(f"Formato no soportado: {suffix} (use pdf, docx, txt o md)")


def detect_languages(texto: str) -> List[str]:
    out = []
    for nombre, patron in _LANG_PATTERNS:
        if patron.search(texto) and nombre not in out:
            out.append(nombre)
    return out


# --------------------------------------------------------------- ATS: extras

CERT_LEXICON = [
    "ISTQB", "ITIL", "PMP", "Scrum Master", "CSM", "AWS Certified",
    "AZ-900", "AZ-104", "CCNA", "CompTIA A+", "CompTIA Security+",
    "Google Analytics", "Microsoft Certified", "Six Sigma", "Agile",
]

_EDU_PATTERNS = [
    ("maestria", re.compile(r"maestr[íi]a|master'?s?\s+degree|mba", re.IGNORECASE)),
    ("universitario", re.compile(
        r"universitario|licenciatura|ingenier[íi]a|bachelor|grado universitario", re.IGNORECASE)),
    ("tecnologo", re.compile(r"tecn[oó]logo", re.IGNORECASE)),
    ("tecnico", re.compile(r"t[eé]cnico\s+(?:medio|superior|en|de)|technical degree", re.IGNORECASE)),
    ("bachiller", re.compile(r"bachiller(?:ato)?", re.IGNORECASE)),
]


def detect_certifications(texto: str) -> List[str]:
    out = []
    for c in CERT_LEXICON:
        if re.search(r"(?<![\w-])" + re.escape(c) + r"(?![\w-])", texto, re.IGNORECASE):
            out.append(c)
    return out


def detect_education_level(texto: str) -> str:
    """Mayor nivel educativo mencionado (jerarquia ATS: maestria > universitario > ...)."""
    for nivel, patron in _EDU_PATTERNS:
        if patron.search(texto):
            return nivel
    return ""


def detect_experience_years(texto: str) -> float:
    """Anios de experiencia: sum de rangos de fechas YYYY-YYYY (o 'hasta actualidad')
    o la frase explicita 'N anios de experiencia' (la mayor)."""
    # frase explicita tiene prioridad
    explicitos = [float(n) for n in re.findall(
        r"(\d{1,2})\s*(?:\+\s*)?an(?:o|õ)s?\s+de\s+experiencia", texto, re.IGNORECASE)]
    if explicitos:
        return min(max(explicitos), 45.0)

    import datetime
    anio_actual = datetime.datetime.now().year
    total = 0.0
    # rangos: 2021 - 2023 | 2021 – actualidad | 2021 to present
    rangos = re.findall(
        r"((?:19|20)\d{2})\s*[-–—]\s*((?:19|20)\d{2}|actualidad|presente|present|actual|now)",
        texto, re.IGNORECASE)
    vistos = set()
    for ini, fin in rangos:
        inicio = int(ini)
        if re.search(r"actual|present|now", fin, re.IGNORECASE):
            fin_anio = anio_actual
        else:
            fin_anio = int(fin)
        if fin_anio < inicio:
            continue
        par = (inicio, fin_anio)
        if par in vistos:  # evita duplicar el mismo rol escrito dos veces
            continue
        vistos.add(par)
        total += fin_anio - inicio
    return float(min(total, 45.0))


def detect_contact(texto: str) -> Dict:
    contacto = {}
    email = re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", texto)
    if email:
        contacto["email"] = email.group(0).strip(".,;)")
    tel = re.search(r"(?:\+?\d{1,3}[\s-]?)?(?:\d{4}[\s-]?){2}\d{3,4}", texto)
    if tel:
        contacto["telefono"] = re.sub(r"\s+", " ", tel.group(0)).strip()
    li = re.search(r"linkedin\.com/in/[\w\-]+", texto, re.IGNORECASE)
    if li:
        contacto["linkedin"] = "https://" + li.group(0)
    return contacto



class CVAnalyzer:
    def __init__(self, engine=None):
        self._engine = engine  # SemanticSearchEngine opcional (lazy)

    @property
    def engine(self):
        if self._engine is None:
            from core.search_engine import SemanticSearchEngine
            self._engine = SemanticSearchEngine()
        return self._engine

    # -------------------------------------------------------------- titulos
    def _suggest_titles(self, texto: str) -> Dict[str, List[str]]:
        """Rankea vocabularios de puestos contra el encabezado del CV.

        Devuelve {"es": [...], "en": [...]} ordenados por cercania semantica.
        """
        from core.search_engine import SemanticSearchEngine

        VOCABULARY_ES = SemanticSearchEngine.VOCABULARY_ES
        VOCABULARY_EN = SemanticSearchEngine.VOCABULARY_EN

        # Encabezado: cabecera explicita o los primeros ~800 caracteres utiles.
        resumen = self._headline(texto)
        es = self.engine.rank_terms(resumen, VOCABULARY_ES, top_k=6)
        en = self.engine.rank_terms(resumen, VOCABULARY_EN, top_k=6)
        # Los titulos citados literalmente en el CV se priorizan al inicio.
        for lado, vocab in (("es", VOCABULARY_ES), ("en", VOCABULARY_EN)):
            lista = es if lado == "es" else en
            rest = [v for v in lista if v.lower() not in resumen.lower()]
            lit = [v for v in vocab if v.lower() in resumen.lower() and v not in lista]
            nuevo = list(dict.fromkeys(lit + lista + rest))
            # mantener orden: literales primero, luego semantico
            if lado == "es":
                es = nuevo[:6]
            else:
                en = nuevo[:6]
        return {"es": es, "en": en}

    def _headline(self, texto: str) -> str:
        # corta contactos y norma
        txt = re.sub(r"\S+@\S+", " ", texto)
        txt = re.sub(r"https?://\S+", " ", txt)
        txt = re.sub(r"\+\d[\d\s\-()]{7,}", " ", txt)  # telefonos
        txt = re.sub(r"\s+", " ", txt).strip()
        return txt[:1500]

    # ---------------------------------------------------------------- analyze
    def analyze(self, cv_texto: str) -> Dict:
        texto = re.sub(r"\s+", " ", cv_texto).strip()
        low = texto.lower()

        # --- skills por lexicon literal ---
        skills = []
        for s in SKILL_LEXICON:
            patron = re.compile(r"(?<![\w-])" + re.escape(s) + r"(?![\w-])", re.IGNORECASE)
            if patron.search(low):
                skills.append(s)

        # --- idiomas ---
        langs = detect_languages(texto)
        skills += [lang for lang in langs if lang not in skills]

        # --- campos ATS (reglas puras, ver core/ats.py) ---
        # busca niveles sin acentos: "Inglés avanzado" -> "ingles avanzado"
        def _deacc(s):
            return "".join(
                c for c in unicodedata.normalize("NFD", s)
                if unicodedata.category(c) != "Mn"
            ).lower()
        texto_d = _deacc(texto)
        idiomas_struct, vistos_idioma = [], set()
        for lang in langs:
            base = lang.split()[0]
            key = _deacc(base)
            if key in vistos_idioma:
                continue
            vistos_idioma.add(key)
            m = re.search(
                key + r"[^.;\n]{0,40}?"
                r"(avanzado|bilingue|fluido|intermedio|basico|c1|c2|b1|b2|a1|a2)",
                texto_d,
            )
            idiomas_struct.append({
                "idioma": base,
                "nivel": m.group(1).capitalize() if m else "no especificado",
            })

        datos_ats = {
            "experiencia_anios": detect_experience_years(texto),
            "educacion_nivel": detect_education_level(texto),
            "certificaciones": detect_certifications(texto),
            "idiomas": idiomas_struct,
            "contacto": detect_contact(texto),
        }

        # --- titulos semanticos ---
        titulos = self._suggest_titles(texto)
        es_titles, en_titles = titulos["es"], titulos["en"]

        # --- temas: pares ES/EN elegidos por similitud cruzada de embeddings
        # (no por mero orden, que emparejaba mal: "Ciberseguridad / Web Developer") ---
        temas = []
        pool_en = list(en_titles)
        for es_title in es_titles[:3]:
            pick = es_title
            if pool_en:
                cands = [t for t in pool_en if t != es_title] or pool_en
                ranked = self.engine.rank_terms(es_title, cands, top_k=1)
                if ranked:
                    pick = ranked[0]
                    pool_en = [t for t in pool_en if t != pick]
            temas.append({
                "puesto": es_title,
                "puesto_en": pick if pick else es_title,
                "ubicacion": "Costa Rica",
            })

        return {
            "titulos": es_titles + [t for t in en_titles if t not in es_titles][:2],
            "skills": skills,
            "temas": temas,
            **datos_ats,
        }


def merge_profile(actual: Dict, novedad: Dict, replace: bool = False) -> Dict:
    """Fusiona: titulos/skills se suman (sin duplicados); temas se reemplazan
    si el CV genero nuevos (reflejan al candidato). Con replace, todo se toma
    del CV salvo los pesos/filtros existentes.

    Campos ATS (experiencia/educacion/certificaciones/idiomas/contacto):
    el CV es la fuente de verdad -> si el nuevo trae datos, reemplazan."""
    nuevo = dict(actual)

    campos_ats = ("experiencia_anios", "educacion_nivel", "certificaciones",
                  "idiomas", "contacto")
    for c in campos_ats:
        v = novedad.get(c)
        if v:  # no vacia (si el CV no detecto anios, se conserva el valor actual)
            nuevo[c] = v

    if replace:
        nuevo["titulos"] = novedad.get("titulos", [])
        nuevo["skills"] = novedad.get("skills", [])
        if novedad.get("temas"):
            nuevo["temas"] = novedad["temas"]
        return nuevo

    def uniq(a, b):
        low = {t.lower() for t in a}
        return a + [t for t in b if t.lower() not in low]

    nuevo["titulos"] = uniq(actual.get("titulos", []), novedad.get("titulos", []))
    nuevo["skills"] = uniq(actual.get("skills", []), novedad.get("skills", []))
    if novedad.get("temas"):
        nuevo["temas"] = novedad["temas"]
    return nuevo
