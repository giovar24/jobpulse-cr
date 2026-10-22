# -*- coding: utf-8 -*-
"""Auditoria de calidad de la base de vacantes (SOLO LECTURA).

Uso:
    python tools/auditar.py
    python tools/auditar.py --db data/jobs.db --salida informes/calidad/auditoria.md
    python tools/auditar.py --tablas jobs

Escribe un Markdown y un JSON en informes/calidad/ (o donde indique --salida).
Devuelve exit code 1 si alguna metrica supera su umbral, para poder usarlo como
puerta de calidad en una corrida o en CI.
"""
import argparse
import json
import re
import sqlite3
import sys
import unicodedata
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from core.rutas import ruta_informe  # noqa: E402

UMBRALES = {
    "html_tags": 0,
    "entities": 0,
    "double_spaces": 0,
    "invalid_dates": 0,
    "empty_title": 0,
    "empty_link": 0,
    "expired": 0,
    "duplicate_groups": 0,
}

# Metricas que se reportan pero no bloquean (faltantes que pueden ser
# legitimos: portales que no publican salario o requisitos).
INFORMATIVAS = ("empty_description", "empty_requirements", "empty_salary",
                "dup_marcados")

ORDEN = ("filas", "html_tags", "entities", "double_spaces", "invalid_dates",
         "empty_title", "empty_link", "empty_description",
         "empty_requirements", "empty_salary", "duplicate_groups",
         "duplicate_rows", "dup_marcados", "expired")


def _norm(valor) -> str:
    """Clave de comparacion: NFKD -> ASCII, minusculas, solo alfanumerico."""
    s = unicodedata.normalize("NFKD", str(valor or ""))
    s = s.encode("ascii", "ignore").decode("ascii").lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())


# Etiqueta real: '<' pegado a letra o '/' de cierre. No matchea texto como
# "English <> Spanish" ni comparaciones "a < b > c".
_RE_TAG = re.compile(r"<[a-zA-Z/][^<>]*>")


def _tiene_html(texto) -> int:
    return 1 if texto and _RE_TAG.search(str(texto)) else 0


def _tablas(conn) -> list:
    return [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")]


def _columnas(conn, tabla: str) -> set:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({tabla})")}


def _scalar(conn, sql: str, params=()):
    fila = conn.execute(sql, params).fetchone()
    return fila[0] if fila else None


def _metricas(conn, tabla: str) -> dict:
    cols = _columnas(conn, tabla)
    tiene = lambda c: c in cols  # noqa: E731
    filtro_dup = (" AND TRIM(COALESCE(dup_of, '')) = ''"
                  if tiene("dup_of") else "")

    m = {"filas": _scalar(conn, f"SELECT COUNT(*) FROM {tabla}") or 0}

    def contar(where: str) -> int:
        if not where:
            return 0
        return _scalar(conn, f"SELECT COUNT(*) FROM {tabla} WHERE {where}") or 0

    campos_texto = [c for c in ("title", "company", "location", "description",
                                "requirements") if tiene(c)]
    m["html_tags"] = contar(" OR ".join(
        f"_tiene_html({c})" for c in campos_texto))
    m["entities"] = contar(" OR ".join(
        f"{c} LIKE '%&amp;%' OR {c} LIKE '%&lt;%' OR {c} LIKE '%&gt;%' "
        f"OR {c} LIKE '%&#%'" for c in campos_texto))
    m["double_spaces"] = contar(" OR ".join(
        f"{c} LIKE '%  %'" for c in campos_texto))

    fechas = [c for c in ("date_published", "expires_at") if tiene(c)]
    m["invalid_dates"] = contar(" OR ".join(
        f"(TRIM(COALESCE({c}, '')) <> '' AND date({c}) IS NULL)"
        for c in fechas))
    m["expired"] = contar(
        "TRIM(COALESCE(expires_at, '')) <> '' "
        "AND date(expires_at) < date('now')") if tiene("expires_at") else 0

    for campo in ("title", "link", "description", "requirements", "salary"):
        clave = f"empty_{campo}"
        m[clave] = contar(
            f"TRIM(COALESCE({campo}, '')) = ''") if tiene(campo) else 0

    m["dup_marcados"] = (_scalar(
        conn, f"SELECT COUNT(*) FROM {tabla} "
              "WHERE TRIM(COALESCE(dup_of, '')) <> ''") or 0
        if tiene("dup_of") else 0)

    if all(tiene(c) for c in ("company", "title", "location")):
        sub = (f"SELECT _norm(company) || '|' || _norm(title) || '|' || "
               f"_norm(location) AS clave, COUNT(*) AS n FROM {tabla} "
               f"WHERE 1=1{filtro_dup} GROUP BY clave HAVING COUNT(*) > 1")
        grupos, filas = conn.execute(
            f"SELECT COUNT(*), COALESCE(SUM(n), 0) FROM ({sub})").fetchone()
        m["duplicate_groups"] = grupos or 0
        m["duplicate_rows"] = filas or 0
    else:
        m["duplicate_groups"] = 0
        m["duplicate_rows"] = 0

    m["fuentes"] = {r[0] or "(sin fuente)": r[1] for r in conn.execute(
        f"SELECT source, COUNT(*) FROM {tabla} GROUP BY source "
        "ORDER BY COUNT(*) DESC")} if tiene("source") else {}
    return m


def _render_md(db: Path, tablas: list, datos: dict, fallos: list,
               avisos: list, integridad: str, journal: str) -> str:
    ahora = datetime.now().isoformat(timespec="seconds")
    lineas = [
        "# Auditoria de calidad de vacantes",
        "",
        f"- Fecha: `{ahora}`",
        f"- Base: `{db}`",
        f"- Tablas: {', '.join(f'`{t}`' for t in tablas)}",
        f"- Integridad: `{integridad}`",
        f"- Journal: `{journal}`",
        f"- Resultado: {'**FALLA**' if fallos else '**OK**'}",
        "",
        "## Metricas",
        "",
        "| Metrica | " + " | ".join(tablas) + " | Umbral |",
        "|---|" + "---|" * len(tablas) + "---|",
    ]
    for clave in ORDEN:
        if not any(clave in datos[t] for t in tablas):
            continue
        vals = []
        for t in tablas:
            v = datos[t].get(clave)
            vals.append("" if v is None else str(v))
        umbral = UMBRALES.get(clave, "-")
        lineas.append(f"| {clave} | " + " | ".join(vals) + f" | {umbral} |")
    lineas += ["", "## Fallos", ""]
    lineas += [f"- {f}" for f in fallos] or ["- Ninguno."]
    lineas += ["", "## Advertencias", ""]
    lineas += [f"- {a}" for a in avisos] or ["- Ninguna."]
    lineas += ["", "## Distribucion por fuente", ""]
    for t in tablas:
        fuentes = datos[t].get("fuentes") or {}
        if not fuentes:
            continue
        lineas.append(f"**{t}**")
        lineas.append("")
        for fuente, n in fuentes.items():
            lineas.append(f"- {fuente}: {n}")
        lineas.append("")
    return "\n".join(lineas).rstrip() + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Auditoria de calidad (solo lectura)")
    parser.add_argument("--db", default=str(BASE_DIR / "data" / "jobs.db"),
                        help="Ruta a jobs.db")
    parser.add_argument("--tablas", default="jobs,jobs_raw",
                        help="Tablas a auditar, separadas por coma")
    parser.add_argument("--salida", default="",
                        help="Markdown de salida (default: informes/calidad/)")
    args = parser.parse_args(argv)

    db = Path(args.db)
    if not db.exists():
        print(f"ERROR: no existe la base {db}", file=sys.stderr)
        return 2

    pedidas = [t.strip() for t in args.tablas.split(",") if t.strip()]
    try:
        conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error as e:
        print(f"ERROR: no se pudo abrir {db}: {e}", file=sys.stderr)
        return 2
    conn.create_function("_norm", 1, _norm)
    conn.create_function("_tiene_html", 1, _tiene_html)

    try:
        existentes = _tablas(conn)
        tablas = [t for t in pedidas if t in existentes]
        faltantes = [t for t in pedidas if t not in existentes]
        if not tablas:
            print("ERROR: ninguna tabla valida para auditar.", file=sys.stderr)
            return 2

        integridad = _scalar(conn, "PRAGMA quick_check") or "desconocida"
        journal = _scalar(conn, "PRAGMA journal_mode") or "desconocido"
        datos = {t: _metricas(conn, t) for t in tablas}
    finally:
        conn.close()

    fallos, avisos = [], []
    if faltantes:
        avisos.append(f"Tablas inexistentes: {', '.join(faltantes)}")
    if str(integridad).lower() != "ok":
        fallos.append(f"quick_check: {integridad}")
    if str(journal).lower() != "wal":
        avisos.append(f"journal_mode={journal} (se recomienda WAL)")
    for t in tablas:
        for clave, umbral in UMBRALES.items():
            valor = datos[t].get(clave)
            if valor is not None and valor > umbral:
                fallos.append(f"{t}.{clave}={valor} (umbral {umbral})")
        for clave in INFORMATIVAS:
            valor = datos[t].get(clave)
            if valor:
                avisos.append(f"{t}.{clave}={valor}")

    salida = (Path(args.salida) if args.salida else
              ruta_informe("calidad",
                           f"auditoria_{datetime.now():%Y%m%d_%H%M}.md"))
    salida.parent.mkdir(parents=True, exist_ok=True)
    json_path = (salida.with_suffix(".json") if salida.suffix
                 else Path(str(salida) + ".json"))
    salida.write_text(
        _render_md(db, tablas, datos, fallos, avisos, integridad, journal),
        encoding="utf-8")
    json_path.write_text(json.dumps({
        "generado": datetime.now().isoformat(timespec="seconds"),
        "db": str(db), "tablas": datos, "umbrales": UMBRALES,
        "fallos": fallos, "avisos": avisos, "ok": not fallos,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"Auditoria: {salida}")
    print(f"JSON:      {json_path}")
    print(f"Resultado: {'FALLA' if fallos else 'OK'} ({len(fallos)} fallos, "
          f"{len(avisos)} avisos)")
    return 1 if fallos else 0


if __name__ == "__main__":
    raise SystemExit(main())
