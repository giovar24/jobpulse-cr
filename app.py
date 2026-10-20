"""Dashboard Streamlit de JobPulse CR: mejores opciones del candidato + busqueda libre.

Ejecutar:  streamlit run app.py
"""
import asyncio
import os
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.bootstrap import ensure_workspace
from core.ats import rank_with_ats
from core.database import JobDatabase
from core.profile import CandidateProfile, DEFAULT_PROFILE_PATH
from core.search_engine import SemanticSearchEngine
from core import credenciales as creds_store
from core.progreso import (corriendo, detener, leer_estado, tail as log_tail,
                           tail_stdout as log_tail_stdout)
from providers.source_groups import GROUP_DIRECTOS, source_group

from dotenv import load_dotenv

ensure_workspace()   # data/, profile.json generico y .env si faltan (equipo nuevo)
load_dotenv()        # HF_TOKEN y demas (el dashboard tambien decide el modelo)

# Job boards (agregadores — pueden republicar el mismo puesto que el ATS de
# la empresa: por eso existe el dedupe cross-source en main/core.dedupe).
BOARDS_SOURCES = ["indeed", "linkedin", "ane", "procomer", "cinde"]
# Providers directos al ATS del employer (fuente canonica del puesto).
DIRECTOS_SOURCES = ["workday", "phenom", "tp", "greenhouse", "icims",
                    "smartrecruiters", "successfactors", "rippling",
                    "ats_ligeros"]
# Sitios del employer que requieren sesion de navegador (Cloudflare/login):
# providers/login/, con pending_login.json + hacer_login.py.
LOGIN_SOURCES = ["login_sites"]

SOURCES = BOARDS_SOURCES + DIRECTOS_SOURCES + LOGIN_SOURCES

GRUPO_TXT = {"boards": "Job board (agregador)", "directos": "Portal de la empresa"}


def grupo_de(source: str) -> str:
    """boards | directos (un source desconocido se trata como board)."""
    return source_group(source)

st.set_page_config(page_title="JobPulse CR", page_icon="💼", layout="wide")
st.title("💼 JobPulse CR — mejores opciones para tu perfil")


def _frescura_datos():
    """Banner de frescura: cuanto hace que el sistema no scrapea."""
    import sqlite3
    db = Path("data/jobs.db")
    if not db.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        (val,) = con.execute("select max(created_at) from jobs_raw").fetchone()
        con.close()
        return datetime.fromisoformat((val or "")[:19])
    except Exception:
        return None


_ult = _frescura_datos()
if _ult:
    _h = (datetime.now() - _ult).total_seconds() / 3600
    _txt = (f"hace un rato" if _h < 1 else
            f"hace {int(_h)} h" if _h < 24 else
            f"hace {int(_h // 24)} día(s)")
    if _h >= 24:
        st.warning(f"🥱 Datos desactualizados: última corrida **{_txt}** "
                   f"({_ult:%d/%m %H:%M}). Corre `python main.py` o "
                   f"`jobpulse.cmd` para refrescar.")
    else:
        st.caption(f"✅ Datos frescos — última corrida {_txt} ({_ult:%d/%m %H:%M}).")
del _ult


async def _query_jobs(days=None):
    db = JobDatabase()
    return await db.get_all_jobs(days=days)


@st.cache_data(ttl=120, show_spinner="Consultando la base de datos...")
def load_jobs(days=None, fuente=None, grupo=None):
    jobs = asyncio.run(_query_jobs(days))
    if fuente:
        jobs = [j for j in jobs if (j.get("source") or "").lower() == fuente.lower()]
    if grupo:
        jobs = [j for j in jobs if grupo_de(j.get("source") or "") == grupo]
    return jobs


@st.cache_resource(show_spinner=False)
def get_engine():
    return SemanticSearchEngine()


def to_df(results, with_ats=False):
    rows = []
    for r in results:
        brk = r.get("secciones") or {}
        a = r.get("ats") or {}
        row = {
            "Match %": round(r["score"] * 100, 1),
            "titulo %": round(brk.get("titulo", 0) * 100),
            "skills %": round(brk.get("skills", 0) * 100),
            "tema %": round(brk.get("tema", 0) * 100),
            "Fuente": r.get("source", ""),
            "Grupo": GRUPO_TXT.get(grupo_de(r.get("source", "")), ""),
            "Puesto": r.get("title", ""),
            "Empresa": r.get("company") or "-",
            "Ubicacion": r.get("location") or "-",
            "Publicada": r.get("date_published") or "",
            "Link": r.get("link", ""),
        }
        if with_ats and a:
            row = {
                "Total %": r.get("combined", 0),
                "ATS %": a.get("score"),
                "Veredicto": a.get("verdict", ""),
                "Semantico %": row["Match %"],
                "ATS skills %": (a.get("components") or {}).get("skills"),
                "ATS titulo %": (a.get("components") or {}).get("titulo"),
                "Faltan": ", ".join(a.get("missing") or []) or "-",
                "Fuente": row["Fuente"],
                "Grupo": row["Grupo"],
                "Puesto": row["Puesto"],
                "Empresa": row["Empresa"],
                "Ubicacion": row["Ubicacion"],
                "Publicada": row["Publicada"],
                "Link": row["Link"],
            }
        rows.append(row)
    return pd.DataFrame(rows)


# ------------------------------------------------------------------- sidebar
with st.sidebar:
    st.header("🔎 Filtros")
    todas = st.checkbox("Todas las vacantes (sin límite de fecha)", value=False)
    days = None if todas else st.slider("Vacantes de los últimos", 1, 60, 30, format="%d días")
    grupo = st.selectbox(
        "Grupo de la fuente", ["todos", "boards", "directos"],
        format_func=lambda g: {"todos": "Todos (boards + portales)",
                               "boards": GRUPO_TXT["boards"],
                               "directos": GRUPO_TXT["directos"]}[g],
        help="Boards = agregadores (Indeed, LinkedIn, ANE, Procomer, CINDE). "
             "Portales de la empresa = ATS directo del empleador (Workday, "
             "iCIMS, Greenhouse, Phenom, Rippling...). El dedupe cross-source "
             "conserva la versión del portal.")
    grupo_f = None if grupo == "todos" else grupo
    opciones = ["todas"] + [s for s in SOURCES
                            if not grupo_f or grupo_de(s) == grupo_f]
    fuente = st.selectbox("Portal", opciones)
    source = None if fuente == "todas" else fuente
    top = st.slider("Mostrar", 5, 50, 15)

    ruta = st.text_input("Perfil del candidato", value=str(DEFAULT_PROFILE_PATH))
    profile = CandidateProfile.load(ruta)
    if profile:
        st.success(f"Perfil: {profile.nombre or profile.path.name}")
        if profile.filtros.get("dias") and not todas and days:
            days = min(days, profile.filtros["dias"])
        with st.expander("🪪 Ficha ATS"):
            st.write(f"**Experiencia:** {profile.experiencia_anios or '—'} años")
            st.write(f"**Educación:** {profile.educacion_nivel or '—'}")
            if profile.seniority:
                st.write(f"**Seniority:** {profile.seniority}")
            if profile.certificaciones:
                st.write("**Certificaciones:** " + ", ".join(profile.certificaciones))
            if profile.idiomas:
                st.write("**Idiomas:** " + ", ".join(
                    f"{i['idioma']} ({i['nivel']})" for i in profile.idiomas
                    if isinstance(i, dict)) or "—")
            st.caption("Campos extraídos por cv2profile.py del CV; alimentan el score ATS.")

    if st.button("🔄 Reindexar vacantes nuevas", width="stretch"):
        st.cache_data.clear()
        st.success("Cache limpio: las vacantes nuevas se indexaran en la proxima consulta.")

    configured = os.getenv("EMBEDDING_MODEL", "google/embeddinggemma-300m")
    with st.expander("Modelo"):
        st.caption(f"Configurado: `{configured}`")
        try:
            eng = get_engine()
            if eng and eng._model is not None:
                st.caption(f"Activo: `{eng.model_name}`")
            elif "HF_TOKEN" not in os.environ:
                st.caption("EmbeddingGemma es gated; sin `HF_TOKEN` en `.env` se usa el "
                           "fallback multilingue abierto al primer uso.")
        except Exception:
            pass

# ------------------------------------------------------------------- tabs
tab_rank, tab_free, tab_run, tab_cred, tab_ses = st.tabs(
    ["🎯 Ranking de mejores opciones", "🔍 Búsqueda libre", "🚀 Corrida",
     "🔑 Credenciales", "🔐 Sesiones"])

with tab_rank:
    if profile:
        p_days = None if todas else days
        if st.button("🎯 Calcular ranking del perfil", type="primary", width="stretch"):
            engine = get_engine()
            with st.spinner("Consultando DB e indexando..."):
                engine.load(load_jobs(p_days, source, grupo_f))
            with st.spinner("Calculando ranking (semantico + ATS)..."):
                results = engine.search_profile(profile, top_k=60)
                results = rank_with_ats(profile.to_dict(), results, engine)
            df = to_df(results, with_ats=True)
            st.subheader(f"Mejores opciones para: {profile.nombre or 'Candidato'}")
            st.caption("Total = 60% similitud semántica + 40% score ATS "
                       "(skills 40% · título 30% · experiencia 15% · educación 7.5% · idiomas 7.5%). "
                       "🟢 ≥85 fuerte · 🟡 70-84 ajustar keywords · 🔴 <70 débil.")
            st.dataframe(
                df.head(top),
                width="stretch",
                hide_index=True,
                column_config={
                    "Link": st.column_config.LinkColumn("Link"),
                },
            )
    else:
        st.info(f"No se encontro un perfil en `{ruta}`. Revisa la ruta.")

with tab_free:
    query = st.text_input("Consulta en lenguaje natural",
                          placeholder="ej.: mesa de ayuda bilingue remoto")
    if st.button("🔍 Buscar") and query.strip():
        engine = get_engine()
        jobs = load_jobs(days, source, grupo_f)
        engine.load(jobs)
        with st.spinner("Buscando..."):
            results = engine.search(query, top_k=top)
        if results:
            st.dataframe(
                to_df(results),
                width="stretch",
                hide_index=True,
                column_config={"Link": st.column_config.LinkColumn("Link")},
            )
            for r in results[:3]:
                with st.expander(f"{r['title']} — {(r.get('company') or '')}"):
                    st.write((r.get("description") or "(sin descripción)")[:1000])
                    if r.get("requirements"):
                        st.caption("Requisitos: " + r["requirements"][:500])
        else:
            st.warning("Sin resultados.")

with tab_run:
    st.subheader("🚀 Corrida de scraping")
    st.caption(
        "El dashboard se abre ANTES del scraping: esta pestaña muestra en "
        "vivo qué está haciendo cada proveedor y separa **job boards** "
        "(agregadores: Indeed, LinkedIn, ANE...) de **portales de la "
        "empresa** (ATS directo: Workday, iCIMS, Greenhouse...). "
        "`jobpulse.cmd` arranca esta pantalla primero y después corre el "
        "scraping — por eso nunca tarda en aparecer.")

    def _arrancar(preflight: bool = False):
        """Lanza `python main.py` como proceso hijo; la GUI solo observa
        data/corrida_estado.json (asi funciona aunque reinicies Streamlit)."""
        import subprocess
        import sys
        raiz = Path(__file__).resolve().parent
        args = [sys.executable, "-u", "-X", "utf8", "main.py"]
        if preflight:
            args.append("--preflight")
        out_path = raiz / "data" / "corrida_stdout.log"
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            fh = open(out_path, "ab")
        except OSError:
            fh = None
        subprocess.Popen(
            args, cwd=str(raiz), stdin=subprocess.DEVNULL,
            stdout=(fh if fh is not None else subprocess.DEVNULL),
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW)
        if fh:
            fh.close()

    @st.fragment(run_every="2s")
    def panel_corrida():
        est = leer_estado()
        estado = est.get("estado", "nunca")
        vivo = corriendo()

        _ICONOS = {"corriendo": "🟢 Corriendo", "ok": "✅ Completada",
                   "preflight": "🧪 Preflight OK (sin red)",
                   "error": "🔴 Error", "detenida": "⏹ Detenida",
                   "interrumpida": "⚠️ Interrumpida", "nunca": "· Sin corridas"}
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Estado", _ICONOS.get(estado, estado))
        c2.metric("Inicio", (est.get("inicio_ts") or "—")[11:19] if est.get("inicio_ts") else "—")
        c3.metric("Job boards (crudas)", (est.get("encontradas") or {}).get("boards", 0))
        c4.metric("Portales de empresa (crudas)", (est.get("encontradas") or {}).get("directos", 0))

        total = int(est.get("total_fases") or 0)
        paso = int(est.get("paso") or 0)
        if total:
            st.progress(min(1.0, paso / total),
                        text=f"Paso {min(paso, total)}/{total}")
        grupo = est.get("grupo") or ""
        chip = f"`{GRUPO_TXT.get(grupo, 'sistema')}`" if grupo else ""
        if estado == "corriendo":
            st.markdown(f"**▶ {est.get('fase_actual', '')}** {chip}"
                        + (f" — {est.get('detalle')}" if est.get("detalle") else ""))
        elif estado == "ok":
            r = est.get("resumen") or {}
            st.success(f"Corrida completada a las {(est.get('fin_ts') or '')[11:19]} — "
                       f"{r.get('crudas_boards', 0)} crudas en boards, "
                       f"{r.get('crudas_directos', 0)} en portales de empresa, "
                       f"**{r.get('nuevas', 0)} vacantes nuevas** tras el gate.")
        elif estado == "preflight":
            st.info("Preflight OK: plan de corrida y credenciales reportados "
                    "sin tocar la red. Revisa el log.")
        elif estado == "error":
            st.error(f"La corrida terminó con error: "
                     f"{(est.get('resumen') or {}).get('motivo', '') or 'ver log'}")
        elif estado == "interrumpida":
            st.warning("La corrida se interrumpió (el proceso ya no existe). "
                       "Puede volver a lanzarla.")
        elif estado == "detenida":
            st.warning("Corrida detenida a mano.")

        b1, b2, b3, b4 = st.columns([2, 2, 2, 3])
        if b1.button("▶️ Iniciar scraping", type="primary", disabled=vivo,
                     width="stretch"):
            _arrancar()
            st.toast("Scraping lanzado: el progreso aparece aquí en unos segundos.")
        if b2.button("🧪 Preflight (sin red)", disabled=vivo, width="stretch",
                     help="Dry-run: publica el plan de corrida y qué "
                          "credenciales faltan, sin navegador ni requests."):
            _arrancar(preflight=True)
            st.toast("Preflight lanzado.")
        if b3.button("⏹ Detener", disabled=not vivo, width="stretch"):
            if detener():
                st.toast("Corrida detenida.")
            else:
                st.error("No se pudo detener (quizás ya terminó).")
        if b4.button("🔄 Reindexar vacantes nuevas", width="stretch"):
            st.cache_data.clear()
            st.toast("Cache limpio: se reindexa en la próxima consulta.")

        with st.expander("📝 Log de la corrida (últimas líneas)"):
            lineas = log_tail(120)
            if lineas:
                st.code("".join(lineas), language="log")
            else:
                st.caption("Sin log todavía (`data/corrida.log`).")
            crudo = log_tail_stdout(40)
            if crudo:
                st.caption("Salida cruda del proceso (`data/corrida_stdout.log`):")
                st.code("".join(crudo[-15:]), language="log")

    panel_corrida()


with tab_cred:
    st.subheader("🔑 Credenciales de los sitios que piden password")
    st.caption(
        "Esta es **la lista** de sitios que exigen password/sesión, "
        "construida con los seeds fijos + lo que los providers detectan en "
        "cada corrida. Aquí (o con `python credenciales.py --set \"Empresa\"`) "
        "pones tu usuario/password: se guardan SOLO en esta máquina, en "
        "`data/empresas/credenciales.json`, que está en `.gitignore` — "
        "**nunca viaja a git**. Si el login automático falla (CAPTCHA/2FA), "
        "el scraper cae al login manual de la pestaña Sesiones sin cortar "
        "la corrida.")

    res = creds_store.resumen()
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Sitios en la lista", res["total"])
    m2.metric("Obligatorios", res["obligatorios"])
    m3.metric("Con credencial", res["con_credencial"])
    m4.metric("Pendientes sin resolver", res["pendientes"])

    sitios = creds_store.lista_sitios_login()
    st.dataframe(
        pd.DataFrame([{
            "Empresa": s["empresa"],
            "Grupo": s["grupo_label"],
            "Obligatorio": "Sí" if s["obligatorio"] else "No",
            "Credencial": (s["usuario"] + (" (.env)" if s.get("password_fuente") == ".env" else "")) if s["tiene_credencial"] else "— falta —",
            "Auto-login": "ON" if s.get("auto_login") else "off",
            "Pendiente": s.get("pendiente") or "",
            "Motivo": (s.get("motivo") or "")[:120],
        } for s in sitios]),
        width="stretch", hide_index=True)

    if any(not s["tiene_credencial"] and s["obligatorio"] for s in sitios):
        faltan = ", ".join(s["empresa"] for s in sitios
                           if s["obligatorio"] and not s["tiene_credencial"])
        st.warning(f"Falta credencial obligatoria en: **{faltan}** — sin ella "
                   f"ese proveedor se omite con un aviso.")

    with st.form("form_cred", clear_on_submit=True):
        st.markdown("**Agregar / actualizar una credencial**")
        nombres = [s["empresa"] for s in sitios]
        c1, c2 = st.columns(2)
        sel = c1.selectbox("Sitio", nombres + ["Otro (escribir a mano)"])
        otra = c2.text_input("Nombre del otro sitio")
        usuario = st.text_input("Usuario")
        password = st.text_input("Password", type="password")
        url_n = st.text_input("URL de login (opcional; obligatoria si el sitio "
                              "entra por el provider de navegador)")
        auto_ok = st.checkbox("Login automático en la corrida (si falla, "
                              "fallback manual)", value=True)
        if st.form_submit_button("💾 Guardar credencial", type="primary"):
            nombre = (otra or "").strip() or sel
            if not usuario.strip() or not password:
                st.error("Faltan usuario o password.")
            else:
                creds_store.upsert_credencial(nombre, usuario, password,
                                              url=url_n, auto_login=auto_ok)
                st.success(f"Credencial de {nombre} guardada localmente.")
                st.rerun()

    st.markdown("**Acciones por sitio**")
    for s in sitios:
        k = s["empresa"].casefold().replace(" ", "_")
        c1, c2, c3, c4 = st.columns([3, 2, 2, 3])
        c1.write(f"{s['empresa']}{' 🔒' if s['obligatorio'] else ''}")
        if c2.button(("🟢 Auto-login ON" if s.get("auto_login")
                      else "⚪ Auto-login OFF"), key=f"al_{k}",
                     disabled=not s["tiene_credencial"] and not s.get("auto_login")):
            creds_store.marcar_auto_login(s["empresa"], not s.get("auto_login"))
            st.rerun()
        if s["tiene_credencial"] and c3.button("🗑 Borrar", key=f"bd_{k}"):
            creds_store.borrar_credencial(s["empresa"])
            st.rerun()
        if s.get("url"):
            c4.markdown(f"[Abrir sitio]({s['url']})")


with tab_ses:
    from core.sesiones import (abrir_en_brave, sitios_con_sesion,
                               verificar_sesion, quitar_pendiente)
    st.subheader("Sitios que requieren login / sesión de navegador")
    st.caption(
        "**El navegador sigue siendo la bóveda preferida**: abrís cada sitio "
        "en tu Brave, te logueás una vez (con tu 2FA normal) y la sesión queda "
        "en tu perfil — el mismo con el que el scraper corre. Esto vale para "
        "los sitios que no piden password en el formulario o donde prefieras "
        "la sesión real. Si un sitio sí exige password, la pestaña "
        "**🔑 Credenciales** te deja ponerla localmente (solo en tu máquina) "
        "y activar el login automático; si ese login falla por CAPTCHA/2FA, "
        "el scraper cae aquí al login manual. Los sitios con pendiente se "
        "saltan en la corrida sin romper nada.")
    sitios = sitios_con_sesion()
    for s in sitios:
        estado = "🟡 Pendiente" if s.get("pendiente") else (
            "⭐ Recomendado" if s.get("recomendado") else "🟢 Configurado")
        with st.expander(f"{estado} — {s['empresa']} ({s.get('grupo','')})"):
            if s.get("pendiente"):
                st.warning(f"**Pendiente**: {s['pendiente']}"
                           + (f" — {s.get('pendiente_ts','')}"
                              if s.get("pendiente_ts") else ""))
            if s.get("porque"):
                st.caption(s["porque"])
            if s.get("url"):
                st.caption(f"URL: `{s['url']}`")
                c1, c2, c3 = st.columns(3)
                if c1.button("🌐 Abrir en Brave", key=f"ab_{s['empresa']}"):
                    if abrir_en_brave(s["url"]):
                        st.info("Brave abierto. Logueate, cerrás la pestaña "
                                "y volvés aquí para verificar.")
                    else:
                        st.error("No encontré brave.exe — configura "
                                 "BRAVE_BIN en .env")
                if c2.button("✅ Verificar sesión",
                             key=f"vf_{s['empresa']}"):
                    url_v = s.get("url_check") or s["url"]
                    with st.spinner("Verificando (Brave se reinicia "
                                    "unos segundos)..."):
                        res = verificar_sesion(url_v)
                    if res["ok"]:
                        st.success(f"Sesión OK — título: {res['titulo']}")
                        if s.get("pendiente"):
                            quitar_pendiente(s["empresa"])
                            st.rerun()
                    else:
                        st.error(f"Sin sesión aún — {res['motivo']} "
                                 f"(título: {res['titulo'] or '?'})")
                if c3.button("🧹 Marcar resuelto",
                             key=f"ok_{s['empresa']}"):
                    if quitar_pendiente(s["empresa"]):
                        st.rerun()

st.caption("Datos: data/jobs.db — 100% local. Ranking por similitud de coseno con embeddings.")
