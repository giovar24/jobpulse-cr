# -*- coding: utf-8 -*-
"""JobPulse CR — UI de escritorio (Tkinter), sin navegador ni servidor.

Paso 1: ver las ofertas aprobadas con filtros, busqueda, orden y detalle.
Sin modelo de embeddings y sin escribir en SQLite (lectura pura): arranca
al instante, para poder juzgar si la tabla es usable antes de invertir mas.

Por que Tkinter: viene con Python (cero dependencias nuevas), corre en un
proceso unico que tú cierras, y su vida NO depende de ninguna pestana del
navegador (el problema con Streamlit: cerrar Brave mataba la UI).

Ejecutar:  python ui.py
"""
import sqlite3
import webbrowser
from datetime import date, datetime, timedelta
from pathlib import Path

import tkinter as tk
from tkinter import ttk

from providers.source_groups import grupo_publico, GRUPOS_PUBLICOS

DB = Path(__file__).resolve().parent / "data" / "jobs.db"

# Etiquetas en español para los 2 grupos publicos (lo que se muestra).
GRUPO_TXT = {
    "estatales": "Portal estatal (ANE/CINDE/Procomer)",
    "otros": "Resto (agregadores + ATS)",
}
# (id, encabezado, ancho, alineacion)
COLS = (
    ("score", "Match", 70, "e"),
    ("fuente", "Fuente", 110, "w"),
    ("grupo", "Grupo", 48, "center"),
    ("puesto", "Puesto", 300, "w"),
    ("empresa", "Empresa", 170, "w"),
    ("ubicacion", "Ubicacion", 150, "w"),
    ("publicada", "Publicada", 105, "center"),
)


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


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("JobPulse CR — Ofertas")
        self.geometry("1340x840")
        self.minsize(940, 560)

        self.todas = []
        self._orden = ("score", False)   # score descendente al abrir (lo mejor arriba)

        self._grupo_et = tk.StringVar(value="(todas)")
        self._busca = tk.StringVar(value="")
        self._dias = tk.StringVar(value="30")
        self._conteo = tk.StringVar(value="Cargando...")
        self._frescura = tk.StringVar(value="")

        self._construir()
        self._recargar()
        for var in (self._busca, self._grupo_et, self._dias):
            var.trace_add("write", lambda *_: self._pintar())

    # ------------------------------------------------------------- interfaz
    def _construir(self):
        barra = ttk.Frame(self, padding=(10, 8))
        barra.pack(fill="x")

        ttk.Label(barra, text="Grupo:").pack(side="left")
        cb = ttk.Combobox(barra, textvariable=self._grupo_et, state="readonly",
                          width=36, values=["(todas)"] + [GRUPO_TXT[g] for g in GRUPOS_PUBLICOS])
        cb.pack(side="left", padx=(4, 14))

        ttk.Label(barra, text="Buscar:").pack(side="left")
        ttk.Entry(barra, textvariable=self._busca, width=34).pack(side="left", padx=(4, 14))

        ttk.Label(barra, text="Dias:").pack(side="left")
        ttk.Entry(barra, textvariable=self._dias, width=6).pack(side="left", padx=(4, 14))

        ttk.Button(barra, text="Actualizar", command=self._recargar).pack(side="left")
        ttk.Button(barra, text="Abrir seleccion", command=self._abrir).pack(side="left", padx=6)
        ttk.Label(barra, textvariable=self._frescura, foreground="#666").pack(side="right")

        # ---- tabla
        marco = ttk.Frame(self)
        marco.pack(fill="both", expand=True, padx=10)

        self._tree = ttk.Treeview(
            marco, columns=[c[0] for c in COLS], show="headings", selectmode="browse"
        )
        for cid, texto, ancho, alin in COLS:
            self._tree.heading(cid, text=texto, command=lambda c=cid: self._ordenar_por(c))
            self._tree.column(cid, width=ancho, anchor=alin, stretch=(cid == "puesto"))

        vs = ttk.Scrollbar(marco, orient="vertical", command=self._tree.yview)
        hs = ttk.Scrollbar(marco, orient="horizontal", command=self._tree.xview)
        self._tree.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)

        self._tree.grid(row=0, column=0, sticky="nsew")
        vs.grid(row=0, column=1, sticky="ns")
        hs.grid(row=1, column=0, sticky="ew")
        marco.rowconfigure(0, weight=1)
        marco.columnconfigure(0, weight=1)

        self._tree.bind("<<TreeviewSelect>>", self._al_seleccionar)
        self._tree.bind("<Double-1>", lambda _e: self._abrir())
        self._tree.bind("<Return>", lambda _e: self._abrir())

        # ---- detalle
        det = ttk.LabelFrame(self, text="Detalle", padding=8)
        det.pack(fill="both", padx=10, pady=(8, 4))
        fondo = ttk.Style(self).lookup("Treeview", "background") or "#ffffff"
        self._detalle = tk.Text(det, height=8, wrap="word", state="disabled",
                                relief="flat", background=fondo,
                                highlightthickness=0)
        self._detalle.pack(fill="both", expand=True)
        self._detalle.tag_configure("tit", font=("Segoe UI", 10, "bold"))
        self._detalle.tag_configure("rot", foreground="#666",
                                    font=("Segoe UI", 9, "bold"))

        # ---- pie
        ttk.Label(self, textvariable=self._conteo, anchor="w",
                  padding=(10, 4)).pack(fill="x")

    # ------------------------------------------------------------ acciones
    def _recargar(self):
        self.todas, ultima = cargar()
        if self.todas is None:
            self._conteo.set(f"No se encontro {DB.name}. Ejecuta primero `python main.py`.")
            return
        if ultima:
            try:
                ts = datetime.fromisoformat(str(ultima)[:19])
                horas = (datetime.now() - ts).total_seconds() / 3600
                suf = (f"hace {int(horas)} h" if horas >= 1
                       else f"hace {int((datetime.now() - ts).total_seconds() // 60)} min")
                self._frescura.set(f"Ultima corrida {ts:%d/%m/%Y %H:%M} ({suf})")
            except ValueError:
                self._frescura.set(f"Ultima corrida: {ultima}")
        self._pintar()

    def _dias_actuales(self):
        try:
            return int(self._dias.get() or 0)
        except ValueError:
            return 0

    def _filas_visibles(self):
        q = self._busca.get().strip().lower()
        dias = self._dias_actuales()
        filas = [f for f in self.todas if en_ventana(f["fecha"], dias)]

        et = self._grupo_et.get()
        if et != "(todas)":
            clave = next((g for g in GRUPOS_PUBLICOS if GRUPO_TXT[g] == et), None)
            if clave:
                filas = [f for f in filas if f["grupo_int"] == clave]

        if q:
            filas = [f for f in filas
                     if q in (f["puesto"] + " " + f["empresa"] + " " + f["ubicacion"]).lower()]

        clave_ord, asc = self._orden
        return ordenar(filas, clave_ord, asc)

    def _pintar(self):
        if not self.todas:
            return
        filas = self._filas_visibles()

        hijos = self._tree.get_children()
        if hijos:
            self._tree.delete(*hijos)
        for i, f in enumerate(filas):
            iid = str(f["job_id"]) if f["job_id"] is not None else f"row{i}"
            self._tree.insert("", "end", iid=iid, values=(
                "" if f["score"] is None else f"{f['score']:.1f}",
                f["fuente"], f["grupo"], f["puesto"], f["empresa"],
                f["ubicacion"], f["publicada"],
            ))
        self._conteo.set(f"Mostrando {len(filas)} de {len(self.todas)} ofertas")

    def _ordenar_por(self, col):
        self._orden = (col, self._orden[0] != col or not self._orden[1])
        for cid, texto, _a, _b in COLS:
            marca = ""
            if cid == self._orden[0]:
                marca = " ▼" if self._orden[1] else " ▲"
            self._tree.heading(cid, text=texto + marca)
        self._pintar()

    def _actual(self):
        sel = self._tree.selection()
        if not sel:
            return None
        return next((f for f in self.todas if str(f["job_id"]) == sel[0]), None)

    def _al_seleccionar(self, _e):
        f = self._actual()
        t = self._detalle
        t.configure(state="normal")
        t.delete("1.0", "end")
        if f:
            t.insert("end", f["puesto"] + "\n", "tit")
            t.insert("end", f"{f['empresa']}  ·  {f['ubicacion']}  ·  "
                            f"{f['publicada']}  ·  "
                            f"{'Match ' + str(f['score']) + '%' if f['score'] is not None else 'sin score'}\n\n")
            t.insert("end", f["link"] + "\n\n")
            t.insert("end", "Descripcion\n", "rot")
            t.insert("end", f["descripcion"][:2500] + "\n\n")
            if f["requisitos"]:
                t.insert("end", "Requisitos\n", "rot")
                t.insert("end", f["requisitos"][:1500])
        t.configure(state="disabled")

    def _abrir(self):
        f = self._actual()
        if f and f["link"]:
            webbrowser.open(f["link"])


if __name__ == "__main__":
    App().mainloop()
