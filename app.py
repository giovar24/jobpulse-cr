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

from dotenv import load_dotenv

ensure_workspace()   # data/, profile.json generico y .env si faltan (equipo nuevo)
load_dotenv()        # HF_TOKEN y demas (el dashboard tambien decide el modelo)

SOURCES = ["indeed", "linkedin", "ane", "procomer"]

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
def load_jobs(days=None, fuente=None):
    jobs = asyncio.run(_query_jobs(days))
    if fuente:
        jobs = [j for j in jobs if (j.get("source") or "").lower() == fuente.lower()]
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
    fuente = st.selectbox("Portal", ["todas"] + SOURCES)
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
tab_rank, tab_free = st.tabs(["🎯 Ranking de mejores opciones", "🔍 Búsqueda libre"])

with tab_rank:
    if profile:
        p_days = None if todas else days
        if st.button("🎯 Calcular ranking del perfil", type="primary", width="stretch"):
            engine = get_engine()
            with st.spinner("Consultando DB e indexando..."):
                engine.load(load_jobs(p_days, source))
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
        jobs = load_jobs(days, source)
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

st.caption("Datos: data/jobs.db — 100% local. Ranking por similitud de coseno con embeddings.")
