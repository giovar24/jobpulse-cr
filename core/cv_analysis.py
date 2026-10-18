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
    # --- Desarrollo de software ---
    "Java", "TypeScript", "Node.js", "React", "Angular", "Vue", "PHP",
    "C#", ".NET", "Spring Boot", "Django", "Laravel", "Redis", "GraphQL",
    "RESTful", "Microservicios", "Scrum", "Metodologias Agiles",
    "CI/CD", "GitLab", "GitHub", "Jenkins", "Kubernetes", "Terraform",
]

# Cabeceras tipicas del encabezado de un CV.
_HEAD_PATTERNS = re.compile(
    r"^(perfil profesional|perfil|resumen ejecutivo|resumen|profesional profile|"
    r"objective|objetivo profesional|objetivo|acerca de mi|about me|about)\b",
    re.IGNORECASE,
)

# Idiomas: reglas bilingues ES|EN (el CV puede venir en cualquiera de los dos).
# (idioma, patron del nombre, [(nivel, patron)] de mayor a menor)
_LANG_RULES = [
    ("Ingles", r"ingl[eé]s|english", [
        ("Nativo", r"nativo|native"),
        ("Avanzado", r"avanzado|fluido|fluent|professional|profesional|advanced|biling[uü]e|c1|c2"),
        ("Intermedio", r"intermedio|intermediate|conversacional|conversational|b1|b2"),
        ("Basico", r"basico|b[aá]sico|basic|elementary|a1|a2"),
    ]),
    ("Espanol", r"espa[nñ]ol|spanish", [
        ("Nativo", r"nativo|native|c2"),
        ("Avanzado", r"avanzado|fluido|fluent|professional|profesional|advanced|c1"),
        ("Intermedio", r"intermedio|intermediate|b1|b2"),
        ("Basico", r"basico|b[aá]sico|basic|a1|a2"),
    ]),
    ("Portugues", r"portugu[eé]s|portuguese", []),
    ("Frances", r"franc[eé]s|french", []),
    ("Aleman", r"alem[aá]n|german", []),
]


def detect_languages_struct(texto: str) -> List[Dict]:
    """[{"idioma": "Ingles", "nivel": "Avanzado"|"no especificado"}, ...]
    Busca el nombre del idioma en ES o EN y el nivel en ES o EN.

    El segmento de nivel se recorta en el proximo nombre de OTRO idioma:
    sin eso, "Inglés: Profesional Avanzado C1 Español: Nativo C2" (lineas
    pegadas por el PDF) hacia que la ventana de Ingles vea el "Nativo"
    del Espanol y reporte el nivel equivocado."""
    out, vistos = [], set()
    for idioma, pat_nombre, niveles in _LANG_RULES:
        if idioma in vistos:
            continue
        m = re.search(r"(?<![\w])(" + pat_nombre + r")(?![\w])", texto, re.IGNORECASE)
        if not m:
            continue
        vistos.add(idioma)
        seg = texto[m.end(): m.end() + 60]
        fin = len(seg)
        for otro_idioma, pat_otro, _ in _LANG_RULES:
            if otro_idioma == idioma:
                continue
            mo = re.search(pat_otro, seg, re.IGNORECASE)
            if mo and mo.start() < fin:
                fin = mo.start()
        seg = seg[:fin]
        nivel = "no especificado"
        for nombre_nivel, pat_nivel in niveles:
            if re.search(pat_nivel, seg, re.IGNORECASE):
                nivel = nombre_nivel
                break
        out.append({"idioma": idioma, "nivel": nivel})
    return out


def detect_languages(texto: str) -> List[str]:
    """Etiquetas para skills: "Ingles Avanzado", "Espanol Nativo", "Portugues"..."""
    return [
        d["idioma"] if d["nivel"] == "no especificado"
        else f"{d['idioma']} {d['nivel']}"
        for d in detect_languages_struct(texto)
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
        txt = "\n".join((page.extract_text() or "") for page in reader.pages)
        # ligaduras tipograficas del PDF: "ﬁcticio" -> "ficticio" (si no,
        # ninguna palabra que contenga fi/fl/ff matchea en el lexicon)
        txt = txt.translate(str.maketrans({
            "ﬁ": "fi", "ﬂ": "fl", "ﬀ": "ff", "ﬃ": "ffi", "ﬄ": "ffl",
        }))
        # palabras partidas a mitad de linea: "usuario-\nficticio" ->
        # "usuario-ficticio" (recupera LinkedIn/URLs rotas por el salto)
        return re.sub(r"(\w)-[ \t]*\n[ \t]*(\w)", r"\1-\2", txt)
    if suffix == ".docx":
        from docx import Document
        doc = Document(str(p))
        return "\n".join(par.text for par in doc.paragraphs)
    if suffix in (".txt", ".md"):
        return p.read_text(encoding="utf-8", errors="replace")
    raise ValueError(f"Formato no soportado: {suffix} (use pdf, docx, txt o md)")


# --------------------------------------------------------------- ATS: extras

def deacc(text: str) -> str:
    """Minusculas sin acentos: 'Instalación' == 'Instalacion' == 'INSTALACION'."""
    return "".join(
        c for c in unicodedata.normalize("NFD", text or "")
        if unicodedata.category(c) != "Mn"
    ).lower()


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
    """Anios de experiencia (ES|EN): frase explicita 'N anios de experiencia' /
    'N years of experience' (la mayor), o suma de rangos YYYY-YYYY
    ('hasta actualidad' / 'present')."""
    # frase explicita tiene prioridad (ES y EN)
    explicitos = [float(n) for n in re.findall(
        r"(\d{1,2})\s*(?:\+\s*)?(?:años|anos|years?)\s*"
        r"(?:de\s+|of\s+)?(?:experiencia|experience)?", texto, re.IGNORECASE)]
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
    """Email, telefono (formato CR +506 / 8000-0000), LinkedIn, GitHub y ciudad."""
    contacto = {}
    email = re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", texto)
    if email:
        contacto["email"] = email.group(0).strip(".,;)")
    # telefono: preferencia por etiqueta (evita confundir fechas 2018-2021)
    tel = re.search(
        r"(?:tel[eé]fono|celular|m[óo]vil|phone)\D{0,10}"
        r"((?:\+\d{1,3}[\s-]?)?\d{4}[\s-]\d{4}(?:[\s-]\d{3,4})?)",
        texto, re.IGNORECASE) or re.search(
        r"\+\d{1,3}[\s-]?\d{4}[\s-]\d{4}", texto)
    if tel:
        contacto["telefono"] = re.sub(r"\s+", " ", tel.group(1)).strip()
    li = re.search(r"linkedin\.com/in/[\w\-]+", texto, re.IGNORECASE)
    if li:
        contacto["linkedin"] = "https://" + li.group(0)
    gh = re.search(r"github\.com/[\w\-\.]+", texto, re.IGNORECASE)
    if gh:
        contacto["github"] = "https://" + gh.group(0)
    ub = re.search(
        r"(?:ubicaci[óo]n|location)\s*:\s*(.+?)(?=\s+(?:linkedin|github|email|tel|phone|id)\b|\s\||$)",
        texto, re.IGNORECASE)
    if ub:
        contacto["ciudad"] = re.sub(r"\s+", " ", ub.group(1)).strip().rstrip("|,")
    return contacto


def detect_seniority(texto: str) -> str:
    """Nivel detectado en el encabezado del CV (titular del puesto)."""
    head = deacc(texto[:300])
    if re.search(r"\b(senior|sr\.?|principal|lead|lider|arquitecto|staff)\b", head):
        return "senior"
    if re.search(r"\b(junior|jr\.?|trainee|becario|asistente)\b", head):
        return "junior"
    if re.search(r"\b(mid|intermedio|pleno|especialista)\b", head):
        return "mid"
    return ""


def _split_phrases(texto: str, max_len: int = 180) -> List[str]:
    """Trocea el CV en fragmentos cortos tipo frase (para evidencia de revision).

    Corta por salto de linea y puntuacion; los fragmentos largos se parten en
    ventanas de max_len por frontera de palabra. Devuelve lista sin vacios.
    """
    base = re.split(r"[\n.;:!?]+|\s+\|\s+", texto)
    frases = []
    for b in base:
        b = re.sub(r"\s+", " ", b).strip()
        if len(b) < 12:      # restos muy cortos no aportan contexto
            continue
        while len(b) > max_len:
            corte = b.rfind(" ", 0, max_len)
            if corte < 40:
                corte = max_len
            frases.append(b[:corte].strip())
            b = b[corte:].strip()
        if b:
            frases.append(b)
    # dedupe conservando orden
    out, vistos = [], set()
    for f in frases:
        if f.lower() not in vistos:
            vistos.add(f.lower())
            out.append(f)
    return out



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

    # ------------------------------------------------------------ revision IA
    def review(self, propuesto: Dict, texto: str) -> Dict:
        """Revision de coherencia semantica de la extraccion (IA local).

        Mecanica IDENTICA a la busqueda: cada campo extraido se embeda como
        consulta (QUERY_PROMPT) y se busca su mejor frase dentro del CV
        troceado en fragmentos embebidos como documentos (DOC_PROMPT).

        Para cada campo devuelve:
          - score: coseno con su MEJOR frase (evidencia maxima en el CV)
          - evidencia: esa frase (donde "encontro" el campo)
        Score bajo + sin evidencia literal => posible falso positivo.

        Devuelve {"titulos": {campo: {score, evidencia}}, "skills": {...},
                  "avisos": [...], "min_titulo", "min_skill"}.
        """
        from core.search_engine import DOC_PROMPT, QUERY_PROMPT

        engine = self.engine
        frases = _split_phrases(texto)
        if not frases:
            return {"titulos": {}, "skills": {}, "avisos": [],
                    "min_titulo": 0.0, "min_skill": 0.0}

        f_vecs = engine.model.encode(
            [DOC_PROMPT.format(text=f) for f in frases], normalize_embeddings=True)

        campos = ([("titulo", t) for t in propuesto.get("titulos", [])] +
                  [("skill", s) for s in propuesto.get("skills", [])])
        if not campos:
            return {"titulos": {}, "skills": {}, "avisos": [],
                    "min_titulo": 0.0, "min_skill": 0.0}

        q_vecs = engine.model.encode(
            [QUERY_PROMPT.format(text=c) for _, c in campos], normalize_embeddings=True)
        M = q_vecs @ f_vecs.T   # coseno campo x frase
        f_deacc = [deacc(f) for f in frases]

        def _literal_idx(campo: str):
            """Indices de frases que contienen el campo literalmente
            (sin acentos, frontera de palabra) -> evidencia dura."""
            cd = deacc(campo)
            patron = re.compile(r"(?<![\w\-/])" + re.escape(cd) + r"(?![\w\-/])")
            return [k for k, fd in enumerate(f_deacc) if patron.search(fd)]

        titulos, skills = {}, {}
        for i, (tipo, campo) in enumerate(campos):
            lit = _literal_idx(campo)
            if lit:
                # presencia literal en el CV: elegir la frase literal de
                # mayor coseno (la regex/lexicon ya confirmo presencia)
                j = max(lit, key=lambda k: float(M[i][k]))
                literal = True
            else:
                j = int(M[i].argmax())
                literal = False
            reg = {
                "score": round(float(M[i][j]), 3),
                "evidencia": frases[j],
                "literal": literal,
            }
            (titulos if tipo == "titulo" else skills)[campo] = reg

        # Umbrales SOLO aplican a campos sin presencia literal (decide la IA):
        # con evidencia literal la presencia es hecho, no estimacion.
        TIT_MIN, SKILL_MIN = 0.40, 0.35
        avisos = [
            f"Titulo sin presencia literal ni buena evidencia semantica: '{t}' "
            f"(coseno {r['score']:.2f}) -> '{r['evidencia'][:70]}...'"
            for t, r in titulos.items() if not r["literal"] and r["score"] < TIT_MIN
        ]
        avisos += [
            f"Skill sin presencia literal ni buena evidencia semantica: '{s_}' "
            f"(coseno {r['score']:.2f}) -> '{r['evidencia'][:70]}...'"
            for s_, r in skills.items() if not r["literal"] and r["score"] < SKILL_MIN
        ]
        return {
            "titulos": titulos,
            "skills": skills,
            "avisos": avisos,
            "min_titulo": TIT_MIN,
            "min_skill": SKILL_MIN,
        }


    # ---------------------------------------------------------------- analyze
    def analyze(self, cv_texto: str) -> Dict:
        texto = re.sub(r"\s+", " ", cv_texto).strip()
        low = deacc(texto)   # sin acentos: "metodologías ágiles" == "metodologias agiles"

        # --- skills por lexicon literal (frontera de palabra, sin acentos) ---
        skills = []
        for s in SKILL_LEXICON:
            patron = re.compile(r"(?<![\w-])" + re.escape(deacc(s)) + r"(?![\w-])")
            if patron.search(low):
                skills.append(s)

        # --- idiomas (reglas bilingues ES|EN en detect_languages_struct) ---
        idiomas_struct = detect_languages_struct(texto)
        for d in idiomas_struct:
            etiqueta = (d["idioma"] if d["nivel"] == "no especificado"
                        else f"{d['idioma']} {d['nivel']}")
            if not any(etiqueta.lower() in s.lower() for s in skills):
                skills.append(etiqueta)

        # --- campos ATS (reglas puras, ver core/ats.py) ---
        datos_ats = {
            "experiencia_anios": detect_experience_years(texto),
            "educacion_nivel": detect_education_level(texto),
            "certificaciones": detect_certifications(texto),
            "idiomas": idiomas_struct,
            "contacto": detect_contact(texto),
            "seniority": detect_seniority(texto),
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
                  "idiomas", "contacto", "seniority")
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
