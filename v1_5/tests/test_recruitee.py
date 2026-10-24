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
from jobpulse_v15.recruitee import RecruiteeFetchError, RecruiteeFetcher
from jobpulse_v15.repository import Repository


class TestRecruiteeFetcher(unittest.TestCase):
    def setUp(self):
        self.fetcher = RecruiteeFetcher()
        self.site = {
            "site_id": "acme-cr",
            "site_name": "Acme Careers CR",
            "company_name": "Acme",
            "country": "CR",
            "base_url": "https://acme.recruitee.com",
            "site_config": {"country_query": "Costa Rica"},
        }

    def test_fetch_maps_public_board_response_to_ingestion_format(self):
        posting = {
            "id": 321,
            "position": "Software Engineer",
            "careers_url": "https://acme.recruitee.com/o/software-engineer",
            "published_at": "2026-10-20T10:30:00Z",
            "location": "San Jose, Costa Rica",
            "locations": ["Costa Rica"],
            "description": "<p>Build <b>reliable</b> systems.</p>",
            "requirements": "<p>Fluent English required.</p>",
            "employment_type": "fulltime",
        }
        with patch.object(
            self.fetcher, "_request_json", return_value={"offers": [posting]}
        ) as request:
            offers = self.fetcher.fetch(self.site)

        request.assert_called_once_with("https://acme.recruitee.com/api/offers/")
        self.assertEqual(len(offers), 1)
        self.assertEqual(offers[0]["source_job_id"], "321")
        self.assertEqual(offers[0]["job"]["job_id"], "recruitee-acme-321")
        self.assertEqual(offers[0]["job"]["company"], "Acme")
        self.assertEqual(
            offers[0]["job"]["location_raw"], "San Jose, Costa Rica"
        )
        self.assertEqual(offers[0]["job"]["employment_type"], "fulltime")
        self.assertEqual(offers[0]["job"]["date_published"], "2026-10-20")
        self.assertEqual(
            offers[0]["job"]["description_raw"],
            "Build reliable systems. Fluent English required.",
        )
        self.assertEqual(offers[0]["raw_payload"], posting)

    def test_fetch_filters_countries_deduplicates_and_counts_manual_skips(self):
        postings = [
            {
                "id": 1,
                "position": "Engineer",
                "location": "Toronto, Canada",
            },
            {
                "id": 2,
                "position": "Engineer",
                "location": "Remote",
                "locations": ["San Jose, Costa Rica"],
            },
            {
                "id": 2,
                "position": "Duplicate",
                "location": "San Jose, Costa Rica",
            },
            {
                "id": 3,
                "position": "Analyst Costa Rica",
                "location": "Remote",
            },
        ]
        with patch.object(
            self.fetcher, "_request_json", return_value={"offers": postings}
        ):
            offers = self.fetcher.fetch(self.site, skip_source_job_ids={"2"})

        self.assertEqual([offer["source_job_id"] for offer in offers], ["3"])
        self.assertEqual(offers.jobs_skipped, 1)
        self.assertEqual(
            offers[0]["job"]["location_raw"], "Costa Rica (multi-loc)"
        )

    def test_fetch_rejects_untrusted_urls_and_malformed_payloads(self):
        for url in (
            "http://acme.recruitee.com",
            "https://example.com/acme",
            "https://user@acme.recruitee.com",
            "https://recruitee.com",
            "https://www.recruitee.com/acme",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.fetcher.fetch({**self.site, "base_url": url})

        invalid_responses = (
            {},
            {"offers": "not-a-list"},
            {"offers": [None]},
            {"offers": [{"id": 1}]},
        )
        for response in invalid_responses:
            with self.subTest(response=response), patch.object(
                self.fetcher, "_request_json", return_value=response
            ), self.assertRaises(RecruiteeFetchError):
                self.fetcher.fetch(self.site)

    def test_recruitee_cli_imports_single_site(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "recruitee.db")
            database.initialize()
            repository = Repository(database)
            repository.register_platform("recruitee", "Recruitee", "api")
            repository.register_site(
                "acme-cr",
                "recruitee",
                "Acme Careers CR",
                "https://acme.recruitee.com",
                company_name="Acme",
                country="CR",
                site_config={"adapter": "recruitee"},
            )
            offer = {
                "source_job_id": "321",
                "source_url": "https://acme.recruitee.com/o/software-engineer",
                "job": {
                    "job_id": "recruitee-acme-321",
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
                    "recruitee",
                    "--site",
                    "acme-cr",
                ],
            ), patch(
                "jobpulse_v15.cli.RecruiteeFetcher.fetch", return_value=[offer]
            ), redirect_stdout(output):
                self.assertEqual(main(), 0)

            result = json.loads(output.getvalue())
            self.assertEqual(result["jobs_new"], 1)
            self.assertIsNotNone(repository.get_job("recruitee-acme-321"))

    def test_recruitee_group_continues_after_site_failure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "recruitee-group.db")
            database.initialize()
            repository = Repository(database)
            repository.register_platform("recruitee", "Recruitee", "api")
            for site_id in ("site-good", "site-fails"):
                repository.register_site(
                    site_id,
                    "recruitee",
                    site_id,
                    f"https://{site_id}.recruitee.com",
                    run_groups=("directos",),
                )

            offer = {
                "source_job_id": "99",
                "job": {"job_id": "recruitee-site-good-99", "title": "Engineer"},
            }

            def fetch(site, **kwargs):
                _ = kwargs
                if site["site_id"] == "site-fails":
                    raise RecruiteeFetchError("tablero no disponible")
                return [offer]

            output = io.StringIO()
            with patch(
                "sys.argv",
                [
                    "jobpulse-v15",
                    "--db",
                    str(database.path),
                    "scrape",
                    "recruitee",
                    "--group",
                    "directos",
                ],
            ), patch(
                "jobpulse_v15.cli.RecruiteeFetcher.fetch", side_effect=fetch
            ), redirect_stdout(output):
                self.assertEqual(main(), 1)

            report = json.loads(output.getvalue())
            self.assertEqual(report["sites_selected"], 2)
            self.assertEqual(len(report["results"]), 1)
            self.assertEqual(len(report["failures"]), 1)
            self.assertIsNotNone(repository.get_job("recruitee-site-good-99"))
            self.assertEqual(
                repository.list_site_runs("site-fails", limit=1)[0]["site_status"],
                "error",
            )


if __name__ == "__main__":
    unittest.main()
