# -*- coding: utf-8 -*-
"""Caza de ATS (Phenom/iCIMS/Taleo/SuccessFactors/SmartRecruiters) en paginas de
carreras de empresas grandes con presencia en CR.

Salida: data/empresas/ats_grandes_huellas.json
Firmas por ATS (marcadores en el HTML):
  phenom: CareerConnectResources / phenompeople / "csrfToken"
  icims: "icims" (dominios x.jobs / c/jobs o query ?icims)
  successfactors: "successfactors" / /careersection
  taleo: "taleo" / .ftl
  smartrecruiters: "smartrecruiters"
Uso:
    python caza_ats_grandes.py --solo empresa
"""
import argparse
import json
import re
import threading
import time
from pathlib import Path

import httpx

BASE = Path(__file__).resolve().parent
OUT = BASE / "data" / "empresas" / "ats_grandes_huellas.json"

H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124",
     "Accept-Language": "en-US,en"}

# empresa -> urls candidatas de paginas de carreras (exploradas en orden)
# ronda 2: se agregan paths search-results (patron Phenom SSR), /c/en (iCIMS)
# y endpoints myworkday directos para las que en ronda 1 dieron 403/404 o
# 200-sin-firma (SPA que carga el listado por JS).
CANDIDATOS = {
    "Amazon AWS CR": ["https://www.amazon.jobs/en/"],
    "Intel CR": ["https://jobs.intel.com/us/en/search-results", "https://jobs.intel.com/"],
    "Cisco CR": ["https://jobs.cisco.com/us/en", "https://careers.cisco.com/"],
    "Dell Technologies CR": ["https://jobs.dell.com/us/en/search-results",
                             "https://search-jobs.dell.com/", "https://jobs.dell.com/"],
    "Western Digital CR": ["https://careers.wdc.com/", "https://jobs.westerndigital.com/",
                           "https://www.westerndigital.com/company/careers"],
    "Concentrix CR": ["https://jobs.concentrixcareers.com/c/en/search-results",
                      "https://jobs.concentrixcareers.com/",
                      "https://www.concentrix.com/careers/"],
    "Sutherland CR": ["https://careers.sutherlandglobal.com/en/sites/sutherlandglobalcareers/jobs",
                      "https://careers.sutherlandglobal.com/"],
    "Foundever (Sykes) CR": ["https://jobs.foundever.com/", "https://www.foundever.com/careers/",
                             "https://careers.foundever.com/"],
    "Bosch CR": ["https://jobs.bosch.com/", "https://www.bosch.com/careers/"],
    "Autodesk CR": ["https://www.autodesk.com/careers",
                    "https://marlin.wd1.myworkdayjobs.com/Autodesk_Careers",
                    "https://careers.autodesk.com/"],
    "Rockwell Automation CR": ["https://careers.rockwellautomation.com/",
                               "https://www.rockwellautomation.com/en-us/company/careers.html"],
    "Broadcom CR": ["https://broadcom.wd1.myworkdayjobs.com/Broadcom",
                    "https://careers.broadcom.com/", "https://www.broadcom.com/company/careers"],
    "Fiserv CR": ["https://careers.fiserv.com/", "https://www.fiserv.com/careers.html"],
    "PayPal CR": ["https://www.paypal.com/careers", "https://jobs.paypal.com/",
                  "https://paypal.wd5.myworkdayjobs.com/htdocs/jobs/paypal/"],
    "Oracle CR": ["https://careers.oracle.com/jobs/", "https://www.oracle.com/careers/"],
    "Microsoft CR": ["https://jobs.careers.microsoft.com/global/en/search?q=Costa%20Rica",
                     "https://careers.microsoft.com/us/en/search-results"],
    "UPS CR": ["https://www.jobs-ups.com/", "https://jobs-upsjobs.com/"],
    "Kimberly-Clark CR": ["https://jobs.kimberly-clark.com/us/en/search-results",
                          "https://jobs.kimberly-clark.com/"],
    "Becton Dickinson CR": ["https://jobs.bd.com/", "https://careers.bd.com/"],
    "Medtronic CR": ["https://jobs.medtronic.com/", "https://medtronic.wd5.myworkdayjobs.com/"],
    "Nestle CR": ["https://jobs.nestle.com/jobs", "https://www.nestle.com/jobs"],
    "Johnson & Johnson CR": ["https://www.jobs.jnj.com/", "https://www.jnj.com/careers"],
    "Deloitte CR": ["https://apply.deloitte.com/careers", "https://jobs.deloitte.com/"],
    "EY CR": ["https://careers.ey.com/", "https://www.ey.com/en_gl/careers"],
    "KPMG CR": ["https://careers.kpmg.com/", "https://jobs.kpmg.com/"],
    "Teleperformance CR": ["https://jobs.teleperformance.com/global/en/search-results",
                           "https://jobs.teleperformance.com/",
                           "https://www.teleperformance.com/en/careers/"],
    "Alorica CR": ["https://careers.alorica.com/jobs", "https://jobs.alorica.com/"],
    "Atento CR": ["https://atento.wd1.myworkdayjobs.com/", "https://www.atento.com/careers/"],
    "Salesforce CR": ["https://career-site-aggregator.salesforce.wd1.myworkdayjobs.com/External_Career_Site",
                      "https://careers.salesforce.com/"],
    "Snowflake CR": ["https://careers.snowflake.com/", "https://snowflake.wd1.myworkdayjobs.com/jobs"],
    "DocuSign CR": ["https://careers.docusign.com/", "https://docusign.wd1.myworkdayjobs.com/DocuSign"],
    "NCR Voyix CR": ["https://careers.ncrvoyix.com/", "https://careers.ncr.com/"],
    "IBM CR": ["https://www.ibm.com/careers/search", "https://careers.ibm.com/"],
    "Coca-Cola FEMSA CR": ["https://www.coca-colafemsa.com/careers/", "https://empleos.coca-colafemsa.com/"],
    "Grupo Bimbo CR": ["https://jobs.grupobimbo.com/", "https://www.grupobimbo.com/careers"],
    "Koch Industries CR": ["https://careers.kochinc.com/", "https://www.kochcareers.com/searchjobs"],
    "Kuehne+Nagel CR": ["https://careers.kuehne-nagel.com/", "https://jobsearch.kuehne-nagel.com/"],
    "DHL CR": ["https://careers.dhl.com/global/en/search-results", "https://jobs.dhl.com/"],
    "Zimmer Biomet CR": ["https://careers.zimmerbiomet.com/", "https://jobs.zimmerbiomet.com/"],
    "Instacart CR": ["https://careers.instacart.com/", "https://instacart.wd1.myworkdayjobs.com/Instacart"],
    "Walmart CR": ["https://careers.walmart.com/", "https://carreras.walmart.com.gt/"],
    "Clear River/NCR CR": ["https://www.clearsource.com/careers/", "https://careers.clearsource.com/"],
    "P&G": ["https://www.pgcareers.com/"],
    "Thermo Fisher Scientific": ["https://jobs.thermofisher.com/"],
    "HPE": ["https://careers.hpe.com/"],
}

FIRMA = [
    ("phenom", re.compile(r"phenompeople|CareerConnectResources|csrfToken|everbe", re.I)),
    ("icims", re.compile(r"icims\.com|\bicims\b|/c/jobs(?:/|$)", re.I)),
    ("successfactors", re.compile(r"successfactors|career-section|sap\.com/", re.I)),
    ("taleo", re.compile(r"taleo|\.ftl\b", re.I)),
    ("smartrecruiters", re.compile(r"smartrecruiters", re.I)),
    ("workday", re.compile(r"myworkdayjobs\.com|wd\d\.myworkday", re.I)),
    ("greenhouse", re.compile(r"greenhouse\.io|boards-greenhouse", re.I)),
    ("lever", re.compile(r"api\.lever\.co|lever\.co", re.I)),
    ("workable", re.compile(r"workable\.com", re.I)),
]

cand = httpx.Client(headers=H, follow_redirects=True, timeout=25.0)
lock_p, lock_s = threading.Lock(), threading.Lock()
res: list = []
n = [0]


def sondear(empresa, urls):
    for u in urls:
        try:
            r = cand.get(u)
        except Exception as e:
            r = None
            err = str(e)[:90]
        if r is not None and 200 <= r.status_code < 300:
            txt = r.text
            firma = [p for p, rx in FIRMA if rx.search(txt)]
            with lock_s:
                res.append({"empresa": empresa, "url_ok": u, "estado": f"HTTP {r.status_code}",
                            "firma": firma})
                n[0] += 1
                if n[0] % 8 == 0:
                    print(f"... {n[0]} paginas OK", flush=True)
                print(f"FIRMA {empresa[:30]:<32} -> {firma[:5]}", flush=True)
            return
        elif r is not None:
            with lock_s:
                res.append({"empresa": empresa, "url_rayada": u, "estado": f"HTTP {r.status_code}"})
    with lock_p:
        res.append({"empresa": empresa, "estado": "sin-exito"})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--solo", default="")
    args = ap.parse_args()
    sel = args.solo.lower()

    hilos = []
    for emp, urls in CANDIDATOS.items():
        if sel and sel not in emp.lower():
            continue
        th = threading.Thread(target=sondear, args=(emp, urls))
        hilos.append(th)
        th.start()
        time.sleep(0.08)
    for th in hilos:
        th.join()

    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"listo -> {OUT} ({len(res)} registros)")


if __name__ == "__main__":
    main()
