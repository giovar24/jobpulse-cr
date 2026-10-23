import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from jobpulse_v15.db import Database
from jobpulse_v15.repository import Repository


class TestRepository(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "test.db")
        self.database.initialize()
        self.repository = Repository(self.database)
        self.repository.register_platform(
            "workday", "Workday", "api", default_language="en"
        )
        self.repository.register_site(
            "intel-cr",
            "workday",
            "Intel Careers Costa Rica",
            "https://example.test/jobs",
            company_name="Intel",
            country="CR",
            run_groups=("directos", "all"),
            site_config={"board": "External"},
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_site_selection_respects_enabled_and_run_group(self):
        self.repository.register_site(
            "paused-site",
            "workday",
            "Paused site",
            "https://paused.example.test/jobs",
            run_groups=("directos",),
            enabled=False,
        )
        selected = self.repository.select_sites("directos")
        self.assertEqual([site["site_id"] for site in selected], ["intel-cr"])
        self.assertEqual(self.repository.select_sites("boards"), [])

        self.assertTrue(self.repository.set_site_enabled("paused-site", True))
        self.assertEqual(
            {site["site_id"] for site in self.repository.select_sites("directos")},
            {"intel-cr", "paused-site"},
        )
        self.assertFalse(self.repository.set_site_enabled("missing", True))

    def test_list_site_runs_returns_only_recent_results_for_requested_site(self):
        self.repository.register_site(
            "other-site",
            "workday",
            "Other site",
            "https://other.example.test/jobs",
        )
        for site_id, kind, found in (
            ("intel-cr", "workday", 2),
            ("other-site", "workday", 8),
            ("intel-cr", "json_import", 3),
        ):
            run_id = self.repository.start_site_run(site_id, kind)
            self.repository.record_site_result(
                run_id,
                site_id,
                jobs_found=found,
                jobs_valid=found,
                jobs_new=found,
                jobs_duplicate=0,
            )
            self.repository.finish_run(run_id)

        runs = self.repository.list_site_runs("intel-cr", limit=1)

        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["run_kind"], "json_import")
        self.assertEqual(runs[0]["jobs_found"], 3)
        self.assertEqual(runs[0]["site_status"], "ok")

    def test_site_config_rejects_credentials(self):
        with self.assertRaisesRegex(ValueError, "credenciales"):
            self.repository.register_site(
                "unsafe-site",
                "workday",
                "Unsafe site",
                "https://unsafe.example.test/jobs",
                site_config={"nested": {"password": "do-not-store"}},
            )

    def test_data_quality_summary_returns_zeroes_for_empty_database(self):
        summary = self.repository.get_data_quality_summary()

        self.assertEqual(summary["total_jobs"], 0)
        self.assertEqual(summary["jobs_needing_review"], 0)
        self.assertTrue(all(value == 0 for value in summary.values()))

    def test_potential_duplicate_groups_normalize_case_accents_and_punctuation(self):
        for job_id, title in (
            ("job-duplicate-1", "Ingeniero de Calidad"),
            ("job-duplicate-2", "  INGENIERO de calidad! "),
        ):
            self.repository.save_job(
                {
                    "job_id": job_id,
                    "title": title,
                    "company": "Empresa Ejemplo",
                    "country": "CR",
                    "city": "San José",
                },
                site_id="intel-cr",
                source_job_id=job_id,
            )
        self.repository.save_job(
            {
                "job_id": "job-distinct-city",
                "title": "Ingeniero de Calidad",
                "company": "Empresa Ejemplo",
                "country": "CR",
                "city": "Heredia",
            },
            site_id="intel-cr",
            source_job_id="job-distinct-city",
        )

        candidates = self.repository.get_potential_duplicate_groups()

        self.assertEqual(candidates["total_groups"], 1)
        self.assertEqual(candidates["groups"][0]["count"], 2)
        self.assertEqual(
            candidates["groups"][0]["job_ids"],
            ["job-duplicate-1", "job-duplicate-2"],
        )
        self.assertEqual(
            self.repository.get_data_quality_summary()["potential_duplicate_groups"],
            1,
        )

    def test_potential_duplicate_group_limit_is_validated(self):
        with self.assertRaisesRegex(ValueError, "limit"):
            self.repository.get_potential_duplicate_groups(limit=0)
        with self.assertRaisesRegex(ValueError, "500"):
            self.repository.get_potential_duplicate_groups(limit=501)

    def test_job_upsert_preserves_text_when_source_omits_it(self):
        self.repository.save_job(
            {
                "job_id": "job-1",
                "title": "Power Integrity Engineer",
                "description_raw": "Bachelor degree required.",
                "posting_language": "en",
                "posting_language_origin": "detected",
                "work_mode": "hybrid",
            },
            site_id="intel-cr",
            source_job_id="JR-1",
            source_url="https://example.test/job/1",
            raw_payload={"id": "JR-1"},
            content_hash="hash-1",
        )
        self.repository.save_job(
            {"job_id": "job-1", "title": "Power Integrity Engineer II"},
            site_id="intel-cr",
            source_job_id="JR-1",
            content_hash="hash-2",
        )

        job = self.repository.get_job("job-1")
        self.assertIsNotNone(job)
        self.assertEqual(job["title"], "Power Integrity Engineer II")
        self.assertEqual(job["description_raw"], "Bachelor degree required.")
        self.assertEqual(job["posting_language"], "en")
        self.assertEqual(job["posting_language_origin"], "detected")
        self.assertEqual(job["work_mode"], "hybrid")

        with self.database.connection() as connection:
            source = connection.execute(
                """
                SELECT raw_payload_json, content_hash
                FROM job_source_records
                WHERE site_id = 'intel-cr' AND source_job_id = 'JR-1'
                """
            ).fetchone()
            memory = connection.execute(
                """
                SELECT content_hash FROM source_job_memory
                WHERE site_id = 'intel-cr' AND source_job_id = 'JR-1'
                """
            ).fetchone()
        self.assertEqual(json.loads(source["raw_payload_json"]), {"id": "JR-1"})
        self.assertEqual(source["content_hash"], "hash-2")
        self.assertEqual(memory["content_hash"], "hash-2")

    def test_manual_job_overrides_survive_source_updates_and_can_be_restored(self):
        self.add_job("job-manual-override")
        self.repository.update_job_manual_overrides(
            "job-manual-override",
            {"company": "Intel Costa Rica", "city": "San Jose"},
        )

        self.repository.save_job(
            {
                "job_id": "job-manual-override",
                "title": "Engineer updated",
                "company": "Intel",
                "country": "CR",
                "city": "Heredia",
                "work_mode": "remote",
            },
            site_id="intel-cr",
            source_job_id="job-manual-override",
            raw_payload={"listing": "latest"},
        )

        job = self.repository.get_job("job-manual-override")
        self.assertEqual(job["title"], "Engineer updated")
        self.assertEqual(job["company"], "Intel Costa Rica")
        self.assertEqual(job["city"], "San Jose")
        self.assertEqual(job["work_mode"], "remote")
        self.assertEqual(
            self.repository.clear_job_manual_overrides("job-manual-override"), 2
        )
        restored = self.repository.get_job("job-manual-override")
        self.assertEqual(restored["company"], "Intel")
        self.assertEqual(restored["city"], "Heredia")

    def test_manual_job_overrides_validate_fields_and_values(self):
        self.add_job("job-manual-validation")
        with self.assertRaisesRegex(ValueError, "Campos no editables"):
            self.repository.update_job_manual_overrides(
                "job-manual-validation", {"location_raw": "edited"}
            )
        with self.assertRaisesRegex(ValueError, "title es obligatorio"):
            self.repository.update_job_manual_overrides(
                "job-manual-validation", {"title": " "}
            )
        with self.assertRaisesRegex(ValueError, "work_mode"):
            self.repository.update_job_manual_overrides(
                "job-manual-validation", {"work_mode": "flexible"}
            )

    def test_save_job_validates_required_fields_and_salary_range(self):
        with self.assertRaisesRegex(ValueError, "job.title"):
            self.repository.save_job(
                {"job_id": "job-2", "title": ""},
                site_id="intel-cr",
                source_job_id="JR-2",
            )

        with self.assertRaisesRegex(ValueError, "salary_min"):
            self.repository.save_job(
                {
                    "job_id": "job-3",
                    "title": "Engineer",
                    "salary_min": 5000,
                    "salary_max": 1000,
                },
                site_id="intel-cr",
                source_job_id="JR-3",
            )

        self.assertIsNone(self.repository.get_job("job-2"))
        self.assertIsNone(self.repository.get_job("job-3"))

    def test_save_job_rejects_unknown_site_without_partial_insert(self):
        with self.assertRaisesRegex(ValueError, "no existe"):
            self.repository.save_job(
                {"job_id": "job-4", "title": "Engineer"},
                site_id="missing-site",
                source_job_id="JR-4",
            )
        self.assertIsNone(self.repository.get_job("job-4"))

    def add_job(self, job_id, *, language="en", city="San Jose"):
        self.repository.save_job(
            {
                "job_id": job_id,
                "title": f"Engineer {job_id}",
                "company": "Intel",
                "country": "CR",
                "city": city,
                "work_mode": "hybrid",
                "posting_language": language,
            },
            site_id="intel-cr",
            source_job_id=job_id,
            source_url=f"https://example.test/jobs/{job_id}",
        )

    @staticmethod
    def bachelor_requirement(**overrides):
        requirement = {
            "category": "education",
            "value_code": "bachelor",
            "value_text": "Bachelor's degree",
            "requirement_level": "required",
            "education_status": "completed",
            "evidence": "Bachelor's degree required.",
            "origin": "rule",
            "confidence": 0.98,
        }
        requirement.update(overrides)
        return requirement

    @staticmethod
    def english_requirement(**overrides):
        requirement = {
            "category": "language",
            "value_code": "en",
            "value_text": "English",
            "qualifier": "advanced",
            "requirement_level": "required",
            "evidence": "Advanced English required.",
            "origin": "local_ai",
            "confidence": 0.91,
        }
        requirement.update(overrides)
        return requirement

    def test_save_and_read_versioned_analysis_with_field_states(self):
        self.add_job("job-analysis")
        analysis_id = self.repository.save_analysis(
            "job-analysis",
            text_hash="description-hash-1",
            extractor_version="rules-1",
            analysis_status="complete",
            source_site_id="intel-cr",
            source_config_hash="site-config-hash",
            field_statuses={
                "education": "found",
                "language": "found",
                "license": "not_mentioned",
            },
            requirements=[
                self.bachelor_requirement(),
                self.english_requirement(),
            ],
        )

        analysis = self.repository.get_current_analysis("job-analysis")
        self.assertEqual(analysis["analysis_id"], analysis_id)
        self.assertEqual(analysis["source_site_id"], "intel-cr")
        self.assertEqual(analysis["source_config_hash"], "site-config-hash")
        self.assertEqual(analysis["field_statuses"]["license"], "not_mentioned")
        self.assertEqual(len(analysis["requirements"]), 2)

    def test_profile_comparison_checks_codes_education_and_experience(self):
        self.add_job("job-profile")
        experience = {
            "category": "experience",
            "value_code": "years_experience",
            "value_text": "3+ years of experience",
            "requirement_level": "required",
            "minimum": 3,
            "unit": "years",
            "evidence": "3+ years of experience required.",
            "origin": "rule",
            "confidence": 0.9,
        }
        self.repository.save_analysis(
            "job-profile",
            text_hash="profile-hash",
            extractor_version="rules-1",
            analysis_status="complete",
            field_statuses={
                "education": "found",
                "language": "found",
                "experience": "found",
            },
            requirements=[
                self.bachelor_requirement(),
                self.english_requirement(),
                experience,
            ],
        )
        self.repository.save_candidate_profile_fact(
            "education",
            "BACHELOR",
            "Bachillerato universitario",
            education_status="completed",
        )
        self.repository.save_candidate_profile_fact(
            "language", "en", "Ingles", qualifier="advanced"
        )
        self.repository.save_candidate_profile_fact(
            "experience",
            "years_experience",
            "Experiencia profesional",
            numeric_value=2,
            unit="years",
        )

        comparison = self.repository.compare_job_to_candidate_profile("job-profile")

        self.assertEqual(
            comparison["counts"],
            {
                "matched": 2,
                "not_registered": 0,
                "below_requirement": 1,
                "needs_review": 0,
            },
        )
        self.assertEqual(
            {
                item["category"]: item["comparison_status"]
                for item in comparison["requirements"]
            },
            {
                "education": "matched",
                "language": "matched",
                "experience": "below_requirement",
            },
        )
        batch = self.repository.compare_jobs_to_candidate_profile(
            [{"job_id": "job-profile", "title": "Engineer"}]
        )
        self.assertEqual(
            batch["job-profile"]["counts"],
            comparison["counts"],
        )
        self.assertEqual(
            self.repository.candidate_profile_status(comparison),
            "below_requirement",
        )

    def test_profile_comparison_marks_unregistered_and_inferred_as_review(self):
        self.add_job("job-profile-review")
        inferred = self.english_requirement(
            requirement_level="inferred",
            evidence="English may be beneficial.",
        )
        self.repository.save_analysis(
            "job-profile-review",
            text_hash="profile-review-hash",
            extractor_version="rules-1",
            analysis_status="complete",
            field_statuses={"language": "found", "education": "found"},
            requirements=[
                inferred,
                self.bachelor_requirement(value_code="master"),
            ],
        )

        comparison = self.repository.compare_job_to_candidate_profile(
            "job-profile-review"
        )

        self.assertEqual(comparison["counts"]["needs_review"], 1)
        self.assertEqual(comparison["counts"]["not_registered"], 1)
        self.assertEqual(
            self.repository.candidate_profile_status(comparison), "needs_review"
        )
        self.assertEqual(
            {
                item["requirement_level"]: item["comparison_status"]
                for item in comparison["requirements"]
            },
            {"inferred": "needs_review", "required": "not_registered"},
        )

    def test_candidate_profile_upserts_by_case_insensitive_code_and_validates(self):
        self.repository.save_candidate_profile_fact("skill", "Python", "Python")
        self.repository.save_candidate_profile_fact(
            "skill", "python", "Python 3", notes="Actualizado"
        )

        facts = self.repository.list_candidate_profile_facts()
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0]["value_text"], "Python 3")
        self.assertEqual(facts[0]["notes"], "Actualizado")
        with self.assertRaisesRegex(ValueError, "solo aplica a experience"):
            self.repository.save_candidate_profile_fact(
                "skill", "test", "Test", numeric_value=2
            )

    def test_candidate_profile_bulk_save_is_atomic(self):
        with self.assertRaisesRegex(ValueError, "Categoria"):
            self.repository.save_candidate_profile_facts(
                [
                    {
                        "category": "skill",
                        "value_code": "python",
                        "value_text": "Python",
                    },
                    {
                        "category": "invalid",
                        "value_code": "unknown",
                        "value_text": "Unknown",
                    },
                ]
            )

        self.assertEqual(self.repository.list_candidate_profile_facts(), [])

    def test_candidate_profile_import_rejects_duplicate_codes_atomically(self):
        with self.assertRaisesRegex(ValueError, "duplicados"):
            self.repository.import_candidate_profile_facts(
                "resume.txt",
                [
                    {
                        "category": "skill",
                        "value_code": "Python",
                        "value_text": "Python",
                    },
                    {
                        "category": "skill",
                        "value_code": "python",
                        "value_text": "Python 3",
                    },
                ],
            )

        self.assertEqual(self.repository.list_candidate_profile_facts(), [])
        self.assertEqual(self.repository.list_candidate_profile_imports(), [])

    def test_import_revert_restores_previous_manual_profile_fact(self):
        self.repository.save_candidate_profile_fact(
            "skill", "python", "Python 2", notes="Ingresado manualmente"
        )

        import_id = self.repository.import_candidate_profile_facts(
            "resume.docx",
            [
                {
                    "category": "skill",
                    "value_code": "python",
                    "value_text": "Python 3",
                    "notes": "Importado",
                }
            ],
        )
        imported = self.repository.list_candidate_profile_facts()[0]
        self.assertEqual(imported["profile_import_id"], import_id)
        self.assertEqual(imported["origin"], "imported")
        self.assertEqual(imported["import_source_name"], "resume.docx")

        reverted = self.repository.revert_last_candidate_profile_import()

        self.assertEqual(reverted["restored"], 1)
        self.assertEqual(reverted["removed"], 0)
        fact = self.repository.list_candidate_profile_facts()[0]
        self.assertEqual(fact["value_text"], "Python 2")
        self.assertEqual(fact["notes"], "Ingresado manualmente")
        self.assertEqual(fact["origin"], "manual")
        self.assertIsNone(fact["source_import_id"])
        self.assertEqual(self.repository.list_candidate_profile_imports(), [])

    def test_import_revert_removes_new_facts_but_preserves_later_manual_edits(self):
        self.repository.import_candidate_profile_facts(
            "resume.pdf",
            [
                {
                    "category": "skill",
                    "value_code": "python",
                    "value_text": "Python",
                },
                {
                    "category": "skill",
                    "value_code": "sql",
                    "value_text": "SQL",
                },
            ],
        )
        self.repository.save_candidate_profile_fact(
            "skill", "python", "Python 3", notes="Corregido manualmente"
        )

        reverted = self.repository.revert_last_candidate_profile_import()

        self.assertEqual(reverted["removed"], 1)
        self.assertEqual(reverted["restored"], 0)
        self.assertEqual(reverted["preserved_manual"], 1)
        facts = self.repository.list_candidate_profile_facts()
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0]["value_code"], "python")
        self.assertEqual(facts[0]["value_text"], "Python 3")
        self.assertEqual(facts[0]["origin"], "manual")

    def test_reverting_imports_in_reverse_order_restores_each_snapshot(self):
        self.repository.import_candidate_profile_facts(
            "first.txt",
            [{"category": "skill", "value_code": "python", "value_text": "Python 2"}],
        )
        self.repository.import_candidate_profile_facts(
            "second.txt",
            [{"category": "skill", "value_code": "python", "value_text": "Python 3"}],
        )

        second = self.repository.revert_last_candidate_profile_import()
        self.assertEqual(second["source_name"], "second.txt")
        self.assertEqual(
            self.repository.list_candidate_profile_facts()[0]["value_text"],
            "Python 2",
        )
        first = self.repository.revert_last_candidate_profile_import()
        self.assertEqual(first["source_name"], "first.txt")
        self.assertEqual(self.repository.list_candidate_profile_facts(), [])

    def test_profile_status_distinguishes_unanalyzed_and_no_required_jobs(self):
        self.add_job("job-no-profile-analysis")
        self.add_job("job-no-required")
        self.add_job("job-with-preferred")
        self.repository.save_analysis(
            "job-no-required",
            text_hash="preferred-only-hash",
            extractor_version="rules-1",
            analysis_status="complete",
            field_statuses={"language": "found"},
            requirements=[
                self.english_requirement(requirement_level="preferred")
            ],
        )
        self.repository.save_analysis(
            "job-with-preferred",
            text_hash="required-and-preferred-hash",
            extractor_version="rules-1",
            analysis_status="complete",
            field_statuses={"language": "found", "skill": "found"},
            requirements=[
                self.english_requirement(),
                {
                    "category": "skill",
                    "value_code": "python",
                    "value_text": "Python",
                    "requirement_level": "preferred",
                    "evidence": "Python experience is preferred.",
                    "origin": "rule",
                },
            ],
        )
        self.repository.save_candidate_profile_fact(
            "language", "en", "English", qualifier="advanced"
        )

        comparisons = self.repository.compare_jobs_to_candidate_profile(
            [
                {"job_id": "job-no-profile-analysis"},
                {"job_id": "job-no-required"},
                {"job_id": "job-with-preferred"},
            ]
        )

        self.assertEqual(
            self.repository.candidate_profile_status(
                comparisons["job-no-profile-analysis"]
            ),
            "not_analyzed",
        )
        self.assertEqual(
            self.repository.candidate_profile_status(
                comparisons["job-no-required"]
            ),
            "no_required",
        )
        self.assertEqual(
            self.repository.candidate_profile_status(
                comparisons["job-with-preferred"]
            ),
            "matched",
        )

    def test_reanalysis_replaces_current_analysis_and_keeps_history(self):
        self.add_job("job-reanalysis")
        old_id = self.repository.save_analysis(
            "job-reanalysis",
            text_hash="old-hash",
            extractor_version="rules-1",
            analysis_status="complete",
            field_statuses={"education": "found"},
            requirements=[self.bachelor_requirement()],
        )
        new_id = self.repository.save_analysis(
            "job-reanalysis",
            text_hash="new-hash",
            extractor_version="rules-2",
            analysis_status="partial",
            field_statuses={"language": "unknown"},
            requirements=[],
        )

        self.assertNotEqual(old_id, new_id)
        self.assertEqual(
            self.repository.get_current_analysis("job-reanalysis")["analysis_id"],
            new_id,
        )
        with self.database.connection() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM job_analysis WHERE job_id = 'job-reanalysis'"
                ).fetchone()[0],
                2,
            )

    def test_same_analysis_version_is_updated_without_duplicate_history(self):
        self.add_job("job-same-version")
        first_id = self.repository.save_analysis(
            "job-same-version",
            text_hash="same-hash",
            extractor_version="rules-1",
            analysis_status="partial",
            field_statuses={"language": "unknown"},
            requirements=[],
        )
        second_id = self.repository.save_analysis(
            "job-same-version",
            text_hash="same-hash",
            extractor_version="rules-1",
            analysis_status="complete",
            field_statuses={"education": "found"},
            requirements=[self.bachelor_requirement()],
        )
        self.assertEqual(first_id, second_id)
        self.assertEqual(
            len(self.repository.get_current_analysis("job-same-version")["requirements"]),
            1,
        )

    def test_analysis_validation_preserves_previous_current_data(self):
        self.add_job("job-validation")
        original_id = self.repository.save_analysis(
            "job-validation",
            text_hash="original",
            extractor_version="rules-1",
            analysis_status="complete",
            field_statuses={"education": "found"},
            requirements=[self.bachelor_requirement()],
        )
        with self.assertRaisesRegex(ValueError, "evidence"):
            self.repository.save_analysis(
                "job-validation",
                text_hash="invalid",
                extractor_version="rules-1",
                analysis_status="complete",
                field_statuses={"education": "found"},
                requirements=[self.bachelor_requirement(evidence=" ")],
            )
        self.assertEqual(
            self.repository.get_current_analysis("job-validation")["analysis_id"],
            original_id,
        )

    def test_analysis_distinguishes_missing_from_unknown_and_rejects_false_facts(self):
        self.add_job("job-field-states")
        with self.assertRaisesRegex(ValueError, "not_mentioned"):
            self.repository.save_analysis(
                "job-field-states",
                text_hash="bad-state",
                extractor_version="rules-1",
                analysis_status="partial",
                field_statuses={"license": "not_mentioned"},
                requirements=[
                    self.bachelor_requirement(
                        category="license",
                        value_code="b",
                        education_status=None,
                    )
                ],
            )
        self.repository.save_analysis(
            "job-field-states",
            text_hash="good-state",
            extractor_version="rules-1",
            analysis_status="partial",
            field_statuses={"license": "unknown", "language": "not_mentioned"},
            requirements=[],
        )
        states = self.repository.get_current_analysis("job-field-states")[
            "field_statuses"
        ]
        self.assertEqual(states["license"], "unknown")
        self.assertEqual(states["language"], "not_mentioned")

    def test_search_filters_jobs_by_required_structured_data(self):
        self.add_job("job-match")
        self.add_job("job-preferred", city="Heredia")
        self.repository.save_analysis(
            "job-match",
            text_hash="match-hash",
            extractor_version="rules-1",
            analysis_status="complete",
            field_statuses={"education": "found", "language": "found"},
            requirements=[
                self.bachelor_requirement(),
                self.english_requirement(),
            ],
        )
        self.repository.save_analysis(
            "job-preferred",
            text_hash="preferred-hash",
            extractor_version="rules-1",
            analysis_status="partial",
            field_statuses={"language": "found"},
            requirements=[
                self.english_requirement(requirement_level="preferred")
            ],
        )

        criteria = [
            {"category": "language", "value_code": "en"},
            {
                "category": "education",
                "value_code": "bachelor",
                "education_status": "completed",
            },
        ]
        matches = self.repository.search_jobs(
            requirements=criteria,
            filters={"country": "CR", "work_mode": "hybrid"},
        )
        self.assertEqual([job["job_id"] for job in matches], ["job-match"])
        include_preferred = self.repository.search_jobs(
            requirements=[{"category": "language", "value_code": "en"}],
            requirement_levels=("required", "preferred"),
        )
        self.assertEqual(
            {job["job_id"] for job in include_preferred},
            {"job-match", "job-preferred"},
        )

    def test_search_review_queue_includes_unanalyzed_and_uncertain_jobs(self):
        self.add_job("job-no-analysis")
        self.add_job("job-partial")
        self.add_job("job-complete")
        self.repository.save_job(
            {
                "job_id": "job-missing-company",
                "title": "Engineer",
                "country": "CR",
                "city": "San Jose",
            },
            site_id="intel-cr",
            source_job_id="missing-company",
        )
        self.repository.save_job(
            {
                "job_id": "job-unparsed-location",
                "title": "Engineer",
                "company": "Example",
                "country": "CR",
                "location_raw": "San Jose, Costa Rica",
            },
            site_id="intel-cr",
            source_job_id="unparsed-location",
        )
        self.repository.save_analysis(
            "job-partial",
            text_hash="partial-hash",
            extractor_version="rules-1",
            analysis_status="partial",
            field_statuses={"license": "unknown"},
            requirements=[],
        )
        self.repository.save_analysis(
            "job-complete",
            text_hash="complete-hash",
            extractor_version="rules-1",
            analysis_status="complete",
            field_statuses={"license": "not_mentioned"},
            requirements=[],
        )
        for job_id in ("job-missing-company", "job-unparsed-location"):
            self.repository.save_analysis(
                job_id,
                text_hash=f"{job_id}-complete",
                extractor_version="rules-1",
                analysis_status="complete",
                field_statuses={"license": "not_mentioned"},
                requirements=[],
            )

        jobs = self.repository.search_jobs(needs_review_only=True)

        self.assertEqual(
            {job["job_id"] for job in jobs},
            {
                "job-no-analysis",
                "job-partial",
                "job-missing-company",
                "job-unparsed-location",
            },
        )
        reasons_by_job = {
            job["job_id"]: job["review_reasons"] for job in jobs
        }
        self.assertEqual(
            reasons_by_job["job-no-analysis"], ["analysis_missing"]
        )
        self.assertEqual(
            reasons_by_job["job-partial"],
            ["analysis_partial", "unknown_field:license"],
        )
        self.assertEqual(
            reasons_by_job["job-missing-company"], ["missing_company"]
        )
        self.assertEqual(
            reasons_by_job["job-unparsed-location"],
            ["location_not_normalized"],
        )
        summary = self.repository.get_data_quality_summary()
        self.assertEqual(
            summary,
            {
                "total_jobs": 5,
                "missing_company": 1,
                "missing_country": 0,
                "location_not_normalized": 1,
                "analysis_missing": 1,
                "analysis_pending": 0,
                "analysis_partial": 1,
                "analysis_error": 0,
                "unknown_fields": 1,
                "ambiguous_fields": 0,
                "jobs_needing_review": 4,
                "potential_duplicate_groups": 0,
            },
        )
        self.assertEqual(len(self.repository.search_jobs()), 5)
        self.assertEqual(
            {
                job["job_id"]
                for job in self.repository.search_jobs(
                    review_reason="missing_company"
                )
            },
            {"job-missing-company"},
        )
        self.assertEqual(
            {
                job["job_id"]
                for job in self.repository.search_jobs(
                    review_reason="unknown_field:license"
                )
            },
            {"job-partial"},
        )
        with self.assertRaisesRegex(ValueError, "booleano"):
            self.repository.search_jobs(needs_review_only="true")
        with self.assertRaisesRegex(ValueError, "motivo permitido"):
            self.repository.search_jobs(review_reason="missing_company OR 1=1")

    def test_search_rejects_unsafe_or_invalid_filters(self):
        with self.assertRaisesRegex(ValueError, "Filtro no soportado"):
            self.repository.search_jobs(filters={"title; DROP TABLE jobs": "x"})
        with self.assertRaisesRegex(ValueError, "Categoria"):
            self.repository.search_jobs(
                requirements=[{"category": "language!", "value_code": "en"}]
            )
        with self.assertRaisesRegex(ValueError, "sort_by"):
            self.repository.search_jobs(sort_by="title")

    def test_search_can_sort_by_publication_date_with_undated_jobs_last(self):
        for job_id, published in (
            ("job-old", "2026-10-01"),
            ("job-new", "2026-10-20"),
            ("job-undated", None),
        ):
            self.repository.save_job(
                {
                    "job_id": job_id,
                    "title": f"Engineer {job_id}",
                    "company": "Intel",
                    "country": "CR",
                    "date_published": published,
                },
                site_id="intel-cr",
                source_job_id=job_id,
            )

        jobs = self.repository.search_jobs(sort_by="date_published")

        self.assertEqual(
            [job["job_id"] for job in jobs],
            ["job-new", "job-old", "job-undated"],
        )

    def test_search_filters_publication_date_inclusive_and_validates_range(self):
        for job_id, published in (
            ("job-before", "2026-10-01"),
            ("job-start", "2026-10-10"),
            ("job-middle", "2026-10-15"),
            ("job-end", "2026-10-20"),
            ("job-after", "2026-10-22"),
            ("job-undated", None),
        ):
            self.repository.save_job(
                {
                    "job_id": job_id,
                    "title": f"Engineer {job_id}",
                    "company": "Intel",
                    "country": "CR",
                    "date_published": published,
                },
                site_id="intel-cr",
                source_job_id=job_id,
            )

        jobs = self.repository.search_jobs(
            published_from="2026-10-10",
            published_to="2026-10-20",
        )

        self.assertEqual(
            {job["job_id"] for job in jobs},
            {"job-start", "job-middle", "job-end"},
        )
        with self.assertRaisesRegex(ValueError, "AAAA-MM-DD"):
            self.repository.search_jobs(published_from="2026/10/10")
        with self.assertRaisesRegex(ValueError, "posterior"):
            self.repository.search_jobs(
                published_from="2026-10-21",
                published_to="2026-10-20",
            )

    def test_search_filters_exact_employment_type_and_schedule(self):
        self.repository.save_job(
            {
                "job_id": "job-target",
                "title": "Engineer",
                "company": "Intel",
                "country": "CR",
                "employment_type": "Full-time",
                "schedule": "Night shift",
            },
            site_id="intel-cr",
            source_job_id="target",
        )
        self.repository.save_job(
            {
                "job_id": "job-different",
                "title": "Engineer",
                "company": "Intel",
                "country": "CR",
                "employment_type": "Contract",
                "schedule": "Day shift",
            },
            site_id="intel-cr",
            source_job_id="different",
        )

        jobs = self.repository.search_jobs(
            filters={
                "employment_type": "Full-time",
                "schedule": "Night shift",
            }
        )

        self.assertEqual([job["job_id"] for job in jobs], ["job-target"])

    def test_search_filters_overlapping_salary_ranges_with_matching_units(self):
        offers = (
            ("job-overlap", 4000, 6000, "USD", "annual"),
            ("job-overlap-edge", 5500, 7000, "USD", "annual"),
            ("job-other-currency", 4000, 6000, "CRC", "annual"),
            ("job-other-period", 4000, 6000, "USD", "monthly"),
            ("job-below", 1000, 3000, "USD", "annual"),
            ("job-open-upper", 5500, None, "USD", "annual"),
            ("job-open-lower", None, 5000, "USD", "annual"),
        )
        for job_id, salary_min, salary_max, currency, period in offers:
            self.repository.save_job(
                {
                    "job_id": job_id,
                    "title": f"Engineer {job_id}",
                    "company": "Intel",
                    "country": "CR",
                    "salary_min": salary_min,
                    "salary_max": salary_max,
                    "salary_currency": currency,
                    "salary_period": period,
                },
                site_id="intel-cr",
                source_job_id=job_id,
            )

        jobs = self.repository.search_jobs(
            filters={"salary_currency": "USD", "salary_period": "annual"},
            salary_at_least=5000,
            salary_at_most=5500,
        )

        self.assertEqual(
            {job["job_id"] for job in jobs},
            {
                "job-overlap",
                "job-overlap-edge",
                "job-open-upper",
                "job-open-lower",
            },
        )
        at_least = self.repository.search_jobs(
            filters={"salary_currency": "USD", "salary_period": "annual"},
            salary_at_least=6000,
        )
        self.assertEqual(
            {job["job_id"] for job in at_least},
            {"job-overlap", "job-overlap-edge", "job-open-upper"},
        )
        at_most = self.repository.search_jobs(
            filters={"salary_currency": "USD", "salary_period": "annual"},
            salary_at_most=3500,
        )
        self.assertEqual(
            {job["job_id"] for job in at_most},
            {"job-below", "job-open-lower"},
        )
        with self.assertRaisesRegex(ValueError, "moneda y periodo"):
            self.repository.search_jobs(salary_at_least=5000)
        with self.assertRaisesRegex(ValueError, "no puede superar"):
            self.repository.search_jobs(
                filters={"salary_currency": "USD", "salary_period": "annual"},
                salary_at_least=6000,
                salary_at_most=5000,
            )
        with self.assertRaisesRegex(ValueError, "no puede ser negativo"):
            self.repository.search_jobs(salary_at_least=-1)

    def test_search_text_matches_job_fields_and_treats_wildcards_literally(self):
        self.repository.save_job(
            {
                "job_id": "job-text",
                "title": "Quality Engineer",
                "company": "Example 100% Corp",
                "description_raw": "Work with manufacturing systems.",
            },
            site_id="intel-cr",
            source_job_id="TEXT-1",
        )
        self.add_job("job-unrelated")

        by_description = self.repository.search_jobs(text="manufacturing systems")
        by_literal_wildcard = self.repository.search_jobs(text="100%")
        self.assertEqual([job["job_id"] for job in by_description], ["job-text"])
        self.assertEqual([job["job_id"] for job in by_literal_wildcard], ["job-text"])
        self.assertEqual(len(self.repository.search_jobs(text="   ")), 2)

    def test_manual_job_state_is_persistent_and_excluded_from_search_by_default(self):
        self.add_job("job-applied")
        self.add_job("job-open")
        self.repository.set_user_job_state(
            "job-applied",
            "applied",
            notes="Enviada el lunes.",
        )

        visible = self.repository.search_jobs()
        included = self.repository.search_jobs(include_manually_skipped=True)
        state = self.repository.get_user_job_state("job-applied")

        self.assertEqual([job["job_id"] for job in visible], ["job-open"])
        self.assertEqual(
            {job["job_id"] for job in included},
            {"job-applied", "job-open"},
        )
        applied = next(job for job in included if job["job_id"] == "job-applied")
        self.assertEqual(applied["user_status"], "applied")
        self.assertEqual(applied["skip_automatic"], 1)
        self.assertEqual(
            self.repository.list_skipped_source_job_ids("intel-cr"),
            {"job-applied"},
        )
        self.assertEqual(state["notes"], "Enviada el lunes.")
        self.assertEqual(state["source_url_snapshot"], "https://example.test/jobs/job-applied")

        self.add_job("job-applied", city="Heredia")
        self.assertEqual(self.repository.get_user_job_state("job-applied"), state)

        self.repository.set_user_job_state(
            "job-applied", "reviewed", skip_automatic=False
        )
        updated = self.repository.get_user_job_state("job-applied")
        self.assertEqual(updated["user_status"], "reviewed")
        self.assertFalse(updated["skip_automatic"])
        self.assertEqual(updated["notes"], "Enviada el lunes.")
        self.assertEqual(self.repository.search_jobs()[0]["job_id"], "job-applied")
        self.repository.set_user_job_state(
            "job-applied", "reviewed", skip_automatic=False, notes=""
        )
        self.assertIsNone(self.repository.get_user_job_state("job-applied")["notes"])

        self.assertTrue(self.repository.clear_user_job_state("job-applied"))
        self.assertIsNone(self.repository.get_user_job_state("job-applied"))

    def test_set_user_job_state_rejects_unknown_state_and_missing_job(self):
        with self.assertRaisesRegex(ValueError, "estado manual"):
            self.repository.set_user_job_state("missing", "unknown")
        self.add_job("job-state")
        with self.assertRaisesRegex(ValueError, "no existe"):
            self.repository.set_user_job_state("missing", "applied")

    def test_application_events_are_validated_ordered_and_survive_job_purge(self):
        self.add_job("job-tracked")
        planned_id = self.repository.record_application(
            "job-tracked",
            "planned",
            next_action_at="2026-10-28",
            notes="Preparar CV.",
        )
        applied_id = self.repository.record_application(
            "job-tracked",
            "applied",
            applied_at="2026-10-23",
        )

        events = self.repository.list_applications("job-tracked")
        self.assertEqual(
            [event["application_id"] for event in events],
            [planned_id, applied_id],
        )
        self.assertEqual(events[0]["application_status"], "planned")
        self.assertEqual(events[0]["next_action_at"], "2026-10-28")
        self.assertEqual(events[0]["notes"], "Preparar CV.")
        self.assertEqual(events[1]["applied_at"], "2026-10-23")

        with self.assertRaisesRegex(ValueError, "AAAA-MM-DD"):
            self.repository.record_application(
                "job-tracked", "interview", next_action_at="23/10/2026"
            )
        with self.assertRaisesRegex(ValueError, "estado permitido"):
            self.repository.record_application("job-tracked", "unknown")

        with self.database.connection() as connection:
            connection.execute("DELETE FROM jobs WHERE job_id = 'job-tracked'")
        self.assertEqual(len(self.repository.list_applications("job-tracked")), 2)

    def test_zero_result_runs_mark_review_without_disabling_site(self):
        for expected_status in ("zero_results", "zero_results", "needs_review"):
            run_id = self.repository.start_run("full", "directos")
            result = self.repository.record_site_result(
                run_id,
                "intel-cr",
                jobs_found=0,
                jobs_valid=0,
                jobs_new=0,
                jobs_duplicate=0,
            )
            self.assertEqual(result, "zero_results")
            self.assertEqual(self.repository.finish_run(run_id), "ok")
            site = self.repository.list_sites()[0]
            self.assertEqual(site["health_status"], expected_status)

        site = self.repository.list_sites()[0]
        self.assertTrue(site["enabled"])
        self.assertTrue(site["review_required"])
        self.assertEqual(site["consecutive_zero_runs"], 3)
        run = self.repository.get_run(run_id)
        self.assertTrue(run["sites"][0]["review_required"])
        self.assertTrue(self.repository.mark_site_reviewed("intel-cr"))
        site = self.repository.list_sites()[0]
        self.assertTrue(site["enabled"])
        self.assertFalse(site["review_required"])
        self.assertEqual(site["health_status"], "zero_results")

    def test_existing_jobs_are_not_misclassified_as_zero_results(self):
        run_id = self.repository.start_run("full", "directos")
        status = self.repository.record_site_result(
            run_id,
            "intel-cr",
            jobs_found=4,
            jobs_valid=4,
            jobs_new=0,
            jobs_duplicate=4,
        )
        self.assertEqual(status, "ok")
        self.assertEqual(self.repository.finish_run(run_id), "ok")
        site = self.repository.list_sites()[0]
        self.assertEqual(site["health_status"], "healthy")
        self.assertEqual(site["consecutive_zero_runs"], 0)

    def test_errors_do_not_increment_zero_count_and_require_review(self):
        run_id = self.repository.start_run("full", "directos")
        status = self.repository.record_site_result(
            run_id,
            "intel-cr",
            jobs_found=0,
            jobs_valid=0,
            jobs_new=0,
            jobs_duplicate=0,
            error_message="HTTP 503",
        )
        self.assertEqual(status, "error")
        self.assertEqual(self.repository.finish_run(run_id), "error")
        site = self.repository.list_sites()[0]
        self.assertEqual(site["health_status"], "error")
        self.assertTrue(site["review_required"])
        self.assertEqual(site["consecutive_zero_runs"], 0)
        self.assertTrue(site["enabled"])

    def test_mixed_run_requires_all_results_and_finishes_partial(self):
        self.repository.register_site(
            "ane-cr",
            "workday",
            "ANE",
            "https://ane.example.test",
            run_groups=("boards",),
        )
        run_id = self.repository.start_run("full")
        run = self.repository.get_run(run_id)
        self.assertEqual(len(run["sites"]), 2)
        with self.assertRaisesRegex(ValueError, "sin resultado"):
            self.repository.finish_run(run_id)

        self.repository.record_site_result(
            run_id,
            "intel-cr",
            jobs_found=0,
            jobs_valid=0,
            jobs_new=0,
            jobs_duplicate=0,
            error_message="timeout",
        )
        self.repository.record_site_result(
            run_id,
            "ane-cr",
            jobs_found=2,
            jobs_valid=2,
            jobs_new=2,
            jobs_duplicate=0,
        )
        self.assertEqual(self.repository.finish_run(run_id), "partial")
        summary = self.repository.list_runs(limit=1)[0]
        self.assertEqual(summary["status"], "partial")
        self.assertEqual(summary["site_count"], 2)
        self.assertEqual(summary["error_count"], 1)
        self.assertEqual(summary["jobs_found"], 2)
        self.assertEqual(summary["jobs_new"], 2)

    def test_result_counts_must_be_consistent(self):
        run_id = self.repository.start_run("full", "directos")
        with self.assertRaisesRegex(ValueError, "jobs_valid"):
            self.repository.record_site_result(
                run_id,
                "intel-cr",
                jobs_found=1,
                jobs_valid=2,
                jobs_new=0,
                jobs_duplicate=0,
            )
        with self.assertRaisesRegex(ValueError, "jobs_new"):
            self.repository.record_site_result(
                run_id,
                "intel-cr",
                jobs_found=2,
                jobs_valid=2,
                jobs_new=2,
                jobs_duplicate=1,
            )


if __name__ == "__main__":
    unittest.main()
