# -*- coding: utf-8 -*-
"""JobPulse CR — UI de escritorio (Tkinter), sin navegador ni servidor.

Paso 1: lista de ofertas aprobadas con filtros, busqueda, orden y detalle.
Paso 2: ranking semantico del perfil (SemanticSearchEngine + rank_with_ats).
Paso 3: cada seccion vive en su propia pestana (ttk.Notebook):
   - "Ranking": tabla filtrable + ranking del perfil
   - "Busqueda libre": consulta en lenguaje natural sobre la base indexada
Paso 4: pestana "Perfil": editor con resumen de cambios y backups.

Por que Tkinter: viene con Python (cero dependencias nuevas), corre en un
proceso unico que tú cierras, y su vida NO depende de ninguna pestana del
navegador (el problema con Streamlit: cerrar Brave mataba la UI).

Ejecutar:  python ui.py
"""
import json
import logging
import queue
import sqlite3
import threading
import webbrowser
from datetime import date, datetime, timedelta
from pathlib import Path

import tkinter as tk
from tkinter import messagebox, ttk

from core.ats import rank_with_ats
from core.profile import (CandidateProfile, DEFAULT_PROFILE_PATH,
                          backups_perfil, guardar_perfil)
from core.search_engine import SemanticSearchEngine
from providers.source_groups import grupo_publico, GRUPOS_PUBLICOS

BASE = Path(__file__).resolve().parent
DB = BASE / "data" / "jobs.db"

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

# (id, encabezado, ancho, alineacion)
COLS_RANK = (
    ("score", "Match", 70, "e"),
    ("ats", "ATS", 58, "e"),
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
        out.append({
            "job_id": f["job_id"],
            "score": None if sc is None else round(sc * 100, 1),
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
                self._corriendo = False
                self._botones(True)
                if tipo == "rank":
                    self._ranking_listo(dato)
                elif tipo == "libre":
                    self._libre_listo(*dato)
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
            t.insert("end", linea + "\n\n")
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


if __name__ == "__main__":
    App().mainloop()
