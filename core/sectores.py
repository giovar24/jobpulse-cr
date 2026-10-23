# -*- coding: utf-8 -*-
"""core.sectores — etiquetado de sector por funcion del puesto.

El sector se usa como SENAL, nunca como filtro mortal solo: la exclusion
permanente (core/exclusiones.py) exige "titulo fuera de dominio Y score
semantico bajo el piso"; un sector que matchea los sectores objetivo del
perfil puede SALVAR un puesto (protege los roles mixtos: "abogado de derecho
digital" con perfil legal sigue siendo candidato aunque el titulo diga
"digital").

Regla de los puestos mixtos:
  - El SECTOR se infiere de la FUNCION del titulo (cocinero -> alimentos).
  - Si el sector del puesto esta en los sectores objetivo del perfil, el
    puesto NUNCA se excluye por sector (a lo sumo lo decide el gate/IA).
  - Si el sector no matchea, solo sirve como confirmacion extra: sin score
    bajo, no excluye nada.

Fuentes: CINDE publica "positionCategoryNameEs" (sector real de la fuente);
se normaliza contra el taxonomo con mapeo suave y lo que no mapea queda tal
cual (deaccented, minusculas) — desconocido = lo decide el score.
"""
import unicodedata

# Sectores objetivo por defecto del perfil activo (soporte tecnico bilingue +
# atencion al cliente bilingue). Se pisa desde profile.json:
#   "busqueda": {"sectores": ["legal", ...]}
SECTORES_OBJETIVO_DEFAULT = ("tecnologia", "atencion_cliente")

# Taxonomia: orden = prioridad de matching (primera coincidencia gana; la
# palabra completa, con padding de espacios para no matchear subcadenas).
# ES + EN. Solo la FUNCION del titulo: un puesto de cocina en una empresa
# tecnologica sigue siendo "alimentos".
_CLAVES = [
    ("tecnologia", (
        "soporte tecnico", "soporte", "support", "help desk", "helpdesk",
        "service desk", "mesa de ayuda", "mesa de servicio", "mesa de parte",
        "informatic", "informatica", "sistemas", "software", "developer",
        "desarrollador", "programador", "datos", "data", "cloud", "redes",
        "network", "devops", "sysadmin", "qa", "tester", "tecnologia",
        "electronica", "electronic", "telecomunicaciones", "ciberseguridad",
        "seguridad informatica", "administrador de sistemas", "servidor",
        "tecnico en computo", "computo", "web", "aplicaciones", "soporte ti",
    )),
    ("legal", (
        "abogado", "abogacia", "derecho", "legal", "juridico", "juridica",
        "notarial", "compliance", "paralegal", "law", "attorney",
        "asesor legal", "corporativo legal",
    )),
    ("salud", (
        "enfermera", "enfermero", "medico", "medica", "doctor", "salud",
        "clinica", "hospital", "farmacia", "farmaceutico", "farmaceutica",
        "dental", "odontologo", "odontologa", "psicologo", "psicologa",
        "nurse", "medical", "physician", "nutricion", "nutricionista",
    )),
    ("finanzas", (
        "contador", "contadora", "contable", "contabilidad", "finanzas",
        "financiero", "tesoreria", "tesorero", "auditor", "auditoria",
        "banca", "bancario", "credit", "credito", "cobros", "accounting",
        "finance", "payroll", "nomina", "bursatil", "seguros", "insurance",
    )),
    ("alimentos", (
        "cocinero", "cocinera", "chef", "cocina", "mesero", "mesera",
        "camarero", "camarera", "bartender", "barista", "kitchen",
        "alimentos", "panadero", "nutricionista", "ayudante de cocina",
    )),
    ("manufactura", (
        "operario", "operaria", "produccion", "planta", "manufactura",
        "bodeguero", "bodega", "almacen", "ensamblador", "warehouse",
        "mecanico", "mecanica", "mantenimiento", "electricista", "soldador",
        "industrial", "maquinaria", "calidad",
    )),
    ("transporte", (
        "conductor", "chofer", "motorizado", "piloto", "repartidor",
        "delivery", "driver", "mensajero", "flota", "transporte",
    )),
    ("educacion", (
        "profesor", "profesora", "docente", "maestro", "maestra",
        "educador", "instructor", "capacitador", "teacher", "tutor",
        "academico", "educacion",
    )),
    ("rrhh", (
        "recursos humanos", "reclutamiento", "reclutador", "reclutadora",
        "seleccion", "talento", "recruiter", "hiring", "onboarding",
    )),
    ("marketing_diseno", (
        "marketing", "mercadeo", "disenador", "diseno", "grafico",
        "grafica", "contenido", "community manager", "social media",
        "seo", "publicista", "publicidad", "copywriter", "fotografia",
        "audiovisual", "creativo",
    )),
    ("ventas", (
        "ventas", "vendedor", "vendedora", "comercial", "telemarketing",
        "telesales", "sales", "ejecutivo comercial", "preventa",
        "business development",
    )),
    ("atencion_cliente", (
        "atencion al cliente", "atencion a clientes", "servicio al cliente",
        "customer service", "customer care", "customer experience",
        "agente bilingue", "agente", "call center", "contact center",
        "csr", "encargado de servicio", "teleoperator", "recepcionista",
    )),
    ("administrativa", (
        "asistente administrativo", "secretaria", "secretarial",
        "asistente ejecutivo", "auxiliar administrativo", "administrativo",
        "administrativa", "back office", "archivo", "compras",
        "asistente virtual", "oficina",
    )),
]

# Mapeo suave de las categorias que publican las FUENTES (hoy CINDE) hacia el
# taxonomo. Lo que no mapea queda como etiqueta propia (deaccented/minuscula):
# desconocido = lo decide el score, nunca excluye solo.
_FUENTE_A_SECTOR = {
    "ingenieria": "tecnologia",
    "ingenieria y operaciones": "manufactura",
    "servicios compartidos": "finanzas",
    "ciencias economicas": "finanzas",
    "tecnologia": "tecnologia",
    "operaciones": "manufactura",
    "comercial": "ventas",
    "recursos humanos": "rrhh",
}


def _deacc(texto) -> str:
    s = unicodedata.normalize("NFD", str(texto or ""))
    return "".join(c for c in s if unicodedata.category(c) != "Mn").lower().strip()


def inferir_sector(title: str) -> str | None:
    """Sector por la funcion del TITULO (primera coincidencia gana).

    Solo el titulo: la descripcion menciona muchos oficios y confundiria.
    Sin coincidencia devuelve None: desconocido = lo decide el score.
    """
    titulo = f" {_deacc(title)} "
    for sector, claves in _CLAVES:
        for clave in claves:
            if f" {clave} " in titulo:
                return sector
    return None


def normalizar_sector_fuente(categoria) -> str | None:
    """Etiqueta de la fuente (ej. CINDE positionCategoryNameEs) -> sector
    canonico si mapea; si no, la etiqueta limpia; None si viene vacia."""
    if not categoria:
        return None
    cat = _deacc(categoria)
    return _FUENTE_A_SECTOR.get(cat, cat or None)


def inferir_sectores_perfil(profile) -> tuple:
    """Auto-evaluacion del perfil: a que sector(s) pertenece el candidato.

    Ponderado por INTENCION: lo que BUSCA (puestos objetivo) manda; los
    titulos del CV lo respaldan; la experiencia vivida pesa poco (trabajar
    en soporte de tarjetas de credito no hace del contador un candidato:
    los sectores objetivo son lo que el usuario QUIERE, no toda industria
    que piso) y las skills casi nada. Calibrado con el perfil real
    (2026-10-22): con experiencia a 2.0, "Credit Card Services" metia
    'finanzas' al objetivo y protegia puestos de contabilidad pura.
    """
    pesos_tally: dict = {}

    def votar(texto, peso):
        sector = inferir_sector(texto)
        if sector:
            pesos_tally[sector] = pesos_tally.get(sector, 0.0) + peso

    busq = getattr(profile, "busqueda", {}) or {}
    for t in (busq.get("puestos_objetivo") or []):
        votar(t, 3.0)
    for t in (getattr(profile, "titulos", None) or []):
        votar(t, 2.0)
    for e in (getattr(profile, "experiencia", None) or [])[:8]:
        if isinstance(e, dict):
            votar(e.get("puesto") or "", 1.0)
    for s in (getattr(profile, "skills", None) or [])[:30]:
        votar(s, 0.25)

    if not pesos_tally:
        return ()
    # Sectores con senal real: uno o dos puestos objetivo/titulos, o una
    # acumulacion fuerte de experiencia+skills.
    ordenados = [(s, p) for s, p in sorted(pesos_tally.items(),
                                           key=lambda x: -x[1])
                 if p >= 1.5]
    return tuple(s for s, _ in ordenados)


def sectores_objetivo_de(profile) -> tuple:
    """Sectores objetivo del perfil, en orden de precedencia:

    1. Manual: `busqueda.sectores` en profile.json (el usuario pisa si quiere).
    2. Auto-inferido del propio perfil (inferir_sectores_perfil): sin
       configuracion, los filtros se alinean solos con lo que el perfil dice.
    3. Default de la sesion activa (ultimo recurso si el perfil no dice nada).
    """
    try:
        manual = (getattr(profile, "busqueda", {}) or {}).get("sectores") or []
    except AttributeError:
        manual = []
    explicito = tuple(_deacc(s) for s in manual if str(s or "").strip())
    if explicito:
        return explicito
    inferido = inferir_sectores_perfil(profile)
    if inferido:
        return inferido
    return SECTORES_OBJETIVO_DEFAULT
