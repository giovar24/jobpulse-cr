# -*- coding: utf-8 -*-
"""JobPulse CR — UI de escritorio (Tkinter), sin navegador ni servidor.

Paso 1: lista de ofertas aprobadas con filtros, busqueda, orden y detalle.
Paso 2: ranking semantico del perfil (SemanticSearchEngine + rank_with_ats).
Paso 3: cada seccion vive en su propia pestana (ttk.Notebook):
   - "Ranking": tabla filtrable + ranking del perfil
   - "Busqueda libre": consulta en lenguaje natural sobre la base indexada
Paso 4: pestana "Perfil": editor con resumen de cambios y backups.
Paso 5: pestana "Corrida": el scraping SOLO empieza con sus botones
(esta UI nunca scrapea sola); corre en su propia ventana de consola
VERDE y aqui solo se observa data/corrida_estado.json.
Paso 6: pestana "Credenciales": LA LISTA de sitios con login, fiel a
core/credenciales.py (solo ANE es de password; el resto es SSO).
Paso 7: pestana "Sesiones": login manual en Brave (CAPTCHA/2FA) y
pendientes, via core/sesiones.

Por que Tkinter: viene con Python (cero dependencias nuevas), corre en un
proceso unico que tú cierras, y su vida NO depende de ninguna pestana del
navegador (el problema con Streamlit: cerrar Brave mataba la UI).

Ejecutar:  python ui.py
"""
import copy
import json
import logging
import os
import queue
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import date, datetime, timedelta
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from core.ats import rank_with_ats
from core import credenciales as creds_store
from core.bootstrap import ensure_workspace
from core.profile import (CandidateProfile, DEFAULT_PROFILE_PATH,
                          backups_perfil, guardar_perfil, normalizar_perfil)
from core.cv_render import cv_harvard_markdown
from core.cv_analysis import CVAnalyzer, extract_text, merge_profile
from core.rutas import CV_DIR, CV_IA_JSON, CV_IA_MD
from core.progreso import (corriendo, detener, leer_estado,
                           tail as log_tail)
from core.search_engine import SemanticSearchEngine
from core.sesiones import (abrir_en_brave, quitar_pendiente,
                           sitios_con_sesion, verificar_sesion)
from providers.source_groups import grupo_publico, GRUPOS_PUBLICOS

BASE = Path(__file__).resolve().parent
DB = BASE / "data" / "jobs.db"
IA_PROVEEDOR_PATH = BASE / "data" / "ia_proveedor.txt"
IA_MODELOS_PATH = BASE / "data" / "ia_modelos.json"

# La UI abre con pythonw (jobpulse.cmd: cero ventanas de comandos). Alli
# stdout/stderr no existen, y un print() de terceros (sentence-transformers
# al cargar el modelo) reventaria con stream None: se apuntan a devnull.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("jobpulse.ui")

# .env del proyecto (HF_TOKEN, EMBEDDING_MODEL): nunca rompe si falta dotenv.
try:
    from dotenv import load_dotenv
    load_dotenv(BASE / ".env")
except ImportError:
    pass

# Etiquetas en español para los 2 grupos publicos (lo que se muestra).
GRUPO_TXT = {
    "estatales": "Portal estatal (ANE/CINDE/Procomer)",
    "otros": "Resto (agregadores + ATS)",
}

# Estado de la corrida (core.progreso) -> texto y color en la pestana Corrida.
ESTADO_TXT = {
    "corriendo": "Corriendo",
    "ok": "Completada",
    "preflight": "Preflight OK (sin red)",
    "error": "Error",
    "detenida": "Detenida a mano",
    "interrumpida": "Interrumpida (el proceso murió)",
    "nunca": "Sin corridas",
}
# Estado de la corrida (core.progreso) -> texto y estilo de color en la
# pestana Corrida. Son ESTILOS ttk (no hexes fijos) para que se repinten
# solos al cambiar de tema.
ESTADO_ESTILO = {
    "corriendo": "SemOK.TLabel",
    "ok": "SemOK.TLabel",
    "preflight": "SemNeutro.TLabel",
    "error": "SemErr.TLabel",
    "detenida": "SemAviso.TLabel",
    "interrumpida": "SemAviso.TLabel",
    "nunca": "SemNeutro.TLabel",
}

# Titulo de la ventana de consola VERDE donde corre el scraping (la unica
# ventana de comandos visible cuando hay una corrida viva).
CORRIDA_TITULO = "JobPulse CR - Scraping"

# ------------------------------------------------------------------ temas
# Sistema de temas: MODO claro/oscuro x TONO (teal, indigo, azul, esmeralda),
# 8 en total, todos de paletas SaaS 2026 (ColorHexHub/Tailwind + guia de
# dark mode 2026: canvas #0F172A, superficie #1E293B, acentos claros
# desaturados y texto blanco suave). Regla 60-30-10 en ambos modos: 60%
# fondo/superficies, 30% texto, 10% acento. El acento NO se reparte: lo
# usan SOLO los botones, la pestana activa y la fila seleccionada de las
# tablas — asi un objeto relleno del acento = algo con lo que se acciona,
# y una pestana del acento = donde estas parado.
#
# En claro el acento es el tone-600 con TEXTO BLANCO encima (pasa contraste).
# En oscuro se invierte: acento tone-400 (brillante, se ve sobre fondo
# oscuro) con texto tone-950 — el patron "boton brillante, texto oscuro"
# de los dashboards dark 2026 (GitHub dark hace lo mismo).
#
# sem_*: colores SEMANTICOS de textos de estado (exito/error/aviso/neutro).
# En claro van en 600 (oscuros sobre fondo claro); en oscuro en 400
# (claros sobre fondo oscuro), como recomienda la guia de dark mode.
def _tema_claro(acento, acento_h, acento_p, acento_cl, fondo):
    return {
        "fondo": fondo,           # blanco tintado del tono: fondo general
        "sup":         "#FFFFFF",   # superficies: tablas, cuadros de texto
        "borde":       "#CBD5E1",   # bordes, separadores, boton deshabilitado
        "texto":       "#0F172A",   # texto principal (casi negro azulado)
        "suave":       "#64748B",   # texto secundario / ayuda
        "acento": acento, "acento_h": acento_h, "acento_p": acento_p,
        "acento_cl": acento_cl,   # tinte claro del tono: hover suave / foco
        "acento_tx":   "#FFFFFF",   # texto sobre el acento
        "inactiva":    "#E2E8F0",   # pestana sin seleccionar (color normal)
        "inactiva_tx": "#334155",   # texto de la pestana inactiva
        "peligro":     "#DC2626",   # acciones que cortan algo (Detener)
        "peligro_h":   "#B91C1C",
        "des_h":       "#94A3B8",   # texto de boton deshabilitado
        "sem_ok":      "#059669", "sem_err": "#DC2626",
        "sem_aviso":   "#D97706", "sem_neutro": "#475569",
    }


def _tema_oscuro(acento, acento_h, acento_p, acento_cl, acento_tx):
    return {
        "fondo":       "#0F172A",   # canvas oscuro (slate-900, no negro puro)
        "sup":         "#1E293B",   # superficies elevadas (slate-800)
        "borde":       "#475569",   # bordes, separadores, boton deshabilitado
        "texto":       "#E2E8F0",   # texto principal (blanco suave, no puro)
        "suave":       "#94A3B8",   # texto secundario / ayuda
        "acento": acento, "acento_h": acento_h, "acento_p": acento_p,
        "acento_cl": acento_cl,   # tinte profundo del tono: hover suave
        "acento_tx": acento_tx,  # texto OSCURO del tono sobre el acento
        "inactiva":    "#334155",   # pestana sin seleccionar (color normal)
        "inactiva_tx": "#CBD5E1",   # texto de la pestana inactiva
        "peligro":     "#EF4444",   # Detener: rojo brillante sobre oscuro
        "peligro_h":   "#DC2626",
        "des_h":       "#64748B",   # texto de boton deshabilitado
        "sem_ok":      "#34D399", "sem_err": "#F87171",
        "sem_aviso":   "#FBBF24", "sem_neutro": "#94A3B8",
    }


TEMAS = {
    "Teal claro":       _tema_claro("#0D9488", "#0F766E", "#115E59",
                                     "#CCFBF1", "#F0FDFA"),
    "Teal oscuro":      _tema_oscuro("#2DD4BF", "#5EEAD4", "#14B8A6",
                                      "#134E4A", "#042F2E"),
    "Índigo claro":     _tema_claro("#4F46E5", "#4338CA", "#3730A3",
                                    "#E0E7FF", "#EEF2FF"),
    "Índigo oscuro":    _tema_oscuro("#818CF8", "#A5B4FC", "#6366F1",
                                     "#312E81", "#1E1B4B"),
    "Azul claro":       _tema_claro("#2563EB", "#1D4ED8", "#1E40AF",
                                     "#DBEAFE", "#F8FAFC"),
    "Azul oscuro":      _tema_oscuro("#60A5FA", "#93C5FD", "#3B82F6",
                                      "#1E3A8A", "#172554"),
    "Esmeralda claro":  _tema_claro("#059669", "#047857", "#065F46",
                                    "#D1FAE5", "#F0FDF4"),
    "Esmeralda oscuro": _tema_oscuro("#34D399", "#6EE7B7", "#10B981",
                                     "#064E3B", "#022C22"),
}
TEMA_DEFECTO = "Teal claro"

# Tema ACTIVO: PALETA apunta al dict del tema en uso y _usar_tema() lo
# re-asigna. Todos los colores de la UI se leen de aqui (cero hexes sueltos).
PALETA = dict(TEMAS[TEMA_DEFECTO])


def _usar_tema(nombre: str) -> dict:
    """Fija el tema activo (module-level) sin tocar widgets todavia."""
    global PALETA
    PALETA = dict(TEMAS.get(nombre, TEMAS[TEMA_DEFECTO]))
    return PALETA


def _tema_guardado() -> str | None:
    """Nombre del tema elegido la ultima vez (data/ui_prefs.json)."""
    try:
        dato = json.loads((BASE / "data" / "ui_prefs.json")
                           .read_text(encoding="utf-8"))
        return dato.get("tema")
    except (OSError, ValueError):
        return None


def _aplicar_tema(root: tk.Tk) -> None:
    """Pinta TODA la UI desde un solo lugar (colores arriba, en PALETA).

    Se usa el tema "clam" porque es el unico que trae Tkinter que respeta
    background/foreground en botones y pestanas: el tema nativo de Windows
    ignora esos colores y por eso todos los controles quedaban gris claro
    (se veian igual una pestana que un boton que un cuadro de texto).
    """
    p = PALETA
    root.configure(bg=p["fondo"])

    # Colores de los cuadros tk.Text y de las listas de los Combobox
    # (esos widgets no son ttk y no leen el estilo del tema; los Text de
    # los dialogos nacen DESPUES de cada cambio y leen estos defaults).
    root.option_add("*TCombobox*Listbox.background", p["sup"])
    root.option_add("*TCombobox*Listbox.foreground", p["texto"])
    root.option_add("*TCombobox*Listbox.selectBackground", p["acento"])
    root.option_add("*TCombobox*Listbox.selectForeground", p["acento_tx"])
    root.option_add("*Text.background", p["sup"])
    root.option_add("*Text.foreground", p["texto"])
    root.option_add("*Text.insertBackground", p["texto"])
    root.option_add("*Text.selectBackground", p["acento"])
    root.option_add("*Text.selectForeground", p["acento_tx"])
    root.option_add("*Listbox.background", p["sup"])
    root.option_add("*Listbox.foreground", p["texto"])
    root.option_add("*Listbox.selectBackground", p["acento"])
    root.option_add("*Listbox.selectForeground", p["acento_tx"])

    st = ttk.Style(root)
    try:
        st.theme_use("clam")
    except tk.TclError:
        pass

    # --- base: fondo y texto de todos los widgets ttk
    st.configure(".", background=p["fondo"], foreground=p["texto"],
                 font=("Segoe UI", 9))
    st.configure("TFrame", background=p["fondo"])
    st.configure("TLabel", background=p["fondo"], foreground=p["texto"])
    # Etiquetas con color SEMANTICO (no hex fijo): al ser estilos, se
    # repintan solas en TODAS las ventanas cuando cambia el tema.
    #   Suave     -> texto secundario/ayuda
    #   SemOK     -> exito (credencial guardada, corrida OK...)
    #   SemErr    -> error (validaciones, modelos IA, login)
    #   SemAviso  -> advertencia (no bloquea)
    #   SemNeutro -> estado neutro (sin corridas, preflight)
    for nom, clave in (("Suave.TLabel", "suave"),
                       ("SemOK.TLabel", "sem_ok"),
                       ("SemErr.TLabel", "sem_err"),
                       ("SemAviso.TLabel", "sem_aviso"),
                       ("SemNeutro.TLabel", "sem_neutro")):
        st.configure(nom, background=p["fondo"], foreground=p[clave])
    st.configure("TLabelframe", background=p["fondo"], foreground=p["texto"],
                 borderwidth=1, relief="groove", bordercolor=p["borde"])
    st.configure("TLabelframe.Label", background=p["fondo"],
                 foreground=p["texto"], font=("Segoe UI", 9, "bold"))
    st.configure("TCheckbutton", background=p["fondo"], foreground=p["texto"])
    st.map("TCheckbutton", background=[("active", p["fondo"])])

    # --- 1) BOTONES: relleno solido de acento y texto blanco en negrita.
    #        A simple vista se distinguen de una pestana, de un cuadro de
    #        texto (blanco con borde) y de una etiqueta (sin relleno).
    st.configure("TButton",
                 background=p["acento"], foreground=p["acento_tx"],
                 bordercolor=p["acento"], lightcolor=p["acento"],
                 darkcolor=p["acento_p"],
                 font=("Segoe UI", 9, "bold"), padding=(12, 5),
                 focusthickness=2, focuscolor=p["acento_cl"])
    st.map("TButton",
           background=[("active", p["acento_h"]), ("pressed", p["acento_p"]),
                       ("disabled", p["borde"])],
           foreground=[("disabled", p["des_h"])],
           lightcolor=[("pressed", p["acento_p"])],
           darkcolor=[("pressed", p["acento_p"])],
           bordercolor=[("pressed", p["acento_p"]), ("disabled", p["borde"])])
    # variante de alerta (mismo formato, otro color): solo "Detener"
    st.configure("Danger.TButton",
                 background=p["peligro"], foreground="#FFFFFF",
                 bordercolor=p["peligro"], lightcolor=p["peligro"],
                 darkcolor=p["peligro_h"],
                 font=("Segoe UI", 9, "bold"), padding=(12, 5),
                 focusthickness=2, focuscolor="#FECACA")
    st.map("Danger.TButton",
           background=[("active", p["peligro_h"]), ("pressed", "#991B1B"),
                       ("disabled", p["borde"])],
           foreground=[("disabled", p["des_h"])],
           lightcolor=[("pressed", "#991B1B")],
           darkcolor=[("pressed", "#991B1B")],
           bordercolor=[("pressed", "#991B1B"), ("disabled", p["borde"])])

    # --- 2) PESTANAS: la activa en acento, las demas en su color normal.
    st.configure("TNotebook", background=p["fondo"], borderwidth=0,
                 tabmargins=(6, 6, 0, 0))
    st.configure("TNotebook.Tab",
                 background=p["inactiva"], foreground=p["inactiva_tx"],
                 font=("Segoe UI", 9, "bold"), padding=(16, 7),
                 borderwidth=0, relief="flat")
    # El orden importa: lo ultimo pisa lo anterior, asi "selected" (pestana
    # activa) gana aunque el mouse este encima.
    st.map("TNotebook.Tab",
           background=[("active", p["acento_cl"]), ("selected", p["acento"])],
           foreground=[("active", p["acento_h"]), ("selected", p["acento_tx"])])

    # --- cuadros de texto y combos: borde visible, relleno blanco
    st.configure("TEntry",
                 background=p["sup"], fieldbackground=p["sup"],
                 foreground=p["texto"], insertcolor=p["texto"],
                 borderwidth=1, padding=4)
    st.map("TEntry",
           bordercolor=[("focus", p["acento"])],
           lightcolor=[("focus", p["acento"])],
           fieldbackground=[("disabled", p["inactiva"])],
           foreground=[("disabled", p["suave"])])
    st.configure("TCombobox",
                 background=p["sup"], fieldbackground=p["sup"],
                 foreground=p["texto"], arrowcolor=p["suave"],
                 borderwidth=1, padding=4)
    st.map("TCombobox",
           fieldbackground=[("readonly", p["sup"]), ("disabled", p["inactiva"])],
           foreground=[("readonly", p["texto"]), ("disabled", p["suave"])],
           bordercolor=[("focus", p["acento"])],
           lightcolor=[("focus", p["acento"])])

    # --- tablas: filas blancas y seleccion en acento (mismo lenguaje)
    st.configure("Treeview",
                 background=p["sup"], fieldbackground=p["sup"],
                 foreground=p["texto"], rowheight=25,
                 borderwidth=0, relief="flat")
    st.map("Treeview",
           background=[("selected", p["acento"])],
           foreground=[("selected", p["acento_tx"])])
    st.configure("Treeview.Heading",
                 background=p["inactiva"], foreground=p["texto"],
                 font=("Segoe UI", 9, "bold"), relief="flat", padding=(6, 6))
    st.map("Treeview.Heading", background=[("active", p["borde"])])

    # --- barras de desplazamiento, progreso y separadores
    for nom in ("Vertical.TScrollbar", "Horizontal.TScrollbar"):
        st.configure(nom, background=p["borde"], troughcolor=p["fondo"],
                     arrowcolor=p["suave"], bordercolor=p["fondo"],
                     lightcolor=p["borde"], darkcolor=p["borde"])
        st.map(nom, background=[("active", p["suave"]), ("pressed", p["suave"])])
    st.configure("TProgressbar", background=p["acento"], troughcolor=p["inactiva"],
                 bordercolor=p["fondo"], lightcolor=p["acento"],
                 darkcolor=p["acento"], thickness=12)
    st.configure("TSeparator", background=p["borde"])

# (id, encabezado, ancho, alineacion)
COLS_RANK = (
    ("score", "Match", 70, "e"),
    ("ats", "ATS", 58, "e"),
    ("ia", "IA", 44, "e"),
    ("veredicto", "Veredicto", 132, "w"),
    ("fuente", "Fuente", 105, "w"),
    ("grupo", "Grupo", 48, "center"),
    ("puesto", "Puesto", 285, "w"),
    ("empresa", "Empresa", 160, "w"),
    ("ubicacion", "Ubicacion", 140, "w"),
    ("publicada", "Publicada", 100, "center"),
)
COLS_LIBRE = (
    ("score", "Match", 70, "e"),
    ("fuente", "Fuente", 105, "w"),
    ("grupo", "Grupo", 48, "center"),
    ("puesto", "Puesto", 300, "w"),
    ("empresa", "Empresa", 160, "w"),
    ("ubicacion", "Ubicacion", 140, "w"),
    ("publicada", "Publicada", 100, "center"),
)

_MOTOR = None   # motor cacheado a nivel de modulo: el modelo se carga 1 sola vez


def motor():
    """Singleton del motor semantico (cargar el modelo cuesta varios segundos)."""
    global _MOTOR
    if _MOTOR is None:
        _MOTOR = SemanticSearchEngine()
    return _MOTOR


def conectar():
    """Lectura pura: la app nunca escribe en la base."""
    if not DB.exists():
        return None
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def cargar():
    """Todas las ofertas aprobadas de `jobs` + frescura de la ultima corrida.

    La ventana de dias NO se aplica aqui: se aplica en cada repintado para
    que cambiar el campo "Dias" refiltre al instante sin recargar.
    """
    con = conectar()
    if con is None:
        return None, None
    try:
        try:
            filas = con.execute(
                "SELECT job_id, source, title, company, location, link,"
                "       date_published, description, requirements, gate_score,"
                "       ia_score, ia_razon"
                " FROM jobs"
            ).fetchall()
        except sqlite3.OperationalError:
            # BD creada antes de la migracion IA: la UI sigue leyendo igual.
            filas = con.execute(
                "SELECT job_id, source, title, company, location, link,"
                "       date_published, description, requirements, gate_score"
                " FROM jobs"
            ).fetchall()
        ultima = con.execute("SELECT MAX(created_at) FROM jobs_raw").fetchone()[0]
    finally:
        con.close()

    out = []
    for f in filas:
        sc = f["gate_score"]
        src = f["source"] or ""
        g = grupo_publico(src)
        tiene_ia = "ia_score" in f.keys()
        ia = f["ia_score"] if tiene_ia else None
        out.append({
            "job_id": f["job_id"],
            "score": None if sc is None else round(sc * 100, 1),
            "ia": None if ia is None else round(float(ia)),
            "ia_razon": (f["ia_razon"] if tiene_ia else "") or "",
            "fuente": src,
            "grupo_int": g,                    # clave publica: estatales|otros
            "grupo": "EST" if g == "estatales" else "RESTO",
            "puesto": f["title"] or "",
            "empresa": f["company"] or "-",
            "ubicacion": f["location"] or "-",
            "publicada": (f["date_published"] or "")[:10],
            "fecha": f["date_published"] or "",
            "link": f["link"] or "",
            "descripcion": f["description"] or "(sin descripcion)",
            "requisitos": f["requirements"] or "",
            # Claves en ingles: las que leen core.ats (ats_score) y
            # build_doc_text. Sin ellas, el ATS puntua 0.0 y toda oferta
            # nueva (cache miss) se embebe con texto vacio. Mismas celdas,
            # dos nombres: la UI habla espanol, el nucleo ingles.
            "title": f["title"] or "",
            "company": f["company"] or "",
            "location": f["location"] or "",
            "description": f["description"] or "",
            "requirements": f["requirements"] or "",
            # solo se llenan al calcular el ranking
            "ats": None,
            "veredicto": "",
            "sem": None,
            "faltantes": [],
            "brk": {},
            "rankeada": False,
        })
    return out, ultima


def en_ventana(fecha: str, dias: int) -> bool:
    """Ventana de dias. Sin fecha parseable: beneficio de la duda (igual que el gate)."""
    if not dias or not fecha:
        return True
    try:
        d = date.fromisoformat(str(fecha)[:10])
    except (ValueError, TypeError):
        return True
    return d >= date.today() - timedelta(days=dias)


def ordenar(filas, clave, asc):
    """Orden con None SIEMPRE al final (tambien en descendente)."""
    con = [f for f in filas if f[clave] is not None]
    nulos = [f for f in filas if f[clave] is None]
    con.sort(key=lambda f: f[clave].lower() if isinstance(f[clave], str) else f[clave],
             reverse=not asc)
    return con + nulos


# ------------------------------------------------------------ perfil
def _lineas(texto: str):
    """Items no vacios de un Text, uno por linea."""
    return [l.strip() for l in texto.splitlines() if l.strip()]


def _temas_de_lineas(lineas):
    """"puesto | puesto_en | ubicacion" -> un dict por linea (como _temas_de_df)."""
    out = []
    for l in lineas:
        p = [x.strip() for x in l.split("|")]
        while len(p) < 3:
            p.append("")
        if p[0]:
            out.append({"puesto": p[0], "puesto_en": p[1], "ubicacion": p[2]})
    return out


def _idiomas_de_lineas(lineas):
    """"idioma | nivel" -> un dict por linea (como _idiomas_de_df)."""
    out = []
    for l in lineas:
        p = [x.strip() for x in l.split("|")]
        while len(p) < 2:
            p.append("")
        if p[0]:
            out.append({"idioma": p[0], "nivel": p[1]})
    return out


def _escalar(v):
    """Normaliza un escalar para comparar cambios (el de app.py, casi literal).

    Sin esto, una clave ausente (None) contra un campo vacio ("") se lee como
    "cambio" y el guardado "sin cambios" reescribe el perfil: con el perfil
    actual sin clave `seniority`, aparecia el fantasma "Seniority: None -> ''"
    en cuanto se abria la pestaña. Tambien evita 5.0 vs 5.
    """
    if v is None:
        return ""
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return v


def _perfil_vacio(datos: dict) -> bool:
    """Sin puestos objetivo, skills ni temas no sirve ni para puntuar ni para buscar."""
    try:
        p = CandidateProfile(datos)
    except Exception:
        return True
    return not (p.titulos or p.skills or p.temas)


def _perfil_campos_faltantes(datos: dict) -> list:
    """Datos importantes vacios o incompletos, en texto para el usuario."""
    try:
        p = CandidateProfile(datos)
    except Exception:
        return ["No se pudo interpretar el perfil."]

    faltan = []
    if not (p.nombre or "").strip():
        faltan.append("Nombre del candidato.")

    contacto = p.contacto or {}
    if (not (contacto.get("email") or "").strip()
            and not (contacto.get("telefono") or "").strip()):
        faltan.append("Email o teléfono de contacto.")

    if not (p.resumen_profesional or "").strip():
        faltan.append("Resumen profesional.")

    exp = p.experiencia or []
    if not exp:
        faltan.append("Experiencia laboral (no hay ninguna cargada).")
    else:
        sin_inicio = sum(1 for e in exp
                         if not str(e.get("inicio") or "").strip())
        sin_puesto = sum(1 for e in exp
                         if not (e.get("puesto") or "").strip())
        sin_empresa = sum(1 for e in exp
                          if not (e.get("empresa") or "").strip())
        if sin_inicio:
            faltan.append(f"Experiencia sin fecha de inicio "
                          f"({sin_inicio}/{len(exp)}).")
        if sin_puesto:
            faltan.append(f"Experiencia sin puesto ({sin_puesto}/{len(exp)}).")
        if sin_empresa:
            faltan.append(f"Experiencia sin empresa ({sin_empresa}/{len(exp)}).")

    edu = p.educacion or []
    if not edu:
        faltan.append("Educación (no hay ninguna cargada).")
    else:
        if not (p.educacion_nivel or "").strip():
            faltan.append("Educación sin título/nivel detectable.")
        sin_inst = sum(1 for e in edu
                       if not (e.get("institucion") or "").strip())
        if sin_inst:
            faltan.append(f"Educación sin institución ({sin_inst}/{len(edu)}).")
        sin_inicio_edu = sum(1 for e in edu
                             if not str(e.get("inicio") or "").strip())
        if sin_inicio_edu:
            faltan.append(f"Educación sin fecha de inicio "
                          f"({sin_inicio_edu}/{len(edu)}).")

    if not (p.habilidades or []):
        faltan.append("Habilidades.")
    if not (p.titulos or []):
        faltan.append("Puestos objetivo.")
    if not (p.busqueda.get("ubicaciones") or []):
        faltan.append("Ubicaciones de búsqueda.")

    return faltan


def _resumen_cambios(viejo: dict, nuevo: dict):
    """Diferencias legibles antes de guardar, sobre el perfil ya derivado."""
    try:
        v = CandidateProfile(viejo)
        n = CandidateProfile(nuevo)
    except Exception:
        return ["- No se pudo comparar el perfil anterior."]

    lineas = []

    def delta_lista(a, b, etiqueta):
        a = [str(x).strip() for x in a]
        b = [str(x).strip() for x in b]
        for xs, verbo in (([x for x in a if x not in b], "se quitan"),
                          ([x for x in b if x not in a], "se agregan")):
            if xs:
                lista = ", ".join(xs[:8]) + ("..." if len(xs) > 8 else "")
                lineas.append(f"- {etiqueta}: {verbo} {len(xs)} — {lista}")

    delta_lista(v.titulos, n.titulos, "Puestos objetivo")
    delta_lista(v.skills, n.skills, "Skills")
    delta_lista([f"{t.get('puesto')} ({t.get('ubicacion')})" for t in v.temas],
                [f"{t.get('puesto')} ({t.get('ubicacion')})" for t in n.temas],
                "Temas")
    delta_lista([f"{i.get('idioma')} {i.get('nivel')}".strip() for i in v.idiomas],
                [f"{i.get('idioma')} {i.get('nivel')}".strip() for i in n.idiomas],
                "Idiomas")
    delta_lista(v.certificaciones, n.certificaciones, "Certificaciones")

    for etiqueta, va, nb in (
        ("Nombre", v.nombre, n.nombre),
        ("Resumen", v.resumen_profesional, n.resumen_profesional),
        ("Contacto", v.contacto, n.contacto),
        ("Experiencia (años)", v.experiencia_anios, n.experiencia_anios),
        ("Educación", v.educacion_nivel, n.educacion_nivel),
        ("Seniority", v.seniority, n.seniority),
        ("Pesos", v.pesos, n.pesos),
    ):
        if _escalar(va) != _escalar(nb):
            lineas.append(f"- {etiqueta}: {va!r} → {nb!r}")

    for campo, etiqueta in (("educacion", "Educación"),
                            ("experiencia", "Experiencia"),
                            ("proyectos", "Proyectos")):
        nv = len(getattr(v, campo) or [])
        nn = len(getattr(n, campo) or [])
        if nv != nn:
            lineas.append(f"- {etiqueta}: {nv} → {nn} registro(s)")
    return lineas


# ------------------------------------------------------------------ hilos
# Tkinter no es multihilo: los hilos NUNCA tocan un widget. Solo encolan y
# el hilo principal drena con `after`.

def _hilo_ranking(perfil, filas, cola: queue.Queue):
    try:
        log.info("Indexando %d ofertas...", len(filas))
        eng = motor()
        eng.load(filas)
        log.info("Buscando las mejores opciones para el perfil...")
        res = eng.search_profile(perfil, top_k=60)
        log.info("Puntando contra ATS (%d)...", len(res))
        res = rank_with_ats(perfil.to_dict(), res, eng)
        cola.put(("rank", res))
    except Exception as exc:
        log.exception("Fallo el ranking")
        cola.put(("err", exc))


def _hilo_buscar(query: str, top: int, filas, cola: queue.Queue):
    try:
        log.info("Busqueda libre: '%s' sobre %d ofertas", query, len(filas))
        eng = motor()
        eng.load(filas)
        res = eng.search(query, top_k=top)
        cola.put(("libre", (query, res)))
    except Exception as exc:
        log.exception("Fallo la busqueda libre")
        cola.put(("err", exc))


def _hilo_corrida(q: queue.Queue, stop: threading.Event):
    """Lector de estado de corrida: nunca toca widgets, solo encola.

    Corre SIEMPRE en segundo plano (2 s): leer_estado() ejecuta tasklist
    cuando hay corrida viva y eso no puede ir en el hilo de Tkinter.
    """
    while not stop.is_set():
        try:
            q.put((leer_estado(), log_tail(120)))
        except Exception:
            pass
        stop.wait(2.0)


def _hilo_verificar(empresa: str, url: str, cola: queue.Queue):
    """Verificar sesion monta Brave por CDP (cierra y REABRE tu navegador):
    tarda varios segundos, va SIEMPRE en segundo plano. Nunca toca widgets."""
    try:
        cola.put(("sesion", (empresa, verificar_sesion(url))))
    except Exception as exc:
        cola.put(("err", exc))


def _nombre_desde_cv(texto: str) -> str:
    for linea in texto.splitlines():
        limpio = re.sub(r"\s+", " ", linea).strip()
        if limpio:
            return limpio[:100]
    return "Candidato"


def _perfil_desde_cv_local(texto: str) -> dict:
    """Extraccion sin embeddings (skills, contacto, ATS) para cuando el modelo
    semantico no esta disponible (p.ej. torch bloqueado por una politica de
    Control de aplicaciones en Windows)."""
    from core.cv_analysis import (SKILL_LEXICON, deacc, detect_certifications,
                                  detect_contact, detect_education_level,
                                  detect_experience_years,
                                  detect_languages_struct, detect_seniority)

    low = deacc(re.sub(r"\s+", " ", texto)).strip()
    skills = [
        s for s in SKILL_LEXICON
        if re.search(r"(?<![\w-])" + re.escape(deacc(s)) + r"(?![\w-])", low)
    ]
    idiomas = detect_languages_struct(texto)
    for d in idiomas:
        etiqueta = (d["idioma"] if d["nivel"] == "no especificado"
                    else f"{d['idioma']} {d['nivel']}")
        if not any(etiqueta.lower() in s.lower() for s in skills):
            skills.append(etiqueta)
    return {
        "skills": skills,
        "certificaciones": detect_certifications(texto),
        "idiomas": idiomas,
        "contacto": detect_contact(texto),
        "experiencia_anios": detect_experience_years(texto),
        "educacion_nivel": detect_education_level(texto),
        "seniority": detect_seniority(texto),
    }


def _perfil_ia_desde_cv(texto: str, merged: dict, modelo: str,
                        vacantes=None, proveedor: str = ""):
    """Analisis OPCIONAL con el modelo de la API (IA externa, consume cuota).

    Devuelve (perfil_fusionado, evaluacion, aviso). Si la IA falla, conserva
    el resultado local y explica el motivo en `aviso`.
    """
    try:
        from datetime import datetime

        from core.ia_api import IAClient, cv_a_markdown, perfil_hash

        cfg = {}
        if proveedor:
            cfg["proveedor"] = proveedor.strip().lower()
        if modelo:
            cfg["modelo"] = modelo.strip()
        client = IAClient(cfg or None)
        if not client.disponible:
            return merged, {}, client.motivo_no_disponible()

        perfil_obj = CandidateProfile(merged)
        puesto = (perfil_obj.titulos or [""])[0]
        evaluacion_raw = client.evaluar_cv(texto, perfil_obj, vacantes, puesto)
        propuesta = evaluacion_raw.get("perfil")
        if propuesta:
            merged = merge_profile(merged, propuesta, replace=True)

        meta = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "proveedor": client.proveedor,
            "modelo": client.modelo,
            "cv_path": "",
            "cv_hash": client.hash_cv(texto, perfil_hash(perfil_obj), puesto),
            "perfil_hash": perfil_hash(perfil_obj),
            "puesto": puesto,
            "tokens_in": client.tokens_in,
            "tokens_out": client.tokens_out,
            "cache": False,
        }
        CV_IA_JSON.parent.mkdir(parents=True, exist_ok=True)
        CV_IA_JSON.write_text(
            json.dumps({"meta": meta, "resultado": evaluacion_raw},
                       ensure_ascii=False, indent=2),
            encoding="utf-8")
        CV_IA_MD.write_text(cv_a_markdown(evaluacion_raw, meta),
                            encoding="utf-8")
        return merged, evaluacion_raw.get("evaluacion") or {}, ""
    except Exception as exc:
        log.exception("Importar CV: fallo el analisis con IA")
        return merged, {}, f"La IA falló ({exc}). Se cargó la extracción local."


def _hilo_importar_cv(ruta: str, actual: dict, cola: queue.Queue,
                      usar_ia: bool = False, modelo: str = "",
                      vacantes=None, proveedor: str = ""):
    """Extrae un CV local (PDF/DOCX/TXT/MD) y lo fusiona como BORRADOR.

    Va en segundo plano porque CVAnalyzer carga el modelo de embeddings y la
    IA externa puede tardar. Nunca toca widgets: encola el perfil propuesto.
    """
    try:
        cola.put(("cv_status", "Extrayendo texto del CV..."))
        texto = extract_text(ruta)
        if len(texto.strip()) < 40:
            raise ValueError("el CV no tiene texto legible "
                             "(¿PDF escaneado sin OCR?)")
        try:
            propuesta = CVAnalyzer(engine=motor()).analyze(texto)
        except Exception as exc:
            log.warning("Importar CV: sin modelo semantico (%s); uso "
                        "extraccion local (skills/contacto/ATS).", exc)
            propuesta = _perfil_desde_cv_local(texto)
        novedad = {
            "cv": {
                "nombre": _nombre_desde_cv(texto),
                "contacto": propuesta.get("contacto") or {},
                "habilidades": propuesta.get("skills") or [],
                "certificaciones": propuesta.get("certificaciones") or [],
                "idiomas": propuesta.get("idiomas") or [],
            },
        }
        merged = merge_profile(actual, novedad, replace=True)

        # Años/educación/seniority detectados no llegan como secciones
        # estructuradas: se guardan como overrides para que el ATS los use ya.
        ov = merged.setdefault("busqueda", {}).setdefault("overrides", {})
        ov_actual = (actual.get("busqueda") or {}).get("overrides") or {}
        for clave in ("experiencia_anios", "educacion_nivel", "seniority"):
            if propuesta.get(clave) and clave not in ov_actual:
                ov[clave] = propuesta[clave]

        # Copia local en data/usuario/cv/ para que el CV viva con los datos
        # del usuario y no quede suelto en otra carpeta.
        origen = Path(ruta)
        try:
            if origen.resolve().parent != CV_DIR.resolve():
                CV_DIR.mkdir(parents=True, exist_ok=True)
                shutil.copy2(origen, CV_DIR / origen.name)
        except OSError:
            pass

        evaluacion, aviso = {}, ""
        if usar_ia:
            cola.put(("cv_status",
                      "CV extraído. Consultando el modelo de la API "
                      "(puede tardar 1-2 minutos)..."))
            merged, evaluacion, aviso = _perfil_ia_desde_cv(
                texto, merged, modelo, vacantes, proveedor=proveedor)
            if aviso:
                cola.put(("cv_status",
                          "La IA no completó el análisis; usando la "
                          "extracción local..."))
            else:
                cola.put(("cv_status",
                          "Análisis con IA completado. Preparando borrador..."))

        cola.put(("cv", (origen.name, merged, evaluacion, aviso)))
    except Exception as exc:
        log.exception("Importar CV fallo")
        cola.put(("cv_err", exc))


# ------------------------------------------------------------ componente
class Tabla(ttk.Frame):
    """Treeview con scrollbars y encabezados ordenables. Reutilizable por pestana."""

    def __init__(self, master, cols, al_ordenar=None, al_seleccionar=None,
                 al_abrir=None):
        super().__init__(master)
        self._cols = cols
        self._col, self._asc = cols[0][0], False
        self._al_ordenar = al_ordenar
        self._al_seleccionar = al_seleccionar
        self._al_abrir = al_abrir
        self._vista = {}      # iid -> fila (dict con los datos)

        self.tree = ttk.Treeview(self, columns=[c[0] for c in cols],
                                 show="headings", selectmode="browse")
        for cid, texto, ancho, alin in cols:
            self.tree.heading(cid, text=texto,
                              command=lambda c=cid: self.clic_encabezado(c))
            self.tree.column(cid, width=ancho, anchor=alin,
                             stretch=(cid == "puesto"))

        vs = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        hs = ttk.Scrollbar(self, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vs.grid(row=0, column=1, sticky="ns")
        hs.grid(row=1, column=0, sticky="ew")
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)

        self.tree.bind("<<TreeviewSelect>>", self._seleccion)
        self.tree.bind("<Double-1>", lambda _e: self._abrir())
        self.tree.bind("<Return>", lambda _e: self._abrir())
        self.marcar(self._col, self._asc)

    # API para la pestana dueña de los datos
    @property
    def orden(self):
        return self._col, self._asc

    def marcar(self, col, asc):
        """Flecha en el encabezado que define el orden actual."""
        self._col, self._asc = col, asc
        for cid, texto, _a, _b in self._cols:
            marca = (" ▼" if asc else " ▲") if cid == col else ""
            self.tree.heading(cid, text=texto + marca)

    def clic_encabezado(self, col):
        # misma columna: alterna el sentido; columna nueva: descendente
        self.marcar(col, self._col != col or not self._asc)
        if self._al_ordenar:
            self._al_ordenar()

    def llenar(self, filas, valores):
        """filas: list[dict]; valores: dict -> tupla de celdas en orden de cols."""
        hijos = self.tree.get_children()
        if hijos:
            self.tree.delete(*hijos)
        self._vista = {}
        for i, f in enumerate(filas):
            iid = str(f.get("job_id")) if f.get("job_id") is not None else f"row{i}"
            self._vista[iid] = f
            self.tree.insert("", "end", iid=iid, values=valores(f))

    def seleccionada(self):
        sel = self.tree.selection()
        return self._vista.get(sel[0]) if sel else None

    # eventos internos
    def _seleccion(self, _e):
        if self._al_seleccionar:
            self._al_seleccionar(self.seleccionada())

    def _abrir(self):
        f = self.seleccionada()
        if f and self._al_abrir:
            self._al_abrir(f)


class MarcoScroll(ttk.Frame):
    """Frame con canvas + scrollbar vertical.

    Las pestañas con mucho contenido (Perfil > Mi CV) no caben en pantallas
    de 1080p: sin esto, los campos de abajo (botones, certificaciones,
    idiomas) quedaban fuera de la ventana y eran inalcanzables.
    """

    def __init__(self, master, canvases_tema=None):
        super().__init__(master)
        fondo = (ttk.Style(self).lookup("TFrame", "background")
                 or PALETA["fondo"])
        self.canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0,
                                background=fondo)
        self.sb = ttk.Scrollbar(self, orient="vertical",
                                command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.sb.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.sb.pack(side="right", fill="y")

        self.interior = ttk.Frame(self.canvas)
        self._win = self.canvas.create_window((0, 0), window=self.interior,
                                              anchor="nw")
        self.interior.bind("<Configure>", self._interior_cambio)
        self.canvas.bind("<Configure>", self._canvas_cambio)
        self.canvas.bind("<MouseWheel>", self._rueda)
        self.interior.bind("<MouseWheel>", self._rueda)
        if canvases_tema is not None:
            canvases_tema.append(self.canvas)

    def _interior_cambio(self, _e=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _canvas_cambio(self, e):
        # el contenido acompaña el ancho disponible (sin scroll horizontal)
        self.canvas.itemconfigure(self._win, width=e.width)

    def _rueda(self, e):
        self.canvas.yview_scroll(int(-e.delta / 120), "units")


# ------------------------------------------------------------------ app
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        # Tema: el guardado en data/ui_prefs.json o el default. Se fija
        # ANTES de crear widgets para que todos nazcan con sus colores.
        self._texts_tema = []       # tk.Text vivos: se repintan al cambiar
        self._canvas_tema = []      # tk.Canvas de scroll: idem
        self._tema_nombre = _tema_guardado() or TEMA_DEFECTO
        if self._tema_nombre not in TEMAS:
            self._tema_nombre = TEMA_DEFECTO
        _usar_tema(self._tema_nombre)
        _aplicar_tema(self)
        self.title("JobPulse CR — Ofertas")
        self.geometry("1440x880")
        self.minsize(1000, 600)

        self.todas, self.libres = [], []
        self._corriendo = False
        self._rank_activo = False    # True tras calcular ranking: la tabla muestra SOLO las rankeadas
        self._cola = queue.Queue()
        self._cola_corrida = queue.Queue()
        self._corrida_stop = threading.Event()
        self._cor_log_prev = None
        self._cor_estado_prev = None
        self._ses_verificando = False

        self._grupo_et = tk.StringVar(value="(todas)")
        self._busca = tk.StringVar(value="")
        self._dias = tk.StringVar(value="30")
        self._consulta = tk.StringVar(value="")
        self._top = tk.StringVar(value="40")
        self._conteo = tk.StringVar(value="Cargando...")
        self._frescura = tk.StringVar(value="")
        self._perfil_estado = tk.StringVar(value="")

        self.nb = ttk.Notebook(self)

        self._pestana_rank()
        self._pestana_libre()
        self._pestana_perfil()
        self._pestana_corrida()
        self._pestana_credenciales()
        self._pestana_sesiones()
        self._detalle_construir()

        pie = ttk.Frame(self, padding=(10, 4))
        ttk.Label(pie, textvariable=self._conteo, anchor="w").pack(side="left")
        ttk.Label(pie, textvariable=self._frescura, style="Suave.TLabel",
                  anchor="e").pack(side="right")
        # Selector de tema: readonly, a la izquierda de la frescura.
        ttk.Label(pie, text="Tema:").pack(side="right", padx=(6, 0))
        self._tema_var = tk.StringVar(value=self._tema_nombre)
        cb_tema = ttk.Combobox(pie, textvariable=self._tema_var,
                               state="readonly",
                               width=max(len(n) for n in TEMAS),
                               values=list(TEMAS))
        cb_tema.pack(side="right", padx=(0, 10))
        cb_tema.bind("<<ComboboxSelected>>",
                     lambda *_: self._tema_cambiar(self._tema_var.get()))

        # El orden de pack fija el layout: pie y detalle abajo, notebook toma el resto.
        pie.pack(side="bottom", fill="x")
        self._det_marco.pack(side="bottom", fill="x", padx=10, pady=(8, 2))
        self.nb.pack(side="top", fill="both", expand=True, padx=6, pady=(6, 2))

        self._recargar()
        for var in (self._busca, self._grupo_et, self._dias):
            var.trace_add("write", lambda *_: self._pintar_rank())
        self._after_drenar_id = self.after(120, self._drenar)

        # Observador de corridas: el hilo lee estado+logs cada 2 s y el hilo
        # de Tkinter solo pinta lo que llega por la cola (leer_estado dispara
        # tasklist mientras hay corrida viva: no puede congelar la ventana).
        threading.Thread(target=_hilo_corrida,
                         args=(self._cola_corrida, self._corrida_stop),
                         daemon=True).start()
        self._after_corrida_id = self.after(2000, self._corrida_refrescar)
        self._corrida_pintar(leer_estado(), log_tail(120))
        self.protocol("WM_DELETE_WINDOW", self._al_cerrar)

    def destroy(self):
        """Cancela los timers periodicos ANTES de matar la ventana: si un
        `after` disparara tras el destroy, Tcl imprimira "invalid command
        name ..." en consola (feo cuando la UI corre con `python ui.py`)."""
        for _id in (getattr(self, "_after_drenar_id", None),
                    getattr(self, "_after_corrida_id", None)):
            if _id is not None:
                try:
                    self.after_cancel(_id)
                except tk.TclError:
                    pass
        super().destroy()

    def _tema_cambiar(self, nombre):
        """Cambia el tema EN VIVO: re-aplica estilos y defaults (todos los
        widgets ttk se repintan solos), repinta los tk.Text vivos y guarda
        la eleccion en data/ui_prefs.json para la proxima apertura."""
        _usar_tema(nombre)
        self._tema_nombre = nombre if nombre in TEMAS else TEMA_DEFECTO
        if hasattr(self, "_tema_var"):
            self._tema_var.set(self._tema_nombre)
        _aplicar_tema(self)
        p = PALETA
        for t in self._texts_tema:
            try:
                t.configure(background=p["sup"], foreground=p["texto"],
                            insertbackground=p["texto"],
                            selectbackground=p["acento"],
                            selectforeground=p["acento_tx"])
            except tk.TclError:
                pass                      # un dialogo ya cerrado
        for c in self._canvas_tema:
            try:
                c.configure(background=p["fondo"])
            except tk.TclError:
                pass
        if hasattr(self, "_detalle"):
            self._detalle.tag_configure("rot", foreground=p["suave"])
        try:
            (BASE / "data" / "ui_prefs.json").write_text(
                json.dumps({"tema": self._tema_nombre}, ensure_ascii=False,
                           indent=1), encoding="utf-8")
        except OSError:
            pass

    # ------------------------------------------------------------- pestanas
    def _pestana_rank(self):
        tab = ttk.Frame(self.nb)

        barra = ttk.Frame(tab, padding=(10, 8))
        barra.pack(fill="x")
        ttk.Label(barra, text="Grupo:").pack(side="left")
        ttk.Combobox(barra, textvariable=self._grupo_et, state="readonly",
                     width=36,
                     values=["(todas)"] + [GRUPO_TXT[g] for g in GRUPOS_PUBLICOS]
                     ).pack(side="left", padx=(4, 14))
        ttk.Label(barra, text="Buscar:").pack(side="left")
        ttk.Entry(barra, textvariable=self._busca, width=30).pack(side="left",
                                                                  padx=(4, 14))
        ttk.Label(barra, text="Días:").pack(side="left")
        ttk.Entry(barra, textvariable=self._dias, width=6).pack(side="left",
                                                                padx=(4, 14))
        ttk.Button(barra, text="Actualizar", command=self._recargar).pack(side="left")
        self._btn_rank = ttk.Button(barra, text="Calcular ranking del perfil",
                                    command=self._calcular_ranking)
        self._btn_rank.pack(side="left", padx=(16, 0))

        self._tabla_rank = Tabla(tab, COLS_RANK, al_ordenar=self._pintar_rank,
                                 al_seleccionar=self._ver_detalle,
                                 al_abrir=self._abrir_link)
        self._tabla_rank.pack(fill="both", expand=True, padx=10, pady=(0, 8))
        self.nb.add(tab, text="  Ranking  ")

    def _pestana_libre(self):
        tab = ttk.Frame(self.nb)

        barra = ttk.Frame(tab, padding=(10, 8))
        barra.pack(fill="x")
        ttk.Label(barra, text="Consulta:").pack(side="left")
        ttk.Entry(barra, textvariable=self._consulta, width=46).pack(side="left",
                                                                      padx=(4, 10))
        ttk.Label(barra, text="Top:").pack(side="left")
        ttk.Entry(barra, textvariable=self._top, width=6).pack(side="left",
                                                               padx=(4, 14))
        self._btn_buscar = ttk.Button(barra, text="Buscar", command=self._buscar_libre)
        self._btn_buscar.pack(side="left")
        ttk.Label(barra, style="Suave.TLabel",
                  text="Busca sobre TODAS las ofertas cargadas (sin filtro de días)."
                  ).pack(side="left", padx=(14, 0))

        self._tabla_libre = Tabla(tab, COLS_LIBRE, al_ordenar=self._pintar_libre,
                                  al_seleccionar=self._ver_detalle,
                                  al_abrir=self._abrir_link)
        self._tabla_libre.pack(fill="both", expand=True, padx=10, pady=(0, 8))
        self.nb.add(tab, text="  Búsqueda libre  ")

    def _detalle_construir(self):
        """Un solo panel de detalle, compartido por todas las pestanas."""
        self._detalle_link = ""
        self._det_marco = ttk.LabelFrame(self, text="Detalle", padding=8)

        # Fila superior: a la derecha, el boton para copiar la URL del puesto
        # (la url tambien se ve abajo en el texto, pero asi se copia de un
        # clic sin seleccionar nada).
        enc = ttk.Frame(self._det_marco)
        enc.pack(fill="x", pady=(0, 6))
        ttk.Label(enc, text="Oferta seleccionada").pack(side="left")
        self._btn_copiar_url = ttk.Button(
            enc, text="Copiar URL del puesto", command=self._copiar_url)
        self._btn_copiar_url.pack(side="right")
        self._btn_copiar_url.configure(state="disabled")

        fondo = ttk.Style(self).lookup("Treeview", "background") or PALETA["sup"]
        self._detalle = tk.Text(self._det_marco, height=8, wrap="word",
                                state="disabled", relief="flat", background=fondo,
                                foreground=PALETA["texto"],
                                insertbackground=PALETA["texto"],
                                highlightthickness=0)
        self._detalle.pack(fill="both", expand=True)
        self._texts_tema.append(self._detalle)   # repintar al cambiar tema
        self._detalle.tag_configure("tit", font=("Segoe UI", 10, "bold"))
        self._detalle.tag_configure("rot", foreground=PALETA["suave"],
                                    font=("Segoe UI", 9, "bold"))

    # ------------------------------------------------------------ acciones
    def _recargar(self):
        self.todas, ultima = cargar()
        self._rank_activo = False     # "Actualizar" vuelve a la vista completa con gate_score
        if self.todas is None:
            self._conteo.set(f"No se encontró {DB.name}. Ejecuta primero `python main.py`.")
            return
        if ultima:
            try:
                ts = datetime.fromisoformat(str(ultima)[:19])
                horas = (datetime.now() - ts).total_seconds() / 3600
                suf = (f"hace {int(horas)} h" if horas >= 1
                       else f"hace {int((datetime.now() - ts).total_seconds() // 60)} min")
                self._frescura.set(f"Última corrida {ts:%d/%m/%Y %H:%M} ({suf})")
            except ValueError:
                self._frescura.set(f"Última corrida: {ultima}")
        self._pintar_rank()

    def _dias_actuales(self):
        try:
            return int(self._dias.get() or 0)
        except ValueError:
            return 0

    def _filas_rank(self):
        q = self._busca.get().strip().lower()
        # Con ranking activo la tabla muestra SOLO las rankeadas (como hacia
        # Streamlit): el combined (0-100) y el gate_score (0-100) son escalas
        # distintas y mezclarlas sepultaba a las rankeadas bajo las no rankeadas.
        if self._rank_activo:
            filas = [f for f in self.todas if f["rankeada"]]
        else:
            filas = list(self.todas)
        filas = [f for f in filas
                 if en_ventana(f["fecha"], self._dias_actuales())]

        et = self._grupo_et.get()
        if et != "(todas)":
            clave = next((g for g in GRUPOS_PUBLICOS if GRUPO_TXT[g] == et), None)
            if clave:
                filas = [f for f in filas if f["grupo_int"] == clave]

        if q:
            filas = [f for f in filas
                     if q in (f["puesto"] + " " + f["empresa"] + " "
                              + f["ubicacion"]).lower()]
        return filas

    def _valores_rank(self, f):
        return ("" if f["score"] is None else f"{f['score']:.1f}",
                "" if f["ats"] is None else f"{f['ats']:.0f}",
                "" if f.get("ia") is None else f"{f['ia']:.0f}",
                f["veredicto"],
                f["fuente"], f["grupo"], f["puesto"], f["empresa"],
                f["ubicacion"], f["publicada"])

    def _pintar_rank(self):
        if not self.todas:
            return
        filas = self._filas_rank()
        col, asc = self._tabla_rank.orden
        filas = ordenar(filas, col, asc)
        self._tabla_rank.llenar(filas, self._valores_rank)

        if self._rank_activo:
            n = sum(1 for f in self.todas if f["rankeada"])
            self._conteo.set(f"Mostrando {len(filas)} de {n} rankeadas  ·  "
                             f"ranking semántico + ATS  ·  {len(self.todas)} en base")
        else:
            self._conteo.set(f"Mostrando {len(filas)} de {len(self.todas)} ofertas")

    def _valores_libre(self, f):
        return (f"{f['score']:.1f}", f["fuente"], f["grupo"], f["puesto"],
                f["empresa"], f["ubicacion"], f["publicada"])

    def _pintar_libre(self):
        col, asc = self._tabla_libre.orden
        filas = ordenar(list(self.libres), col, asc)
        self._tabla_libre.llenar(filas, self._valores_libre)

    # ------------------------------------------------------------- ranking
    def _calcular_ranking(self):
        if self._corriendo:
            return
        perfil = CandidateProfile.load()
        if perfil is None:
            self._conteo.set("No hay perfil válido (data/usuario/profile.json). "
                             "Créalo antes de calcular el ranking.")
            return

        dias = self._dias_actuales()
        filas = [f for f in self.todas if en_ventana(f["fecha"], dias)]
        if not filas:
            self._conteo.set("No hay ofertas en la ventana actual para puntuar.")
            return

        self._corriendo = True
        self._botones(False)
        self._conteo.set(f"Calculando ranking de {len(filas)} ofertas... "
                         "la primera vez tarda (carga del modelo).")
        threading.Thread(target=_hilo_ranking, args=(perfil, filas, self._cola),
                         daemon=True).start()

    def _ranking_listo(self, res):
        """Mezcla el resultado del ranking sobre las filas ya cargadas."""
        por_id = {r.get("job_id"): r for r in res}
        for f in self.todas:
            r = por_id.get(f["job_id"])
            if not r:
                continue
            a = r.get("ats") or {}
            f["score"] = r.get("combined")          # total = 60% sem + 40% ATS
            f["sem"] = round((r.get("score") or 0) * 100, 1)
            f["ats"] = a.get("score")
            f["veredicto"] = a.get("verdict", "")
            f["faltantes"] = a.get("missing") or []
            f["brk"] = r.get("secciones") or {}
            f["rankeada"] = True

        self._rank_activo = True
        self._tabla_rank.marcar("score", False)     # lo mejor arriba
        self._pintar_rank()

    # ------------------------------------------------------- busqueda libre
    def _buscar_libre(self):
        if self._corriendo:
            return
        q = self._consulta.get().strip()
        if not q:
            self._conteo.set("Escribe una consulta (ej.: mesa de ayuda bilingüe remoto).")
            return
        if not self.todas:
            self._conteo.set("No hay ofertas cargadas todavía.")
            return
        try:
            top = max(1, int(self._top.get() or 40))
        except ValueError:
            top = 40

        self._corriendo = True
        self._botones(False)
        self._conteo.set(f"Buscando “{q}”...")
        threading.Thread(target=_hilo_buscar,
                         args=(q, top, list(self.todas), self._cola),
                         daemon=True).start()

    def _libre_listo(self, q, res):
        out = []
        for r in res:
            f = dict(r)                            # trae puesto/empresa/links/etc.
            f["score"] = round((r.get("score") or 0) * 100, 1)
            # la busqueda libre NO puntu contra ATS: columnas siempre limpias
            f["ats"], f["veredicto"] = None, ""
            f["sem"], f["faltantes"] = f["score"], []
            out.append(f)
        self.libres = out

        self._tabla_libre.marcar("score", False)
        self._pintar_libre()
        if out:
            self._conteo.set(f"{len(out)} resultados para “{q}”")
        else:
            self._conteo.set(f"Sin resultados para “{q}”.")

    # ------------------------------------------------------------- comun
    def _botones(self, activos: bool):
        st = "normal" if activos else "disabled"
        self._btn_rank.configure(state=st)
        self._btn_buscar.configure(state=st)

    def _drenar(self):
        """Un solo punto de entrada al hilo de Tkinter: todo llega por cola."""
        try:
            while True:
                tipo, dato = self._cola.get_nowait()
                if tipo in ("rank", "libre", "err"):
                    self._corriendo = False
                    self._botones(True)
                if tipo == "rank":
                    self._ranking_listo(dato)
                elif tipo == "libre":
                    self._libre_listo(*dato)
                elif tipo == "sesion":
                    self._sesion_verificada(*dato)
                elif tipo == "cv_status":
                    self._perfil_estado.set(dato)
                    self._cv_progreso_estado(dato)
                elif tipo == "cv":
                    self._importando_cv = False
                    self._cv_progreso_cerrar()
                    self._perfil_cv_importado(*dato)
                elif tipo == "cv_err":
                    self._importando_cv = False
                    self._cv_progreso_cerrar()
                    self._perfil_estado.set(f"No se pudo importar el CV: {dato}")
                    messagebox.showerror("Importar CV", f"No se pudo importar: {dato}")
                else:
                    self._conteo.set(f"Error: {dato}")
        except queue.Empty:
            pass
        self._cv_progreso_estado()
        self._after_drenar_id = self.after(120, self._drenar)

    def _ver_detalle(self, f):
        t = self._detalle
        self._detalle_link = (f or {}).get("link") or ""
        self._btn_copiar_url.configure(
            state="normal" if self._detalle_link else "disabled")
        t.configure(state="normal")
        t.delete("1.0", "end")
        if f:
            t.insert("end", (f.get("puesto") or "") + "\n", "tit")
            linea = (f"{f.get('empresa', '-')}  ·  {f.get('ubicacion', '-')}"
                     f"  ·  {f.get('publicada', '')}")
            if f.get("ats") is not None:
                linea += (f"  ·  Total {f['score']}% (ATS {f['ats']:.0f}% · "
                          f"semántico {f['sem']:.0f}%)  ·  {f['veredicto']}")
            else:
                linea += f"  ·  Match {f.get('score')}%"
            if f.get("ia") is not None:
                linea += f"  ·  IA {f['ia']:.0f}/100"
            t.insert("end", linea + "\n")
            if f.get("ia_razon"):
                t.insert("end", f"IA: {f['ia_razon']}\n", "rot")
            t.insert("end", "\n")
            if self._detalle_link:
                t.insert("end", "Enlace\n", "rot")
                t.insert("end", self._detalle_link + "\n")
                t.insert("end", "(doble clic en la fila de la tabla para "
                                "abrirlo, o el botón de arriba para copiarlo)"
                                "\n\n")
            if f.get("faltantes"):
                t.insert("end", "Keywords ATS que te faltan\n", "rot")
                t.insert("end", ", ".join(f["faltantes"][:25]) + "\n\n")
            t.insert("end", "Descripción\n", "rot")
            t.insert("end", (f.get("descripcion") or "(sin descripción)")[:2500] + "\n\n")
            if f.get("requisitos"):
                t.insert("end", "Requisitos\n", "rot")
                t.insert("end", f["requisitos"][:1500])
        t.configure(state="disabled")

    def _abrir_link(self, f):
        if f and f.get("link"):
            webbrowser.open(f["link"])

    def _copiar_url(self):
        """Copia la URL del puesto mostrado en el panel de Detalle."""
        if not self._detalle_link:
            self._conteo.set("La oferta seleccionada no tiene URL.")
            return
        self.clipboard_clear()
        self.clipboard_append(self._detalle_link)
        self.update_idletasks()   # la URL queda en el portapapeles al instante
        self._conteo.set(f"URL copiada al portapapeles: {self._detalle_link}")
        self._btn_copiar_url.configure(text="✓ URL copiada")
        self.after(1800, lambda: self._btn_copiar_url.configure(
            text="Copiar URL del puesto"))

    # ------------------------------------------------------------- perfil
    # ------------------------------------------------------------- perfil
    def _pestana_perfil(self):
        tab = ttk.Frame(self.nb)

        barra = ttk.Frame(tab, padding=(10, 8))
        barra.pack(fill="x")
        ttk.Button(barra, text="Guardar perfil",
                   command=self._perfil_guardar).pack(side="left")
        ttk.Button(barra, text="Descartar cambios",
                   command=self._perfil_descartar).pack(side="left", padx=6)
        self._v_ia_cv = tk.BooleanVar(value=False)
        ttk.Button(barra, text="Importar CV (PDF/DOCX/TXT/MD)",
                   command=self._perfil_importar_cv).pack(side="left",
                                                          padx=(18, 0))
        ttk.Checkbutton(barra, text="Analizar con IA (API)",
                        variable=self._v_ia_cv).pack(side="left", padx=(8, 0))
        ttk.Label(barra, textvariable=self._perfil_estado,
                  style="Suave.TLabel").pack(side="left", padx=(12, 0))

        self._perfil_nb = ttk.Notebook(tab)
        self._perfil_nb.pack(fill="both", expand=True, padx=10, pady=(0, 8))
        self._perfil_tab_cv()
        self._perfil_tab_busqueda()
        self._perfil_tab_backups()

        self.nb.add(tab, text="  Perfil  ")
        self._perfil_cargar()

    # -------------------------------------------------- pestana Mi CV
    def _perfil_tab_cv(self):
        tab = ttk.Frame(self._perfil_nb)
        self._perfil_nb.add(tab, text="  Mi CV (Harvard/ATS)  ")
        sc = MarcoScroll(tab, self._canvas_tema)
        sc.pack(fill="both", expand=True)
        sc.interior.configure(padding=8)

        cont = ttk.LabelFrame(sc.interior, text="Contacto", padding=8)
        cont.pack(fill="x", pady=(0, 8))
        self._e_contacto = {}
        campos = (
            ("nombre", "Nombre"), ("email", "Email"),
            ("telefono", "Teléfono"), ("ubicacion", "Ubicación"),
            ("linkedin", "LinkedIn"), ("github", "GitHub"),
            ("portafolio", "Portafolio"),
        )
        for i, (clave, etiqueta) in enumerate(campos):
            fila, col = divmod(i, 2)
            ttk.Label(cont, text=etiqueta + ":").grid(
                row=fila, column=col * 2, sticky="w", padx=(0, 4), pady=2)
            e = ttk.Entry(cont, width=36)
            e.grid(row=fila, column=col * 2 + 1, sticky="we",
                   padx=(0, 14), pady=2)
            self._e_contacto[clave] = e
        cont.columnconfigure(1, weight=1)
        cont.columnconfigure(3, weight=1)

        cuerpo = ttk.Frame(sc.interior)
        cuerpo.pack(fill="both", expand=True)
        izq = ttk.Frame(cuerpo)
        izq.pack(side="left", fill="both", expand=True, padx=(0, 8))
        der = ttk.Frame(cuerpo)
        der.pack(side="left", fill="both", expand=True)

        self._t_resumen = self._caja_texto(
            izq, "Resumen profesional (2-4 líneas, opcional)", 3, expand=False)

        self._tree_edu, btns = self._lista_editor(
            izq, "Educación (más reciente primero)",
            ("institucion", "titulo", "fechas"),
            ("Institución", "Título", "Fechas"), (200, 150, 110), 5)
        ttk.Button(btns, text="Agregar", command=self._edu_add).pack(side="left")
        ttk.Button(btns, text="Editar", command=self._edu_edit).pack(side="left", padx=4)
        ttk.Button(btns, text="Eliminar", command=self._edu_del).pack(side="left")
        self._tree_edu.bind("<Double-1>", lambda _e: self._edu_edit())

        self._tree_exp, btns = self._lista_editor(
            izq, "Experiencia (más reciente primero)",
            ("empresa", "puesto", "fechas"),
            ("Empresa", "Puesto", "Fechas"), (160, 150, 110), 5)
        ttk.Button(btns, text="Agregar", command=self._exp_add).pack(side="left")
        ttk.Button(btns, text="Editar", command=self._exp_edit).pack(side="left", padx=4)
        ttk.Button(btns, text="Eliminar", command=self._exp_del).pack(side="left")
        self._tree_exp.bind("<Double-1>", lambda _e: self._exp_edit())

        self._tree_proy, btns = self._lista_editor(
            der, "Proyectos (opcional)",
            ("nombre", "tecnologias"),
            ("Nombre", "Tecnologías"), (160, 190), 4)
        ttk.Button(btns, text="Agregar", command=self._proy_add).pack(side="left")
        ttk.Button(btns, text="Editar", command=self._proy_edit).pack(side="left", padx=4)
        ttk.Button(btns, text="Eliminar", command=self._proy_del).pack(side="left")
        self._tree_proy.bind("<Double-1>", lambda _e: self._proy_edit())

        self._t_skills = self._caja_texto(
            der, "Habilidades (una por línea; palabras clave ATS)", 5, expand=False)
        self._t_certs = self._caja_texto(
            der, "Certificaciones (una por línea)", 3, expand=False)
        self._t_idiomas = self._caja_texto(
            der, "Idiomas — idioma | nivel", 3, expand=False)

        ttk.Button(der, text="Cargar propuesta IA (data/usuario/cv_ia.json)",
                   command=self._perfil_cargar_ia).pack(fill="x", pady=(4, 0))
        ttk.Button(der, text="Vista previa del CV Harvard/ATS",
                   command=self._perfil_preview).pack(fill="x", pady=(4, 0))
        ttk.Label(der, style="Suave.TLabel", wraplength=380, justify="left",
                  text="Importar CV está arriba: lee PDF, DOCX, TXT o MD y "
                       "precarga contacto, habilidades, certificaciones e "
                       "idiomas como borrador. Con 'Analizar con IA (API)' "
                       "activo también consulta el modelo elegido en Corrida: "
                       "envía el CV al proveedor y consume cuota (si falla, "
                       "queda la extracción local). Nada se guarda hasta que "
                       "aprietes Guardar perfil.").pack(anchor="w", pady=(4, 0))

        self._edu_items = []
        self._exp_items = []
        self._proy_items = []

    # -------------------------------------------------- pestana Busqueda
    def _perfil_tab_busqueda(self):
        tab = ttk.Frame(self._perfil_nb, padding=8)
        self._perfil_nb.add(tab, text="  Búsqueda (interno)  ")

        ttk.Label(tab, justify="left", wraplength=950, style="Suave.TLabel",
                  text="Esto es lo que JobPulse usa para buscar y puntuar. "
                       "Las habilidades, los años, la educación y el seniority "
                       "se calculan desde tu CV y se actualizan solos. Si "
                       "escribís un ajuste manual, queda fijo; dejalo vacío "
                       "para volver a automático.").pack(anchor="w")

        cuerpo = ttk.Frame(tab)
        cuerpo.pack(fill="both", expand=True, pady=(8, 0))
        izq = ttk.Frame(cuerpo)
        izq.pack(side="left", fill="both", expand=True, padx=(0, 8))
        der = ttk.Frame(cuerpo)
        der.pack(side="left", fill="both", expand=True)

        self._t_titulos = self._caja_texto(
            izq, "Puestos objetivo (uno por línea)", 6, expand=False)
        self._l_titulos_auto = ttk.Label(izq, style="Suave.TLabel", wraplength=400,
                                         justify="left", text="")
        self._l_titulos_auto.pack(anchor="w", pady=(0, 6))

        self._t_ubicaciones = self._caja_texto(
            izq, "Ubicaciones (una por línea)", 3, expand=False)
        self._l_ubicaciones_auto = ttk.Label(izq, style="Suave.TLabel",
                                             wraplength=400, justify="left",
                                             text="")
        self._l_ubicaciones_auto.pack(anchor="w", pady=(0, 6))

        self._t_temas = self._caja_texto(
            izq, "Temas manuales — puesto | puesto (EN) | ubicación "
                 "(vacío = automático)", 5, expand=False)
        self._l_temas_auto = ttk.Label(izq, style="Suave.TLabel", wraplength=400,
                                       justify="left", text="")
        self._l_temas_auto.pack(anchor="w")

        self._l_skills_auto = ttk.Label(der, style="Suave.TLabel", wraplength=400,
                                        justify="left", text="")
        self._l_skills_auto.pack(anchor="w", pady=(0, 4))
        self._t_skills_extra = self._caja_texto(
            der, "Skills extra para buscar (una por línea)", 3, expand=False)
        self._t_skills_excluir = self._caja_texto(
            der, "Skills a excluir (una por línea)", 2, expand=False)

        ov = ttk.LabelFrame(der, text="Ajustes manuales (vacío = automático)",
                            padding=8)
        ov.pack(fill="x", pady=(4, 8))
        ttk.Label(ov, text="Experiencia (años):").grid(row=0, column=0,
                                                       sticky="w", pady=2)
        self._e_ov_exp = ttk.Entry(ov, width=10)
        self._e_ov_exp.grid(row=0, column=1, sticky="w", pady=2)
        self._l_auto_exp = ttk.Label(ov, style="Suave.TLabel", text="")
        self._l_auto_exp.grid(row=0, column=2, sticky="w", padx=(8, 0))
        ttk.Label(ov, text="Educación:").grid(row=1, column=0, sticky="w", pady=2)
        self._e_ov_edu = ttk.Entry(ov, width=18)
        self._e_ov_edu.grid(row=1, column=1, sticky="w", pady=2)
        self._l_auto_edu = ttk.Label(ov, style="Suave.TLabel", text="")
        self._l_auto_edu.grid(row=1, column=2, sticky="w", padx=(8, 0))
        ttk.Label(ov, text="Seniority:").grid(row=2, column=0, sticky="w", pady=2)
        self._e_ov_sen = ttk.Entry(ov, width=14)
        self._e_ov_sen.grid(row=2, column=1, sticky="w", pady=2)
        self._l_auto_sen = ttk.Label(ov, style="Suave.TLabel", text="")
        self._l_auto_sen.grid(row=2, column=2, sticky="w", padx=(8, 0))

        pes = ttk.LabelFrame(der, text="Pesos de cada sección", padding=8)
        pes.pack(fill="x")
        self._v_pesos = {}
        for i, (et, clave, defecto) in enumerate((
                ("Título", "titulo", 1.0), ("Skills", "skills", 0.7),
                ("Tema", "tema", 0.5))):
            v = tk.StringVar(value=str(defecto))
            self._v_pesos[clave] = v
            ttk.Label(pes, text=et + ":").grid(row=i, column=0, sticky="w", pady=2)
            ttk.Spinbox(pes, from_=0.0, to=2.0, increment=0.05, width=6,
                        textvariable=v).grid(row=i, column=1, sticky="w", pady=2)
        self._l_pats = ttk.Label(pes, style="Suave.TLabel", wraplength=380,
                                 justify="left", text="Score ATS: —")
        self._l_pats.grid(row=3, column=0, columnspan=2, sticky="w", pady=(6, 0))

    # -------------------------------------------------- pestana Backups
    def _perfil_tab_backups(self):
        tab = ttk.Frame(self._perfil_nb, padding=8)
        self._perfil_nb.add(tab, text="  Backups  ")
        ttk.Label(tab, style="Suave.TLabel", wraplength=900, justify="left",
                  text="Cada guardado hace backup del perfil anterior. Si algo "
                       "sale mal, restaurá una versión desde acá.").pack(anchor="w")
        self._tree_bk = ttk.Treeview(tab, columns=("archivo", "fecha"),
                                     show="headings", height=12,
                                     selectmode="browse")
        self._tree_bk.heading("archivo", text="Archivo")
        self._tree_bk.heading("fecha", text="Fecha")
        self._tree_bk.column("archivo", width=520, anchor="w")
        self._tree_bk.column("fecha", width=180, anchor="center")
        self._tree_bk.pack(side="left", fill="both", expand=True, pady=(8, 0))
        acciones = ttk.Frame(tab)
        acciones.pack(side="left", fill="y", padx=(10, 0), pady=(8, 0))
        ttk.Button(acciones, text="Restaurar seleccionado",
                   command=self._perfil_restaurar).pack(fill="x")
        ttk.Button(acciones, text="Eliminar seleccionado",
                   command=self._perfil_eliminar_backup).pack(
                       fill="x", pady=(8, 0))
        ttk.Button(acciones, text="Eliminar todos los backups",
                   command=self._perfil_eliminar_backups).pack(
                       fill="x", pady=(8, 0))

    def _caja_texto(self, parent, titulo, alto, expand=False):
        """LabelFrame con un Text multilinea + scrollbar (lista editable)."""
        marco = ttk.LabelFrame(parent, text=titulo, padding=6)
        marco.pack(fill="both" if expand else "x", expand=expand, pady=(0, 8))
        t = tk.Text(marco, height=alto, wrap="word", undo=True,
                    font=("Segoe UI", 10))
        self._texts_tema.append(t)     # repintar al cambiar tema
        sb = ttk.Scrollbar(marco, orient="vertical", command=t.yview)
        t.configure(yscrollcommand=sb.set)
        t.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        return t

    def _lista_editor(self, parent, titulo, columnas, cabeceras, anchos, alto):
        marco = ttk.LabelFrame(parent, text=titulo, padding=6)
        marco.pack(fill="both", expand=True, pady=(0, 8))
        cuerpo = ttk.Frame(marco)
        cuerpo.pack(fill="both", expand=True)
        tree = ttk.Treeview(cuerpo, columns=columnas, show="headings",
                            height=alto, selectmode="browse")
        for col, cab, ancho in zip(columnas, cabeceras, anchos):
            tree.heading(col, text=cab)
            tree.column(col, width=ancho, anchor="w")
        sb = ttk.Scrollbar(cuerpo, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        botones = ttk.Frame(marco)
        botones.pack(fill="x", pady=(4, 0))
        return tree, botones

    # -------------------------------------------------- dialogos de registros
    @staticmethod
    def _campos_edu():
        return [
            ("institucion", "Institución"), ("titulo", "Título"),
            ("campo", "Campo / carrera"), ("ubicacion", "Ubicación"),
            ("inicio", "Inicio (MM/AAAA)"), ("fin", "Fin (MM/AAAA)"),
            ("gpa", "GPA / nota"), ("honores", "Honores"),
            ("cursos", "Cursos relevantes (uno por línea)"),
        ]

    @staticmethod
    def _campos_exp():
        return [
            ("empresa", "Empresa"), ("puesto", "Puesto"),
            ("ubicacion", "Ubicación"), ("inicio", "Inicio (MM/AAAA)"),
            ("fin", "Fin (MM/AAAA o Actualidad)"),
            ("logros", "Logros (uno por línea, con verbo de acción)"),
        ]

    @staticmethod
    def _campos_proy():
        return [
            ("nombre", "Nombre"), ("tecnologias", "Tecnologías (una por línea)"),
            ("inicio", "Inicio"), ("fin", "Fin"), ("link", "Link"),
            ("descripcion", "Descripción"),
        ]

    def _dialogo_registro(self, titulo, campos, valores=None, multilinea=None):
        valores = valores or {}
        multilinea = multilinea or {}
        win = tk.Toplevel(self)
        win.title(titulo)
        win.transient(self)
        win.grab_set()
        marco = ttk.Frame(win, padding=10)
        marco.pack(fill="both", expand=True)
        widgets = {}
        for i, (clave, etiqueta) in enumerate(campos):
            ttk.Label(marco, text=etiqueta + ":").grid(
                row=i, column=0, sticky="nw", pady=3, padx=(0, 6))
            if clave in multilinea:
                w = tk.Text(marco, height=multilinea[clave], width=52,
                            wrap="word", undo=True)
                w.grid(row=i, column=1, sticky="we", pady=3)
                val = valores.get(clave)
                if isinstance(val, list):
                    val = "\n".join(str(x) for x in val)
                if val:
                    w.insert("1.0", str(val))
            else:
                w = ttk.Entry(marco, width=54)
                w.grid(row=i, column=1, sticky="we", pady=3)
                w.insert(0, str(valores.get(clave) or ""))
            widgets[clave] = w
        marco.columnconfigure(1, weight=1)
        resultado = {}

        def guardar():
            for clave, _ in campos:
                w = widgets[clave]
                if isinstance(w, tk.Text):
                    texto = w.get("1.0", "end").strip()
                    if clave in ("cursos", "logros", "tecnologias"):
                        resultado[clave] = [l.strip() for l in texto.splitlines()
                                            if l.strip()]
                    else:
                        resultado[clave] = re.sub(r"\s+", " ", texto).strip()
                else:
                    resultado[clave] = w.get().strip()
            if not any(v for v in resultado.values()):
                messagebox.showwarning("Registro vacío",
                                       "Completá al menos un campo.",
                                       parent=win)
                return
            win.destroy()

        def cancelar():
            resultado.clear()
            win.destroy()

        btns = ttk.Frame(marco)
        btns.grid(row=len(campos), column=0, columnspan=2, sticky="e",
                  pady=(10, 0))
        ttk.Button(btns, text="Guardar", command=guardar).pack(side="left")
        ttk.Button(btns, text="Cancelar", command=cancelar).pack(side="left", padx=6)
        win.wait_window()
        return resultado or None

    @staticmethod
    def _indice_seleccionado(tree):
        sel = tree.selection()
        if not sel:
            return None
        return tree.index(sel[0])

    # -------------------------------------------------- educacion
    def _refrescar_edu(self):
        if not hasattr(self, "_tree_edu"):
            return
        self._tree_edu.delete(*self._tree_edu.get_children())
        for i, e in enumerate(self._edu_items):
            fechas = f"{e.get('inicio', '')} – {e.get('fin', '')}".strip(" –")
            self._tree_edu.insert("", "end", iid=str(i), values=(
                e.get("institucion", ""), e.get("titulo", ""), fechas))

    def _edu_add(self):
        d = self._dialogo_registro("Agregar educación", self._campos_edu(),
                                   multilinea={"cursos": 3})
        if d:
            self._edu_items.append(d)
            self._refrescar_edu()

    def _edu_edit(self):
        idx = self._indice_seleccionado(self._tree_edu)
        if idx is None:
            return
        d = self._dialogo_registro("Editar educación", self._campos_edu(),
                                   self._edu_items[idx],
                                   multilinea={"cursos": 3})
        if d:
            self._edu_items[idx] = d
            self._refrescar_edu()

    def _edu_del(self):
        idx = self._indice_seleccionado(self._tree_edu)
        if idx is None:
            return
        del self._edu_items[idx]
        self._refrescar_edu()

    # -------------------------------------------------- experiencia
    def _refrescar_exp(self):
        if not hasattr(self, "_tree_exp"):
            return
        self._tree_exp.delete(*self._tree_exp.get_children())
        for i, e in enumerate(self._exp_items):
            fechas = f"{e.get('inicio', '')} – {e.get('fin', '')}".strip(" –")
            self._tree_exp.insert("", "end", iid=str(i), values=(
                e.get("empresa", ""), e.get("puesto", ""), fechas))

    def _exp_add(self):
        d = self._dialogo_registro("Agregar experiencia", self._campos_exp(),
                                   multilinea={"logros": 5})
        if d:
            self._exp_items.append(d)
            self._refrescar_exp()

    def _exp_edit(self):
        idx = self._indice_seleccionado(self._tree_exp)
        if idx is None:
            return
        d = self._dialogo_registro("Editar experiencia", self._campos_exp(),
                                   self._exp_items[idx],
                                   multilinea={"logros": 5})
        if d:
            self._exp_items[idx] = d
            self._refrescar_exp()

    def _exp_del(self):
        idx = self._indice_seleccionado(self._tree_exp)
        if idx is None:
            return
        del self._exp_items[idx]
        self._refrescar_exp()

    # -------------------------------------------------- proyectos
    def _refrescar_proy(self):
        if not hasattr(self, "_tree_proy"):
            return
        self._tree_proy.delete(*self._tree_proy.get_children())
        for i, p in enumerate(self._proy_items):
            self._tree_proy.insert("", "end", iid=str(i), values=(
                p.get("nombre", ""), ", ".join(p.get("tecnologias") or [])))

    def _proy_add(self):
        d = self._dialogo_registro("Agregar proyecto", self._campos_proy(),
                                   multilinea={"tecnologias": 3, "descripcion": 4})
        if d:
            self._proy_items.append(d)
            self._refrescar_proy()

    def _proy_edit(self):
        idx = self._indice_seleccionado(self._tree_proy)
        if idx is None:
            return
        d = self._dialogo_registro("Editar proyecto", self._campos_proy(),
                                   self._proy_items[idx],
                                   multilinea={"tecnologias": 3, "descripcion": 4})
        if d:
            self._proy_items[idx] = d
            self._refrescar_proy()

    def _proy_del(self):
        idx = self._indice_seleccionado(self._tree_proy)
        if idx is None:
            return
        del self._proy_items[idx]
        self._refrescar_proy()

    # -------------------------------------------------- carga / guardado
    @staticmethod
    def _set_texto(t, items):
        t.delete("1.0", "end")
        if items:
            t.insert("1.0", "\n".join(str(x) for x in items))

    def _perfil_pintar(self, perfil):
        cv = perfil["cv"]
        b = perfil["busqueda"]
        self._e_contacto["nombre"].delete(0, "end")
        self._e_contacto["nombre"].insert(0, cv.get("nombre") or "")
        for clave in ("email", "telefono", "ubicacion", "linkedin",
                      "github", "portafolio"):
            e = self._e_contacto[clave]
            e.delete(0, "end")
            e.insert(0, (cv.get("contacto") or {}).get(clave, ""))

        self._t_resumen.delete("1.0", "end")
        if cv.get("resumen"):
            self._t_resumen.insert("1.0", cv["resumen"])

        self._edu_items = copy.deepcopy(cv.get("educacion") or [])
        self._exp_items = copy.deepcopy(cv.get("experiencia") or [])
        self._proy_items = copy.deepcopy(cv.get("proyectos") or [])
        self._refrescar_edu()
        self._refrescar_exp()
        self._refrescar_proy()

        self._set_texto(self._t_skills, cv.get("habilidades") or [])
        self._set_texto(self._t_certs, cv.get("certificaciones") or [])
        self._set_texto(self._t_idiomas, [
            " | ".join(x for x in (i.get("idioma", ""), i.get("nivel", "")) if x)
            for i in (cv.get("idiomas") or []) if isinstance(i, dict)
        ])

        self._set_texto(self._t_titulos, b.get("puestos_objetivo") or [])
        self._set_texto(self._t_ubicaciones, b.get("ubicaciones") or [])
        self._set_texto(self._t_temas, [
            " | ".join(x for x in (t.get("puesto", ""), t.get("puesto_en", ""),
                                   t.get("ubicacion", "")) if x)
            for t in (b.get("temas") or []) if isinstance(t, dict)
        ])
        self._set_texto(self._t_skills_extra, b.get("skills_extra") or [])
        self._set_texto(self._t_skills_excluir, b.get("skills_excluir") or [])

        ov = b.get("overrides") or {}
        self._e_ov_exp.delete(0, "end")
        if ov.get("experiencia_anios") not in (None, ""):
            self._e_ov_exp.insert(0, str(ov["experiencia_anios"]))
        self._e_ov_edu.delete(0, "end")
        if ov.get("educacion_nivel"):
            self._e_ov_edu.insert(0, str(ov["educacion_nivel"]))
        self._e_ov_sen.delete(0, "end")
        if ov.get("seniority"):
            self._e_ov_sen.insert(0, str(ov["seniority"]))

        pesos = b.get("pesos") or {}
        for clave, defecto in (("titulo", 1.0), ("skills", 0.7), ("tema", 0.5)):
            self._v_pesos[clave].set(str(float(pesos.get(clave, defecto))))

        # Derivados y etiquetas de apoyo
        p = CandidateProfile(perfil)
        self._l_skills_auto.configure(
            text="Habilidades que usará el buscador: "
                 + (", ".join(p.skills) or "(ninguna)"))
        self._l_auto_exp.configure(
            text=f"Automático desde el CV: {p.experiencia_anios:g} años")
        self._l_auto_edu.configure(
            text=f"Automático: {p.educacion_nivel or 'no detectada'}")
        self._l_auto_sen.configure(
            text=f"Automático: {p.seniority or 'no detectado'}")
        if b.get("puestos_objetivo"):
            self._l_titulos_auto.configure(text="Puestos objetivo manuales.")
        else:
            self._l_titulos_auto.configure(
                text="Sugeridos desde tu CV: "
                     + (", ".join(p.titulos) or "(sin experiencia cargada)"))
        if b.get("ubicaciones"):
            self._l_ubicaciones_auto.configure(text="Ubicaciones manuales.")
        else:
            self._l_ubicaciones_auto.configure(
                text="Automático desde el CV: "
                     + (p.contacto.get("ubicacion") or "Costa Rica"))
        if b.get("temas"):
            self._l_temas_auto.configure(text="Temas manuales.")
        else:
            self._l_temas_auto.configure(
                text="Se generarán: " + ("; ".join(
                    f"{t['puesto']} ({t['ubicacion']})" for t in p.temas)
                    or "(sin puestos objetivo)"))
        pa = b.get("pesos_ats") or {}
        self._l_pats.configure(
            text="Score ATS: "
                 + (" · ".join(f"{k} {float(v):g}" for k, v in pa.items()) or "—"))

    def _perfil_cargar(self, aviso: str = ""):
        ruta = DEFAULT_PROFILE_PATH
        if not ruta.exists():
            self._perfil_datos = normalizar_perfil({})
            self._perfil_pintar(self._perfil_datos)
            self._perfil_estado.set(
                "No existe profile.json todavía: al Guardar se crea uno nuevo "
                "(o usá cv2profile.py / Importar CV para generarlo).")
            self._perfil_backups_pintar()
            return
        try:
            datos = json.loads(ruta.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            self._perfil_datos = normalizar_perfil({})
            self._perfil_pintar(self._perfil_datos)
            self._perfil_estado.set(f"Perfil ilegible ({ruta.name}): {exc}")
            return
        self._perfil_datos = normalizar_perfil(datos)
        self._perfil_pintar(self._perfil_datos)
        self._perfil_estado.set(
            aviso or "Editá tu CV y guardá: se hace backup del anterior.")
        self._perfil_backups_pintar()

    def _perfil_recolectar(self):
        """Construye el perfil canónico con lo editado en los widgets."""
        nuevo = copy.deepcopy(self._perfil_datos or normalizar_perfil({}))
        cv = nuevo.setdefault("cv", {})
        b = nuevo.setdefault("busqueda", {})

        cv["nombre"] = self._e_contacto["nombre"].get().strip()
        contacto = {}
        for clave in ("email", "telefono", "ubicacion", "linkedin",
                      "github", "portafolio"):
            v = self._e_contacto[clave].get().strip()
            if v:
                contacto[clave] = v
        cv["contacto"] = contacto
        cv["resumen"] = self._t_resumen.get("1.0", "end").strip()
        cv["educacion"] = copy.deepcopy(self._edu_items)
        cv["experiencia"] = copy.deepcopy(self._exp_items)
        cv["proyectos"] = copy.deepcopy(self._proy_items)
        cv["habilidades"] = _lineas(self._t_skills.get("1.0", "end"))
        cv["certificaciones"] = _lineas(self._t_certs.get("1.0", "end"))
        cv["idiomas"] = _idiomas_de_lineas(
            _lineas(self._t_idiomas.get("1.0", "end")))

        b["puestos_objetivo"] = _lineas(self._t_titulos.get("1.0", "end"))
        b["ubicaciones"] = _lineas(self._t_ubicaciones.get("1.0", "end"))
        b["temas"] = _temas_de_lineas(_lineas(self._t_temas.get("1.0", "end")))
        b["skills_extra"] = _lineas(self._t_skills_extra.get("1.0", "end"))
        b["skills_excluir"] = _lineas(self._t_skills_excluir.get("1.0", "end"))

        overrides = {}
        v = self._e_ov_exp.get().strip()
        if v:
            try:
                overrides["experiencia_anios"] = float(v)
            except ValueError:
                pass
        v = self._e_ov_edu.get().strip()
        if v:
            overrides["educacion_nivel"] = v
        v = self._e_ov_sen.get().strip()
        if v:
            overrides["seniority"] = v
        b["overrides"] = overrides

        try:
            b["pesos"] = {clave: float(self._v_pesos[clave].get() or defecto)
                          for clave, defecto in (("titulo", 1.0),
                                                 ("skills", 0.7),
                                                 ("tema", 0.5))}
        except ValueError:
            pass    # peso ilegible: se conserva el que hay en disco

        return normalizar_perfil(nuevo)

    def _advertir_perfil_incompleto(self, faltantes: list) -> bool:
        """Ventana de advertencia; True si el usuario decide guardar igual."""
        win = tk.Toplevel(self)
        win.title("Perfil incompleto")
        win.transient(self)
        win.resizable(False, False)
        marco = ttk.Frame(win, padding=16)
        marco.pack(fill="both", expand=True)
        ttk.Label(marco, text="Faltan datos importantes",
                  font=("Segoe UI", 11, "bold")).pack(anchor="w")
        ttk.Label(
            marco, style="Suave.TLabel", wraplength=540, justify="left",
            text="El perfil se puede guardar, pero el ranking y el ATS pueden "
                 "quedar incompletos. Revisá estos puntos:").pack(
                     anchor="w", pady=(4, 10))
        for texto in faltantes:
            ttk.Label(marco, text=f"• {texto}", style="Suave.TLabel",
                      wraplength=540, justify="left").pack(
                          anchor="w", pady=(0, 4))

        botones = ttk.Frame(marco)
        botones.pack(fill="x", pady=(12, 0))
        resultado = {"guardar": False}

        def volver(_e=None):
            win.destroy()

        def guardar_igual():
            resultado["guardar"] = True
            win.destroy()

        ttk.Button(botones, text="Volver a revisar",
                   command=volver).pack(side="right")
        ttk.Button(botones, text="Guardar igual",
                   command=guardar_igual).pack(side="right", padx=(0, 8))
        win.protocol("WM_DELETE_WINDOW", volver)
        win.bind("<Escape>", volver)
        win.update_idletasks()
        x = self.winfo_rootx() + (self.winfo_width() - win.winfo_width()) // 2
        y = self.winfo_rooty() + (self.winfo_height() - win.winfo_height()) // 2
        win.geometry(f"+{max(0, x)}+{max(0, y)}")
        win.lift()
        win.grab_set()
        self.wait_window(win)
        return resultado["guardar"]

    def _perfil_guardar(self):
        nuevo = self._perfil_recolectar()
        if _perfil_vacio(nuevo):
            messagebox.showerror(
                "Perfil vacío",
                "No guardo: quedaría sin puestos objetivo, skills ni temas "
                "(no sirve para puntuar ni para buscar).")
            return
        cambios = _resumen_cambios(self._perfil_datos or {}, nuevo)
        if not cambios:
            self._perfil_estado.set("Sin cambios pendientes.")
            return
        if not messagebox.askyesno(
                "Guardar perfil",
                "Cambios a guardar:\n\n" + "\n".join(cambios[:20])
                + ("\n..." if len(cambios) > 20 else "")
                + "\n\n¿Guardar ahora?"):
            return
        faltantes = _perfil_campos_faltantes(nuevo)
        if faltantes and not self._advertir_perfil_incompleto(faltantes):
            self._perfil_estado.set(
                "Guardado cancelado: revisá los datos importantes faltantes.")
            return
        bk = guardar_perfil(nuevo, DEFAULT_PROFILE_PATH)
        self._perfil_datos = nuevo
        msg = "Perfil guardado. El próximo ranking ya usa estos valores."
        if bk:
            msg += f" (backup: {bk.name})"
        self._perfil_cargar(aviso=msg)

    def _perfil_descartar(self):
        self._perfil_cargar(aviso="Cambios descartados: se recargó lo que "
                                   "hay en disco.")

    def _perfil_importar_cv(self):
        """Elegí un CV soportado y precargá sus datos en el editor (borrador)."""
        if getattr(self, "_importando_cv", False):
            self._perfil_estado.set("Ya estoy analizando un CV; esperá un momento.")
            return
        ruta = filedialog.askopenfilename(
            title="Importar CV",
            filetypes=[
                ("CV soportado", "*.pdf *.docx *.txt *.md"),
                ("PDF", "*.pdf"),
                ("Word (.docx)", "*.docx"),
                ("Texto / Markdown", "*.txt *.md"),
                ("Todos los archivos", "*.*"),
            ])
        if not ruta:
            return
        usar_ia = bool(self._v_ia_cv.get()) if hasattr(self, "_v_ia_cv") else False
        proveedor = ""
        modelo = ""
        if usar_ia:
            pvar = getattr(self, "_ia_proveedor_var", None)
            proveedor = ((pvar.get() if pvar else self._ia_proveedor_guardado())
                         or "").strip().lower()
            var = getattr(self, "_ia_modelo_var", None)
            modelo = ((var.get() if var else self._ia_modelo_guardado(proveedor))
                      or "").strip().replace('"', "")
        vacantes = (list(getattr(self, "todas", None) or [])[:25]
                    if usar_ia else None)
        actual = self._perfil_recolectar()
        self._importando_cv = True
        if usar_ia:
            self._cv_progreso_abrir(Path(ruta).name, proveedor, modelo)
        que = " y consultando el modelo de la API" if usar_ia else ""
        self._perfil_estado.set(
            f"Analizando {Path(ruta).name}{que}... "
            "(la primera vez tarda: carga el modelo)")
        threading.Thread(target=_hilo_importar_cv,
                         args=(ruta, actual, self._cola, usar_ia, modelo,
                               vacantes, proveedor),
                         daemon=True).start()

    def _cv_progreso_abrir(self, nombre: str, proveedor: str = "",
                           modelo: str = ""):
        """Ventana con barra de progreso para el análisis opcional con IA.

        El trabajo corre en un hilo; esta ventana solo muestra estado/tiempo
        y se cierra sola al llegar el resultado o el error.
        """
        self._cv_progreso_cerrar()
        win = tk.Toplevel(self)
        win.title("Analizando CV con IA (API)")
        win.transient(self)
        win.resizable(False, False)
        marco = ttk.Frame(win, padding=18)
        marco.pack(fill="both", expand=True)
        ttk.Label(marco, text=f"CV: {nombre}",
                  font=("Segoe UI", 10, "bold")).pack(anchor="w")
        detalle = " · ".join(x for x in (proveedor, modelo) if x)
        if detalle:
            ttk.Label(marco, text=detalle, style="Suave.TLabel").pack(
                anchor="w", pady=(2, 0))
        self._cv_prog_txt = ttk.Label(
            marco, text="Preparando el análisis...", style="Suave.TLabel",
            wraplength=430, justify="left")
        self._cv_prog_txt.pack(anchor="w", pady=(8, 10))
        self._cv_prog_bar = ttk.Progressbar(marco, mode="indeterminate",
                                            length=430)
        self._cv_prog_bar.pack(fill="x")
        self._cv_prog_bar.start(12)
        self._cv_prog_tiempo = ttk.Label(marco, text="0 s",
                                         style="Suave.TLabel")
        self._cv_prog_tiempo.pack(anchor="w", pady=(8, 0))
        ttk.Label(marco, style="Suave.TLabel", wraplength=430,
                  justify="left",
                  text="Se envía el texto del CV a la API elegida. Podés "
                       "cerrar esta ventana: el análisis sigue y el resultado "
                       "aparece al terminar.").pack(anchor="w", pady=(4, 0))
        win.protocol("WM_DELETE_WINDOW", self._cv_progreso_cerrar)
        win.update_idletasks()
        x = self.winfo_rootx() + (self.winfo_width() - win.winfo_width()) // 2
        y = self.winfo_rooty() + (self.winfo_height() - win.winfo_height()) // 2
        win.geometry(f"+{max(0, x)}+{max(0, y)}")
        self._cv_prog_win = win
        self._cv_prog_inicio = time.monotonic()

    def _cv_progreso_estado(self, texto: str = ""):
        if not getattr(self, "_cv_prog_win", None):
            return
        if texto:
            self._cv_prog_txt.configure(text=texto)
        if getattr(self, "_cv_prog_inicio", None):
            seg = int(time.monotonic() - self._cv_prog_inicio)
            self._cv_prog_tiempo.configure(text=f"{seg} s transcurridos")

    def _cv_progreso_cerrar(self):
        bar = getattr(self, "_cv_prog_bar", None)
        if bar is not None:
            try:
                bar.stop()
            except tk.TclError:
                pass
        win = getattr(self, "_cv_prog_win", None)
        if win is not None:
            try:
                win.destroy()
            except tk.TclError:
                pass
        self._cv_prog_win = None
        self._cv_prog_bar = None
        self._cv_prog_txt = None
        self._cv_prog_tiempo = None

    def _perfil_cv_importado(self, nombre: str, merged: dict,
                             evaluacion: dict, aviso: str):
        """Muestra los cambios del CV importado y los aplica como borrador."""
        actual = self._perfil_recolectar()
        cambios = _resumen_cambios(actual, merged)
        resumen = "\n".join(cambios[:18]) + ("\n..." if len(cambios) > 18 else "")
        detalle = ""
        if evaluacion:
            punt = evaluacion.get("puntaje")
            punt_txt = (f"{punt:g}/100"
                        if isinstance(punt, (int, float)) else "sin puntaje")
            detalle = (f"\n\nIA (API): {punt_txt} — "
                       f"{evaluacion.get('resumen') or 'sin resumen'}")
        if aviso:
            detalle += f"\n\n⚠ {aviso}"
        faltantes = _perfil_campos_faltantes(merged)
        if faltantes:
            detalle += ("\n\n⚠ Datos importantes incompletos:\n"
                        + "\n".join(f"• {f}" for f in faltantes[:8])
                        + ("\n..." if len(faltantes) > 8 else ""))
        if not messagebox.askyesno(
                "Importar CV (borrador)",
                f"CV: {nombre}\n\nDatos detectados:\n\n"
                + (resumen or "(sin cambios visibles en el editor)")
                + detalle
                + "\n\n¿Cargarlos en el editor? NO se guarda nada hasta que "
                  "aprietes 'Guardar perfil' y revises el CV."):
            self._perfil_estado.set("Importación de CV cancelada.")
            return
        self._perfil_datos = merged
        self._perfil_pintar(merged)
        msg = (f"CV «{nombre}» importado como borrador. "
               "Revisá Mi CV y Guardar perfil.")
        if evaluacion:
            msg = (f"CV «{nombre}» importado (IA: {evaluacion.get('puntaje')}"
                   "/100). Revisá Mi CV y Guardar perfil.")
        self._perfil_estado.set(msg)

    def _perfil_cargar_ia(self):
        """Carga data/usuario/cv_ia.json como BORRADOR en el editor, sin guardar.

        La IA solo propone: el usuario revisa, corrige y recién ahí guarda.
        """
        ruta = CV_IA_JSON
        if not ruta.exists():
            messagebox.showinfo(
                "Sin propuesta IA",
                "Todavía no hay data/usuario/cv_ia.json.\n\nPrimero usá "
                "'Evaluar CV con IA' en la pestaña Corrida (sin aplicar el "
                "perfil), y después volvé acá para revisarlo.")
            return
        try:
            data = json.loads(ruta.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            messagebox.showerror("Propuesta IA", f"No se pudo leer {ruta.name}: {exc}")
            return
        propuesta = (data.get("resultado") or {}).get("perfil")
        if not propuesta:
            messagebox.showerror("Propuesta IA",
                                 f"{ruta.name} no trae un perfil utilizable.")
            return
        actual = self._perfil_recolectar()
        try:
            merged = merge_profile(actual, propuesta, replace=True)
        except Exception as exc:
            messagebox.showerror("Propuesta IA", f"No se pudo fusionar: {exc}")
            return
        cambios = _resumen_cambios(actual, merged)
        resumen = "\n".join(cambios[:18]) + ("\n..." if len(cambios) > 18 else "")
        faltantes = _perfil_campos_faltantes(merged)
        aviso_falt = ""
        if faltantes:
            aviso_falt = ("\n\n⚠ Datos importantes incompletos:\n"
                          + "\n".join(f"• {f}" for f in faltantes[:8])
                          + ("\n..." if len(faltantes) > 8 else ""))
        if not messagebox.askyesno(
                "Cargar propuesta IA (borrador)",
                "La IA propone estos cambios:\n\n"
                + (resumen or "(sin cambios visibles; puede ser solo "
                              "reordenamiento o datos internos)")
                + aviso_falt
                + "\n\n¿Cargarlos en el editor? NO se guarda nada hasta que "
                  "aprietes 'Guardar perfil' y revises el CV."):
            return
        self._perfil_datos = merged
        self._perfil_pintar(merged)
        self._perfil_estado.set(
            "Propuesta IA cargada como borrador. Revisá Mi CV y Guardar perfil.")

    def _perfil_preview(self):
        datos = self._perfil_recolectar()
        texto = cv_harvard_markdown(datos)
        win = tk.Toplevel(self)
        win.title("Vista previa CV Harvard/ATS")
        win.geometry("900x700")
        marco = ttk.Frame(win, padding=8)
        marco.pack(fill="both", expand=True)
        t = tk.Text(marco, wrap="word", font=("Segoe UI", 10))
        sb = ttk.Scrollbar(marco, orient="vertical", command=t.yview)
        t.configure(yscrollcommand=sb.set)
        t.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        t.insert("1.0", texto)
        t.configure(state="disabled")
        btns = ttk.Frame(win, padding=(8, 0, 8, 8))
        btns.pack(fill="x")

        def copiar():
            self.clipboard_clear()
            self.clipboard_append(texto)
            self._perfil_estado.set("CV copiado al portapapeles.")

        ttk.Button(btns, text="Copiar", command=copiar).pack(side="left")
        ttk.Button(btns, text="Cerrar", command=win.destroy).pack(side="left", padx=6)

    def _perfil_backups_pintar(self):
        if not hasattr(self, "_tree_bk"):
            return
        hijos = self._tree_bk.get_children()
        if hijos:
            self._tree_bk.delete(*hijos)
        bks = backups_perfil(DEFAULT_PROFILE_PATH)[:20]
        self._bk_rutas = {}
        for i, bk in enumerate(bks):
            self._bk_rutas[f"bk{i}"] = bk
            fecha = datetime.fromtimestamp(bk.stat().st_mtime)
            self._tree_bk.insert("", "end", iid=f"bk{i}",
                                 values=(bk.name, f"{fecha:%d/%m/%Y %H:%M}"))

    def _perfil_restaurar(self):
        sel = self._tree_bk.selection()
        if not sel:
            self._perfil_estado.set("Elegí un backup de la lista primero.")
            return
        bk = self._bk_rutas.get(sel[0])
        if bk is None or not bk.exists():
            return
        if not messagebox.askyesno(
                "Restaurar backup",
                f"¿Restaurar {bk.name}?\n"
                "Tu versión actual queda guardada como backup."):
            return
        ruta = DEFAULT_PROFILE_PATH
        try:
            datos = json.loads(bk.read_text(encoding="utf-8"))
            guardar_perfil(datos, ruta)
        except (OSError, json.JSONDecodeError) as exc:
            messagebox.showerror("Restaurar", f"No se pudo restaurar: {exc}")
            return
        self._perfil_cargar(aviso=f"Restaurado {bk.name}.")

    def _perfil_eliminar_backup(self):
        sel = self._tree_bk.selection()
        if not sel:
            self._perfil_estado.set("Elegí un backup de la lista primero.")
            return
        bk = self._bk_rutas.get(sel[0])
        if bk is None or not bk.exists():
            self._perfil_backups_pintar()
            return
        if not messagebox.askyesno(
                "Eliminar backup",
                f"¿Eliminar {bk.name}?\n\n"
                "Esta acción no se puede deshacer.",
                icon=messagebox.WARNING):
            return
        try:
            bk.unlink()
        except OSError as exc:
            messagebox.showerror(
                "Eliminar backup",
                f"No se pudo eliminar {bk.name}: {exc}")
            return
        self._perfil_backups_pintar()
        self._perfil_estado.set(f"Backup eliminado: {bk.name}")

    def _perfil_eliminar_backups(self):
        bks = backups_perfil(DEFAULT_PROFILE_PATH)
        if not bks:
            self._perfil_estado.set("No hay backups para eliminar.")
            return
        if not messagebox.askyesno(
                "Eliminar todos los backups",
                f"¿Eliminar los {len(bks)} backups del perfil?\n\n"
                "Esta acción no se puede deshacer. El perfil actual NO se toca.",
                icon=messagebox.WARNING):
            return
        errores = []
        for bk in bks:
            try:
                bk.unlink()
            except OSError as exc:
                errores.append(f"{bk.name}: {exc}")
        self._perfil_backups_pintar()
        if errores:
            messagebox.showerror(
                "Eliminar backups",
                f"Se eliminaron {len(bks) - len(errores)} de {len(bks)}.\n\n"
                + "\n".join(errores[:8]))
        else:
            self._perfil_estado.set(f"Eliminados {len(bks)} backups.")

    # ------------------------------------------------------------ corrida
    def _pestana_corrida(self):
        tab = ttk.Frame(self.nb)

        barra = ttk.Frame(tab, padding=(10, 8))
        barra.pack(fill="x")
        self._btn_ats = ttk.Button(
            barra, text="Corrida ATS de empresa",
            command=lambda: self._corrida_arrancar(solo="ats"))
        self._btn_ats.pack(side="left")
        self._btn_boards = ttk.Button(
            barra, text="Corrida job boards + estatales",
            command=lambda: self._corrida_arrancar(solo="boards"))
        self._btn_boards.pack(side="left", padx=6)
        self._btn_todos = ttk.Button(barra, text="Corrida completa",
                                     command=self._corrida_arrancar)
        self._btn_todos.pack(side="left")
        ttk.Label(barra, style="Suave.TLabel",
                  text="(equivalen a: main.py --solo ats · --solo boards · main.py)"
                  ).pack(side="left", padx=8)

        barra2 = ttk.Frame(tab, padding=(10, 0))
        barra2.pack(fill="x")
        self._btn_preflight = ttk.Button(
            barra2, text="Preflight (sin red)",
            command=lambda: self._corrida_arrancar(preflight=True))
        self._btn_preflight.pack(side="left")
        self._btn_detener = ttk.Button(barra2, text="Detener",
                                       command=self._corrida_detener,
                                       style="Danger.TButton")
        self._btn_detener.pack(side="left", padx=6)
        ttk.Label(barra2, style="Suave.TLabel", wraplength=1000,
                  text="Cada botón abre la corrida en su propia ventana de "
                       "consola VERDE (título \"JobPulse CR - Scraping\"): "
                       "la única ventana de comandos visible mientras corre, "
                       "y se cierra sola al terminar. Si cierras la UI, la "
                       "corrida sigue; si cierras la ventana verde, se detiene."
                  ).pack(side="left", padx=8)

        barra_ia = ttk.Frame(tab, padding=(10, 0))
        barra_ia.pack(fill="x", pady=(6, 0))
        self._btn_ia = ttk.Button(
            barra_ia, text="Re-evaluar crudo con IA",
            command=lambda: self._corrida_arrancar(
                extra=("--ia " + self._ia_args()).strip()))
        self._btn_ia.pack(side="left")
        self._btn_ia_cv = ttk.Button(
            barra_ia, text="Evaluar CV con IA", command=self._ia_cv)
        self._btn_ia_cv.pack(side="left", padx=6)
        self._btn_ia_rep = ttk.Button(
            barra_ia, text="Calibrar IA local",
            command=lambda: self._corrida_arrancar(extra="--ia-reporte"))
        self._btn_ia_rep.pack(side="left")
        ttk.Label(barra_ia, style="Suave.TLabel", wraplength=1000,
                  text="IA externa (Gemini/NVIDIA, requiere IA_API_KEY en "
                       ".env): re-puntúa el crudo (no cambia qué entra a "
                       "jobs), evalúa/crea el perfil desde un CV, o mide el "
                       "acuerdo de la IA local. Consumen cuota gratuita."
                  ).pack(side="left", padx=8)

        barra_mod = ttk.Frame(tab, padding=(10, 4))
        barra_mod.pack(fill="x")
        from core.ia_api import PRESETS
        ttk.Label(barra_mod, text="API:").pack(side="left")
        self._ia_proveedor_var = tk.StringVar(
            value=self._ia_proveedor_guardado())
        self._ia_proveedor_cb = ttk.Combobox(
            barra_mod, textvariable=self._ia_proveedor_var, state="readonly",
            width=14, values=list(PRESETS.keys()))
        self._ia_proveedor_cb.pack(side="left", padx=(6, 12))
        self._ia_proveedor_cb.bind("<<ComboboxSelected>>",
                                   lambda _e: self._ia_proveedor_cambio())
        ttk.Label(barra_mod, text="Modelo IA:").pack(side="left")
        self._ia_modelo_var = tk.StringVar(
            value=self._ia_modelo_guardado(self._ia_proveedor_var.get()))
        self._ia_modelo_cb = ttk.Combobox(
            barra_mod, textvariable=self._ia_modelo_var, state="readonly",
            width=46)
        self._ia_modelo_cb.pack(side="left", padx=(6, 6))
        self._ia_modelo_cb.bind("<<ComboboxSelected>>",
                                lambda _e: self._ia_modelo_cambio())
        ttk.Button(barra_mod, text="Actualizar lista",
                   command=self._ia_cargar_modelos).pack(side="left")
        self._ia_modelos_estado = ttk.Label(barra_mod, text="",
                                            style="Suave.TLabel")
        self._ia_modelos_estado.pack(side="left", padx=8)
        ttk.Label(barra_mod, style="Suave.TLabel",
                  text="(cada API tiene su propia lista; cambia con el tiempo)"
                  ).pack(side="left", padx=4)
        self._ia_modelos_cola = queue.Queue()
        self._ia_cargando = False
        self._ia_carga_id = 0
        self._ia_cargar_modelos()

        panel = ttk.LabelFrame(tab, text="Estado de la corrida", padding=10)
        panel.pack(fill="x", padx=10, pady=8)
        negr = ("Segoe UI", 9, "bold")
        ttk.Label(panel, text="Estado:", font=negr).grid(row=0, column=0, sticky="w")
        self._cor_estado = ttk.Label(panel, text="…")
        self._cor_estado.grid(row=0, column=1, sticky="w", padx=(4, 20))
        ttk.Label(panel, text="Inicio:", font=negr).grid(row=0, column=2, sticky="w")
        self._cor_inicio = ttk.Label(panel, text="—")
        self._cor_inicio.grid(row=0, column=3, sticky="w", padx=(4, 20))
        ttk.Label(panel, text="Estatales (crudas):", font=negr).grid(
            row=0, column=4, sticky="w")
        self._cor_est = ttk.Label(panel, text="0")
        self._cor_est.grid(row=0, column=5, sticky="w", padx=(4, 20))
        ttk.Label(panel, text="Resto (crudas):", font=negr).grid(
            row=0, column=6, sticky="w")
        self._cor_resto = ttk.Label(panel, text="0")
        self._cor_resto.grid(row=0, column=7, sticky="w", padx=4)

        self._cor_paso = ttk.Label(panel, text="—")
        self._cor_paso.grid(row=1, column=0, sticky="w", pady=(8, 0))
        self._cor_bar = ttk.Progressbar(panel, length=380, maximum=1)
        self._cor_bar.grid(row=1, column=1, columnspan=7, sticky="we", pady=(8, 0))
        panel.columnconfigure(1, weight=1)

        self._cor_fase = ttk.Label(panel, text="Sin corridas todavía",
                                   font=("Segoe UI", 10, "bold"))
        self._cor_fase.grid(row=2, column=0, columnspan=8, sticky="w",
                            pady=(8, 2))
        self._cor_msg = ttk.Label(panel, text="", wraplength=900, style="Suave.TLabel")
        self._cor_msg.grid(row=3, column=0, columnspan=8, sticky="w")

        logm = ttk.LabelFrame(tab, text="Log de la corrida (últimas líneas)",
                              padding=6)
        logm.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self._cor_log = tk.Text(logm, height=10, wrap="none",
                                font=("Consolas", 9), state="disabled")
        self._texts_tema.append(self._cor_log)   # repintar al cambiar tema
        sb = ttk.Scrollbar(logm, orient="vertical", command=self._cor_log.yview)
        self._cor_log.configure(yscrollcommand=sb.set)
        self._cor_log.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        self.nb.add(tab, text="  Corrida  ")

    # ------------------------------------------------- selector de API/modelo IA
    @staticmethod
    def _leer_json(ruta: Path) -> dict:
        try:
            datos = json.loads(ruta.read_text(encoding="utf-8"))
            return datos if isinstance(datos, dict) else {}
        except (OSError, ValueError):
            return {}

    def _ia_proveedor_entorno(self) -> str:
        """API definida en .env (o inferida por la clave); ignora la UI."""
        from core.ia_api import IAClient, PRESETS
        prov = (os.getenv("IA_PROVEEDOR") or "").strip().lower()
        if prov in PRESETS:
            return prov
        try:
            prov = IAClient().proveedor
            return prov if prov in PRESETS else ""
        except Exception:
            return ""

    def _ia_proveedor_guardado(self) -> str:
        """API elegida en la UI; si no hay, IA_PROVEEDOR del .env o la inferida."""
        from core.ia_api import IAClient, PRESETS
        try:
            prov = IA_PROVEEDOR_PATH.read_text(encoding="utf-8").strip().lower()
            if prov in PRESETS:
                return prov
        except OSError:
            pass
        prov = (os.getenv("IA_PROVEEDOR") or "").strip().lower()
        if prov in PRESETS:
            return prov
        try:
            return IAClient().proveedor
        except Exception:
            return "google"

    def _ia_proveedor_guardar(self, proveedor: str):
        prov = (proveedor or "").strip().lower()
        if not prov:
            return
        try:
            IA_PROVEEDOR_PATH.parent.mkdir(parents=True, exist_ok=True)
            IA_PROVEEDOR_PATH.write_text(prov, encoding="utf-8")
        except OSError:
            pass

    def _ia_modelo_guardado(self, proveedor: str = "") -> str:
        """Modelo guardado para ESA api; si no, el default de su preset."""
        from core.ia_api import PRESETS
        var = getattr(self, "_ia_proveedor_var", None)
        prov = (proveedor or (var.get() if var else "")
                or self._ia_proveedor_guardado()).strip().lower()
        guardados = self._leer_json(IA_MODELOS_PATH)
        if prov and guardados.get(prov):
            return str(guardados[prov])
        # Compatibilidad con data/ia_modelo.txt (cuando habia una sola API).
        try:
            legacy = (BASE / "data" / "ia_modelo.txt").read_text(
                encoding="utf-8").strip()
            if legacy and prov == self._ia_proveedor_entorno():
                return legacy
        except OSError:
            pass
        env_model = (os.getenv("IA_MODEL") or "").strip()
        env_prov = (os.getenv("IA_PROVEEDOR") or "").strip().lower()
        if env_model and prov == env_prov:
            return env_model
        return (PRESETS.get(prov) or {}).get("modelo", "")

    def _ia_modelo_guardar(self, proveedor: str, modelo: str):
        prov = (proveedor or "").strip().lower()
        mod = (modelo or "").strip().replace('"', "")
        if not prov or not mod:
            return
        datos = self._leer_json(IA_MODELOS_PATH)
        datos[prov] = mod
        try:
            IA_MODELOS_PATH.parent.mkdir(parents=True, exist_ok=True)
            IA_MODELOS_PATH.write_text(
                json.dumps(datos, ensure_ascii=False, indent=2),
                encoding="utf-8")
            (BASE / "data" / "ia_modelo.txt").write_text(
                mod, encoding="utf-8")
        except OSError:
            pass

    def _ia_proveedor_cambio(self):
        prov = (self._ia_proveedor_var.get() or "").strip().lower()
        self._ia_proveedor_guardar(prov)
        self._ia_modelo_var.set(self._ia_modelo_guardado(prov))
        self._ia_modelos_estado.configure(text="", style="Suave.TLabel")
        self._ia_cargar_modelos()

    def _ia_modelo_cambio(self):
        self._ia_modelo_guardar(self._ia_proveedor_var.get(),
                                self._ia_modelo_var.get())

    def _ia_args(self) -> str:
        """Flag --ia-modelo y memoria de API/modelo para la proxima apertura."""
        pvar = getattr(self, "_ia_proveedor_var", None)
        prov = ((pvar.get() if pvar else "")
                or self._ia_proveedor_guardado()).strip().lower()
        var = getattr(self, "_ia_modelo_var", None)
        modelo = ((var.get() if var else "") or "").strip().replace('"', "")
        if prov:
            self._ia_proveedor_guardar(prov)
        if modelo:
            self._ia_modelo_guardar(prov, modelo)
        return f'--ia-modelo "{modelo}"' if modelo else ""

    def _ia_cargar_modelos(self):
        """Pide la lista VIVA de ESA api en segundo plano (no bloquea la UI)."""
        pvar = getattr(self, "_ia_proveedor_var", None)
        prov = ((pvar.get() if pvar else "")
                or self._ia_proveedor_guardado()).strip().lower()
        self._ia_carga_id = getattr(self, "_ia_carga_id", 0) + 1
        carga_id = self._ia_carga_id
        self._ia_cargando = True
        self._ia_modelos_estado.configure(
            text=f"consultando modelos de {prov}…", style="Suave.TLabel")

        def trabajo():
            try:
                from core.ia_api import IAClient
                modelos = IAClient({"proveedor": prov}).listar_modelos()
                self._ia_modelos_cola.put((carga_id, modelos, ""))
            except Exception as exc:
                self._ia_modelos_cola.put((carga_id, [], str(exc)))

        threading.Thread(target=trabajo, daemon=True).start()
        self.after(300, self._ia_modelos_poll)

    def _ia_modelos_poll(self):
        try:
            carga_id, modelos, error = self._ia_modelos_cola.get_nowait()
        except queue.Empty:
            if getattr(self, "_ia_cargando", False):
                self.after(300, self._ia_modelos_poll)
            return
        if carga_id != getattr(self, "_ia_carga_id", 0):
            self.after(50, self._ia_modelos_poll)   # respuesta vieja: descartar
            return
        self._ia_cargando = False
        self._ia_modelos_listos(modelos, error)

    def _ia_modelos_listos(self, modelos, error):
        prov = (self._ia_proveedor_var.get() or "").strip().lower()
        actual = (self._ia_modelo_var.get() or "").strip()
        if not modelos:
            self._ia_modelos_estado.configure(
                text=f"No se pudo cargar la lista de {prov}: "
                     f"{(error or 'sin modelos')[:140]}",
                style="SemErr.TLabel")
            return
        valores = list(modelos)
        no_listado = bool(actual) and actual not in valores
        if no_listado:
            valores.insert(0, actual)     # que siga visible aunque haya cambiado
        self._ia_modelo_cb.configure(values=valores)
        if not actual:
            self._ia_modelo_var.set(valores[0])
            self._ia_modelo_guardar(prov, valores[0])
            self._ia_modelos_estado.configure(
                text=f"{len(modelos)} modelos en {prov}",
                style="Suave.TLabel")
            return
        if no_listado:
            self._ia_modelos_estado.configure(
                text=f"{len(modelos)} modelos en {prov} · el actual no "
                     "aparece (¿retirado?); elegí otro",
                style="SemAviso.TLabel")
        else:
            self._ia_modelos_estado.configure(
                text=f"{len(modelos)} modelos en {prov}", style="Suave.TLabel")

    def _corrida_arrancar(self, preflight: bool = False, solo: str = "todos",
                          extra: str = ""):
        """Lanza `python main.py` en su PROPIA ventana de consola VERDE.

        El scraping SOLO empieza con estos botones: esta UI nunca scrapea
        sola. La corrida corre en una ventana aparte ("JobPulse CR -
        Scraping", texto verde via `color 0A`) para distinguirla de
        cualquier otro proceso, y se cierra sola al terminar (/c). La UI
        sigue observando data/corrida_estado.json; cerrar la ventana
        verde detiene la corrida (queda "interrumpida").

        `solo` separa el alcance (ats | boards | todos). El candado de
        iniciar_corrida() es exclusivo: si hay una viva, main.py se niega
        solo; aqui tambien se re-chequea antes de gastar proceso.
        `extra` agrega flags tal cual (--ia, --ia-reporte, --ia-cv ...).
        """
        if corriendo():
            messagebox.showinfo("Corrida en curso",
                                "Ya hay una corrida viva: espera a que termine "
                                "o presiona Detener.")
            return
        args = ""
        if preflight:
            args += " --preflight"
        if solo and solo != "todos":
            args += f" --solo {solo}"
        if extra:
            args += f" {extra}"
        # La UI corre bajo pythonw (sin consola): la corrida necesita
        # python.exe para tener donde escribir su salida.
        py = Path(sys.executable)
        if py.name.lower() == "pythonw.exe":
            candidato = py.with_name("python.exe")
            py = candidato if candidato.exists() else py
        # La API elegida en la UI viaja como variable de entorno para que
        # main.py use ESA clave/modelo (pisa IA_PROVEEDOR del .env).
        pvar = getattr(self, "_ia_proveedor_var", None)
        prov = (pvar.get() if pvar else "").strip().lower()
        env_prov = f"set IA_PROVEEDOR={prov} && " if prov else ""
        # `start` desde un cmd OCULTO (CREATE_NO_WINDOW) abre la ventana
        # verde como proceso INDEPENDIENTE: cerrar la UI no la mata, y no
        # queda ninguna otra consola visible.
        verde = (f'chcp 65001 >nul && color 0A && title {CORRIDA_TITULO}'
                 f' && {env_prov}"{py}" -u -X utf8 main.py{args}')
        subprocess.Popen(
            f'cmd /d /c start "{CORRIDA_TITULO}" cmd /d /s /c "{verde}"',
            cwd=str(BASE), stdin=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW)

    def _ia_cv(self):
        """Elige un CV y lanza --ia-cv; pregunta si aplicar el perfil."""
        ruta = filedialog.askopenfilename(
            title="Elegí tu CV (PDF, DOCX, TXT o MD)",
            filetypes=[("CV", "*.pdf *.docx *.txt *.md"),
                       ("Todos los archivos", "*.*")])
        if not ruta:
            return
        guardar = messagebox.askyesno(
            "Perfil desde el CV",
            "La IA solo propone. Lo recomendado es elegir No y revisar el "
            "borrador en Perfil > Mi CV > 'Cargar propuesta IA' antes de "
            "guardar.\n\n¿Querés que además actualice "
            "data/usuario/profile.json directamente? (Se guarda un backup "
            "del actual.)",
            default=messagebox.NO)
        extra = f'--ia-cv "{ruta}"'
        if guardar:
            extra += " --ia-guardar"
        args_modelo = self._ia_args()
        if args_modelo:
            extra += f" {args_modelo}"
        self._corrida_arrancar(extra=extra)

    def _corrida_detener(self):
        if not detener():
            messagebox.showinfo("Detener",
                                "No se pudo detener (quizás ya terminó).")

    def _corrida_refrescar(self):
        """Drena la cola del lector; pinta solo lo mas reciente."""
        dato = None
        try:
            while True:
                dato = self._cola_corrida.get_nowait()
        except queue.Empty:
            pass
        if dato is not None:
            self._corrida_pintar(*dato)
        self._after_corrida_id = self.after(2000, self._corrida_refrescar)

    def _corrida_pintar(self, est, log):
        estado = est.get("estado", "nunca")
        vivo = estado == "corriendo"

        self._cor_estado.configure(
            text=ESTADO_TXT.get(estado, estado),
            style=ESTADO_ESTILO.get(estado, "TLabel"))
        ts = est.get("inicio_ts")
        self._cor_inicio.configure(text=ts[11:19] if ts else "—")
        enc = est.get("encontradas") or {}
        self._cor_est.configure(text=str(enc.get("estatales", 0)))
        self._cor_resto.configure(
            text=str(enc.get("boards", 0) + enc.get("directos", 0)))

        total = int(est.get("total_fases") or 0)
        paso = int(est.get("paso") or 0)
        self._cor_bar.configure(maximum=total or 1, value=min(paso, total))
        self._cor_paso.configure(
            text=f"Paso {min(paso, total)}/{total}" if total else "—")

        # Alcance de la corrida (--solo ats|boards): del resumen final o del
        # titulo que publica main.py al arrancar (mismo criterio que app.py).
        r = est.get("resumen") or {}
        alcance = r.get("alcance") or ""
        if not alcance:
            t = est.get("titulo") or ""
            alcance = t.split(" · ", 1)[1] if " · " in t else ""
        achip = f"  ·  {alcance}" if alcance else ""

        if estado == "corriendo":
            self._cor_fase.configure(text=f"▶ {est.get('fase_actual', '')}"
                                          + achip)
            self._cor_msg.configure(text=est.get("detalle") or "")
        elif estado == "ok":
            self._cor_fase.configure(text=f"Completada{achip}")
            self._cor_msg.configure(
                text=f"{(est.get('fin_ts') or '')[11:19]} — "
                     f"{r.get('crudas_estatales', 0)} en portales estatales, "
                     f"{r.get('crudas_boards', 0) + r.get('crudas_directos', 0)} "
                     f"en el resto (agregadores + ATS), "
                     f"{r.get('nuevas', 0)} vacantes nuevas tras el gate.")
        elif estado == "error":
            self._cor_fase.configure(text="Error")
            self._cor_msg.configure(
                text=f"La corrida terminó con error: "
                     f"{r.get('motivo', '') or 'ver log'}")
        elif estado == "interrumpida":
            self._cor_fase.configure(text="Interrumpida")
            self._cor_msg.configure(
                text="El proceso ya no existe (se cerró la ventana verde o "
                     "murió). Puedes volver a lanzarla.")
        elif estado == "detenida":
            self._cor_fase.configure(text="Detenida")
            self._cor_msg.configure(text="Corrida detenida a mano.")
        elif estado == "preflight":
            self._cor_fase.configure(text=f"Preflight OK{achip}")
            self._cor_msg.configure(
                text="Plan de corrida y credenciales reportados sin tocar la "
                     "red. Revisa el log.")
        else:
            self._cor_fase.configure(text="Sin corridas todavía")
            self._cor_msg.configure(
                text="Nada scrapea solo: lanza la corrida con los botones de "
                     "arriba y verás el progreso aquí (y en su ventana verde) "
                     "en segundos.")

        for b in (self._btn_ats, self._btn_boards, self._btn_todos,
                  self._btn_preflight):
            b.configure(state="disabled" if vivo else "normal")
        self._btn_detener.configure(state="normal" if vivo else "disabled")

        cuerpo = "".join(log or [])
        if cuerpo != self._cor_log_prev:
            self._cor_log_prev = cuerpo
            t = self._cor_log
            t.configure(state="normal")
            t.delete("1.0", "end")
            if cuerpo:
                t.insert("1.0", cuerpo)
                t.see("end")
            t.configure(state="disabled")

        # Transicion corriendo -> terminada: la corrida trajo datos nuevos,
        # la pestaña Ranking se actualiza sola.
        prev = self._cor_estado_prev
        self._cor_estado_prev = estado
        if prev == "corriendo" and estado in ("ok", "error", "detenida") \
                and self.todas:
            self._recargar()

    def _al_cerrar(self):
        """Cerrar la ventana mata SOLO la UI: una corrida viva es un proceso
        aparte y sigue andando; al reabrir, esta pestaña la vuelve a mostrar."""
        self._corrida_stop.set()
        self.destroy()

    # -------------------------------------------------------- credenciales
    def _pestana_credenciales(self):
        tab = ttk.Frame(self.nb)

        ttk.Label(
            tab, padding=(10, 8), style="Suave.TLabel", wraplength=1000,
            justify="left",
            text=("La lista de sitios que exigen password/sesión: seeds fijos + "
                  "lo que los providers detectan. Las credenciales se guardan SOLO "
                  "en esta máquina, en data/empresas/credenciales.json (.gitignore): "
                  "nunca viajan a git. Si la cuenta no tiene clave propia (Indeed se "
                  "entra con tu Gmail), marcá el acceso vía Google/SSO y guardá solo "
                  "el correo. Equivalente por terminal: python credenciales.py "
                  "--set \"Empresa\".")
        ).pack(fill="x")

        met = ttk.Frame(tab, padding=(10, 0))
        met.pack(fill="x")
        negr = ("Segoe UI", 9, "bold")
        ttk.Label(met, text="Sitios en la lista:", font=negr).pack(side="left")
        self._cred_m_total = ttk.Label(met, text="0")
        self._cred_m_total.pack(side="left", padx=(4, 18))
        ttk.Label(met, text="Obligatorios:", font=negr).pack(side="left")
        self._cred_m_obl = ttk.Label(met, text="0")
        self._cred_m_obl.pack(side="left", padx=(4, 18))
        ttk.Label(met, text="Con credencial:", font=negr).pack(side="left")
        self._cred_m_con = ttk.Label(met, text="0")
        self._cred_m_con.pack(side="left", padx=(4, 18))
        ttk.Label(met, text="Pendientes:", font=negr).pack(side="left")
        self._cred_m_pend = ttk.Label(met, text="0")
        self._cred_m_pend.pack(side="left", padx=4)

        marco = ttk.LabelFrame(
            tab, text="LA LISTA — sitios que piden password / sesión", padding=6)
        marco.pack(fill="both", expand=True, padx=10, pady=(8, 0))
        self._tabla_cred = Tabla(
            marco,
            (("empresa", "Empresa", 140, "w"),
             ("grupo_label", "Grupo", 165, "w"),
             ("obligatorio_txt", "Obligatorio", 80, "center"),
             ("credencial_txt", "Credencial", 210, "w"),
             ("auto_txt", "Auto-login", 86, "center"),
             ("pendiente", "Pendiente", 130, "w"),
             ("motivo", "Motivo", 230, "w")),
            al_ordenar=self._cred_pintar,
            al_seleccionar=self._cred_seleccion,
        )
        self._tabla_cred.pack(fill="both", expand=True)

        self._cred_aviso = ttk.Label(tab, style="SemAviso.TLabel", padding=(10, 2))
        self._cred_aviso.pack(fill="x")

        acc = ttk.LabelFrame(tab, text="Acciones sobre el sitio seleccionado",
                             padding=8)
        acc.pack(fill="x", padx=10, pady=(4, 0))
        self._btn_cred_auto = ttk.Button(acc, text="Auto-login: —",
                                         command=self._cred_toggle_auto)
        self._btn_cred_auto.pack(side="left")
        self._btn_cred_borrar = ttk.Button(acc, text="Borrar credencial",
                                           command=self._cred_borrar)
        self._btn_cred_borrar.pack(side="left", padx=6)
        self._btn_cred_quitar = ttk.Button(acc, text="Quitar del flujo",
                                           command=self._cred_quitar)
        self._btn_cred_quitar.pack(side="left")
        self._btn_cred_abrir = ttk.Button(acc, text="Abrir sitio",
                                          command=self._cred_abrir)
        self._btn_cred_abrir.pack(side="left", padx=6)
        ttk.Label(acc, style="Suave.TLabel",
                  text="Quitar del flujo solo aplica a sitios que agregaste a mano."
                  ).pack(side="left", padx=8)

        # ---- formulario de alta / actualizacion (como el form de app.py)
        form = ttk.LabelFrame(tab, text="Agregar / actualizar una credencial",
                              padding=10)
        form.pack(fill="x", padx=10, pady=(8, 10))
        self._cred_sel = tk.StringVar()
        self._cred_otra = tk.StringVar()
        self._cred_usuario = tk.StringVar()
        self._cred_password = tk.StringVar()
        self._cred_sso = tk.BooleanVar(value=False)
        self._cred_url = tk.StringVar()
        self._cred_auto = tk.BooleanVar(value=True)
        self._cred_card = tk.StringVar()
        self._cred_loc = tk.StringVar()

        ttk.Label(form, text="Sitio:").grid(row=0, column=0, sticky="w", pady=2)
        self._cb_cred = ttk.Combobox(form, textvariable=self._cred_sel,
                                     state="readonly", width=30)
        self._cb_cred.grid(row=0, column=1, sticky="w", pady=2)
        ttk.Label(form, text="Nombre del otro sitio:").grid(
            row=0, column=2, sticky="w", pady=2, padx=(16, 0))
        ttk.Entry(form, textvariable=self._cred_otra, width=24).grid(
            row=0, column=3, sticky="w", pady=2)

        ttk.Label(form, text="Usuario:").grid(row=1, column=0, sticky="w", pady=2)
        ttk.Entry(form, textvariable=self._cred_usuario, width=32).grid(
            row=1, column=1, sticky="w", pady=2)
        ttk.Label(form, text="Password (opcional si usás SSO):").grid(
            row=1, column=2, sticky="w", pady=2, padx=(16, 0))
        ttk.Entry(form, textvariable=self._cred_password, width=24,
                  show="•").grid(row=1, column=3, sticky="w", pady=2)

        ttk.Checkbutton(
            form, variable=self._cred_sso,
            text="Sin clave: entrar vía Google/SSO (Indeed se entra con la cuenta "
                 "de Gmail; se reutiliza tu sesión en Brave)"
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=2)
        ttk.Checkbutton(
            form, variable=self._cred_auto,
            text="Login automático en la corrida (si falla, fallback manual)"
        ).grid(row=2, column=2, columnspan=2, sticky="w", pady=2)

        ttk.Label(form, text="URL de login:").grid(row=3, column=0,
                                                   sticky="w", pady=2)
        ttk.Entry(form, textvariable=self._cred_url, width=56).grid(
            row=3, column=1, columnspan=3, sticky="w", pady=2)
        ttk.Label(form, text="Selectores (sitio nuevo): card_sel").grid(
            row=4, column=0, sticky="w", pady=2)
        ttk.Entry(form, textvariable=self._cred_card, width=24).grid(
            row=4, column=1, sticky="w", pady=2)
        ttk.Label(form, text="loc_sel").grid(row=4, column=2, sticky="w",
                                             pady=2, padx=(16, 0))
        ttk.Entry(form, textvariable=self._cred_loc, width=20).grid(
            row=4, column=3, sticky="w", pady=2)

        pies = ttk.Frame(form)
        pies.grid(row=5, column=0, columnspan=4, sticky="w", pady=(6, 0))
        ttk.Button(pies, text="Guardar credencial",
                   command=self._cred_guardar).pack(side="left")
        self._cred_estado = ttk.Label(pies, text="", style="SemOK.TLabel")
        self._cred_estado.pack(side="left", padx=10)

        self.nb.add(tab, text="  Credenciales  ")
        self._cred_pintar()

    @staticmethod
    def _cred_valores(f):
        return (f["empresa"], f["grupo_label"], f["obligatorio_txt"],
                f["credencial_txt"], f["auto_txt"], f["pendiente"], f["motivo"])

    def _cred_pintar(self):
        sitios = creds_store.lista_sitios_login()
        r = creds_store.resumen()
        self._cred_m_total.configure(text=str(r["total"]))
        self._cred_m_obl.configure(text=str(r["obligatorios"]))
        self._cred_m_con.configure(text=str(r["con_credencial"]))
        self._cred_m_pend.configure(text=str(r["pendientes"]))

        filas = []
        for s in sitios:
            cred = ((s["usuario"]
                     + ("" if not s.get("sso") else " · " + s["acceso"])
                     + (" (.env)" if s.get("password_fuente") == ".env" else ""))
                    if s["tiene_credencial"] else "— falta —")
            filas.append({
                "empresa": s["empresa"],
                "grupo_label": s.get("grupo_label") or "",
                "obligatorio_txt": "Sí" if s["obligatorio"] else "No",
                "credencial_txt": cred,
                "auto_txt": "ON" if s.get("auto_login") else "off",
                "pendiente": s.get("pendiente") or "",
                "motivo": (s.get("motivo") or "")[:120],
                "_s": s,          # el sitio original, para las acciones
            })
        col, asc = self._tabla_cred.orden
        filas = ordenar(filas, col, asc)
        self._tabla_cred.llenar(filas, self._cred_valores)

        faltan = [s["empresa"] for s in sitios
                  if s["obligatorio"] and not s["tiene_credencial"]]
        self._cred_aviso.configure(
            text=("⚠ Falta credencial obligatoria en: " + ", ".join(faltan)
                  + " — sin ella ese proveedor se omite con un aviso."
                  if faltan else ""))

        valores = [s["empresa"] for s in sitios] + ["Otro (escribir a mano)"]
        self._cb_cred["values"] = valores
        if self._cred_sel.get() not in valores:
            self._cred_sel.set(valores[0])
        self._cred_seleccion(None)

    def _cred_seleccion(self, fila):
        """Habilita las acciones segun el sitio seleccionado (mismas reglas
        de app.py: el toggle exige credencial, quitar exige alta manual)."""
        s = (fila or {}).get("_s")
        if not s:
            for b in (self._btn_cred_auto, self._btn_cred_borrar,
                      self._btn_cred_quitar, self._btn_cred_abrir):
                b.configure(state="disabled")
            return
        self._btn_cred_auto.configure(
            text=("Auto-login: ON" if s.get("auto_login") else "Auto-login: OFF"),
            state=("normal" if (s["tiene_credencial"] or s.get("auto_login"))
                   else "disabled"))
        self._btn_cred_borrar.configure(
            state="normal" if s["tiene_credencial"] else "disabled")
        self._btn_cred_quitar.configure(
            state="normal" if s.get("detectado") == "manual" else "disabled")
        self._btn_cred_abrir.configure(
            state="normal" if s.get("url") else "disabled")

    def _cred_toggle_auto(self):
        f = self._tabla_cred.seleccionada()
        if not f:
            return
        s = f["_s"]
        creds_store.marcar_auto_login(s["empresa"], not s.get("auto_login"))
        self._cred_pintar()

    def _cred_borrar(self):
        f = self._tabla_cred.seleccionada()
        if not f:
            return
        s = f["_s"]
        if not messagebox.askyesno("Borrar credencial",
                                   f"¿Borrar la credencial de {s['empresa']}?"):
            return
        creds_store.borrar_credencial(s["empresa"])
        self._cred_pintar()

    def _cred_quitar(self):
        f = self._tabla_cred.seleccionada()
        if not f:
            return
        s = f["_s"]
        if not messagebox.askyesno(
                "Quitar del flujo",
                f"¿Quitar {s['empresa']} de la lista y del barrido?\n"
                "La credencial se conserva. Los sitios fijos del sistema no se "
                "pueden quitar."):
            return
        creds_store.quitar_sitio_login(s["empresa"])
        self._cred_pintar()

    def _cred_abrir(self):
        f = self._tabla_cred.seleccionada()
        if f and f["_s"].get("url"):
            webbrowser.open(f["_s"]["url"])

    def _cred_guardar(self):
        nombre = (self._cred_otra.get() or "").strip() or self._cred_sel.get()
        usuario = self._cred_usuario.get().strip()
        password = self._cred_password.get()
        sso = "google" if self._cred_sso.get() else ""
        if not nombre:
            self._cred_estado.configure(
                text="Elegí un sitio de la lista o escribí el nombre del otro.",
                style="SemErr.TLabel")
            return
        if not usuario or (not password and not sso):
            self._cred_estado.configure(
                text="Falta el usuario, o bien la password o el acceso vía SSO "
                     "(marcá la casilla si la cuenta no tiene clave propia).",
                style="SemErr.TLabel")
            return
        url = self._cred_url.get().strip()
        nuevo = not creds_store.sitio_en_flujo(nombre)
        creds_store.upsert_credencial(
            nombre, usuario, password, url=url,
            auto_login=self._cred_auto.get(), sso=sso,
            card_sel=self._cred_card.get().strip(),
            loc_sel=self._cred_loc.get().strip())
        if nuevo and url:
            msg = (f"{nombre} dado de alta en el flujo: credencial guardada, "
                   "aparece en LA LISTA y entra al barrido de portales (cada "
                   "corrida lo visita en tu Brave).")
        elif nuevo:
            msg = (f"{nombre} en LA LISTA con su credencial. Sin URL no se barre "
                   "todavía: volvé a guardar con la URL del portal y el scraper "
                   "lo recorrerá cada corrida.")
        else:
            msg = (f"Credencial de {nombre} guardada localmente"
                   + (" (acceso vía Google, sin clave)" if sso else "") + ".")
        self._cred_estado.configure(text=msg, style="SemOK.TLabel")

        # El form de Streamlit se limpiaba tras guardar (clear_on_submit).
        for v in (self._cred_usuario, self._cred_password, self._cred_otra,
                  self._cred_url, self._cred_card, self._cred_loc):
            v.set("")
        self._cred_sso.set(False)
        self._cred_auto.set(True)
        self._cred_pintar()

    # ------------------------------------------------------------ sesiones
    def _pestana_sesiones(self):
        tab = ttk.Frame(self.nb)

        ttk.Label(
            tab, padding=(10, 8), style="Suave.TLabel", wraplength=1000,
            justify="left",
            text=("El navegador sigue siendo la bóveda preferida: abrís cada "
                  "sitio en tu Brave, te logueás una vez (con tu 2FA normal) y "
                  "la sesión queda en tu perfil — el mismo con el que corre el "
                  "scraper. Si un sitio exige password, la pestaña Credenciales "
                  "te deja ponerla localmente y activar el login automático; si "
                  "ese login falla por CAPTCHA/2FA, el scraper cae aquí al login "
                  "manual. Los sitios con pendiente saltan en la corrida sin "
                  "romper nada.")
        ).pack(fill="x")

        marco = ttk.LabelFrame(
            tab, text="Sitios que requieren login / sesión de navegador",
            padding=6)
        marco.pack(fill="both", expand=True, padx=10)
        self._tabla_ses = Tabla(
            marco,
            (("empresa", "Empresa", 140, "w"),
             ("grupo", "Grupo", 130, "w"),
             ("estado_txt", "Estado", 100, "center"),
             ("pendiente", "Pendiente", 170, "w"),
             ("porque", "Motivo", 250, "w"),
             ("url", "URL", 230, "w")),
            al_ordenar=self._ses_pintar,
            al_seleccionar=self._ses_seleccion,
        )
        self._tabla_ses.pack(fill="both", expand=True)

        acc = ttk.LabelFrame(tab, text="Acciones sobre el sitio seleccionado",
                             padding=8)
        acc.pack(fill="x", padx=10, pady=(8, 10))
        self._btn_ses_abrir = ttk.Button(acc, text="Abrir en Brave",
                                         command=self._ses_abrir)
        self._btn_ses_abrir.pack(side="left")
        self._btn_ses_verificar = ttk.Button(acc, text="Verificar sesión",
                                             command=self._ses_verificar)
        self._btn_ses_verificar.pack(side="left", padx=6)
        self._btn_ses_resuelto = ttk.Button(acc, text="Marcar resuelto",
                                            command=self._ses_resuelto)
        self._btn_ses_resuelto.pack(side="left")
        self._ses_estado = ttk.Label(acc, text="", style="Suave.TLabel")
        self._ses_estado.pack(side="left", padx=12)

        self.nb.add(tab, text="  Sesiones  ")
        self._ses_pintar()

    @staticmethod
    def _ses_valores(f):
        return (f["empresa"], f["grupo"], f["estado_txt"],
                f["pendiente"], f["porque"], f["url"])

    def _ses_pintar(self):
        filas = []
        for s in sitios_con_sesion():
            estado = ("Pendiente" if s.get("pendiente")
                      else ("Recomendado" if s.get("recomendado")
                            else "Configurado"))
            filas.append({
                "empresa": s.get("empresa") or "?",
                "grupo": s.get("grupo") or "",
                "estado_txt": estado,
                "pendiente": s.get("pendiente") or "",
                "porque": (s.get("porque") or "")[:120],
                "url": s.get("url") or "",
                "_s": s,
            })
        col, asc = self._tabla_ses.orden
        self._tabla_ses.llenar(ordenar(filas, col, asc), self._ses_valores)
        self._ses_seleccion(None)

    def _ses_seleccion(self, fila):
        s = (fila or {}).get("_s")
        if not s:
            for b in (self._btn_ses_abrir, self._btn_ses_verificar,
                      self._btn_ses_resuelto):
                b.configure(state="disabled")
            return
        tiene_url = bool(s.get("url"))
        self._btn_ses_abrir.configure(state="normal" if tiene_url else "disabled")
        self._btn_ses_verificar.configure(
            state=("normal" if tiene_url and not self._ses_verificando
                   else "disabled"))
        self._btn_ses_resuelto.configure(
            state="normal" if s.get("pendiente") else "disabled")

    def _ses_abrir(self):
        f = self._tabla_ses.seleccionada()
        if not f or not f["_s"].get("url"):
            return
        if abrir_en_brave(f["_s"]["url"]):
            self._ses_estado.configure(
                text="Brave abierto. Logueate, cerrás la pestaña y volvés "
                     "aquí para verificar.", style="Suave.TLabel")
        else:
            self._ses_estado.configure(
                text="No encontré brave.exe — configura BRAVE_BIN en .env",
                style="SemErr.TLabel")

    def _ses_verificar(self):
        if self._ses_verificando:
            return
        f = self._tabla_ses.seleccionada()
        if not f:
            return
        s = f["_s"]
        url = s.get("url_check") or s.get("url")
        if not url:
            return
        self._ses_verificando = True
        self._btn_ses_verificar.configure(state="disabled")
        self._ses_estado.configure(
            text="Verificando (Brave se reinicia unos segundos)...",
            style="Suave.TLabel")
        threading.Thread(target=_hilo_verificar,
                         args=(s["empresa"], url, self._cola),
                         daemon=True).start()

    def _sesion_verificada(self, empresa, res):
        self._ses_verificando = False
        f = self._tabla_ses.seleccionada()
        self._ses_seleccion(f)          # reevalua el estado del boton
        if res.get("ok"):
            self._ses_estado.configure(
                text=f"Sesión OK — título: {res.get('titulo', '')}",
                style="SemOK.TLabel")
            if f and f["_s"].get("pendiente"):
                quitar_pendiente(empresa)   # resuelto: como hacia app.py
                self._ses_pintar()
        else:
            self._ses_estado.configure(
                text=(f"Sin sesión aún — {res.get('motivo', '')} "
                      f"(título: {res.get('titulo') or '?'})"),
                style="SemErr.TLabel")

    def _ses_resuelto(self):
        f = self._tabla_ses.seleccionada()
        if not f:
            return
        if quitar_pendiente(f["_s"]["empresa"]):
            self._ses_pintar()


if __name__ == "__main__":
    ensure_workspace()
    App().mainloop()
