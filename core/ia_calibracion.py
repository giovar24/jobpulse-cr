"""Calibracion de la IA local con las evaluaciones de la IA fuerte.

Las evaluaciones del LLM (`ia_evals`, append-only) son etiquetas debiles: no
son verdad absoluta, pero sirven para dos cosas concretas y baratas:

  1. Medir si el gate local (embeddings) esta de acuerdo con la IA fuerte:
     correlacion, error medio y matriz de confusion.
  2. Recomendar el umbral `--score-min` que mejor reproduce el veredicto del
     LLM (`IA_MIN`, default 70). Se aplica a mano; nada cambia solo.

Con suficientes etiquetas (cientos), esos mismos pares (embedding, score)
permiten entrenar un reranker local por regresion ridge (distillation). Este
modulo NO entrena: deja el terreno medido y los datos guardados.
"""
from math import sqrt
from typing import Dict, List, Optional


def _pearson(xs: List[float], ys: List[float]) -> Optional[float]:
    n = len(xs)
    if n < 3:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = sqrt(sum((x - mx) ** 2 for x in xs))
    dy = sqrt(sum((y - my) ** 2 for y in ys))
    if dx == 0 or dy == 0:
        return None
    return round(num / (dx * dy), 3)


def _rangos(valores: List[float]) -> List[float]:
    """Rangos 1..n con promedio en empates (para Spearman)."""
    orden = sorted(range(len(valores)), key=lambda i: valores[i])
    out = [0.0] * len(valores)
    i = 0
    while i < len(orden):
        j = i
        while j + 1 < len(orden) and valores[orden[j + 1]] == valores[orden[i]]:
            j += 1
        rango = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            out[orden[k]] = rango
        i = j + 1
    return out


def _spearman(xs: List[float], ys: List[float]) -> Optional[float]:
    if len(xs) < 3:
        return None
    return _pearson(_rangos(xs), _rangos(ys))


def _matriz(pares: List[dict], umbral_local: float, ia_min: float) -> Dict[str, int]:
    tp = fp = fn = tn = 0
    for p in pares:
        ia_ok = p["ia"] >= ia_min
        local_ok = p["gate"] >= umbral_local
        if ia_ok and local_ok:
            tp += 1
        elif local_ok and not ia_ok:
            fp += 1
        elif ia_ok and not local_ok:
            fn += 1
        else:
            tn += 1
    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn}


def _metricas(m: Dict[str, int]) -> Dict[str, float]:
    tp, fp, fn, tn = m["tp"], m["fp"], m["fn"], m["tn"]
    n = tp + fp + fn + tn or 1
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = (2 * precision * recall / (precision + recall)
          if (precision + recall) else 0.0)
    return {
        "precision": round(precision, 3),
        "recall": round(recall, 3),
        "f1": round(f1, 3),
        "exactitud": round((tp + tn) / n, 3),
    }


def calcular(pares: List[dict], ia_min: float = 70.0,
             score_min: float = 0.30) -> Dict:
    """pares = [{"job_id","source","gate","ia","puesto","ia_razon","gate_razon"}]."""
    pares = [p for p in pares
             if p.get("gate") is not None and p.get("ia") is not None]
    rep: Dict = {"n": len(pares), "ia_min": float(ia_min),
                 "score_min": float(score_min)}
    if not pares:
        return rep

    gates = [float(p["gate"]) * 100.0 for p in pares]
    ias = [float(p["ia"]) for p in pares]
    difs = [ia - g for g, ia in zip(gates, ias)]
    rep["correlacion"] = _pearson(gates, ias)
    rep["spearman"] = _spearman(gates, ias)
    rep["mae"] = round(sum(abs(d) for d in difs) / len(difs), 1)
    rep["sesgo"] = round(sum(difs) / len(difs), 1)

    m = _matriz(pares, score_min, ia_min)
    rep["matriz"] = m
    rep.update(_metricas(m))

    # Barrido del umbral local: el que mejor reproduce "aplica / no aplica".
    mejor = None
    if len(pares) >= 8:
        for k in range(10, 81):
            t = k / 100.0
            mt = _metricas(_matriz(pares, t, ia_min))
            cand = {"score_min": round(t, 2), **mt}
            if (mejor is None or (cand["f1"], cand["exactitud"]) >
                    (mejor["f1"], mejor["exactitud"])):
                mejor = cand
    rep["umbral_recomendado"] = mejor

    # Por fuente (solo grupos con muestra suficiente).
    fuentes: Dict[str, List[dict]] = {}
    for p in pares:
        fuentes.setdefault(p.get("source") or "(sin fuente)", []).append(p)
    por_fuente = []
    for fuente, grupo in sorted(fuentes.items(), key=lambda kv: -len(kv[1])):
        if len(grupo) < 3:
            continue
        dif = [float(x["ia"]) - float(x["gate"]) * 100 for x in grupo]
        mg = _matriz(grupo, score_min, ia_min)
        por_fuente.append({
            "fuente": fuente,
            "n": len(grupo),
            "mae": round(sum(abs(d) for d in dif) / len(dif), 1),
            "sesgo": round(sum(dif) / len(dif), 1),
            "acuerdo": _metricas(mg)["exactitud"],
        })
    rep["por_fuente"] = por_fuente

    # Discrepancias mas grandes: lo que hay que mirar para mejorar la IA local.
    orden = sorted(pares, key=lambda p: -abs(float(p["ia"]) - float(p["gate"]) * 100))
    rep["discrepancias"] = [
        {
            "job_id": p.get("job_id"),
            "puesto": (p.get("puesto") or "")[:80],
            "fuente": p.get("source") or "",
            "gate": round(float(p["gate"]) * 100, 1),
            "ia": round(float(p["ia"]), 1),
            "ia_razon": (p.get("ia_razon") or "")[:200],
            "gate_razon": (p.get("gate_razon") or "")[:200],
        }
        for p in orden[:12]
    ]
    return rep


def render_md(rep: Dict, cv_evals: Optional[List[dict]] = None) -> str:
    """Informe legible de la calibracion + ultimas evaluaciones de CV."""
    cv_evals = cv_evals or []
    lineas = ["# Calibración de la IA local con la IA fuerte", ""]
    n = rep.get("n", 0)
    lineas.append(f"- **Vacantes comparadas:** {n}")
    lineas.append(f"- **Umbral IA (aplica):** {rep.get('ia_min', 70):.0f}/100")
    lineas.append(f"- **Umbral gate actual:** {rep.get('score_min', 0.30):.2f}")
    if not n:
        lineas += ["", "Todavía no hay evaluaciones de vacantes con IA. "
                       "Corré `python main.py --ia` primero."]
    else:
        lineas += [
            "",
            "## Acuerdo gate local ↔ IA fuerte",
            "",
            f"- Correlación (Pearson): **{rep.get('correlacion')}**",
            f"- Correlación de rangos (Spearman): **{rep.get('spearman')}**",
            f"- Error medio absoluto (puntos): **{rep.get('mae')}**",
            f"- Sesgo (IA − gate): **{rep.get('sesgo')}** puntos",
            f"- Exactitud con el umbral actual: **{rep.get('exactitud', 0):.1%}**"
            f" · F1 **{rep.get('f1', 0):.2f}**",
            "",
            "| | IA aplica | IA no aplica |",
            "|---|---|---|",
            f"| **Gate aprueba** | {rep['matriz']['tp']} | {rep['matriz']['fp']} |",
            f"| **Gate descarta** | {rep['matriz']['fn']} | {rep['matriz']['tn']} |",
        ]
        u = rep.get("umbral_recomendado")
        if u:
            lineas += [
                "",
                "## Umbral recomendado",
                f"`--score-min {u['score_min']:.2f}` "
                f"(F1 {u['f1']:.2f}, exactitud {u['exactitud']:.1%}). "
                "Es una sugerencia: cambialo en la corrida o en la UI y "
                "después corré `--refiltrar`.",
            ]
        if rep.get("por_fuente"):
            lineas += ["", "## Por fuente", "",
                       "| Fuente | n | MAE | Sesgo | Acuerdo |",
                       "|---|---:|---:|---:|---:|"]
            for f in rep["por_fuente"]:
                lineas.append(f"| {f['fuente']} | {f['n']} | {f['mae']} | "
                              f"{f['sesgo']:+} | {f['acuerdo']:.0%} |")
        if rep.get("discrepancias"):
            lineas += ["", "## Mayores discrepancias (para revisar)", ""]
            for d in rep["discrepancias"][:8]:
                lineas.append(
                    f"- **{d['puesto']}** ({d['fuente']}) — gate {d['gate']:.0f} "
                    f"vs IA {d['ia']:.0f}\n"
                    f"  - IA: {d['ia_razon'] or '(sin razón)'}\n"
                    f"  - Gate: {d['gate_razon'] or '(sin razón)'}")

    lineas += ["", "## Evaluaciones de CV guardadas", ""]
    if not cv_evals:
        lineas.append("Todavía no hay evaluaciones de CV con IA "
                      "(`python main.py --ia-cv \"MiCV.pdf\"`).")
    else:
        for e in cv_evals[:10]:
            lineas.append(f"- {e.get('ts', '')[:16]} · **{float(e.get('score') or 0):.0f}/100**"
                          f" · {e.get('modelo') or ''} · {(e.get('razon') or '')[:140]}")
    return "\n".join(lineas) + "\n"
