import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from jobpulse_v15.ashby import AshbyFetchError, AshbyFetcher
from jobpulse_v15.cli import main
from jobpulse_v15.db import Database
from jobpulse_v15.repository import Repository


class TestAshbyFetcher(unittest.TestCase):
    def setUp(self):
        self.fetcher = AshbyFetcher()
        self.site = {
            "site_id": "acme-cr",
            "site_name": "Acme Careers CR",
            "company_name": "Acme",
            "country": "CR",
            "base_url": "https://jobs.ashbyhq.com/acme",
            "site_config": {"country_query": "Costa Rica"},
        }

    def test_fetch_maps_public_board_response_to_ingestion_format(self):
        posting = {
            "id": "posting-123",
            "title": "Software Engineer",
            "location": "San Jose, Costa Rica",
            "secondaryLocations": [{"name": "Remote, Costa Rica"}],
            "jobUrl": "https://jobs.ashbyhq.com/acme/123",
            "publishedAt": "2026-10-20T10:30:00.000Z",
            "descriptionHtml": "<p>Build <b>reliable</b> systems.</p>",
            "workplaceType": "Hybrid",
            "employmentType": "FullTime",
        }
        with patch.object(
            self.fetcher, "_request_json", return_value={"jobs": [posting]}
        ) as request:
            offers = self.fetcher.fetch(self.site)

        request.assert_called_once_with(
            "https://api.ashbyhq.com/posting-api/job-board/acme"
        )
        self.assertEqual(len(offers), 1)
        self.assertEqual(offers[0]["source_job_id"], "posting-123")
        self.assertEqual(offers[0]["job"]["job_id"], "ashby-acme-posting-123")
        self.assertEqual(offers[0]["job"]["company"], "Acme")
        self.assertEqual(offers[0]["job"]["location_raw"], "San Jose, Costa Rica")
        self.assertEqual(offers[0]["job"]["work_mode"], "hybrid")
        self.assertEqual(offers[0]["job"]["employment_type"], "FullTime")
        self.assertEqual(offers[0]["job"]["date_published"], "2026-10-20")
        self.assertEqual(
            offers[0]["job"]["description_raw"], "Build reliable systems."
        )
        self.assertEqual(offers[0]["raw_payload"], posting)

    def test_fetch_filters_countries_deduplicates_and_counts_manual_skips(self):
        postings = [
            {
                "id": 1,
                "title": "Engineer",
                "location": "Toronto, Canada",
            },
            {
                "id": 2,
                "title": "Engineer",
                "location": "Remote",
                "secondaryLocations": ["San Jose, Costa Rica"],
            },
            {
                "id": 2,
                "title": "Duplicate",
                "location": "San Jose, Costa Rica",
            },
            {
                "id": 3,
                "title": "Analyst Costa Rica",
                "location": "Remote",
            },
        ]
        with patch.object(
            self.fetcher, "_request_json", return_value={"jobs": postings}
        ):
            offers = self.fetcher.fetch(self.site, skip_source_job_ids={"2"})

        self.assertEqual([offer["source_job_id"] for offer in offers], ["3"])
        self.assertEqual(offers.jobs_skipped, 1)
        self.assertEqual(
            offers[0]["job"]["location_raw"], "Costa Rica (multi-loc)"
        )

    def test_fetch_rejects_untrusted_urls_and_malformed_payloads(self):
        for url in (
            "http://jobs.ashbyhq.com/acme",
            "https://example.com/acme",
            "https://user@jobs.ashbyhq.com/acme",
            "https://jobs.ashbyhq.com/",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.fetcher.fetch({**self.site, "base_url": url})

        invalid_responses = (
            {},
            {"jobs": "not-a-list"},
            {"jobs": [None]},
            {"jobs": [{"id": 1}]},
        )
        for response in invalid_responses:
            with self.subTest(response=response), patch.object(
                self.fetcher, "_request_json", return_value=response
            ), self.assertRaises(AshbyFetchError):
                self.fetcher.fetch(self.site)

    def test_ashby_cli_imports_single_site(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "ashby.db")
            database.initialize()
            repository = Repository(database)
            repository.register_platform("ashby", "Ashby", "api")
            repository.register_site(
                "acme-cr",
                "ashby",
                "Acme Careers CR",
                "https://jobs.ashbyhq.com/acme",
                company_name="Acme",
                country="CR",
                site_config={"adapter": "ashby"},
            )
            offer = {
                "source_job_id": "posting-123",
                "source_url": "https://jobs.ashbyhq.com/acme/123",
                "job": {
                    "job_id": "ashby-acme-posting-123",
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
                    "ashby",
                    "--site",
                    "acme-cr",
                ],
            ), patch(
                "jobpulse_v15.cli.AshbyFetcher.fetch", return_value=[offer]
            ), redirect_stdout(output):
                self.assertEqual(main(), 0)

            result = json.loads(output.getvalue())
            self.assertEqual(result["jobs_new"], 1)
            self.assertIsNotNone(repository.get_job("ashby-acme-posting-123"))

    def test_ashby_group_continues_after_site_failure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "ashby-group.db")
            database.initialize()
            repository = Repository(database)
            repository.register_platform("ashby", "Ashby", "api")
            for site_id in ("site-good", "site-fails"):
                repository.register_site(
                    site_id,
                    "ashby",
                    site_id,
                    f"https://jobs.ashbyhq.com/{site_id}",
                    run_groups=("directos",),
                )

            offer = {
                "source_job_id": "99",
                "job": {"job_id": "ashby-site-good-99", "title": "Engineer"},
            }

            def fetch(site, **kwargs):
                _ = kwargs
                if site["site_id"] == "site-fails":
                    raise AshbyFetchError("tablero no disponible")
                return [offer]

            output = io.StringIO()
            with patch(
                "sys.argv",
                [
                    "jobpulse-v15",
                    "--db",
                    str(database.path),
                    "scrape",
                    "ashby",
                    "--group",
                    "directos",
                ],
            ), patch(
                "jobpulse_v15.cli.AshbyFetcher.fetch", side_effect=fetch
            ), redirect_stdout(output):
                self.assertEqual(main(), 1)

            report = json.loads(output.getvalue())
            self.assertEqual(report["sites_selected"], 2)
            self.assertEqual(len(report["results"]), 1)
            self.assertEqual(len(report["failures"]), 1)
            self.assertIsNotNone(repository.get_job("ashby-site-good-99"))
            self.assertEqual(
                repository.list_site_runs("site-fails", limit=1)[0]["site_status"],
                "error",
            )


if __name__ == "__main__":
    unittest.main()
