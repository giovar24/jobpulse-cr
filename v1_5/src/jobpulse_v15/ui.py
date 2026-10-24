"""Interfaz de escritorio Tkinter para buscar y revisar vacantes v1.5."""

import csv
import hashlib
import queue
import re
import sqlite3
import threading
import tkinter as tk
import webbrowser
from dataclasses import replace
from tkinter import filedialog, messagebox, ttk
from typing import Any, Mapping, Sequence

from jobpulse_v15.ane import AneFetcher
from jobpulse_v15.analysis import EXTRACTOR_VERSION, RuleBasedExtractor
from jobpulse_v15.ashby import AshbyFetcher
from jobpulse_v15.candidate_profile import (
    CandidateProfileImport,
    extract_candidate_profile,
)
from jobpulse_v15.cinde import CindeFetcher
from jobpulse_v15.db import Database
from jobpulse_v15.exports import (
    PROFILE_COMPATIBILITY_LABELS,
    write_job_search_csv,
)
from jobpulse_v15.greenhouse import GreenhouseFetcher
from jobpulse_v15.ingestion import IngestionError, IngestionService
from jobpulse_v15.indeed import IndeedFetcher
from jobpulse_v15.lever import LeverFetcher
from jobpulse_v15.linkedin import LinkedInFetcher
from jobpulse_v15.procomer import ProcomerFetcher
from jobpulse_v15.recruitee import RecruiteeFetcher
from jobpulse_v15.repository import (
    REQUIREMENT_CATEGORIES as REVIEW_CATEGORIES,
    Repository,
    job_review_reasons,
)
from jobpulse_v15.successfactors import SuccessFactorsFetcher
from jobpulse_v15.workable import WorkableFetcher
from jobpulse_v15.workday import MAX_PAGES, WorkdayFetcher


REQUIREMENT_CATEGORIES = (
    "language",
    "education",
    "license",
    "experience",
    "skill",
    "certification",
    "work_authorization",
    "schedule",
    "availability",
    "other",
)
MANUAL_JOB_STATUSES = {
    "Sin marcar": None,
    "Revisada": "reviewed",
    "Aplicada": "applied",
    "Archivada": "archived",
    "Descartada": "dismissed",
}
MANUAL_JOB_STATUS_LABELS = {
    status: label for label, status in MANUAL_JOB_STATUSES.items() if status
}
APPLICATION_STATUS_LABELS = {
    "Planificada": "planned",
    "Aplicada": "applied",
    "Contacto de reclutamiento": "recruiter_contact",
    "Entrevista": "interview",
    "Oferta": "offer",
    "Rechazada": "rejected",
    "Retirada": "withdrawn",
}
APPLICATION_STATUS_NAMES = {
    status: label for label, status in APPLICATION_STATUS_LABELS.items()
}
PROFILE_COMPATIBILITY_FILTERS = {
    "Cualquiera": None,
    "Coinciden requisitos obligatorios": "matched",
    "Datos no registrados": "not_registered",
    "Brecha conocida": "below_requirement",
    "Requiere revision": "needs_review",
    "Sin analisis": "not_analyzed",
    "Sin requisitos obligatorios": "no_required",
}
JOB_REVIEW_REASON_LABELS = {
    "missing_company": "Empresa no registrada",
    "missing_country": "Pais no registrado",
    "location_not_normalized": "Ubicacion sin normalizar",
    "analysis_missing": "Sin analisis",
    "analysis_pending": "Analisis pendiente",
    "analysis_partial": "Analisis parcial",
    "analysis_error": "Error en analisis",
}
JOB_REVIEW_REASON_OPTIONS = {"Cualquiera": None}
JOB_REVIEW_REASON_OPTIONS.update(
    {
        label: code
        for code, label in JOB_REVIEW_REASON_LABELS.items()
    }
)
JOB_REVIEW_REASON_OPTIONS.update(
    {
        f"Campo {category} desconocido": f"unknown_field:{category}"
        for category in sorted(REVIEW_CATEGORIES)
    }
)
JOB_REVIEW_REASON_OPTIONS.update(
    {
        f"Campo {category} ambiguo": f"ambiguous_field:{category}"
        for category in sorted(REVIEW_CATEGORIES)
    }
)
JOB_SEARCH_SORT_OPTIONS = {
    "Actividad reciente": "last_seen",
    "Fecha de publicacion": "date_published",
}


def format_job_review_reasons(reasons: Sequence[str]) -> list[str]:
    """Convierte codigos de calidad en mensajes cortos para la interfaz."""
    labels = []
    for reason in reasons:
        if reason.startswith("unknown_field:"):
            labels.append(f"Campo {reason.split(':', 1)[1]} desconocido")
        elif reason.startswith("ambiguous_field:"):
            labels.append(f"Campo {reason.split(':', 1)[1]} ambiguo")
        else:
            labels.append(JOB_REVIEW_REASON_LABELS.get(reason, reason))
    return labels


def format_job_review_summary(reasons: Sequence[str]) -> str:
    if not reasons:
        return "OK"
    return f"Revisar ({len(reasons)})"


def format_job_detail(
    job: Mapping[str, Any],
    analysis: Mapping[str, Any] | None,
    sources: Sequence[Mapping[str, Any]],
) -> str:
    """Forma un resumen legible para el panel de detalle."""
    salary_fields = ("salary_min", "salary_max", "salary_currency", "salary_period")
    if any(job.get(field) is not None for field in salary_fields):
        salary_min = job.get("salary_min")
        salary_max = job.get("salary_max")
        salary_summary = (
            f"{salary_min if salary_min is not None else '?'} - "
            f"{salary_max if salary_max is not None else '?'} "
            f"{job.get('salary_currency') or ''} "
            f"{job.get('salary_period') or ''}"
        ).strip()
    else:
        salary_summary = str(job.get("salary_raw") or "No especificado")
    lines = [
        str(job.get("title") or "Vacante sin titulo"),
        f"Empresa: {job.get('company') or 'No especificada'}",
        "Ubicacion: "
        + ", ".join(
            str(value)
            for value in (
                job.get("city"),
                job.get("region"),
                job.get("country"),
            )
            if value
        ),
        f"Modalidad: {job.get('work_mode') or 'No especificada'}",
        f"Idioma del anuncio: {job.get('posting_language') or 'No determinado'}",
        f"Tipo de empleo: {job.get('employment_type') or 'No especificado'}",
        f"Horario: {job.get('schedule') or 'No especificado'}",
        f"Salario: {salary_summary}",
        "",
        "Revision de datos:",
    ]
    review_reasons = job.get("review_reasons")
    if not isinstance(review_reasons, Sequence) or isinstance(
        review_reasons, (str, bytes)
    ):
        review_reasons = job_review_reasons(
            job,
            str(analysis["analysis_status"]) if analysis is not None else None,
            analysis.get("field_statuses", {}) if analysis is not None else {},
        )
    formatted_reasons = format_job_review_reasons(review_reasons)
    if formatted_reasons:
        lines.extend(f"- {reason}" for reason in formatted_reasons)
    else:
        lines.append("- Sin problemas detectados por las reglas actuales.")
    lines.extend(
        [
            "",
            "Fuentes:",
        ]
    )
    if sources:
        for source in sources:
            lines.append(
                f"- {source.get('site_name') or source.get('site_id')}: "
                f"{source.get('source_url') or 'Enlace no disponible'}"
            )
    else:
        lines.append("- Sin enlaces de fuente registrados.")

    lines.extend(
        [
            "",
            "Estado del analisis:",
        ]
    )
    if analysis is None:
        lines.append("Aun no hay un analisis guardado.")
    else:
        lines.append(
            f"{analysis.get('analysis_status')} "
            f"(extractor {analysis.get('extractor_version')})"
        )
        for category, status in analysis.get("field_statuses", {}).items():
            lines.append(f"- {category}: {status}")
        requirements = analysis.get("requirements", [])
        if requirements:
            lines.extend(["", "Requisitos y evidencia:"])
            for requirement in requirements:
                label = requirement.get("value_text") or requirement.get("value_code")
                qualifier = requirement.get("qualifier")
                if qualifier:
                    label = f"{label} ({qualifier})"
                lines.append(
                    f"- [{requirement.get('requirement_level')}] "
                    f"{requirement.get('category')}: {label}"
                )
                lines.append(f"  Evidencia: {requirement.get('evidence')}")

    lines.extend(["", "Descripcion:", job.get("description_raw") or "Sin descripcion."])
    if job.get("requirements_raw"):
        lines.extend(["", "Requisitos originales:", job["requirements_raw"]])
    return "\n".join(lines)


def format_source_row(site: Mapping[str, Any]) -> tuple[str, str, str, str, str]:
    """Transforma el registro del sitio a columnas visibles en la UI."""
    health = site.get("health_status") or "unknown"
    if site.get("review_required"):
        health = f"{health} (revisar)"
    return (
        str(site.get("site_id") or ""),
        str(site.get("site_name") or ""),
        str(site.get("company_name") or ""),
        "Activo" if site.get("enabled") else "Pausado",
        str(health),
    )


def format_site_run_row(run: Mapping[str, Any]) -> tuple[str, ...]:
    """Convierte un resultado de fuente a valores legibles para su historial."""
    statuses = {
        "running": "En curso",
        "ok": "Correcta",
        "zero_results": "Sin resultados",
        "partial": "Parcial",
        "error": "Error",
        "cancelled": "Cancelada",
    }
    kinds = {
        "workday": "Workday",
        "json_import": "Importacion JSON",
        "import": "Importacion",
    }
    return (
        str(run.get("run_id") or ""),
        str(run.get("started_at") or ""),
        kinds.get(str(run.get("run_kind")), str(run.get("run_kind") or "")),
        statuses.get(str(run.get("site_status")), str(run.get("site_status") or "")),
        str(run.get("jobs_found") or 0),
        str(run.get("jobs_valid") or 0),
        str(run.get("jobs_new") or 0),
        str(run.get("jobs_duplicate") or 0),
        str(run.get("error_message") or ""),
    )


def format_profile_comparison(comparison: Mapping[str, Any]) -> str:
    """Forma una comparacion explicable sin convertir datos ausentes en rechazo."""
    lines = [
        f"Comparacion del perfil: {comparison.get('job_title') or 'Vacante'}",
        f"Estado del analisis: {comparison.get('analysis_status') or 'desconocido'}",
        "",
        "El perfil se registra manualmente. Una coincidencia exige datos compatibles "
        "y exactos; un dato no registrado no significa que la persona no lo tenga.",
        "",
    ]
    if comparison.get("analysis_status") == "not_analyzed":
        lines.append("La vacante aun no tiene un analisis de requisitos.")
        return "\n".join(lines)

    counts = comparison.get("counts") or {}
    labels = {
        "matched": "Coincidencias exactas",
        "not_registered": "No registrados en el perfil",
        "below_requirement": "Por debajo del minimo registrado",
        "needs_review": "Requieren revision manual",
    }
    lines.append("Resumen:")
    lines.extend(
        f"- {label}: {counts.get(key, 0)}" for key, label in labels.items()
    )
    requirements = comparison.get("requirements") or []
    if not requirements:
        lines.extend(["", "El analisis vigente no registro requisitos comparables."])
    else:
        lines.extend(["", "Detalle:"])
        status_labels = {
            "matched": "Coincide exactamente",
            "not_registered": "No registrado en el perfil",
            "below_requirement": "Por debajo del minimo registrado",
            "needs_review": "Requiere revision manual",
        }
        for requirement in requirements:
            status = requirement.get("comparison_status", "needs_review")
            lines.append(
                f"- [{status_labels.get(status, status)}] "
                f"{requirement.get('category')}: {requirement.get('value_text')}"
            )
            if requirement.get("qualifier"):
                lines.append(f"  Nivel solicitado: {requirement['qualifier']}")
            if requirement.get("minimum") is not None:
                lines.append(
                    f"  Minimo solicitado: {requirement['minimum']} "
                    f"{requirement.get('unit') or ''}".rstrip()
                )
            fact = requirement.get("profile_fact")
            if fact:
                lines.append(f"  Perfil: {fact.get('value_text')}")
            lines.append(f"  Evidencia: {requirement.get('evidence')}")
    return "\n".join(lines)


class CandidateProfileWindow:
    """Edicion local y manual de datos estructurados del perfil de candidato."""

    def __init__(self, parent: tk.Tk, repository: Repository) -> None:
        self.repository = repository
        self.window = tk.Toplevel(parent)
        self.window.title("Mi perfil - JobPulse CR 1.5")
        self.window.geometry("960x650")
        self.window.minsize(780, 500)
        self.window.transient(parent)
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        self.category_var = tk.StringVar(value="skill")
        self.code_var = tk.StringVar()
        self.value_var = tk.StringVar()
        self.qualifier_var = tk.StringVar()
        self.education_status_var = tk.StringVar()
        self.numeric_value_var = tk.StringVar()
        self.unit_var = tk.StringVar()
        self._build()
        self.refresh()

    def _build(self) -> None:
        frame = ttk.Frame(self.window, padding=14)
        frame.grid(row=0, column=0, sticky="nsew")
        self.window.rowconfigure(0, weight=1)
        self.window.columnconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)
        ttk.Label(
            frame,
            text=(
                "Registra datos manualmente o importa un CV PDF, DOCX o TXT local. "
                "La extraccion es preliminar y requiere seleccion explicita; "
                "el archivo original no se guarda."
            ),
            wraplength=900,
            justify="left",
        ).grid(row=0, column=0, sticky="w", pady=(0, 10))

        columns = (
            "code",
            "value",
            "qualifier",
            "education",
            "numeric",
            "unit",
            "origin",
            "source",
            "notes",
        )
        self.facts_table = ttk.Treeview(
            frame, columns=columns, show="tree headings", selectmode="browse"
        )
        self.facts_table.heading("#0", text="Categoria")
        self.facts_table.column("#0", width=125, minwidth=90)
        headings = {
            "code": "Codigo",
            "value": "Dato",
            "qualifier": "Nivel",
            "education": "Estudios",
            "numeric": "Cantidad",
            "unit": "Unidad",
            "origin": "Origen",
            "source": "Archivo",
            "notes": "Nota",
        }
        widths = {
            "code": 125,
            "value": 185,
            "qualifier": 110,
            "education": 100,
            "numeric": 80,
            "unit": 80,
            "origin": 90,
            "source": 130,
            "notes": 180,
        }
        for column in columns:
            self.facts_table.heading(column, text=headings[column])
            self.facts_table.column(column, width=widths[column], minwidth=65)
        self.facts_table.grid(row=1, column=0, sticky="nsew")
        self.facts_table.bind("<<TreeviewSelect>>", self._on_select)

        form = ttk.LabelFrame(frame, text="Dato del perfil", padding=10)
        form.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        for column in (1, 3):
            form.columnconfigure(column, weight=1)
        ttk.Label(form, text="Categoria").grid(row=0, column=0, sticky="w")
        ttk.Combobox(
            form,
            textvariable=self.category_var,
            values=REQUIREMENT_CATEGORIES,
            state="readonly",
            width=20,
        ).grid(row=0, column=1, sticky="ew", padx=(6, 14))
        ttk.Label(form, text="Codigo").grid(row=0, column=2, sticky="w")
        ttk.Entry(form, textvariable=self.code_var).grid(
            row=0, column=3, sticky="ew", padx=(6, 0)
        )
        ttk.Label(form, text="Dato / nombre").grid(
            row=1, column=0, sticky="w", pady=(6, 0)
        )
        ttk.Entry(form, textvariable=self.value_var).grid(
            row=1, column=1, sticky="ew", padx=(6, 14), pady=(6, 0)
        )
        ttk.Label(form, text="Nivel").grid(
            row=1, column=2, sticky="w", pady=(6, 0)
        )
        ttk.Entry(form, textvariable=self.qualifier_var).grid(
            row=1, column=3, sticky="ew", padx=(6, 0), pady=(6, 0)
        )
        ttk.Label(form, text="Estado de estudios").grid(
            row=2, column=0, sticky="w", pady=(6, 0)
        )
        ttk.Combobox(
            form,
            textvariable=self.education_status_var,
            values=("", "completed", "pursuing", "unspecified"),
            state="readonly",
            width=20,
        ).grid(row=2, column=1, sticky="ew", padx=(6, 14), pady=(6, 0))
        ttk.Label(form, text="Cantidad / unidad").grid(
            row=2, column=2, sticky="w", pady=(6, 0)
        )
        numeric_frame = ttk.Frame(form)
        numeric_frame.grid(row=2, column=3, sticky="ew", pady=(6, 0))
        ttk.Entry(numeric_frame, textvariable=self.numeric_value_var, width=10).pack(
            side="left"
        )
        ttk.Entry(numeric_frame, textvariable=self.unit_var, width=15).pack(
            side="left", padx=(6, 0), fill="x", expand=True
        )
        ttk.Label(form, text="Nota / contexto").grid(
            row=3, column=0, sticky="w", pady=(6, 0)
        )
        self.notes_entry = ttk.Entry(form)
        self.notes_entry.grid(
            row=3, column=1, columnspan=3, sticky="ew", padx=(6, 0), pady=(6, 0)
        )

        buttons = ttk.Frame(frame)
        buttons.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        ttk.Button(
            buttons,
            text="Revertir ultima importacion",
            command=self.revert_last_import,
        ).pack(side="left", padx=(8, 0))
        ttk.Button(
            buttons, text="Importar CV local", command=self.import_cv
        ).pack(side="left")
        ttk.Button(buttons, text="Nuevo", command=self.clear_form).pack(side="left")
        ttk.Button(buttons, text="Eliminar", command=self.delete_selected).pack(
            side="left", padx=(8, 0)
        )
        ttk.Button(buttons, text="Guardar / actualizar", command=self.save).pack(
            side="right"
        )

    def refresh(self) -> None:
        try:
            facts = self.repository.list_candidate_profile_facts()
        except sqlite3.Error as exc:
            messagebox.showerror(
                "No se pudo cargar el perfil", str(exc), parent=self.window
            )
            return
        for item in self.facts_table.get_children():
            self.facts_table.delete(item)
        for fact in facts:
            self.facts_table.insert(
                "",
                "end",
                iid=str(fact["profile_fact_id"]),
                text=fact["category"],
                values=(
                    fact["value_code"],
                    fact["value_text"],
                    fact.get("qualifier") or "",
                    fact.get("education_status") or "",
                    (
                        fact["numeric_value"]
                        if fact.get("numeric_value") is not None
                        else ""
                    ),
                    fact.get("unit") or "",
                    "Importado" if fact.get("origin") == "imported" else "Manual",
                    fact.get("import_source_name") or "",
                    fact.get("notes") or "",
                ),
            )

    def _on_select(self, event: tk.Event) -> None:
        selected = event.widget.selection()
        if not selected:
            return
        item = self.facts_table.item(selected[0])
        values = item["values"]
        self.category_var.set(item["text"])
        self.code_var.set(values[0])
        self.value_var.set(values[1])
        self.qualifier_var.set(values[2])
        self.education_status_var.set(values[3])
        self.numeric_value_var.set(values[4])
        self.unit_var.set(values[5])
        self.notes_entry.delete(0, "end")
        self.notes_entry.insert(0, values[8])

    def clear_form(self) -> None:
        self.facts_table.selection_remove(self.facts_table.selection())
        self.category_var.set("skill")
        self.code_var.set("")
        self.value_var.set("")
        self.qualifier_var.set("")
        self.education_status_var.set("")
        self.numeric_value_var.set("")
        self.unit_var.set("")
        self.notes_entry.delete(0, "end")

    def save(self) -> None:
        numeric_text = self.numeric_value_var.get().strip()
        try:
            numeric_value = float(numeric_text) if numeric_text else None
            self.repository.save_candidate_profile_fact(
                self.category_var.get(),
                self.code_var.get(),
                self.value_var.get(),
                qualifier=self.qualifier_var.get(),
                education_status=self.education_status_var.get() or None,
                numeric_value=numeric_value,
                unit=self.unit_var.get(),
                notes=self.notes_entry.get(),
            )
        except (ValueError, sqlite3.Error) as exc:
            messagebox.showerror(
                "No se pudo guardar el perfil", str(exc), parent=self.window
            )
            return
        self.refresh()
        self.clear_form()

    def revert_last_import(self) -> None:
        try:
            imports = self.repository.list_candidate_profile_imports(limit=1)
        except (ValueError, sqlite3.Error) as exc:
            messagebox.showerror(
                "No se pudo consultar el historial", str(exc), parent=self.window
            )
            return
        if not imports:
            messagebox.showinfo(
                "Sin importaciones",
                "No hay importaciones de CV para revertir.",
                parent=self.window,
            )
            return
        latest = imports[0]
        if not messagebox.askyesno(
            "Revertir importacion",
            f"¿Revertir la importacion mas reciente de "
            f"{latest['source_name']}?\n\n"
            "Se eliminaran los datos nuevos y se restauraran los valores "
            "anteriores. Los cambios manuales posteriores se conservaran.",
            parent=self.window,
        ):
            return
        try:
            result = self.repository.revert_last_candidate_profile_import()
        except sqlite3.Error as exc:
            messagebox.showerror(
                "No se pudo revertir la importacion",
                str(exc),
                parent=self.window,
            )
            return
        if result is None:
            messagebox.showerror(
                "Importacion no disponible",
                "La importacion ya no existe.",
                parent=self.window,
            )
            return
        self.refresh()
        self.clear_form()
        messagebox.showinfo(
            "Importacion revertida",
            f"Datos nuevos eliminados: {result['removed']}\n"
            f"Datos anteriores restaurados: {result['restored']}\n"
            f"Cambios manuales preservados: {result['preserved_manual']}",
            parent=self.window,
        )

    def delete_selected(self) -> None:
        selected = self.facts_table.selection()
        if not selected:
            messagebox.showinfo(
                "Selecciona un dato",
                "Selecciona un dato del perfil para eliminarlo.",
                parent=self.window,
            )
            return
        if not messagebox.askyesno(
            "Eliminar dato",
            "¿Eliminar el dato seleccionado del perfil?",
            parent=self.window,
        ):
            return
        try:
            self.repository.delete_candidate_profile_fact(int(selected[0]))
        except (ValueError, sqlite3.Error) as exc:
            messagebox.showerror(
                "No se pudo eliminar el dato", str(exc), parent=self.window
            )
            return
        self.refresh()
        self.clear_form()

    def import_cv(self) -> None:
        path = filedialog.askopenfilename(
            parent=self.window,
            title="Seleccionar CV para importar localmente",
            filetypes=(
                ("CV compatibles", "*.pdf *.docx *.txt"),
                ("PDF", "*.pdf"),
                ("Word", "*.docx"),
                ("Texto", "*.txt"),
            ),
        )
        if not path:
            return
        try:
            imported = extract_candidate_profile(path)
        except (OSError, ValueError, sqlite3.Error) as exc:
            messagebox.showerror(
                "No se pudo importar el CV", str(exc), parent=self.window
            )
            return
        CandidateProfileImportWindow(
            self.window,
            self.repository,
            imported,
            on_import_complete=self.refresh,
        )

    def close(self) -> None:
        self.window.destroy()


class CandidateProfileImportWindow:
    """Previsualiza sugerencias locales y solo persiste filas seleccionadas."""

    def __init__(
        self,
        parent: tk.Toplevel,
        repository: Repository,
        imported: CandidateProfileImport,
        *,
        on_import_complete: Any,
    ) -> None:
        self.repository = repository
        self.imported = imported
        self.suggestions = list(imported.suggestions)
        self.on_import_complete = on_import_complete
        self.window = tk.Toplevel(parent)
        self.window.title(f"Revisar importacion - {imported.source_name}")
        self.window.geometry("1080x570")
        self.window.minsize(800, 430)
        self.window.transient(parent)
        frame = ttk.Frame(self.window, padding=12)
        frame.grid(row=0, column=0, sticky="nsew")
        self.window.rowconfigure(0, weight=1)
        self.window.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)
        frame.columnconfigure(0, weight=1)
        warning_text = "\n".join(f"- {warning}" for warning in imported.warnings)
        ttk.Label(
            frame,
            text=warning_text or "Revisa las sugerencias antes de importarlas.",
            justify="left",
            wraplength=1030,
        ).grid(row=0, column=0, sticky="ew", pady=(0, 10))

        columns = (
            "code",
            "value",
            "qualifier",
            "education",
            "numeric",
            "unit",
            "evidence",
        )
        self.suggestions_table = ttk.Treeview(
            frame,
            columns=columns,
            show="tree headings",
            selectmode="extended",
        )
        self.suggestions_table.heading("#0", text="Categoria")
        self.suggestions_table.column("#0", width=100, minwidth=75)
        headers = {
            "code": "Codigo",
            "value": "Dato propuesto",
            "qualifier": "Nivel",
            "education": "Estudios",
            "numeric": "Cantidad",
            "unit": "Unidad",
            "evidence": "Fragmento detectado",
        }
        widths = {
            "code": 105,
            "value": 160,
            "qualifier": 90,
            "education": 90,
            "numeric": 70,
            "unit": 70,
            "evidence": 340,
        }
        for column in columns:
            self.suggestions_table.heading(column, text=headers[column])
            self.suggestions_table.column(
                column, width=widths[column], minwidth=60
            )
        vertical = ttk.Scrollbar(
            frame, orient="vertical", command=self.suggestions_table.yview
        )
        horizontal = ttk.Scrollbar(
            frame, orient="horizontal", command=self.suggestions_table.xview
        )
        self.suggestions_table.configure(
            yscrollcommand=vertical.set, xscrollcommand=horizontal.set
        )
        self.suggestions_table.grid(row=1, column=0, sticky="nsew")
        vertical.grid(row=1, column=1, sticky="ns")
        horizontal.grid(row=2, column=0, sticky="ew")
        for index, suggestion in enumerate(self.suggestions):
            self.suggestions_table.insert(
                "",
                "end",
                iid=str(index),
                text=suggestion.category,
                values=(
                    suggestion.value_code,
                    suggestion.value_text,
                    suggestion.qualifier or "",
                    suggestion.education_status or "",
                    (
                        suggestion.numeric_value
                        if suggestion.numeric_value is not None
                        else ""
                    ),
                    suggestion.unit or "",
                    suggestion.evidence,
                ),
            )

        buttons = ttk.Frame(frame)
        buttons.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        ttk.Button(
            buttons, text="Editar sugerencia", command=self.edit_selected
        ).pack(side="left", padx=(8, 0))
        ttk.Button(
            buttons, text="Seleccionar todo", command=self.select_all
        ).pack(side="left")
        ttk.Button(buttons, text="Cancelar", command=self.close).pack(side="right")
        ttk.Button(
            buttons, text="Importar seleccionados", command=self.import_selected
        ).pack(side="right", padx=(0, 8))

    def select_all(self) -> None:
        self.suggestions_table.selection_set(
            self.suggestions_table.get_children()
        )

    def edit_selected(self) -> None:
        selected = self.suggestions_table.selection()
        if not selected:
            messagebox.showinfo(
                "Selecciona una sugerencia",
                "Selecciona una fila para revisar o corregirla.",
                parent=self.window,
            )
            return
        suggestion_index = int(selected[0])
        suggestion = self.suggestions[suggestion_index]
        dialog = tk.Toplevel(self.window)
        dialog.title("Editar sugerencia del CV")
        dialog.geometry("520x340")
        dialog.resizable(False, False)
        dialog.transient(self.window)
        dialog.grab_set()
        form = ttk.Frame(dialog, padding=14)
        form.grid(row=0, column=0, sticky="nsew")
        form.columnconfigure(1, weight=1)
        variables = {
            "category": tk.StringVar(value=suggestion.category),
            "value_code": tk.StringVar(value=suggestion.value_code),
            "value_text": tk.StringVar(value=suggestion.value_text),
            "qualifier": tk.StringVar(value=suggestion.qualifier or ""),
            "education_status": tk.StringVar(
                value=suggestion.education_status or ""
            ),
            "numeric_value": tk.StringVar(
                value=(
                    str(suggestion.numeric_value)
                    if suggestion.numeric_value is not None
                    else ""
                )
            ),
            "unit": tk.StringVar(value=suggestion.unit or ""),
        }
        fields = (
            ("category", "Categoria"),
            ("value_code", "Codigo"),
            ("value_text", "Dato / nombre"),
            ("qualifier", "Nivel"),
            ("education_status", "Estado de estudios"),
            ("numeric_value", "Cantidad"),
            ("unit", "Unidad"),
        )
        for row, (key, label) in enumerate(fields):
            ttk.Label(form, text=label).grid(
                row=row, column=0, sticky="w", pady=3
            )
            if key == "category":
                field: tk.Widget = ttk.Combobox(
                    form,
                    textvariable=variables[key],
                    values=REQUIREMENT_CATEGORIES,
                    state="readonly",
                )
            elif key == "education_status":
                field = ttk.Combobox(
                    form,
                    textvariable=variables[key],
                    values=("", "completed", "pursuing", "unspecified"),
                    state="readonly",
                )
            else:
                field = ttk.Entry(form, textvariable=variables[key])
            field.grid(row=row, column=1, sticky="ew", padx=(10, 0), pady=3)
        ttk.Label(
            form,
            text="La evidencia detectada no se modifica.",
            wraplength=450,
        ).grid(row=len(fields), column=0, columnspan=2, sticky="w", pady=(8, 0))

        def save_edit() -> None:
            numeric_text = variables["numeric_value"].get().strip()
            try:
                numeric_value = float(numeric_text) if numeric_text else None
                updated = replace(
                    suggestion,
                    category=variables["category"].get(),
                    value_code=variables["value_code"].get().strip(),
                    value_text=variables["value_text"].get().strip(),
                    qualifier=variables["qualifier"].get().strip() or None,
                    education_status=(
                        variables["education_status"].get() or None
                    ),
                    numeric_value=numeric_value,
                    unit=variables["unit"].get().strip() or None,
                )
                if updated.category not in REQUIREMENT_CATEGORIES:
                    raise ValueError("La categoria no es valida.")
                if not updated.value_code or not updated.value_text:
                    raise ValueError("El codigo y el dato son obligatorios.")
                if numeric_value is not None and numeric_value < 0:
                    raise ValueError("La cantidad no puede ser negativa.")
                if updated.education_status and updated.category != "education":
                    raise ValueError(
                        "El estado de estudios solo aplica a education."
                    )
                if numeric_value is not None and updated.category != "experience":
                    raise ValueError(
                        "La cantidad solo aplica a experience."
                    )
                duplicate = any(
                    index != suggestion_index
                    and item.category == updated.category
                    and item.value_code.casefold() == updated.value_code.casefold()
                    for index, item in enumerate(self.suggestions)
                )
                if duplicate:
                    raise ValueError(
                        "Ya existe otra sugerencia con esa categoria y codigo."
                    )
            except ValueError as exc:
                messagebox.showerror(
                    "Sugerencia no valida", str(exc), parent=dialog
                )
                return

            self.suggestions[suggestion_index] = updated
            self.suggestions_table.item(
                str(suggestion_index),
                text=updated.category,
                values=(
                    updated.value_code,
                    updated.value_text,
                    updated.qualifier or "",
                    updated.education_status or "",
                    (
                        updated.numeric_value
                        if updated.numeric_value is not None
                        else ""
                    ),
                    updated.unit or "",
                    updated.evidence,
                ),
            )
            dialog.destroy()

        buttons = ttk.Frame(form)
        buttons.grid(row=len(fields) + 1, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(buttons, text="Cancelar", command=dialog.destroy).pack(
            side="right"
        )
        ttk.Button(buttons, text="Aplicar cambios", command=save_edit).pack(
            side="right", padx=(0, 8)
        )

    def import_selected(self) -> None:
        selected = self.suggestions_table.selection()
        if not selected:
            messagebox.showinfo(
                "Selecciona datos",
                "Selecciona al menos un dato para importar.",
                parent=self.window,
            )
            return
        suggestions = [
            self.suggestions[int(index)] for index in selected
        ]
        try:
            existing = self.repository.list_candidate_profile_facts()
            existing_keys = {
                (
                    str(fact["category"]),
                    str(fact["value_code"]).casefold().strip(),
                )
                for fact in existing
            }
            replacements = [
                f"{item.category}: {item.value_code}"
                for item in suggestions
                if (
                    item.category,
                    item.value_code.casefold().strip(),
                ) in existing_keys
            ]
            if replacements and not messagebox.askyesno(
                "Confirmar actualizacion del perfil",
                "Estos datos ya existen y seran reemplazados:\n\n"
                + "\n".join(replacements)
                + "\n\n¿Continuar?",
                parent=self.window,
            ):
                return
            self.repository.import_candidate_profile_facts(
                self.imported.source_name,
                [
                    {
                        "category": item.category,
                        "value_code": item.value_code,
                        "value_text": item.value_text,
                        "qualifier": item.qualifier,
                        "education_status": item.education_status,
                        "numeric_value": item.numeric_value,
                        "unit": item.unit,
                        "notes": "Importado localmente; revisar contra el CV.",
                    }
                    for item in suggestions
                ],
            )
        except (ValueError, sqlite3.Error) as exc:
            messagebox.showerror(
                "No se pudo guardar el perfil", str(exc), parent=self.window
            )
            return
        self.on_import_complete()
        messagebox.showinfo(
            "Perfil actualizado",
            f"Se importaron {len(suggestions)} dato(s). Revisa el perfil antes "
            "de usarlo en comparaciones.",
            parent=self.window,
        )
        self.close()

    def close(self) -> None:
        self.window.destroy()


class JobPulseWindow:
    """Ventana de busqueda, detalle y revision de analisis."""

    def __init__(self, root: tk.Tk, repository: Repository) -> None:
        self.root = root
        self.repository = repository
        self.selected_job_id: str | None = None
        self.current_results: list[dict[str, Any]] = []
        self.selected_sources: list[dict[str, Any]] = []
        self.source_manager: SourceManagerWindow | None = None
        self.application_window: ApplicationTrackingWindow | None = None
        self.profile_window: CandidateProfileWindow | None = None

        self.root.title("JobPulse CR 1.5")
        self.root.geometry("1180x780")
        self.root.minsize(860, 560)
        self._create_variables()
        self._build_layout()
        self.search_jobs()

    def _create_variables(self) -> None:
        self.text_var = tk.StringVar()
        self.country_var = tk.StringVar()
        self.region_var = tk.StringVar()
        self.city_var = tk.StringVar()
        self.work_mode_var = tk.StringVar(value="Cualquiera")
        self.posting_language_var = tk.StringVar(value="Cualquiera")
        self.employment_type_var = tk.StringVar()
        self.schedule_var = tk.StringVar()
        self.salary_currency_var = tk.StringVar()
        self.salary_period_var = tk.StringVar()
        self.salary_at_least_var = tk.StringVar()
        self.salary_at_most_var = tk.StringVar()
        self.category_var = tk.StringVar(value="Cualquiera")
        self.value_code_var = tk.StringVar()
        self.limit_var = tk.StringVar(value="100")
        self.include_preferred_var = tk.BooleanVar(value=False)
        self.include_manually_skipped_var = tk.BooleanVar(value=False)
        self.needs_review_only_var = tk.BooleanVar(value=False)
        self.review_reason_var = tk.StringVar(value="Cualquiera")
        self.sort_by_var = tk.StringVar(value="Actividad reciente")
        self.published_from_var = tk.StringVar()
        self.published_to_var = tk.StringVar()
        self.profile_compatibility_var = tk.StringVar(value="Cualquiera")
        self.status_var = tk.StringVar(value="Preparando busqueda...")

    def _build_layout(self) -> None:
        outer = ttk.Frame(self.root, padding=16)
        outer.grid(row=0, column=0, sticky="nsew")
        self.root.rowconfigure(0, weight=1)
        self.root.columnconfigure(0, weight=1)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(2, weight=1)

        heading = ttk.Frame(outer)
        heading.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        ttk.Label(
            heading, text="JobPulse CR", font=("Segoe UI", 20, "bold")
        ).pack(anchor="w")
        ttk.Label(
            heading,
            text="Busca y revisa vacantes guardadas en la base aislada v1.5.",
        ).pack(anchor="w")
        ttk.Button(
            heading,
            text="Administrar fuentes",
            command=self.open_source_manager,
        ).pack(side="right", anchor="ne")
        ttk.Button(
            heading,
            text="Mi perfil",
            command=self.open_candidate_profile,
        ).pack(side="right", anchor="ne", padx=(0, 8))
        ttk.Button(
            heading,
            text="Calidad de datos",
            command=self.show_data_quality_summary,
        ).pack(side="right", anchor="ne", padx=(0, 8))
        ttk.Button(
            heading,
            text="Revisar duplicados",
            command=self.show_potential_duplicates,
        ).pack(side="right", anchor="ne", padx=(0, 8))
        ttk.Button(
            heading,
            text="Respaldar DB",
            command=self.backup_database,
        ).pack(side="right", anchor="ne", padx=(0, 8))
        ttk.Button(
            heading,
            text="Configuraci\u00f3n",
            command=self.open_configuration,
        ).pack(side="right", anchor="ne", padx=(0, 8))
        ttk.Button(
            heading,
            text="Scrape",
            command=self.open_scrape_dialog,
        ).pack(side="right", anchor="ne", padx=(0, 8))

        filters = ttk.LabelFrame(outer, text="Filtros", padding=10)
        filters.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        for column in range(8):
            filters.columnconfigure(column, weight=1 if column in {1, 3, 5} else 0)
        self._add_entry(filters, "Texto", self.text_var, 0, 0, width=30)
        self._add_entry(filters, "Pais", self.country_var, 0, 2, width=12)
        self._add_entry(filters, "Region", self.region_var, 0, 4, width=18)
        self._add_entry(filters, "Ciudad", self.city_var, 0, 6, width=18)

        ttk.Label(filters, text="Modalidad").grid(
            row=1, column=0, sticky="w", padx=(4, 6), pady=(8, 0)
        )
        ttk.Combobox(
            filters,
            textvariable=self.work_mode_var,
            values=("Cualquiera", "onsite", "hybrid", "remote", "unknown"),
            state="readonly",
            width=13,
        ).grid(row=1, column=1, sticky="ew", pady=(8, 0))
        ttk.Label(filters, text="Idioma anuncio").grid(
            row=1, column=2, sticky="w", padx=(12, 6), pady=(8, 0)
        )
        ttk.Combobox(
            filters,
            textvariable=self.posting_language_var,
            values=("Cualquiera", "es", "en", "mixed", "undetermined"),
            state="readonly",
            width=13,
        ).grid(row=1, column=3, sticky="ew", pady=(8, 0))
        ttk.Label(filters, text="Categoria").grid(
            row=1, column=4, sticky="w", padx=(12, 6), pady=(8, 0)
        )
        ttk.Combobox(
            filters,
            textvariable=self.category_var,
            values=("Cualquiera", *REQUIREMENT_CATEGORIES),
            state="readonly",
            width=21,
        ).grid(row=1, column=5, sticky="ew", pady=(8, 0))
        ttk.Label(filters, text="Codigo").grid(
            row=1, column=6, sticky="w", padx=(12, 6), pady=(8, 0)
        )
        ttk.Entry(filters, textvariable=self.value_code_var, width=14).grid(
            row=1, column=7, sticky="ew", pady=(8, 0)
        )

        actions = ttk.Frame(filters)
        actions.grid(row=8, column=0, columnspan=8, sticky="ew", pady=(10, 0))
        ttk.Label(actions, text="Max. resultados").pack(side="left")
        ttk.Entry(actions, textvariable=self.limit_var, width=7).pack(
            side="left", padx=(6, 12)
        )
        ttk.Checkbutton(
            actions,
            text="Incluir requisitos preferidos",
            variable=self.include_preferred_var,
        ).pack(side="left")
        ttk.Checkbutton(
            actions,
            text="Incluir vacantes excluidas de busqueda",
            variable=self.include_manually_skipped_var,
        ).pack(side="left", padx=(8, 0))
        ttk.Label(actions, text="Compatibilidad").pack(side="left", padx=(12, 4))
        ttk.Combobox(
            actions,
            textvariable=self.profile_compatibility_var,
            values=tuple(PROFILE_COMPATIBILITY_FILTERS),
            state="readonly",
            width=25,
        ).pack(side="left")
        ttk.Button(actions, text="Buscar", command=self.search_jobs).pack(
            side="right"
        )
        ttk.Button(
            actions, text="Exportar CSV", command=self.export_search_results
        ).pack(side="right", padx=(0, 8))
        ttk.Button(actions, text="Limpiar", command=self.clear_filters).pack(
            side="right", padx=(0, 8)
        )
        ttk.Label(filters, text="Motivo de revision").grid(
            row=3, column=0, sticky="w", padx=(4, 6), pady=(8, 0)
        )
        self.review_reason_combo = ttk.Combobox(
            filters,
            textvariable=self.review_reason_var,
            values=tuple(JOB_REVIEW_REASON_OPTIONS),
            state="readonly",
            width=35,
        )
        self.review_reason_combo.grid(
            row=3, column=1, columnspan=4, sticky="w", pady=(8, 0)
        )
        self.review_reason_combo.bind(
            "<<ComboboxSelected>>", self._on_review_reason_selected
        )
        ttk.Label(filters, text="Orden").grid(
            row=3, column=5, sticky="w", padx=(12, 6), pady=(8, 0)
        )
        ttk.Combobox(
            filters,
            textvariable=self.sort_by_var,
            values=tuple(JOB_SEARCH_SORT_OPTIONS),
            state="readonly",
            width=23,
        ).grid(row=3, column=6, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Checkbutton(
            filters,
            text="Solo mostrar vacantes con motivos de revision",
            variable=self.needs_review_only_var,
            command=self.search_jobs,
        ).grid(row=4, column=0, columnspan=8, sticky="w", pady=(8, 0))
        self._add_entry(
            filters,
            "Tipo de empleo",
            self.employment_type_var,
            2,
            0,
            width=22,
        )
        self._add_entry(
            filters,
            "Horario",
            self.schedule_var,
            2,
            4,
            width=22,
        )
        self._add_entry(
            filters,
            "Publicada desde (AAAA-MM-DD)",
            self.published_from_var,
            5,
            0,
            width=16,
        )
        self._add_entry(
            filters,
            "Publicada hasta (AAAA-MM-DD)",
            self.published_to_var,
            5,
            4,
            width=16,
        )
        self._add_entry(
            filters,
            "Salario desde",
            self.salary_at_least_var,
            6,
            0,
            width=16,
        )
        self._add_entry(
            filters,
            "Salario hasta",
            self.salary_at_most_var,
            6,
            4,
            width=16,
        )
        self._add_entry(
            filters,
            "Moneda",
            self.salary_currency_var,
            7,
            0,
            width=16,
        )
        self._add_entry(
            filters,
            "Periodo salarial",
            self.salary_period_var,
            7,
            4,
            width=16,
        )

        split = ttk.Panedwindow(outer, orient="vertical")
        split.grid(row=2, column=0, sticky="nsew")
        list_frame = ttk.Frame(split)
        detail_frame = ttk.Frame(split)
        split.add(list_frame, weight=3)
        split.add(detail_frame, weight=2)

        columns = (
            "company",
            "location",
            "work_mode",
            "posting_language",
            "date_published",
            "profile_compatibility",
            "data_review",
            "manual_status",
        )
        self.jobs_table = ttk.Treeview(
            list_frame,
            columns=columns,
            show="tree headings",
            selectmode="browse",
        )
        self.jobs_table.heading("#0", text="Vacante")
        self.jobs_table.column("#0", width=320, minwidth=180)
        headings = {
            "company": "Empresa",
            "location": "Ubicacion",
            "work_mode": "Modalidad",
            "posting_language": "Idioma",
            "date_published": "Publicada",
            "profile_compatibility": "Perfil",
            "data_review": "Datos",
            "manual_status": "Estado manual",
        }
        widths = {
            "company": 190,
            "location": 190,
            "work_mode": 100,
            "posting_language": 90,
            "date_published": 105,
            "profile_compatibility": 125,
            "data_review": 90,
            "manual_status": 105,
        }
        for column in columns:
            self.jobs_table.heading(column, text=headings[column])
            self.jobs_table.column(column, width=widths[column], minwidth=80)
        table_scroll = ttk.Scrollbar(
            list_frame, orient="vertical", command=self.jobs_table.yview
        )
        self.jobs_table.configure(yscrollcommand=table_scroll.set)
        self.jobs_table.grid(row=0, column=0, sticky="nsew")
        table_scroll.grid(row=0, column=1, sticky="ns")
        list_frame.rowconfigure(0, weight=1)
        list_frame.columnconfigure(0, weight=1)
        self.jobs_table.bind("<<TreeviewSelect>>", self._on_select_job)

        detail_header = ttk.Frame(detail_frame)
        detail_header.grid(row=0, column=0, sticky="ew", pady=(8, 4))
        ttk.Label(detail_header, text="Detalle de la vacante").pack(side="left")
        ttk.Button(
            detail_header,
            text="Abrir fuente",
            command=self.open_source,
        ).pack(side="right")
        ttk.Button(
            detail_header,
            text="Analizar seleccionada",
            command=self.analyze_selected,
        ).pack(side="right", padx=(0, 8))
        ttk.Button(
            detail_header,
            text="Analizar pendientes",
            command=self.analyze_visible_unanalyzed,
        ).pack(side="right", padx=(0, 8))
        ttk.Button(
            detail_header,
            text="Estado manual",
            command=self.edit_selected_job_state,
        ).pack(side="right", padx=(0, 8))
        ttk.Button(
            detail_header,
            text="Editar datos",
            command=self.edit_selected_job_data,
        ).pack(side="right", padx=(0, 8))
        ttk.Button(
            detail_header,
            text="Seguimiento",
            command=self.open_application_tracking,
        ).pack(side="right", padx=(0, 8))
        ttk.Button(
            detail_header,
            text="Comparar perfil",
            command=self.compare_selected_profile,
        ).pack(side="right", padx=(0, 8))
        self.detail_text = tk.Text(
            detail_frame,
            wrap="word",
            height=12,
            state="disabled",
            font=("Segoe UI", 10),
            padx=8,
            pady=8,
        )
        detail_scroll = ttk.Scrollbar(
            detail_frame, orient="vertical", command=self.detail_text.yview
        )
        self.detail_text.configure(yscrollcommand=detail_scroll.set)
        self.detail_text.grid(row=1, column=0, sticky="nsew")
        detail_scroll.grid(row=1, column=1, sticky="ns")
        detail_frame.columnconfigure(0, weight=1)
        detail_frame.rowconfigure(1, weight=1)

        ttk.Label(outer, textvariable=self.status_var, anchor="w").grid(
            row=3, column=0, sticky="ew", pady=(8, 0)
        )
        self._set_detail("Selecciona una vacante para ver sus datos y analisis.")

    @staticmethod
    def _add_entry(
        parent: ttk.LabelFrame,
        label: str,
        variable: tk.StringVar,
        row: int,
        column: int,
        *,
        width: int,
    ) -> None:
        ttk.Label(parent, text=label).grid(
            row=row, column=column, sticky="w", padx=(4, 6)
        )
        ttk.Entry(parent, textvariable=variable, width=width).grid(
            row=row, column=column + 1, sticky="ew", padx=(0, 8)
        )

    @staticmethod
    def _optional_float(value: str, label: str) -> float | None:
        text = value.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError as exc:
            raise ValueError(f"{label} debe ser numerico.") from exc

    def _on_review_reason_selected(self, _event: tk.Event) -> None:
        if _event.widget is not self.review_reason_combo:
            return
        self.search_jobs()

    def search_jobs(self) -> None:
        try:
            limit = int(self.limit_var.get())
            if limit < 1:
                raise ValueError("El limite debe ser mayor que cero.")
            requirements = []
            category = self.category_var.get()
            value_code = self.value_code_var.get().strip()
            if category != "Cualquiera":
                criterion: dict[str, str] = {"category": category}
                if value_code:
                    criterion["value_code"] = value_code
                requirements.append(criterion)
            filters = {
                "country": self.country_var.get().strip(),
                "region": self.region_var.get().strip(),
                "city": self.city_var.get().strip(),
            }
            if self.work_mode_var.get() != "Cualquiera":
                filters["work_mode"] = self.work_mode_var.get()
            if self.posting_language_var.get() != "Cualquiera":
                filters["posting_language"] = self.posting_language_var.get()
            if self.employment_type_var.get().strip():
                filters["employment_type"] = self.employment_type_var.get().strip()
            if self.schedule_var.get().strip():
                filters["schedule"] = self.schedule_var.get().strip()
            if self.salary_currency_var.get().strip():
                filters["salary_currency"] = self.salary_currency_var.get().strip()
            if self.salary_period_var.get().strip():
                filters["salary_period"] = self.salary_period_var.get().strip()
            levels = ("required", "preferred") if self.include_preferred_var.get() else (
                "required",
            )
            jobs = self.repository.search_jobs(
                text=self.text_var.get(),
                requirements=requirements,
                filters=filters,
                requirement_levels=levels,
                limit=limit,
                include_manually_skipped=self.include_manually_skipped_var.get(),
                needs_review_only=self.needs_review_only_var.get(),
                review_reason=JOB_REVIEW_REASON_OPTIONS[
                    self.review_reason_var.get()
                ],
                sort_by=JOB_SEARCH_SORT_OPTIONS[self.sort_by_var.get()],
                published_from=self.published_from_var.get(),
                published_to=self.published_to_var.get(),
                salary_at_least=self._optional_float(
                    self.salary_at_least_var.get(), "Salario desde"
                ),
                salary_at_most=self._optional_float(
                    self.salary_at_most_var.get(), "Salario hasta"
                ),
            )
            comparisons = self.repository.compare_jobs_to_candidate_profile(jobs)
            compatibility_filter = PROFILE_COMPATIBILITY_FILTERS[
                self.profile_compatibility_var.get()
            ]
            filtered_jobs = [
                job
                for job in jobs
                if compatibility_filter is None
                or self.repository.candidate_profile_status(
                    comparisons[job["job_id"]]
                )
                == compatibility_filter
            ]
        except (ValueError, sqlite3.Error) as exc:
            messagebox.showerror("No se pudo buscar", str(exc), parent=self.root)
            return

        for item in self.jobs_table.get_children():
            self.jobs_table.delete(item)
        self.current_results = filtered_jobs
        for job in filtered_jobs:
            comparison = comparisons[job["job_id"]]
            profile_status = self.repository.candidate_profile_status(comparison)
            job["profile_compatibility"] = PROFILE_COMPATIBILITY_LABELS[profile_status]
            location = ", ".join(
                str(value)
                for value in (job.get("city"), job.get("region"), job.get("country"))
                if value
            )
            self.jobs_table.insert(
                "",
                "end",
                iid=job["job_id"],
                text=job.get("title") or "Sin titulo",
                values=(
                    job.get("company") or "",
                    location,
                    job.get("work_mode") or "unknown",
                    job.get("posting_language") or "undetermined",
                    job.get("date_published") or "",
                    PROFILE_COMPATIBILITY_LABELS[profile_status],
                    format_job_review_summary(job.get("review_reasons", [])),
                    MANUAL_JOB_STATUS_LABELS.get(job.get("user_status"), ""),
                ),
            )
        self.selected_job_id = None
        self.selected_sources = []
        self._set_detail("Selecciona una vacante para ver sus datos y analisis.")
        self.status_var.set(
            f"{len(filtered_jobs)} vacante(s) encontradas "
            f"de {len(jobs)} evaluadas con los filtros actuales."
        )

    def export_search_results(self) -> None:
        if not self.current_results:
            messagebox.showinfo(
                "Sin resultados",
                "Realiza una busqueda con resultados antes de exportar.",
                parent=self.root,
            )
            return
        destination = filedialog.asksaveasfilename(
            parent=self.root,
            title="Exportar resultados de busqueda",
            defaultextension=".csv",
            filetypes=(("Archivo CSV", "*.csv"),),
        )
        if not destination:
            return
        try:
            with open(destination, "w", newline="", encoding="utf-8-sig") as output:
                write_job_search_csv(output, self.current_results)
        except (OSError, csv.Error) as exc:
            messagebox.showerror(
                "No se pudieron exportar los resultados", str(exc), parent=self.root
            )
            return
        self.status_var.set(
            f"Se exportaron {len(self.current_results)} vacante(s) a CSV."
        )

    def show_data_quality_summary(self) -> None:
        try:
            summary = self.repository.get_data_quality_summary()
        except sqlite3.Error as exc:
            messagebox.showerror(
                "No se pudo consultar la calidad de datos",
                str(exc),
                parent=self.root,
            )
            return
        messagebox.showinfo(
            "Calidad de datos v1.5",
            "\n".join(
                (
                    f"Vacantes totales: {summary['total_jobs']}",
                    f"Requieren revision: {summary['jobs_needing_review']}",
                    (
                        "Grupos candidatos a duplicados: "
                        f"{summary['potential_duplicate_groups']}"
                    ),
                    f"Empresa faltante: {summary['missing_company']}",
                    f"Pais faltante: {summary['missing_country']}",
                    f"Ubicacion sin normalizar: {summary['location_not_normalized']}",
                    f"Sin analisis: {summary['analysis_missing']}",
                    f"Analisis pendiente: {summary['analysis_pending']}",
                    f"Analisis parcial: {summary['analysis_partial']}",
                    f"Analisis con error: {summary['analysis_error']}",
                    f"Campos desconocidos: {summary['unknown_fields']}",
                    f"Campos ambiguos: {summary['ambiguous_fields']}",
                )
            ),
            parent=self.root,
        )

    def show_potential_duplicates(self) -> None:
        try:
            report = self.repository.get_potential_duplicate_groups(limit=20)
        except sqlite3.Error as exc:
            messagebox.showerror(
                "No se pudieron consultar posibles duplicados",
                str(exc),
                parent=self.root,
            )
            return
        if not report["groups"]:
            messagebox.showinfo(
                "Posibles duplicados",
                "No se encontraron grupos candidatos con titulo, empresa, pais "
                "y ciudad completos.",
                parent=self.root,
            )
            return

        lines = [
            f"Grupos candidatos: {report['total_groups']}",
            "Verifica cada grupo manualmente; no se fusionan vacantes.",
            "",
        ]
        for group in report["groups"]:
            lines.append(
                f"{group['title']} — {group['company']} "
                f"({group['city']}, {group['country']})"
            )
            lines.append(f"IDs: {', '.join(group['job_ids'])}")
            lines.append("")
        if report["total_groups"] > len(report["groups"]):
            lines.append(
                "Se muestran los primeros 20 grupos. Consulta la CLI para "
                "ver mas resultados."
            )
        messagebox.showinfo(
            "Posibles duplicados",
            "\n".join(lines).rstrip(),
            parent=self.root,
        )

    def backup_database(self) -> None:
        destination = filedialog.asksaveasfilename(
            parent=self.root,
            title="Crear respaldo de la base v1.5",
            defaultextension=".db",
            filetypes=(("Base SQLite", "*.db"), ("Todos los archivos", "*.*")),
        )
        if not destination:
            return
        try:
            backup_path = self.repository.database.backup_to(destination)
        except (OSError, sqlite3.Error, ValueError) as exc:
            messagebox.showerror(
                "No se pudo crear el respaldo", str(exc), parent=self.root
            )
            return
        messagebox.showinfo(
            "Respaldo creado",
            f"La copia consistente se guardo en:\n{backup_path}",
            parent=self.root,
        )

    def clear_filters(self) -> None:
        self.text_var.set("")
        self.country_var.set("")
        self.region_var.set("")
        self.city_var.set("")
        self.work_mode_var.set("Cualquiera")
        self.posting_language_var.set("Cualquiera")
        self.employment_type_var.set("")
        self.schedule_var.set("")
        self.salary_currency_var.set("")
        self.salary_period_var.set("")
        self.salary_at_least_var.set("")
        self.salary_at_most_var.set("")
        self.category_var.set("Cualquiera")
        self.value_code_var.set("")
        self.limit_var.set("100")
        self.include_preferred_var.set(False)
        self.include_manually_skipped_var.set(False)
        self.needs_review_only_var.set(False)
        self.review_reason_var.set("Cualquiera")
        self.sort_by_var.set("Actividad reciente")
        self.published_from_var.set("")
        self.published_to_var.set("")
        self.profile_compatibility_var.set("Cualquiera")
        self.search_jobs()

    def _on_select_job(self, event: tk.Event) -> None:
        selected = event.widget.selection()
        if not selected:
            return
        self._load_job_detail(selected[0])

    def _load_job_detail(self, job_id: str) -> None:
        try:
            job = self.repository.get_job(job_id)
            if job is None:
                self._set_detail("La vacante ya no existe en la base.")
                return
            analysis = self.repository.get_current_analysis(job_id)
            self.selected_sources = self.repository.get_job_sources(job_id)
        except sqlite3.Error as exc:
            messagebox.showerror("No se pudo cargar el detalle", str(exc), parent=self.root)
            return
        self.selected_job_id = job_id
        self._set_detail(format_job_detail(job, analysis, self.selected_sources))

    def analyze_selected(self) -> None:
        if self.selected_job_id is None:
            messagebox.showinfo(
                "Selecciona una vacante",
                "Selecciona una vacante antes de iniciar el analisis.",
                parent=self.root,
            )
            return
        try:
            title = self._analyze_job(self.selected_job_id)
        except (ValueError, sqlite3.Error) as exc:
            messagebox.showerror("No se pudo analizar", str(exc), parent=self.root)
            return
        self._load_job_detail(self.selected_job_id)
        self.status_var.set(f"Analisis actualizado para {title}.")

    def _analyze_job(self, job_id: str) -> str:
        job = self.repository.get_job(job_id)
        if job is None:
            raise ValueError("La vacante ya no existe en la base.")
        text = "\n\n".join(
            value.strip()
            for value in (job.get("description_raw"), job.get("requirements_raw"))
            if isinstance(value, str) and value.strip()
        )
        if not text:
            raise ValueError("La vacante no tiene texto para analizar.")
        result = RuleBasedExtractor().extract(text)
        source = self.repository.get_job_source_context(job_id)
        source_config_hash = (
            hashlib.sha256(source["site_config_json"].encode("utf-8")).hexdigest()
            if source is not None
            else None
        )
        self.repository.save_analysis(
            job_id,
            text_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            extractor_version=EXTRACTOR_VERSION,
            analysis_status=result.analysis_status,
            field_statuses=result.field_statuses,
            requirements=result.requirements,
            source_site_id=source["site_id"] if source is not None else None,
            source_config_hash=source_config_hash,
        )
        return str(job["title"])

    def analyze_visible_unanalyzed(self) -> None:
        pending_jobs = [
            job
            for job in self.current_results
            if "analysis_missing" in job.get("review_reasons", [])
        ]
        if not pending_jobs:
            messagebox.showinfo(
                "Sin analisis pendiente",
                "Los resultados visibles no contienen vacantes sin analisis.",
                parent=self.root,
            )
            return
        if not messagebox.askyesno(
            "Analizar vacantes visibles",
            f"Se analizaran localmente {len(pending_jobs)} vacante(s) visibles "
            "que aun no tienen analisis. No se consultaran fuentes web. ¿Continuar?",
            parent=self.root,
        ):
            return

        previously_selected = self.selected_job_id
        completed = 0
        failures = []
        for job in pending_jobs:
            try:
                self._analyze_job(str(job["job_id"]))
            except (ValueError, sqlite3.Error) as exc:
                failures.append(
                    f"{job.get('title') or job['job_id']}: {exc}"
                )
            else:
                completed += 1

        self.search_jobs()
        if previously_selected and any(
            job["job_id"] == previously_selected for job in self.current_results
        ):
            self.jobs_table.selection_set(previously_selected)
            self._load_job_detail(previously_selected)
        summary = f"Analizadas: {completed}/{len(pending_jobs)}."
        if failures:
            details = "\n".join(f"- {failure}" for failure in failures)
            self.status_var.set(
                f"{summary} {len(failures)} vacante(s) requieren atencion."
            )
            messagebox.showwarning(
                "Analisis completado con errores",
                f"{summary}\nFallidas: {len(failures)}\n{details}",
                parent=self.root,
            )
            return
        self.status_var.set(summary)
        messagebox.showinfo(
            "Analisis completado",
            f"{summary} No se realizaron consultas web.",
            parent=self.root,
        )

    def edit_selected_job_data(self) -> None:
        if self.selected_job_id is None:
            messagebox.showinfo(
                "Selecciona una vacante",
                "Selecciona una vacante para corregir sus datos.",
                parent=self.root,
            )
            return
        try:
            job = self.repository.get_job(self.selected_job_id)
        except sqlite3.Error as exc:
            messagebox.showerror(
                "No se pudieron cargar los datos", str(exc), parent=self.root
            )
            return
        if job is None:
            messagebox.showerror(
                "Vacante no disponible",
                "La vacante ya no existe en la base.",
                parent=self.root,
            )
            return

        dialog = tk.Toplevel(self.root)
        dialog.title("Editar datos normalizados")
        dialog.geometry("500x390")
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.grab_set()
        form = ttk.Frame(dialog, padding=16)
        form.grid(row=0, column=0, sticky="nsew")
        ttk.Label(
            form,
            text=(
                "Las correcciones se conservan al actualizar desde la fuente. "
                "location_raw y el payload original no se modifican."
            ),
            wraplength=460,
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))

        fields = (
            ("title", "Titulo"),
            ("company", "Empresa"),
            ("country", "Pais"),
            ("region", "Provincia/region"),
            ("city", "Ciudad"),
        )
        variables: dict[str, tk.StringVar] = {}
        for row, (field_name, label) in enumerate(fields, start=1):
            variable = tk.StringVar(value=job.get(field_name) or "")
            variables[field_name] = variable
            ttk.Label(form, text=label).grid(
                row=row, column=0, sticky="w", pady=4
            )
            ttk.Entry(form, textvariable=variable, width=42).grid(
                row=row, column=1, sticky="ew", padx=(10, 0), pady=4
            )

        work_modes = {
            "No especificada": None,
            "Presencial": "onsite",
            "Hibrido": "hybrid",
            "Remoto": "remote",
            "Desconocido": "unknown",
        }
        current_work_mode = job.get("work_mode")
        mode_var = tk.StringVar(
            value=next(
                (
                    label
                    for label, value in work_modes.items()
                    if value == current_work_mode
                ),
                "No especificada",
            )
        )
        ttk.Label(form, text="Modalidad").grid(
            row=6, column=0, sticky="w", pady=4
        )
        ttk.Combobox(
            form,
            textvariable=mode_var,
            values=tuple(work_modes),
            state="readonly",
            width=39,
        ).grid(row=6, column=1, sticky="ew", padx=(10, 0), pady=4)

        def refresh_after_edit(message: str) -> None:
            job_id = self.selected_job_id
            dialog.destroy()
            self.search_jobs()
            if job_id and self.jobs_table.exists(job_id):
                self.jobs_table.selection_set(job_id)
                self._load_job_detail(job_id)
            self.status_var.set(message)

        def save_data() -> None:
            overrides: dict[str, str | None] = {
                field_name: variable.get()
                for field_name, variable in variables.items()
            }
            overrides["work_mode"] = work_modes[mode_var.get()]
            try:
                self.repository.update_job_manual_overrides(
                    self.selected_job_id or "", overrides
                )
            except (sqlite3.Error, ValueError) as exc:
                messagebox.showerror(
                    "No se pudieron guardar los datos", str(exc), parent=dialog
                )
                return
            refresh_after_edit("Datos normalizados actualizados manualmente.")

        def restore_source_data() -> None:
            if not messagebox.askyesno(
                "Restaurar datos de fuente",
                "Se eliminaran las correcciones manuales de esta vacante. "
                "¿Deseas restaurar los valores recibidos de la fuente?",
                parent=dialog,
            ):
                return
            try:
                restored = self.repository.clear_job_manual_overrides(
                    self.selected_job_id or ""
                )
            except (sqlite3.Error, ValueError) as exc:
                messagebox.showerror(
                    "No se pudieron restaurar los datos", str(exc), parent=dialog
                )
                return
            refresh_after_edit(
                f"Correcciones manuales eliminadas: {restored} campo(s)."
            )

        buttons = ttk.Frame(form)
        buttons.grid(row=7, column=0, columnspan=2, sticky="ew", pady=(14, 0))
        ttk.Button(
            buttons, text="Restaurar datos de fuente", command=restore_source_data
        ).pack(side="left")
        ttk.Button(buttons, text="Cancelar", command=dialog.destroy).pack(
            side="right"
        )
        ttk.Button(buttons, text="Guardar", command=save_data).pack(
            side="right", padx=(0, 8)
        )

    def edit_selected_job_state(self) -> None:
        if self.selected_job_id is None:
            messagebox.showinfo(
                "Selecciona una vacante",
                "Selecciona una vacante para registrar su estado manual.",
                parent=self.root,
            )
            return
        try:
            job = self.repository.get_job(self.selected_job_id)
            state = self.repository.get_user_job_state(self.selected_job_id)
        except sqlite3.Error as exc:
            messagebox.showerror(
                "No se pudo cargar el estado manual", str(exc), parent=self.root
            )
            return
        if job is None:
            messagebox.showerror(
                "Vacante no disponible",
                "La vacante ya no existe en la base.",
                parent=self.root,
            )
            return

        dialog = tk.Toplevel(self.root)
        dialog.title("Estado manual de la vacante")
        dialog.geometry("480x330")
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.grab_set()
        form = ttk.Frame(dialog, padding=16)
        form.grid(row=0, column=0, sticky="nsew")
        ttk.Label(
            form,
            text=f"{job['title']} - {job.get('company') or 'Empresa no especificada'}",
            wraplength=440,
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))
        status_var = tk.StringVar(
            value=MANUAL_JOB_STATUS_LABELS.get(
                state["user_status"] if state else None, "Sin marcar"
            )
        )
        ttk.Label(form, text="Estado").grid(row=1, column=0, sticky="w")
        status_combo = ttk.Combobox(
            form,
            textvariable=status_var,
            values=tuple(MANUAL_JOB_STATUSES),
            state="readonly",
            width=20,
        )
        status_combo.grid(row=1, column=1, sticky="w", padx=(10, 0), pady=4)
        skip_var = tk.BooleanVar(
            value=bool(state["skip_automatic"]) if state else True
        )
        ttk.Checkbutton(
            form,
            text="Excluir de busquedas normales",
            variable=skip_var,
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(8, 4))
        ttk.Label(form, text="Notas").grid(row=3, column=0, sticky="nw", pady=4)
        notes = tk.Text(form, width=42, height=6, wrap="word")
        notes.grid(row=3, column=1, sticky="ew", padx=(10, 0), pady=4)
        if state and state.get("notes"):
            notes.insert("1.0", state["notes"])

        def save_state() -> None:
            selected_status = MANUAL_JOB_STATUSES[status_var.get()]
            try:
                if selected_status is None:
                    self.repository.clear_user_job_state(self.selected_job_id or "")
                    message = "Estado manual eliminado."
                else:
                    self.repository.set_user_job_state(
                        self.selected_job_id or "",
                        selected_status,
                        skip_automatic=skip_var.get(),
                        notes=notes.get("1.0", "end-1c"),
                    )
                    message = "Estado manual actualizado."
            except (sqlite3.Error, ValueError) as exc:
                messagebox.showerror(
                    "No se pudo guardar el estado", str(exc), parent=dialog
                )
                return
            job_id = self.selected_job_id
            dialog.destroy()
            self.search_jobs()
            if job_id and self.jobs_table.exists(job_id):
                self.jobs_table.selection_set(job_id)
                self._load_job_detail(job_id)
            self.status_var.set(message)

        buttons = ttk.Frame(form)
        buttons.grid(row=4, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(buttons, text="Cancelar", command=dialog.destroy).pack(
            side="right"
        )
        ttk.Button(buttons, text="Guardar", command=save_state).pack(
            side="right", padx=(0, 8)
        )

    def open_application_tracking(self) -> None:
        if self.selected_job_id is None:
            messagebox.showinfo(
                "Selecciona una vacante",
                "Selecciona una vacante para abrir su seguimiento.",
                parent=self.root,
            )
            return
        try:
            job = self.repository.get_job(self.selected_job_id)
        except sqlite3.Error as exc:
            messagebox.showerror(
                "No se pudo cargar la vacante", str(exc), parent=self.root
            )
            return
        if job is None:
            messagebox.showerror(
                "Vacante no disponible",
                "La vacante ya no existe en la base.",
                parent=self.root,
            )
            return
        if (
            self.application_window is not None
            and self.application_window.window.winfo_exists()
        ):
            if self.application_window.job_id == self.selected_job_id:
                self.application_window.window.deiconify()
                self.application_window.window.lift()
                self.application_window.refresh()
                return
            self.application_window.close()
        self.application_window = ApplicationTrackingWindow(
            self.root, self.repository, job
        )

    def open_source(self) -> None:
        if not self.selected_sources:
            messagebox.showinfo(
                "Fuente no disponible",
                "No hay un enlace de fuente registrado para esta vacante.",
                parent=self.root,
            )
            return
        url = self.selected_sources[0].get("source_url")
        if not url:
            messagebox.showinfo(
                "Fuente no disponible",
                "El registro de origen no contiene una URL.",
                parent=self.root,
            )
            return
        if not webbrowser.open(url):
            messagebox.showerror(
                "No se pudo abrir la fuente",
                f"Abre manualmente este enlace:\n{url}",
                parent=self.root,
            )

    def open_candidate_profile(self) -> None:
        if (
            self.profile_window is not None
            and self.profile_window.window.winfo_exists()
        ):
            self.profile_window.window.deiconify()
            self.profile_window.window.lift()
            self.profile_window.refresh()
            return
        self.profile_window = CandidateProfileWindow(self.root, self.repository)

    def compare_selected_profile(self) -> None:
        if self.selected_job_id is None:
            messagebox.showinfo(
                "Selecciona una vacante",
                "Selecciona una vacante para comparar sus requisitos con el perfil.",
                parent=self.root,
            )
            return
        try:
            comparison = self.repository.compare_job_to_candidate_profile(
                self.selected_job_id
            )
        except (ValueError, sqlite3.Error) as exc:
            messagebox.showerror(
                "No se pudo comparar el perfil", str(exc), parent=self.root
            )
            return

        dialog = tk.Toplevel(self.root)
        dialog.title("Comparacion con mi perfil")
        dialog.geometry("760x560")
        dialog.minsize(600, 420)
        dialog.transient(self.root)
        frame = ttk.Frame(dialog, padding=12)
        frame.grid(row=0, column=0, sticky="nsew")
        dialog.rowconfigure(0, weight=1)
        dialog.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        text = tk.Text(frame, wrap="word", state="normal", padx=8, pady=8)
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=scrollbar.set)
        text.insert("1.0", format_profile_comparison(comparison))
        text.configure(state="disabled")
        text.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        ttk.Button(frame, text="Cerrar", command=dialog.destroy).grid(
            row=1, column=0, sticky="e", pady=(8, 0)
        )

    def _set_detail(self, text: str) -> None:
        self.detail_text.configure(state="normal")
        self.detail_text.delete("1.0", "end")
        self.detail_text.insert("1.0", text)
        self.detail_text.configure(state="disabled")

    def open_source_manager(self) -> None:
        if (
            self.source_manager is not None
            and self.source_manager.window.winfo_exists()
        ):
            self.source_manager.window.deiconify()
            self.source_manager.window.lift()
            self.source_manager.refresh()
            return
        self.source_manager = SourceManagerWindow(
            self.root,
            self.repository,
            on_import_complete=self.search_jobs,
        )

    def open_scrape_dialog(self) -> None:
        """Abre el diálogo para ejecutar scrapers desde la UI principal."""
        dialog = tk.Toplevel(self.root)
        dialog.title("Scrape - JobPulse CR 1.5")
        dialog.geometry("500x400")
        dialog.minsize(450, 350)
        dialog.transient(self.root)
        dialog.grab_set()

        frame = ttk.Frame(dialog, padding=15)
        frame.grid(row=0, column=0, sticky="nsew")
        dialog.rowconfigure(0, weight=1)
        dialog.columnconfigure(0, weight=1)
        frame.rowconfigure(2, weight=1)
        frame.columnconfigure(0, weight=1)

        # Título
        ttk.Label(
            frame,
            text="Seleccionar fuente y ejecutar barrido",
            font=("Segoe UI", 12, "bold"),
        ).grid(row=0, column=0, sticky="w", pady=(0, 15))

        # Selección de fuente
        ttk.Label(frame, text="Fuente:").grid(row=1, column=0, sticky="w", pady=5)
        fuente_var = tk.StringVar(value="cinde")
        fuente_combo = ttk.Combobox(
            frame,
            textvariable=fuente_var,
            values=[
                ("CINDE (API)", "cinde"),
                ("Procomer (API)", "procomer"),
                ("ANE (Brave CDP)", "ane"),
                ("Indeed (Brave CDP)", "indeed"),
                ("LinkedIn (Brave CDP)", "linkedin"),
            ],
            state="readonly",
            width=40,
        )
        fuente_combo.grid(row=2, column=0, sticky="ew", pady=5)

        # Páginas
        ttk.Label(frame, text="Páginas:").grid(row=3, column=0, sticky="w", pady=5)
        pages_var = tk.StringVar(value="2")
        pages_spin = ttk.Spinbox(frame, from_=1, to=50, textvariable=pages_var, width=10)
        pages_spin.grid(row=4, column=0, sticky="w", pady=5)

        # Puerto Brave CDP
        ttk.Label(frame, text="Puerto CDP Brave:").grid(row=5, column=0, sticky="w", pady=5)
        port_var = tk.StringVar(value="9222")
        port_spin = ttk.Spinbox(frame, from_=1024, to=65535, textvariable=port_var, width=10)
        port_spin.grid(row=6, column=0, sticky="w", pady=5)

        # Área de resultados
        ttk.Label(frame, text="Resultado:").grid(row=7, column=0, sticky="nw", pady=5)
        resultado_text = tk.Text(frame, height=8, width=50, wrap="word")
        resultado_text.grid(row=8, column=0, sticky="nsew", pady=5)
        scroll = ttk.Scrollbar(frame, orient="vertical", command=resultado_text.yview)
        scroll.grid(row=8, column=1, sticky="ns")
        resultado_text.configure(yscrollcommand=scroll.set)

        # Estado
        status_var = tk.StringVar(value="Listo")
        status_label = ttk.Label(frame, textvariable=status_var, foreground="gray")
        status_label.grid(row=9, column=0, sticky="w", pady=5)

        def _ejecutar():
            fuente = fuente_var.get()
            try:
                pages = int(pages_var.get())
            except ValueError:
                pages = 2
            try:
                port = int(port_var.get())
            except ValueError:
                port = 9222

            resultado_text.delete("1.0", "end")
            resultado_text.insert("end", "Iniciando barrido...\n")
            status_var.set("Ejecutando...")
            dialog.update()

            def _run():
                import asyncio
                from jobpulse_v15.db import Database
                from jobpulse_v15.repository import Repository

                db = Database()
                db.initialize()
                repo = Repository(db)
                service = IngestionService(repo)

                try:
                    if fuente == "cinde":
                        fetcher = CindeFetcher()
                        site = repo.get_site("cinde-cr")
                        if not site:
                            raise ValueError("Sitio cinde-cr no encontrado en la BD")
                        result = service.fetch_and_import(
                            "cinde-cr",
                            lambda: fetcher.fetch(site, pages=pages),
                            run_kind="cinde",
                        )
                    elif fuente == "procomer":
                        fetcher = ProcomerFetcher()
                        site = repo.get_site("procomer-cr")
                        if not site:
                            raise ValueError("Sitio procomer-cr no encontrado en la BD")
                        result = service.fetch_and_import(
                            "procomer-cr",
                            lambda: fetcher.fetch(site, pages=pages),
                            run_kind="procomer",
                        )
                    elif fuente == "ane":
                        fetcher = AneFetcher()
                        site = repo.get_site("ane-cr")
                        if not site:
                            raise ValueError("Sitio ane-cr no encontrado en la BD")
                        result = service.fetch_and_import(
                            "ane-cr",
                            lambda: fetcher.fetch(
                                site, pages=pages, port=port, dias=30
                            ),
                            run_kind="ane",
                        )
                    elif fuente == "indeed":
                        fetcher = IndeedFetcher()
                        site = repo.get_site("indeed-cr")
                        if not site:
                            raise ValueError("Sitio indeed-cr no encontrado en la BD")
                        result = service.fetch_and_import(
                            "indeed-cr",
                            lambda: fetcher.fetch(site, pages=pages, port=port),
                            run_kind="indeed",
                        )
                    elif fuente == "linkedin":
                        fetcher = LinkedInFetcher()
                        site = repo.get_site("linkedin-cr")
                        if not site:
                            raise ValueError("Sitio linkedin-cr no encontrado en la BD")
                        result = service.fetch_and_import(
                            "linkedin-cr",
                            lambda: fetcher.fetch(site, pages=pages, port=port),
                            run_kind="linkedin",
                        )
                    else:
                        raise ValueError("Fuente no reconocida")

                    dialog.after(0, lambda: _mostrar_resultado(result))
                except Exception as e:
                    dialog.after(0, lambda: _mostrar_error(str(e)))

            def _mostrar_resultado(result):
                resultado_text.delete("1.0", "end")
                resultado_text.insert(
                    "end",
                    "Barrido completado:\n"
                    "  Ofertas encontradas: %d\n"
                    "  Ofertas válidas: %d\n"
                    "  Ofertas nuevas: %d\n"
                    "  Ofertas duplicadas: %d\n"
                    "  Ofertas omitidas: %d\n"
                    % (
                        result.get("jobs_found", 0),
                        result.get("jobs_valid", 0),
                        result.get("jobs_new", 0),
                        result.get("jobs_duplicate", 0),
                        result.get("jobs_skipped", 0),
                    )
                )
                status_var.set("Completado")
                messagebox.showinfo(
                    "Barrido completado",
                    "Se importaron %d ofertas nuevas." % result.get("jobs_new", 0),
                    parent=dialog,
                )

            def _mostrar_error(msg):
                resultado_text.delete("1.0", "end")
                resultado_text.insert("end", "Error: %s\n" % msg)
                status_var.set("Error")
                messagebox.showerror("Error en el barrido", msg, parent=dialog)

            # Ejecutar en hilo separado para no bloquear la UI
            hilo = threading.Thread(target=_run, daemon=True)
            hilo.start()

        # Botones
        btn_frame = ttk.Frame(frame)
        btn_frame.grid(row=10, column=0, sticky="ew", pady=15)
        ttk.Button(btn_frame, text="Ejecutar barrido", command=_ejecutar).pack(
            side="left", padx=(0, 8)
        )
        ttk.Button(btn_frame, text="Cerrar", command=dialog.destroy).pack(side="right")

    def open_configuration(self) -> None:
        """Abre la ventana de configuraci\u00f3n global."""
        from jobpulse_v15.configuration_window import ConfigurationWindow
        
        if (
            hasattr(self, '_config_window')
            and self._config_window is not None
            and self._config_window.window.winfo_exists()
        ):
            self._config_window.window.deiconify()
            self._config_window.window.lift()
            return
        self._config_window = ConfigurationWindow(self.root, self.repository)


class SourceManagerWindow:
    """Administra fuentes ATS y scraping con confirmacion explicita."""

    def __init__(
        self,
        parent: tk.Tk,
        repository: Repository,
        *,
        on_import_complete: Any = None,
    ) -> None:
        self.parent = parent
        self.repository = repository
        self.on_import_complete = on_import_complete
        self.window = tk.Toplevel(parent)
        self.window.title("Fuentes de empleo - JobPulse CR 1.5")
        self.window.geometry("900x510")
        self.window.minsize(720, 400)
        self.window.transient(parent)
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        self.scrape_thread: threading.Thread | None = None
        self.scrape_running = False
        self.scrape_results: queue.Queue[
            tuple[bool, dict[str, Any] | str]
        ] = queue.Queue()
        self.poll_after_id: str | None = None
        self.history_window: SourceRunHistoryWindow | None = None
        self.pages_var = tk.StringVar(value="1")
        self.status_var = tk.StringVar(value="")
        self._build()
        self.refresh()

    def _build(self) -> None:
        frame = ttk.Frame(self.window, padding=14)
        frame.grid(row=0, column=0, sticky="nsew")
        self.window.rowconfigure(0, weight=1)
        self.window.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)
        frame.columnconfigure(0, weight=1)
        ttk.Label(
            frame,
            text="Las fuentes solo se consultan al iniciar una accion explicita.",
        ).grid(row=0, column=0, sticky="w", pady=(0, 8))

        table_frame = ttk.Frame(frame)
        table_frame.grid(row=1, column=0, sticky="nsew")
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)
        columns = ("name", "company", "enabled", "health")
        self.sites_table = ttk.Treeview(
            table_frame, columns=columns, show="tree headings", selectmode="extended"
        )
        self.sites_table.heading("#0", text="ID")
        self.sites_table.column("#0", width=130, minwidth=90)
        headers = {
            "name": "Sitio",
            "company": "Empresa",
            "enabled": "Ejecucion",
            "health": "Estado",
        }
        widths = {"name": 230, "company": 190, "enabled": 95, "health": 140}
        for column in columns:
            self.sites_table.heading(column, text=headers[column])
            self.sites_table.column(column, width=widths[column], minwidth=75)
        scrollbar = ttk.Scrollbar(
            table_frame, orient="vertical", command=self.sites_table.yview
        )
        self.sites_table.configure(yscrollcommand=scrollbar.set)
        self.sites_table.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")

        actions = ttk.Frame(frame)
        actions.grid(row=2, column=0, sticky="ew", pady=(10, 4))
        self.add_button = ttk.Button(
            actions, text="Agregar Workday", command=self.open_add_workday
        )
        self.add_button.pack(side="left")
        self.add_greenhouse_button = ttk.Button(
            actions, text="Agregar Greenhouse", command=self.open_add_greenhouse
        )
        self.add_greenhouse_button.pack(side="left", padx=(8, 0))
        self.add_ashby_button = ttk.Button(
            actions, text="Agregar Ashby", command=self.open_add_ashby
        )
        self.add_ashby_button.pack(side="left", padx=(8, 0))
        self.add_lever_button = ttk.Button(
            actions, text="Agregar Lever", command=self.open_add_lever
        )
        self.add_lever_button.pack(side="left", padx=(8, 0))
        self.add_recruitee_button = ttk.Button(
            actions, text="Agregar Recruitee", command=self.open_add_recruitee
        )
        self.add_recruitee_button.pack(side="left", padx=(8, 0))
        self.add_workable_button = ttk.Button(
            actions, text="Agregar Workable", command=self.open_add_workable
        )
        self.add_workable_button.pack(side="left", padx=(8, 0))
        self.add_successfactors_button = ttk.Button(
            actions,
            text="Agregar SuccessFactors",
            command=self.open_add_successfactors,
        )
        self.add_successfactors_button.pack(side="left", padx=(8, 0))
        self.toggle_button = ttk.Button(
            actions, text="Activar / pausar", command=self.toggle_selected
        )
        self.toggle_button.pack(side="left", padx=(8, 0))
        self.scrape_button = ttk.Button(
            actions, text="Consultar seleccionada", command=self.scrape_selected
        )
        self.scrape_button.pack(side="left", padx=(8, 0))
        self.scrape_group_button = ttk.Button(
            actions,
            text="Consultar seleccionadas",
            command=self.scrape_selected_group,
        )
        self.scrape_group_button.pack(side="left", padx=(8, 0))
        self.history_button = ttk.Button(
            actions, text="Historial", command=self.open_run_history
        )
        self.history_button.pack(side="left", padx=(8, 0))
        self.review_button = ttk.Button(
            actions, text="Marcar revisada", command=self.mark_selected_reviewed
        )
        self.review_button.pack(side="left", padx=(8, 0))
        ttk.Label(actions, text="Paginas Workday (max. 10)").pack(
            side="left", padx=(16, 6)
        )
        ttk.Spinbox(
            actions, from_=1, to=MAX_PAGES, textvariable=self.pages_var, width=4
        ).pack(side="left")
        ttk.Button(actions, text="Actualizar", command=self.refresh).pack(
            side="right"
        )
        ttk.Label(frame, textvariable=self.status_var, anchor="w").grid(
            row=3, column=0, sticky="ew", pady=(4, 0)
        )
        self.sites_table.bind("<<TreeviewSelect>>", self._on_select_site)
        self._set_action_state("disabled")

    def refresh(self) -> None:
        try:
            sites = self.repository.list_sites()
        except sqlite3.Error as exc:
            messagebox.showerror(
                "No se pudieron cargar las fuentes",
                str(exc),
                parent=self.window,
            )
            return
        for item in self.sites_table.get_children():
            self.sites_table.delete(item)
        for site in sites:
            site_id, name, company, enabled, health = format_source_row(site)
            self.sites_table.insert(
                "",
                "end",
                iid=site_id,
                text=site_id,
                values=(name, company, enabled, health),
            )
        self.status_var.set(f"{len(sites)} fuente(s) registradas.")

    def _on_select_site(self, event: tk.Event) -> None:
        if event.widget is self.sites_table:
            self._update_action_state()

    def _selected_site(self) -> dict[str, Any] | None:
        selection = self.sites_table.selection()
        if not selection:
            return None
        try:
            return self.repository.get_site(selection[0])
        except sqlite3.Error as exc:
            messagebox.showerror(
                "No se pudo consultar el sitio", str(exc), parent=self.window
            )
            return None

    def _selected_sites(self) -> list[dict[str, Any]] | None:
        try:
            sites = [
                self.repository.get_site(site_id)
                for site_id in self.sites_table.selection()
            ]
        except sqlite3.Error as exc:
            messagebox.showerror(
                "No se pudieron consultar las fuentes",
                str(exc),
                parent=self.window,
            )
            return None
        if any(site is None for site in sites):
            messagebox.showerror(
                "Fuente no disponible",
                "Actualiza la lista e intenta de nuevo.",
                parent=self.window,
            )
            return None
        return [site for site in sites if site is not None]

    def _update_action_state(self) -> None:
        sites = self._selected_sites()
        single_selection = sites is not None and len(sites) == 1
        site = sites[0] if single_selection else None
        state = "normal" if site is not None and not self.scrape_running else "disabled"
        self.toggle_button.configure(state=state)
        selected_provider = (
            self._site_provider(sites[0]) if sites else None
        )
        same_active_provider = bool(sites) and selected_provider is not None and all(
            self._site_provider(source) == selected_provider and source["enabled"]
            for source in (sites or [])
        )
        scrape_enabled = bool(
            single_selection
            and same_active_provider
            and not self.scrape_running
        )
        self.scrape_button.configure(state="normal" if scrape_enabled else "disabled")
        self.scrape_group_button.configure(
            state=(
                "normal"
                if same_active_provider and not self.scrape_running
                else "disabled"
            )
        )
        self.history_button.configure(state=state)
        needs_review = bool(site is not None and site["review_required"])
        self.review_button.configure(
            state="normal" if needs_review and not self.scrape_running else "disabled"
        )

    def _set_action_state(self, state: str) -> None:
        self.add_button.configure(state=state)
        self.add_greenhouse_button.configure(state=state)
        self.add_ashby_button.configure(state=state)
        self.add_lever_button.configure(state=state)
        self.add_recruitee_button.configure(state=state)
        self.add_workable_button.configure(state=state)
        self.add_successfactors_button.configure(state=state)
        self.toggle_button.configure(state=state)
        self.scrape_button.configure(state=state)
        self.scrape_group_button.configure(state=state)
        self.history_button.configure(state=state)
        self.review_button.configure(state=state)

    @staticmethod
    def _site_provider(site: Mapping[str, Any]) -> str | None:
        provider = str(site.get("platform_id") or "").casefold()
        adapter = str(site.get("site_config", {}).get("adapter") or "").casefold()
        if provider == "workday" or adapter == "workday":
            return "workday"
        if provider == "greenhouse" or adapter == "greenhouse":
            return "greenhouse"
        if provider == "ashby" or adapter == "ashby":
            return "ashby"
        if provider == "lever" or adapter == "lever":
            return "lever"
        if provider == "recruitee" or adapter == "recruitee":
            return "recruitee"
        if provider == "workable" or adapter == "workable":
            return "workable"
        if provider == "successfactors" or adapter == "successfactors":
            return "successfactors"
        return None

    def open_run_history(self) -> None:
        site = self._selected_site()
        if site is None:
            return
        if (
            self.history_window is not None
            and self.history_window.window.winfo_exists()
        ):
            self.history_window.window.deiconify()
            self.history_window.window.lift()
            self.history_window.refresh()
            return
        self.history_window = SourceRunHistoryWindow(self.window, self.repository, site)

    def mark_selected_reviewed(self) -> None:
        site = self._selected_site()
        if site is None or not site["review_required"]:
            return
        try:
            if not self.repository.mark_site_reviewed(site["site_id"]):
                raise ValueError(f"No existe el sitio {site['site_id']!r}.")
        except (sqlite3.Error, ValueError) as exc:
            messagebox.showerror(
                "No se pudo marcar la fuente como revisada",
                str(exc),
                parent=self.window,
            )
            return
        self.refresh()
        self.sites_table.selection_set(site["site_id"])
        self._update_action_state()
        self.status_var.set(f"Fuente {site['site_name']} marcada como revisada.")

    def toggle_selected(self) -> None:
        site = self._selected_site()
        if site is None:
            return
        try:
            if not self.repository.set_site_enabled(site["site_id"], not site["enabled"]):
                raise ValueError(f"No existe el sitio {site['site_id']!r}.")
        except (sqlite3.Error, ValueError) as exc:
            messagebox.showerror(
                "No se pudo cambiar el estado", str(exc), parent=self.window
            )
            return
        self.refresh()
        self.sites_table.selection_set(site["site_id"])
        self._update_action_state()

    def open_add_workday(self) -> None:
        dialog = tk.Toplevel(self.window)
        dialog.title("Agregar tablero Workday")
        dialog.geometry("570x390")
        dialog.resizable(False, False)
        dialog.transient(self.window)
        dialog.grab_set()

        values = {
            "site_id": tk.StringVar(),
            "site_name": tk.StringVar(),
            "company": tk.StringVar(),
            "url": tk.StringVar(),
            "country": tk.StringVar(value="CR"),
            "country_query": tk.StringVar(value="Costa Rica"),
            "search_text": tk.StringVar(value="Costa Rica"),
        }
        form = ttk.Frame(dialog, padding=16)
        form.grid(row=0, column=0, sticky="nsew")
        for row, (key, label) in enumerate(
            (
                ("site_id", "ID unico (ej. acme-cr)"),
                ("site_name", "Nombre del tablero"),
                ("company", "Empresa"),
                ("url", "URL del tablero Workday"),
                ("country", "Codigo de pais (ej. CR)"),
                ("country_query", "Texto de ubicacion para validar el pais"),
                ("search_text", "Texto de busqueda Workday"),
            )
        ):
            ttk.Label(form, text=label).grid(row=row, column=0, sticky="w", pady=4)
            ttk.Entry(form, textvariable=values[key], width=52).grid(
                row=row, column=1, sticky="ew", padx=(10, 0), pady=4
            )
        form.columnconfigure(1, weight=1)

        def save_site() -> None:
            site_id = values["site_id"].get().strip().lower()
            if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", site_id):
                messagebox.showerror(
                    "ID invalido",
                    "Usa letras minusculas, numeros, guion o guion bajo.",
                    parent=dialog,
                )
                return
            site_name = values["site_name"].get().strip()
            company = values["company"].get().strip()
            base_url = values["url"].get().strip()
            search_text = values["search_text"].get().strip()
            country_query = values["country_query"].get().strip()
            country = values["country"].get().strip().upper()
            if not site_name or not company or not search_text or not country_query:
                messagebox.showerror(
                    "Faltan datos",
                    "Nombre, empresa y textos de busqueda/ubicacion son obligatorios.",
                    parent=dialog,
                )
                return
            try:
                WorkdayFetcher._parse_board_url(base_url)
                if self.repository.get_site(site_id) is not None:
                    raise ValueError(f"Ya existe un sitio con ID {site_id!r}.")
                if self.repository.get_platform("workday") is None:
                    self.repository.register_platform(
                        "workday",
                        "Workday",
                        "api",
                        default_language="en",
                    )
                self.repository.register_site(
                    site_id,
                    "workday",
                    site_name,
                    base_url,
                    company_name=company,
                    country=country,
                    run_groups=("directos",),
                    site_config={
                        "adapter": "workday",
                        "search_text": search_text,
                        "country_query": country_query,
                    },
                    enabled=True,
                )
            except (sqlite3.Error, ValueError) as exc:
                messagebox.showerror(
                    "No se pudo agregar el tablero", str(exc), parent=dialog
                )
                return
            dialog.destroy()
            self.refresh()
            self.sites_table.selection_set(site_id)
            self._update_action_state()

        buttons = ttk.Frame(form)
        buttons.grid(row=6, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(buttons, text="Cancelar", command=dialog.destroy).pack(
            side="right"
        )
        ttk.Button(buttons, text="Guardar", command=save_site).pack(
            side="right", padx=(0, 8)
        )
        def submit_on_return(event: tk.Event) -> str:
            if event.keysym == "Return":
                save_site()
            return "break"

        def dismiss_on_escape(event: tk.Event) -> str:
            if event.keysym == "Escape":
                dialog.destroy()
            return "break"

        dialog.bind("<Return>", submit_on_return)
        dialog.bind("<Escape>", dismiss_on_escape)

    def open_add_greenhouse(self) -> None:
        dialog = tk.Toplevel(self.window)
        dialog.title("Agregar tablero Greenhouse")
        dialog.geometry("570x340")
        dialog.resizable(False, False)
        dialog.transient(self.window)
        dialog.grab_set()

        values = {
            "site_id": tk.StringVar(),
            "site_name": tk.StringVar(),
            "company": tk.StringVar(),
            "url": tk.StringVar(),
            "country": tk.StringVar(value="CR"),
            "country_query": tk.StringVar(value="Costa Rica"),
        }
        form = ttk.Frame(dialog, padding=16)
        form.grid(row=0, column=0, sticky="nsew")
        for row, (key, label) in enumerate(
            (
                ("site_id", "ID unico (ej. acme-cr)"),
                ("site_name", "Nombre del tablero"),
                ("company", "Empresa"),
                ("url", "URL del tablero Greenhouse"),
                ("country", "Codigo de pais (ej. CR)"),
                ("country_query", "Texto para validar la ubicacion"),
            )
        ):
            ttk.Label(form, text=label).grid(row=row, column=0, sticky="w", pady=4)
            ttk.Entry(form, textvariable=values[key], width=52).grid(
                row=row, column=1, sticky="ew", padx=(10, 0), pady=4
            )
        form.columnconfigure(1, weight=1)

        def save_site() -> None:
            site_id = values["site_id"].get().strip().lower()
            if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", site_id):
                messagebox.showerror(
                    "ID invalido",
                    "Usa letras minusculas, numeros, guion o guion bajo.",
                    parent=dialog,
                )
                return
            site_name = values["site_name"].get().strip()
            company = values["company"].get().strip()
            base_url = values["url"].get().strip()
            country_query = values["country_query"].get().strip()
            country = values["country"].get().strip().upper()
            if not site_name or not company or not country_query or not country:
                messagebox.showerror(
                    "Faltan datos",
                    "Nombre, empresa, pais y texto de ubicacion son obligatorios.",
                    parent=dialog,
                )
                return
            try:
                GreenhouseFetcher._board_token(base_url)
                if self.repository.get_site(site_id) is not None:
                    raise ValueError(f"Ya existe un sitio con ID {site_id!r}.")
                if self.repository.get_platform("greenhouse") is None:
                    self.repository.register_platform(
                        "greenhouse", "Greenhouse", "api"
                    )
                self.repository.register_site(
                    site_id,
                    "greenhouse",
                    site_name,
                    base_url,
                    company_name=company,
                    country=country,
                    run_groups=("directos",),
                    site_config={
                        "adapter": "greenhouse",
                        "country_query": country_query,
                    },
                    enabled=True,
                )
            except (sqlite3.Error, ValueError) as exc:
                messagebox.showerror(
                    "No se pudo agregar el tablero", str(exc), parent=dialog
                )
                return
            dialog.destroy()
            self.refresh()
            self.sites_table.selection_set(site_id)
            self._update_action_state()

        buttons = ttk.Frame(form)
        buttons.grid(row=6, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(buttons, text="Cancelar", command=dialog.destroy).pack(
            side="right"
        )
        ttk.Button(buttons, text="Guardar", command=save_site).pack(
            side="right", padx=(0, 8)
        )

        def submit_on_return(event: tk.Event) -> str:
            if event.keysym == "Return":
                save_site()
            return "break"

        def dismiss_on_escape(event: tk.Event) -> str:
            if event.keysym == "Escape":
                dialog.destroy()
            return "break"

        dialog.bind("<Return>", submit_on_return)
        dialog.bind("<Escape>", dismiss_on_escape)

    def open_add_ashby(self) -> None:
        dialog = tk.Toplevel(self.window)
        dialog.title("Agregar tablero Ashby")
        dialog.geometry("570x340")
        dialog.resizable(False, False)
        dialog.transient(self.window)
        dialog.grab_set()

        values = {
            "site_id": tk.StringVar(),
            "site_name": tk.StringVar(),
            "company": tk.StringVar(),
            "url": tk.StringVar(),
            "country": tk.StringVar(value="CR"),
            "country_query": tk.StringVar(value="Costa Rica"),
        }
        form = ttk.Frame(dialog, padding=16)
        form.grid(row=0, column=0, sticky="nsew")
        for row, (key, label) in enumerate(
            (
                ("site_id", "ID unico (ej. acme-cr)"),
                ("site_name", "Nombre del tablero"),
                ("company", "Empresa"),
                ("url", "URL del tablero Ashby"),
                ("country", "Codigo de pais (ej. CR)"),
                ("country_query", "Texto para validar la ubicacion"),
            )
        ):
            ttk.Label(form, text=label).grid(row=row, column=0, sticky="w", pady=4)
            ttk.Entry(form, textvariable=values[key], width=52).grid(
                row=row, column=1, sticky="ew", padx=(10, 0), pady=4
            )
        form.columnconfigure(1, weight=1)

        def save_site() -> None:
            site_id = values["site_id"].get().strip().lower()
            if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", site_id):
                messagebox.showerror(
                    "ID invalido",
                    "Usa letras minusculas, numeros, guion o guion bajo.",
                    parent=dialog,
                )
                return
            site_name = values["site_name"].get().strip()
            company = values["company"].get().strip()
            base_url = values["url"].get().strip()
            country_query = values["country_query"].get().strip()
            country = values["country"].get().strip().upper()
            if not site_name or not company or not country_query or not country:
                messagebox.showerror(
                    "Faltan datos",
                    "Nombre, empresa, pais y texto de ubicacion son obligatorios.",
                    parent=dialog,
                )
                return
            try:
                AshbyFetcher._board_token(base_url)
                if self.repository.get_site(site_id) is not None:
                    raise ValueError(f"Ya existe un sitio con ID {site_id!r}.")
                if self.repository.get_platform("ashby") is None:
                    self.repository.register_platform("ashby", "Ashby", "api")
                self.repository.register_site(
                    site_id,
                    "ashby",
                    site_name,
                    base_url,
                    company_name=company,
                    country=country,
                    run_groups=("directos",),
                    site_config={
                        "adapter": "ashby",
                        "country_query": country_query,
                    },
                    enabled=True,
                )
            except (sqlite3.Error, ValueError) as exc:
                messagebox.showerror(
                    "No se pudo agregar el tablero", str(exc), parent=dialog
                )
                return
            dialog.destroy()
            self.refresh()
            self.sites_table.selection_set(site_id)
            self._update_action_state()

        buttons = ttk.Frame(form)
        buttons.grid(row=6, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(buttons, text="Cancelar", command=dialog.destroy).pack(
            side="right"
        )
        ttk.Button(buttons, text="Guardar", command=save_site).pack(
            side="right", padx=(0, 8)
        )

        def submit_on_return(event: tk.Event) -> str:
            if event.keysym == "Return":
                save_site()
            return "break"

        def dismiss_on_escape(event: tk.Event) -> str:
            _ = event
            dialog.destroy()
            return "break"

        dialog.bind("<Return>", submit_on_return)
        dialog.bind("<Escape>", dismiss_on_escape)

    def open_add_lever(self) -> None:
        dialog = tk.Toplevel(self.window)
        dialog.title("Agregar tablero Lever")
        dialog.geometry("570x340")
        dialog.resizable(False, False)
        dialog.transient(self.window)
        dialog.grab_set()

        values = {
            "site_id": tk.StringVar(),
            "site_name": tk.StringVar(),
            "company": tk.StringVar(),
            "url": tk.StringVar(),
            "country": tk.StringVar(value="CR"),
            "country_query": tk.StringVar(value="Costa Rica"),
        }
        form = ttk.Frame(dialog, padding=16)
        form.grid(row=0, column=0, sticky="nsew")
        for row, (key, label) in enumerate(
            (
                ("site_id", "ID unico (ej. acme-cr)"),
                ("site_name", "Nombre del tablero"),
                ("company", "Empresa"),
                ("url", "URL del tablero Lever"),
                ("country", "Codigo de pais (ej. CR)"),
                ("country_query", "Texto para validar la ubicacion"),
            )
        ):
            ttk.Label(form, text=label).grid(row=row, column=0, sticky="w", pady=4)
            ttk.Entry(form, textvariable=values[key], width=52).grid(
                row=row, column=1, sticky="ew", padx=(10, 0), pady=4
            )
        form.columnconfigure(1, weight=1)

        def save_site() -> None:
            site_id = values["site_id"].get().strip().lower()
            if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", site_id):
                messagebox.showerror(
                    "ID invalido",
                    "Usa letras minusculas, numeros, guion o guion bajo.",
                    parent=dialog,
                )
                return
            site_name = values["site_name"].get().strip()
            company = values["company"].get().strip()
            base_url = values["url"].get().strip()
            country_query = values["country_query"].get().strip()
            country = values["country"].get().strip().upper()
            if not site_name or not company or not country_query or not country:
                messagebox.showerror(
                    "Faltan datos",
                    "Nombre, empresa, pais y texto de ubicacion son obligatorios.",
                    parent=dialog,
                )
                return
            try:
                LeverFetcher._board_token(base_url)
                if self.repository.get_site(site_id) is not None:
                    raise ValueError(f"Ya existe un sitio con ID {site_id!r}.")
                if self.repository.get_platform("lever") is None:
                    self.repository.register_platform("lever", "Lever", "api")
                self.repository.register_site(
                    site_id,
                    "lever",
                    site_name,
                    base_url,
                    company_name=company,
                    country=country,
                    run_groups=("directos",),
                    site_config={
                        "adapter": "lever",
                        "country_query": country_query,
                    },
                    enabled=True,
                )
            except (sqlite3.Error, ValueError) as exc:
                messagebox.showerror(
                    "No se pudo agregar el tablero", str(exc), parent=dialog
                )
                return
            dialog.destroy()
            self.refresh()
            self.sites_table.selection_set(site_id)
            self._update_action_state()

        buttons = ttk.Frame(form)
        buttons.grid(row=6, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(buttons, text="Cancelar", command=dialog.destroy).pack(
            side="right"
        )
        ttk.Button(buttons, text="Guardar", command=save_site).pack(
            side="right", padx=(0, 8)
        )

        def submit_on_return(event: tk.Event) -> str:
            if event.keysym == "Return":
                save_site()
            return "break"

        def dismiss_on_escape(event: tk.Event) -> str:
            _ = event
            dialog.destroy()
            return "break"

        dialog.bind("<Return>", submit_on_return)
        dialog.bind("<Escape>", dismiss_on_escape)

    def open_add_recruitee(self) -> None:
        dialog = tk.Toplevel(self.window)
        dialog.title("Agregar tablero Recruitee")
        dialog.geometry("570x340")
        dialog.resizable(False, False)
        dialog.transient(self.window)
        dialog.grab_set()

        values = {
            "site_id": tk.StringVar(),
            "site_name": tk.StringVar(),
            "company": tk.StringVar(),
            "url": tk.StringVar(),
            "country": tk.StringVar(value="CR"),
            "country_query": tk.StringVar(value="Costa Rica"),
        }
        form = ttk.Frame(dialog, padding=16)
        form.grid(row=0, column=0, sticky="nsew")
        for row, (key, label) in enumerate(
            (
                ("site_id", "ID unico (ej. acme-cr)"),
                ("site_name", "Nombre del tablero"),
                ("company", "Empresa"),
                ("url", "URL del tablero Recruitee"),
                ("country", "Codigo de pais (ej. CR)"),
                ("country_query", "Texto para validar la ubicacion"),
            )
        ):
            ttk.Label(form, text=label).grid(row=row, column=0, sticky="w", pady=4)
            ttk.Entry(form, textvariable=values[key], width=52).grid(
                row=row, column=1, sticky="ew", padx=(10, 0), pady=4
            )
        form.columnconfigure(1, weight=1)

        def save_site() -> None:
            site_id = values["site_id"].get().strip().lower()
            if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", site_id):
                messagebox.showerror(
                    "ID invalido",
                    "Usa letras minusculas, numeros, guion o guion bajo.",
                    parent=dialog,
                )
                return
            site_name = values["site_name"].get().strip()
            company = values["company"].get().strip()
            base_url = values["url"].get().strip()
            country_query = values["country_query"].get().strip()
            country = values["country"].get().strip().upper()
            if not site_name or not company or not country_query or not country:
                messagebox.showerror(
                    "Faltan datos",
                    "Nombre, empresa, pais y texto de ubicacion son obligatorios.",
                    parent=dialog,
                )
                return
            try:
                RecruiteeFetcher._board_token(base_url)
                if self.repository.get_site(site_id) is not None:
                    raise ValueError(f"Ya existe un sitio con ID {site_id!r}.")
                if self.repository.get_platform("recruitee") is None:
                    self.repository.register_platform("recruitee", "Recruitee", "api")
                self.repository.register_site(
                    site_id,
                    "recruitee",
                    site_name,
                    base_url,
                    company_name=company,
                    country=country,
                    run_groups=("directos",),
                    site_config={
                        "adapter": "recruitee",
                        "country_query": country_query,
                    },
                    enabled=True,
                )
            except (sqlite3.Error, ValueError) as exc:
                messagebox.showerror(
                    "No se pudo agregar el tablero", str(exc), parent=dialog
                )
                return
            dialog.destroy()
            self.refresh()
            self.sites_table.selection_set(site_id)
            self._update_action_state()

        buttons = ttk.Frame(form)
        buttons.grid(row=6, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(buttons, text="Cancelar", command=dialog.destroy).pack(
            side="right"
        )
        ttk.Button(buttons, text="Guardar", command=save_site).pack(
            side="right", padx=(0, 8)
        )

        def submit_on_return(event: tk.Event) -> str:
            if event.keysym == "Return":
                save_site()
            return "break"

        def dismiss_on_escape(event: tk.Event) -> str:
            _ = event
            dialog.destroy()
            return "break"

        dialog.bind("<Return>", submit_on_return)
        dialog.bind("<Escape>", dismiss_on_escape)

    def open_add_workable(self) -> None:
        dialog = tk.Toplevel(self.window)
        dialog.title("Agregar tablero Workable")
        dialog.geometry("570x340")
        dialog.resizable(False, False)
        dialog.transient(self.window)
        dialog.grab_set()

        values = {
            "site_id": tk.StringVar(),
            "site_name": tk.StringVar(),
            "company": tk.StringVar(),
            "url": tk.StringVar(),
            "country": tk.StringVar(value="CR"),
            "country_query": tk.StringVar(value="Costa Rica"),
        }
        form = ttk.Frame(dialog, padding=16)
        form.grid(row=0, column=0, sticky="nsew")
        for row, (key, label) in enumerate(
            (
                ("site_id", "ID unico (ej. acme-cr)"),
                ("site_name", "Nombre del tablero"),
                ("company", "Empresa"),
                ("url", "URL del tablero Workable"),
                ("country", "Codigo de pais (ej. CR)"),
                ("country_query", "Texto para validar la ubicacion"),
            )
        ):
            ttk.Label(form, text=label).grid(row=row, column=0, sticky="w", pady=4)
            ttk.Entry(form, textvariable=values[key], width=52).grid(
                row=row, column=1, sticky="ew", padx=(10, 0), pady=4
            )
        form.columnconfigure(1, weight=1)

        def save_site() -> None:
            site_id = values["site_id"].get().strip().lower()
            if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", site_id):
                messagebox.showerror(
                    "ID invalido",
                    "Usa letras minusculas, numeros, guion o guion bajo.",
                    parent=dialog,
                )
                return
            site_name = values["site_name"].get().strip()
            company = values["company"].get().strip()
            base_url = values["url"].get().strip()
            country_query = values["country_query"].get().strip()
            country = values["country"].get().strip().upper()
            if not site_name or not company or not country_query or not country:
                messagebox.showerror(
                    "Faltan datos",
                    "Nombre, empresa, pais y texto de ubicacion son obligatorios.",
                    parent=dialog,
                )
                return
            try:
                WorkableFetcher._board_token(base_url)
                if self.repository.get_site(site_id) is not None:
                    raise ValueError(f"Ya existe un sitio con ID {site_id!r}.")
                if self.repository.get_platform("workable") is None:
                    self.repository.register_platform("workable", "Workable", "api")
                self.repository.register_site(
                    site_id,
                    "workable",
                    site_name,
                    base_url,
                    company_name=company,
                    country=country,
                    run_groups=("directos",),
                    site_config={
                        "adapter": "workable",
                        "country_query": country_query,
                    },
                    enabled=True,
                )
            except (sqlite3.Error, ValueError) as exc:
                messagebox.showerror(
                    "No se pudo agregar el tablero", str(exc), parent=dialog
                )
                return
            dialog.destroy()
            self.refresh()
            self.sites_table.selection_set(site_id)
            self._update_action_state()

        buttons = ttk.Frame(form)
        buttons.grid(row=6, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(buttons, text="Cancelar", command=dialog.destroy).pack(
            side="right"
        )
        ttk.Button(buttons, text="Guardar", command=save_site).pack(
            side="right", padx=(0, 8)
        )

        def submit_on_return(event: tk.Event) -> str:
            if event.keysym == "Return":
                save_site()
            return "break"

        def dismiss_on_escape(event: tk.Event) -> str:
            _ = event
            dialog.destroy()
            return "break"

        dialog.bind("<Return>", submit_on_return)
        dialog.bind("<Escape>", dismiss_on_escape)

    def open_add_successfactors(self) -> None:
        dialog = tk.Toplevel(self.window)
        dialog.title("Agregar portal SuccessFactors")
        dialog.geometry("570x340")
        dialog.resizable(False, False)
        dialog.transient(self.window)
        dialog.grab_set()

        values = {
            "site_id": tk.StringVar(),
            "site_name": tk.StringVar(),
            "company": tk.StringVar(),
            "url": tk.StringVar(),
            "country": tk.StringVar(value="CR"),
            "country_query": tk.StringVar(value="Costa Rica"),
        }
        form = ttk.Frame(dialog, padding=16)
        form.grid(row=0, column=0, sticky="nsew")
        for row, (key, label) in enumerate(
            (
                ("site_id", "ID unico (ej. kn-cr)"),
                ("site_name", "Nombre del portal"),
                ("company", "Empresa"),
                ("url", "URL del portal SuccessFactors"),
                ("country", "Codigo de pais (ej. CR)"),
                ("country_query", "Texto para filtrar el feed"),
            )
        ):
            ttk.Label(form, text=label).grid(row=row, column=0, sticky="w", pady=4)
            ttk.Entry(form, textvariable=values[key], width=52).grid(
                row=row, column=1, sticky="ew", padx=(10, 0), pady=4
            )
        form.columnconfigure(1, weight=1)

        def save_site() -> None:
            site_id = values["site_id"].get().strip().lower()
            if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", site_id):
                messagebox.showerror(
                    "ID invalido",
                    "Usa letras minusculas, numeros, guion o guion bajo.",
                    parent=dialog,
                )
                return
            site_name = values["site_name"].get().strip()
            company = values["company"].get().strip()
            base_url = values["url"].get().strip()
            country_query = values["country_query"].get().strip()
            country = values["country"].get().strip().upper()
            if not site_name or not company or not country_query or not country:
                messagebox.showerror(
                    "Faltan datos",
                    "Nombre, empresa, pais y texto de ubicacion son obligatorios.",
                    parent=dialog,
                )
                return
            try:
                SuccessFactorsFetcher._portal_base(base_url)
                if self.repository.get_site(site_id) is not None:
                    raise ValueError(f"Ya existe un sitio con ID {site_id!r}.")
                if self.repository.get_platform("successfactors") is None:
                    self.repository.register_platform(
                        "successfactors", "SuccessFactors", "rss"
                    )
                self.repository.register_site(
                    site_id,
                    "successfactors",
                    site_name,
                    base_url,
                    company_name=company,
                    country=country,
                    run_groups=("directos",),
                    site_config={
                        "adapter": "successfactors",
                        "country_query": country_query,
                    },
                    enabled=True,
                )
            except (sqlite3.Error, ValueError) as exc:
                messagebox.showerror(
                    "No se pudo agregar el portal", str(exc), parent=dialog
                )
                return
            dialog.destroy()
            self.refresh()
            self.sites_table.selection_set(site_id)
            self._update_action_state()

        buttons = ttk.Frame(form)
        buttons.grid(row=6, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(buttons, text="Cancelar", command=dialog.destroy).pack(
            side="right"
        )
        ttk.Button(buttons, text="Guardar", command=save_site).pack(
            side="right", padx=(0, 8)
        )

        def submit_on_return(event: tk.Event) -> str:
            if event.keysym == "Return":
                save_site()
            return "break"

        def dismiss_on_escape(event: tk.Event) -> str:
            _ = event
            dialog.destroy()
            return "break"

        dialog.bind("<Return>", submit_on_return)
        dialog.bind("<Escape>", dismiss_on_escape)

    def scrape_selected(self) -> None:
        site = self._selected_site()
        if site is None or self.scrape_running:
            return
        if not site["enabled"]:
            messagebox.showinfo(
                "Fuente pausada",
                "Activa la fuente antes de consultarla.",
                parent=self.window,
            )
            return
        provider = self._site_provider(site)
        if provider is None:
            messagebox.showerror(
                "Proveedor no compatible",
                "Esta fuente no tiene un adaptador de consulta implementado.",
                parent=self.window,
            )
            return
        pages = 1
        if provider == "workday":
            try:
                pages = int(self.pages_var.get())
            except ValueError:
                messagebox.showerror(
                    "Numero de paginas invalido",
                    "Selecciona un numero de paginas entre 1 y 10.",
                    parent=self.window,
                )
                return
            if not 1 <= pages <= MAX_PAGES:
                messagebox.showerror(
                    "Numero de paginas invalido",
                    f"El maximo permitido es {MAX_PAGES} paginas.",
                    parent=self.window,
                )
                return
        limit_text = (
            f" con un maximo de {pages} pagina(s)"
            if provider == "workday"
            else ""
        )
        confirmed = messagebox.askyesno(
            f"Iniciar consulta {provider.title()}",
            f"Se consultara el tablero {site['site_name']} ({site['base_url']})"
            f"{limit_text}. ¿Continuar?",
            parent=self.window,
        )
        if not confirmed:
            return

        self.scrape_running = True
        self.status_var.set(f"Consultando {site['site_name']}...")
        self._set_action_state("disabled")
        worker = threading.Thread(
            target=self._scrape_worker,
            args=(site, pages),
            name=f"jobpulse-{provider}-{site['site_id']}",
            daemon=True,
        )
        self.scrape_thread = worker
        worker.start()
        self._schedule_scrape_poll()

    def scrape_selected_group(self) -> None:
        sites = self._selected_sites()
        if not sites or self.scrape_running:
            return
        provider = self._site_provider(sites[0])
        unsupported = [
            site["site_id"]
            for site in sites
            if self._site_provider(site) != provider or provider is None
        ]
        paused = [site["site_id"] for site in sites if not site["enabled"]]
        if unsupported or paused:
            messagebox.showerror(
                "Seleccion no valida",
                "Selecciona solo tableros activos del mismo proveedor "
                "(Workday, Greenhouse, Ashby, Lever, Recruitee, Workable o SuccessFactors). "
                + (
                    "Fuentes no compatibles o de otro proveedor: "
                    + ", ".join(unsupported)
                    + ". "
                    if unsupported
                    else ""
                )
                + ("Fuentes pausadas: " + ", ".join(paused) + "." if paused else ""),
                parent=self.window,
            )
            return
        pages = 1
        if provider == "workday":
            try:
                pages = int(self.pages_var.get())
            except ValueError:
                messagebox.showerror(
                    "Numero de paginas invalido",
                    "Selecciona un numero de paginas entre 1 y 10.",
                    parent=self.window,
                )
                return
            if not 1 <= pages <= MAX_PAGES:
                messagebox.showerror(
                    "Numero de paginas invalido",
                    f"El maximo permitido es {MAX_PAGES} paginas.",
                    parent=self.window,
                )
                return

        site_names = "\n".join(
            f"- {site['site_name']} ({site['base_url']})" for site in sites
        )
        limit_text = (
            f" con un maximo de {pages} pagina(s) por tablero"
            if provider == "workday"
            else ""
        )
        confirmed = messagebox.askyesno(
            f"Iniciar consulta {provider.title()}",
            f"Se consultaran secuencialmente {len(sites)} tableros"
            f"{limit_text}. Un error no detendra "
            f"los siguientes.\n\n{site_names}\n\n¿Continuar?",
            parent=self.window,
        )
        if not confirmed:
            return

        self.scrape_running = True
        self.status_var.set(
            f"Consultando {len(sites)} fuentes {provider.title()}..."
        )
        self._set_action_state("disabled")
        worker = threading.Thread(
            target=self._scrape_group_worker,
            args=(sites, pages),
            name=f"jobpulse-{provider}-group",
            daemon=True,
        )
        self.scrape_thread = worker
        worker.start()
        self._schedule_scrape_poll()

    def _scrape_group_worker(
        self, sites: list[dict[str, Any]], pages: int
    ) -> None:
        results = []
        failures = []
        fetcher = WorkdayFetcher()
        service = IngestionService(self.repository)
        for site in sites:
            site_id = site["site_id"]
            try:
                provider = self._site_provider(site)
                if provider == "workday":
                    fetcher = WorkdayFetcher()
                    skip_source_job_ids = (
                        self.repository.list_skipped_source_job_ids(site_id)
                    )
                    fetch_offers = lambda site=site, skip_source_job_ids=skip_source_job_ids: (
                        fetcher.fetch(
                            site,
                            pages=pages,
                            skip_source_job_ids=skip_source_job_ids,
                        )
                    )
                elif provider == "greenhouse":
                    fetcher = GreenhouseFetcher()
                    skip_source_job_ids = (
                        self.repository.list_skipped_source_job_ids(site_id)
                    )
                    fetch_offers = (
                        lambda site=site, skip_source_job_ids=skip_source_job_ids: (
                            fetcher.fetch(
                                site,
                                skip_source_job_ids=skip_source_job_ids,
                            )
                        )
                    )
                elif provider == "ashby":
                    fetcher = AshbyFetcher()
                    skip_source_job_ids = (
                        self.repository.list_skipped_source_job_ids(site_id)
                    )
                    fetch_offers = (
                        lambda site=site, skip_source_job_ids=skip_source_job_ids: (
                            fetcher.fetch(
                                site,
                                skip_source_job_ids=skip_source_job_ids,
                            )
                        )
                    )
                elif provider == "lever":
                    fetcher = LeverFetcher()
                    skip_source_job_ids = (
                        self.repository.list_skipped_source_job_ids(site_id)
                    )
                    fetch_offers = (
                        lambda site=site, skip_source_job_ids=skip_source_job_ids: (
                            fetcher.fetch(
                                site,
                                skip_source_job_ids=skip_source_job_ids,
                            )
                        )
                    )
                elif provider == "recruitee":
                    fetcher = RecruiteeFetcher()
                    skip_source_job_ids = (
                        self.repository.list_skipped_source_job_ids(site_id)
                    )
                    fetch_offers = (
                        lambda site=site, skip_source_job_ids=skip_source_job_ids: (
                            fetcher.fetch(
                                site,
                                skip_source_job_ids=skip_source_job_ids,
                            )
                        )
                    )
                elif provider == "workable":
                    fetcher = WorkableFetcher()
                    skip_source_job_ids = (
                        self.repository.list_skipped_source_job_ids(site_id)
                    )
                    fetch_offers = (
                        lambda site=site, skip_source_job_ids=skip_source_job_ids: (
                            fetcher.fetch(
                                site,
                                skip_source_job_ids=skip_source_job_ids,
                            )
                        )
                    )
                elif provider == "successfactors":
                    fetcher = SuccessFactorsFetcher()
                    skip_source_job_ids = (
                        self.repository.list_skipped_source_job_ids(site_id)
                    )
                    fetch_offers = (
                        lambda site=site, skip_source_job_ids=skip_source_job_ids: (
                            fetcher.fetch(
                                site,
                                skip_source_job_ids=skip_source_job_ids,
                            )
                        )
                    )
                else:
                    raise ValueError(
                        f"No hay adaptador de scraping para el sitio {site_id!r}."
                    )
                result = service.fetch_and_import(
                    site_id,
                    fetch_offers,
                    run_kind=provider or "unknown",
                )
            except (IngestionError, ValueError, sqlite3.Error) as exc:
                failures.append(
                    {
                        "site_id": site_id,
                        "run_id": (
                            exc.run_id if isinstance(exc, IngestionError) else None
                        ),
                        "error": str(exc),
                    }
                )
                continue
            results.append({"site_id": site_id, **result})
        self.scrape_results.put(
            (
                True,
                {
                    "mode": "group",
                    "sites_selected": len(sites),
                    "results": results,
                    "failures": failures,
                },
            )
        )

    def _scrape_worker(self, site: dict[str, Any], pages: int) -> None:
        try:
            provider = self._site_provider(site)
            if provider == "workday":
                fetcher = WorkdayFetcher()
                skip_source_job_ids = self.repository.list_skipped_source_job_ids(
                    site["site_id"]
                )
                fetch_offers = lambda: fetcher.fetch(
                    site,
                    pages=pages,
                    skip_source_job_ids=skip_source_job_ids,
                )
            elif provider == "greenhouse":
                fetcher = GreenhouseFetcher()
                skip_source_job_ids = self.repository.list_skipped_source_job_ids(
                    site["site_id"]
                )
                fetch_offers = lambda: fetcher.fetch(
                    site, skip_source_job_ids=skip_source_job_ids
                )
            elif provider == "ashby":
                fetcher = AshbyFetcher()
                skip_source_job_ids = self.repository.list_skipped_source_job_ids(
                    site["site_id"]
                )
                fetch_offers = lambda: fetcher.fetch(
                    site, skip_source_job_ids=skip_source_job_ids
                )
            elif provider == "lever":
                fetcher = LeverFetcher()
                skip_source_job_ids = self.repository.list_skipped_source_job_ids(
                    site["site_id"]
                )
                fetch_offers = lambda: fetcher.fetch(
                    site, skip_source_job_ids=skip_source_job_ids
                )
            elif provider == "recruitee":
                fetcher = RecruiteeFetcher()
                skip_source_job_ids = self.repository.list_skipped_source_job_ids(
                    site["site_id"]
                )
                fetch_offers = lambda: fetcher.fetch(
                    site, skip_source_job_ids=skip_source_job_ids
                )
            elif provider == "workable":
                fetcher = WorkableFetcher()
                skip_source_job_ids = self.repository.list_skipped_source_job_ids(
                    site["site_id"]
                )
                fetch_offers = lambda: fetcher.fetch(
                    site, skip_source_job_ids=skip_source_job_ids
                )
            elif provider == "successfactors":
                fetcher = SuccessFactorsFetcher()
                skip_source_job_ids = self.repository.list_skipped_source_job_ids(
                    site["site_id"]
                )
                fetch_offers = lambda: fetcher.fetch(
                    site, skip_source_job_ids=skip_source_job_ids
                )
            else:
                raise ValueError(
                    f"No hay adaptador de scraping para {site['site_id']!r}."
                )
            result = IngestionService(self.repository).fetch_and_import(
                site["site_id"],
                fetch_offers,
                run_kind=provider,
            )
        except (IngestionError, ValueError, sqlite3.Error) as exc:
            self.scrape_results.put((False, str(exc)))
            return
        self.scrape_results.put((True, result))

    def _poll_scrape_results(self) -> None:
        self.poll_after_id = None
        if not self.window.winfo_exists():
            return
        try:
            succeeded, payload = self.scrape_results.get_nowait()
        except queue.Empty:
            self._schedule_scrape_poll()
            return
        if succeeded:
            if isinstance(payload, dict):
                if payload.get("mode") == "group":
                    self._scrape_group_completed(payload)
                else:
                    self._scrape_completed(payload)
            else:
                self._scrape_failed("La respuesta de ingesta no es valida.")
        elif isinstance(payload, str):
            self._scrape_failed(payload)
        else:
            self._scrape_failed("La consulta fallo con un error no especificado.")
        if self.scrape_running and self.window.winfo_exists():
            self._schedule_scrape_poll()

    def _schedule_scrape_poll(self) -> None:
        if self.window.winfo_exists():
            self.poll_after_id = self.window.after(100, self._poll_scrape_results)

    def _scrape_failed(self, error: str) -> None:
        self.scrape_running = False
        self.scrape_thread = None
        self.refresh()
        self._refresh_open_history()
        self._update_action_state()
        self.status_var.set("La consulta fallo; la corrida quedo registrada.")
        messagebox.showerror("Error de consulta", error, parent=self.window)

    def _scrape_completed(self, result: dict[str, int | str]) -> None:
        self.scrape_running = False
        self.scrape_thread = None
        self.refresh()
        self._refresh_open_history()
        self._update_action_state()
        self.status_var.set(
            f"Corrida {result['run_id']}: {result['jobs_new']} nuevas, "
            f"{result['jobs_duplicate']} duplicadas, "
            f"{result.get('jobs_skipped', 0)} omitidas por estado manual."
        )
        if callable(self.on_import_complete):
            self.on_import_complete()
        messagebox.showinfo(
            "Consulta completada",
            f"Encontradas: {result['jobs_found']}\n"
            f"Nuevas: {result['jobs_new']}\n"
            f"Duplicadas: {result['jobs_duplicate']}\n"
            f"Omitidas por estado manual: {result.get('jobs_skipped', 0)}",
            parent=self.window,
        )

    def _scrape_group_completed(self, report: dict[str, Any]) -> None:
        self.scrape_running = False
        self.scrape_thread = None
        self.refresh()
        self._refresh_open_history()
        self._update_action_state()
        results = report["results"]
        failures = report["failures"]
        new_jobs = sum(int(result["jobs_new"]) for result in results)
        duplicates = sum(int(result["jobs_duplicate"]) for result in results)
        self.status_var.set(
            f"Consulta de grupo: {len(results)} fuente(s) completada(s), "
            f"{len(failures)} con error; {new_jobs} vacantes nuevas."
        )
        if callable(self.on_import_complete) and results:
            self.on_import_complete()

        summary = [
            f"Fuentes seleccionadas: {report['sites_selected']}",
            f"Completadas: {len(results)}",
            f"Con error: {len(failures)}",
            f"Vacantes nuevas: {new_jobs}",
            f"Duplicadas: {duplicates}",
        ]
        if failures:
            summary.extend(
                (
                    "",
                    "Errores:",
                    *[
                        f"{failure['site_id']}: {failure['error']}"
                        for failure in failures
                    ],
                )
            )
            messagebox.showerror(
                "Consulta de grupo terminada con errores",
                "\n".join(summary),
                parent=self.window,
            )
        else:
            messagebox.showinfo(
                "Consulta de grupo completada",
                "\n".join(summary),
                parent=self.window,
            )

    def _refresh_open_history(self) -> None:
        if (
            self.history_window is not None
            and self.history_window.window.winfo_exists()
        ):
            self.history_window.refresh()

    def close(self) -> None:
        if self.scrape_running:
            self.window.withdraw()
            messagebox.showinfo(
                "Consulta en curso",
                "La consulta sigue ejecutandose en segundo plano. "
                "Vuelve a abrir Administrar fuentes para ver el resultado.",
                parent=self.parent,
            )
            return
        if self.poll_after_id is not None and self.window.winfo_exists():
            self.window.after_cancel(self.poll_after_id)
            self.poll_after_id = None
        self.window.destroy()


class SourceRunHistoryWindow:
    """Muestra el resultado de las corridas recientes de una fuente."""

    def __init__(
        self, parent: tk.Toplevel, repository: Repository, site: Mapping[str, Any]
    ) -> None:
        self.repository = repository
        self.site_id = str(site["site_id"])
        self.window = tk.Toplevel(parent)
        self.window.title(f"Historial - {site['site_name']}")
        self.window.geometry("1040x430")
        self.window.minsize(760, 320)
        self.window.transient(parent)
        frame = ttk.Frame(self.window, padding=14)
        frame.grid(row=0, column=0, sticky="nsew")
        self.window.rowconfigure(0, weight=1)
        self.window.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)
        frame.columnconfigure(0, weight=1)
        ttk.Label(
            frame,
            text=f"Ultimas 20 corridas de {site['site_name']} ({self.site_id})",
        ).grid(row=0, column=0, sticky="w", pady=(0, 8))

        columns = (
            "run_id",
            "started_at",
            "run_kind",
            "status",
            "found",
            "valid",
            "new",
            "duplicate",
            "error",
        )
        self.runs_table = ttk.Treeview(
            frame, columns=columns, show="headings", selectmode="browse"
        )
        headers = {
            "run_id": "Corrida",
            "started_at": "Inicio",
            "run_kind": "Tipo",
            "status": "Estado",
            "found": "Encontradas",
            "valid": "Validas",
            "new": "Nuevas",
            "duplicate": "Duplicadas",
            "error": "Error",
        }
        widths = {
            "run_id": 65,
            "started_at": 140,
            "run_kind": 105,
            "status": 120,
            "found": 80,
            "valid": 70,
            "new": 65,
            "duplicate": 85,
            "error": 260,
        }
        for column in columns:
            self.runs_table.heading(column, text=headers[column])
            self.runs_table.column(
                column,
                width=widths[column],
                minwidth=55,
                stretch=column in {"started_at", "error"},
            )
        vertical = ttk.Scrollbar(
            frame, orient="vertical", command=self.runs_table.yview
        )
        horizontal = ttk.Scrollbar(
            frame, orient="horizontal", command=self.runs_table.xview
        )
        self.runs_table.configure(
            yscrollcommand=vertical.set, xscrollcommand=horizontal.set
        )
        self.runs_table.grid(row=1, column=0, sticky="nsew")
        vertical.grid(row=1, column=1, sticky="ns")
        horizontal.grid(row=2, column=0, sticky="ew")
        self.status_var = tk.StringVar(value="")
        ttk.Label(frame, textvariable=self.status_var, anchor="w").grid(
            row=3, column=0, sticky="ew", pady=(6, 0)
        )
        ttk.Button(frame, text="Actualizar", command=self.refresh).grid(
            row=3, column=1, sticky="e", pady=(6, 0)
        )
        self.refresh()

    def refresh(self) -> None:
        try:
            runs = self.repository.list_site_runs(self.site_id, limit=20)
        except (sqlite3.Error, ValueError) as exc:
            messagebox.showerror(
                "No se pudo cargar el historial", str(exc), parent=self.window
            )
            return
        for item in self.runs_table.get_children():
            self.runs_table.delete(item)
        for run in runs:
            self.runs_table.insert("", "end", values=format_site_run_row(run))
        self.status_var.set(
            f"{len(runs)} corrida(s) mostradas."
            if runs
            else "Esta fuente aun no tiene corridas registradas."
        )

    def close(self) -> None:
        self.window.destroy()


class ApplicationTrackingWindow:
    """Agrega y consulta eventos del seguimiento opcional de una postulacion."""

    def __init__(
        self, parent: tk.Tk, repository: Repository, job: Mapping[str, Any]
    ) -> None:
        self.repository = repository
        self.job_id = str(job["job_id"])
        self.window = tk.Toplevel(parent)
        self.window.title(f"Seguimiento - {job['title']}")
        self.window.geometry("960x570")
        self.window.minsize(720, 440)
        self.window.transient(parent)
        frame = ttk.Frame(self.window, padding=14)
        frame.grid(row=0, column=0, sticky="nsew")
        self.window.rowconfigure(0, weight=1)
        self.window.columnconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)
        ttk.Label(
            frame,
            text=f"{job['title']} - {job.get('company') or 'Empresa no especificada'}",
        ).grid(row=0, column=0, sticky="w", pady=(0, 8))

        columns = ("status", "applied_at", "next_action_at", "notes", "created_at")
        self.events_table = ttk.Treeview(
            frame, columns=columns, show="headings", selectmode="browse"
        )
        headers = {
            "status": "Estado",
            "applied_at": "Fecha de aplicacion",
            "next_action_at": "Proxima accion",
            "notes": "Notas",
            "created_at": "Registrado",
        }
        widths = {
            "status": 180,
            "applied_at": 120,
            "next_action_at": 120,
            "notes": 300,
            "created_at": 145,
        }
        for column in columns:
            self.events_table.heading(column, text=headers[column])
            self.events_table.column(
                column, width=widths[column], minwidth=75, stretch=column == "notes"
            )
        vertical = ttk.Scrollbar(
            frame, orient="vertical", command=self.events_table.yview
        )
        horizontal = ttk.Scrollbar(
            frame, orient="horizontal", command=self.events_table.xview
        )
        self.events_table.configure(
            yscrollcommand=vertical.set, xscrollcommand=horizontal.set
        )
        self.events_table.grid(row=1, column=0, sticky="nsew")
        vertical.grid(row=1, column=1, sticky="ns")
        horizontal.grid(row=2, column=0, sticky="ew")

        form = ttk.LabelFrame(frame, text="Registrar evento", padding=10)
        form.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        form.columnconfigure(5, weight=1)
        self.status_var = tk.StringVar(value="Planificada")
        self.applied_at_var = tk.StringVar()
        self.next_action_at_var = tk.StringVar()
        ttk.Label(form, text="Estado").grid(row=0, column=0, sticky="w")
        ttk.Combobox(
            form,
            textvariable=self.status_var,
            values=tuple(APPLICATION_STATUS_LABELS),
            state="readonly",
            width=26,
        ).grid(row=0, column=1, sticky="w", padx=(6, 14))
        ttk.Label(form, text="Fecha de aplicacion").grid(
            row=0, column=2, sticky="w"
        )
        ttk.Entry(form, textvariable=self.applied_at_var, width=13).grid(
            row=0, column=3, sticky="w", padx=(6, 14)
        )
        ttk.Label(form, text="Proxima accion").grid(row=0, column=4, sticky="w")
        ttk.Entry(form, textvariable=self.next_action_at_var, width=13).grid(
            row=0, column=5, sticky="ew", padx=(6, 0)
        )
        ttk.Label(form, text="Notas").grid(row=1, column=0, sticky="nw", pady=(8, 0))
        self.notes_text = tk.Text(form, width=60, height=3, wrap="word")
        self.notes_text.grid(
            row=1, column=1, columnspan=5, sticky="ew", padx=(6, 0), pady=(8, 0)
        )
        buttons = ttk.Frame(form)
        buttons.grid(row=2, column=0, columnspan=6, sticky="e", pady=(10, 0))
        ttk.Button(buttons, text="Guardar evento", command=self.save_event).pack(
            side="right"
        )
        self.status_message_var = tk.StringVar(value="")
        ttk.Label(frame, textvariable=self.status_message_var, anchor="w").grid(
            row=4, column=0, columnspan=2, sticky="ew", pady=(8, 0)
        )
        self.refresh()

    def refresh(self) -> None:
        try:
            events = self.repository.list_applications(self.job_id)
        except (sqlite3.Error, ValueError) as exc:
            messagebox.showerror(
                "No se pudo cargar el seguimiento", str(exc), parent=self.window
            )
            return
        for item in self.events_table.get_children():
            self.events_table.delete(item)
        for event in events:
            self.events_table.insert(
                "",
                "end",
                iid=str(event["application_id"]),
                values=(
                    APPLICATION_STATUS_NAMES.get(
                        event["application_status"], event["application_status"]
                    ),
                    event.get("applied_at") or "",
                    event.get("next_action_at") or "",
                    event.get("notes") or "",
                    event.get("created_at") or "",
                ),
            )
        self.status_message_var.set(
            f"{len(events)} evento(s) registrados."
            if events
            else "El seguimiento es opcional; aun no hay eventos."
        )

    def save_event(self) -> None:
        try:
            application_id = self.repository.record_application(
                self.job_id,
                APPLICATION_STATUS_LABELS[self.status_var.get()],
                applied_at=self.applied_at_var.get(),
                next_action_at=self.next_action_at_var.get(),
                notes=self.notes_text.get("1.0", "end-1c"),
            )
        except (sqlite3.Error, ValueError) as exc:
            messagebox.showerror(
                "No se pudo guardar el evento", str(exc), parent=self.window
            )
            return
        self.applied_at_var.set("")
        self.next_action_at_var.set("")
        self.notes_text.delete("1.0", "end")
        self.refresh()
        self.status_message_var.set(f"Evento {application_id} guardado.")

    def close(self) -> None:
        self.window.destroy()


def main() -> int:
    database = Database()
    database.initialize()
    root = tk.Tk()
    JobPulseWindow(root, Repository(database))
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
