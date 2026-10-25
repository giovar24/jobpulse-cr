# -*- coding: utf-8 -*-
"""A/B de modelos IA sobre los mismos jobs: velocidad y calidad de extraccion.

Selecciona jobs con huecos reales (work_mode 'unknown', sin city, sin salary,
sin languages), les corre el MISMO pipeline de ia_enrichment contra cada
servidor y compara: tiempo, tok/s y campos llenados.

Uso: python tools/comparar_ia.py
"""
import asyncio
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

import httpx

# Caché AISLADA para el test: sin esto, Qwen respondería desde la caché
# del pase de ayer (hash idéntico job+modelo) y la comparación sería nula.
os.environ["JOBPULSE_IA_CACHE"] = str(Path(__file__).resolve().parent.parent / "data" / "comparacion_cache")

from jobpulse_v15 import ia_enrichment as iae  # noqa: E402  (después del env)
from jobpulse_v15.db import Database
from jobpulse_v15.ia_api import IAClient
from jobpulse_v15.repository import Repository

RAIZ = Path(__file__).resolve().parent.parent

# (nombre, base_url, modelo) — Qwen de prueba en 8082, Gemma canonical en 8081
MODELOS = [
    ("gemma4-e2b", "http://127.0.0.1:8081", "gemma4-e2b-qat"),
    ("qwen3.5-2b", "http://127.0.0.1:8082", "qwen3.5-2b"),
]

N_POR_GRUPO = 2  # jobs por tipo de hueco
FILTRO_DESC = "LENGTH(COALESCE(description_raw,'')) > 200"  # excluye basura

CONSULTAS = [
    ("work_mode-unknown",
     f"SELECT job_id FROM jobs WHERE work_mode='unknown' AND {FILTRO_DESC} "
     "ORDER BY job_id LIMIT ?"),
    ("sin-city",
     f"SELECT job_id FROM jobs WHERE city IS NULL "
     "AND TRIM(COALESCE(location_raw,''))<>'' "
     f"AND {FILTRO_DESC} ORDER BY job_id LIMIT ?"),
    ("sin-salary",
     f"SELECT job_id FROM jobs WHERE salary_min IS NULL "
     "AND TRIM(COALESCE(salary_raw,''))<>'' "
     f"AND {FILTRO_DESC} ORDER BY job_id LIMIT ?"),
    ("sin-languages",
     f"SELECT job_id FROM jobs WHERE (languages IS NULL OR languages='' "
     f"OR languages='{{}}') AND {FILTRO_DESC} ORDER BY job_id LIMIT ?"),
]


def elegir_jobs() -> list:
    """Jobs con huecos reales, deduplicados, cargados via Repository (igual que el pipeline)."""
    con = sqlite3.connect(RAIZ / "data" / "jobpulse_v15.db")
    ids, grupos = [], {}
    for etiqueta, sql in CONSULTAS:
        filas = [r[0] for r in con.execute(sql, (N_POR_GRUPO,))]
        grupos[etiqueta] = filas
        for jid in filas:
            if jid not in ids:
                ids.append(jid)
    con.close()

    db = Database(str(RAIZ / "data" / "jobpulse_v15.db"))
    db.initialize()
    repo = Repository(db)
    jobs = []
    for jid in ids:
        job = repo.get_job(jid)
        if job:
            job["_grupos"] = [e for e, lst in grupos.items() if jid in lst]
            jobs.append(job)
    return jobs


def metricas(host: str) -> tuple:
    """(tokens_predicted_total, tokens_prompt_total) del /metrics de llama.cpp."""
    txt = httpx.get(f"{host}/metrics", timeout=5).text

    def val(fragmento: str) -> float:
        nums = [float(m.group(1))
                for m in re.finditer(rf"{fragmento}[^\s]*\s+([0-9.eE+\-]+)", txt)]
        return max(nums) if nums else 0.0

    return val("tokens_predicted"), val("tokens_prompt")


def resumen_campos(filled: dict) -> str:
    if not filled:
        return "(nada)"
    partes = []
    for k, v in filled.items():
        if k in ("salary_max", "salary_currency", "salary_period"):
            continue
        if k == "salary_min":
            partes.append(f"salary={v}..{filled.get('salary_max')} "
                          f"{filled.get('salary_currency') or ''}/{filled.get('salary_period') or '?'}")
        elif k == "skills":
            partes.append(f"skills[{len(v)}]={', '.join(v[:4])}")
        elif k == "languages":
            partes.append(f"lang={json.dumps(v, ensure_ascii=False)[:60]}")
        else:
            partes.append(f"{k}={str(v)[:44]}")
    return " | ".join(partes)


async def probar(base_url: str, modelo: str, jobs: list) -> tuple:
    """Corre el MISMO enrich_job_fields del pipeline; devuelve (filas, wall)."""
    cli = IAClient(config={"base_url": f"{base_url}/v1", "modelo": modelo})
    filas, t0 = [], time.perf_counter()
    for job in jobs:
        tj = time.perf_counter()
        try:
            filled = await iae.enrich_job_fields(job, cli) or {}
        except Exception as e:  # server caido, timeout, etc.
            filled = {"__ERROR__": f"{type(e).__name__}: {str(e)[:70]}"}
        filas.append({
            "id": job["job_id"],
            "titulo": (job.get("title") or "")[:46],
            "seg": round(time.perf_counter() - tj, 1),
            "n_campos": len(filled),
            "lleno": filled,
        })
    return filas, time.perf_counter() - t0


async def main() -> None:
    jobs = elegir_jobs()
    print(f"Jobs de prueba: {len(jobs)}\n")
    for j in jobs:
        print(f"[{j['job_id']}] {(j.get('title') or '')[:60]}")
        print(f"    empresa={j.get('company')} | grupos={j['_grupos']}")
        print(f"    location_raw={j.get('location_raw')!r}")
        print(f"    salary_raw={(j.get('salary_raw') or '')[:60]!r} "
              f"| work_mode={j.get('work_mode')!r} | desc={len(j.get('description_raw') or '')} chars")

    resultados = {}
    for nombre, base_url, modelo in MODELOS:
        # preflight
        try:
            cli = IAClient(config={"base_url": f"{base_url}/v1", "modelo": modelo})
            err = await iae.verificar_servidor_ia(cli)
            if err:
                print(f"\n*** {nombre}: server no responde — se omite ({err[:90]})")
                resultados[nombre] = None
                continue
        except Exception as e:
            print(f"\n*** {nombre}: cliente no configuro — se omite ({e})")
            resultados[nombre] = None
            continue

        print(f"\n=== Probando {nombre} ({modelo}) en {base_url} ===")
        p0, pr0 = metricas(base_url)
        filas, wall = await probar(base_url, modelo, jobs)
        p1, pr1 = metricas(base_url)
        tok_pred = p1 - p0
        resultados[nombre] = {
            "filas": filas, "wall": wall, "tok_pred": tok_pred,
            "tok_prompt": pr1 - pr0,
            "tok_s": tok_pred / wall if wall > 0 else 0.0,
            "campos": sum(f["n_campos"] for f in filas),
        }
        print(f"    wall={wall:.1f}s | tokens generados={tok_pred:.0f} "
              f"({resultados[nombre]['tok_s']:.1f} tok/s) "
              f"| tokens prompt={pr1-pr0:.0f} | campos llenados={resultados[nombre]['campos']}")

    # comparacion lado a lado
    validos = {k: v for k, v in resultados.items() if v}
    if len(validos) >= 1:
        print("\n" + "=" * 100)
        print("COMPARACION POR JOB")
        print("=" * 100)
        nombres = list(validos.keys())
        for i, fila0 in enumerate(validos[nombres[0]]["filas"]):
            print(f"\n[{fila0['id']}] {fila0['titulo']}")
            for nombre in nombres:
                f = validos[nombre]["filas"][i]
                print(f"  {nombre:<12} {f['seg']:>5}s  {resumen_campos(f['lleno'])}")

        print("\n" + "=" * 100)
        print("RESUMEN")
        print("=" * 100)
        for nombre, r in validos.items():
            print(f"  {nombre:<12} wall={r['wall']:>6.1f}s  tok/s={r['tok_s']:>6.1f}  "
                  f"campos={r['campos']:>3}  jobs_ok={sum(1 for f in r['filas'] if '__ERROR__' not in f['lleno'])}/{len(jobs)}")
        if len(validos) == 2:
            n0, n1 = list(validos.keys())
            v0, v1 = validos[n0], validos[n1]
            rapido = n0 if v0["wall"] < v1["wall"] else n1
            fructifero = n0 if v0["campos"] >= v1["campos"] else n1
            print(f"\n  MAS RAPIDO: {rapido} ({min(v0['wall'], v1['wall']):.1f}s vs {max(v0['wall'], v1['wall']):.1f}s)")
            print(f"  MAS CAMPOS LLENADOS: {fructifero} "
                  f"({max(v0['campos'], v1['campos'])} vs {min(v0['campos'], v1['campos'])})")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    asyncio.run(main())
