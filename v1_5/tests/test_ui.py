import gc
import csv
import io
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from unittest.mock import patch
from pathlib import Path
from tkinter import ttk

import tkinter as tk

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from jobpulse_v15.candidate_profile import (
    CandidateProfileImport,
    CandidateProfileSuggestion,
)
from jobpulse_v15.db import Database
from jobpulse_v15.repository import Repository
from jobpulse_v15.ui import (
    ApplicationTrackingWindow,
    CandidateProfileImportWindow,
    CandidateProfileWindow,
    JobPulseWindow,
    SourceManagerWindow,
    format_job_detail,
    format_job_review_reasons,
    format_job_review_summary,
    format_profile_comparison,
    format_site_run_row,
    format_source_row,
    write_job_search_csv,
)


class TestFormatJobDetail(unittest.TestCase):
    def test_includes_location_sources_analysis_and_evidence(self):
        detail = format_job_detail(
            {
                "title": "QA Engineer",
                "company": "Example",
                "city": "San Jose",
                "region": "San Jose",
                "country": "CR",
                "work_mode": "hybrid",
                "posting_language": "en",
                "employment_type": "Full-time",
                "salary_min": 5000,
                "salary_max": 6500,
                "salary_currency": "USD",
                "salary_period": "annual",
                "description_raw": "Build quality systems.",
                "requirements_raw": "English required.",
            },
            {
                "analysis_status": "complete",
                "extractor_version": "rules-1",
                "field_statuses": {"language": "found"},
                "requirements": [
                    {
                        "category": "language",
                        "value_text": "English",
                        "qualifier": "advanced",
                        "requirement_level": "required",
                        "evidence": "Advanced English required.",
                    }
                ],
            },
            [
                {
                    "site_id": "source-1",
                    "site_name": "Example Careers",
                    "source_url": "https://example.test/job/1",
                }
            ],
        )

        self.assertIn("San Jose, San Jose, CR", detail)
        self.assertIn("Example Careers: https://example.test/job/1", detail)
        self.assertIn("Salario: 5000 - 6500 USD annual", detail)
        self.assertIn("[required] language: English (advanced)", detail)
        self.assertIn("Evidencia: Advanced English required.", detail)
        self.assertIn("Revision de datos:", detail)
        self.assertIn("Sin problemas detectados", detail)
        self.assertIn("Build quality systems.", detail)

    def test_review_reason_labels_explain_each_quality_issue(self):
        labels = format_job_review_reasons(
            [
                "missing_company",
                "location_not_normalized",
                "analysis_partial",
                "unknown_field:license",
                "ambiguous_field:language",
            ]
        )

        self.assertEqual(
            labels,
            [
                "Empresa no registrada",
                "Ubicacion sin normalizar",
                "Analisis parcial",
                "Campo license desconocido",
                "Campo language ambiguo",
            ],
        )
        self.assertEqual(format_job_review_summary(labels), "Revisar (5)")

    def test_handles_missing_analysis_and_source(self):
        detail = format_job_detail(
            {"title": "Engineer"},
            None,
            [],
        )

        self.assertIn("Aun no hay un analisis guardado.", detail)
        self.assertIn("Sin enlaces de fuente registrados.", detail)
        self.assertIn("Sin descripcion.", detail)


class TestJobSearchCSV(unittest.TestCase):
    def test_csv_contains_actionable_fields_without_raw_description_or_payload(self):
        output = io.StringIO(newline="")
        write_job_search_csv(
            output,
            [
                {
                    "job_id": "job-1",
                    "title": "Ingeniero, QA",
                    "company": "Example",
                    "country": "CR",
                    "region": "San Jose",
                    "city": "San Jose",
                    "work_mode": "hybrid",
                    "posting_language": "es",
                    "date_published": "2026-10-20",
                    "salary_min": 5000,
                    "salary_max": 6500,
                    "salary_currency": "USD",
                    "salary_period": "annual",
                    "profile_compatibility": "Coincide",
                    "review_reasons": ["missing_company", "analysis_partial"],
                    "source_url": "https://example.test/job-1",
                    "description_raw": "Should not be exported",
                    "raw_payload_json": '{"private":"source payload"}',
                }
            ],
        )

        rows = list(csv.reader(io.StringIO(output.getvalue())))
        self.assertEqual(
            rows[0],
            [
                "job_id",
                "titulo",
                "empresa",
                "pais",
                "region",
                "ciudad",
                "modalidad",
                "idioma_anuncio",
                "fecha_publicacion",
                "salario_minimo",
                "salario_maximo",
                "moneda_salario",
                "periodo_salario",
                "compatibilidad_perfil",
                "motivos_revision",
                "url_fuente",
            ],
        )
        self.assertEqual(
            rows[1],
            [
                "job-1",
                "Ingeniero, QA",
                "Example",
                "CR",
                "San Jose",
                "San Jose",
                "hybrid",
                "es",
                "2026-10-20",
                "5000",
                "6500",
                "USD",
                "annual",
                "Coincide",
                "missing_company;analysis_partial",
                "https://example.test/job-1",
            ],
        )
        self.assertNotIn("Should not be exported", output.getvalue())
        self.assertNotIn("private", output.getvalue())

    def test_csv_neutralizes_spreadsheet_formula_prefixes(self):
        output = io.StringIO(newline="")
        write_job_search_csv(
            output,
            [
                {
                    "job_id": "job-formula",
                    "title": "=HYPERLINK(\"https://bad.test\")",
                    "company": " \t@SUM(1,1)",
                    "country": "+1",
                    "region": "-2",
                    "city": "\t=3",
                    "work_mode": "remote",
                    "posting_language": "en",
                    "profile_compatibility": "OK",
                    "review_reasons": [],
                    "source_url": "https://safe.test/job",
                }
            ],
        )
        row = list(csv.reader(io.StringIO(output.getvalue())))[1]

        self.assertEqual(row[1], "'=HYPERLINK(\"https://bad.test\")")
        self.assertEqual(row[2], "' \t@SUM(1,1)")
        self.assertEqual(row[3:6], ["'+1", "'-2", "'\t=3"])


class TestFormatProfileComparison(unittest.TestCase):
    def test_report_explains_exact_match_and_non_registered_data(self):
        report = format_profile_comparison(
            {
                "job_title": "QA Engineer",
                "analysis_status": "complete",
                "counts": {
                    "matched": 1,
                    "not_registered": 1,
                    "below_requirement": 0,
                    "needs_review": 0,
                },
                "requirements": [
                    {
                        "category": "language",
                        "value_text": "English",
                        "qualifier": "advanced",
                        "requirement_level": "required",
                        "comparison_status": "matched",
                        "profile_fact": {"value_text": "Ingles"},
                        "evidence": "Advanced English required.",
                    },
                    {
                        "category": "license",
                        "value_text": "Driver's license B1",
                        "requirement_level": "required",
                        "comparison_status": "not_registered",
                        "evidence": "A valid license is required.",
                    },
                ],
            }
        )

        self.assertIn("Coincidencias exactas: 1", report)
        self.assertIn("No registrados en el perfil: 1", report)
        self.assertIn("un dato no registrado no significa", report)
        self.assertIn("Evidencia: Advanced English required.", report)

    def test_report_identifies_jobs_without_analysis(self):
        report = format_profile_comparison(
            {
                "job_title": "Engineer",
                "analysis_status": "not_analyzed",
                "counts": {},
                "requirements": [],
            }
        )

        self.assertIn("aun no tiene un analisis", report)


class TestSourceManagerWindow(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "ui.db")
        self.database.initialize()
        self.repository = Repository(self.database)
        self.repository.register_platform("workday", "Workday", "api")
        self.repository.register_site(
            "intel-cr",
            "workday",
            "Intel Careers CR",
            "https://intel.wd1.myworkdayjobs.com/External",
            company_name="Intel",
            country="CR",
        )
        self.root = tk.Tk()
        self.root.withdraw()

    def tearDown(self):
        self.root.update_idletasks()
        self.root.destroy()
        gc.collect()
        self.temp_dir.cleanup()

    def test_source_rows_show_enabled_and_review_status(self):
        row = format_source_row(
            {
                "site_id": "intel-cr",
                "site_name": "Intel Careers CR",
                "company_name": "Intel",
                "enabled": True,
                "health_status": "needs_review",
                "review_required": True,
            }
        )

        self.assertEqual(
            row,
            (
                "intel-cr",
                "Intel Careers CR",
                "Intel",
                "Activo",
                "needs_review (revisar)",
            ),
        )

    def test_site_run_rows_format_status_counts_and_errors(self):
        row = format_site_run_row(
            {
                "run_id": 12,
                "started_at": "2026-10-23 18:10:00",
                "run_kind": "workday",
                "site_status": "error",
                "jobs_found": 0,
                "jobs_valid": 0,
                "jobs_new": 0,
                "jobs_duplicate": 0,
                "error_message": "HTTP 503",
            }
        )

        self.assertEqual(
            row,
            (
                "12",
                "2026-10-23 18:10:00",
                "Workday",
                "Error",
                "0",
                "0",
                "0",
                "0",
                "HTTP 503",
            ),
        )

    def test_source_manager_lists_and_pauses_a_registered_site(self):
        manager = SourceManagerWindow(self.root, self.repository)
        try:
            self.root.update()
            self.assertEqual(manager.sites_table.get_children(), ("intel-cr",))
            manager.sites_table.selection_set("intel-cr")
            manager.toggle_selected()

            site = self.repository.get_site("intel-cr")
            self.assertFalse(site["enabled"])
            self.assertEqual(
                manager.sites_table.set("intel-cr", "enabled"), "Pausado"
            )
            self.assertEqual(str(manager.scrape_button["state"]), "disabled")
            self.assertEqual(str(manager.review_button["state"]), "disabled")
        finally:
            manager.close()

    def test_source_manager_can_mark_a_flagged_site_as_reviewed(self):
        for _ in range(3):
            run_id = self.repository.start_site_run("intel-cr", "workday")
            self.repository.record_site_result(
                run_id,
                "intel-cr",
                jobs_found=0,
                jobs_valid=0,
                jobs_new=0,
                jobs_duplicate=0,
            )
            self.repository.finish_run(run_id)

        manager = SourceManagerWindow(self.root, self.repository)
        manager.sites_table.selection_set("intel-cr")
        try:
            self.root.update()
            self.assertEqual(str(manager.review_button["state"]), "normal")
            manager.mark_selected_reviewed()

            site = self.repository.get_site("intel-cr")
            self.assertFalse(site["review_required"])
            self.assertEqual(site["health_status"], "zero_results")
            self.assertEqual(
                manager.sites_table.set("intel-cr", "health"), "zero_results"
            )
            self.assertEqual(str(manager.review_button["state"]), "disabled")
            self.assertIn("marcada como revisada", manager.status_var.get())
        finally:
            manager.close()

    def test_add_workday_dialog_registers_normalized_site_configuration(self):
        manager = SourceManagerWindow(self.root, self.repository)
        try:
            manager.open_add_workday()
            self.root.update()
            dialogs = [
                widget
                for widget in manager.window.winfo_children()
                if isinstance(widget, tk.Toplevel)
                and widget.title() == "Agregar tablero Workday"
            ]
            self.assertEqual(len(dialogs), 1)
            dialog = dialogs[0]
            form = dialog.winfo_children()[0]
            entries = sorted(
                (
                    widget
                    for widget in form.winfo_children()
                    if isinstance(widget, ttk.Entry)
                ),
                key=lambda widget: int(widget.grid_info()["row"]),
            )
            values = (
                "acme-cr",
                "Acme Careers",
                "Acme",
                "https://acme.wd5.myworkdayjobs.com/External",
                "CR",
                "Costa Rica",
                "Costa Rica",
            )
            for entry, value in zip(entries, values):
                entry.delete(0, "end")
                entry.insert(0, value)

            def descendants(widget):
                for child in widget.winfo_children():
                    yield child
                    yield from descendants(child)

            save_button = next(
                widget
                for widget in descendants(form)
                if isinstance(widget, ttk.Button) and widget.cget("text") == "Guardar"
            )
            save_button.invoke()

            site = self.repository.get_site("acme-cr")
            self.assertEqual(site["provider_type"], "api")
            self.assertEqual(site["site_config"]["adapter"], "workday")
            self.assertEqual(site["site_config"]["country_query"], "Costa Rica")
            self.assertEqual(site["run_groups"], ["directos"])
            self.assertIn("acme-cr", manager.sites_table.get_children())
        finally:
            manager.close()

    def test_add_greenhouse_dialog_registers_grouped_source(self):
        manager = SourceManagerWindow(self.root, self.repository)
        try:
            manager.open_add_greenhouse()
            self.root.update()
            dialogs = [
                widget
                for widget in manager.window.winfo_children()
                if isinstance(widget, tk.Toplevel)
                and widget.title() == "Agregar tablero Greenhouse"
            ]
            self.assertEqual(len(dialogs), 1)
            dialog = dialogs[0]
            form = dialog.winfo_children()[0]
            entries = sorted(
                (
                    widget
                    for widget in form.winfo_children()
                    if isinstance(widget, ttk.Entry)
                ),
                key=lambda widget: int(widget.grid_info()["row"]),
            )
            for entry, value in zip(
                entries,
                (
                    "acme-cr",
                    "Acme Careers CR",
                    "Acme",
                    "https://job-boards.greenhouse.io/acme",
                    "CR",
                    "Costa Rica",
                ),
            ):
                entry.delete(0, "end")
                entry.insert(0, value)

            def descendants(widget):
                for child in widget.winfo_children():
                    yield child
                    yield from descendants(child)

            save_button = next(
                widget
                for widget in descendants(form)
                if isinstance(widget, ttk.Button) and widget.cget("text") == "Guardar"
            )
            save_button.invoke()

            site = self.repository.get_site("acme-cr")
            self.assertEqual(site["platform_id"], "greenhouse")
            self.assertEqual(site["site_config"]["adapter"], "greenhouse")
            self.assertEqual(site["run_groups"], ["directos"])
            self.assertIn("acme-cr", manager.sites_table.get_children())
        finally:
            manager.close()

    def test_add_ashby_dialog_registers_grouped_source(self):
        manager = SourceManagerWindow(self.root, self.repository)
        try:
            manager.open_add_ashby()
            self.root.update()
            dialogs = [
                widget
                for widget in manager.window.winfo_children()
                if isinstance(widget, tk.Toplevel)
                and widget.title() == "Agregar tablero Ashby"
            ]
            self.assertEqual(len(dialogs), 1)
            dialog = dialogs[0]
            form = dialog.winfo_children()[0]
            entries = sorted(
                (
                    widget
                    for widget in form.winfo_children()
                    if isinstance(widget, ttk.Entry)
                ),
                key=lambda widget: int(widget.grid_info()["row"]),
            )
            for entry, value in zip(
                entries,
                (
                    "acme-cr",
                    "Acme Careers CR",
                    "Acme",
                    "https://jobs.ashbyhq.com/acme",
                    "CR",
                    "Costa Rica",
                ),
            ):
                entry.delete(0, "end")
                entry.insert(0, value)

            def descendants(widget):
                for child in widget.winfo_children():
                    yield child
                    yield from descendants(child)

            save_button = next(
                widget
                for widget in descendants(form)
                if isinstance(widget, ttk.Button) and widget.cget("text") == "Guardar"
            )
            save_button.invoke()

            site = self.repository.get_site("acme-cr")
            self.assertEqual(site["platform_id"], "ashby")
            self.assertEqual(site["site_config"]["adapter"], "ashby")
            self.assertEqual(site["run_groups"], ["directos"])
            self.assertIn("acme-cr", manager.sites_table.get_children())
        finally:
            manager.close()

    def test_add_lever_dialog_registers_grouped_source(self):
        manager = SourceManagerWindow(self.root, self.repository)
        try:
            manager.open_add_lever()
            self.root.update()
            dialogs = [
                widget
                for widget in manager.window.winfo_children()
                if isinstance(widget, tk.Toplevel)
                and widget.title() == "Agregar tablero Lever"
            ]
            self.assertEqual(len(dialogs), 1)
            dialog = dialogs[0]
            form = dialog.winfo_children()[0]
            entries = sorted(
                (
                    widget
                    for widget in form.winfo_children()
                    if isinstance(widget, ttk.Entry)
                ),
                key=lambda widget: int(widget.grid_info()["row"]),
            )
            for entry, value in zip(
                entries,
                (
                    "acme-cr",
                    "Acme Careers CR",
                    "Acme",
                    "https://jobs.lever.co/acme",
                    "CR",
                    "Costa Rica",
                ),
            ):
                entry.delete(0, "end")
                entry.insert(0, value)

            def descendants(widget):
                for child in widget.winfo_children():
                    yield child
                    yield from descendants(child)

            save_button = next(
                widget
                for widget in descendants(form)
                if isinstance(widget, ttk.Button) and widget.cget("text") == "Guardar"
            )
            save_button.invoke()

            site = self.repository.get_site("acme-cr")
            self.assertEqual(site["platform_id"], "lever")
            self.assertEqual(site["site_config"]["adapter"], "lever")
            self.assertEqual(site["run_groups"], ["directos"])
            self.assertIn("acme-cr", manager.sites_table.get_children())
        finally:
            manager.close()

    def test_add_recruitee_dialog_registers_grouped_source(self):
        manager = SourceManagerWindow(self.root, self.repository)
        try:
            manager.open_add_recruitee()
            self.root.update()
            dialogs = [
                widget
                for widget in manager.window.winfo_children()
                if isinstance(widget, tk.Toplevel)
                and widget.title() == "Agregar tablero Recruitee"
            ]
            self.assertEqual(len(dialogs), 1)
            dialog = dialogs[0]
            form = dialog.winfo_children()[0]
            entries = sorted(
                (
                    widget
                    for widget in form.winfo_children()
                    if isinstance(widget, ttk.Entry)
                ),
                key=lambda widget: int(widget.grid_info()["row"]),
            )
            for entry, value in zip(
                entries,
                (
                    "acme-cr",
                    "Acme Careers CR",
                    "Acme",
                    "https://acme.recruitee.com",
                    "CR",
                    "Costa Rica",
                ),
            ):
                entry.delete(0, "end")
                entry.insert(0, value)

            def descendants(widget):
                for child in widget.winfo_children():
                    yield child
                    yield from descendants(child)

            save_button = next(
                widget
                for widget in descendants(form)
                if isinstance(widget, ttk.Button) and widget.cget("text") == "Guardar"
            )
            save_button.invoke()

            site = self.repository.get_site("acme-cr")
            self.assertEqual(site["platform_id"], "recruitee")
            self.assertEqual(site["site_config"]["adapter"], "recruitee")
            self.assertEqual(site["run_groups"], ["directos"])
            self.assertIn("acme-cr", manager.sites_table.get_children())
        finally:
            manager.close()

    def test_workday_query_runs_only_after_explicit_confirmation(self):
        manager = SourceManagerWindow(self.root, self.repository)
        manager.sites_table.selection_set("intel-cr")
        try:
            with patch(
                "jobpulse_v15.ui.messagebox.askyesno", return_value=False
            ), patch("jobpulse_v15.ui.WorkdayFetcher.fetch") as fetch:
                manager.scrape_selected()
            fetch.assert_not_called()
            self.assertEqual(self.repository.list_runs(), [])
        finally:
            manager.close()

    def test_confirmed_ashby_query_imports_in_background(self):
        self.repository.register_platform("ashby", "Ashby", "api")
        self.repository.register_site(
            "ashby-cr",
            "ashby",
            "Ashby Careers CR",
            "https://jobs.ashbyhq.com/acme",
            company_name="Acme",
            country="CR",
        )
        manager = SourceManagerWindow(self.root, self.repository)
        manager.sites_table.selection_set("ashby-cr")
        offer = {
            "source_job_id": "REQ-ASHBY-1",
            "source_url": "https://jobs.ashbyhq.com/acme/REQ-ASHBY-1",
            "job": {
                "job_id": "ashby-acme-REQ-ASHBY-1",
                "title": "Ashby UI Engineer",
                "country": "CR",
            },
        }
        try:
            with patch(
                "jobpulse_v15.ui.messagebox.askyesno", return_value=True
            ), patch(
                "jobpulse_v15.ui.messagebox.showinfo"
            ), patch(
                "jobpulse_v15.ui.AshbyFetcher.fetch", return_value=[offer]
            ) as fetch:
                manager.scrape_selected()
                self.assertIsNotNone(manager.scrape_thread)
                manager.scrape_thread.join(timeout=5)
                self.assertFalse(manager.scrape_thread.is_alive())
                self.root.after(150, self.root.quit)
                self.root.mainloop()

            fetch.assert_called_once()
            self.assertIsNotNone(
                self.repository.get_job("ashby-acme-REQ-ASHBY-1")
            )
            self.assertEqual(manager.status_var.get().split(":")[0], "Corrida 1")
        finally:
            manager.close()

    def test_source_manager_opens_history_for_selected_site(self):
        run_id = self.repository.start_site_run("intel-cr", "workday")
        self.repository.record_site_result(
            run_id,
            "intel-cr",
            jobs_found=2,
            jobs_valid=2,
            jobs_new=1,
            jobs_duplicate=1,
        )
        self.repository.finish_run(run_id)
        manager = SourceManagerWindow(self.root, self.repository)
        manager.sites_table.selection_set("intel-cr")
        try:
            manager.open_run_history()
            self.root.update()
            history = manager.history_window
            self.assertIsNotNone(history)
            rows = history.runs_table.get_children()
            self.assertEqual(len(rows), 1)
            self.assertEqual(
                history.runs_table.item(rows[0], "values"),
                (
                    str(run_id),
                    history.runs_table.item(rows[0], "values")[1],
                    "Workday",
                    "Correcta",
                    "2",
                    "2",
                    "1",
                    "1",
                    "",
                ),
            )
            self.assertIn("1 corrida", history.status_var.get())
        finally:
            manager.close()

    def test_confirmed_workday_query_runs_in_background_and_imports(self):
        manager = SourceManagerWindow(self.root, self.repository)
        manager.sites_table.selection_set("intel-cr")
        offers = [
            {
                "source_job_id": "REQ-UI-1",
                "source_url": "https://example.test/jobs/REQ-UI-1",
                "job": {
                    "job_id": "workday-REQ-UI-1",
                    "title": "UI Integration Engineer",
                    "country": "CR",
                },
            }
        ]
        try:
            with patch(
                "jobpulse_v15.ui.messagebox.askyesno", return_value=True
            ), patch(
                "jobpulse_v15.ui.messagebox.showinfo"
            ), patch(
                "jobpulse_v15.ui.WorkdayFetcher.fetch", return_value=offers
            ) as fetch:
                manager.scrape_selected()
                self.assertIsNotNone(manager.scrape_thread)
                manager.scrape_thread.join(timeout=5)
                self.assertFalse(manager.scrape_thread.is_alive())
                self.root.after(150, self.root.quit)
                self.root.mainloop()

            fetch.assert_called_once()
            self.assertIsNotNone(
                self.repository.get_job("workday-REQ-UI-1")
            )
            self.assertEqual(manager.status_var.get().split(":")[0], "Corrida 1")
        finally:
            manager.close()

    def test_group_query_continues_after_one_source_fails(self):
        self.repository.register_site(
            "acme-cr",
            "workday",
            "Acme Careers CR",
            "https://acme.wd5.myworkdayjobs.com/External",
            company_name="Acme",
            country="CR",
        )
        manager = SourceManagerWindow(self.root, self.repository)
        manager.sites_table.selection_set("intel-cr", "acme-cr")
        manager._update_action_state()
        self.assertEqual(str(manager.scrape_group_button["state"]), "normal")
        offers = [
            {
                "source_job_id": "REQ-GROUP-1",
                "source_url": "https://example.test/jobs/REQ-GROUP-1",
                "job": {
                    "job_id": "workday-REQ-GROUP-1",
                    "title": "Group Query Engineer",
                    "country": "CR",
                },
            }
        ]

        def fetch(site, **kwargs):
            _ = kwargs
            if site["site_id"] == "acme-cr":
                raise RuntimeError("portal no disponible")
            return offers

        try:
            with patch(
                "jobpulse_v15.ui.messagebox.askyesno", return_value=True
            ), patch(
                "jobpulse_v15.ui.messagebox.showerror"
            ) as showerror, patch(
                "jobpulse_v15.ui.WorkdayFetcher.fetch", side_effect=fetch
            ):
                manager.scrape_selected_group()
                self.assertIsNotNone(manager.scrape_thread)
                manager.scrape_thread.join(timeout=5)
                self.assertFalse(manager.scrape_thread.is_alive())
                self.root.after(150, self.root.quit)
                self.root.mainloop()

            self.assertIsNotNone(
                self.repository.get_job("workday-REQ-GROUP-1")
            )
            self.assertEqual(
                self.repository.list_site_runs("intel-cr", limit=1)[0][
                    "site_status"
                ],
                "zero_results" if not offers else "ok",
            )
            self.assertEqual(
                self.repository.list_site_runs("acme-cr", limit=1)[0][
                    "site_status"
                ],
                "error",
            )
            showerror.assert_called_once()
            self.assertIn("acme-cr", showerror.call_args.args[1])
            self.assertFalse(manager.scrape_running)
        finally:
            manager.close()

    def test_group_query_supports_greenhouse_boards(self):
        self.repository.register_platform("greenhouse", "Greenhouse", "api")
        for site_id in ("greenhouse-good", "greenhouse-fails"):
            self.repository.register_site(
                site_id,
                "greenhouse",
                site_id,
                f"https://boards.greenhouse.io/{site_id}",
                run_groups=("directos",),
                site_config={"adapter": "greenhouse"},
            )
        manager = SourceManagerWindow(self.root, self.repository)
        manager.sites_table.selection_set("greenhouse-good", "greenhouse-fails")
        manager._update_action_state()
        self.assertEqual(str(manager.scrape_group_button["state"]), "normal")

        offer = {
            "source_job_id": "55",
            "job": {
                "job_id": "greenhouse-greenhouse-good-55",
                "title": "Greenhouse Group Engineer",
            },
        }

        def fetch(site, **kwargs):
            _ = kwargs
            if site["site_id"] == "greenhouse-fails":
                raise RuntimeError("portal no disponible")
            return [offer]

        try:
            with patch(
                "jobpulse_v15.ui.messagebox.askyesno", return_value=True
            ), patch(
                "jobpulse_v15.ui.messagebox.showerror"
            ) as showerror, patch(
                "jobpulse_v15.ui.GreenhouseFetcher.fetch", side_effect=fetch
            ):
                manager.scrape_selected_group()
                self.assertIsNotNone(manager.scrape_thread)
                manager.scrape_thread.join(timeout=5)
                self.assertFalse(manager.scrape_thread.is_alive())
                self.root.after(150, self.root.quit)
                self.root.mainloop()

            self.assertIsNotNone(
                self.repository.get_job("greenhouse-greenhouse-good-55")
            )
            self.assertEqual(
                self.repository.list_site_runs("greenhouse-good", limit=1)[0][
                    "run_kind"
                ],
                "greenhouse",
            )
            self.assertEqual(
                self.repository.list_site_runs("greenhouse-fails", limit=1)[0][
                    "site_status"
                ],
                "error",
            )
            showerror.assert_called_once()
            self.assertNotIn(
                "greenhouse-good:",
                showerror.call_args.args[1],
            )
        finally:
            manager.close()

    def test_group_query_supports_ashby_boards(self):
        self.repository.register_platform("ashby", "Ashby", "api")
        for site_id in ("ashby-good", "ashby-fails"):
            self.repository.register_site(
                site_id,
                "ashby",
                site_id,
                f"https://jobs.ashbyhq.com/{site_id}",
                run_groups=("directos",),
                site_config={"adapter": "ashby"},
            )
        manager = SourceManagerWindow(self.root, self.repository)
        manager.sites_table.selection_set("ashby-good", "ashby-fails")
        manager._update_action_state()
        self.assertEqual(str(manager.scrape_group_button["state"]), "normal")

        offer = {
            "source_job_id": "55",
            "job": {"job_id": "ashby-ashby-good-55", "title": "Ashby Group Engineer"},
        }

        def fetch(site, **kwargs):
            _ = kwargs
            if site["site_id"] == "ashby-fails":
                raise RuntimeError("portal no disponible")
            return [offer]

        try:
            with patch(
                "jobpulse_v15.ui.messagebox.askyesno", return_value=True
            ), patch(
                "jobpulse_v15.ui.messagebox.showerror"
            ) as showerror, patch(
                "jobpulse_v15.ui.AshbyFetcher.fetch", side_effect=fetch
            ):
                manager.scrape_selected_group()
                self.assertIsNotNone(manager.scrape_thread)
                manager.scrape_thread.join(timeout=5)
                self.assertFalse(manager.scrape_thread.is_alive())
                self.root.after(150, self.root.quit)
                self.root.mainloop()

            self.assertIsNotNone(
                self.repository.get_job("ashby-ashby-good-55")
            )
            self.assertEqual(
                self.repository.list_site_runs("ashby-good", limit=1)[0][
                    "run_kind"
                ],
                "ashby",
            )
            self.assertEqual(
                self.repository.list_site_runs("ashby-fails", limit=1)[0][
                    "site_status"
                ],
                "error",
            )
            showerror.assert_called_once()
        finally:
            manager.close()

    def test_confirmed_lever_query_imports_in_background(self):
        self.repository.register_platform("lever", "Lever", "api")
        self.repository.register_site(
            "lever-cr",
            "lever",
            "Lever Careers CR",
            "https://jobs.lever.co/acme",
            company_name="Acme",
            country="CR",
        )
        manager = SourceManagerWindow(self.root, self.repository)
        manager.sites_table.selection_set("lever-cr")
        offer = {
            "source_job_id": "REQ-LEVER-1",
            "source_url": "https://jobs.lever.co/acme/REQ-LEVER-1",
            "job": {
                "job_id": "lever-acme-REQ-LEVER-1",
                "title": "Lever UI Engineer",
                "country": "CR",
            },
        }
        try:
            with patch(
                "jobpulse_v15.ui.messagebox.askyesno", return_value=True
            ), patch(
                "jobpulse_v15.ui.messagebox.showinfo"
            ), patch(
                "jobpulse_v15.ui.LeverFetcher.fetch", return_value=[offer]
            ) as fetch:
                manager.scrape_selected()
                self.assertIsNotNone(manager.scrape_thread)
                manager.scrape_thread.join(timeout=5)
                self.assertFalse(manager.scrape_thread.is_alive())
                self.root.after(150, self.root.quit)
                self.root.mainloop()

            fetch.assert_called_once()
            self.assertIsNotNone(
                self.repository.get_job("lever-acme-REQ-LEVER-1")
            )
            self.assertEqual(manager.status_var.get().split(":")[0], "Corrida 1")
        finally:
            manager.close()

    def test_group_query_supports_lever_boards(self):
        self.repository.register_platform("lever", "Lever", "api")
        for site_id in ("lever-good", "lever-fails"):
            self.repository.register_site(
                site_id,
                "lever",
                site_id,
                f"https://jobs.lever.co/{site_id}",
                run_groups=("directos",),
                site_config={"adapter": "lever"},
            )
        manager = SourceManagerWindow(self.root, self.repository)
        manager.sites_table.selection_set("lever-good", "lever-fails")
        manager._update_action_state()
        self.assertEqual(str(manager.scrape_group_button["state"]), "normal")

        offer = {
            "source_job_id": "55",
            "job": {"job_id": "lever-lever-good-55", "title": "Lever Group Engineer"},
        }

        def fetch(site, **kwargs):
            _ = kwargs
            if site["site_id"] == "lever-fails":
                raise RuntimeError("portal no disponible")
            return [offer]

        try:
            with patch(
                "jobpulse_v15.ui.messagebox.askyesno", return_value=True
            ), patch(
                "jobpulse_v15.ui.messagebox.showerror"
            ) as showerror, patch(
                "jobpulse_v15.ui.LeverFetcher.fetch", side_effect=fetch
            ):
                manager.scrape_selected_group()
                self.assertIsNotNone(manager.scrape_thread)
                manager.scrape_thread.join(timeout=5)
                self.assertFalse(manager.scrape_thread.is_alive())
                self.root.after(150, self.root.quit)
                self.root.mainloop()

            self.assertIsNotNone(
                self.repository.get_job("lever-lever-good-55")
            )
            self.assertEqual(
                self.repository.list_site_runs("lever-good", limit=1)[0][
                    "run_kind"
                ],
                "lever",
            )
            self.assertEqual(
                self.repository.list_site_runs("lever-fails", limit=1)[0][
                    "site_status"
                ],
                "error",
            )
            showerror.assert_called_once()
        finally:
            manager.close()

    def test_confirmed_recruitee_query_imports_in_background(self):
        self.repository.register_platform("recruitee", "Recruitee", "api")
        self.repository.register_site(
            "recruitee-cr",
            "recruitee",
            "Recruitee Careers CR",
            "https://acme.recruitee.com",
            company_name="Acme",
            country="CR",
        )
        manager = SourceManagerWindow(self.root, self.repository)
        manager.sites_table.selection_set("recruitee-cr")
        offer = {
            "source_job_id": "REQ-RECRUITEE-1",
            "source_url": "https://acme.recruitee.com/o/engineer",
            "job": {
                "job_id": "recruitee-acme-REQ-RECRUITEE-1",
                "title": "Recruitee UI Engineer",
                "country": "CR",
            },
        }
        try:
            with patch(
                "jobpulse_v15.ui.messagebox.askyesno", return_value=True
            ), patch(
                "jobpulse_v15.ui.messagebox.showinfo"
            ), patch(
                "jobpulse_v15.ui.RecruiteeFetcher.fetch", return_value=[offer]
            ) as fetch:
                manager.scrape_selected()
                self.assertIsNotNone(manager.scrape_thread)
                manager.scrape_thread.join(timeout=5)
                self.assertFalse(manager.scrape_thread.is_alive())
                self.root.after(150, self.root.quit)
                self.root.mainloop()

            fetch.assert_called_once()
            self.assertIsNotNone(
                self.repository.get_job("recruitee-acme-REQ-RECRUITEE-1")
            )
            self.assertEqual(manager.status_var.get().split(":")[0], "Corrida 1")
        finally:
            manager.close()


class TestManualJobStateUI(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "manual-state.db")
        self.database.initialize()
        self.repository = Repository(self.database)
        self.repository.register_platform("workday", "Workday", "api")
        self.repository.register_site(
            "intel-cr",
            "workday",
            "Intel Careers CR",
            "https://intel.wd1.myworkdayjobs.com/External",
            company_name="Intel",
            country="CR",
        )
        self.repository.save_job(
            {
                "job_id": "workday-REQ-1",
                "title": "Software Engineer",
                "company": "Intel",
                "country": "CR",
                "city": "San Jose",
                "location_raw": "San Jose, Costa Rica",
            },
            site_id="intel-cr",
            source_job_id="REQ-1",
            source_url="https://example.test/jobs/1",
        )
        self.root = tk.Tk()
        self.root.withdraw()

    def tearDown(self):
        self.root.update_idletasks()
        self.root.destroy()
        gc.collect()
        self.temp_dir.cleanup()

    def test_marking_job_applied_hides_it_by_default_and_can_be_reversed(self):
        window = JobPulseWindow(self.root, self.repository)
        self.root.update()
        self.assertEqual(window.jobs_table.get_children(), ("workday-REQ-1",))
        self.assertEqual(
            window.jobs_table.set("workday-REQ-1", "profile_compatibility"),
            "Sin analisis",
        )
        window.jobs_table.selection_set("workday-REQ-1")
        self.root.update()

        window.edit_selected_job_state()
        self.root.update()
        dialogs = [
            widget
            for widget in self.root.winfo_children()
            if isinstance(widget, tk.Toplevel)
            and widget.title() == "Estado manual de la vacante"
        ]
        self.assertEqual(len(dialogs), 1)
        dialog = dialogs[0]

        def descendants(widget):
            for child in widget.winfo_children():
                yield child
                yield from descendants(child)

        widgets = list(descendants(dialog))
        status = next(widget for widget in widgets if isinstance(widget, ttk.Combobox))
        status.set("Aplicada")
        save = next(
            widget
            for widget in widgets
            if isinstance(widget, ttk.Button) and widget.cget("text") == "Guardar"
        )
        save.invoke()
        self.root.update()

        state = self.repository.get_user_job_state("workday-REQ-1")
        self.assertEqual(state["user_status"], "applied")
        self.assertTrue(state["skip_automatic"])
        self.assertEqual(window.jobs_table.get_children(), ())

        window.include_manually_skipped_var.set(True)
        window.search_jobs()
        self.assertEqual(window.jobs_table.get_children(), ("workday-REQ-1",))
        values = window.jobs_table.item("workday-REQ-1", "values")
        self.assertEqual(values[-1], "Aplicada")

    def test_profile_compatibility_filter_limits_visible_jobs(self):
        window = JobPulseWindow(self.root, self.repository)
        self.assertEqual(window.jobs_table.get_children(), ("workday-REQ-1",))

        window.profile_compatibility_var.set("Coinciden requisitos obligatorios")
        window.search_jobs()
        self.assertEqual(window.jobs_table.get_children(), ())

        window.profile_compatibility_var.set("Sin analisis")
        window.search_jobs()
        self.assertEqual(window.jobs_table.get_children(), ("workday-REQ-1",))
        self.assertIn("1 vacante(s)", window.status_var.get())

    def test_data_review_filter_shows_incomplete_metadata_or_uncertain_analysis(self):
        window = JobPulseWindow(self.root, self.repository)
        self.repository.save_analysis(
            "workday-REQ-1",
            text_hash="review-filter-hash",
            extractor_version="rules-1",
            analysis_status="complete",
            field_statuses={"license": "not_mentioned"},
            requirements=[],
        )

        window.needs_review_only_var.set(True)
        window.search_jobs()
        self.assertEqual(window.jobs_table.get_children(), ())

        self.repository.save_analysis(
            "workday-REQ-1",
            text_hash="review-filter-partial",
            extractor_version="rules-1",
            analysis_status="partial",
            field_statuses={"license": "unknown"},
            requirements=[],
        )
        window.search_jobs()
        self.assertEqual(window.jobs_table.get_children(), ("workday-REQ-1",))
        self.assertEqual(
            window.jobs_table.set("workday-REQ-1", "data_review"), "Revisar (2)"
        )
        window.review_reason_var.set("Campo license desconocido")
        window.search_jobs()
        self.assertEqual(window.jobs_table.get_children(), ("workday-REQ-1",))
        window.jobs_table.selection_set("workday-REQ-1")
        self.root.update()
        detail = window.detail_text.get("1.0", "end")
        self.assertIn("Analisis parcial", detail)
        self.assertIn("Campo license desconocido", detail)

    def test_batch_analysis_cancel_does_not_process_visible_jobs(self):
        window = JobPulseWindow(self.root, self.repository)
        window.search_jobs()

        with patch(
            "jobpulse_v15.ui.messagebox.askyesno", return_value=False
        ) as confirm, patch.object(self.repository, "save_analysis") as save_analysis:
            window.analyze_visible_unanalyzed()

        confirm.assert_called_once()
        save_analysis.assert_not_called()
        self.assertIsNone(self.repository.get_current_analysis("workday-REQ-1"))

    def test_batch_analysis_processes_only_unanalyzed_visible_jobs_and_reports_errors(self):
        self.repository.save_job(
            {
                "job_id": "workday-REQ-1",
                "title": "Software Engineer",
                "company": "Intel",
                "country": "CR",
                "city": "San Jose",
                "location_raw": "San Jose, Costa Rica",
                "description_raw": "Advanced English required.",
            },
            site_id="intel-cr",
            source_job_id="REQ-1",
            source_url="https://example.test/jobs/1",
        )
        self.repository.save_job(
            {
                "job_id": "workday-REQ-2",
                "title": "QA Engineer",
                "company": "Intel",
                "country": "CR",
                "city": "San Jose",
                "location_raw": "San Jose, Costa Rica",
            },
            site_id="intel-cr",
            source_job_id="REQ-2",
            source_url="https://example.test/jobs/2",
        )
        self.repository.save_job(
            {
                "job_id": "workday-REQ-3",
                "title": "Product Engineer",
                "company": "Intel",
                "country": "CR",
                "city": "San Jose",
                "location_raw": "San Jose, Costa Rica",
            },
            site_id="intel-cr",
            source_job_id="REQ-3",
            source_url="https://example.test/jobs/3",
        )
        self.repository.save_analysis(
            "workday-REQ-3",
            text_hash="existing-analysis",
            extractor_version="rules-1",
            analysis_status="complete",
            field_statuses={},
            requirements=[],
        )
        window = JobPulseWindow(self.root, self.repository)
        window.search_jobs()
        window.jobs_table.selection_set("workday-REQ-2")
        self.root.update()

        with patch(
            "jobpulse_v15.ui.messagebox.askyesno", return_value=True
        ) as confirm, patch(
            "jobpulse_v15.ui.messagebox.showwarning"
        ) as showwarning, patch(
            "jobpulse_v15.ui.messagebox.showinfo"
        ) as showinfo:
            window.analyze_visible_unanalyzed()

        confirm.assert_called_once()
        self.assertIn("2 vacante(s)", confirm.call_args.args[1])
        self.assertIsNotNone(self.repository.get_current_analysis("workday-REQ-1"))
        self.assertEqual(
            self.repository.get_current_analysis("workday-REQ-3")["text_hash"],
            "existing-analysis",
        )
        self.assertIsNone(self.repository.get_current_analysis("workday-REQ-2"))
        self.assertEqual(window.selected_job_id, "workday-REQ-2")
        self.assertIn(
            "Aun no hay un analisis guardado.",
            window.detail_text.get("1.0", "end"),
        )
        showwarning.assert_called_once()
        self.assertIn("Analizadas: 1/2", showwarning.call_args.args[1])
        self.assertIn("no tiene texto para analizar", showwarning.call_args.args[1])
        showinfo.assert_not_called()

    def test_manual_data_edit_persists_without_changing_source_location(self):
        self.repository.save_job(
            {
                "job_id": "workday-REQ-1",
                "title": "Software Engineer",
                "location_raw": "San Jose, Costa Rica",
            },
            site_id="intel-cr",
            source_job_id="REQ-1",
        )
        window = JobPulseWindow(self.root, self.repository)
        window.jobs_table.selection_set("workday-REQ-1")
        self.root.update()
        window.edit_selected_job_data()
        self.root.update()
        dialogs = [
            widget
            for widget in self.root.winfo_children()
            if isinstance(widget, tk.Toplevel)
            and widget.title() == "Editar datos normalizados"
        ]
        self.assertEqual(len(dialogs), 1)
        dialog = dialogs[0]

        def descendants(widget):
            for child in widget.winfo_children():
                yield child
                yield from descendants(child)

        widgets = list(descendants(dialog))
        entries = [
            widget for widget in widgets if isinstance(widget, ttk.Entry)
        ]
        entries[3].delete(0, "end")
        entries[3].insert(0, "San Jose")
        entries[4].delete(0, "end")
        entries[4].insert(0, "San Jose")
        save = next(
            widget
            for widget in widgets
            if isinstance(widget, ttk.Button) and widget.cget("text") == "Guardar"
        )
        save.invoke()

        job = self.repository.get_job("workday-REQ-1")
        self.assertEqual(job["region"], "San Jose")
        self.assertEqual(job["city"], "San Jose")
        self.assertEqual(job["location_raw"], "San Jose, Costa Rica")

    def test_search_ui_can_order_publication_dates_and_reset_sort(self):
        self.repository.save_job(
            {
                "job_id": "workday-REQ-1",
                "title": "Software Engineer",
                "company": "Intel",
                "country": "CR",
                "city": "San Jose",
                "location_raw": "San Jose, Costa Rica",
                "date_published": "2026-10-01",
            },
            site_id="intel-cr",
            source_job_id="REQ-1",
        )
        self.repository.save_job(
            {
                "job_id": "workday-REQ-2",
                "title": "QA Engineer",
                "company": "Intel",
                "country": "CR",
                "city": "San Jose",
                "location_raw": "San Jose, Costa Rica",
                "date_published": "2026-10-22",
                "employment_type": "Full-time",
                "schedule": "Night shift",
                "salary_min": 5000,
                "salary_max": 6500,
                "salary_currency": "USD",
                "salary_period": "annual",
            },
            site_id="intel-cr",
            source_job_id="REQ-2",
        )
        window = JobPulseWindow(self.root, self.repository)

        window.sort_by_var.set("Fecha de publicacion")
        window.search_jobs()
        self.assertEqual(
            window.jobs_table.get_children(),
            ("workday-REQ-2", "workday-REQ-1"),
        )
        self.assertEqual(
            window.jobs_table.set("workday-REQ-2", "date_published"),
            "2026-10-22",
        )

        window.published_from_var.set("2026-10-20")
        window.published_to_var.set("2026-10-23")
        window.search_jobs()
        self.assertEqual(window.jobs_table.get_children(), ("workday-REQ-2",))

        window.published_from_var.set("")
        window.published_to_var.set("")
        window.employment_type_var.set("Full-time")
        window.schedule_var.set("Night shift")
        window.salary_currency_var.set("USD")
        window.salary_period_var.set("annual")
        window.salary_at_least_var.set("6000")
        window.salary_at_most_var.set("7000")
        window.search_jobs()
        self.assertEqual(window.jobs_table.get_children(), ("workday-REQ-2",))

        window.clear_filters()
        self.assertEqual(window.sort_by_var.get(), "Actividad reciente")
        self.assertEqual(window.published_from_var.get(), "")
        self.assertEqual(window.published_to_var.get(), "")
        self.assertEqual(window.employment_type_var.get(), "")
        self.assertEqual(window.schedule_var.get(), "")
        self.assertEqual(window.salary_currency_var.get(), "")
        self.assertEqual(window.salary_period_var.get(), "")
        self.assertEqual(window.salary_at_least_var.get(), "")
        self.assertEqual(window.salary_at_most_var.get(), "")

    def test_search_ui_reports_invalid_publication_date(self):
        window = JobPulseWindow(self.root, self.repository)
        window.published_from_var.set("2026/10/01")

        with patch("jobpulse_v15.ui.messagebox.showerror") as showerror:
            window.search_jobs()

        showerror.assert_called_once()
        self.assertIn("AAAA-MM-DD", showerror.call_args.args[1])

    def test_data_quality_summary_is_available_in_ui(self):
        window = JobPulseWindow(self.root, self.repository)

        with patch("jobpulse_v15.ui.messagebox.showinfo") as showinfo:
            window.show_data_quality_summary()

        showinfo.assert_called_once()
        report_text = showinfo.call_args.args[1]
        self.assertIn("Vacantes totales: 1", report_text)
        self.assertIn("Requieren revision: 1", report_text)
        self.assertIn("Grupos candidatos a duplicados: 0", report_text)
        self.assertIn("Sin analisis: 1", report_text)

    def test_potential_duplicate_candidates_are_available_in_ui(self):
        self.repository.save_job(
            {
                "job_id": "workday-REQ-2",
                "title": "software engineer!",
                "company": "Intel",
                "country": "CR",
                "city": "San José",
            },
            site_id="intel-cr",
            source_job_id="REQ-2",
        )
        window = JobPulseWindow(self.root, self.repository)

        with patch("jobpulse_v15.ui.messagebox.showinfo") as showinfo:
            window.show_potential_duplicates()

        showinfo.assert_called_once()
        report_text = showinfo.call_args.args[1]
        self.assertIn("Grupos candidatos: 1", report_text)
        self.assertIn("workday-REQ-1, workday-REQ-2", report_text)
        self.assertIn("no se fusionan vacantes", report_text)

    def test_database_backup_ui_prompts_and_creates_snapshot(self):
        window = JobPulseWindow(self.root, self.repository)
        destination = Path(self.temp_dir.name) / "ui-backup.db"
        with patch(
            "jobpulse_v15.ui.filedialog.asksaveasfilename",
            return_value=str(destination),
        ), patch("jobpulse_v15.ui.messagebox.showinfo") as showinfo:
            window.backup_database()

        self.assertTrue(destination.is_file())
        showinfo.assert_called_once()
        with closing(sqlite3.connect(destination)) as connection:
            self.assertEqual(
                connection.execute("PRAGMA integrity_check").fetchone()[0], "ok"
            )

    def test_export_search_results_saves_only_current_results(self):
        window = JobPulseWindow(self.root, self.repository)
        destination = Path(self.temp_dir.name) / "review.csv"
        with patch(
            "jobpulse_v15.ui.filedialog.asksaveasfilename",
            return_value=str(destination),
        ):
            window.export_search_results()
        self.assertTrue(destination.exists())
        content = destination.read_text(encoding="utf-8-sig")
        self.assertIn("workday-REQ-1", content)
        self.assertIn("Software Engineer", content)
        self.assertIn("https://example.test/jobs/1", content)
        self.assertIn("Sin analisis", content)
        self.assertNotIn("description_raw", content)

    def test_application_tracking_adds_and_displays_events(self):
        window = ApplicationTrackingWindow(
            self.root,
            self.repository,
            self.repository.get_job("workday-REQ-1"),
        )
        try:
            self.assertEqual(window.events_table.get_children(), ())
            window.status_var.set("Aplicada")
            window.applied_at_var.set("2026-10-23")
            window.next_action_at_var.set("2026-10-30")
            window.notes_text.insert("1.0", "Formulario enviado")
            window.save_event()

            rows = window.events_table.get_children()
            self.assertEqual(len(rows), 1)
            values = window.events_table.item(rows[0], "values")
            self.assertEqual(
                values[:4],
                ("Aplicada", "2026-10-23", "2026-10-30", "Formulario enviado"),
            )
            self.assertEqual(
                self.repository.list_applications("workday-REQ-1")[0][
                    "application_status"
                ],
                "applied",
            )
        finally:
            window.close()

    def test_candidate_profile_window_saves_structured_local_profile_data(self):
        window = CandidateProfileWindow(self.root, self.repository)
        try:
            window.category_var.set("experience")
            window.code_var.set("years_experience")
            window.value_var.set("Experiencia profesional")
            window.numeric_value_var.set("5")
            window.unit_var.set("years")
            window.notes_entry.insert(0, "Experiencia indicada en CV")
            window.save()

            facts = self.repository.list_candidate_profile_facts()
            self.assertEqual(len(facts), 1)
            self.assertEqual(facts[0]["numeric_value"], 5.0)
            self.assertEqual(facts[0]["unit"], "years")
            self.assertEqual(window.facts_table.get_children(), ("1",))
        finally:
            window.close()

    def test_profile_import_saves_only_explicitly_selected_suggestions(self):
        imported = CandidateProfileImport(
            source_name="candidate.txt",
            suggestions=(
                CandidateProfileSuggestion(
                    category="skill",
                    value_code="python",
                    value_text="Python",
                    evidence="Technical skills: Python.",
                ),
                CandidateProfileSuggestion(
                    category="language",
                    value_code="en",
                    value_text="English",
                    qualifier="advanced",
                    evidence="English - advanced.",
                ),
            ),
            warnings=("Revisa las sugerencias.",),
        )
        window = CandidateProfileImportWindow(
            self.root,
            self.repository,
            imported,
            on_import_complete=lambda: None,
        )
        try:
            window.suggestions_table.selection_set("1")
            with patch("jobpulse_v15.ui.messagebox.showinfo"):
                window.import_selected()

            facts = self.repository.list_candidate_profile_facts()
            self.assertEqual(len(facts), 1)
            self.assertEqual(facts[0]["category"], "language")
            self.assertEqual(facts[0]["value_code"], "en")
            self.assertEqual(facts[0]["origin"], "imported")
            self.assertEqual(facts[0]["import_source_name"], "candidate.txt")
        finally:
            if window.window.winfo_exists():
                window.close()

    def test_profile_import_suggestion_can_be_corrected_before_saving(self):
        imported = CandidateProfileImport(
            source_name="candidate.txt",
            suggestions=(
                CandidateProfileSuggestion(
                    category="skill",
                    value_code="python",
                    value_text="Python",
                    evidence="Technical skills: Python.",
                ),
            ),
            warnings=(),
        )
        window = CandidateProfileImportWindow(
            self.root,
            self.repository,
            imported,
            on_import_complete=lambda: None,
        )
        try:
            window.suggestions_table.selection_set("0")
            window.edit_selected()
            dialog = next(
                child
                for child in window.window.winfo_children()
                if isinstance(child, tk.Toplevel)
                and child.title() == "Editar sugerencia del CV"
            )

            def descendants(widget):
                for child in widget.winfo_children():
                    yield child
                    yield from descendants(child)

            value_entry = next(
                widget
                for widget in descendants(dialog)
                if isinstance(widget, ttk.Entry)
                and int(widget.grid_info().get("row", -1)) == 2
            )
            value_entry.delete(0, "end")
            value_entry.insert(0, "Python 3")
            apply_button = next(
                widget
                for widget in descendants(dialog)
                if isinstance(widget, ttk.Button)
                and widget.cget("text") == "Aplicar cambios"
            )
            apply_button.invoke()

            self.assertEqual(window.suggestions[0].value_text, "Python 3")
            self.assertEqual(
                window.suggestions_table.set("0", "value"), "Python 3"
            )
            window.suggestions_table.selection_set("0")
            with patch("jobpulse_v15.ui.messagebox.showinfo"):
                window.import_selected()
            self.assertEqual(
                self.repository.list_candidate_profile_facts()[0]["value_text"],
                "Python 3",
            )
        finally:
            if window.window.winfo_exists():
                window.close()

    def test_profile_import_file_picker_only_previews_before_user_selection(self):
        profile_window = CandidateProfileWindow(self.root, self.repository)
        path = Path(self.temp_dir.name) / "candidate.txt"
        path.write_text("Technical skills: Python.", encoding="utf-8")
        try:
            with patch(
                "jobpulse_v15.ui.filedialog.askopenfilename", return_value=str(path)
            ):
                profile_window.import_cv()

            review_windows = [
                child
                for child in profile_window.window.winfo_children()
                if isinstance(child, tk.Toplevel)
                and child.title().startswith("Revisar importacion")
            ]
            self.assertEqual(len(review_windows), 1)
            self.assertEqual(self.repository.list_candidate_profile_facts(), [])
            review_windows[0].destroy()
            self.root.update()
        finally:
            profile_window.close()
            self.root.update()

    def test_profile_import_requires_confirmation_to_replace_existing_fact(self):
        self.repository.save_candidate_profile_fact("skill", "python", "Python 2")
        imported = CandidateProfileImport(
            source_name="candidate.txt",
            suggestions=(
                CandidateProfileSuggestion(
                    category="skill",
                    value_code="python",
                    value_text="Python 3",
                    evidence="Skills: Python.",
                ),
            ),
            warnings=(),
        )
        window = CandidateProfileImportWindow(
            self.root,
            self.repository,
            imported,
            on_import_complete=lambda: None,
        )
        window.suggestions_table.selection_set("0")
        try:
            with patch(
                "jobpulse_v15.ui.messagebox.askyesno", return_value=False
            ) as confirm:
                window.import_selected()

            confirm.assert_called_once()
            fact = self.repository.list_candidate_profile_facts()[0]
            self.assertEqual(fact["value_text"], "Python 2")
        finally:
            window.close()

    def test_profile_window_reverts_latest_import_after_confirmation(self):
        self.repository.import_candidate_profile_facts(
            "candidate.txt",
            [{"category": "skill", "value_code": "python", "value_text": "Python"}],
        )
        window = CandidateProfileWindow(self.root, self.repository)
        try:
            with patch(
                "jobpulse_v15.ui.messagebox.askyesno", return_value=True
            ), patch("jobpulse_v15.ui.messagebox.showinfo") as showinfo:
                window.revert_last_import()

            self.assertEqual(self.repository.list_candidate_profile_facts(), [])
            self.assertEqual(self.repository.list_candidate_profile_imports(), [])
            self.assertEqual(window.facts_table.get_children(), ())
            showinfo.assert_called_once()
        finally:
            window.close()

    def test_job_window_opens_comparison_for_selected_job(self):
        window = JobPulseWindow(self.root, self.repository)
        window.jobs_table.selection_set("workday-REQ-1")
        self.root.update()

        window.compare_selected_profile()
        dialogs = [
            child
            for child in self.root.winfo_children()
            if isinstance(child, tk.Toplevel)
            and child.title() == "Comparacion con mi perfil"
        ]
        self.assertEqual(len(dialogs), 1)
        texts = [
            widget
            for widget in dialogs[0].winfo_children()[0].winfo_children()
            if isinstance(widget, tk.Text)
        ]
        self.assertIn("aun no tiene un analisis", texts[0].get("1.0", "end"))
        dialogs[0].destroy()


if __name__ == "__main__":
    unittest.main()
