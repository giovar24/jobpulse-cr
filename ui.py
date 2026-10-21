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
import json
import logging
import os
import queue
import sqlite3
import subprocess
import sys
import threading
import webbrowser
from datetime import date, datetime, timedelta
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from core.ats import rank_with_ats
from core import credenciales as creds_store
from core.profile import (CandidateProfile, DEFAULT_PROFILE_PATH,
                          backups_perfil, guardar_perfil)
from core.progreso import (corriendo, detener, leer_estado,
                           tail as log_tail)
from core.search_engine import SemanticSearchEngine
from core.sesiones import (abrir_en_brave, quitar_pendiente,
                           sitios_con_sesion, verificar_sesion)
from providers.source_groups import grupo_publico, GRUPOS_PUBLICOS

BASE = Path(__file__).resolve().parent
DB = BASE / "data" / "jobs.db"

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
ESTADO_COLOR = {
    "corriendo": "#0a7f24",
    "ok": "#0a7f24",
    "preflight": "#555555",
    "error": "#b01818",
    "detenida": "#a05a00",
    "interrumpida": "#a05a00",
    "nunca": "#555555",
}

# Titulo de la ventana de consola VERDE donde corre el scraping (la unica
# ventana de comandos visible cuando hay una corrida viva).
CORRIDA_TITULO = "JobPulse CR - Scraping"

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
    """Sin titulos/skills/temas no sirve ni para puntuar ni para buscar."""
    return not (datos.get("titulos") or datos.get("skills") or datos.get("temas"))


def _resumen_cambios(viejo: dict, nuevo: dict):
    """Diferencias legibles antes de guardar (el panel de Streamlit, en dialogo)."""
    def rep(i):
        return i.get("puesto", i.get("idioma", i)) if isinstance(i, dict) else i

    lineas = []
    for k, et in [("titulos", "Títulos"), ("skills", "Skills"), ("temas", "Temas"),
                  ("idiomas", "Idiomas"), ("certificaciones", "Certificaciones")]:
        v = [str(rep(x)).strip() for x in (viejo.get(k) or [])]
        n = [str(rep(x)).strip() for x in (nuevo.get(k) or [])]
        for xs, verbo in (([x for x in v if x not in n], "se quitan"),
                          ([x for x in n if x not in v], "se agregan")):
            if xs:
                lista = ", ".join(xs[:8]) + ("..." if len(xs) > 8 else "")
                lineas.append(f"- {et}: {verbo} {len(xs)} — {lista}")
    for k, et in [("nombre", "Nombre"), ("experiencia_anios", "Experiencia"),
                  ("educacion_nivel", "Educación"), ("seniority", "Seniority"),
                  ("pesos", "Pesos")]:
        if _escalar(viejo.get(k)) != _escalar(nuevo.get(k)):
            lineas.append(f"- {et}: {viejo.get(k)!r} → {nuevo.get(k)!r}")
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


# ------------------------------------------------------------------ app
class App(tk.Tk):
    def __init__(self):
        super().__init__()
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
        ttk.Label(pie, textvariable=self._frescura, foreground="#666",
                  anchor="e").pack(side="right")

        # El orden de pack fija el layout: pie y detalle abajo, notebook toma el resto.
        pie.pack(side="bottom", fill="x")
        self._det_marco.pack(side="bottom", fill="x", padx=10, pady=(8, 2))
        self.nb.pack(side="top", fill="both", expand=True, padx=6, pady=(6, 2))

        self._recargar()
        for var in (self._busca, self._grupo_et, self._dias):
            var.trace_add("write", lambda *_: self._pintar_rank())
        self.after(120, self._drenar)

        # Observador de corridas: el hilo lee estado+logs cada 2 s y el hilo
        # de Tkinter solo pinta lo que llega por la cola (leer_estado dispara
        # tasklist mientras hay corrida viva: no puede congelar la ventana).
        threading.Thread(target=_hilo_corrida,
                         args=(self._cola_corrida, self._corrida_stop),
                         daemon=True).start()
        self.after(2000, self._corrida_refrescar)
        self._corrida_pintar(leer_estado(), log_tail(120))
        self.protocol("WM_DELETE_WINDOW", self._al_cerrar)

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
        ttk.Label(barra, foreground="#666",
                  text="Busca sobre TODAS las ofertas cargadas (sin filtro de días)."
                  ).pack(side="left", padx=(14, 0))

        self._tabla_libre = Tabla(tab, COLS_LIBRE, al_ordenar=self._pintar_libre,
                                  al_seleccionar=self._ver_detalle,
                                  al_abrir=self._abrir_link)
        self._tabla_libre.pack(fill="both", expand=True, padx=10, pady=(0, 8))
        self.nb.add(tab, text="  Búsqueda libre  ")

    def _detalle_construir(self):
        """Un solo panel de detalle, compartido por todas las pestanas."""
        self._det_marco = ttk.LabelFrame(self, text="Detalle", padding=8)
        fondo = ttk.Style(self).lookup("Treeview", "background") or "#ffffff"
        self._detalle = tk.Text(self._det_marco, height=8, wrap="word",
                                state="disabled", relief="flat", background=fondo,
                                highlightthickness=0)
        self._detalle.pack(fill="both", expand=True)
        self._detalle.tag_configure("tit", font=("Segoe UI", 10, "bold"))
        self._detalle.tag_configure("rot", foreground="#666",
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
            self._conteo.set("No hay perfil válido (data/profile.json). "
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
                else:
                    self._conteo.set(f"Error: {dato}")
        except queue.Empty:
            pass
        self.after(120, self._drenar)

    def _ver_detalle(self, f):
        t = self._detalle
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
            t.insert("end", (f.get("link") or "") + "\n\n")
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

    # ------------------------------------------------------------- perfil
    def _pestana_perfil(self):
        tab = ttk.Frame(self.nb)

        barra = ttk.Frame(tab, padding=(10, 8))
        barra.pack(fill="x")
        ttk.Button(barra, text="Guardar perfil",
                   command=self._perfil_guardar).pack(side="left")
        ttk.Button(barra, text="Descartar cambios",
                   command=self._perfil_descartar).pack(side="left", padx=6)
        ttk.Label(barra, textvariable=self._perfil_estado,
                  foreground="#666").pack(side="left", padx=(12, 0))

        cuerpo = ttk.Frame(tab)
        cuerpo.pack(fill="both", expand=True, padx=10, pady=(0, 8))
        izq = ttk.Frame(cuerpo)
        izq.pack(side="left", fill="y", padx=(0, 12))
        der = ttk.Frame(cuerpo)
        der.pack(side="left", fill="both", expand=True)

        # --- escalares
        ats = ttk.LabelFrame(izq, text="Datos ATS", padding=10)
        ats.pack(fill="x")
        ttk.Label(ats, text="Nombre:").grid(row=0, column=0, sticky="w", pady=2)
        self._e_nombre = ttk.Entry(ats, width=32)
        self._e_nombre.grid(row=0, column=1, sticky="we", pady=2)
        ttk.Label(ats, text="Experiencia (años):").grid(row=1, column=0,
                                                        sticky="w", pady=2)
        self._v_exp = tk.StringVar(value="0")
        ttk.Spinbox(ats, from_=0.0, to=40.0, increment=0.5, width=8,
                    textvariable=self._v_exp).grid(row=1, column=1,
                                                  sticky="w", pady=2)
        ttk.Label(ats, text="Educación:").grid(row=2, column=0, sticky="w", pady=2)
        self._e_edu = ttk.Entry(ats, width=32)
        self._e_edu.grid(row=2, column=1, sticky="we", pady=2)
        ttk.Label(ats, text="Seniority:").grid(row=3, column=0, sticky="w", pady=2)
        self._e_sen = ttk.Entry(ats, width=32)
        self._e_sen.grid(row=3, column=1, sticky="we", pady=2)
        ats.columnconfigure(1, weight=1)

        pes = ttk.LabelFrame(izq, text="Pesos de cada sección", padding=10)
        pes.pack(fill="x", pady=(8, 0))
        self._v_pesos = {}
        for i, (et, clave, defecto) in enumerate((
                ("Título", "titulo", 1.0), ("Skills", "skills", 0.7),
                ("Tema", "tema", 0.5))):
            v = tk.StringVar(value=str(defecto))
            self._v_pesos[clave] = v
            ttk.Label(pes, text=et + ":").grid(row=i, column=0, sticky="w", pady=2)
            ttk.Spinbox(pes, from_=0.0, to=2.0, increment=0.05, width=6,
                        textvariable=v).grid(row=i, column=1, sticky="w", pady=2)
        self._l_pats = ttk.Label(pes, foreground="#666", wraplength=250,
                                 text="Score ATS: —")
        self._l_pats.grid(row=3, column=0, columnspan=2, sticky="w", pady=(6, 0))

        # --- listas editables (una por linea)
        self._t_titulos = self._caja_texto(
            der, "Títulos / palabras clave del puesto (uno por línea)", 5)
        self._t_skills = self._caja_texto(der, "Skills (uno por línea)", 7)
        self._t_temas = self._caja_texto(
            der, "Temas de búsqueda — puesto | puesto (EN) | ubicación", 4)
        self._t_idiomas = self._caja_texto(der, "Idiomas — idioma | nivel", 3)
        self._t_certs = self._caja_texto(der, "Certificaciones (una por línea)", 3)

        # --- backups
        bkm = ttk.LabelFrame(
            tab, text="Backups (se crean solos en cada guardado)", padding=8)
        bkm.pack(fill="x", padx=10, pady=(0, 10))
        self._tree_bk = ttk.Treeview(bkm, columns=("archivo", "fecha"),
                                     show="headings", height=4,
                                     selectmode="browse")
        self._tree_bk.heading("archivo", text="Archivo")
        self._tree_bk.heading("fecha", text="Fecha")
        self._tree_bk.column("archivo", width=340, anchor="w")
        self._tree_bk.column("fecha", width=150, anchor="center")
        self._tree_bk.pack(side="left", fill="both", expand=True)
        ttk.Button(bkm, text="Restaurar seleccionado",
                    command=self._perfil_restaurar).pack(side="left", padx=(10, 0),
                                                         anchor="n")

        self.nb.add(tab, text="  Perfil  ")
        self._perfil_cargar()

    def _caja_texto(self, parent, titulo, alto):
        """LabelFrame con un Text multilinea + scrollbar (lista editable)."""
        marco = ttk.LabelFrame(parent, text=titulo, padding=6)
        marco.pack(fill="x", pady=(0, 8))
        t = tk.Text(marco, height=alto, wrap="word", undo=True,
                    font=("Segoe UI", 10))
        sb = ttk.Scrollbar(marco, orient="vertical", command=t.yview)
        t.configure(yscrollcommand=sb.set)
        t.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        return t

    def _perfil_cargar(self, aviso: str = ""):
        """(Re)lee profile.json y llena los widgets con lo que hay en disco."""
        ruta = DEFAULT_PROFILE_PATH
        for t in (self._t_titulos, self._t_skills, self._t_temas,
                  self._t_idiomas, self._t_certs):
            t.delete("1.0", "end")
        self._e_nombre.delete(0, "end")
        self._e_edu.delete(0, "end")
        self._e_sen.delete(0, "end")

        if not ruta.exists():
            self._perfil_datos = {}
            self._perfil_estado.set(
                "No existe profile.json todavía: al Guardar se crea uno nuevo "
                "(o usá cv2profile.py para generarlo desde tu CV).")
            self._perfil_backups_pintar()
            return
        try:
            datos = json.loads(ruta.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            self._perfil_datos = {}
            self._perfil_estado.set(f"Perfil ilegible ({ruta.name}): {exc}")
            return
        self._perfil_datos = datos

        self._e_nombre.insert(0, str(datos.get("nombre") or ""))
        self._v_exp.set(str(float(datos.get("experiencia_anios") or 0)))
        self._e_edu.insert(0, str(datos.get("educacion_nivel") or ""))
        self._e_sen.insert(0, str(datos.get("seniority") or ""))
        pesos = datos.get("pesos") or {}
        for clave, defecto in (("titulo", 1.0), ("skills", 0.7), ("tema", 0.5)):
            self._v_pesos[clave].set(str(float(pesos.get(clave, defecto))))
        pa = datos.get("pesos_ats") or {}
        self._l_pats.configure(
            text="Score ATS: "
                 + (" · ".join(f"{k} {float(v):g}" for k, v in pa.items()) or "—"))

        self._llenar_texto(self._t_titulos,
                          [str(x) for x in (datos.get("titulos") or [])])
        self._llenar_texto(self._t_skills,
                           [str(x) for x in (datos.get("skills") or [])])
        self._llenar_texto(
            self._t_temas,
            [" | ".join(p for p in (t.get("puesto", ""), t.get("puesto_en", ""),
                                    t.get("ubicacion", "")) if p)
             for t in (datos.get("temas") or []) if isinstance(t, dict)])
        self._llenar_texto(
            self._t_idiomas,
            [" | ".join(p for p in (i.get("idioma", ""), i.get("nivel", "")) if p)
             for i in (datos.get("idiomas") or []) if isinstance(i, dict)])
        self._llenar_texto(self._t_certs,
                           [str(x) for x in (datos.get("certificaciones") or [])])

        self._perfil_estado.set(aviso or "Editá lo que quieras: al guardar se "
                                         "hace backup del archivo anterior.")
        self._perfil_backups_pintar()

    @staticmethod
    def _llenar_texto(t, items):
        if items:
            t.insert("1.0", "\n".join(items))

    def _perfil_recolectar(self):
        """El dict NUEVO: igual al de disco salvo las claves editadas aqui."""
        nuevo = dict(self._perfil_datos or {})
        nuevo["nombre"] = self._e_nombre.get().strip()
        try:
            nuevo["experiencia_anios"] = float(self._v_exp.get() or 0)
        except ValueError:
            nuevo["experiencia_anios"] = 0.0
        nuevo["educacion_nivel"] = self._e_edu.get().strip()
        nuevo["seniority"] = self._e_sen.get().strip()
        nuevo["titulos"] = _lineas(self._t_titulos.get("1.0", "end"))
        nuevo["skills"] = _lineas(self._t_skills.get("1.0", "end"))
        nuevo["temas"] = _temas_de_lineas(_lineas(self._t_temas.get("1.0", "end")))
        nuevo["idiomas"] = _idiomas_de_lineas(_lineas(self._t_idiomas.get("1.0", "end")))
        nuevo["certificaciones"] = _lineas(self._t_certs.get("1.0", "end"))
        try:
            nuevo["pesos"] = {clave: float(self._v_pesos[clave].get() or defecto)
                              for clave, defecto in (("titulo", 1.0),
                                                     ("skills", 0.7),
                                                     ("tema", 0.5))}
        except ValueError:
            pass    # peso ilegible: se conserva el que hay en disco
        return nuevo

    def _perfil_guardar(self):
        nuevo = self._perfil_recolectar()
        if _perfil_vacio(nuevo):
            messagebox.showerror(
                "Perfil vacío",
                "No guardo: quedaría sin títulos, skills ni temas "
                "(no sirve para puntuar ni para buscar).")
            return
        cambios = _resumen_cambios(self._perfil_datos or {}, nuevo)
        if not cambios:
            self._perfil_estado.set("Sin cambios pendientes.")
            return
        if not messagebox.askyesno(
                "Guardar perfil",
                "Cambios a guardar:\n\n" + "\n".join(cambios)
                + "\n\n¿Guardar ahora?"):
            return
        bk = guardar_perfil(nuevo, DEFAULT_PROFILE_PATH)
        self._perfil_datos = nuevo
        msg = "Perfil guardado. El próximo ranking ya usa estos valores."
        if bk:
            msg += f" (backup del anterior: {bk.name})"
        self._perfil_estado.set(msg)
        self._perfil_backups_pintar()

    def _perfil_descartar(self):
        self._perfil_cargar(aviso="Cambios descartados: se recargó lo que "
                                   "hay en disco.")

    def _perfil_backups_pintar(self):
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
            if ruta.exists():
                # backup del actual ANTES de pisarlo (como hacia Streamlit)
                guardar_perfil(json.loads(ruta.read_text(encoding="utf-8")), ruta)
            ruta.write_text(bk.read_text(encoding="utf-8"), encoding="utf-8")
        except (OSError, json.JSONDecodeError) as exc:
            messagebox.showerror("Restaurar", f"No se pudo restaurar: {exc}")
            return
        self._perfil_cargar(aviso=f"Restaurado {bk.name}.")

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
        ttk.Label(barra, foreground="#666",
                  text="(equivalen a: main.py --solo ats · --solo boards · main.py)"
                  ).pack(side="left", padx=8)

        barra2 = ttk.Frame(tab, padding=(10, 0))
        barra2.pack(fill="x")
        self._btn_preflight = ttk.Button(
            barra2, text="Preflight (sin red)",
            command=lambda: self._corrida_arrancar(preflight=True))
        self._btn_preflight.pack(side="left")
        self._btn_detener = ttk.Button(barra2, text="Detener",
                                       command=self._corrida_detener)
        self._btn_detener.pack(side="left", padx=6)
        ttk.Label(barra2, foreground="#666", wraplength=1000,
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
            command=lambda: self._corrida_arrancar(extra="--ia"))
        self._btn_ia.pack(side="left")
        self._btn_ia_cv = ttk.Button(
            barra_ia, text="Evaluar CV con IA", command=self._ia_cv)
        self._btn_ia_cv.pack(side="left", padx=6)
        self._btn_ia_rep = ttk.Button(
            barra_ia, text="Calibrar IA local",
            command=lambda: self._corrida_arrancar(extra="--ia-reporte"))
        self._btn_ia_rep.pack(side="left")
        ttk.Label(barra_ia, foreground="#666", wraplength=1000,
                  text="IA externa (Gemini/NVIDIA, requiere IA_API_KEY en "
                       ".env): re-puntúa el crudo (no cambia qué entra a "
                       "jobs), evalúa/crea el perfil desde un CV, o mide el "
                       "acuerdo de la IA local. Consumen cuota gratuita."
                  ).pack(side="left", padx=8)

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
        self._cor_msg = ttk.Label(panel, text="", wraplength=900, foreground="#444")
        self._cor_msg.grid(row=3, column=0, columnspan=8, sticky="w")

        logm = ttk.LabelFrame(tab, text="Log de la corrida (últimas líneas)",
                              padding=6)
        logm.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self._cor_log = tk.Text(logm, height=10, wrap="none",
                                font=("Consolas", 9), state="disabled")
        sb = ttk.Scrollbar(logm, orient="vertical", command=self._cor_log.yview)
        self._cor_log.configure(yscrollcommand=sb.set)
        self._cor_log.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        self.nb.add(tab, text="  Corrida  ")

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
        # `start` desde un cmd OCULTO (CREATE_NO_WINDOW) abre la ventana
        # verde como proceso INDEPENDIENTE: cerrar la UI no la mata, y no
        # queda ninguna otra consola visible.
        verde = (f'chcp 65001 >nul && color 0A && title {CORRIDA_TITULO}'
                 f' && "{py}" -u -X utf8 main.py{args}')
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
            "¿Querés que la IA además actualice data/profile.json con el "
            "perfil propuesto?\n\nSe guarda un backup del perfil actual y "
            "podés revisarlo en la pestaña Perfil. Si elegís No, solo se "
            "genera el informe (data/cv_ia.md).")
        extra = f'--ia-cv "{ruta}"'
        if guardar:
            extra += " --ia-guardar"
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
        self.after(2000, self._corrida_refrescar)

    def _corrida_pintar(self, est, log):
        estado = est.get("estado", "nunca")
        vivo = estado == "corriendo"

        self._cor_estado.configure(
            text=ESTADO_TXT.get(estado, estado),
            foreground=ESTADO_COLOR.get(estado, "#000000"))
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
            tab, padding=(10, 8), foreground="#555", wraplength=1000,
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

        self._cred_aviso = ttk.Label(tab, foreground="#a05a00", padding=(10, 2))
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
        ttk.Label(acc, foreground="#666",
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
        self._cred_estado = ttk.Label(pies, text="", foreground="#0a7f24")
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
                foreground="#b01818")
            return
        if not usuario or (not password and not sso):
            self._cred_estado.configure(
                text="Falta el usuario, o bien la password o el acceso vía SSO "
                     "(marcá la casilla si la cuenta no tiene clave propia).",
                foreground="#b01818")
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
        self._cred_estado.configure(text=msg, foreground="#0a7f24")

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
            tab, padding=(10, 8), foreground="#555", wraplength=1000,
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
        self._ses_estado = ttk.Label(acc, text="", foreground="#555")
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
                     "aquí para verificar.", foreground="#555")
        else:
            self._ses_estado.configure(
                text="No encontré brave.exe — configura BRAVE_BIN en .env",
                foreground="#b01818")

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
            foreground="#555")
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
                foreground="#0a7f24")
            if f and f["_s"].get("pendiente"):
                quitar_pendiente(empresa)   # resuelto: como hacia app.py
                self._ses_pintar()
        else:
            self._ses_estado.configure(
                text=(f"Sin sesión aún — {res.get('motivo', '')} "
                      f"(título: {res.get('titulo') or '?'})"),
                foreground="#b01818")

    def _ses_resuelto(self):
        f = self._tabla_ses.seleccionada()
        if not f:
            return
        if quitar_pendiente(f["_s"]["empresa"]):
            self._ses_pintar()


if __name__ == "__main__":
    App().mainloop()
