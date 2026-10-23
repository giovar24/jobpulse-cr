import argparse
import asyncio
import csv
import json
import logging
import os
import re
import socket
import pandas as pd
from datetime import datetime, timedelta
from dotenv import load_dotenv

from core.bootstrap import ensure_workspace
from core.database import JobDatabase
from core.browser_manager import BraveManager
from core.gate import (gate_jobs, es_reciente, es_vigente, DEFAULT_DIAS,
                       DEFAULT_MIN_SCORE)
from core.ia_api import (IAClient, IAError, DEFAULT_IA_MIN, cv_a_markdown,
                         perfil_hash)
from providers.boards.procomer import ProcomerProvider
from providers.boards.linkedin import LinkedInProvider
from providers.boards.indeed import IndeedProvider
from providers.boards.ane import AneProvider
from providers.boards.cinde import CindeProvider
from providers.directos.workday import WorkdayProvider
from providers.base import JobOffer
from providers.source_groups import (GROUP_BOARDS, GROUP_DIRECTOS,
                                     GROUP_ESTATALES, RUN_CHOICES,
                                     RUN_LABEL, RUN_TODOS, run_incluye,
                                     source_group)
from core.dedupe import dedupe_boards_vs_directos
from core.dates import fecha_efectiva
from core.exclusiones import excluir_y_purgar, filtrar_offers
from core.progreso import GRUPO_LABEL, GRUPO_RESTO, iniciar_corrida
from core import credenciales as creds_store
from core.rutas import BASE_DIR, CV_IA_JSON, CV_IA_MD, ruta_informe

ensure_workspace()   # crea data/, profile.json (generico) y .env si faltan
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("jobpulse")

# ANE barre por fecha (su buscador esta roto): tope de paginas de listado.
# El corte por fecha real lo pone la ventana (dias); esto es una red de seguridad.
ANE_PAGES = int(os.getenv("ANE_MAX_PAGES", "15"))

# CINDE Jobs barre todo el tablero activo (su API no busca bien en ES y son
# pocas vacantes): tope de paginas; cada pagina trae hasta 100 ofertas.
CINDE_PAGES = int(os.getenv("CINDE_MAX_PAGES", "10"))

# Workday: barrido de tenants curados con searchText "Costa Rica"; `pages`
# paginas de 20 por tenant (los hits CR casi nunca pasan de 20).
WORKDAY_PAGES = int(os.getenv("WORKDAY_MAX_PAGES", "3"))

# Phenom: barrido de sitios SSR con keywords "Costa Rica"; 10 paginas por sitio.
PHENOM_PAGES = int(os.getenv("PHENOM_MAX_PAGES", "3"))

# TP (Teleperformance): API Umbraco de tp.com filtrada country=Costa Rica;
# paginas de 50 (el cat CR actual es corto, 2 pag sobra).
TP_PAGES = int(os.getenv("TP_MAX_PAGES", "2"))

# Greenhouse: 1 GET por tablero (API board con content=true); los tokens
# vienen del barrido multi-ATS (otros_ats_cr.json). Pages se usa como reintentos.
GREENHOUSE_PAGES = int(os.getenv("GREENHOUSE_MAX_PAGES", "1"))

# iCIMS: listado paginado pr=N (50/pag) por board; sin filtro server-side
# de keyword -> se barre y se filtra CR del lado del proveedor.
ICIMS_PAGES = int(os.getenv("ICIMS_MAX_PAGES", "8"))

# SmartRecruiters: API publica q=Costa Rica por compania (pag de 100).
SR_PAGES = int(os.getenv("SR_MAX_PAGES", "10"))

# Rippling: boards con peloteo SSR; filtrado por registro (title+locations).
RIPPLING_PAGES = int(os.getenv("RIPPLING_MAX_PAGES", "1"))

# ATS ligeros: Ashby/Lever/Recruitee/Workable (1 GET por tablero; Workable
# paginado de 10 y con throttle propio -> AL_MAX_PAGES limita el paginado).
AL_PAGES = int(os.getenv("AL_MAX_PAGES", "6"))

# Orden de los barridos "de un solo tiro" (uno por proveedor, sin termino de
# busqueda). Lo usa la GUI (pestana Corrida) para la barra de progreso y para
# diferenciar PORTALES ESTATALES (ANE/CINDE/Procomer) de JOB BOARDS
# (agregadores: Indeed/LinkedIn) y de PORTALES DE LA EMPRESA (ATS directo:
# Workday, iCIMS, Greenhouse...). El grupo tambien define el ALCANCE con
# --solo: "ats" se corre aparte de "boards", y los estatales entran en la
# corrida de boards (no son ATS de empresa), asi que cada corrida puede
# fallar o reprogramarse sin la otra.
BARRIDOS = [
    ("ANE (barrido por fecha)", "estatales"),
    ("CINDE Jobs", "estatales"),
    ("Workday", "directos"),
    ("Phenom", "directos"),
    ("TP (Teleperformance)", "directos"),
    ("Greenhouse", "directos"),
    ("iCIMS", "directos"),
    ("SmartRecruiters", "directos"),
    ("SuccessFactors", "directos"),
    ("Rippling", "directos"),
    ("ATS ligeros (Ashby/Lever/Recruitee/Workable)", "directos"),
    ("Login sites (navegador del usuario)", "directos"),
]


def strip_html(text):
    if not text:
        return ""
    return re.sub(r"<[^>]+>", " ", text)


def resume_text(text, limit=200):
    clean = re.sub(r"\s+", " ", strip_html(text)).strip()
    return clean[:limit] + ("..." if len(clean) > limit else "")


def resolve_first(hosts):
    """Devuelve la IP del primer host que resuelva por DNS (o None)."""
    for h in hosts:
        try:
            return socket.gethostbyname(h)
        except OSError:
            continue
    return None


def job_row(j):
    return {
        "Fuente": j.source,
        "Grupo": GRUPO_LABEL.get(source_group(j.source), GRUPO_RESTO),
        "Puesto": j.title,
        "Empresa": j.company,
        "Ubicación": j.location,
        "Link": j.link,
        "Fecha": j.date_published or "",
        "Vence": j.expires_at or "",
        "Salario": j.salary or "",
        "Gate": (f"{j.gate_score:.2f}" if j.gate_score is not None else ""),
        "IA": (f"{j.ia_score:.0f}"
               if getattr(j, "ia_score", None) is not None else ""),
        "Descripción": resume_text(j.description),
        "Requisitos": resume_text(j.requirements),
    }


def dict_a_offer(d: dict) -> JobOffer:
    """Convierte una fila de jobs_raw (dict) en JobOffer para re-filtrar."""
    return JobOffer(
        source=d.get("source", ""), search_term=d.get("search_term", ""),
        job_id=d.get("job_id", ""), title=d.get("title", ""),
        company=d.get("company", ""), location=d.get("location", ""),
        link=d.get("link", ""), date_published=d.get("date_published"),
        expires_at=d.get("expires_at"),
        description=d.get("description"), requirements=d.get("requirements"),
        salary=d.get("salary"),
    )


def _motor(profile, sin_expand: bool, searches: list):
    """Crea (si hay perfil) el motor semantico y expande las busquedas."""
    if not profile:
        return None, searches
    from core.search_engine import SemanticSearchEngine
    engine = SemanticSearchEngine()
    if not sin_expand:
        logger.info("Expandiendo tematicas del perfil...")
        searches = engine.expand_searches(
            searches, top_n=2, vocab_es=profile.titulos, vocab_en=profile.titulos
        )
    return engine, searches


async def _aplicar_gate(db, profile, engine, offers, dias, min_score):
    """Gate Fecha Y relevancia -> staging (jobs_raw) + curado (jobs).

    Antes de tocar la base: barrera de exclusiones (core/exclusiones.py) —
    lo ya excluido como fuera del perfil NO re-ingresa, aunque el board lo
    republica en el scraping de hoy.
    """
    claves, ids = await db.exclusiones_conocidas()
    offers, n_fuera = filtrar_offers(offers, claves, ids)
    if n_fuera:
        logger.info("Barrera de exclusiones: %d offers ya excluidas "
                    "(jobs_excluidas) no re-ingresaron.", n_fuera)
    aprobados, descartados = gate_jobs(offers, profile, engine, dias, min_score)
    await db.save_raw_jobs(offers)                 # todo lo crudo + score/razon
    newly = await db.save_jobs(aprobados)          # solo aprobados -> tabla jobs
    logger.info(
        "Pipeline: %d crudas -> %d aprobadas -> %d nuevas en jobs (%d descartadas).",
        len(offers), len(aprobados), len(newly), len(descartados),
    )
    return newly, aprobados, descartados


def _reportes(newly, sufijo: str = ""):
    """CSV del dia. `sufijo` ("_ats"/"_boards") evita que dos corridas del
    mismo dia se pisen entre si."""
    stamp = datetime.now().strftime("%Y%m%d")
    if not newly:
        logger.info("No new jobs found.")
        return
    logger.info(f"Found {len(newly)} new jobs today!")
    df = pd.DataFrame([job_row(j) for j in newly])
    report_name = ruta_informe("vacantes", f"report_{stamp}{sufijo}.csv")
    df.to_csv(report_name, index=False, encoding="utf-8-sig")
    logger.info(f"Report generated: {report_name}")

    now = datetime.now()
    threshold = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    today = now.strftime("%Y-%m-%d")
    # Solo fechas válidas dentro de las últimas 24 h (excluye vacías y futuras).
    recent = [j for j in newly if threshold <= (j.date_published or "") <= today]
    if recent:
        df24 = pd.DataFrame([job_row(j) for j in recent])
        name24 = ruta_informe("vacantes", f"report_24h_{stamp}{sufijo}.csv")
        df24.to_csv(name24, index=False, encoding="utf-8-sig")
        logger.info(f"Publicadas en las últimas 24 h: {len(recent)} (guardadas en {name24})")
    else:
        logger.info("Sin publicaciones en las últimas 24 horas.")


async def refiltrar(db, profile, engine, dias, min_score, prog=None):
    """Re-aplica el gate sobre jobs_raw SIN re-scrapear (valor de la 2da tabla).

    La tabla curada `jobs` se RECONSTRUYE: lo que aprobo una corrida anterior
    pero ya no pasa los criterios actuales sale de `jobs` (nunca de jobs_raw).
    """
    if prog:
        prog.fase("Re-filtrar (sin scraping)")
    raw = await db.get_raw_jobs()
    if not raw:
        logger.warning("jobs_raw vacia: corre una corrida normal primero.")
        return
    await db.clear_curated()
    offers = [dict_a_offer(r) for r in raw]
    # mismo orden de prioridad que la corrida normal: first directos, luego
    # boards (las copias de boards que ya existen en ATS se descartan).
    offers, n_dup = dedupe_boards_vs_directos(offers)
    logger.info("Re-filtrando %d vacantes crudas (dedupe boards<->directos "
                "descarto %d; sin scraping)..." % (len(offers), n_dup))
    if prog:
        prog.fase("Gate + reportes")
    newly, _, _ = await _aplicar_gate(db, profile, engine, offers, dias, min_score)
    # Misma regla de vigencia que la corrida normal: lo vencido no se conserva.
    await db.purge_expired(dias)
    # Duplicados exactos marcados con dup_of (no se borra nada).
    await db.marcar_duplicados_exactos()
    # Puestos fuera del perfil -> exclusion permanente (tombstone + DELETE),
    # y veredictos restaurados en la curada desde el crudo.
    res_excl = await excluir_y_purgar(db, profile)
    await db.resincronizar_veredictos()
    _reportes(newly)
    if prog:
        prog.sumar(GROUP_ESTATALES, sum(
            1 for o in offers if source_group(o.source) == GROUP_ESTATALES))
        prog.sumar(GROUP_BOARDS, sum(
            1 for o in offers
            if source_group(o.source) not in (GROUP_DIRECTOS, GROUP_ESTATALES)))
        prog.sumar(GROUP_DIRECTOS, sum(
            1 for o in offers if source_group(o.source) == GROUP_DIRECTOS))
    return _resumen_progreso(prog, len(newly)) if prog else None


# ---------------------------------------------------------------------------
# IA externa (a demanda): segunda opinion fuerte sobre el crudo y el CV.
# NUNCA se ejecuta dentro de una corrida normal de scraping; solo con --ia,
# --ia-cv o --ia-reporte. La cuota se controla en core/ia_api.py.
# ---------------------------------------------------------------------------

def _ventana_ia(raw: list, dias: int) -> list:
    """Crudo con descripcion, vigente y dentro de la ventana de fechas.

    Ventana por FECHA EFECTIVA: la que trae la pagina si es legible; si no,
    la del scrape (created_at) como inferida — todos los registros se
    procesan por fecha (core/dates.fecha_efectiva).
    """
    return [
        r for r in raw
        if (r.get("description") or "").strip()
        and es_vigente(r.get("expires_at"))
        and es_reciente(fecha_efectiva(r), dias)
    ]


def listar_modelos_ia(modelo: str = "") -> None:
    """Imprime la lista VIVA de modelos del proveedor (no hardcodeada).

    NVIDIA/Google agregan y retiran modelos, y algunos aparecen en /models
    pero no estan habilitados para la cuenta. El selector de la UI y este
    comando usan la misma lista.
    """
    client = IAClient({"modelo": modelo} if modelo else None)
    try:
        modelos = client.listar_modelos()
    except IAError as exc:
        logger.error("IA: %s", exc)
        return
    actual = modelo or client.modelo
    print(f"{len(modelos)} modelo(s) en {client.proveedor} "
          f"({client.base_url}):")
    for m in modelos:
        print(("  * " if m == actual else "    ") + m)
    if actual not in modelos:
        print(f"\nOJO: el modelo actual '{actual}' no aparece en la lista; "
              "puede haber sido retirado. Elegí otro con --ia-modelo.")
    print("\nUsalo con: python main.py --ia --ia-modelo \"<modelo>\" "
          "(o el selector de la UI).")


async def procesar_ia(db, profile, dias: int, ia_min: float = DEFAULT_IA_MIN,
                      ia_max: int = 0, forzar: bool = False, prog=None,
                      modelo: str = "") -> dict:
    """Evalua el crudo (jobs_raw) con la IA fuerte, a demanda.

    No cambia la membresia del gate: `jobs` sigue decidiendose con embeddings.
    Solo anota score/razon en jobs_raw y jobs, guarda el historial en
    `ia_evals` (para calibrar/destilar) y deja un CSV revisable del dia.
    """
    client = IAClient({"modelo": modelo} if modelo else None)
    if not client.disponible:
        logger.error("IA: %s", client.motivo_no_disponible())
        return {"_estado": "error", "motivo": "IA sin clave (IA_API_KEY)"}
    if ia_max and ia_max > 0:
        client.max_vacantes = ia_max
    if profile is None:
        logger.warning("IA: no hay data/usuario/profile.json; la evaluación será "
                       "genérica (sin títulos ni skills del candidato).")

    raw = await db.get_raw_jobs()
    ventana = _ventana_ia(raw, dias)
    # Los duplicados marcados (dup_of) no gastan cuota: su canonico ya esta
    # en la lista.
    ventana = [r for r in ventana if not (r.get("dup_of") or "").strip()]
    curada_ids = await db.get_curada_ids()
    por_id = {r.get("job_id"): r for r in ventana}
    p_hash = perfil_hash(profile)
    modelo = f"{client.proveedor}/{client.modelo}"

    # PRIORIDAD (flujo replanteado 2026-10-22): primero las CANDIDATAS (la
    # curada, mejor gate_score primero — el presupuesto de la IA va a lo que
    # el usuario va a revisar), y el resto solo para llenar presupuesto: las
    # crudas con mejor score (la banda cercana al umbral es la senal de
    # recalibracion). "Mas recientes primero" descartaba oro.
    ventana.sort(key=lambda r: (0 if r.get("job_id") in curada_ids else 1,
                                -(r.get("gate_score") or 0.0)))
    n_curada = sum(1 for r in ventana if r.get("job_id") in curada_ids)

    pendientes = []
    for r in ventana:
        h = client.hash_vacante(r, p_hash)
        r["_ia_hash"] = h
        cacheada = (r.get("ia_hash") == h and r.get("ia_score") is not None)
        if cacheada and not forzar:
            continue
        pendientes.append(r)

    logger.info("IA: %d en ventana (%d días): %d candidatas + %d crudo · "
                "%d ya evaluadas (caché) · %d pendientes (tope %d) · modelo %s",
                len(ventana), dias, n_curada, len(ventana) - n_curada,
                len(ventana) - len(pendientes), len(pendientes),
                client.max_vacantes, modelo)
    if prog:
        prog.fase(f"IA: evaluando {len(pendientes)} vacantes", "IA",
                  f"{modelo} · lotes de {client.lote}")

    resultados, errores = {}, {}
    if pendientes:
        resultados, errores = client.puntuar_vacantes(
            pendientes, profile,
            progreso=(lambda msg: prog.nota(msg)) if prog else None)
        for jid, res in resultados.items():
            v = por_id.get(jid) or {}
            res["hash"] = v.get("_ia_hash") or ""
        await db.save_ia_latest(resultados, modelo)
        ts = datetime.now().isoformat(timespec="seconds")
        registros = []
        for jid, res in resultados.items():
            v = por_id.get(jid) or {}
            registros.append({
                "ts": ts, "tipo": "vacante", "proveedor": client.proveedor,
                "modelo": client.modelo, "perfil_hash": p_hash,
                "job_id": jid, "job_hash": res.get("hash"),
                "score": res.get("score"),
                "veredicto": ("aplica" if float(res.get("score") or 0) >= ia_min
                              else "no"),
                "razon": res.get("razon"),
                "payload": json.dumps({
                    "puesto": v.get("title"), "empresa": v.get("company"),
                    "fuente": v.get("source"), "score": res.get("score"),
                    "razon": res.get("razon")}, ensure_ascii=False),
                "tokens_in": 0, "tokens_out": 0,
            })
        await db.registrar_ia(registros)

        # Veredictos (el embudo de 3 banderas): apta = verde (lista final),
        # descartada = amarilla (oculta del ranking, conservada hasta el TTL).
        # La decision del usuario (veredicto_por='usuario') nunca se pisa.
        veredictos = {}
        for jid, res in resultados.items():
            veredictos[jid] = {
                "veredicto": ("apta" if float(res.get("score") or 0) >= ia_min
                              else "descartada"),
                "razon": res.get("razon"), "por": "ia",
            }
        n_ver = await db.guardar_veredictos(veredictos)
        if n_ver:
            logger.info("IA: %d veredictos escritos (apta >= %.0f).", n_ver,
                        ia_min)

    if prog:
        prog.fase("IA: guardando evaluaciones y reporte", "IA")

    # CSV de REVISION: solo las candidatas (la curada), mejor gate primero,
    # con la segunda opinion y el veredicto al lado.
    ruta_csv = ""
    candidatas = [r for r in ventana if r.get("job_id") in curada_ids]
    if candidatas:
        stamp = datetime.now().strftime("%Y%m%d_%H%M")
        ruta_csv = ruta_informe("ia", f"ia_{stamp}.csv")
        with open(ruta_csv, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["Job ID", "Fuente", "Puesto", "Empresa", "Ubicación",
                        "Publicada", "Vence", "IA", "¿Aplica?", "Razón IA",
                        "Veredicto", "Gate", "Link"])
            for r in candidatas:
                jid = r.get("job_id")
                nuevo = resultados.get(jid) or {}
                ia = nuevo.get("score", r.get("ia_score"))
                razon = nuevo.get("razon", r.get("ia_razon"))
                gate = r.get("gate_score")
                ver = (r.get("veredicto")
                       or ("apta" if (ia is not None and float(ia) >= ia_min)
                           else None))
                w.writerow([
                    jid, r.get("source"), r.get("title"), r.get("company"),
                    r.get("location"), (r.get("date_published") or "")[:10],
                    (r.get("expires_at") or "")[:10],
                    "" if ia is None else f"{float(ia):.0f}",
                    "" if ia is None else ("sí" if float(ia) >= ia_min else "no"),
                    razon or "", ver or "",
                    "" if gate is None else f"{float(gate):.2f}",
                    r.get("link") or "",
                ])
        logger.info("IA: CSV de candidatas en %s", ruta_csv)

    aprob = 0
    for r in candidatas:
        ia = resultados.get(r.get("job_id"), {}).get("score", r.get("ia_score"))
        if ia is not None and float(ia) >= ia_min:
            aprob += 1
    logger.info("IA: %d evaluadas ahora · %d desde cache · de %d candidatas "
                "aplican %d (>= %.0f) · %d errores · %s",
                len(resultados), len(ventana) - len(pendientes),
                len(candidatas), aprob, ia_min,
                len(errores), client.resumen_uso())
    for jid, motivo in list(errores.items())[:5]:
        logger.warning("IA: sin evaluar %s (%s)", jid, motivo)
    return {
        "_estado": "ok",
        "ia_evaluadas": len(resultados),
        "ia_cache": len(ventana) - len(pendientes),
        "ia_candidatas": len(candidatas),
        "ia_aplican": aprob,
        "ia_errores": len(errores),
        "ia_modelo": modelo,
        "ia_llamadas": client.llamadas,
        "ia_csv": ruta_csv,
    }


async def evaluar_cv_ia(db, profile, cv_path: str, puesto: str = "",
                        guardar: bool = False, forzar: bool = False,
                        prog=None, dias: int = DEFAULT_DIAS,
                        modelo: str = "") -> dict:
    """Crea el perfil desde el CV (como cv2profile.py) y evalua el CV.

    Por defecto NO toca el perfil: deja el informe en data/usuario/cv_ia.md
    y el JSON completo en data/usuario/cv_ia.json. Con `guardar=True` escribe
    el perfil (merge + backup) usando el CV como fuente de verdad.
    """
    client = IAClient({"modelo": modelo} if modelo else None)
    if not client.disponible:
        logger.error("IA: %s", client.motivo_no_disponible())
        return {"_estado": "error", "motivo": "IA sin clave (IA_API_KEY)"}

    from core.cv_analysis import extract_text, merge_profile
    from core import profile as profile_mod
    from core.profile import guardar_perfil

    try:
        cv_texto = extract_text(cv_path)
    except Exception as exc:                     # PDF/DOCX roto, ruta mala...
        logger.error("CV: no pude leer %s (%s)", cv_path, exc)
        return {"_estado": "error", "motivo": f"CV ilegible: {exc}"}
    if len((cv_texto or "").strip()) < 120:
        logger.error("CV: el texto extraido es muy corto (%d caracteres); "
                     "¿es un PDF escaneado sin OCR?", len((cv_texto or "").strip()))
        return {"_estado": "error", "motivo": "CV sin texto suficiente"}

    p_hash = perfil_hash(profile)
    c_hash = client.hash_cv(cv_texto, p_hash, puesto)
    ruta_json, ruta_md = str(CV_IA_JSON), str(CV_IA_MD)
    previo = None
    if not forzar and os.path.exists(ruta_json):
        try:
            with open(ruta_json, encoding="utf-8") as f:
                previo = json.load(f)
        except (OSError, ValueError):
            previo = None
        if previo and (previo.get("meta") or {}).get("cv_hash") != c_hash:
            logger.info("CV: cambió el CV, el puesto o el modelo; se reevalúa.")
            previo = None

    if previo:
        resultado = previo.get("resultado") or {}
        meta = previo.get("meta") or {}
        meta["cache"] = True
        logger.info("CV: reutilizando evaluación cacheada de %s "
                    "(usá --ia-forzar para repetirla).", ruta_json)
    else:
        if prog:
            prog.fase("IA: leyendo el CV y creando el perfil", "IA",
                      f"{client.proveedor}/{client.modelo}")
        recientes = _ventana_ia(await db.get_raw_jobs(), dias)
        recientes.sort(key=lambda r: (
            r.get("ia_score") if r.get("ia_score") is not None
            else (r.get("gate_score") or 0) * 100), reverse=True)
        try:
            resultado = client.evaluar_cv(cv_texto, profile, recientes[:25],
                                          puesto)
        except IAError as exc:
            logger.error("CV: la IA falló: %s", exc)
            return {"_estado": "error", "motivo": str(exc)}
        meta = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "proveedor": client.proveedor, "modelo": client.modelo,
            "cv_path": os.path.abspath(cv_path), "cv_hash": c_hash,
            "perfil_hash": p_hash, "puesto": puesto,
            "tokens_in": client.tokens_in, "tokens_out": client.tokens_out,
            "cache": False,
        }
        CV_IA_JSON.parent.mkdir(parents=True, exist_ok=True)
        with open(ruta_json, "w", encoding="utf-8") as f:
            json.dump({"meta": meta, "resultado": resultado}, f,
                      ensure_ascii=False, indent=2)
        with open(ruta_md, "w", encoding="utf-8") as f:
            f.write(cv_a_markdown(resultado, meta))
        ev = resultado.get("evaluacion") or {}
        await db.registrar_ia([{
            "ts": meta["ts"], "tipo": "cv", "proveedor": client.proveedor,
            "modelo": client.modelo, "perfil_hash": p_hash, "job_id": None,
            "job_hash": c_hash, "score": ev.get("puntaje"),
            "veredicto": "cv", "razon": ev.get("resumen"),
            "payload": json.dumps(resultado, ensure_ascii=False),
            "tokens_in": client.tokens_in, "tokens_out": client.tokens_out,
        }])

    if prog:
        prog.fase("IA: guardando informe y perfil", "IA")
    if guardar:
        # Igual que cv2profile.py: se parte del JSON COMPLETO para conservar
        # claves que CandidateProfile.to_dict() no expone (contacto, filtros,
        # pesos_ats, _comentario...). El CV solo reemplaza lo suyo.
        ruta_actual = getattr(profile, "path", None) or profile_mod.DEFAULT_PATH
        actual = {}
        try:
            if ruta_actual.exists():
                actual = json.loads(ruta_actual.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("CV: perfil actual ilegible (%s); se parte de cero.",
                           exc)
        if not actual and profile is not None:
            actual = profile.to_dict()
        perfil_ia = dict(resultado.get("perfil") or {})
        cv_ia = perfil_ia.setdefault("cv", {})
        bus_ia = perfil_ia.setdefault("busqueda", {})
        overrides = bus_ia.setdefault("overrides", {})
        # Completar con los extractores locales (contacto, fechas, idiomas...)
        # lo que el LLM no haya devuelto: el perfil queda usable por el ATS.
        try:
            from core.cv_analysis import (detect_certifications, detect_contact,
                                          detect_education_level,
                                          detect_experience_years,
                                          detect_languages_struct,
                                          detect_seniority)
            contacto = dict(cv_ia.get("contacto") or {})
            for k, v in detect_contact(cv_texto).items():
                if v:
                    contacto.setdefault(k, v)
            if contacto:
                cv_ia["contacto"] = contacto
            if not cv_ia.get("experiencia") and not overrides.get("experiencia_anios"):
                v = detect_experience_years(cv_texto)
                if v:
                    overrides["experiencia_anios"] = v
            if not cv_ia.get("educacion") and not overrides.get("educacion_nivel"):
                v = detect_education_level(cv_texto)
                if v:
                    overrides["educacion_nivel"] = v
            if not cv_ia.get("certificaciones"):
                v = detect_certifications(cv_texto)
                if v:
                    cv_ia["certificaciones"] = v
            if not cv_ia.get("idiomas"):
                v = detect_languages_struct(cv_texto)
                if v:
                    cv_ia["idiomas"] = v
            if not cv_ia.get("experiencia") and not overrides.get("seniority"):
                v = detect_seniority(cv_texto)
                if v:
                    overrides["seniority"] = v
        except Exception as exc:
            logger.debug("CV: extractores locales no disponibles (%s).", exc)
        nuevo = merge_profile(actual, perfil_ia, replace=True)
        nuevo["_comentario"] = (
            "Perfil del candidato (CV Harvard/ATS). Datos extraidos por la IA "
            f"desde {os.path.basename(cv_path)} "
            f"({datetime.now():%Y-%m-%d}). Revisalo en la pestaña Perfil > Mi CV.")
        bk = guardar_perfil(nuevo)
        logger.info("CV: perfil actualizado%s. Revisalo en la pestaña Perfil.",
                    f" (backup: {bk.name})" if bk else "")
        # El perfil viejo sigue en memoria; este proceso termina acá.

    ev = resultado.get("evaluacion") or {}
    logger.info("CV: puntaje %.0f/100 · informe en %s",
                float(ev.get("puntaje") or 0), ruta_md)
    if ev.get("resumen"):
        logger.info("CV: %s", ev["resumen"])
    for s in (ev.get("sugerencias") or [])[:5]:
        logger.info("CV: sugerencia — %s: %s", s.get("donde"),
                    s.get("cambio"))
    return {
        "_estado": "ok",
        "ia_cv_puntaje": ev.get("puntaje"),
        "ia_cv_informe": ruta_md,
        "ia_cv_json": ruta_json,
        "ia_cv_cache": bool(meta.get("cache")),
        "ia_cv_perfil_guardado": bool(guardar),
        "ia_cv_llamadas": client.llamadas,
        "ia_cv_modelo": f"{meta.get('proveedor')}/{meta.get('modelo')}",
    }


async def reporte_ia(db, min_score: float = DEFAULT_MIN_SCORE,
                     ia_min: float = DEFAULT_IA_MIN) -> dict:
    """Calibra el gate local contra las evaluaciones IA ya guardadas.

    Solo mide y sugiere; no cambia umbrales ni la membresia de `jobs`.
    """
    from core.ia_calibracion import calcular, render_md

    evals = await db.get_ia_evals()
    ultimo = {}
    for e in evals:
        if e.get("tipo") == "vacante" and e.get("job_id"):
            ultimo[e["job_id"]] = e            # el historial viene en orden
    raw = {r.get("job_id"): r for r in await db.get_raw_jobs()}
    pares = []
    for jid, e in ultimo.items():
        r = raw.get(jid)
        if not r or r.get("gate_score") is None:
            continue
        pares.append({
            "job_id": jid, "source": r.get("source"),
            "gate": float(r["gate_score"]), "ia": float(e.get("score") or 0),
            "puesto": r.get("title"), "ia_razon": e.get("razon"),
            "gate_razon": r.get("gate_razon"),
        })
    cvs = {}
    for e in evals:
        if e.get("tipo") == "cv":
            cvs[e.get("job_hash") or str(e.get("id"))] = e
    rep = calcular(pares, ia_min=ia_min, score_min=min_score)
    md = render_md(rep, list(cvs.values()))
    md_path = ruta_informe("ia", "ia_calibracion.md")
    json_path = ruta_informe("ia", "ia_calibracion.json")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({"generado": datetime.now().isoformat(timespec="seconds"),
                   "reporte": rep}, f, ensure_ascii=False, indent=2)
    informe = (str(md_path.relative_to(BASE_DIR))
               if md_path.is_relative_to(BASE_DIR) else str(md_path))

    if not pares:
        logger.warning("IA calibración: no hay vacantes evaluadas por la IA "
                       "todavía. Corré `python main.py --ia` primero.")
        return {"_estado": "ok", "ia_pares": 0, "ia_informe": informe}
    u = rep.get("umbral_recomendado") or {}
    logger.info("IA calibración: %d pares · r=%s · MAE=%s · exactitud=%.1f%% "
                "con --score-min %.2f", rep["n"], rep.get("correlacion"),
                rep.get("mae"), 100 * rep.get("exactitud", 0), min_score)
    if u:
        logger.info("IA calibración: umbral sugerido --score-min %.2f "
                    "(F1 %.2f, exactitud %.1f%%). Es una sugerencia: aplicalo "
                    "y después corré --refiltrar.",
                    u["score_min"], u["f1"], 100 * u["exactitud"])
    logger.info("IA calibración: informe en %s", informe)
    return {"_estado": "ok", "ia_pares": rep["n"],
            "ia_correlacion": rep.get("correlacion"),
            "ia_mae": rep.get("mae"),
            "ia_umbral_sugerido": u.get("score_min"),
            "ia_informe": informe}


def _fases_totales(searches: list, re_filtrar: bool, solo: str = RUN_TODOS) -> int:
    """Numero EXACTO de fases (para la barra de la GUI).

    Se calcula sobre las busquedas ya EXPANDIDAS (el motor semantico puede
    duplicarlas), mas los barridos de un solo tiro, el dedupe y el gate.
    Con --solo solo cuentan los barridos y busquedas de ese alcance, o la
    barra de progreso se quedaria pegada en el 60%.
    """
    if re_filtrar:
        return 2   # re-filtrar + reportes (sin scraping)
    n_barridos = sum(1 for _, g in BARRIDOS if run_incluye(solo, g))
    return (1                           # fase 1: preparar (perfil + BD)
            + 1                         # fase 2: navegador Brave
            + (len(searches) if run_incluye(solo, GROUP_BOARDS) else 0)
            + n_barridos                # barridos de un solo tiro del alcance
            + 2)                        # dedupe cross-source + gate/reportes


_BARRIDO_GRUPO = {nombre: grupo for nombre, grupo in BARRIDOS}


def _barrido_cfg(nombre: str):
    """(nombre, grupo) para prog.fase(...) — grupo = boards|directos."""
    return nombre, _BARRIDO_GRUPO.get(nombre, "directos")


async def main(fresh: bool = False, searches: list = None, sin_expand: bool = False,
               dias: int = DEFAULT_DIAS, min_score=None,
               re_filtrar: bool = False, preflight: bool = False,
               solo: str = RUN_TODOS, ia: bool = False, ia_cv: str = "",
               ia_puesto: str = "", ia_guardar: bool = False,
               ia_forzar: bool = False, ia_min: float = DEFAULT_IA_MIN,
               ia_max: int = 0, ia_reporte: bool = False,
               ia_modelo: str = "", podar: bool = False,
               decidir: list = None, nota: str = ""):
    """Entrada: bloquea corridas dobles y publica el estado de progreso ANTES
    de tocar la red, para que la GUI (pestana Corrida) muestre que paso.

    `solo` (RUN_TODOS|RUN_ATS|RUN_BOARDS) separa la corrida en dos alcances:
    los ATS de empresa por un lado, los job boards (Indeed/LinkedIn/Procomer/
    ANE-Brete/CINDE) por el otro. El candado sigue siendo exclusivo, asi que
    las dos se lanzan una tras otra, no en paralelo.

    `ia` / `ia_cv` / `ia_reporte` son modos a demanda: no scrapean ni abren
    Brave. La corrida normal (sin esos flags) NUNCA llama a la IA externa.
    `podar` (mantenimiento de la base) y `decidir` (veredicto manual) tampoco
    usan navegador ni red.
    """
    if ia_cv:
        alcance = "IA · CV"
    elif ia_reporte:
        alcance = "IA · calibración"
    elif ia:
        alcance = "IA · crudo"
    elif podar:
        alcance = "poda de base"
    elif decidir:
        alcance = "veredicto manual"
    else:
        alcance = RUN_LABEL.get(solo, solo)
    prog = iniciar_corrida(
        ("preflight" if preflight else "corrida") + f" · {alcance}")
    if not prog:
        return                      # ya hay una corrida viva (PID en uso)
    try:
        resumen = await _corrida(prog, fresh, searches, sin_expand, dias,
                                 min_score, re_filtrar, preflight, solo,
                                 ia=ia, ia_cv=ia_cv, ia_puesto=ia_puesto,
                                 ia_guardar=ia_guardar, ia_forzar=ia_forzar,
                                 ia_min=ia_min, ia_max=ia_max,
                                 ia_reporte=ia_reporte,
                                 ia_modelo=ia_modelo,
                                 podar=podar, decidir=decidir, nota=nota) or {}
        estado = resumen.pop("_estado",
                             "preflight" if preflight else "ok")
        resumen.setdefault("alcance", alcance)
        prog.finalizar(estado, resumen or _resumen_progreso(prog, 0))
    except Exception as e:
        prog.finalizar("error", {"error": str(e), "alcance": alcance})
        logger.exception("Corrida abortada: %s", e)
        raise


def _resumen_progreso(prog, nuevas: int) -> dict:
    enc = prog._estado.get("encontradas", {})
    return {
        "nuevas": nuevas,
        "crudas_boards": enc.get("boards", 0),
        "crudas_directos": enc.get("directos", 0),
        "crudas_estatales": enc.get("estatales", 0),
    }


def _preflight(prog, profile, searches: list, solo: str = RUN_TODOS) -> dict:
    """Dry-run: publica el plan de corrida + estado de credenciales/archivos,
    sin navegador ni requests. Validacion rapida del setup y de la GUI."""
    logger.info("PREFLIGHT: sin red ni navegador; solo reporte de setup.")
    prog.fase(f"Preflight: plan de corrida ({RUN_LABEL.get(solo, solo)})")
    # Las busquedas por termino solo las usan los boards; listarlas en una
    # corrida ATS daria a entender que se van a ejecutar.
    if run_incluye(solo, GROUP_BOARDS):
        for s in searches:
            logger.info("  busqueda: %s / %s", s["puesto"], s["ubicacion"])
    plan = 0
    for nombre, grupo in BARRIDOS:
        if not run_incluye(solo, grupo):
            continue
        plan += 1
        prog.fase(nombre, grupo, "preflight (sin ejecutar)")
    prog.fase("Preflight: credenciales de login")
    lst = creds_store.lista_sitios_login()
    faltan = [s["empresa"] for s in lst if not s["tiene_credencial"]]
    for s in lst:
        logger.info("  login %-14s %-9s %s%s", s["empresa"],
                    "OBLIGATORIO" if s["obligatorio"] else "opcional",
                    "credencial ✓" if s["tiene_credencial"] else "SIN credencial",
                    f" — auto-login {'ON' if s.get('auto_login') else 'OFF'}")
    if faltan:
        logger.warning("  Faltan credenciales en: %s (python credenciales.py "
                       "--set \"Empresa\")", ", ".join(faltan))
    return {"alcance": RUN_LABEL.get(solo, solo), "plan_fases": plan,
            "busquedas": len(searches),
            "login_total": len(lst), "login_faltan": faltan}


async def _corrida(prog, fresh: bool, searches: list, sin_expand: bool,
                   dias: int, min_score: float, re_filtrar: bool,
                   preflight: bool, solo: str = RUN_TODOS,
                   ia: bool = False, ia_cv: str = "", ia_puesto: str = "",
                   ia_guardar: bool = False, ia_forzar: bool = False,
                   ia_min: float = DEFAULT_IA_MIN, ia_max: int = 0,
                   ia_reporte: bool = False, ia_modelo: str = "",
                   podar: bool = False, decidir: list = None,
                   nota: str = ""):
    modo_ia = ia or bool(ia_cv) or ia_reporte
    if modo_ia:
        prog.total(3)       # preparación + consulta + guardado
    # Perfil del candidato: fuente unica de verdad de las tematicas.
    from core.profile import CandidateProfile
    prog.fase(f"Preparación (perfil + base de datos)",
              detalle=f"alcance: {RUN_LABEL.get(solo, solo)}")
    profile = CandidateProfile.load()

    db = JobDatabase()
    await db.init_db()

    # --- Modos IA: a demanda, sin navegador ni scraping ---
    if modo_ia:
        if ia_reporte:
            prog.fase("IA: calibrando el gate local", "IA")
            # reporte_ia solo muestra el umbral; si viene None (default por
            # modelo) se usa el default del modulo para el informe.
            resumen = await reporte_ia(
                db, DEFAULT_MIN_SCORE if min_score is None else min_score,
                ia_min)
            prog.fase("IA: informe de calibración", "IA")
            return resumen
        if ia_cv:
            return await evaluar_cv_ia(db, profile, ia_cv, ia_puesto,
                                       ia_guardar, ia_forzar, prog, dias,
                                       ia_modelo)
        return await procesar_ia(db, profile, dias, ia_min, ia_max,
                                 ia_forzar, prog, ia_modelo)

    # --- Modo poda: mantenimiento sin scraping ni navegador ---
    if podar:
        prog.total(3)
        prog.fase("Poda: sectores faltantes")
        n_sec = await db.inferir_sectores_faltantes()
        prog.fase("Poda: exclusiones por perfil")
        res_excl = await excluir_y_purgar(db, profile)
        prog.fase("Poda: vigencia, TTL y veredictos")
        n_ttl = await db.purge_expired(dias)
        await db.marcar_duplicados_exactos()
        await db.resincronizar_veredictos()
        logger.info("Poda: %d sectores inferidos · %d puestos excluidos (%d "
                    "filas borradas) · %d vencidas/TTL.",
                    n_sec, res_excl.get("excluidas", 0),
                    res_excl.get("borradas", 0), n_ttl)
        return {"_estado": "ok", "poda_sectores": n_sec,
                "poda_ttl": n_ttl, **res_excl}

    # --- Modo decidir: veredicto manual del usuario (final, pisa al de la IA) ---
    if decidir:
        job_id, veredicto = (decidir[0] or "").strip(), (decidir[1] or "").strip().lower()
        if veredicto not in ("apta", "descartada"):
            logger.error("Veredicto invalido '%s': usa apta o descartada.",
                         veredicto)
            return {"_estado": "error", "motivo": "veredicto invalido"}
        n = await db.guardar_veredictos({job_id: {
            "veredicto": veredicto, "razon": nota, "por": "usuario"}})
        await db.resincronizar_veredictos()
        logger.info("Veredicto manual: %s -> %s (%d fila(s) actualizadas%s).",
                    job_id, veredicto, n, "; job_id no encontrado" if n == 0 else "")
        return {"_estado": "ok", "job_id": job_id, "veredicto": veredicto,
                "filas": n}

    # Alcance elegido con --solo (los dos bloques de scraping de abajo).
    correr_boards = run_incluye(solo, GROUP_BOARDS)
    correr_ats = run_incluye(solo, GROUP_DIRECTOS)

    # --- Modo re-filtrar: sin navegador, reevalua el crudo ya guardado ---
    if re_filtrar:
        prog.total(_fases_totales(searches, re_filtrar, solo))
        engine, _ = _motor(profile, sin_expand, [])
        await refiltrar(db, profile, engine, dias, min_score, prog)
        return

    if searches is None:
        if profile:
            searches = profile.to_searches()
            logger.info("Perfil: %s -> %d temas de busqueda",
                        profile.nombre or profile.path.name, len(searches))
        else:
            searches = [
                {"puesto": "Soporte", "puesto_en": "IT Support", "ubicacion": "Costa Rica"},
                {"puesto": "Soporte Técnico", "puesto_en": "Technical Support", "ubicacion": "Costa Rica"},
                {"puesto": "Mesa de Ayuda", "puesto_en": "Help Desk", "ubicacion": "Costa Rica"},
            ]

    # Motor semantico: expande busquedas y luego decide el gate (Fecha Y relevancia).
    engine, searches = _motor(profile, sin_expand, searches)
    # total de fases DESPUES de expandir: el motor puede duplicar las busquedas.
    prog.total(_fases_totales(searches, re_filtrar, solo))

    # Preflight: publica el plan de corrida (y el estado de las credenciales)
    # SIN navegador ni red. Sirve para validar la GUI y el setup.
    if preflight:
        _preflight(prog, profile, searches, solo)
        return

    if fresh:
        await db.clear_jobs()
        logger.info("Corrida en limpio: base de datos vaciada.")
    await db.clean_empty_jobs()

    browser_cfg = {}
    if os.getenv("BRAVE_BIN"):
        browser_cfg["brave_bin"] = os.getenv("BRAVE_BIN")
    if os.getenv("BRAVE_PERFIL"):
        browser_cfg["brave_perfil"] = os.getenv("BRAVE_PERFIL")

    # ANE: el ISP a veces no resuelve www.ane.cr; usamos un alias que si resuelva (brete.cr)
    # para obtener la IP y forzarla en el navegador.
    ane_ip = resolve_first(["www.ane.cr", "brete.cr", "ane.cr"])
    if ane_ip:
        browser_cfg["host_resolver_rules"] = f"MAP www.ane.cr {ane_ip}"
        logger.info(f"ANE host mapping: www.ane.cr -> {ane_ip}")

    browser_mgr = BraveManager(browser_cfg)
    prog.fase("Navegador (Brave + perfil del usuario)", "",
              "cierra y reabre Brave con depuracion CDP")
    port = browser_mgr.start_brave()

    if not port:
        logger.error("Could not start Brave browser. Exiting.")
        prog.nota("Brave no arranco: revisa BRAVE_BIN en .env")
        return {"_estado": "error", "motivo": "Brave no arranco",
                **_resumen_progreso(prog, 0)}

    # Todo lo crudo de los 5 portales; el gate despues decide que pasa a jobs.
    raw_offers: list = []
    try:
        if correr_boards:
            # --- 1. Portales por termino (keyword) ---
            providers = [ProcomerProvider({}), LinkedInProvider({}), IndeedProvider({})]
            for idx, search in enumerate(searches, 1):
                term = search["puesto"]
                loc = search["ubicacion"]
                logger.info(f"Searching for '{term}' in {loc}...")
                prog.fase(f"Boards por término: {term}", "boards",
                          f"búsqueda {idx}/{len(searches)} · {loc}")

                tasks = []
                for p in providers:
                    provider_term = search.get("puesto_en", term) \
                        if isinstance(p, (LinkedInProvider, IndeedProvider)) else term
                    if isinstance(p, (LinkedInProvider, IndeedProvider)):
                        tasks.append(p.fetch(provider_term, loc, pages=2, port=port))
                    else:
                        tasks.append(p.fetch(provider_term, loc, pages=2))

                results = await asyncio.gather(*tasks, return_exceptions=True)
                for i, res in enumerate(results):
                    name = providers[i].__class__.__name__
                    if isinstance(res, Exception):
                        logger.error(f"Provider {name} error: {res}")
                    elif res is None:
                        logger.warning(f"Provider {name} returned None")
                    else:
                        valid = [j for j in res if j.title and j.link]
                        logger.info(f"Provider {name} found {len(valid)} valid jobs")
                        # Procomer es portal estatal; LinkedIn/Indeed son
                        # agregadores. Se cuentan por separado en la GUI.
                        prog.sumar(GROUP_ESTATALES, sum(
                            1 for j in valid if source_group(j.source) == GROUP_ESTATALES))
                        prog.sumar(GROUP_BOARDS, sum(
                            1 for j in valid if source_group(j.source) != GROUP_ESTATALES))
                        raw_offers.extend(valid)

            # --- 2. ANE por barrido de fecha (UNA sola vez, ignora termino) ---
            # Credenciales: .env (ANE_USER/ANE_PASSWORD) o el almacén local
            # (data/empresas/credenciales.json, pestaña "Credenciales" / CLI).
            _cred_ane = creds_store.credencial("ANE") or {}
            prog.fase(*_barrido_cfg("ANE (barrido por fecha)"))
            ane = AneProvider({
                "username": _cred_ane.get("usuario"),
                "password": _cred_ane.get("password"),
                # Para no re-descargar el detalle de vacantes ya guardadas.
                "db_path": db.db_path,
            })
            try:
                ane_jobs = await ane.fetch(
                    "barrido-fecha", "Costa Rica", pages=ANE_PAGES, port=port, dias=dias
                )
                valid_ane = [j for j in ane_jobs if j.title and j.link]
                logger.info(f"ANE barrido: {len(valid_ane)} vacantes crudas")
                prog.sumar(GROUP_ESTATALES, len(valid_ane))
                raw_offers.extend(valid_ane)
            except Exception as e:
                logger.error(f"ANE barrido error: {e}")
            # --- 2b. CINDE Jobs: barrido completo UNA sola vez (API GraphQL
            # publica, sin navegador); el gate luego decide por relevancia ---
            prog.fase(*_barrido_cfg("CINDE Jobs"))
            cinde = CindeProvider({})
            try:
                cinde_jobs = await cinde.fetch(
                    "barrido-todos", "Costa Rica", pages=CINDE_PAGES
                )
                valid_cinde = [j for j in cinde_jobs if j.title and j.link]
                logger.info(f"CINDE barrido: {len(valid_cinde)} vacantes crudas")
                prog.sumar(GROUP_ESTATALES, len(valid_cinde))
                raw_offers.extend(valid_cinde)
            except Exception as e:
                logger.error(f"CINDE barrido error: {e}")

        if correr_ats:
            # --- 2c. Workday: tenants curados (empresas grandes sin LinkedIn/Indeed) ---
            prog.fase(*_barrido_cfg("Workday"))
            try:
                wd = WorkdayProvider({})
                wd_jobs = await wd.fetch("barrido-workday", "Costa Rica",
                                         pages=WORKDAY_PAGES)
                valid_wd = [j for j in wd_jobs if j.title and j.link]
                logger.info(f"Workday barrido: {len(valid_wd)} vacantes crudas")
                prog.sumar("directos", len(valid_wd))
                raw_offers.extend(valid_wd)
            except Exception as e:
                logger.error(f"Workday barrido error: {e}")
            # --- 2d. Phenom: sitios SSR confirmados (P&G, Zimmer Biomet...) ---
            prog.fase(*_barrido_cfg("Phenom"))
            try:
                from providers.directos.phenom import PhenomProvider
                ph = PhenomProvider({})
                ph_jobs = await ph.fetch("barrido-phenom", "Costa Rica",
                                         pages=PHENOM_PAGES)
                valid_ph = [j for j in ph_jobs if j.title and j.link]
                logger.info(f"Phenom barrido: {len(valid_ph)} vacantes crudas")
                prog.sumar("directos", len(valid_ph))
                raw_offers.extend(valid_ph)
            except Exception as e:
                logger.error(f"Phenom barrido error: {e}")
            # --- 2e. TP Umbraco: el board de Teleperformance via tp.com ---
            prog.fase(*_barrido_cfg("TP (Teleperformance)"))
            try:
                from providers.directos.tp import TpProvider
                tp = TpProvider({})
                tp_jobs = await tp.fetch("barrido-tp", "Costa Rica", pages=TP_PAGES)
                valid_tp = [j for j in tp_jobs if j.title and j.link]
                logger.info(f"TP barrido: {len(valid_tp)} vacantes crudas")
                prog.sumar("directos", len(valid_tp))
                raw_offers.extend(valid_tp)
            except Exception as e:
                logger.error(f"TP barrido error: {e}")
            # --- 2f. Greenhouse: tokens con CR del barrido multi-ATS ---
            prog.fase(*_barrido_cfg("Greenhouse"))
            try:
                from providers.directos.greenhouse import GreenhouseProvider
                gh = GreenhouseProvider({})
                gh_jobs = await gh.fetch("barrido-greenhouse", "Costa Rica",
                                         pages=GREENHOUSE_PAGES)
                valid_gh = [j for j in gh_jobs if j.title and j.link]
                logger.info(f"Greenhouse barrido: {len(valid_gh)} vacantes crudas")
                prog.sumar("directos", len(valid_gh))
                raw_offers.extend(valid_gh)
            except Exception as e:
                logger.error(f"Greenhouse barrido error: {e}")
            # --- 2g. iCIMS: boards TP/DocuSign (listado paginado pr=N) ---
            prog.fase(*_barrido_cfg("iCIMS"))
            try:
                from providers.directos.icims import IcimsProvider
                ii = IcimsProvider({})
                ii_jobs = await ii.fetch("barrido-icims", "Costa Rica", pages=ICIMS_PAGES)
                valid_ii = [j for j in ii_jobs if j.title and j.link]
                logger.info(f"iCIMS barrido: {len(valid_ii)} vacantes crudas")
                prog.sumar("directos", len(valid_ii))
                raw_offers.extend(valid_ii)
            except Exception as e:
                logger.error(f"iCIMS barrido error: {e}")
            # --- 2h. SmartRecruiters: API publica q=Costa Rica por compania ---
            prog.fase(*_barrido_cfg("SmartRecruiters"))
            try:
                from providers.directos.smartrecruiters import SmartRecruitersProvider
                sr = SmartRecruitersProvider({})
                sr_jobs = await sr.fetch("barrido-sr", "Costa Rica", pages=SR_PAGES)
                valid_sr = [j for j in sr_jobs if j.title and j.link]
                logger.info(f"SmartRecruiters barrido: {len(valid_sr)} vacantes crudas")
                prog.sumar("directos", len(valid_sr))
                raw_offers.extend(valid_sr)
            except Exception as e:
                logger.error(f"SmartRecruiters barrido error: {e}")
            # --- 2i. SuccessFactors: RSS publico keywords=(Costa Rica) ---
            prog.fase(*_barrido_cfg("SuccessFactors"))
            try:
                from providers.directos.successfactors import SuccessFactorsProvider
                sf = SuccessFactorsProvider({})
                sf_jobs = await sf.fetch("barrido-sf", "Costa Rica", pages=1)
                valid_sf = [j for j in sf_jobs if j.title and j.link]
                logger.info(f"SuccessFactors barrido: {len(valid_sf)} vacantes crudas")
                prog.sumar("directos", len(valid_sf))
                raw_offers.extend(valid_sf)
            except Exception as e:
                logger.error(f"SuccessFactors barrido error: {e}")
            # --- 2j. Rippling: boards ats.rippling.com con CR real (scan 2026-10-19) ---
            prog.fase(*_barrido_cfg("Rippling"))
            try:
                from providers.directos.rippling import RipplingProvider
                rp = RipplingProvider({})
                rp_jobs = await rp.fetch("barrido-rippling", "Costa Rica", pages=RIPPLING_PAGES)
                valid_rp = [j for j in rp_jobs if j.title and j.link]
                logger.info(f"Rippling barrido: {len(valid_rp)} vacantes crudas")
                prog.sumar("directos", len(valid_rp))
                raw_offers.extend(valid_rp)
            except Exception as e:
                logger.error(f"Rippling barrido error: {e}")
            # --- 2k. ATS ligeros: Ashby/Lever/Recruitee/Workable (tokens JSON) ---
            prog.fase(*_barrido_cfg("ATS ligeros (Ashby/Lever/Recruitee/Workable)"))
            try:
                from providers.directos.ats_ligeros import AtsLigerosProvider
                al = AtsLigerosProvider({})
                al_jobs = await al.fetch("barrido-ats-ligeros", "Costa Rica", pages=AL_PAGES)
                valid_al = [j for j in al_jobs if j.title and j.link]
                logger.info(f"ATS ligeros barrido: {len(valid_al)} vacantes crudas")
                prog.sumar("directos", len(valid_al))
                raw_offers.extend(valid_al)
            except Exception as e:
                logger.error(f"ATS ligeros barrido error: {e}")
            # --- 2l. Sitios con sesion/Cloudflare: browser del usuario (cookies) ---
            prog.fase(*_barrido_cfg("Login sites (navegador del usuario)"))
            try:
                from providers.login.browser_sites import LoginSitesProvider
                ls = LoginSitesProvider({})
                ls_jobs = await ls.fetch("barrido-login-sites", "Costa Rica",
                                         pages=1, port=port)
                valid_ls = [j for j in ls_jobs if j.title and j.link]
                logger.info(f"Login-sites barrido: {len(valid_ls)} vacantes crudas")
                prog.sumar("directos", len(valid_ls))
                raw_offers.extend(valid_ls)
            except Exception as e:
                logger.error(f"Login-sites barrido error: {e}")
    finally:
        browser_mgr.stop_brave()

    # --- 2z. Dedupe cross-source: prioridad a providers DIRECTOS. Si el
    # puesto ya esta en un provider directo (ATS de la empresa), se descarta
    # la copia republicada en los job boards (LinkedIn/Indeed/ANE...). ---
    prog.fase("Dedupe cross-source (boards vs portales de empresa)")
    prev_total = len(raw_offers)
    # Con --solo boards la corrida no ve a la de ATS: se le pasan como
    # referencia los directos ya guardados en jobs_raw para no reintroducir
    # copias que la corrida de ATS descarto. Sin referencia cuando el alcance
    # trae los dos grupos (o solo ATS) porque ahi ya estan en `raw_offers`.
    ref_directos: list = []
    if correr_boards and not correr_ats:
        ref_directos = [
            dict_a_offer(r) for r in await db.get_raw_jobs()
            if source_group(r.get("source") or "") == GROUP_DIRECTOS
        ]
        logger.info("Dedupe: %d portales de empresa de corridas anteriores "
                    "se usan como referencia.", len(ref_directos))
    raw_offers, n_dupes = dedupe_boards_vs_directos(raw_offers,
                                                    referencia=ref_directos)
    logger.info(f"Dedupe: {prev_total} crudas -> {len(raw_offers)} deduplicadas "
                f"({n_dupes} copias de job boards descartadas por existir ya "
                f"en providers directos)")

    # --- 3. Gate (Fecha Y relevancia) -> staging + curado ---
    prog.fase("Gate (fecha + relevancia) y reportes")
    newly, _, _ = await _aplicar_gate(db, profile, engine, raw_offers, dias, min_score)
    # Vigencia: lo vencido no se acumula (fecha de vencimiento del portal o,
    # si no la publica, ventana de publicacion). Va despues del gate para
    # cubrir tambien lo descartado que acaba de entrar a jobs_raw.
    await db.purge_expired(dias)
    # Duplicados exactos marcados con dup_of en ambas tablas (no se borra nada).
    await db.marcar_duplicados_exactos()
    # Puestos fuera del perfil -> exclusion permanente; veredictos restaurados.
    res_excl = await excluir_y_purgar(db, profile)
    await db.resincronizar_veredictos()
    # sufijo de alcance: dos corridas del mismo dia no pisan su CSV.
    _reportes(newly, "" if solo == RUN_TODOS else f"_{solo}")
    return _resumen_progreso(prog, len(newly))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="JobPulse CR - scraper de empleo")
    parser.add_argument("--fresh", action="store_true", help="Vacia la base de datos antes de la corrida")
    parser.add_argument("--sin-expand", action="store_true",
                        help="Desactiva la expansion semantica de tematicas (default: activa con perfil)")
    parser.add_argument("--dias", type=int, default=DEFAULT_DIAS,
                        help=f"Ventana de fechas del gate (default: {DEFAULT_DIAS})")
    parser.add_argument("--score-min", type=float, default=None,
                        help="Umbral de relevancia semantica 0-1 (default: "
                             "calibrado por modelo de embeddings; 0.26 con "
                             "EmbeddingGemma, 0.30 con mpnet)")
    parser.add_argument("--refiltrar", action="store_true",
                        help="Reaplica el gate sobre jobs_raw sin re-scrapear")
    parser.add_argument("--preflight", action="store_true",
                        help="Dry-run: publica el plan de corrida y el estado de "
                             "credenciales en la GUI, sin navegador ni red")
    parser.add_argument("--solo", choices=list(RUN_CHOICES), default=RUN_TODOS,
                        help="Separa la corrida en dos alcances: 'ats' solo "
                             "portales de empresa (Workday, Greenhouse, iCIMS, "
                             "Rippling...), 'boards' los job boards (Indeed, "
                             "LinkedIn) + los 3 portales estatales (ANE/Brete, "
                             "CINDE, Procomer), 'todos' (default) ambos como "
                             "hasta ahora. Sin efecto con --refiltrar, que "
                             "siempre reconstruye todo jobs_raw")
    # --- IA externa (a demanda; nunca dentro de una corrida normal) ---
    parser.add_argument("--ia", action="store_true",
                        help="Re-evalúa el crudo (jobs_raw) con la IA fuerte "
                             "(Gemini/NVIDIA). No cambia qué entra a jobs: "
                             "anota score+razón y guarda el historial.")
    parser.add_argument("--ia-cv", metavar="RUTA", default="",
                        help="Lee un CV (pdf/docx/txt/md), crea el perfil "
                             "propuesto y evalúa/adapta el CV. Deja el informe "
                             "en data/usuario/cv_ia.md (no toca el perfil sin "
                             "--ia-guardar).")
    parser.add_argument("--ia-puesto", default="",
                        help="Puesto objetivo para --ia-cv (ej. \"Soporte Técnico\")")
    parser.add_argument("--ia-guardar", action="store_true",
                        help="Con --ia-cv: escribe data/usuario/profile.json "
                             "(con backup) usando el CV como fuente de verdad.")
    parser.add_argument("--ia-forzar", action="store_true",
                        help="Ignora la caché de la IA y repite las consultas "
                             "(consume cuota).")
    parser.add_argument("--ia-min", type=float, default=DEFAULT_IA_MIN,
                        help=f"Score IA (0-100) desde el cual una vacante "
                             f"'aplica' (default: {DEFAULT_IA_MIN:.0f}).")
    parser.add_argument("--ia-max", type=int, default=0,
                        help="Tope de vacantes a evaluar en esta ejecución "
                             "(0 = IA_MAX_VACANTES del .env).")
    parser.add_argument("--ia-modelo", default="", metavar="MODELO",
                        help="Modelo para esta ejecución (pisa IA_MODEL del "
                             ".env). Ver --ia-listar-modelos.")
    parser.add_argument("--ia-listar-modelos", action="store_true",
                        help="Lista los modelos disponibles del proveedor y "
                             "sale (no scrapea ni toca la base).")
    parser.add_argument("--ia-reporte", action="store_true",
                        help="Calibra la IA local contra las evaluaciones IA "
                             "guardadas y sugiere --score-min (no aplica cambios).")
    # --- Mantenimiento y veredictos (sin navegador ni red) ---
    parser.add_argument("--podar", action="store_true",
                        help="Mantenimiento de la base sin scraping: infiere "
                             "sectores faltantes, excluye (tombstone + DELETE) "
                             "los puestos fuera del perfil (dominio + score "
                             "bajo el piso + sector), y aplica TTL (vencidas, "
                             "fuera de ventana, sin fecha).")
    parser.add_argument("--decidir", nargs=2, metavar=("JOB_ID", "VEREDICTO"),
                        help="Veredicto manual del usuario: apta (verde, lista "
                             "final) o descartada (amarilla, oculta hasta el "
                             "TTL). Pisa el veredicto de la IA; es final.")
    parser.add_argument("--nota", default="",
                        help="Razón/nota opcional que acompaña a --decidir.")
    args = parser.parse_args()
    if args.ia and args.ia_cv:
        parser.error("--ia y --ia-cv son modos distintos; elegí uno.")
    if args.ia_guardar and not args.ia_cv:
        parser.error("--ia-guardar solo tiene sentido con --ia-cv RUTA.")
    if args.podar and args.decidir:
        parser.error("--podar y --decidir son modos distintos; elegí uno.")
    if args.ia_listar_modelos:
        listar_modelos_ia(args.ia_modelo)
    else:
        asyncio.run(main(
            fresh=args.fresh, sin_expand=args.sin_expand,
            dias=args.dias, min_score=args.score_min, re_filtrar=args.refiltrar,
            preflight=args.preflight, solo=args.solo,
            ia=args.ia, ia_cv=args.ia_cv, ia_puesto=args.ia_puesto,
            ia_guardar=args.ia_guardar, ia_forzar=args.ia_forzar,
            ia_min=args.ia_min, ia_max=args.ia_max,
            ia_reporte=args.ia_reporte, ia_modelo=args.ia_modelo,
            podar=args.podar, decidir=args.decidir, nota=args.nota,
        ))
