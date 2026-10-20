"""Sondea los ATS no-Workday del directorio (12,235 tableros, 8 plataformas).

Salida:  data/empresas/otros_ats_cr.json
         data/empresas/otros_ats_fallos.json
Cada hit = una empresa (por token de tablero) con >=1 vacante cuya ubicacion
conene "Costa Rica". Se mina solo campos publicos (titulo/ubicacion/link), sin
login ni nada autenticado.

Uso:
    python barrido_ats_otros.py --muestra     # 4 tableros por plataforma
    python barrido_ats_otros.py               # todo menos workday
    python barrido_ats_otros.py --plataforma greenhouse,lever
"""
import argparse
import csv
import json
import re
import threading
import time
from pathlib import Path

import httpx

BASE_DIR = Path(__file__).resolve().parent
DIR = BASE_DIR / "data" / "empresas"
DIR_CSV = DIR / "ats_dir.csv"
OUT_HITS = DIR / "otros_ats_cr.json"
OUT_FAIL = DIR / "otros_ats_fallos.json"

HEADERS = {"User-Agent": "JobPulseCR/1.0 (local; personal)"}
CR_RE = re.compile(r"costa\s+rica", re.I)


# ------------------------------------------------- extraccion por plataforma
def _rec_loc(obj: str) -> str:
    """Recorre el dict/lista y junta las variantes con 'location'."""
    out = []
    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if isinstance(v, (dict, list)):
                    walk(v)
                elif isinstance(v, str) and ("location" in k.lower()
                                             or k.lower() in ("country", "region", "city", "locationtext")
                                             or k.lower() in ("countryname", "locationname")):
                    out.append(v)
                elif k.lower() == "title" and isinstance(v, str):
                    out.append(v)
    walk(obj)
    return " ".join(out)


def _extract(platform: str, data) -> list:
    """Devuelve [{titulo, ubicacion, url}] de las vacantes cuyos campos ubican CR."""
    if isinstance(data, dict):
        items = data.get("jobs") or data.get("postings") \
            or data.get("offers") or []
    elif isinstance(data, list):
        items = data
    else:
        items = []
    vacas = []
    for it in items:
        if not isinstance(it, dict):
            continue
        texto = _rec_loc(it)
        if CR_RE.search(texto):
            url = it.get("absolute_url") or (it.get("job") or {}).get("url") \
                or it.get("publicUrl") or it.get("url") \
                or it.get("applyUrl") \
                or it.get("enterpriseURL") or it.get("link") or ""
            vacas.append({"titulo": it.get("title") or it.get("text")
                          or it.get("name"),
                          "ubicacion": texto[:120], "url": url,
                          "id": str(it.get("id") or "")})
    return vacas


def sondear(client, fila: dict) -> dict:
    try:
        r = client.get(fila["api_url"], timeout=25.0)
    except Exception as e:
        return {"fila": fila, "estado": "error: " + str(e)[:120]}
    if not (200 <= r.status_code < 300):
        return {"fila": fila, "estado": f"HTTP {r.status_code}"}
    try:
        data = r.json()
    except ValueError:
        if fila["platform"] == "personio" and "<workzag-jobs>" in r.text[:400]:
            # personio sirve XML "workzag-jobs": <position> con <name>/<office>
            vacas = []
            for m in re.finditer(r"<position>(.*?)</position>", r.text, re.S):
                bloq = m.group(1)
                t = re.search(r"<name[^>]*>(.*?)</name>", bloq)
                office = re.search(r"<office[^>]*>(.*?)</office>", bloq)
                texto = (office.group(1) if office else "") + " " + (t.group(1) if t else "")
                if CR_RE.search(texto):
                    vacas.append({"titulo": t.group(1) if t else "",
                                  "ubicacion": (office.group(1) if office else "")})
            return {"fila": fila, "estado": "ok", "total_cr": len(vacas),
                    "vacantes": vacas[:5]}
        return {"fila": fila, "estado": "no-JSON"}
    vacas = _extract(fila["platform"], data)
    return {"fila": fila, "estado": "ok",
            "total_cr": len(vacas),
            "vacantes": vacas[:5]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=14)
    ap.add_argument("--muestra", action="store_true")
    ap.add_argument("--plataforma", default="")
    args = ap.parse_args()

    with open(DIR_CSV, encoding="utf-8-sig") as f:
        filas = list(csv.DictReader(f))
    filas = [x for x in filas if x["platform"] != "workday"]
    if args.plataforma:
        sel = {p.strip() for p in args.plataforma.split(",")}
        filas = [x for x in filas if x["platform"] in sel]
    if args.muestra:
        sub, conteo = [], {}
        for x in filas:
            if conteo.get(x["platform"], 0) < 4:
                sub.append(x)
                conteo[x["platform"]] = conteo.get(x["platform"], 0) + 1
        filas = sub

    print(f"Tableros a sondear: {len(filas)}", flush=True)

    hits, fallos = [], []
    lock_n, lock_d = threading.Lock(), threading.Lock()
    n = [0]
    cand = httpx.Client(headers=HEADERS, http2=False,
                        limits=httpx.Limits(max_connections=args.workers))

    def trabajo(fila):
        res = sondear(cand, fila)
        with lock_d:
            if res["estado"] == "ok":
                if res["total_cr"]:
                    print(f"CR HIT {res['total_cr']:>3}  {res['fila']['company']} "
                          f"({res['fila']['platform']}::{res['fila']['token'][:40]})",
                          flush=True)
                    hits.append(res)
            else:
                fallos.append(res)
        with lock_n:
            n[0] += 1
            if n[0] % 150 == 0:
                print(f"... {n[0]}/{len(filas)} ({len(hits)} con CR, "
                      f"{len(fallos)} fallos)", flush=True)

    threads = [threading.Thread(target=trabajo, args=(f,)) for f in filas]
    for t in threads:
        t.start()
        time.sleep(0.04)
    for t in threads:
        t.join()

    OUT_HITS.write_text(
        json.dumps([{"platform": h["fila"]["platform"],
                     "company": h["fila"]["company"], "token": h["fila"]["token"],
                     "board_url": h["fila"]["board_url"], "api_url": h["fila"]["api_url"],
                     "total_cr": h["total_cr"], "vacantes": h["vacantes"]}
                    for h in sorted(hits, key=lambda x: (x["fila"]["platform"], -x["total_cr"]))],
                   ensure_ascii=False, indent=1), encoding="utf-8")
    OUT_FAIL.write_text(
        json.dumps([{"platform": f["fila"]["platform"],
                     "company": f["fila"]["company"], "token": f["fila"]["token"],
                     "api_url": f["fila"]["api_url"], "estado": f["estado"]}
                    for f in fallos], ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Hecho: {len(hits)} tableros con CR, {len(fallos)} fallos.", flush=True)


if __name__ == "__main__":
    main()
