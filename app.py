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
from core.cv_analysis import merge_profile
from core.search_engine import SemanticSearchEngine
from core import credenciales as creds_store
from core.progreso import (corriendo, detener, leer_estado, tail as log_tail,
                           tail_stdout as log_tail_stdout)
from providers.source_groups import (GROUP_ESTATALES, GROUP_OTROS, grupo_publico)

from dotenv import load_dotenv

ensure_workspace()   # data/, profile.json generico y .env si faltan (equipo nuevo)
load_dotenv()        # HF_TOKEN y demas (el dashboard tambien decide el modelo)

# Grupos PUBLICOS (2): el trio estatal y todo lo demas. La distincion
# agregador-vs-ATS (boards/directos) es interna: la resuelve
# providers/source_groups.py para el dedupe cross-source y el alcance de la
# corrida (--solo ats|boards), pero no se filtra desde la UI.
PORTALES_ESTATALES = ["ane", "cinde", "procomer"]
OTRAS_FUENTES = ["indeed", "linkedin", "workday", "phenom", "tp", "greenhouse",
                 "icims", "smartrecruiters", "successfactors", "rippling",
                 "ats_ligeros", "login_sites"]

SOURCES = PORTALES_ESTATALES + OTRAS_FUENTES

GRUPO_RESTO = "Resto (agregadores + ATS)"
GRUPO_ESTATALES = "Portal estatal (ANE/CINDE/Procomer)"
GRUPO_TXT = {GROUP_ESTATALES: GRUPO_ESTATALES, GROUP_OTROS: GRUPO_RESTO}


def grupo_de(source: str) -> str:
    """Grupo publico de una fuente: estatales | otros."""
    return grupo_publico(source)

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


# ------------------------------------------------------- helpers del editor
def _df_lista(col: str, items) -> pd.DataFrame:
    """Columna unica para editar una lista de strings (titulos/skills/...).

    Se fuerza dtype object: con lista vacia pandas crearia float64 y
    data_editor se queja al agregar la primera fila.
    """
    return pd.DataFrame({col: pd.Series([str(i) for i in (items or [])],
                                        dtype="object")},
                        columns=[col])


def _celda(v):
    """Texto de una celda de data_editor: '' si esta vacia (None/NaN/espacio).

    data_editor devuelve None en object y NaN en float cuando se borra una
    fila; str(None) es "None" y str(nan) es "nan", que NO se pueden descartar
    comparando texto por eso se chequea el valor crudo antes de castear.
    """
    if v is None:
        return ""
    try:
        if pd.isna(v):
            return ""
    except (TypeError, ValueError):
        pass
    return str(v).strip()


def _lista_de_df(df: pd.DataFrame, col: str):
    """Vuelta de data_editor a lista limpia (celdas vacias y NaN afuera)."""
    if col not in df.columns:
        return []
    return [t for t in (_celda(v) for v in df[col].tolist()) if t]


_TEMAS_COLS = ["Puesto", "Puesto (EN)", "Ubicación"]
_IDIOMAS_COLS = ["Idioma", "Nivel"]


def _df_temas(temas) -> pd.DataFrame:
    filas = [{"Puesto": t.get("puesto", ""), "Puesto (EN)": t.get("puesto_en", ""),
              "Ubicación": t.get("ubicacion", "")}
             for t in (temas or []) if isinstance(t, dict)]
    return pd.DataFrame(filas, columns=_TEMAS_COLS)


def _temas_de_df(df: pd.DataFrame):
    out = []
    for _, r in df.iterrows():
        p = _celda(r.get("Puesto"))
        if not p:
            continue
        out.append({"puesto": p,
                    "puesto_en": _celda(r.get("Puesto (EN)")),
                    "ubicacion": _celda(r.get("Ubicación"))})
    return out


def _df_idiomas(idiomas) -> pd.DataFrame:
    filas = [{"Idioma": i.get("idioma", ""), "Nivel": i.get("nivel", "")}
             for i in (idiomas or []) if isinstance(i, dict)]
    return pd.DataFrame(filas, columns=_IDIOMAS_COLS)


def _idiomas_de_df(df: pd.DataFrame):
    out = []
    for _, r in df.iterrows():
        idi = _celda(r.get("Idioma"))
        if not idi:
            continue
        out.append({"idioma": idi, "nivel": _celda(r.get("Nivel"))})
    return out


def _delta_listas(viejo, nuevo):
    """(quitados, agregados) entre dos listas de strings, para el resumen."""
    v = [str(x).strip() for x in (viejo or [])]
    n = [str(x).strip() for x in (nuevo or [])]
    return ([x for x in v if x not in n], [x for x in n if x not in v])


def _limpiar_widgets_editor():
    """Borra el estado de los widgets del editor (on_click: corre ANTES de
    instanciarlos, que es lo unico momento en que session_state se puede borrar).
    Al re-render los widgets vuelven a leer el archivo de disco."""
    for k in list(st.session_state):
        if k.startswith("ed_"):
            del st.session_state[k]


def _perfil_vacio(datos: dict) -> bool:
    """Un perfil sin títulos, skills ni temas no sirve para nada: ni para
    puntuar (secciones()) ni para buscar (to_searches()). Se niega el guardado."""
    return not (datos.get("titulos") or datos.get("skills") or datos.get("temas"))


def _escalar(v):
    """Normaliza un valor escalar del perfil para comparar cambios.

    Sin esto, una clave ausente (None) contra una celda vacia ("") se lee
    como "cambio" y deja el boton de guardar siempre habilitado aunque el
    usuario no haya tocado nada. Tambien 5.0 vs 5.
    """
    if v is None:
        return ""
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return v


def _aviso_editor(texto: str):
    """Mensaje que debe sobrevivir a un st.rerun() (el exito del guardado)."""
    st.session_state["_aviso_editor"] = texto


def _drenar_aviso_editor():
    txt = st.session_state.pop("_aviso_editor", None)
    if txt:
        st.success(txt)


# ------------------------------------------------------------------- sidebar
with st.sidebar:
    st.header("🔎 Filtros")
    todas = st.checkbox("Todas las vacantes (sin límite de fecha)", value=False)
    days = None if todas else st.slider("Vacantes de los últimos", 1, 60, 30, format="%d días")
    grupo = st.selectbox(
        "Grupo de la fuente", ["todos", "estatales", "otros"],
        format_func=lambda g: {"todos": "Todos (estatales + resto)",
                               "estatales": GRUPO_TXT["estatales"],
                               "otros": GRUPO_TXT["otros"]}[g],
        help="Dos grupos: **portales estatales** = los 3 oficiales de CR "
             "(ANE/Brete, CINDE y Procomer — talento.procomer.com) y "
             "**el resto** = agregadores comerciales (Indeed, LinkedIn) + "
             "ATS directo del empleador (Workday, iCIMS, Greenhouse, "
             "Phenom, Rippling...). El dedupe cross-source, que decide "
             "conservar la versión del portal, sigue distinguiendo "
             "agregador de ATS por dentro.")
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
tab_rank, tab_free, tab_perfil, tab_run, tab_cred, tab_ses = st.tabs(
    ["🎯 Ranking de mejores opciones", "🔍 Búsqueda libre", "✏️ Perfil",
     "🚀 Corrida", "🔑 Credenciales", "🔐 Sesiones"])

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

with tab_perfil:
    import json as _json

    from core.profile import backups_perfil, guardar_perfil

    st.subheader("✏️ Editar perfil del candidato")
    st.caption(
        "El perfil (`data/profile.json`) es lo que decide **qué busca** y "
        "**cómo puntúa** el sistema. El extractor automático puede meter "
        "ruido (un título que no tenés, un skill inflado, un tema que no "
        "querés); aquí lo corregís a mano. Cada guardado hace **backup** "
        "automático del archivo anterior, y lo que cambies aplica ya en el "
        "próximo ranking. Lo que ya estaba capturado no se re-evalúa solo: "
        "para eso está `python main.py --refiltrar`.")
    _drenar_aviso_editor()

    ruta_p = Path(ruta)
    if not ruta_p.exists():
        st.info(f"No existe todavía un perfil en `{ruta_p}`. Usá "
                "`python cv2profile.py MiCV.pdf --save` para crearlo desde un CV.")
    else:
        try:
            datos = _json.loads(ruta_p.read_text(encoding="utf-8"))
        except (OSError, _json.JSONDecodeError) as exc:
            st.error(f"Perfil ilegible (`{ruta_p}`): {exc}")
            datos = None

        if isinstance(datos, dict):
            # Vista plana para los widgets: el perfil guardado usa el esquema
            # Harvard/ATS (cv + busqueda), pero Streamlit edita los campos de
            # busqueda y el CV estructurado se preserva al fusionar en guardar.
            perfil_orig = datos
            datos = {k: v for k, v in CandidateProfile(datos).to_dict().items()
                     if k not in ("cv", "busqueda")}
            # Firma del archivo: si cambia por fuera (cv2profile, git, etc.)
            # los widgets se resetean y muestran lo que realmente hay en disco.
            firma = f"{ruta_p.stat().st_mtime_ns}_{len(ruta_p.read_bytes())}"

            nombre_nuevo = st.text_input(
                "Nombre", value=str(datos.get("nombre") or ""),
                placeholder="Como figura en tu CV",
                key=f"ed_nombre_{firma}")

            with st.expander("🪪 Datos ATS (experiencia, educación, seniority)"):
                a1, a2, a3 = st.columns(3)
                exp_anios = a1.number_input(
                    "Experiencia (años)", min_value=0.0, max_value=40.0, step=0.5,
                    value=float(datos.get("experiencia_anios") or 0),
                    key=f"ed_exp_{firma}")
                edu_nivel = a2.text_input(
                    "Educación", value=str(datos.get("educacion_nivel") or ""),
                    placeholder="universitario / tecnico medio / ...",
                    key=f"ed_edu_{firma}")
                sen = a3.text_input(
                    "Seniority", value=str(datos.get("seniority") or ""),
                    placeholder="junior / mid / senior",
                    key=f"ed_sen_{firma}")

            st.markdown("**Títulos / palabras clave del puesto**")
            st.caption("Lo que el buscador usa para la sección *título* del "
                       "match. Borra las filas que no te representen.")
            df_t = st.data_editor(
                _df_lista("titulo", datos.get("titulos")),
                num_rows="dynamic", width="stretch", hide_index=True,
                key=f"ed_titulos_{firma}")

            st.markdown("**Skills**")
            st.caption("Alimenta *skills* del match y el score ATS. "
                       "Sin acentos ni mayúsculas raras: el sistema normaliza.")
            df_s = st.data_editor(
                _df_lista("skill", datos.get("skills")),
                num_rows="dynamic", width="stretch", hide_index=True,
                key=f"ed_skills_{firma}")

            st.markdown("**Temas de búsqueda (lo que scrapea)**")
            st.caption("Cada tema es una búsqueda real en los portales. "
                       "`Puesto (EN)` es lo que se le manda a LinkedIn/Indeed.")
            df_te = st.data_editor(
                _df_temas(datos.get("temas")),
                num_rows="dynamic", width="stretch", hide_index=True,
                column_config={"Puesto": st.column_config.TextColumn(required=True)},
                key=f"ed_temas_{firma}")

            c1, c2 = st.columns(2)
            with c1:
                st.markdown("**Idiomas**")
                df_id = st.data_editor(
                    _df_idiomas(datos.get("idiomas")),
                    num_rows="dynamic", width="stretch", hide_index=True,
                    key=f"ed_idiomas_{firma}")
            with c2:
                st.markdown("**Certificaciones**")
                df_ce = st.data_editor(
                    _df_lista("certificacion", datos.get("certificaciones")),
                    num_rows="dynamic", width="stretch", hide_index=True,
                    key=f"ed_certs_{firma}")

            with st.expander("⚖️ Pesos (qué tanto pesa cada sección)"):
                p1, p2, p3 = st.columns(3)
                w_tit = p1.number_input("Título", 0.0, 2.0, step=0.05,
                                        value=float((datos.get("pesos") or {}).get("titulo", 1.0)),
                                        key=f"ed_wt_{firma}")
                w_sk = p2.number_input("Skills", 0.0, 2.0, step=0.05,
                                       value=float((datos.get("pesos") or {}).get("skills", 0.7)),
                                       key=f"ed_ws_{firma}")
                w_te = p3.number_input("Tema", 0.0, 2.0, step=0.05,
                                       value=float((datos.get("pesos") or {}).get("tema", 0.5)),
                                       key=f"ed_wte_{firma}")
                pa = datos.get("pesos_ats") or {}
                st.caption("Score ATS (deben sumar 1): "
                           + (" · ".join(f"{k} {float(v):g}" for k, v in pa.items())
                              or "—"))

            # --- nuevo dict: SOLO se pisan las claves editadas arriba ---
            nuevo = dict(datos)
            nuevo["nombre"] = nombre_nuevo.strip()
            nuevo["experiencia_anios"] = float(exp_anios)
            nuevo["educacion_nivel"] = edu_nivel.strip()
            nuevo["seniority"] = sen.strip()
            nuevo["titulos"] = _lista_de_df(df_t, "titulo")
            nuevo["skills"] = _lista_de_df(df_s, "skill")
            nuevo["temas"] = _temas_de_df(df_te)
            nuevo["idiomas"] = _idiomas_de_df(df_id)
            nuevo["certificaciones"] = _lista_de_df(df_ce, "certificacion")
            nuevo["pesos"] = {"titulo": float(w_tit), "skills": float(w_sk),
                              "tema": float(w_te)}

            # --- resumen de diferencias contra lo que hay en disco ---
            cambios = []
            for k, etiqueta in [("titulos", "Títulos"), ("skills", "Skills"),
                                ("temas", "Temas"), ("idiomas", "Idiomas"),
                                ("certificaciones", "Certificaciones")]:
                q, a = _delta_listas(
                    [i.get("puesto", i.get("idioma", i)) if isinstance(i, dict) else i
                     for i in (datos.get(k) or [])],
                    [i.get("puesto", i.get("idioma", i)) if isinstance(i, dict) else i
                     for i in (nuevo.get(k) or [])])
                if q:
                    cambios.append(f"🗑️ {etiqueta}: se quitan **{len(q)}** — "
                                   + ", ".join(f"`{x}`" for x in q[:8])
                                   + ("..." if len(q) > 8 else ""))
                if a:
                    cambios.append(f"➕ {etiqueta}: se agregan **{len(a)}** — "
                                   + ", ".join(f"`{x}`" for x in a[:8])
                                   + ("..." if len(a) > 8 else ""))
            for k, etiqueta in [("nombre", "Nombre"),
                                ("experiencia_anios", "Experiencia"),
                                ("educacion_nivel", "Educación"),
                                ("seniority", "Seniority"),
                                ("pesos", "Pesos")]:
                if _escalar(datos.get(k)) != _escalar(nuevo.get(k)):
                    cambios.append(f"✏️ {etiqueta}: `{datos.get(k)}` → `{nuevo.get(k)}`")

            b1, b2 = st.columns([1, 1])
            guardar = b1.button("💾 Guardar perfil", type="primary",
                                width="stretch", disabled=not cambios)
            if b2.button("↺ Descartar mis cambios", width="stretch",
                         disabled=not cambios, on_click=_limpiar_widgets_editor):
                pass

            if cambios:
                st.markdown("**Cambios pendientes (aún no guardados)**")
                for c in cambios:
                    st.markdown("- " + c)
                if _perfil_vacio(nuevo):
                    st.warning("El perfil quedaría sin títulos/skills/temas: "
                               "el ranking y el scraping se quedan sin nada.")
            else:
                st.caption("Sin cambios pendientes.")

            if guardar:
                if _perfil_vacio(nuevo):
                    st.error("No guardo: el perfil quedaría vacío "
                             "(sin títulos, skills ni temas).")
                else:
                    # Fusiona los campos planos editados con el CV estructurado
                    # que Streamlit no toca, y guarda en esquema Harvard/ATS.
                    bk = guardar_perfil(merge_profile(perfil_orig, nuevo,
                                                      replace=True), ruta_p)
                    st.cache_data.clear()
                    _aviso_editor(
                        f"Perfil guardado en `{ruta_p.name}`"
                        + (f" (backup del anterior: `{bk.name}`)" if bk else "")
                        + ". El próximo ranking ya usa estos valores.")
                    st.rerun()

            with st.expander(f"🗂️ Backups ({len(backups_perfil(ruta_p))}) — "
                             "volver a una versión anterior"):
                bks = backups_perfil(ruta_p)
                if not bks:
                    st.caption("Todavía no hay backups: se crean al primer guardado.")
                for bk in bks[:10]:
                    bb1, bb2, bb3 = st.columns([6, 2, 2])
                    bb1.caption(f"`{bk.name}` — "
                                f"{datetime.fromtimestamp(bk.stat().st_mtime):%d/%m %Y %H:%M}")
                    if bb2.button("Ver", key=f"ver_{bk.name}"):
                        try:
                            st.json(_json.loads(bk.read_text(encoding="utf-8")))
                        except (OSError, _json.JSONDecodeError) as exc:
                            st.error(f"Backup ilegible: {exc}")
                    if bb3.button("Restaurar", key=f"rest_{bk.name}"):
                        actual_bk = guardar_perfil(
                            _json.loads(ruta_p.read_text(encoding="utf-8")), ruta_p)
                        ruta_p.write_text(bk.read_text(encoding="utf-8"),
                                          encoding="utf-8")
                        st.cache_data.clear()
                        _aviso_editor(
                            f"Restaurado `{bk.name}`."
                            + (f" Tu versión anterior queda en `{actual_bk.name}`."
                               if actual_bk else ""))
                        st.rerun()

with tab_run:
    st.subheader("🚀 Corrida de scraping")
    st.caption(
        "El dashboard se abre ANTES del scraping: esta pestaña muestra en "
        "vivo qué está haciendo cada proveedor y cuenta los hallazgos por "
        "**grupo**: **portales estatales** (los 3 oficiales de CR: ANE/Brete, "
        "CINDE y Procomer) y **el resto** (agregadores: Indeed, LinkedIn; "
        "ATS de empresa: Workday, iCIMS, Greenhouse...). "
        "Las corridas se pueden lanzar **por separado**: si una falla, "
        "la otra sigue andando. `jobpulse.cmd` arranca esta pantalla "
        "primero y después corre el scraping — por eso nunca tarda en "
        "aparecer.")

    def _arrancar(preflight: bool = False, solo: str = "todos"):
        """Lanza `python main.py` como proceso hijo; la GUI solo observa
        data/corrida_estado.json (asi funciona aunque reinicies Streamlit).

        `solo` separa la corrida en dos alcances (ver source_groups.RUN_*):
        "ats" = portales de empresa, "boards" = job boards (Indeed, LinkedIn,
        Procomer, ANE/Brete, CINDE). El candado sigue siendo exclusivo, asi
        que si hay una viva no se lanza otra."""
        import subprocess
        import sys
        raiz = Path(__file__).resolve().parent
        args = [sys.executable, "-u", "-X", "utf8", "main.py"]
        if preflight:
            args.append("--preflight")
        if solo and solo != "todos":
            args += ["--solo", solo]
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
        _enc = est.get("encontradas") or {}
        c1.metric("Estado", _ICONOS.get(estado, estado))
        c2.metric("Inicio", (est.get("inicio_ts") or "—")[11:19] if est.get("inicio_ts") else "—")
        c3.metric("Portales estatales (crudas)", _enc.get("estatales", 0))
        c4.metric("Resto: agregadores + ATS (crudas)",
                  _enc.get("boards", 0) + _enc.get("directos", 0))

        total = int(est.get("total_fases") or 0)
        paso = int(est.get("paso") or 0)
        if total:
            st.progress(min(1.0, paso / total),
                        text=f"Paso {min(paso, total)}/{total}")
        grupo = est.get("grupo") or ""
        chip = f"`{GRUPO_TXT.get(grupo, 'sistema')}`" if grupo else ""
        # Alcance de la corrida (--solo ats|boards|todos): sale del resumen
        # (corrida finalizada) o del titulo que publica main.py al arrancar.
        alcance = (est.get("resumen") or {}).get("alcance") or ""
        if not alcance:
            _t = est.get("titulo") or ""
            alcance = _t.split(" · ", 1)[1] if " · " in _t else ""
        achip = f" · **{alcance}**" if alcance else ""
        if estado == "corriendo":
            st.markdown(f"**▶ {est.get('fase_actual', '')}** {chip}{achip}"
                        + (f" — {est.get('detalle')}" if est.get("detalle") else ""))
        elif estado == "ok":
            r = est.get("resumen") or {}
            st.success(f"Corrida completada{achip} a las "
                       f"{(est.get('fin_ts') or '')[11:19]} — "
                       f"{r.get('crudas_estatales', 0)} en portales estatales, "
                       f"{r.get('crudas_boards', 0) + r.get('crudas_directos', 0)} "
                       f"en el resto (agregadores + ATS), "
                       f"**{r.get('nuevas', 0)} vacantes nuevas** tras el gate.")
        elif estado == "preflight":
            st.info(f"Preflight OK{achip}: plan de corrida y credenciales "
                    "reportados sin tocar la red. Revisa el log.")
        elif estado == "error":
            st.error(f"La corrida terminó con error: "
                     f"{(est.get('resumen') or {}).get('motivo', '') or 'ver log'}")
        elif estado == "interrumpida":
            st.warning("La corrida se interrumpió (el proceso ya no existe). "
                       "Puede volver a lanzarla.")
        elif estado == "detenida":
            st.warning("Corrida detenida a mano.")

        b1, b2, b3 = st.columns(3)
        if b1.button("🏢 Corrida ATS de empresa", type="primary", disabled=vivo,
                     width="stretch",
                     help="Solo portales directos del ATS: Workday, Phenom, TP, "
                          "Greenhouse, iCIMS, SmartRecruiters, SuccessFactors, "
                          "Rippling, ATS ligeros y login-sites. "
                          "Equivalente: python main.py --solo ats"):
            _arrancar(solo="ats")
            st.toast("Corrida ATS lanzada: el progreso aparece aquí en unos segundos.")
        if b2.button("🏛️ Corrida job boards + estatales", type="primary", disabled=vivo,
                     width="stretch",
                     help="Agregadores (Indeed, LinkedIn) + los 3 portales "
                          "estatales: ANE/Brete (brete.cr), CINDE y Procomer "
                          "(talento.procomer.com). Equivalente: "
                          "python main.py --solo boards"):
            _arrancar(solo="boards")
            st.toast("Corrida de job boards lanzada: el progreso aparece aquí en unos segundos.")
        if b3.button("▶️ Corrida completa", disabled=vivo, width="stretch",
                     help="Estatales + job boards + ATS de empresa en una sola "
                          "corrida (equivalente: python main.py)."):
            _arrancar()
            st.toast("Scraping lanzado: el progreso aparece aquí en unos segundos.")

        c1, c2, c3 = st.columns(3)
        if c1.button("🧪 Preflight (sin red)", disabled=vivo, width="stretch",
                     help="Dry-run: publica el plan de corrida y qué "
                          "credenciales faltan, sin navegador ni requests."):
            _arrancar(preflight=True)
            st.toast("Preflight lanzado.")
        if c2.button("⏹ Detener", disabled=not vivo, width="stretch"):
            if detener():
                st.toast("Corrida detenida.")
            else:
                st.error("No se pudo detener (quizás ya terminó).")
        if c3.button("🔄 Reindexar vacantes nuevas", width="stretch"):
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
        "**nunca viaja a git**. Si la cuenta no tiene clave propia (Indeed "
        "se entra con tu Gmail), marca el acceso vía **Google/SSO** y solo "
        "guardas el correo. Si el login automático falla (CAPTCHA/2FA), "
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
            "Credencial": ((s["usuario"]
                            + ("" if not s.get("sso")
                               else " · " + s["acceso"])
                            + (" (.env)" if s.get("password_fuente") == ".env"
                               else "")) if s["tiene_credencial"]
                           else "— falta —"),
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
        usuario = st.text_input("Usuario",
                                help="Correo de la cuenta. Para Indeed usa tu "
                                     "Gmail: ahí no hay clave propia.")
        password = st.text_input("Password (opcional si usas SSO)",
                                 type="password")
        sso_ok = st.checkbox(
            "Sin clave: entrar vía Google/SSO (Indeed se entra con la cuenta "
            "de Gmail; se reutiliza tu sesión en Brave)",
            value=False)
        url_n = st.text_input("URL de login (opcional; obligatoria si el sitio "
                              "entra por el provider de navegador)")
        auto_ok = st.checkbox("Login automático en la corrida (si falla, "
                              "fallback manual)", value=True)
        card_sel = st.text_input(
            "Selectores del listado (opcional): card_sel",
            help="Clase CSS de cada tarjeta de vacante (p. ej. '.job-card'). "
                 "Sin esto el sitio nuevo se visita y se le aplica el "
                 "login/pendientes, pero todavía no extrae vacantes.")
        loc_sel = st.text_input(
            "Selector de ubicación (opcional): loc_sel",
            help="Clase CSS de la ubicación dentro de la tarjeta; sin ella "
                 "solo pasan las vacantes con 'Costa Rica' en el título.")
        if st.form_submit_button("💾 Guardar credencial", type="primary"):
            nombre = (otra or "").strip() or sel
            sso = "google" if sso_ok else ""
            if not usuario.strip() or (not password and not sso):
                st.error("Falta el usuario, o bien la password o el acceso "
                         "vía SSO (marca la casilla si la cuenta no tiene "
                         "clave propia).")
            else:
                nuevo = not creds_store.sitio_en_flujo(nombre)
                creds_store.upsert_credencial(nombre, usuario, password,
                                              url=url_n, auto_login=auto_ok,
                                              sso=sso, card_sel=card_sel,
                                              loc_sel=loc_sel)
                if nuevo and url_n.strip():
                    st.success(
                        f"**{nombre}** dado de alta en el flujo: credencial "
                        "guardada, aparece en LA LISTA y entra al barrido de "
                        "portales (cada corrida lo visita en tu Brave).")
                elif nuevo:
                    st.success(
                        f"**{nombre}** en LA LISTA con su credencial. Sin URL "
                        "no se barre todavía: vuelve a guardar con la URL del "
                        "portal y el scraper lo recorrerá cada corrida.")
                else:
                    st.success(
                        f"Credencial de {nombre} guardada localmente"
                        f"{' (acceso vía Google, sin clave)' if sso else ''}.")
                st.rerun()

    st.markdown("**Acciones por sitio**")
    for s in sitios:
        k = s["empresa"].casefold().replace(" ", "_")
        es_manual = s.get("detectado") == "manual"
        cols = [3, 2, 2, 3] + ([3] if es_manual else [])
        c1, c2, c3, c4, *rest = st.columns(cols)
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
        if es_manual and rest and rest[0].button(
                "🚫 Quitar del flujo", key=f"qs_{k}",
                help="Quita el sitio que tú agregaste de la lista y del "
                     "barrido (los sitios fijos del sistema no se pueden "
                     "quitar). La credencial se conserva."):
            creds_store.quitar_sitio_login(s["empresa"])
            st.rerun()


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
