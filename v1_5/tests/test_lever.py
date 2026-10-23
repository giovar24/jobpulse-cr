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
from jobpulse_v15.lever import LeverFetchError, LeverFetcher
from jobpulse_v15.repository import Repository


class TestLeverFetcher(unittest.TestCase):
    def setUp(self):
        self.fetcher = LeverFetcher()
        self.site = {
            "site_id": "acme-cr",
            "site_name": "Acme Careers CR",
            "company_name": "Acme",
            "country": "CR",
            "base_url": "https://jobs.lever.co/acme",
            "site_config": {"country_query": "Costa Rica"},
        }

    def test_fetch_maps_public_board_response_to_ingestion_format(self):
        posting = {
            "id": "aa11bb22",
            "text": "Software Engineer",
            "hostedUrl": "https://jobs.lever.co/acme/aa11bb22",
            "applyUrl": "https://jobs.lever.co/acme/aa11bb22/apply",
            "createdAt": 1760900000000,
            "description": "<p>Build <b>reliable</b> systems.</p>",
            "additional": "<p>Fluent English required.</p>",
            "categories": {
                "location": "San Jose, Costa Rica",
                "commitment": "Full-time",
            },
            "country": "Costa Rica",
            "lists": [
                {"text": "Requirements"},
                {"text": "Nice to have"},
            ],
        }
        with patch.object(
            self.fetcher, "_request_json", return_value=[posting]
        ) as request:
            offers = self.fetcher.fetch(self.site)

        request.assert_called_once_with(
            "https://api.eu.lever.co/v0/postings/acme?mode=json"
        )
        self.assertEqual(len(offers), 1)
        self.assertEqual(offers[0]["source_job_id"], "aa11bb22")
        self.assertEqual(offers[0]["job"]["job_id"], "lever-acme-aa11bb22")
        self.assertEqual(offers[0]["job"]["company"], "Acme")
        self.assertEqual(
            offers[0]["job"]["location_raw"], "San Jose, Costa Rica"
        )
        self.assertEqual(offers[0]["job"]["employment_type"], "Full-time")
        self.assertEqual(
            offers[0]["job"]["description_raw"],
            "Build reliable systems. Fluent English required.",
        )
        self.assertEqual(offers[0]["raw_payload"], posting)

    def test_fetch_falls_back_to_com_api_when_eu_fails(self):
        with patch.object(
            self.fetcher,
            "_request_json",
            side_effect=[
                LeverFetchError("Lever respondio HTTP 404."),
                [],
            ],
        ) as request:
            offers = self.fetcher.fetch(self.site)

        self.assertEqual(offers, [])
        self.assertEqual(request.call_count, 2)
        self.assertEqual(
            request.call_args_list[1].args[0],
            "https://api.lever.co/v0/postings/acme?mode=json",
        )

    def test_fetch_filters_countries_deduplicates_and_counts_manual_skips(self):
        postings = [
            {
                "id": 1,
                "text": "Engineer",
                "categories": {"location": "Toronto, Canada"},
            },
            {
                "id": 2,
                "text": "Engineer",
                "categories": {"location": "Remote"},
                "country": "Costa Rica",
            },
            {
                "id": 2,
                "text": "Duplicate",
                "categories": {"location": "San Jose, Costa Rica"},
            },
            {
                "id": 3,
                "text": "Analyst Costa Rica",
                "categories": {"location": "Remote"},
            },
        ]
        with patch.object(
            self.fetcher, "_request_json", return_value=postings
        ):
            offers = self.fetcher.fetch(self.site, skip_source_job_ids={"2"})

        self.assertEqual([offer["source_job_id"] for offer in offers], ["3"])
        self.assertEqual(offers.jobs_skipped, 1)
        self.assertEqual(
            offers[0]["job"]["location_raw"], "Costa Rica (multi-loc)"
        )

    def test_fetch_rejects_untrusted_urls_and_malformed_payloads(self):
        for url in (
            "http://jobs.lever.co/acme",
            "https://example.com/acme",
            "https://user@jobs.lever.co/acme",
            "https://jobs.lever.co/",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.fetcher.fetch({**self.site, "base_url": url})

        invalid_responses = (
            {},
            ["not-an-object"],
            [None],
            [{"id": 1}],
        )
        for response in invalid_responses:
            with self.subTest(response=response), patch.object(
                self.fetcher, "_request_json", return_value=response
            ), self.assertRaises(LeverFetchError):
                self.fetcher.fetch(self.site)

    def test_lever_cli_imports_single_site(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "lever.db")
            database.initialize()
            repository = Repository(database)
            repository.register_platform("lever", "Lever", "api")
            repository.register_site(
                "acme-cr",
                "lever",
                "Acme Careers CR",
                "https://jobs.lever.co/acme",
                company_name="Acme",
                country="CR",
                site_config={"adapter": "lever"},
            )
            offer = {
                "source_job_id": "aa11bb22",
                "source_url": "https://jobs.lever.co/acme/aa11bb22",
                "job": {
                    "job_id": "lever-acme-aa11bb22",
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
                    "lever",
                    "--site",
                    "acme-cr",
                ],
            ), patch(
                "jobpulse_v15.cli.LeverFetcher.fetch", return_value=[offer]
            ), redirect_stdout(output):
                self.assertEqual(main(), 0)

            result = json.loads(output.getvalue())
            self.assertEqual(result["jobs_new"], 1)
            self.assertIsNotNone(repository.get_job("lever-acme-aa11bb22"))

    def test_lever_group_continues_after_site_failure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "lever-group.db")
            database.initialize()
            repository = Repository(database)
            repository.register_platform("lever", "Lever", "api")
            for site_id in ("site-good", "site-fails"):
                repository.register_site(
                    site_id,
                    "lever",
                    site_id,
                    f"https://jobs.lever.co/{site_id}",
                    run_groups=("directos",),
                )

            offer = {
                "source_job_id": "99",
                "job": {"job_id": "lever-site-good-99", "title": "Engineer"},
            }

            def fetch(site, **kwargs):
                _ = kwargs
                if site["site_id"] == "site-fails":
                    raise LeverFetchError("tablero no disponible")
                return [offer]

            output = io.StringIO()
            with patch(
                "sys.argv",
                [
                    "jobpulse-v15",
                    "--db",
                    str(database.path),
                    "scrape",
                    "lever",
                    "--group",
                    "directos",
                ],
            ), patch(
                "jobpulse_v15.cli.LeverFetcher.fetch", side_effect=fetch
            ), redirect_stdout(output):
                self.assertEqual(main(), 1)

            report = json.loads(output.getvalue())
            self.assertEqual(report["sites_selected"], 2)
            self.assertEqual(len(report["results"]), 1)
            self.assertEqual(len(report["failures"]), 1)
            self.assertIsNotNone(repository.get_job("lever-site-good-99"))
            self.assertEqual(
                repository.list_site_runs("site-fails", limit=1)[0]["site_status"],
                "error",
            )


if __name__ == "__main__":
    unittest.main()
