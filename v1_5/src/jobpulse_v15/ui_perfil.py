# -*- coding: utf-8 -*-
"""Ventana 'Mi perfil' con forma de CV (estandar harvard-ats-v1).

Reemplaza al editor plano de facts por un editor por secciones en el orden
de un CV Harvard: datos personales, resumen, educacion, experiencia (con
logros), proyectos, habilidades, certificaciones, idiomas (+otros datos de
matching). Incluye asistente de importacion (CV PDF/DOCX/TXT y profile.json)
con revision por fila: aceptar/editar/descartar.

El backend sigue siendo candidate_profile_facts; las secciones contact/
summary/project son inertes para el matching (ver profile_model.py).
"""

from __future__ import annotations

import sqlite3
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable, Mapping

from jobpulse_v15.candidate_profile import extract_candidate_profile
from jobpulse_v15.profile_model import (
    CONTACTO_CODIGOS,
    RESUMEN_CODE,
    fact_certificacion,
    fact_contacto,
    fact_educacion,
    fact_experiencia,
    fact_idioma,
    fact_proyecto,
    fact_resumen,
    fact_skill,
    facts_from_harvard_json,
    parse_certificacion_notes,
    parse_experiencia_notes,
    parse_proyecto_notes,
    suggestion_to_fact,
)

ETIQUETAS_CATEGORIA = {
    "contact": "Datos personales",
    "summary": "Resumen",
    "education": "Educación",
    "experience": "Experiencia",
    "project": "Proyectos",
    "skill": "Habilidad",
    "certification": "Certificación",
    "language": "Idioma",
    "license": "Licencia",
    "work_authorization": "Autorización de trabajo",
    "schedule": "Horario",
    "availability": "Disponibilidad",
    "other": "Otro",
}

NIVELES_CEFR = ("", "A1", "A2", "B1", "B2", "C1", "C2", "nativo")
ESTADOS_EDUCACION = ("completed", "pursuing", "unspecified")
CATEGORIAS_OTROS = ("license", "work_authorization", "schedule", "availability", "other")


# ─── Componente reutilizable: lista + formulario por seccion ────────────────

class _Campo:
    """Declaracion de un campo de formulario de seccion."""

    def __init__(
        self,
        clave: str,
        etiqueta: str,
        *,
        tipo: str = "entry",
        opciones: tuple[str, ...] = (),
        ancho: int = 34,
        alto_texto: int = 4,
    ) -> None:
        self.clave = clave
        self.etiqueta = etiqueta
        self.tipo = tipo
        self.opciones = opciones
        self.ancho = ancho
        self.alto_texto = alto_texto


class _SeccionLista(ttk.Frame):
    """Lista de entradas (Treeview) + formulario de alta/edicion/borrado.

    `fila_de_fact` define como se muestra un fact en la lista;
    `fact_de_form` construye el fact desde el formulario (dict clave->texto);
    `fact_a_form` hace la vuelta (fact -> dict clave->texto) para editar.
    """

    def __init__(
        self,
        master: tk.Widget,
        repository: Any,
        *,
        category: str,
        columnas: tuple[str, ...],
        encabezados: Mapping[str, str],
        anchos: Mapping[str, int],
        campos: list[_Campo],
        etiqueta_item: Callable[[Mapping[str, Any]], str],
        fila_de_fact: Callable[[Mapping[str, Any]], tuple],
        fact_de_form: Callable[[Mapping[str, str]], Mapping[str, Any] | None],
        fact_a_form: Callable[[Mapping[str, Any]], dict[str, str]],
        al_cambiar: Callable[[], None],
    ) -> None:
        super().__init__(master, padding=10)
        self.repository = repository
        self.category = category
        self.campos = campos
        self.etiqueta_item = etiqueta_item
        self.fila_de_fact = fila_de_fact
        self.fact_de_form = fact_de_form
        self.fact_a_form = fact_a_form
        self.al_cambiar = al_cambiar
        self.facts: dict[int, Mapping[str, Any]] = {}
        self._construir(columnas, encabezados, anchos)

    def _construir(
        self,
        columnas: tuple[str, ...],
        encabezados: Mapping[str, str],
        anchos: Mapping[str, int],
    ) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        self.tabla = ttk.Treeview(
            self, columns=columnas, show="tree headings", selectmode="browse", height=7
        )
        self.tabla.heading("#0", text="Código")
        self.tabla.column("#0", width=190, minwidth=120, stretch=True)
        for col in columnas:
            self.tabla.heading(col, text=encabezados.get(col, col))
            self.tabla.column(col, width=anchos.get(col, 120), minwidth=60, stretch=True)
        self.tabla.grid(row=0, column=0, sticky="nsew")
        self.tabla.bind("<<TreeviewSelect>>", self._al_seleccionar)

        form = ttk.LabelFrame(self, text="Entrada", padding=10)
        form.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        self.widgets: dict[str, Any] = {}
        for i, campo in enumerate(self.campos):
            fila, col = divmod(i, 2)
            col_par = col * 2
            ttk.Label(form, text=campo.etiqueta).grid(
                row=fila, column=col_par, sticky="w", padx=(0, 6), pady=(4, 0)
            )
            if campo.tipo == "text":
                widget = tk.Text(form, width=campo.ancho, height=campo.alto_texto, wrap="word")
            elif campo.tipo == "combo":
                widget = ttk.Combobox(
                    form, values=campo.opciones, state="readonly", width=campo.ancho // 2
                )
            else:
                widget = ttk.Entry(form, width=campo.ancho)
            widget.grid(
                row=fila, column=col_par + 1, sticky="ew", padx=(0, 16), pady=(4, 0)
            )
            self.widgets[campo.clave] = widget
        for c in (1, 3):
            form.columnconfigure(c, weight=1)

        botones = ttk.Frame(self)
        botones.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        ttk.Button(botones, text="Añadir / actualizar", command=self._guardar).pack(
            side="left"
        )
        ttk.Button(botones, text="Eliminar", command=self._eliminar).pack(
            side="left", padx=(8, 0)
        )
        ttk.Button(botones, text="Limpiar formulario", command=self._limpiar).pack(
            side="left", padx=(8, 0)
        )

    # ── datos ──
    def cargar(self, facts: list[Mapping[str, Any]]) -> None:
        self.facts = {int(f["profile_fact_id"]): f for f in facts}
        seleccion = self.tabla.selection()
        for item in self.tabla.get_children():
            self.tabla.delete(item)
        for fact_id, fact in sorted(self.facts.items()):
            self.tabla.insert(
                "", "end", iid=str(fact_id),
                text=self.etiqueta_item(fact), values=self.fila_de_fact(fact),
            )
        if seleccion and self.tabla.exists(seleccion[0]):
            self.tabla.selection_set(seleccion[0])

    def _valores_form(self) -> dict[str, str]:
        valores: dict[str, str] = {}
        for campo in self.campos:
            widget = self.widgets[campo.clave]
            if campo.tipo == "text":
                valores[campo.clave] = widget.get("1.0", "end").strip()
            else:
                valores[campo.clave] = widget.get().strip()
        return valores

    def _poner_valores(self, valores: Mapping[str, str]) -> None:
        for campo in self.campos:
            widget = self.widgets[campo.clave]
            texto = valores.get(campo.clave, "")
            if campo.tipo == "text":
                widget.delete("1.0", "end")
                widget.insert("1.0", texto)
            else:
                if isinstance(widget, ttk.Combobox):
                    widget.set(texto)
                else:
                    widget.delete(0, "end")
                    widget.insert(0, texto)

    # ── eventos ──
    def _al_seleccionar(self, _event: tk.Event) -> None:
        seleccion = self.tabla.selection()
        if not seleccion:
            return
        fact = self.facts.get(int(seleccion[0]))
        if fact:
            self._poner_valores(self.fact_a_form(fact))

    def _guardar(self) -> None:
        try:
            fact = self.fact_de_form(self._valores_form())
            if fact is None:
                return
            fact = dict(fact, category=self.category)
            # Si se edito el codigo de un fact existente, borrar el viejo.
            seleccion = self.tabla.selection()
            if seleccion:
                anterior = self.facts.get(int(seleccion[0]))
                if anterior and str(anterior["value_code"]).casefold() != str(
                    fact["value_code"]
                ).casefold():
                    self.repository.delete_candidate_profile_fact(
                        int(anterior["profile_fact_id"])
                    )
            self.repository.save_candidate_profile_facts([fact])
        except (ValueError, sqlite3.Error) as exc:
            messagebox.showerror("No se pudo guardar", str(exc), parent=self)
            return
        self._limpiar()
        self.al_cambiar()

    def _eliminar(self) -> None:
        seleccion = self.tabla.selection()
        if not seleccion:
            messagebox.showinfo("Selecciona una entrada", "Selecciona una entrada de la lista.", parent=self)
            return
        if not messagebox.askyesno("Eliminar", "¿Eliminar la entrada seleccionada?", parent=self):
            return
        try:
            self.repository.delete_candidate_profile_fact(int(seleccion[0]))
        except sqlite3.Error as exc:
            messagebox.showerror("No se pudo eliminar", str(exc), parent=self)
            return
        self._limpiar()
        self.al_cambiar()

    def _limpiar(self) -> None:
        self.tabla.selection_remove(self.tabla.selection())
        self._poner_valores({clave: "" for clave in self.widgets})


# ─── Dialogo de revision de importacion ─────────────────────────────────────

class _DialogoRevision(tk.Toplevel):
    """Muestra facts propuestos: aceptar/editar/descartar fila por fila."""

    def __init__(
        self,
        parent: tk.Widget,
        repository: Any,
        *,
        source_name: str,
        facts: list[dict[str, Any]],
        aviso: str,
        al_terminar: Callable[[], None],
    ) -> None:
        super().__init__(parent)
        self.repository = repository
        self.al_terminar = al_terminar
        self.source_name = source_name
        self.title(f"Revisar importación — {source_name}")
        self.geometry("920x620")
        self.minsize(760, 460)
        self.transient(parent)
        self.facts = facts
        self.filas: list[dict[str, Any]] = []

        marco = ttk.Frame(self, padding=12)
        marco.pack(fill="both", expand=True)
        ttk.Label(
            marco,
            text=(
                f"{source_name}: {len(facts)} datos propuestos. Marca los que "
                "quieres guardar; puedes editar el texto de cada uno. Se guardan "
                "como una importación revertible (snapshot automático)."
            ),
            wraplength=860,
            justify="left",
        ).pack(anchor="w", pady=(0, 4))
        ttk.Label(marco, text=aviso, foreground="#8a6d3b", wraplength=860).pack(
            anchor="w", pady=(0, 8)
        )

        lienzo = tk.Canvas(marco, highlightthickness=0)
        scroll = ttk.Scrollbar(marco, orient="vertical", command=lienzo.yview)
        self.interior = ttk.Frame(lienzo, padding=2)
        self.interior.bind(
            "<Configure>",
            lambda e: lienzo.configure(scrollregion=lienzo.bbox("all")),
        )
        lienzo.create_window((0, 0), window=self.interior, anchor="nw", width=880)
        lienzo.configure(yscrollcommand=scroll.set)
        lienzo.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        for fact in facts:
            self._agregar_fila(fact)

        pie = ttk.Frame(marco)
        pie.pack(fill="x", pady=(10, 0))
        ttk.Button(pie, text="Marcar todas", command=lambda: self._marcar(True)).pack(side="left")
        ttk.Button(pie, text="Desmarcar todas", command=lambda: self._marcar(False)).pack(
            side="left", padx=(6, 0)
        )
        self.boton_guardar = ttk.Button(
            pie, text="Guardar seleccionados", command=self._guardar
        )
        self.boton_guardar.pack(side="right")
        ttk.Button(pie, text="Cancelar", command=self.destroy).pack(side="right", padx=(0, 8))
        self._actualizar_contador()

    def _agregar_fila(self, fact: Mapping[str, Any]) -> None:
        fila = ttk.Frame(self.interior, padding=(4, 6))
        fila.pack(fill="x")
        var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            fila, variable=var,
            text=ETIQUETAS_CATEGORIA.get(str(fact.get("category")), str(fact.get("category"))),
            width=20, command=self._actualizar_contador,
        ).pack(side="left", anchor="n")
        variable_texto = tk.StringVar(value=str(fact.get("value_text") or ""))
        ttk.Entry(fila, textvariable=variable_texto).pack(
            side="left", fill="x", expand=True, padx=(8, 0)
        )
        detalle = str(fact.get("notes") or "").replace("\n", "  ·  ")[:160]
        if detalle:
            ttk.Label(fila, text=detalle, foreground="#666666", wraplength=640).pack(
                anchor="w", padx=(196, 0)
            )
        self.filas.append({"var": var, "texto": variable_texto, "fact": dict(fact)})

    def _marcar(self, estado: bool) -> None:
        for fila in self.filas:
            fila["var"].set(estado)
        self._actualizar_contador()

    def _actualizar_contador(self) -> None:
        n = sum(1 for f in self.filas if f["var"].get())
        self.boton_guardar.configure(text=f"Guardar seleccionados ({n})")

    def _guardar(self) -> None:
        elegidos = []
        for fila in self.filas:
            if not fila["var"].get():
                continue
            fact = dict(fila["fact"])
            fact["value_text"] = fila["texto"].get().strip()
            if not fact["value_text"]:
                continue
            elegidos.append(fact)
        if not elegidos:
            messagebox.showinfo("Nada seleccionado", "Marca al menos un dato para guardar.", parent=self)
            return
        try:
            import_id = self.repository.import_candidate_profile_facts(
                self.source_name, elegidos
            )
        except (ValueError, sqlite3.Error) as exc:
            messagebox.showerror("No se pudo importar", str(exc), parent=self)
            return
        self.al_terminar()
        messagebox.showinfo(
            "Importación lista",
            f"Import #{import_id} creada con {len(elegidos)} datos.\n"
            "Puedes revertirla desde el botón 'Revertir importación'.",
            parent=self,
        )
        self.destroy()


# ─── Ventana principal: editor con forma de CV ──────────────────────────────

class CandidateProfileWindowCV:
    """Editor del perfil con secciones en orden de CV Harvard/ATS."""

    def __init__(self, parent: tk.Tk, repository: Any) -> None:
        self.repository = repository
        self.window = tk.Toplevel(parent)
        self.window.title("Mi perfil — CV Harvard/ATS · JobPulse CR 1.5")
        self.window.geometry("1020x720")
        self.window.minsize(880, 560)
        self.window.transient(parent)
        self.window.protocol("WM_DELETE_WINDOW", self.window.destroy)
        self._contacto_facts: dict[str, Mapping[str, Any]] = {}
        self._summary_fact: Mapping[str, Any] | None = None
        self._construir()
        self.refresh()

    # ── construccion ──
    def _construir(self) -> None:
        barra = ttk.Frame(self.window, padding=(12, 10, 12, 0))
        barra.pack(fill="x")
        ttk.Button(barra, text="Importar CV…", command=self._importar_cv).pack(side="left")
        ttk.Button(barra, text="Importar profile.json…", command=self._importar_json).pack(
            side="left", padx=(8, 0)
        )
        ttk.Button(barra, text="Revertir importación…", command=self._revertir).pack(
            side="left", padx=(8, 0)
        )
        ttk.Button(barra, text="Cerrar", command=self.window.destroy).pack(side="right")

        self.notebook = ttk.Notebook(self.window, padding=6)
        self.notebook.pack(fill="both", expand=True, padx=12, pady=10)

        self._tab_datos_personales()
        self._tab_resumen()
        self._tab_educacion()
        self._tab_experiencia()
        self._tab_proyectos()
        self._tab_habilidades()
        self._tab_certificaciones()
        self._tab_idiomas()
        self._tab_otros()

        self.estado = ttk.Label(self.window, text="", foreground="#555555", padding=(14, 0, 14, 8))
        self.estado.pack(fill="x", side="bottom")

    def _tab_datos_personales(self) -> None:
        tab = ttk.Frame(self.notebook, padding=16)
        self.notebook.add(tab, text="  Datos personales  ")
        for i in (1,):
            tab.columnconfigure(i, weight=1)
        ttk.Label(
            tab,
            text="Aparecen al inicio de tu CV. No se usan para el matching, "
                 "solo para documentos y reportes.",
            wraplength=640, justify="left", foreground="#555555",
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))
        self.contacto_vars: dict[str, tk.StringVar] = {}
        etiquetas = {
            "nombre": "Nombre completo",
            "email": "Correo electrónico",
            "telefono": "Teléfono",
            "ubicacion": "Ubicación",
            "linkedin": "LinkedIn / portfolio",
        }
        for fila, (codigo, etiqueta) in enumerate(etiquetas.items(), start=1):
            ttk.Label(tab, text=etiqueta).grid(row=fila, column=0, sticky="w", pady=6)
            var = tk.StringVar()
            ttk.Entry(tab, textvariable=var, width=56).grid(
                row=fila, column=1, sticky="ew", pady=6, padx=(10, 0)
            )
            self.contacto_vars[codigo] = var
        ttk.Button(tab, text="Guardar datos personales", command=self._guardar_contacto).grid(
            row=len(etiquetas) + 1, column=1, sticky="e", pady=(14, 0)
        )

    def _tab_resumen(self) -> None:
        tab = ttk.Frame(self.notebook, padding=16)
        self.notebook.add(tab, text="  Resumen  ")
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(1, weight=1)
        ttk.Label(
            tab,
            text="Perfil profesional de 2–4 líneas: quién sos, especialidad y años "
                 "de experiencia. Es lo primero que lee un reclutador.",
            wraplength=640, justify="left", foreground="#555555",
        ).grid(row=0, column=0, sticky="w", pady=(0, 8))
        self.resumen_texto = tk.Text(tab, height=9, wrap="word")
        self.resumen_texto.grid(row=1, column=0, sticky="nsew")
        ttk.Button(tab, text="Guardar resumen", command=self._guardar_resumen).grid(
            row=2, column=0, sticky="e", pady=(10, 0)
        )

    def _tab_educacion(self) -> None:
        campos = [
            _Campo("institucion", "Institución", ancho=34),
            _Campo("titulo", "Título", ancho=34),
            _Campo("campo", "Campo / carrera", ancho=22),
            _Campo("inicio", "Inicio (MM/YYYY)", ancho=16),
            _Campo("fin", "Fin (MM/YYYY)", ancho=16),
            _Campo("estado", "Estado", tipo="combo", opciones=ESTADOS_EDUCACION, ancho=18),
        ]
        self.seccion_educacion = _SeccionLista(
            self.notebook, self.repository, category="education",
            columnas=("dato", "campo", "estado"),
            encabezados={"dato": "Título — Institución (periodo)", "campo": "Campo", "estado": "Estado"},
            anchos={"dato": 330, "campo": 130, "estado": 100},
            campos=campos,
            etiqueta_item=lambda f: str(f.get("value_code")),
            fila_de_fact=lambda f: (f.get("value_text"), f.get("notes") or "", f.get("education_status") or ""),
            fact_de_form=lambda v: (
                None if not (v["titulo"].strip() or v["institucion"].strip())
                else dict(fact_educacion(
                    institucion=v["institucion"], titulo=v["titulo"], campo=v["campo"],
                    inicio=v["inicio"], fin=v["fin"],
                    estado=v["estado"] or "completed",
                ))
            ),
            fact_a_form=lambda f: {
                "institucion": self._institucion_de_texto(f.get("value_text")),
                "titulo": self._titulo_de_texto(f.get("value_text")),
                "campo": (f.get("notes") or "").removeprefix("Campo: "),
                "inicio": "", "fin": "", "estado": f.get("education_status") or "completed",
            },
            al_cambiar=self.refresh,
        )
        self.notebook.add(self.seccion_educacion, text="  Educación  ")

    @staticmethod
    def _titulo_de_texto(texto: str) -> str:
        return (texto or "").split(" — ")[0].strip()

    @staticmethod
    def _institucion_de_texto(texto: str) -> str:
        partes = (texto or "").split(" — ")
        if len(partes) < 2:
            return ""
        institucion = partes[1].strip()
        return institucion.rsplit(" (", 1)[0].strip()

    def _tab_experiencia(self) -> None:
        campos = [
            _Campo("empresa", "Empresa", ancho=30),
            _Campo("puesto", "Puesto", ancho=30),
            _Campo("ubicacion", "Ubicación", ancho=24),
            _Campo("inicio", "Inicio (MM/YYYY)", ancho=16),
            _Campo("fin", "Fin (MM/YYYY)", ancho=16),
            _Campo("logros", "Logros (uno por línea)", tipo="text", ancho=56, alto_texto=5),
        ]
        self.seccion_experiencia = _SeccionLista(
            self.notebook, self.repository, category="experience",
            columnas=("dato", "anios"),
            encabezados={"dato": "Puesto — Empresa (periodo)", "anios": "Años"},
            anchos={"dato": 430, "anios": 70},
            campos=campos,
            etiqueta_item=lambda f: str(f.get("value_code")),
            fila_de_fact=lambda f: (f.get("value_text"), f.get("numeric_value") if f.get("numeric_value") is not None else ""),
            fact_de_form=lambda v: (
                None if not (v["empresa"].strip() or v["puesto"].strip())
                else dict(fact_experiencia(
                    empresa=v["empresa"], puesto=v["puesto"], ubicacion=v["ubicacion"],
                    inicio=v["inicio"], fin=v["fin"],
                    logros=[l for l in v["logros"].splitlines() if l.strip()],
                ))
            ),
            fact_a_form=lambda f: {
                "empresa": self._empresa_de_texto(f.get("value_text")),
                "puesto": self._puesto_de_texto(f.get("value_text")),
                "ubicacion": parse_experiencia_notes(f.get("notes"))[0],
                "logros": "\n".join(parse_experiencia_notes(f.get("notes"))[1]),
                "inicio": "", "fin": "",
            },
            al_cambiar=self.refresh,
        )
        self.notebook.add(self.seccion_experiencia, text="  Experiencia  ")

    @staticmethod
    def _puesto_de_texto(texto: str) -> str:
        return (texto or "").split(" — ")[0].strip()

    @staticmethod
    def _empresa_de_texto(texto: str) -> str:
        partes = (texto or "").split(" — ")
        if len(partes) < 2:
            return ""
        empresa = partes[1].strip()
        return empresa.rsplit(" (", 1)[0].strip()

    def _tab_proyectos(self) -> None:
        campos = [
            _Campo("nombre", "Nombre", ancho=34),
            _Campo("rol", "Rol / tecnologías", ancho=34),
            _Campo("enlace", "Enlace", ancho=40),
            _Campo("descripcion", "Descripción", tipo="text", ancho=56, alto_texto=4),
        ]
        self.seccion_proyectos = _SeccionLista(
            self.notebook, self.repository, category="project",
            columnas=("nombre", "rol", "enlace"),
            encabezados={"nombre": "Proyecto", "rol": "Rol / tecnologías", "enlace": "Enlace"},
            anchos={"nombre": 200, "rol": 200, "enlace": 180},
            campos=campos,
            etiqueta_item=lambda f: str(f.get("value_code")),
            fila_de_fact=lambda f: (f.get("value_text"),) + parse_proyecto_notes(f.get("notes"))[:2],
            fact_de_form=lambda v: (
                None if not v["nombre"].strip()
                else dict(fact_proyecto(
                    nombre=v["nombre"], rol=v["rol"], descripcion=v["descripcion"], enlace=v["enlace"],
                ))
            ),
            fact_a_form=lambda f: dict(zip(
                ("nombre", "rol", "enlace", "descripcion"),
                (f.get("value_text"), *parse_proyecto_notes(f.get("notes"))),
            )),
            al_cambiar=self.refresh,
        )
        self.notebook.add(self.seccion_proyectos, text="  Proyectos  ")

    def _tab_habilidades(self) -> None:
        campos = [_Campo("nombre", "Habilidad", ancho=40)]
        self.seccion_habilidades = _SeccionLista(
            self.notebook, self.repository, category="skill",
            columnas=("dato",),
            encabezados={"dato": "Habilidad"},
            anchos={"dato": 420},
            campos=campos,
            etiqueta_item=lambda f: str(f.get("value_code")),
            fila_de_fact=lambda f: (f.get("value_text"),),
            fact_de_form=lambda v: (
                None if not v["nombre"].strip()
                else dict(fact_skill(v["nombre"]))
            ),
            fact_a_form=lambda f: {"nombre": f.get("value_text")},
            al_cambiar=self.refresh,
        )
        self.notebook.add(self.seccion_habilidades, text="  Habilidades  ")

    def _tab_certificaciones(self) -> None:
        campos = [
            _Campo("nombre", "Certificación", ancho=42),
            _Campo("emisor", "Emisor", ancho=30),
            _Campo("anio", "Año", ancho=10),
        ]
        self.seccion_certificaciones = _SeccionLista(
            self.notebook, self.repository, category="certification",
            columnas=("dato", "detalle"),
            encabezados={"dato": "Certificación", "detalle": "Emisor · Año"},
            anchos={"dato": 320, "detalle": 200},
            campos=campos,
            etiqueta_item=lambda f: str(f.get("value_code")),
            fila_de_fact=lambda f: (f.get("value_text"), f.get("notes") or ""),
            fact_de_form=lambda v: (
                None if not v["nombre"].strip()
                else dict(fact_certificacion(nombre=v["nombre"], emisor=v["emisor"], anio=v["anio"]))
            ),
            fact_a_form=lambda f: dict(zip(
                ("nombre", "emisor", "anio"),
                (f.get("value_text"), *parse_certificacion_notes(f.get("notes"))),
            )),
            al_cambiar=self.refresh,
        )
        self.notebook.add(self.seccion_certificaciones, text="  Certificaciones  ")

    def _tab_idiomas(self) -> None:
        campos = [
            _Campo("idioma", "Idioma", ancho=28),
            _Campo("nivel", "Nivel (CEFR)", tipo="combo", opciones=NIVELES_CEFR, ancho=14),
        ]
        self.seccion_idiomas = _SeccionLista(
            self.notebook, self.repository, category="language",
            columnas=("dato", "nivel"),
            encabezados={"dato": "Idioma", "nivel": "Nivel"},
            anchos={"dato": 300, "nivel": 120},
            campos=campos,
            etiqueta_item=lambda f: str(f.get("value_code")),
            fila_de_fact=lambda f: (f.get("value_text"), f.get("qualifier") or ""),
            fact_de_form=lambda v: (
                None if not v["idioma"].strip()
                else dict(fact_idioma(v["idioma"], v["nivel"]))
            ),
            fact_a_form=lambda f: {"idioma": f.get("value_text"), "nivel": f.get("qualifier") or ""},
            al_cambiar=self.refresh,
        )
        self.notebook.add(self.seccion_idiomas, text="  Idiomas  ")

    def _tab_otros(self) -> None:
        campos = [
            _Campo("codigo", "Código", ancho=26),
            _Campo("valor", "Dato", ancho=40),
            _Campo("nivel", "Nivel / detalle", ancho=26),
        ]
        self.seccion_otros = _SeccionLista(
            self.notebook, self.repository, category="other",
            columnas=("dato", "nivel"),
            encabezados={"dato": "Dato", "nivel": "Nivel / detalle"},
            anchos={"dato": 380, "nivel": 180},
            campos=campos,
            etiqueta_item=lambda f: f"{f.get('category')} · {f.get('value_code')}",
            fila_de_fact=lambda f: (f.get("value_text"), f.get("qualifier") or ""),
            fact_de_form=lambda v: (
                None if not (v["codigo"].strip() and v["valor"].strip())
                else {
                    "category": "other",
                    "value_code": v["codigo"].strip(),
                    "value_text": v["valor"].strip(),
                    "qualifier": v["nivel"].strip() or None,
                }
            ),
            fact_a_form=lambda f: {
                "codigo": f.get("value_code"), "valor": f.get("value_text"),
                "nivel": f.get("qualifier") or "",
            },
            al_cambiar=self.refresh,
        )
        self.notebook.add(self.seccion_otros, text="  Otros (matching)  ")

    # ── datos <-> UI ──
    def refresh(self) -> None:
        try:
            facts = self.repository.list_candidate_profile_facts()
        except sqlite3.Error as exc:
            messagebox.showerror("No se pudo cargar el perfil", str(exc), parent=self.window)
            return
        self._contacto_facts = {}
        self._summary_fact = None
        por_categoria: dict[str, list[Mapping[str, Any]]] = {}
        for fact in facts:
            categoria = fact["category"]
            if categoria == "contact":
                codigo = str(fact["value_code"]).casefold()
                if codigo in CONTACTO_CODIGOS:
                    self._contacto_facts[codigo] = fact
                    self.contacto_vars[codigo].set(fact["value_text"])
                continue
            if categoria == "summary":
                self._summary_fact = fact
                if not self.resumen_texto.get("1.0", "end").strip():
                    self.resumen_texto.delete("1.0", "end")
                    self.resumen_texto.insert("1.0", fact["value_text"])
                continue
            por_categoria.setdefault(categoria, []).append(fact)

        self.seccion_educacion.cargar(por_categoria.get("education", []))
        self.seccion_experiencia.cargar(por_categoria.get("experience", []))
        self.seccion_proyectos.cargar(por_categoria.get("project", []))
        self.seccion_habilidades.cargar(por_categoria.get("skill", []))
        self.seccion_certificaciones.cargar(por_categoria.get("certification", []))
        self.seccion_idiomas.cargar(por_categoria.get("language", []))
        otros = [f for f in facts if f["category"] in CATEGORIAS_OTROS]
        self.seccion_otros.cargar(otros)
        self.estado.configure(text=f"{len(facts)} datos en el perfil")

    def _guardar_contacto(self) -> None:
        try:
            lotes = []
            for codigo in CONTACTO_CODIGOS:
                valor = self.contacto_vars[codigo].get().strip()
                existente = self._contacto_facts.get(codigo)
                if valor:
                    lotes.append(fact_contacto(codigo, valor))
                elif existente:
                    self.repository.delete_candidate_profile_fact(
                        int(existente["profile_fact_id"])
                    )
            if lotes:
                self.repository.save_candidate_profile_facts(lotes)
        except (ValueError, sqlite3.Error) as exc:
            messagebox.showerror("No se pudo guardar", str(exc), parent=self.window)
            return
        self.refresh()

    def _guardar_resumen(self) -> None:
        texto = self.resumen_texto.get("1.0", "end").strip()
        try:
            if texto:
                self.repository.save_candidate_profile_facts([fact_resumen(texto)])
            elif self._summary_fact is not None:
                self.repository.delete_candidate_profile_fact(
                    int(self._summary_fact["profile_fact_id"])
                )
        except (ValueError, sqlite3.Error) as exc:
            messagebox.showerror("No se pudo guardar", str(exc), parent=self.window)
            return
        self.refresh()

    # ── importacion ──
    def _importar_cv(self) -> None:
        ruta = filedialog.askopenfilename(
            parent=self.window,
            title="Seleccionar CV",
            filetypes=(("CV compatibles", "*.pdf *.docx *.txt"), ("Todos", "*.*")),
        )
        if not ruta:
            return
        try:
            importado = extract_candidate_profile(ruta)
        except (OSError, ValueError) as exc:
            messagebox.showerror("No se pudo leer el CV", str(exc), parent=self.window)
            return
        facts = [suggestion_to_fact(s) for s in importado.suggestions]
        avisos = "\n".join(importado.warnings)
        if not facts:
            messagebox.showinfo(
                "Sin sugerencias",
                f"No se detectaron datos en {importado.source_name}.\n\n{avisos}",
                parent=self.window,
            )
            return
        _DialogoRevision(
            self.window, self.repository, source_name=importado.source_name,
            facts=facts, aviso=avisos, al_terminar=self.refresh,
        )

    def _importar_json(self) -> None:
        ruta = filedialog.askopenfilename(
            parent=self.window,
            title="Seleccionar profile.json (harvard-ats-v1)",
            filetypes=(("Perfil JSON", "*.json"),),
        )
        if not ruta:
            return
        try:
            source_name, facts = facts_from_harvard_json(ruta)
        except (OSError, ValueError, KeyError) as exc:
            messagebox.showerror("No se pudo leer el perfil", str(exc), parent=self.window)
            return
        if not facts:
            messagebox.showinfo("Sin datos", "El JSON no contiene secciones reconocidas.", parent=self.window)
            return
        _DialogoRevision(
            self.window, self.repository, source_name=source_name,
            facts=facts, aviso="Revisa cada sección: el import es revertible.",
            al_terminar=self.refresh,
        )

    def _revertir(self) -> None:
        try:
            imports = self.repository.list_candidate_profile_imports(limit=1)
        except sqlite3.Error as exc:
            messagebox.showerror("No se pudo consultar el historial", str(exc), parent=self.window)
            return
        if not imports:
            messagebox.showinfo("Sin importaciones", "No hay importaciones para revertir.", parent=self.window)
            return
        latest = imports[0]
        if not messagebox.askyesno(
            "Revertir importación",
            f"¿Revertir la importación más reciente de {latest['source_name']}?\n\n"
            "Se eliminarán los datos nuevos y se restaurarán los valores anteriores.",
            parent=self.window,
        ):
            return
        try:
            resultado = self.repository.revert_last_candidate_profile_import()
        except sqlite3.Error as exc:
            messagebox.showerror("No se pudo revertir", str(exc), parent=self.window)
            return
        if resultado is None:
            messagebox.showerror("Importación no disponible", "La importación ya no existe.", parent=self.window)
            return
        self.refresh()
        messagebox.showinfo(
            "Importación revertida",
            f"Datos nuevos eliminados: {resultado['removed']}\n"
            f"Valores anteriores restaurados: {resultado['restored']}\n"
            f"Cambios manuales preservados: {resultado['preserved_manual']}",
            parent=self.window,
        )
