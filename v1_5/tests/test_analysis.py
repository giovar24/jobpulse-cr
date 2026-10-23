import csv
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from jobpulse_v15.analysis import RuleBasedExtractor
from jobpulse_v15.cli import main
from jobpulse_v15.db import Database
from jobpulse_v15.repository import Repository


class TestRuleBasedExtractor(unittest.TestCase):
    def setUp(self):
        self.extractor = RuleBasedExtractor()

    def test_extracts_explicit_english_requirements_with_evidence(self):
        text = (
            "Fluent English required. Bachelor's degree completed is required. "
            "Valid driver's license class B1 mandatory. "
            "At least 3 years of experience required."
        )
        result = self.extractor.extract(text)

        self.assertEqual(result.analysis_status, "complete")
        self.assertEqual(
            result.field_statuses,
            {
                "language": "found",
                "education": "found",
                "license": "found",
                "experience": "found",
                "skill": "not_mentioned",
            },
        )
        by_category = {item["category"]: item for item in result.requirements}
        self.assertEqual(by_category["language"]["value_code"], "en")
        self.assertEqual(by_category["language"]["qualifier"], "fluent")
        self.assertEqual(by_category["education"]["education_status"], "completed")
        self.assertEqual(by_category["license"]["value_code"], "B1")
        self.assertEqual(by_category["experience"]["minimum"], 3)
        self.assertIn("At least 3 years", by_category["experience"]["evidence"])

    def test_extracts_spanish_phrases_and_education_in_progress(self):
        result = self.extractor.extract(
            "Se requiere inglés avanzado. "
            "Bachillerato universitario en curso. "
            "Licencia B1 deseable. Experiencia de al menos 3 años."
        )
        by_category = {item["category"]: item for item in result.requirements}

        self.assertEqual(result.analysis_status, "complete")
        self.assertEqual(by_category["language"]["requirement_level"], "required")
        self.assertEqual(by_category["education"]["education_status"], "pursuing")
        self.assertEqual(by_category["license"]["value_code"], "B1")
        self.assertEqual(by_category["license"]["requirement_level"], "preferred")
        self.assertEqual(by_category["experience"]["minimum"], 3)
        self.assertEqual(by_category["experience"]["requirement_level"], "required")

    def test_extracts_explicit_technical_skills_in_english_and_spanish(self):
        result = self.extractor.extract(
            "Required skills: Python, SQL, and Power BI. "
            "Se requiere experiencia con Excel y SAP."
        )

        skills = {
            requirement["value_code"]: requirement
            for requirement in result.requirements
            if requirement["category"] == "skill"
        }
        self.assertEqual(set(skills), {"python", "sql", "power_bi", "excel", "sap"})
        self.assertTrue(
            all(requirement["requirement_level"] == "required" for requirement in skills.values())
        )
        self.assertEqual(result.field_statuses["skill"], "found")
        self.assertTrue(
            all(requirement["evidence"] for requirement in skills.values())
        )

    def test_skill_mentions_without_explicit_context_are_not_extracted(self):
        ordinary_mention = self.extractor.extract(
            "Our team uses Python for internal scripts."
        )
        unsupported_skill = self.extractor.extract(
            "Required skills include Kubernetes."
        )

        self.assertEqual(ordinary_mention.field_statuses["skill"], "not_mentioned")
        self.assertEqual(ordinary_mention.requirements, [])
        self.assertEqual(unsupported_skill.field_statuses["skill"], "unknown")
        self.assertEqual(unsupported_skill.analysis_status, "partial")
        self.assertEqual(unsupported_skill.requirements, [])

    def test_extracts_preferred_skill_and_marks_conflicting_cues_ambiguous(self):
        preferred = self.extractor.extract("Python is preferred.")
        conflicting = self.extractor.extract(
            "Python is required and desirable."
        )

        preferred_skill = next(
            item for item in preferred.requirements if item["category"] == "skill"
        )
        conflicting_skill = next(
            item for item in conflicting.requirements if item["category"] == "skill"
        )
        self.assertEqual(preferred_skill["requirement_level"], "preferred")
        self.assertEqual(conflicting_skill["requirement_level"], "ambiguous")
        self.assertEqual(conflicting.field_statuses["skill"], "ambiguous")

    def test_unknown_and_not_mentioned_are_different(self):
        no_evidence = self.extractor.extract("We offer flexible benefits.")
        announcement_english = self.extractor.extract(
            "This job posting is written in English."
        )
        unsupported = self.extractor.extract(
            "Fluent Portuguese required; professional experience is required."
        )

        self.assertEqual(no_evidence.field_statuses["language"], "not_mentioned")
        self.assertEqual(announcement_english.field_statuses["language"], "unknown")
        self.assertEqual(announcement_english.requirements, [])
        self.assertEqual(unsupported.field_statuses["language"], "unknown")
        self.assertEqual(unsupported.field_statuses["experience"], "unknown")
        self.assertEqual(unsupported.analysis_status, "partial")
        self.assertEqual(unsupported.requirements, [])

    def test_conflicting_level_cues_are_marked_ambiguous(self):
        result = self.extractor.extract("English required, Spanish preferred.")

        self.assertEqual(result.analysis_status, "partial")
        self.assertEqual(result.field_statuses["language"], "ambiguous")
        self.assertTrue(
            all(
                requirement["requirement_level"] == "ambiguous"
                for requirement in result.requirements
            )
        )

    def test_rejects_empty_input(self):
        for text in ("", " \n\t "):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, "obligatorio"):
                self.extractor.extract(text)

    def test_extracts_and_persists_analysis_with_source_context(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "test.db")
            database.initialize()
            repository = Repository(database)
            repository.register_platform("workday", "Workday", "api")
            repository.register_site(
                "site-1",
                "workday",
                "Example",
                "https://example.test/jobs",
                site_config={"board": "External"},
            )
            text = (
                "Fluent English required. Bachelor's degree completed required. "
                "Python and SQL skills required."
            )
            repository.save_job(
                {
                    "job_id": "job-analysis",
                    "title": "Engineer",
                    "description_raw": text,
                    "date_published": "2026-10-20",
                    "employment_type": "Full-time",
                    "schedule": "Night shift",
                    "salary_min": 4000,
                    "salary_max": 6000,
                    "salary_currency": "USD",
                    "salary_period": "annual",
                },
                site_id="site-1",
                source_job_id="source-1",
            )

            output = io.StringIO()
            with patch(
                "sys.argv",
                [
                    "jobpulse-v15",
                    "--db",
                    str(database.path),
                    "analysis",
                    "extract",
                    "--job",
                    "job-analysis",
                ],
            ), redirect_stdout(output):
                self.assertEqual(main(), 0)

            saved = repository.get_current_analysis("job-analysis")
            cli_result = json.loads(output.getvalue())
            self.assertEqual(saved["analysis_id"], cli_result["analysis_id"])
            self.assertEqual(saved["source_site_id"], "site-1")
            self.assertEqual(len(saved["requirements"]), 4)
            self.assertEqual(saved["field_statuses"]["skill"], "found")
            repository.save_candidate_profile_fact("skill", "python", "Python")
            repository.save_candidate_profile_fact("skill", "sql", "SQL")
            repository.save_candidate_profile_fact(
                "language", "en", "English", qualifier="fluent"
            )
            repository.save_candidate_profile_fact(
                "education",
                "bachelor",
                "Bachelor's degree",
                education_status="completed",
            )
            comparison = repository.compare_job_to_candidate_profile("job-analysis")
            skill_statuses = {
                requirement["value_code"]: requirement["comparison_status"]
                for requirement in comparison["requirements"]
                if requirement["category"] == "skill"
            }
            self.assertEqual(
                skill_statuses, {"python": "matched", "sql": "matched"}
            )

            search_output = io.StringIO()
            with patch(
                "sys.argv",
                [
                    "jobpulse-v15",
                    "--db",
                    str(database.path),
                    "search",
                    "--text",
                    "Engineer",
                    "--employment-type",
                    "Full-time",
                    "--schedule",
                    "Night shift",
                    "--salary-currency",
                    "USD",
                    "--salary-period",
                    "annual",
                    "--salary-at-least",
                    "5000",
                    "--salary-at-most",
                    "5500",
                    "--requirement",
                    "language=en",
                ],
            ), redirect_stdout(search_output):
                self.assertEqual(main(), 0)
            search_results = json.loads(search_output.getvalue())
            self.assertEqual(
                [job["job_id"] for job in search_results], ["job-analysis"]
            )

            compatibility_output = io.StringIO()
            with patch(
                "sys.argv",
                [
                    "jobpulse-v15",
                    "--db",
                    str(database.path),
                    "search",
                    "--profile-compatibility",
                    "matched",
                ],
            ), redirect_stdout(compatibility_output):
                self.assertEqual(main(), 0)
            compatibility_results = json.loads(compatibility_output.getvalue())
            self.assertEqual(
                [job["job_id"] for job in compatibility_results], ["job-analysis"]
            )
            self.assertEqual(
                compatibility_results[0]["profile_compatibility"], "Coincide"
            )

            repository.save_job(
                {
                    "job_id": "job-without-analysis",
                    "title": "Engineer without analysis",
                    "company": "Example",
                    "country": "CR",
                },
                site_id="site-1",
                source_job_id="source-without-analysis",
            )
            unanalyzed_output = io.StringIO()
            with patch(
                "sys.argv",
                [
                    "jobpulse-v15",
                    "--db",
                    str(database.path),
                    "search",
                    "--profile-compatibility",
                    "not_analyzed",
                ],
            ), redirect_stdout(unanalyzed_output):
                self.assertEqual(main(), 0)
            unanalyzed_results = json.loads(unanalyzed_output.getvalue())
            self.assertEqual(
                [job["job_id"] for job in unanalyzed_results],
                ["job-without-analysis"],
            )
            self.assertEqual(
                unanalyzed_results[0]["profile_compatibility"], "Sin analisis"
            )

            review_output = io.StringIO()
            with patch(
                "sys.argv",
                [
                    "jobpulse-v15",
                    "--db",
                    str(database.path),
                    "search",
                    "--review-reason",
                    "missing_company",
                ],
            ), redirect_stdout(review_output):
                self.assertEqual(main(), 0)
            review_results = json.loads(review_output.getvalue())
            self.assertEqual(
                [job["job_id"] for job in review_results], ["job-analysis"]
            )
            self.assertIn("missing_company", review_results[0]["review_reasons"])

            csv_output = io.StringIO()
            with patch(
                "sys.argv",
                [
                    "jobpulse-v15",
                    "--db",
                    str(database.path),
                    "search",
                    "--format",
                    "csv",
                    "--review-reason",
                    "missing_company",
                ],
            ), redirect_stdout(csv_output):
                self.assertEqual(main(), 0)
            csv_rows = list(csv.reader(io.StringIO(csv_output.getvalue())))
            self.assertEqual(csv_rows[0][0], "job_id")
            self.assertEqual(csv_rows[0][-1], "url_fuente")
            self.assertEqual(csv_rows[1][0], "job-analysis")
            self.assertEqual(csv_rows[1][1], "Engineer")
            self.assertEqual(
                csv_rows[1][-2], "missing_company;missing_country"
            )
            self.assertNotIn("description_raw", csv_output.getvalue())

            repository.save_job(
                {
                    "job_id": "job-analysis-newer",
                    "title": "Engineer newer",
                    "company": "Example",
                    "country": "CR",
                    "date_published": "2026-10-22",
                },
                site_id="site-1",
                source_job_id="source-newer",
            )
            sorted_output = io.StringIO()
            with patch(
                "sys.argv",
                [
                    "jobpulse-v15",
                    "--db",
                    str(database.path),
                    "search",
                    "--sort-by",
                    "date_published",
                    "--published-from",
                    "2026-10-21",
                    "--published-to",
                    "2026-10-23",
                    "--text",
                    "Engineer",
                ],
            ), redirect_stdout(sorted_output):
                self.assertEqual(main(), 0)
            sorted_jobs = json.loads(sorted_output.getvalue())
            self.assertEqual(
                [job["job_id"] for job in sorted_jobs],
                ["job-analysis-newer"],
            )


if __name__ == "__main__":
    unittest.main()
