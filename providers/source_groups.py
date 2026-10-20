"""Mapeo source -> grupo.

Hay DOS niveles:

1. **Grupo publico (2, es lo que se ve y se filtra)**:
   - `estatales`: los tres portales oficiales de Costa Rica (ANE/Brete,
     CINDE, Procomer).
   - `otros`: todo lo demas (Indeed, LinkedIn, Workday, iCIMS...).
2. **Clasificacion interna (3, NO se muestra)**: `boards` | `directos` |
   `estatales`. Solo sirve para dos decisiones que exigen distinguir al
   agregador del ATS:

   - *dedupe cross-source*: el mismo puesto suele aparecer en un job board
     y en el ATS de la empresa; se conserva la version **directa** (req id
     estable, fecha exacta, descripcion completa) y se descarta la copia
     republicada (ver core.dedupe).
   - *alcance de la corrida* (`--solo ats|boards`): los ATS de empresa se
     barren aparte de los agregadores + estatales.

Providers REMOVIDOS del mapeo (sources antiguos) caen al grupo `otros`.
"""

GROUP_ESTATALES = "estatales"   # grupo publico 1
GROUP_OTROS = "otros"           # grupo publico 2
GRUPOS_PUBLICOS = (GROUP_ESTATALES, GROUP_OTROS)

# --- clasificacion interna (dedupe + alcance de corrida) ---
GROUP_BOARDS = "boards"
GROUP_DIRECTOS = "directos"

SOURCE_GROUP = {
    # --- portales estatales / de promocion publica ---
    # ANE (Brete.cr, MTSS), CINDE y Procomer (talento.procomer.com). Se
    # distinguen de los agregadores porque son los tres portales oficiales de
    # empleo de Costa Rica: ninguno publica fecha confiable, por eso se les
    # da el beneficio de la duda en la ventana del gate.
    "ane": GROUP_ESTATALES,
    "cinde": GROUP_ESTATALES,
    "procomer": GROUP_ESTATALES,
    # --- job boards / agregadores ---
    "indeed": GROUP_BOARDS,
    "linkedin": GROUP_BOARDS,
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
ESTATALES_SOURCES = tuple(s for s, g in SOURCE_GROUP.items() if g == GROUP_ESTATALES)


def source_group(source: str) -> str:
    """Clasificacion interna (boards | directos | estatales); boards si no esta mapeado."""
    return SOURCE_GROUP.get((source or "").strip().lower(), GROUP_BOARDS)


def grupo_publico(source: str) -> str:
    """Grupo que se MUESTRA y se filtra: estatales | otros.

    Indeed/LinkedIn y los ATS de empresa comparten grupo en la UI porque la
    distincion agregador-vs-ATS es una decision interna (dedupe y alcance),
    no algo que el usuario tenga que filtrar.
    """
    return (GROUP_ESTATALES if source_group(source) == GROUP_ESTATALES
            else GROUP_OTROS)


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
# `directos` pasa a llamarse "ats" en la CLI. Los portales estatales entran
# en los DOS alcances: no son ATS de empresa, asi que --solo ats no los
# barre, pero --solo boards (y el default) si.
# --------------------------------------------------------------------------
RUN_TODOS = "todos"     # corrida unica (como hasta ahora; default)
RUN_ATS = "ats"         # solo providers/directos/ + login/
RUN_BOARDS = "boards"   # providers/boards/ (agregadores) + estatales

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
    RUN_BOARDS: frozenset({GROUP_BOARDS, GROUP_ESTATALES}),
}


def run_incluye(solo: str, grupo: str) -> bool:
    """¿El grupo entra en el alcance elegido con --solo?

    Alcance desconocido = todo (no se debe perder una corrida por un typo).
    """
    permitidos = _RUN_GRUPOS.get((solo or "").strip().lower())
    return permitidos is None or grupo in permitidos
