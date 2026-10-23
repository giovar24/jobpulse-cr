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
from jobpulse_v15.greenhouse import GreenhouseFetchError, GreenhouseFetcher
from jobpulse_v15.repository import Repository


class TestGreenhouseFetcher(unittest.TestCase):
    def setUp(self):
        self.fetcher = GreenhouseFetcher()
        self.site = {
            "site_id": "acme-cr",
            "site_name": "Acme Careers CR",
            "company_name": "Acme",
            "country": "CR",
            "base_url": "https://job-boards.greenhouse.io/acme",
            "site_config": {"country_query": "Costa Rica"},
        }

    def test_fetch_maps_public_board_response_to_ingestion_format(self):
        response = {
            "jobs": [
                {
                    "id": 321,
                    "title": "Software Engineer",
                    "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/321",
                    "first_published": "2026-10-20T10:30:00-06:00",
                    "updated_at": "2026-10-21T10:30:00-06:00",
                    "location": {"name": "San José, Costa Rica"},
                    "offices": [{"name": "San José"}],
                    "departments": [{"name": "Engineering"}],
                    "content": "&lt;p&gt;Build &lt;b&gt;reliable&lt;/b&gt; systems.&lt;/p&gt;",
                }
            ]
        }
        with patch.object(
            self.fetcher, "_request_json", return_value=response
        ) as request:
            offers = self.fetcher.fetch(self.site)

        request.assert_called_once_with(
            "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true"
        )
        self.assertEqual(len(offers), 1)
        self.assertEqual(offers[0]["source_job_id"], "321")
        self.assertEqual(offers[0]["job"]["job_id"], "greenhouse-acme-321")
        self.assertEqual(offers[0]["job"]["company"], "Acme")
        self.assertEqual(offers[0]["job"]["date_published"], "2026-10-20")
        self.assertEqual(
            offers[0]["job"]["description_raw"],
            "Build reliable systems. Departamento: Engineering",
        )
        self.assertEqual(offers[0]["raw_payload"], response["jobs"][0])

    def test_fetch_filters_other_countries_and_deduplicates_ids(self):
        postings = [
            {
                "id": 1,
                "title": "Engineer",
                "location": {"name": "Toronto, Canada"},
            },
            {
                "id": 2,
                "title": "Engineer Costa Rica",
                "location": {"name": "Remote"},
                "offices": [{"name": "San José, Costa Rica"}],
            },
            {
                "id": 2,
                "title": "Duplicate listing",
                "location": {"name": "San José, Costa Rica"},
            },
        ]
        with patch.object(self.fetcher, "_request_json", return_value={"jobs": postings}):
            offers = self.fetcher.fetch(self.site)

        self.assertEqual([offer["source_job_id"] for offer in offers], ["2"])
        self.assertEqual(offers[0]["job"]["location_raw"], "Remote")

    def test_fetch_respects_manually_skipped_source_ids(self):
        posting = {
            "id": 7,
            "title": "Engineer",
            "location": {"name": "San José, Costa Rica"},
        }
        with patch.object(
            self.fetcher, "_request_json", return_value={"jobs": [posting]}
        ):
            offers = self.fetcher.fetch(self.site, skip_source_job_ids={"7"})

        self.assertEqual(offers, [])
        self.assertEqual(offers.jobs_skipped, 1)

    def test_fetch_rejects_untrusted_urls_and_malformed_responses(self):
        for url in (
            "http://job-boards.greenhouse.io/acme",
            "https://example.com/acme",
            "https://user@boards.greenhouse.io/acme",
            "https://boards.greenhouse.io/",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.fetcher.fetch({**self.site, "base_url": url})

        for response in ({}, {"jobs": "not-a-list"}):
            with self.subTest(response=response), patch.object(
                self.fetcher, "_request_json", return_value=response
            ), self.assertRaises(GreenhouseFetchError):
                self.fetcher.fetch(self.site)

    def test_greenhouse_cli_imports_single_site(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "greenhouse.db")
            database.initialize()
            repository = Repository(database)
            repository.register_platform("greenhouse", "Greenhouse", "api")
            repository.register_site(
                "acme-cr",
                "greenhouse",
                "Acme Careers CR",
                "https://job-boards.greenhouse.io/acme",
                company_name="Acme",
                country="CR",
                site_config={"adapter": "greenhouse"},
            )
            offer = {
                "source_job_id": "321",
                "source_url": "https://job-boards.greenhouse.io/acme/jobs/321",
                "job": {
                    "job_id": "greenhouse-acme-321",
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
                    "greenhouse",
                    "--site",
                    "acme-cr",
                ],
            ), patch(
                "jobpulse_v15.cli.GreenhouseFetcher.fetch",
                return_value=[offer],
            ), redirect_stdout(output):
                self.assertEqual(main(), 0)

            result = json.loads(output.getvalue())
            self.assertEqual(result["jobs_new"], 1)
            self.assertIsNotNone(repository.get_job("greenhouse-acme-321"))

    def test_greenhouse_group_continues_after_site_failure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "greenhouse-group.db")
            database.initialize()
            repository = Repository(database)
            repository.register_platform("greenhouse", "Greenhouse", "api")
            for site_id in ("site-good", "site-fails"):
                repository.register_site(
                    site_id,
                    "greenhouse",
                    site_id,
                    f"https://boards.greenhouse.io/{site_id}",
                    run_groups=("directos",),
                )

            offer = {
                "source_job_id": "99",
                "job": {"job_id": "greenhouse-site-good-99", "title": "Engineer"},
            }

            def fetch(site, **kwargs):
                _ = kwargs
                if site["site_id"] == "site-fails":
                    raise GreenhouseFetchError("portal no disponible")
                return [offer]

            output = io.StringIO()
            with patch(
                "sys.argv",
                [
                    "jobpulse-v15",
                    "--db",
                    str(database.path),
                    "scrape",
                    "greenhouse",
                    "--group",
                    "directos",
                ],
            ), patch(
                "jobpulse_v15.cli.GreenhouseFetcher.fetch", side_effect=fetch
            ), redirect_stdout(output):
                self.assertEqual(main(), 1)

            report = json.loads(output.getvalue())
            self.assertEqual(report["sites_selected"], 2)
            self.assertEqual(len(report["results"]), 1, output.getvalue())
            self.assertEqual(
                report["results"][0]["site_id"],
                "site-good",
                output.getvalue(),
            )
            self.assertEqual(report["failures"][0]["site_id"], "site-fails")
            self.assertEqual(
                repository.list_site_runs("site-fails", limit=1)[0]["site_status"],
                "error",
            )


if __name__ == "__main__":
    unittest.main()
