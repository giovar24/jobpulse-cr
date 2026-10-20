"""Mapeo source -> grupo ("boards" | "directos").

Razon de negocio: el mismo puesto (job posting) suele aparecer dos veces:
en un agregador/job board (LinkedIn, Indeed, ANE, Procomer, CINDE) y en el
ATS directo de la empresa (Workday, Greenhouse, Rippling...). El dedupe
cross-source usa este mapeo para decidir cual instancia conservar:

- **directos**: fuente canonica (req id estable, fecha exacta del ATS,
  descripcion completa) -> se CONSERVA.
- **boards**: republicación en agregadores -> se DESCARTA como duplicada
  cuando un equal-con-label de (empresa, titulo, ubic) ya existe en
  directos (ver core.dedupe).

Providers REMOVIDOS del mapeo (sources antiguos) caen al grupo "boards".
"""

GROUP_BOARDS = "boards"
GROUP_DIRECTOS = "directos"

SOURCE_GROUP = {
    # --- job boards / portales locales (agregadores) ---
    "indeed": GROUP_BOARDS,
    "linkedin": GROUP_BOARDS,
    "ane": GROUP_BOARDS,
    "procomer": GROUP_BOARDS,
    "cinde": GROUP_BOARDS,
    # --- providers directos contra el ATS de la empresa ---
    "workday": GROUP_DIRECTOS,
    "phenom": GROUP_DIRECTOS,
    "tp": GROUP_DIRECTOS,
    "greenhouse": GROUP_DIRECTOS,
    "icims": GROUP_DIRECTOS,
    "smartrecruiters": GROUP_DIRECTOS,
    "successfactors": GROUP_DIRECTOS,
    "rippling": GROUP_DIRECTOS,
    "ats_ligeros": GROUP_DIRECTOS,   # ashby/lever/recruitee/workable
    "login_sites": GROUP_DIRECTOS,   # front propio del employer via browser
}

BOARDS_SOURCES = tuple(s for s, g in SOURCE_GROUP.items() if g == GROUP_BOARDS)
DIRECTOS_SOURCES = tuple(s for s, g in SOURCE_GROUP.items() if g == GROUP_DIRECTOS)


def source_group(source: str) -> str:
    """Grupo de un source; boards si el source no esta mapeado."""
    return SOURCE_GROUP.get((source or "").strip().lower(), GROUP_BOARDS)


# --------------------------------------------------------------------------
# ALCANCE DE LA CORRIDA (--solo): dos corridas separables en vez de una sola.
#
# Razon: los portales de la empresa (ATS: Workday, Greenhouse...) y los job
# boards (Indeed, LinkedIn, Procomer, ANE/Brete, CINDE) son naturalezas
# distintas — los ATS son la fuente canonica y los boards agregan o
# republican. Correrlos aparte permite un fallo parcial sin tumbar la otra
# corrida y darles cadencia/parametros propios.
#
# El corte reutiliza el par boards|directos que YA existe, asi que no se
# inventa un tercer mapeo: `boards` pasa a ser su propio alcance y
# `directos` pasa a llamarse "ats" en la CLI.
# --------------------------------------------------------------------------
RUN_TODOS = "todos"     # corrida unica (como hasta ahora; default)
RUN_ATS = "ats"         # solo providers/directos/ + login/
RUN_BOARDS = "boards"   # solo providers/boards/ (agregadores + locales)

RUN_CHOICES = (RUN_TODOS, RUN_ATS, RUN_BOARDS)

RUN_LABEL = {
    RUN_TODOS: "corrida completa",
    RUN_ATS: "ATS de empresa",
    RUN_BOARDS: "job boards",
}

# Que grupos de SOURCE_GROUP entran en cada alcance; None = todos.
_RUN_GRUPOS = {
    RUN_TODOS: None,
    RUN_ATS: frozenset({GROUP_DIRECTOS}),
    RUN_BOARDS: frozenset({GROUP_BOARDS}),
}


def run_incluye(solo: str, grupo: str) -> bool:
    """¿El grupo entra en el alcance elegido con --solo?

    Alcance desconocido = todo (no se debe perder una corrida por un typo).
    """
    permitidos = _RUN_GRUPOS.get((solo or "").strip().lower())
    return permitidos is None or grupo in permitidos
