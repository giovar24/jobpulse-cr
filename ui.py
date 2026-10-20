# -*- coding: utf-8 -*-
"""JobPulse CR — UI de escritorio (Tkinter), sin navegador ni servidor.

Paso 1: lista de ofertas aprobadas con filtros, busqueda, orden y detalle.
Paso 2: ranking semantico del perfil (SemanticSearchEngine + rank_with_ats).
Paso 3: cada seccion vive en su propia pestana (ttk.Notebook):
   - "Ranking": tabla filtrable + ranking del perfil
   - "Busqueda libre": consulta en lenguaje natural sobre la base indexada

Por que Tkinter: viene con Python (cero dependencias nuevas), corre en un
proceso unico que tú cierras, y su vida NO depende de ninguna pestana del
navegador (el problema con Streamlit: cerrar Brave mataba la UI).

Ejecutar:  python ui.py
"""
import logging
import queue
import sqlite3
import threading
import webbrowser
from datetime import date, datetime, timedelta
from pathlib import Path

import tkinter as tk
from tkinter import ttk

from core.ats import rank_with_ats
from core.profile import CandidateProfile
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

        self.nb = ttk.Notebook(self)

        self._pestana_rank()
        self._pestana_libre()
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


if __name__ == "__main__":
    App().mainloop()
