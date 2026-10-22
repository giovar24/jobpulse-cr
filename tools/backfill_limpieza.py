# -*- coding: utf-8 -*-
"""Backfill unico de limpieza sobre jobs_raw / jobs (y marcado de duplicados).

La limpieza vive desde ahora en la frontera de `JobDatabase.save_jobs`, pero
las filas historicas entraron antes de esa normalizacion. Este script las
recorre TODAS y les aplica la misma `core.text.limpiar_texto`, sin tocar
job_id, source, gate_*, ia_* ni created_at (trazabilidad intacta).

Uso:
    python tools/backfill_limpieza.py                  # dry-run (solo cuenta)
    python tools/backfill_limpieza.py --aplicar        # limpia + marca dup_of
    python tools/backfill_limpieza.py --aplicar --sin-marcar

Deja un resumen en informes/calidad/backfill_<stamp>.md/.json.
Idempotente: correrlo dos veces reporta 0 cambios.
"""
import argparse
import asyncio
import json
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from core.rutas import ruta_informe  # noqa: E402
from core.text import limpiar_texto  # noqa: E402

# Texto que se reescribe tal cual se haria al ingerir. description/requirements
# conservan saltos de linea (los encabezados de seccion dependen de eso).
CAMPOS_TEXTO_PLANO = ("search_term", "title", "company", "location",
                      "link", "salary")
CAMPOS_TEXTO_SALTOS = ("description", "requirements")
CAMPOS_FECHA = ("date_published", "expires_at")


def _validar_tablas(tablas):
    for t in tablas:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", t):
            raise SystemExit(f"Nombre de tabla invalido: {t!r}")
    return tablas


def _limpiar_fila(fila, campos):
    """Devuelve {campo: valor_nuevo} solo con lo que cambia."""
    cambios = {}
    for campo in CAMPOS_TEXTO_PLANO:
        if campo not in campos:
            continue
        nuevo = limpiar_texto(fila[campo])
        if nuevo != (fila[campo] or ""):
            cambios[campo] = nuevo
    for campo in CAMPOS_TEXTO_SALTOS:
        if campo not in campos:
            continue
        nuevo = limpiar_texto(fila[campo], conservar_saltos=True)
        if nuevo != (fila[campo] or ""):
            cambios[campo] = nuevo
    for campo in CAMPOS_FECHA:
        if campo not in campos:
            continue
        nuevo = limpiar_texto(fila[campo]) or None
        if nuevo != fila[campo]:
            cambios[campo] = nuevo
    return cambios


def _procesar_tabla(conn, tabla, aplicar):
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({tabla})")}
    if not cols:
        return {"error": "tabla inexistente"}
    campos = [c for c in (*CAMPOS_TEXTO_PLANO, *CAMPOS_TEXTO_SALTOS,
                          *CAMPOS_FECHA) if c in cols]
    seleccion = ", ".join(f'"{c}"' for c in campos)
    filas = conn.execute(
        f'SELECT job_id, {seleccion} FROM "{tabla}"').fetchall()

    detalle = {c: 0 for c in campos}
    ejemplos = []
    updates = []
    for fila in filas:
        cambios = _limpiar_fila(fila, campos)
        if not cambios:
            continue
        for c in cambios:
            detalle[c] += 1
        if len(ejemplos) < 5:
            campo = next(iter(cambios))
            ejemplos.append({
                "job_id": fila["job_id"], "campo": campo,
                "antes": str(fila[campo])[:80],
                "despues": str(cambios[campo])[:80],
            })
        sets = ", ".join(f'"{c}" = ?' for c in cambios)
        updates.append((tabla, sets, list(cambios.values()), fila["job_id"]))

    if aplicar and updates:
        for _, sets, valores, job_id in updates:
            conn.execute(
                f'UPDATE "{tabla}" SET {sets} WHERE job_id = ?', (*valores, job_id))

    return {
        "filas": len(filas),
        "filas_modificadas": len(updates),
        "campos_modificados": detalle,
        "ejemplos": ejemplos,
    }


def _marcar_duplicados(db_path, tablas):
    from core.database import JobDatabase

    async def _run():
        db = JobDatabase(str(db_path))
        return await db.marcar_duplicados_exactos(tuple(tablas))

    return asyncio.run(_run())


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Backfill de limpieza de jobs_raw/jobs (idempotente)")
    parser.add_argument("--db", default=str(BASE_DIR / "data" / "jobs.db"))
    parser.add_argument("--tablas", default="jobs_raw,jobs")
    parser.add_argument("--aplicar", action="store_true",
                        help="Escribe los cambios (sin esto es dry-run)")
    parser.add_argument("--sin-marcar", action="store_true",
                        help="Con --aplicar: no recalcular dup_of")
    args = parser.parse_args(argv)

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"ERROR: no existe la base {db_path}", file=sys.stderr)
        return 2
    tablas = _validar_tablas([t.strip() for t in args.tablas.split(",")
                              if t.strip()])

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        resumen = {t: _procesar_tabla(conn, t, args.aplicar) for t in tablas}
        if args.aplicar:
            conn.commit()
        else:
            conn.rollback()
    finally:
        conn.close()

    marcados = 0
    if args.aplicar and not args.sin_marcar:
        marcados = _marcar_duplicados(db_path, tablas)

    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    md_path = ruta_informe("calidad", f"backfill_{stamp}.md")
    json_path = md_path.with_suffix(".json")
    lineas = [
        "# Backfill de limpieza",
        "",
        f"- Fecha: `{datetime.now().isoformat(timespec='seconds')}`",
        f"- Base: `{db_path}`",
        f"- Modo: {'APLICAR' if args.aplicar else 'DRY-RUN'}",
        f"- Duplicados marcados: {marcados}",
        "",
    ]
    for tabla, r in resumen.items():
        if "error" in r:
            lineas += [f"## {tabla}", "", f"- {r['error']}", ""]
            continue
        lineas += [
            f"## {tabla}", "",
            f"- Filas revisadas: {r['filas']}",
            f"- Filas modificadas: {r['filas_modificadas']}",
            "- Campos modificados: "
            + (", ".join(f"{k}={v}" for k, v in r["campos_modificados"].items()
                         if v) or "ninguno"),
            "",
        ]
        for e in r["ejemplos"]:
            lineas += [f"- `{e['job_id']}` · {e['campo']}:",
                       f"  - antes: `{e['antes']}`",
                       f"  - despues: `{e['despues']}`"]
        if r["ejemplos"]:
            lineas.append("")
    md_path.write_text("\n".join(lineas), encoding="utf-8")
    json_path.write_text(json.dumps({
        "generado": datetime.now().isoformat(timespec="seconds"),
        "modo": "aplicar" if args.aplicar else "dry-run",
        "db": str(db_path), "tablas": resumen,
        "duplicados_marcados": marcados,
    }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    total = sum(r.get("filas_modificadas", 0) for r in resumen.values()
                if "error" not in r)
    print(f"Modo: {'APLICAR' if args.aplicar else 'DRY-RUN'}")
    for tabla, r in resumen.items():
        if "error" in r:
            print(f"  {tabla}: {r['error']}")
        else:
            print(f"  {tabla}: {r['filas_modificadas']}/{r['filas']} filas "
                  f"modificadas")
    print(f"Duplicados marcados: {marcados}")
    print(f"Resumen: {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
