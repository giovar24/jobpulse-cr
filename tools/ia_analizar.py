# -*- coding: utf-8 -*-
"""Analiza las evaluaciones de la muestra estratificada vs el gate local.

Lee ia_evals (tipo="vacante", con "grupo" en el payload) y jobs_raw, agrupa
por (proveedor, modelo) y produce informes/calidad/calibracion_gate.md:

  - Pearson/Spearman gate_score <-> score IA por modelo
  - Score IA promedio por grupo de la muestra (A..E)
  - Barrido del umbral del gate simulando la regla completa:
    aprobar = (titulo en dominio Y gate >= t) O gate >= escape
  - Falsos negativos (IA >= --ia-aplica pero el gate rechaza) y
    falsos positivos (gate aprueba pero IA < --ia-basura)
  - Acuerdo entre modelos sobre el traslape

Uso: python tools/ia_analizar.py [--ia-aplica 70] [--ia-basura 50]
"""
import argparse
import io
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from core.gate import titulo_en_dominio  # noqa: E402
from core.ia_calibracion import _pearson, _spearman  # noqa: E402
from core.rutas import ruta_informe  # noqa: E402

UMBRAL_SWEEP = [0.22, 0.24, 0.25, 0.26, 0.27, 0.28, 0.30]
GRUPO_ORDEN = ["A_aprobadas", "B_banda_critica", "C_dominio_bajo",
               "D_no_dominio_alto", "E_no_dominio_bajo"]

IA_APLICA = 70.0
IA_BASURA = 50.0
UMBRAL_CANDIDATO = 0.26
ESCAPE_PROPUESTO = 0.32


def cargar_evals(db_path: str):
    """[(modelo, ...)] tomando la ultima evaluacion por (modelo, job_id);
    solo las de la muestra estratificada (payload con grupo)."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    filas = conn.execute(
        "SELECT e.proveedor, e.modelo, e.job_id, e.score, e.payload, e.ts, "
        "r.gate_score, r.title, r.company "
        "FROM ia_evals e LEFT JOIN jobs_raw r ON r.job_id = e.job_id "
        "WHERE e.tipo = 'vacante' ORDER BY e.ts ASC, e.id ASC").fetchall()
    conn.close()
    unicos = {}
    for f in filas:
        try:
            grupo = (json.loads(f["payload"] or "{}") or {}).get("grupo")
        except json.JSONDecodeError:
            continue
        if not grupo or f["score"] is None or f["gate_score"] is None:
            continue
        unicos[(f["modelo"], f["job_id"])] = {
            "modelo": f["modelo"], "proveedor": f["proveedor"],
            "job_id": f["job_id"], "grupo": grupo,
            "ia": float(f["score"]), "gate": float(f["gate_score"]),
            "title": f["title"] or "", "company": f["company"] or "",
            "dominio": titulo_en_dominio(f["title"]),
        }
    por_modelo = defaultdict(list)
    for v in unicos.values():
        por_modelo[v["modelo"]].append(v)
    return por_modelo


def simulacion(muestra, t: float, escape: float):
    """Regla completa del gate contra el veredicto de la IA."""
    tp = fp = fn = tn = 0
    for r in muestra:
        pasa = (r["dominio"] and r["gate"] >= t) or r["gate"] >= escape
        aplica = r["ia"] >= IA_APLICA
        if pasa and aplica:
            tp += 1
        elif pasa and not aplica:
            fp += 1
        elif not pasa and aplica:
            fn += 1
        else:
            tn += 1
    prec = tp / (tp + fp) if tp + fp else 0.0
    rec = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
    return tp, fp, fn, tn, prec, rec, f1


def _fmt(x):
    return f"{x:.3f}" if isinstance(x, float) else str(x)


def main():
    global IA_APLICA, IA_BASURA
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(BASE_DIR / "data" / "jobs.db"))
    parser.add_argument("--ia-aplica", type=float, default=70.0)
    parser.add_argument("--ia-basura", type=float, default=50.0)
    args = parser.parse_args()
    IA_APLICA, IA_BASURA = args.ia_aplica, args.ia_basura

    por_modelo = cargar_evals(args.db)
    if not por_modelo:
        print("No hay evaluaciones de muestra en ia_evals "
              "(corre tools/ia_muestra.py --evaluar primero).", file=sys.stderr)
        return 2

    modelos = sorted(por_modelo)
    lineas = [
        "# Calibracion del gate local contra IA externa", "",
        f"- Fecha: `{datetime.now().isoformat(timespec='seconds')}`",
        "- Regla simulada: `(titulo en dominio Y gate >= t) O gate >= escape`",
        f"- Veredictos IA: 'aplica' >= {IA_APLICA:.0f}, 'basura' < {IA_BASURA:.0f}",
        "- Escape vigente 0.45 (inalcanzable con EmbeddingGemma: max no-dominio "
        f"observado 0.36). Propuesto: {ESCAPE_PROPUESTO}.",
        "- Muestra estratificada (tools/ia_muestra.py); las metricas son "
        "DENTRO de la muestra, no de la poblacion total.",
        "",
        "## Correlacion gate <-> IA", "",
        "| Modelo | n | Pearson | Spearman | IA>=70 | IA<50 |",
        "|---|---|---|---|---|---|",
    ]
    for modelo in modelos:
        muestra = por_modelo[modelo]
        gates = [r["gate"] for r in muestra]
        ias = [r["ia"] for r in muestra]
        lineas.append(f"| {modelo} | {len(muestra)} | "
                      f"{_fmt(_pearson(gates, ias) or 0)} | "
                      f"{_fmt(_spearman(gates, ias) or 0)} | "
                      f"{sum(1 for i in ias if i >= IA_APLICA)} | "
                      f"{sum(1 for i in ias if i < IA_BASURA)} |")

    lineas += ["", "## Score IA promedio por grupo", "",
               "| Grupo | " + " | ".join(modelos) + " |",
               "|---" * (len(modelos) + 1) + "|"]
    for grupo in GRUPO_ORDEN:
        fila = [grupo]
        for modelo in modelos:
            vals = [r["ia"] for r in por_modelo[modelo] if r["grupo"] == grupo]
            fila.append(f"{sum(vals)/len(vals):.0f} (n={len(vals)})"
                        if vals else "-")
        lineas.append("| " + " | ".join(fila) + " |")

    lineas += ["", f"## Barrido del umbral (escape propuesto {ESCAPE_PROPUESTO})", ""]
    for modelo in modelos:
        muestra = por_modelo[modelo]
        lineas += [f"**{modelo}**", "",
                   "| t | TP | FP | FN | TN | Precision | Recall | F1 |",
                   "|---|---|---|---|---|---|---|---|"]
        for t in UMBRAL_SWEEP:
            tp, fp, fn, tn, p, r, f1 = simulacion(muestra, t, ESCAPE_PROPUESTO)
            lineas.append(f"| {t:.2f} | {tp} | {fp} | {fn} | {tn} | "
                          f"{p:.2f} | {r:.2f} | {f1:.2f} |")
        lineas.append("")

    lineas += ["", f"## Falsos negativos al umbral {UMBRAL_CANDIDATO} "
               f"(IA>={IA_APLICA:.0f} que el gate rechaza)", ""]
    for modelo in modelos:
        muestra = por_modelo[modelo]
        fns = [r for r in muestra if r["ia"] >= IA_APLICA and not (
            (r["dominio"] and r["gate"] >= UMBRAL_CANDIDATO)
            or r["gate"] >= ESCAPE_PROPUESTO)]
        fns.sort(key=lambda r: -r["ia"])
        lineas.append(f"**{modelo}: {len(fns)}**")
        for r in fns[:12]:
            lineas.append(f"- IA {r['ia']:.0f} · gate {r['gate']:.3f} · "
                          f"[{'D' if r['dominio'] else ' '}] "
                          f"{r['title'][:55]} ({r['company'][:20]})")
        lineas.append("")

    lineas += ["", f"## Falsos positivos al umbral {UMBRAL_CANDIDATO} "
               f"(gate aprueba, IA<{IA_BASURA:.0f})", ""]
    for modelo in modelos:
        muestra = por_modelo[modelo]
        fps = [r for r in muestra if r["ia"] < IA_BASURA and (
            (r["dominio"] and r["gate"] >= UMBRAL_CANDIDATO)
            or r["gate"] >= ESCAPE_PROPUESTO)]
        fps.sort(key=lambda r: r["ia"])
        lineas.append(f"**{modelo}: {len(fps)}**")
        for r in fps[:12]:
            lineas.append(f"- IA {r['ia']:.0f} · gate {r['gate']:.3f} · "
                          f"[{'D' if r['dominio'] else ' '}] "
                          f"{r['title'][:55]} ({r['company'][:20]})")
        lineas.append("")

    if len(modelos) >= 2:
        lineas += ["", "## Acuerdo entre modelos (traslape)", ""]
        sets = {m: {r["job_id"]: r for r in por_modelo[m]} for m in modelos}
        comunes = set(sets[modelos[0]])
        for m in modelos[1:]:
            comunes &= set(sets[m])
        if comunes:
            a = [sets[modelos[0]][j]["ia"] for j in comunes]
            b = [sets[modelos[1]][j]["ia"] for j in comunes]
            difs = [abs(x - y) for x, y in zip(a, b)]
            acuerdos = sum(
                1 for j in comunes
                if (sets[modelos[0]][j]["ia"] >= IA_APLICA)
                == (sets[modelos[1]][j]["ia"] >= IA_APLICA))
            lineas += [
                f"- n comun: {len(comunes)}",
                f"- Pearson IA vs IA: {_fmt(_pearson(a, b) or 0)}",
                f"- |diferencia| media: {sum(difs)/len(difs):.1f} puntos",
                f"- Mismo veredicto (>= {IA_APLICA:.0f}): "
                f"{acuerdos}/{len(comunes)}",
            ]

    ruta = ruta_informe("calidad", "calibracion_gate.md")
    ruta.write_text("\n".join(lineas), encoding="utf-8")
    print(f"Informe: {ruta}")
    for modelo in modelos:
        print(f"  {modelo}: n={len(por_modelo[modelo])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
