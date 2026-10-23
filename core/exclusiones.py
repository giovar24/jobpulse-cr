# -*- coding: utf-8 -*-
"""core.exclusiones — exclusion PERMANENTE de puestos fuera del perfil.

Diseño (acordado con el usuario, 2026-10-22):

  scraping -> jobs_raw (todo lo crudo)
     |  salto las excluidas conocidas (tabla jobs_excluidas: job_id o clave)
  gate local (fecha + score calibrado + dominio)
     |  candidatas -> jobs (la curada)      fuera de dominio + score bajo el
     |                                       piso -> tombstone + DELETE + no
     |                                       re-entra (los boards lo republican
     |                                       manana y el filtro lo salta)
  evaluacion cuidadosa (IA externa sobre la curada, mejor gate primero)
     |  veredicto='apta' (verde, lista final) / 'descartada' (amarilla: oculta
     |  del ranking, conservada hasta que el TTL la elimine)
  TTL: vencidas (expires_at) + fuera de ventana (date_published) + sin fecha
       legible (created_at).

REGLA DE LOS PUESTOS MIXTOS: el sector NUNCA excluye solo. La exclusion exige
conjuncion: titulo fuera del dominio (veto de core/gate) Y gate_score por
debajo del piso. Un sector que matchea los sectores objetivo del perfil puede
SALVAR ("abogado de derecho digital" con perfil legal; "disenador CAD" con
perfil de ingenieria). Reciprocamente, un sector ajeno solo confirma.
"""
import logging
from typing import Optional, Tuple

from core.dedupe import clave_duplicado_exacto
from core.gate import titulo_en_dominio
from core.sectores import sectores_objetivo_de

logger = logging.getLogger("jobpulse.exclusiones")

# Piso de exclusion: por debajo, un puesto fuera del dominio no tiene
# recuperacion realista (el escape calibrado de EmbeddingGemma es 0.32; la
# mediana del no-dominio es 0.20; el junk profundo ronda 0.10-0.18).
SCORE_PISO_DEFAULT = 0.20


def criterio_exclusion(title: str, gate_score, sector, sectores_objetivo,
                       score_piso: float = SCORE_PISO_DEFAULT) -> Optional[str]:
    """None = NO excluir; texto = motivo de exclusion permanente.

    - Sin gate_score: no se afirma nada (beneficio de la duda).
    - Titulo dentro del dominio del perfil (veto del gate): re-filtrable,
      jamas exclusion permanente.
    - Sector dentro de los sectores objetivo: Protegido (roles mixtos), aunque
      el titulo sea creativo. A lo sumo lo decide el gate/la IA despues.
    - Fuera de dominio Y score < piso: exclusion permanente.
    """
    if gate_score is None:
        return None
    if titulo_en_dominio(title):
        return None
    if sector and sectores_objetivo and sector in sectores_objetivo:
        return None
    if gate_score >= score_piso:
        return None
    extra = f", sector {sector}" if sector else ""
    return (f"fuera de dominio{extra} y score {gate_score:.2f} "
            f"< piso {score_piso:.2f}")


def clave_exclusion(company: str, title: str, location: str) -> Optional[str]:
    """Clave normalizada empresa|titulo|ubicacion (la de dedupe exacto)."""
    clave = clave_duplicado_exacto(company, title, location)
    return "|".join(clave) if clave else None


def filtrar_offers(offers, claves_conocidas: set, ids_conocidos: set
                   ) -> Tuple[list, int]:
    """Saca del lote de ingesta los offers ya excluidos (por clave o job_id).

    Devuelve (offers_filtradas, n_descartadas). Es la barrera que evita que
    un puesto excluido y borrado resucite en el siguiente scraping.
    """
    if not (claves_conocidas or ids_conocidos):
        return list(offers), 0
    vivos, fuera = [], 0
    for o in offers:
        if o.job_id in ids_conocidos:
            fuera += 1
            continue
        clave = clave_exclusion(o.company, o.title, o.location)
        if clave and clave in claves_conocidas:
            fuera += 1
            continue
        vivos.append(o)
    return vivos, fuera


async def excluir_y_purgar(db, profile=None,
                           score_piso: float = SCORE_PISO_DEFAULT) -> dict:
    """Pasada post-corrida: evalua el crudo contra el criterio del perfil y
    excluye (tombstone + DELETE) lo que no encaja. Idempotente.

    La evaluacion la hace la IA LOCAL ya aplicada (gate_score almacenado) o
    criterios de SQL (fecha/vencimiento via purge_expired); no gasta cuota
    de la IA externa en basura evidente.
    """
    objetivos = sectores_objetivo_de(profile)
    crudo = await db.get_raw_jobs()
    conocidas_claves, conocidas_ids = await db.exclusiones_conocidas()

    candidatas = []  # [(fila, clave, motivo)]
    for r in crudo:
        jid = r.get("job_id") or ""
        if jid in conocidas_ids:
            continue
        clave = clave_exclusion(r.get("company"), r.get("title"),
                                r.get("location"))
        if clave and clave in conocidas_claves:
            continue
        motivo = criterio_exclusion(
            r.get("title"), r.get("gate_score"),
            (r.get("sector") or "").strip() or None,
            objetivos, score_piso)
        if motivo:
            candidatas.append((r, clave, motivo))

    if not candidatas:
        logger.info("Exclusiones: 0 nuevas en %d crudas (criterio: dominio + "
                    "score < %.2f + sectores %s).", len(crudo), score_piso,
                    ",".join(objetivos))
        return {"excluidas": 0, "borradas": 0, "total_crudo": len(crudo)}

    tombstones = []
    for r, clave, motivo in candidatas:
        tombstones.append({
            "clave": clave or f"id:{r.get('job_id')}",
            "job_id": r.get("job_id"), "source": r.get("source"),
            "title": r.get("title"), "company": r.get("company"),
            "sector": (r.get("sector") or "").strip() or None,
            "motivo": motivo, "gate_score": r.get("gate_score"),
        })
    n_reg = await db.excluir_puestos(tombstones)
    n_del = await db.purgar_excluidas([t["job_id"] for t in tombstones])
    logger.info("Exclusiones: %d puestos fuera del perfil (score < %.2f, "
                "sectores %s) -> tombstone + %d filas borradas.",
                len(tombstones), score_piso, ",".join(objetivos), n_del)

    ejemplos = []
    for t in tombstones[:10]:
        ejemplos.append(f"{t.get('gate_score')} {t.get('sector') or '-'}: "
                        f"{t.get('title')} ({t.get('company')}) — {t.get('motivo')}")
    return {"excluidas": n_reg, "borradas": n_del,
            "total_crudo": len(crudo), "ejemplos": ejemplos}
