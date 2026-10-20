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
