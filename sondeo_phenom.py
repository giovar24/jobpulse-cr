# -*- coding: utf-8 -*-
"""Sondea los sitios Phenom confirmados con keywords 'Costa Rica' via SSR.

Salida: data/empresas/phenom_cr.json  (+ log de fallos)
"""
import json
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
from phenom import PhenomSite, tiene_cr

OUT = BASE / "data" / "empresas" / "phenom_cr.json"

SITIOS = [
    ("P&G", "https://www.pgcareers.com"),
    ("Thermo Fisher Scientific", "https://jobs.thermofisher.com"),
    ("HPE", "https://careers.hpe.com"),
    ("Cisco", "https://jobs.cisco.com"),
    ("Fiserv", "https://careers.fiserv.com"),
    ("UPS", "https://www.jobs-ups.com"),
    ("Snowflake", "https://careers.snowflake.com"),
    ("Zimmer Biomet", "https://careers.zimmerbiomet.com"),
]

salida = []
for nombre, base in SITIOS:
    print("==", nombre, base, flush=True)
    try:
        ph = PhenomSite(base)
        cfg = ph.descubrir()
        print(f"   descubrir: {cfg}", flush=True)
        if not cfg["ok"]:
            salida.append({"empresa": nombre, "base": base,
                           "estado": "sin-locale-path SSR"})
            continue
        jobs, total = ph.buscar("Costa Rica", max_vacantes=80)
        con_cr = [j for j in jobs if tiene_cr(j)]
        print(f"   jobs keyword-CR: {len(jobs)}  total SSR: {total}  con CR en ubicacion: {len(con_cr)}", flush=True)
        for j in con_cr[:6]:
            print(f"      - {j['titulo']} | {j['ubicaciones'][:2]}", flush=True)
        salida.append({"empresa": nombre, "base": base, "refNum": cfg["refNum"],
                       "locale_path": cfg["locale_path"],
                       "total_keyword_cr": total, "paginadas": len(jobs),
                       "total_cr_ubicacion": len(con_cr),
                       "vacantes": con_cr if con_cr else jobs[:6],
                       "muestra_no_cr": [] if con_cr else jobs[:4]})
    except Exception as e:
        print("   ERROR:", str(e)[:120], flush=True)
        salida.append({"empresa": nombre, "base": base, "estado": "error: " + str(e)[:150]})
    time.sleep(0.8)

OUT.write_text(json.dumps(salida, ensure_ascii=False, indent=1), encoding="utf-8")
print("listo ->", OUT, flush=True)
