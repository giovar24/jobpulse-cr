# -*- coding: utf-8 -*-
"""Muestra estratificada para calibrar el gate local contra una IA externa.

El modo --ia de main.py evalua "lo mas reciente", que no sirve para calibrar:
la senal de calibracion vive en TODA la banda de scores. Esta herramienta
elige una muestra DETERMINISTA que cubre:

  A. aprobadas del gate (tabla jobs, sin duplicados)
  B. banda critica: dominio 0.24-0.30 (las que el umbral actual descarta)
  C. dominio bajo 0.18-0.24 (para saber si ahi ya es basura)
  D. no-dominio alto >=0.26 (las que el escape dejaria pasar)
  E. no-dominio bajo 0.14-0.20 (control: la IA deberia puntuarlas bajo)

Uso:
  python tools/ia_muestra.py                    # dry-run: solo arma la muestra
  python tools/ia_muestra.py --evaluar          # evalua con IA_PROVEEDOR/IA_MODEL
  python tools/ia_muestra.py --evaluar --proveedor nvidia --modelo openai/gpt-oss-20b

Deja:
  - informes/calidad/ia_muestra_<proveedor>_<modelo>.csv (gate vs IA lado a lado)
  - ia_evals (append-only) con las evaluaciones, para --ia-reporte.

Cuota: UNA pasada por invocacion (~len(muestra)/IA_LOTE requests). El dry-run
no toca la API.
"""
import argparse
import asyncio
import io
import json
import sys
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from core.gate import titulo_en_dominio  # noqa: E402
from core.database import JobDatabase  # noqa: E402
from core.ia_api import (DEFAULT_IA_MIN, IAClient, IAError, perfil_hash)  # noqa: E402
from core.profile import CandidateProfile  # noqa: E402
from core.rutas import ruta_informe  # noqa: E402

from dotenv import load_dotenv  # noqa: E402
load_dotenv(BASE_DIR / ".env")

# (grupo, titulo del grupo, cantidad)
GRUPOS = [
    ("A_aprobadas", "aprobadas por el gate", 14),
    ("B_banda_critica", "dominio 0.24-0.30", 14),
    ("C_dominio_bajo", "dominio 0.18-0.24", 6),
    ("D_no_dominio_alto", "no-dominio >=0.26", 6),
    ("E_no_dominio_bajo", "no-dominio 0.14-0.20", 6),
]


def _tomar(lista, n):
    """n elementos espaciados uniformemente en el orden dado (determinista)."""
    if n <= 0 or not lista:
        return []
    if len(lista) <= n:
        return lista
    paso = len(lista) / n
    return [lista[int(i * paso)] for i in range(n)]


def armar_muestra(filas, aprobadas_ids):
    """Devuelve [(grupo, fila)] determinista y sin duplicados de job_id."""
    con_desc = [r for r in filas if (r.get("description") or "").strip()]
    usados, muestra = set(), []

    aprobadas = [r for r in con_desc if r["job_id"] in aprobadas_ids]
    banda = [r for r in con_desc
             if titulo_en_dominio(r["title"])
             and 0.24 <= r["gate_score"] < 0.30]
    dominio_bajo = [r for r in con_desc
                    if titulo_en_dominio(r["title"])
                    and 0.18 <= r["gate_score"] < 0.24]
    no_dom_alto = [r for r in con_desc
                   if not titulo_en_dominio(r["title"])
                   and r["gate_score"] >= 0.26]
    no_dom_bajo = [r for r in con_desc
                   if not titulo_en_dominio(r["title"])
                   and 0.14 <= r["gate_score"] < 0.20]

    for (grupo, _, n), lista in zip(
            GRUPOS, (aprobadas, banda, dominio_bajo, no_dom_alto, no_dom_bajo)):
        lista = sorted(lista, key=lambda r: -r["gate_score"])
        for r in _tomar(lista, n):
            if r["job_id"] in usados:
                continue
            usados.add(r["job_id"])
            muestra.append((grupo, r))
    return muestra


async def main_async(args):
    db = JobDatabase(args.db)
    raw = await db.get_raw_jobs()
    aprobadas_ids = {r["job_id"] for r in await db.get_all_jobs()}
    con_gate = [r for r in raw if r.get("gate_score") is not None
                and not (r.get("dup_of") or "").strip()]
    muestra = armar_muestra(con_gate, aprobadas_ids)

    conteo = {}
    for grupo, r in muestra:
        conteo[grupo] = conteo.get(grupo, 0) + 1
    print(f"Muestra: {len(muestra)} vacantes")
    for (grupo, titulo, _), n in zip(GRUPOS, [conteo.get(g, 0) for g, _, _ in GRUPOS]):
        print(f"  {grupo:20} {n:3d}  ({titulo})")

    if not muestra:
        print("ERROR: muestra vacia", file=sys.stderr)
        return 2
    if not args.evaluar:
        for grupo, r in muestra:
            print(f"  [{grupo}] {r['gate_score']:.3f} {r['title'][:60]:60} "
                  f"{r['company'][:24]}")
        print("\nDry-run: no se llamo a la API. Corre con --evaluar para evaluar.")
        return 0

    profile = CandidateProfile.load()
    client = IAClient({"proveedor": args.proveedor, "modelo": args.modelo})
    if not client.disponible:
        print(f"ERROR: {client.motivo_no_disponible()}", file=sys.stderr)
        return 2
    client.max_vacantes = len(muestra)  # la muestra ES el tope
    p_hash = perfil_hash(profile)
    etiqueta = f"{client.proveedor}_{client.modelo}".replace("/", "_").replace("~", "latest_")
    print(f"\nEvaluando con {client.proveedor}/{client.modelo} "
          f"(lotes de {client.lote}, {client.rpm} rpm)...")

    vacantes = [r for _, r in muestra]
    try:
        resultados, errores = client.puntuar_vacantes(vacantes, profile)
    except IAError as exc:
        print(f"ERROR de API: {exc}", file=sys.stderr)
        return 3
    print(f"  {len(resultados)} evaluadas · {len(errores)} errores · "
          f"{client.resumen_uso()}")

    ts = datetime.now().isoformat(timespec="seconds")
    registros = []
    for grupo, r in muestra:
        res = resultados.get(r["job_id"])
        if not res:
            continue
        registros.append({
            "ts": ts, "tipo": "vacante", "proveedor": client.proveedor,
            "modelo": client.modelo, "perfil_hash": p_hash,
            "job_id": r["job_id"], "job_hash": res.get("hash") or "",
            "score": res.get("score"), "veredicto":
                ("aplica" if float(res.get("score") or 0) >= args.ia_min else "no"),
            "razon": res.get("razon"),
            "payload": json.dumps({
                "grupo": grupo, "puesto": r.get("title"),
                "empresa": r.get("company"), "fuente": r.get("source"),
                "gate_score": r.get("gate_score"),
                "score": res.get("score"), "razon": res.get("razon"),
            }, ensure_ascii=False),
            "tokens_in": 0, "tokens_out": 0,
        })
    if registros:
        await db.registrar_ia(registros)
        print(f"  {len(registros)} evaluaciones guardadas en ia_evals")

    # CSV lado a lado: gate vs IA
    ruta = ruta_informe("calidad", f"ia_muestra_{etiqueta}.csv")
    with open(ruta, "w", encoding="utf-8-sig", newline="") as f:
        f.write("grupo;job_id;gate_score;ia_score;puesto;empresa;razon_ia\n")
        for grupo, r in muestra:
            res = resultados.get(r["job_id"]) or {}
            razon = (res.get("razon") or "").replace(";", ",").replace("\n", " ")
            f.write(f"{grupo};{r['job_id']};{r['gate_score']:.3f};"
                    f"{res.get('score', '')};{(r['title'] or '')[:80]};"
                    f"{(r['company'] or '')[:40]};{razon[:200]}\n")
    print(f"CSV: {ruta}")
    if errores:
        for jid, motivo in list(errores.items())[:5]:
            print(f"  sin evaluar {jid}: {motivo}")
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="Muestra estratificada para calibrar gate vs IA externa")
    parser.add_argument("--db", default=str(BASE_DIR / "data" / "jobs.db"))
    parser.add_argument("--evaluar", action="store_true",
                        help="Llama a la API (sin esto solo imprime la muestra)")
    parser.add_argument("--proveedor", default="",
                        help="Pisa IA_PROVEEDOR (ej. openrouter, nvidia)")
    parser.add_argument("--modelo", default="", help="Pisa IA_MODEL")
    parser.add_argument("--ia-min", type=float, default=DEFAULT_IA_MIN,
                        help="Score IA desde el que una vacante 'aplica'")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
