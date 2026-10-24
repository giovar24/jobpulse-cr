import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from jobpulse_v15.cli import main
from jobpulse_v15.db import Database
from jobpulse_v15.repository import Repository
from jobpulse_v15.workable import WorkableFetchError, WorkableFetcher


class TestWorkableFetcher(unittest.TestCase):
    def setUp(self):
        self.fetcher = WorkableFetcher()
        self.site = {
            "site_id": "acme-cr",
            "site_name": "Acme Careers CR",
            "company_name": "Acme",
            "country": "CR",
            "base_url": "https://apply.workable.com/acme",
            "site_config": {"country_query": "Costa Rica"},
        }

    def test_fetch_maps_public_board_response_to_ingestion_format(self):
        posting = {
            "shortcode": "A1B2C3",
            "title": "Software Engineer",
            "url": "https://apply.workable.com/acme/j/A1B2C3/",
            "published_on": "2026-10-20T10:30:00+00:00",
            "description": "<p>Build <b>reliable</b> systems.</p>",
            "requirements": "<p>Fluent English required.</p>",
            "type": "Full-time",
            "telecommuting": False,
            "location": {
                "city": "San Jose",
                "region": "San Jose",
                "country": "Costa Rica",
            },
        }
        with patch.object(
            self.fetcher, "_request_json", return_value={"jobs": [posting]}
        ) as request:
            offers = self.fetcher.fetch(self.site)

        request.assert_called_once_with(
            "https://apply.workable.com/api/v1/accounts/acme/jobs?details=true"
        )
        self.assertEqual(len(offers), 1)
        self.assertEqual(offers[0]["source_job_id"], "A1B2C3")
        self.assertEqual(offers[0]["job"]["job_id"], "workable-acme-A1B2C3")
        self.assertEqual(offers[0]["job"]["company"], "Acme")
        self.assertEqual(
            offers[0]["job"]["location_raw"], "San Jose, San Jose, Costa Rica"
        )
        self.assertEqual(offers[0]["job"]["work_mode"], "unknown")
        self.assertEqual(offers[0]["job"]["employment_type"], "Full-time")
        self.assertEqual(offers[0]["job"]["date_published"], "2026-10-20")
        self.assertEqual(
            offers[0]["job"]["description_raw"],
            "Build reliable systems. Fluent English required.",
        )
        self.assertEqual(offers[0]["raw_payload"], posting)

    def test_fetch_filters_countries_deduplicates_and_counts_manual_skips(self):
        postings = [
            {
                "shortcode": 1,
                "title": "Analyst Costa Rica",
                "location": {"city": "Toronto", "country": "Canada"},
            },
            {
                "shortcode": 2,
                "title": "Engineer",
                "telecommuting": True,
                "location": {"city": "Remote", "country": "Costa Rica"},
            },
            {
                "shortcode": 2,
                "title": "Duplicate",
                "location": {"city": "San Jose", "country": "Costa Rica"},
            },
        ]
        with patch.object(
            self.fetcher, "_request_json", return_value={"jobs": postings}
        ):
            offers = self.fetcher.fetch(self.site, skip_source_job_ids={"2"})

        self.assertEqual([offer["source_job_id"] for offer in offers], ["1"])
        self.assertEqual(offers.jobs_skipped, 1)

    def test_fetch_rejects_untrusted_urls_and_malformed_payloads(self):
        for url in (
            "http://apply.workable.com/acme",
            "https://example.com/acme",
            "https://user@apply.workable.com/acme",
            "https://apply.workable.com/",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.fetcher.fetch({**self.site, "base_url": url})

        invalid_responses = (
            {},
            {"jobs": "not-a-list"},
            {"jobs": [None]},
            {"jobs": [{"shortcode": 1}]},
        )
        for response in invalid_responses:
            with self.subTest(response=response), patch.object(
                self.fetcher, "_request_json", return_value=response
            ), self.assertRaises(WorkableFetchError):
                self.fetcher.fetch(self.site)

    def test_workable_cli_imports_single_site(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "workable.db")
            database.initialize()
            repository = Repository(database)
            repository.register_platform("workable", "Workable", "api")
            repository.register_site(
                "acme-cr",
                "workable",
                "Acme Careers CR",
                "https://apply.workable.com/acme",
                company_name="Acme",
                country="CR",
                site_config={"adapter": "workable"},
            )
            offer = {
                "source_job_id": "A1B2C3",
                "source_url": "https://apply.workable.com/acme/j/A1B2C3/",
                "job": {
                    "job_id": "workable-acme-A1B2C3",
                    "title": "Software Engineer",
                    "country": "CR",
                },
            }
            output = io.StringIO()
            with patch(
                "sys.argv",
                [
                    "jobpulse-v15",
                    "--db",
                    str(database.path),
                    "scrape",
                    "workable",
                    "--site",
                    "acme-cr",
                ],
            ), patch(
                "jobpulse_v15.cli.WorkableFetcher.fetch", return_value=[offer]
            ), redirect_stdout(output):
                self.assertEqual(main(), 0)

            result = json.loads(output.getvalue())
            self.assertEqual(result["jobs_new"], 1)
            self.assertIsNotNone(repository.get_job("workable-acme-A1B2C3"))

    def test_workable_group_continues_after_site_failure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "workable-group.db")
            database.initialize()
            repository = Repository(database)
            repository.register_platform("workable", "Workable", "api")
            for site_id in ("site-good", "site-fails"):
                repository.register_site(
                    site_id,
                    "workable",
                    site_id,
                    f"https://apply.workable.com/{site_id}",
                    run_groups=("directos",),
                )

            offer = {
                "source_job_id": "99",
                "job": {"job_id": "workable-site-good-99", "title": "Engineer"},
            }

            def fetch(site, **kwargs):
                _ = kwargs
                if site["site_id"] == "site-fails":
                    raise WorkableFetchError("tablero no disponible")
                return [offer]

            output = io.StringIO()
            with patch(
                "sys.argv",
                [
                    "jobpulse-v15",
                    "--db",
                    str(database.path),
                    "scrape",
                    "workable",
                    "--group",
                    "directos",
                ],
            ), patch(
                "jobpulse_v15.cli.WorkableFetcher.fetch", side_effect=fetch
            ), redirect_stdout(output):
                self.assertEqual(main(), 1)

            report = json.loads(output.getvalue())
            self.assertEqual(report["sites_selected"], 2)
            self.assertEqual(len(report["results"]), 1)
            self.assertEqual(len(report["failures"]), 1)
            self.assertIsNotNone(repository.get_job("workable-site-good-99"))
            self.assertEqual(
                repository.list_site_runs("site-fails", limit=1)[0]["site_status"],
                "error",
            )


if __name__ == "__main__":
    unittest.main()
