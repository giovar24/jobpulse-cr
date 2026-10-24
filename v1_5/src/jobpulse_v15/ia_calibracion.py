# -*- coding: utf-8 -*-
"""jobpulse_v15.ia_calibracion — Calibracion del gate local contra IA externa.

Compara gate_score (embeddings locales) vs score IA fuerte (Gemini/NVIDIA/etc.)
y sugiere --score-min optimo.

Metricas:
- Pearson / Spearman correlation
- MAE (Mean Absolute Error)
- Matriz de confusion (umbral IA vs umbral gate)
- Barrido de umbral gate (0.10-0.80) -> exactitud, precision, recall, F1
- Discrepancias mayores (top-N)
"""

from __future__ import annotations

import json
import logging
import statistics
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from scipy import stats

logger = logging.getLogger("jobpulse.ia_cal")


@dataclass
class CalibrationReport:
    """Informe completo de calibracion."""
    n: int                                    # num pares gate/IA
    correlacion: Optional[Dict[str, float]]   # pearson, spearman, p-values
    mae: float                                # Mean Absolute Error
    sesgo: float                              # media(IA - gate) en puntos
    matriz: Dict[str, int]                    # TP/FP/TN/FN con umbral IA=70
    umbral_barrido: List[Dict]                # resultados por umbral gate
    umbral_recomendado: Optional[Dict]        # mejor F1
    discrepancias: List[Dict]                 # top discrepancias
    generado: str                             # timestamp


def calcular(
    pares: List[Dict[str, Any]],
    ia_min: float = 70.0,
    score_min: float = 0.28,
) -> Dict[str, Any]:
    """
    Calcula metricas de calibracion.

    pares: lista de dicts con keys:
        - job_id: str
        - gate: float (0-1, score local)
        - ia: float (0-100, score IA)
        - source: str (opcional)
        - puesto: str (opcional)
        - ia_razon: str (opcional)
        - gate_razon: str (opcional)

    ia_min: umbral IA (0-100) para considerar "aplica"
    score_min: umbral gate actual (0-1)
    """
    if not pares:
        return {"n": 0, "error": "sin pares"}

    gate_scores = np.array([p["gate"] for p in pares])
    ia_scores = np.array([p["ia"] for p in pares])

    # Correlaciones
    pearson_r, pearson_p = stats.pearsonr(gate_scores, ia_scores)
    spearman_r, spearman_p = stats.spearmanr(gate_scores, ia_scores)

    # MAE y sesgo
    diff = ia_scores - (gate_scores * 100)  # IA 0-100 vs gate 0-1 -> *100
    mae = float(np.mean(np.abs(diff)))
    sesgo = float(np.mean(diff))

    # Matriz de confusion con umbral actual
    gate_pred = gate_scores >= score_min
    ia_true = ia_scores >= ia_min
    tp = int(np.sum(gate_pred & ia_true))
    fp = int(np.sum(gate_pred & ~ia_true))
    tn = int(np.sum(~gate_pred & ~ia_true))
    fn = int(np.sum(~gate_pred & ia_true))

    # Barrido de umbral gate (0.10 a 0.80 step 0.01)
    umbrales = np.arange(0.10, 0.81, 0.01)
    barrido = []
    for u in umbrales:
        pred = gate_scores >= u
        tp_u = int(np.sum(pred & ia_true))
        fp_u = int(np.sum(pred & ~ia_true))
        tn_u = int(np.sum(~pred & ~ia_true))
        fn_u = int(np.sum(~pred & ia_true))

        precision = tp_u / (tp_u + fp_u) if (tp_u + fp_u) > 0 else 0.0
        recall = tp_u / (tp_u + fn_u) if (tp_u + fn_u) > 0 else 0.0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0
        exactitud = (tp_u + tn_u) / len(pares)

        barrido.append({
            "score_min": round(float(u), 2),
            "tp": tp_u, "fp": fp_u, "tn": tn_u, "fn": fn_u,
            "precision": round(precision, 3),
            "recall": round(recall, 3),
            "f1": round(f1, 3),
            "exactitud": round(exactitud, 3),
        })

    # Mejor F1
    mejor = max(barrido, key=lambda x: x["f1"])

    # Discrepancias (top 10 por |diff|)
    discrepancias = []
    for p in pares:
        d = p["ia"] - (p["gate"] * 100)
        discrepancias.append({
            "job_id": p["job_id"],
            "source": p.get("source", ""),
            "puesto": p.get("puesto", "")[:80],
            "gate": round(p["gate"], 3),
            "ia": round(p["ia"], 1),
            "diff": round(d, 1),
            "gate_razon": p.get("gate_razon", ""),
            "ia_razon": p.get("ia_razon", "")[:120],
        })
    discrepancias.sort(key=lambda x: -abs(x["diff"]))

    return {
        "n": len(pares),
        "correlacion": {
            "pearson": round(pearson_r, 3),
            "pearson_p": round(pearson_p, 4),
            "spearman": round(spearman_r, 3),
            "spearman_p": round(spearman_p, 4),
        },
        "mae": round(mae, 2),
        "sesgo": round(sesgo, 2),
        "matriz": {"tp": tp, "fp": fp, "tn": tn, "fn": fn},
        "umbral_barrido": barrido,
        "umbral_recomendado": {
            "score_min": mejor["score_min"],
            "f1": mejor["f1"],
            "exactitud": mejor["exactitud"],
            "precision": mejor["precision"],
            "recall": mejor["recall"],
        },
        "discrepancias": discrepancias[:10],
    }


def render_md(rep: Dict[str, Any], cv_evals: List[Dict] = None) -> str:
    """Genera reporte Markdown legible."""
    if rep.get("n", 0) == 0:
        return "# Calibracion IA\n\nSin pares para calibrar."

    lines = []
    lines.append("# Calibracion Gate Local vs IA Externa")
    lines.append(f"\n**Generado:** {datetime.now().isoformat(timespec='seconds')}")
    lines.append(f"**Pares evaluados:** {rep['n']}")
    lines.append("")

    # Correlacion
    c = rep["correlacion"]
    lines.append("## Correlacion Gate vs IA")
    lines.append(f"- Pearson r = {c['pearson']} (p={c['pearson_p']})")
    lines.append(f"- Spearman rho = {c['spearman']} (p={c['spearman_p']})")
    lines.append(f"- MAE = {rep['mae']} puntos (IA 0-100 vs Gate*100)")
    lines.append(f"- Sesgo medio (IA - Gate*100) = {rep['sesgo']:.1f} pts")
    lines.append("")

    # Matriz
    m = rep["matriz"]
    lines.append("## Matriz de Confusion (Gate >= score_min vs IA >= 70)")
    lines.append(f"| | IA Aplica | IA No Aplica |")
    lines.append(f"|---|---|---|")
    lines.append(f"| **Gate Aprueba** | {m['tp']} | {m['fp']} |")
    lines.append(f"| **Gate Rechaza** | {m['fn']} | {m['tn']} |")
    lines.append("")

    # Umbral recomendado
    u = rep["umbral_recomendado"]
    lines.append("## Umbral Sugerido (--score-min)")
    lines.append(f"- **score_min = {u['score_min']:.2f}**")
    lines.append(f"- F1 = {u['f1']:.3f}, Exactitud = {u['exactitud']:.1%}")
    lines.append(f"- Precision = {u['precision']:.3f}, Recall = {u['recall']:.3f}")
    lines.append("")

    # Top discrepancias
    lines.append("## Top Discrepancias (|IA - Gate*100| mayor)")
    lines.append("| Job ID | Fuente | Puesto | Gate | IA | Diff | Gate Razon | IA Razon |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for d in rep["discrepancias"][:10]:
        lines.append(f"| {d['job_id']} | {d['source']} | {d['puesto']} | {d['gate']} | {d['ia']} | {d['diff']:+.1f} | {d['gate_razon'][:40]} | {d['ia_razon'][:60]} |")
    lines.append("")

    # CV evals si hay
    if cv_evals:
        lines.append("## Evaluaciones de CV (IA)")
        for cv in cv_evals:
            ev = cv.get("evaluacion", {})
            lines.append(f"- **{cv.get('archivo', 'CV')}**: {ev.get('puntaje', '?')}/100 — {ev.get('resumen', 'Sin resumen')[:100]}")

    return "\n".join(lines)


def calibrar_desde_db(
    db,
    profile: Dict[str, Any],
    engine,
    ia_min: float = 70.0,
    score_min: float = 0.28,
    dias: int = 30,
) -> Dict[str, Any]:
    """
    Flujo completo: obtiene evaluaciones IA de la DB, cruza con gate_score,
    genera reporte y sugiere --score-min.
    """
    from jobpulse_v15.ia_api import IAClient

    # Obtener evaluaciones IA (ultima por job_id)
    evals_ia = db.get_ia_evals()
    ultima_ia = {}
    for e in evals_ia:
        if e.get("tipo") == "vacante" and e.get("job_id"):
            ultima_ia[e["job_id"]] = e

    # Obtener crudo con gate_score
    raw_jobs = db.get_raw_jobs()
    raw_dict = {r.get("job_id"): r for r in raw_jobs}

    pares = []
    for jid, e in ultima_ia.items():
        r = raw_dict.get(jid)
        if not r or r.get("gate_score") is None:
            continue
        pares.append({
            "job_id": jid,
            "source": r.get("source"),
            "puesto": r.get("title"),
            "gate": float(r["gate_score"]),
            "ia": float(e.get("score") or 0),
            "ia_razon": e.get("razon"),
            "gate_razon": r.get("gate_razon"),
        })

    if not pares:
        return {"n": 0, "error": "No hay vacantes con ambas evaluaciones"}

    rep = calcular(pares, ia_min=ia_min, score_min=score_min)
    rep["generado"] = datetime.now().isoformat(timespec="seconds")
    return rep