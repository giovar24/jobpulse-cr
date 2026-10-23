import io
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from jobpulse_v15.cli import main
from jobpulse_v15.db import Database
from jobpulse_v15.ingestion import IngestionError, IngestionService
from jobpulse_v15.repository import Repository


class TestIngestionService(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "test.db")
        self.database.initialize()
        self.repository = Repository(self.database)
        self.repository.register_platform("test-platform", "Test", "import")
        self.repository.register_site(
            "test-site",
            "test-platform",
            "Test Site",
            "https://example.test/jobs",
        )
        self.service = IngestionService(self.repository)

    def tearDown(self):
        self.temp_dir.cleanup()

    @staticmethod
    def offer(
        source_job_id="source-1",
        *,
        title="Software Engineer",
        job_id=None,
        description="Build software.",
    ):
        job = {
            "title": title,
            "company": "Example",
            "country": "CR",
            "city": "San Jose",
            "work_mode": "hybrid",
            "description_raw": description,
        }
        if job_id is not None:
            job["job_id"] = job_id
        return {
            "source_job_id": source_job_id,
            "source_url": f"https://example.test/jobs/{source_job_id}",
            "job": job,
            "raw_payload": {"id": source_job_id},
        }

    def test_import_creates_site_run_and_normalizes_provider_records(self):
        result = self.service.import_offers(
            "test-site",
            [self.offer()],
        )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["jobs_found"], 1)
        self.assertEqual(result["jobs_new"], 1)
        self.assertEqual(result["jobs_duplicate"], 0)
        job = self.repository.get_job("test-site:source-1")
        self.assertEqual(job["title"], "Software Engineer")
        self.assertEqual(job["country"], "CR")
        self.assertEqual(job["work_mode"], "hybrid")
        self.assertEqual(
            self.repository.get_job_sources(job["job_id"])[0]["source_url"],
            "https://example.test/jobs/source-1",
        )
        run = self.repository.get_run(result["run_id"])
        self.assertEqual(len(run["sites"]), 1)
        self.assertEqual(run["sites"][0]["site_id"], "test-site")
        self.assertEqual(run["sites"][0]["jobs_new"], 1)

    def test_reimport_is_idempotent_and_counts_known_source_as_duplicate(self):
        first = self.service.import_offers("test-site", [self.offer()])
        second = self.service.import_offers(
            "test-site",
            [
                self.offer(
                    title="Software Engineer II",
                    description="Updated description.",
                )
            ],
        )

        self.assertEqual(first["jobs_new"], 1)
        self.assertEqual(second["jobs_new"], 0)
        self.assertEqual(second["jobs_duplicate"], 1)
        self.assertEqual(
            self.repository.get_job("test-site:source-1")["title"],
            "Software Engineer II",
        )

    def test_invalid_batch_is_rejected_before_a_run_is_created(self):
        invalid = self.offer()
        invalid["job"]["title"] = " "
        with self.assertRaisesRegex(ValueError, "title es obligatorio"):
            self.service.import_offers("test-site", [invalid])
        self.assertEqual(self.repository.list_runs(), [])

    def test_disabled_site_cannot_be_ingested(self):
        self.repository.set_site_enabled("test-site", False)
        with self.assertRaisesRegex(ValueError, "deshabilitado"):
            self.service.import_offers("test-site", [self.offer()])
        self.assertEqual(self.repository.list_runs(), [])

    def test_write_error_is_recorded_and_run_is_closed(self):
        with patch.object(
            self.repository,
            "save_job",
            side_effect=sqlite3.OperationalError("disk unavailable"),
        ):
            with self.assertRaises(IngestionError) as context:
                self.service.import_offers("test-site", [self.offer()])

        run = self.repository.get_run(context.exception.run_id)
        self.assertEqual(run["status"], "error")
        self.assertEqual(run["sites"][0]["status"], "error")
        self.assertIn("disk unavailable", run["sites"][0]["error_message"])
        self.assertTrue(run["sites"][0]["review_required"])

    def test_cli_import_json_reads_file_and_reports_counts(self):
        input_path = Path(self.temp_dir.name) / "offers.json"
        input_path.write_text(
            json.dumps({"offers": [self.offer()]}),
            encoding="utf-8",
        )
        output = io.StringIO()
        with patch(
            "sys.argv",
            [
                "jobpulse-v15",
                "--db",
                str(self.database.path),
                "ingest",
                "import-json",
                "--site",
                "test-site",
                "--input",
                str(input_path),
            ],
        ), redirect_stdout(output):
            self.assertEqual(main(), 0)

        result = json.loads(output.getvalue())
        self.assertEqual(result["jobs_new"], 1)
        self.assertEqual(result["status"], "ok")


if __name__ == "__main__":
    unittest.main()
