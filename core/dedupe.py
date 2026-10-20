# -*- coding: utf-8 -*-
"""core.dedupe — dedupe cross-source boards vs providers directos.

REGLA DE NEGOCIO (razon de la separacion providers/boards vs
providers/directos): el mismo puesto se publica dos veces — en agregadores
(LinkedIn, Indeed, ANE...) y en el ATS propio (Workday, Greenhouse,
Rippling...). Prioridad: si el puesto YA esta en un provider directo, se
descarta la copia de los job boards; si solo existe en boards (o solo en
directos), se conserva.

No se deduplican ofertas DENTRO del mismo grupo (eso ya lo cubre el PK
job_id por fuente). El matching es conservador: empresa normalizada igual +
titulo normalizado igual + ubicacion compatible. Normalizaciones:
- unicode NFKD->ASCII, lowercase
- ruido de ubicacion/remoto fuera (tokens descartados)
- codigos de req tipo "(12345-1)" al final se quitan
- company saca sufijos legales (inc/llc/sa/s.a...).
"""
import logging
import re
import unicodedata
from typing import Iterable, List, Sequence

from providers.base import JobOffer
from providers.source_groups import GROUP_DIRECTOS, source_group

logger = logging.getLogger("jobpulse.dedupe")

# tokens que NO cuentan para matching (ubicacion/remoto en el titulo)
_RUIDO_TOK = {
    "costa", "rica", "remote", "remoto", "remota", "remota", "hibrido",
    "hybrid", "on", "site", "onsite", "off", "offsite", "presencial",
    "san", "jose", "heredia", "alajuela", "cartago", "escazu", "santa",
    "ana", "moravia", "curridabat", "barreal", "liberia", "guanacaste",
    "cr", "costarica", "beln", "beln2",
}
# sufijos legales de company
_COMPANY_SUFFIX = {"inc", "llc", "ltd", "plc", "sa", "sasa", "corp",
                   "corporation", "company", "group", "holdings", "srl",
                   "gmbh", "co"}


def _ascii(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    return s.encode("ascii", "ignore").decode("ascii")


def _tokens(s: str) -> List[str]:
    s = _ascii(s).lower()
    s = s.replace("+", " ").replace("&", " ")   # "Kuehne+Nagel"=="Kuehne + Nagel"
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return [t for t in s.split() if t]


def norm_title(title: str) -> str:
    """Tokens del titulo sin ruido de ubicacion/remoto ni codigos de req,
    ORDEN-INSENSITIVE (boards reordenan: 'Engineer, Senior')."""
    toks = _tokens(title)
    # quitar codigos de requisicion al final ("senior dev 22098 1", "(1234)")
    while toks and (toks[-1].isdigit() or len(toks[-1]) <= 2):
        toks.pop()
    return " ".join(sorted(t for t in toks if t not in _RUIDO_TOK))


def norm_company(company: str) -> str:
    toks = [t for t in _tokens(company) if t not in _RUIDO_TOK]
    while toks and (toks[-1] in _COMPANY_SUFFIX or len(toks[-1]) <= 2):
        toks.pop()   # quita tambien "AG"/"KG"/"s a" del "Kuehne + Nagel (AG & Co.) KG"
    return " ".join(sorted(toks))


def norm_place(place: str) -> str:
    return " ".join(t for t in _tokens(place) if t not in _RUIDO_TOK)


def _key(o: JobOffer) -> tuple:
    empresa = norm_company(o.company)
    titulo = norm_title(o.title)
    lugar = norm_place(o.location or "")
    return (empresa, titulo, lugar)


def _key_base(o: JobOffer) -> tuple:
    """Matching relajado (sin ubicacion) para cuando una fuente no la trae."""
    return (norm_company(o.company), norm_title(o.title))


def _link_key(link: str) -> str:
    """Link normalizado (sin query/UTM, lowercase) para matching exacto."""
    return (link or "").split("?")[0].strip().lower()


def dedupe_boards_vs_directos(
        offers: Iterable[JobOffer],
        referencia: Iterable[JobOffer] = ()) -> tuple[List[JobOffer], int]:
    """Politica: los providers DIRECTOS ganan; en boards se descartan los
    puestos que ya existen en directos (otra fuente).

    - mismo (empresa, titulo[, ubic]) exacto o relajado -> duplicado.
    - mismo link (query/UTM off) -> duplicado (agregadores redirigen al ATS).
    - si la ubic difiere (o falta de un lado) y empresa+titulo coinciden,
      TAMBIEN se considera duplicado (agregadores reetiquetan lugares).

    `referencia`: ofertas DIRECTO de corridas anteriores (lee jobs_raw con
    `main.py --solo boards`). Sin ella, cuando las dos corridas se lanzan por
    separado la de boards no ve a la de ATS y reintroduciria copias que esta
    ya habia descartado. Solo aporta llaves de directos; nunca agrega filas.

    Devuelve (offers_filtradas, n_descartadas). No dedupe intra-grupo.
    """
    vivos: List[JobOffer] = []
    directo_keys: set = set()
    directo_keys_base: set = set()
    directo_links: set = set()
    descartadas = 0

    def _es_directo(o: JobOffer) -> bool:
        return source_group(o.source) == GROUP_DIRECTOS

    def _registrar(o: JobOffer) -> None:
        directo_keys.add(_key(o))
        directo_keys_base.add(_key_base(o))
        lk = _link_key(o.link)
        if lk:
            directo_links.add(lk)

    lista = list(offers)
    # registrar TODO lo directo primero (incluida la referencia de corridas
    # anteriores: va antes para que mande sobre los boards de ESTA corrida)
    for o in referencia:
        if _es_directo(o):
            _registrar(o)
    for o in lista:
        if _es_directo(o):
            _registrar(o)

    for o in lista:
        if _es_directo(o):
            vivos.append(o)
            continue
        k, kb = _key(o), _key_base(o)
        if k in directo_keys or kb in directo_keys_base:
            descartadas += 1
            continue
        lk = _link_key(o.link)
        if lk and lk in directo_links:
            descartadas += 1
            continue
        vivos.append(o)

    if descartadas:
        logger.info("Dedupe boards<->directos: %d puestos de job boards "
                    "descartados (ya estan en providers directos).",
                    descartadas)
    return vivos, descartadas
