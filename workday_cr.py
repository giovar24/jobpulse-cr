"""Sondea la API CXS de todos los tableros Workday conocidos buscando CR.

Entrada:  data/empresas/workday_dir.csv  (directorio publico de tableros
validados, columnas platform/token/company/board_url/api_url/open_roles).
Salida:   data/empresas/workday_cr.json (tenants con vacantes en Costa Rica)
          data/empresas/workday_fallos.json (para reintentar luego)

Metodo: POST {api_url} con {"appliedFacets": {}, "limit": 20, "offset": 0,
"searchText": "Costa Rica"}. El searchText busca en titulos/ubicaciones;
aqui es un SCANROSS cortesia: se piden 20 y se exige que locationsText del
resultado incluya "Costa Rica" (evita falsos positivos de textos que solo
MENCIONAN el pais). Vacantes sin location CR se ignoran.

Uso:
    python workday_cr.py            # sondea todo el directorio
    python workday_cr.py --solo-fallos
"""
import argparse
import csv
import json
import re
import sys
import threading
import time
from pathlib import Path

import httpx

BASE_DIR = Path(__file__).resolve().parent
DIR = BASE_DIR / "data" / "empresas"
DIR_CSV = DIR / "workday_dir.csv"
OUT_HITS = DIR / "workday_cr.json"
OUT_FAIL = DIR / "workday_fallos.json"

CUESTIONARIO = re.compile(r"costa\s+rica", re.I)

HEADERS = {"User-Agent": "JobPulseCR/1.0 (local; personal)",
           "Content-Type": "application/json"}


def sondear(client, fila: dict) -> dict:
    url = fila["api_url"]
    body = {"appliedFacets": {}, "limit": 20, "offset": 0,
            "searchText": "Costa Rica"}
    errores = []
    for intento in range(2):
        try:
            r = client.post(url, json=body, timeout=20.0)
            if not (200 <= r.status_code < 300):
                return {"fila": fila, "estado": f"HTTP {r.status_code}"}
            data = r.json()
            postings = data.get("jobPostings") or []
            con_cr = [p for p in postings
                      if CUESTIONARIO.search((p.get("locationsText") or "")
                                             + " " + (p.get("title") or ""))]
            return {
                "fila": fila,
                "estado": "ok",
                "total_cr": len(con_cr),
                "vacantes": [
                    {"titulo": p.get("title"), "ubicacion": p.get("locationsText"),
                     "postedOn": p.get("postedOn"), "path": p.get("externalPath")}
                    for p in con_cr[:5]
                ],
            }
        except Exception as e:
            errores.append(str(e)[:120])
            time.sleep(0.5 + intento)
    return {"fila": fila, "estado": "error: " + "; ".join(errores)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--max-filas", type=int, default=0, help="0 = todas")
    args = ap.parse_args()

    with open(DIR_CSV, encoding="utf-8-sig") as f:
        filas = list(csv.DictReader(f))
    if args.max_filas:
        filas = filas[: args.max_filas]
    print(f"Tableros a sondear: {len(filas)}", flush=True)

    hits, fallos = [], []
    cand = httpx.Client(headers=HEADERS, http2=True,
                        limits=httpx.Limits(max_connections=args.workers))
    lock_t, lock_p, lock_d = threading.Lock(), threading.Lock(), threading.Lock()
    n = [0]

    def trabajo(fila):
        res = sondear(cand, fila)
        with lock_p:
            if res["estado"] == "ok":
                if res["total_cr"]:
                    print(f"CR HIT  {res['total_cr']:>3}  {res['fila']['company']} "
                          f"({res['fila']['token']})", flush=True)
                    with lock_d:
                        hits.append(res)
                else:
                    pass  # 0 hits: normal (vacia o sin CR)
            else:
                with lock_d:
                    fallos.append(res)
        with lock_t:
            n[0] += 1
            if n[0] % 100 == 0:
                print(f"... {n[0]}/{len(filas)} sondeados "
                      f"({len(hits)} con CR, {len(fallos)} fallos)", flush=True)

    threads = [threading.Thread(target=trabajo, args=(f,)) for f in filas]
    for t in threads:
        t.start()
        time.sleep(0.05)
    for t in threads:
        t.join()

    OUT_HITS.write_text(
        json.dumps([{"company": h["fila"]["company"], "token": h["fila"]["token"],
                     "board_url": h["fila"]["board_url"], "api_url": h["fila"]["api_url"],
                     "total_cr": h["total_cr"], "vacantes": h["vacantes"]}
                    for h in sorted(hits, key=lambda x: -x["total_cr"])],
                   ensure_ascii=False, indent=1), encoding="utf-8")
    OUT_FAIL.write_text(
        json.dumps([{"company": f["fila"]["company"], "token": f["fila"]["token"],
                     "api_url": f["fila"]["api_url"], "estado": f["estado"]}
                    for f in fallos], ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Hecho: {len(hits)} empresas con vacantes CR, {len(fallos)} fallos.",
          flush=True)


if __name__ == "__main__":
    main()
