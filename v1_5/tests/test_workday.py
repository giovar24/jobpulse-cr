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
from jobpulse_v15.ingestion import IngestionService
from jobpulse_v15.repository import Repository
from jobpulse_v15.workday import MAX_PAGES, WorkdayFetchError, WorkdayFetcher


class TestWorkdayFetcher(unittest.TestCase):
    def setUp(self):
        self.fetcher = WorkdayFetcher(request_delay=0)
        self.site = {
            "site_id": "intel-cr",
            "site_name": "Intel Careers CR",
            "company_name": "Intel",
            "country": "CR",
            "base_url": "https://intel.wd1.myworkdayjobs.com/External/en-US",
            "site_config": {},
        }

    @staticmethod
    def posting(location="San Jose, Costa Rica"):
        return {
            "title": "Software Engineer",
            "locationsText": location,
            "externalPath": (
                "/job/San-Jose/Software-Engineer_JR1234567"
            ),
        }

    @staticmethod
    def detail(location="San Jose, Costa Rica"):
        return {
            "jobPostingInfo": {
                "title": "Software Engineer",
                "jobPostingId": "JR1234567",
                "externalUrl": "https://intel.wd1.myworkdayjobs.com/External/job/1",
                "location": location,
                "jobDescription": (
                    "<div>Build <b>reliable</b> software.</div>"
                    "<p>Fluent English required.</p>"
                ),
                "startDate": "2026-10-20",
                "timeType": "Full time",
            }
        }

    def test_fetch_maps_single_board_response_to_v15_ingestion_format(self):
        with patch.object(
            self.fetcher,
            "_request_json",
            side_effect=[
                {"jobPostings": [self.posting()]},
                self.detail(),
            ],
        ) as request:
            offers = self.fetcher.fetch(self.site, pages=1)

        self.assertEqual(len(offers), 1)
        offer = offers[0]
        self.assertEqual(offer["source_job_id"], "JR1234567")
        self.assertEqual(offer["job"]["job_id"], "workday-JR1234567")
        self.assertEqual(offer["job"]["company"], "Intel")
        self.assertEqual(offer["job"]["country"], "CR")
        self.assertIsNone(offer["job"]["city"])
        self.assertIsNone(offer["job"]["region"])
        self.assertEqual(offer["job"]["location_raw"], "San Jose, Costa Rica")
        self.assertEqual(offer["job"]["work_mode"], "unknown")
        self.assertEqual(offer["job"]["date_published"], "2026-10-20")
        self.assertEqual(
            offer["job"]["description_raw"],
            "Build reliable software. Fluent English required.",
        )
        self.assertEqual(len(offer["raw_payload"]), 2)
        self.assertEqual(
            request.call_args_list[0].args[0],
            "https://intel.wd1.myworkdayjobs.com"
            "/wday/cxs/intel/External/jobs",
        )
        self.assertEqual(
            request.call_args_list[1].args[0],
            "https://intel.wd1.myworkdayjobs.com"
            "/wday/cxs/intel/External/job/San-Jose/Software-Engineer_JR1234567",
        )
        self.assertEqual(request.call_args_list[0].kwargs["payload"]["searchText"],
                         "Costa Rica")

    def test_fetch_normalizes_province_only_location_without_inventing_city(self):
        with patch.object(
            self.fetcher,
            "_request_json",
            side_effect=[
                {"jobPostings": [self.posting("Guanacaste, Costa Rica")]},
                self.detail("Guanacaste, Costa Rica"),
            ],
        ):
            offer = self.fetcher.fetch(self.site)[0]

        self.assertIsNone(offer["job"]["city"])
        self.assertEqual(offer["job"]["region"], "Guanacaste")
        self.assertEqual(offer["job"]["location_raw"], "Guanacaste, Costa Rica")

    def test_fetch_splits_city_and_province_when_both_are_explicit(self):
        location = "Liberia, Guanacaste, Costa Rica"
        with patch.object(
            self.fetcher,
            "_request_json",
            side_effect=[
                {"jobPostings": [self.posting(location)]},
                self.detail(location),
            ],
        ):
            offer = self.fetcher.fetch(self.site)[0]

        self.assertEqual(offer["job"]["city"], "Liberia")
        self.assertEqual(offer["job"]["region"], "Guanacaste")
        self.assertEqual(offer["job"]["location_raw"], location)

    def test_fetch_does_not_guess_between_capital_city_and_province(self):
        location = "San Jose, Costa Rica"
        with patch.object(
            self.fetcher,
            "_request_json",
            side_effect=[
                {"jobPostings": [self.posting(location)]},
                self.detail(location),
            ],
        ):
            offer = self.fetcher.fetch(self.site)[0]

        self.assertIsNone(offer["job"]["city"])
        self.assertIsNone(offer["job"]["region"])
        self.assertEqual(offer["job"]["location_raw"], location)

    def test_fetch_leaves_ambiguous_location_unstructured(self):
        location = "San Jose, Central, Costa Rica"
        with patch.object(
            self.fetcher,
            "_request_json",
            side_effect=[
                {"jobPostings": [self.posting(location)]},
                self.detail(location),
            ],
        ):
            offer = self.fetcher.fetch(self.site)[0]

        self.assertIsNone(offer["job"]["city"])
        self.assertIsNone(offer["job"]["region"])
        self.assertEqual(offer["job"]["location_raw"], location)

    def test_manual_state_skips_workday_detail_and_is_not_counted_as_zero_results(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "manual-skip.db")
            database.initialize()
            repository = Repository(database)
            repository.register_platform("workday", "Workday", "api")
            repository.register_site(
                "intel-cr",
                "workday",
                "Intel Careers CR",
                self.site["base_url"],
                country="CR",
            )
            repository.save_job(
                {
                    "job_id": "workday-JR1234567",
                    "title": "Software Engineer",
                },
                site_id="intel-cr",
                source_job_id="JR1234567",
                source_url="https://example.test/jobs/1",
            )
            repository.set_user_job_state("workday-JR1234567", "applied")
            skipped_ids = repository.list_skipped_source_job_ids("intel-cr")
            fetcher = WorkdayFetcher(request_delay=0)

            with patch.object(
                fetcher,
                "_request_json",
                return_value={"jobPostings": [self.posting()]},
            ) as request:
                result = IngestionService(repository).fetch_and_import(
                    "intel-cr",
                    lambda: fetcher.fetch(
                        self.site,
                        skip_source_job_ids=skipped_ids,
                    ),
                    run_kind="workday",
                )

            request.assert_called_once()
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["jobs_found"], 1)
            self.assertEqual(result["jobs_valid"], 0)
            self.assertEqual(result["jobs_skipped"], 1)
            run = repository.get_run(result["run_id"])
            self.assertEqual(run["sites"][0]["status"], "ok")
            self.assertEqual(run["sites"][0]["jobs_found"], 1)

    def test_fetch_uses_configured_search_and_does_not_import_other_country(self):
        site = {
            **self.site,
            "site_config": {
                "search_text": "Costa Rica",
                "country_query": "Costa Rica",
                "country": "CR",
            },
        }
        with patch.object(
            self.fetcher,
            "_request_json",
            side_effect=[
                {"jobPostings": [self.posting("Austin, Texas")]},
                self.detail("Austin, Texas"),
            ],
        ):
            self.assertEqual(self.fetcher.fetch(site), [])

    def test_invalid_board_url_and_page_limits_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "URL Workday invalida"):
            self.fetcher.fetch({**self.site, "base_url": "https://example.com/jobs"})
        with self.assertRaisesRegex(ValueError, "pages debe estar"):
            self.fetcher.fetch(self.site, pages=MAX_PAGES + 1)

    def test_malformed_workday_response_is_an_error_not_zero_results(self):
        with patch.object(
            self.fetcher, "_request_json", return_value={"unexpected": []}
        ):
            with self.assertRaisesRegex(WorkdayFetchError, "jobPostings"):
                self.fetcher.fetch(self.site)

    def test_cli_fetch_is_explicit_and_persists_to_v15(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "cli.db")
            database.initialize()
            repository = Repository(database)
            repository.register_platform("workday", "Workday", "api")
            repository.register_site(
                "intel-cr",
                "workday",
                "Intel Careers CR",
                self.site["base_url"],
                company_name="Intel",
                country="CR",
            )
            output = io.StringIO()
            with patch(
                "jobpulse_v15.cli.WorkdayFetcher.fetch",
                return_value=[
                    {
                        "source_job_id": "JR1234567",
                        "source_url": "https://intel.example/job/1",
                        "job": {
                            "job_id": "workday-JR1234567",
                            "title": "Software Engineer",
                            "country": "CR",
                        },
                    }
                ],
            ) as fetch, patch(
                "sys.argv",
                [
                    "jobpulse-v15",
                    "--db",
                    str(database.path),
                    "scrape",
                    "workday",
                    "--site",
                    "intel-cr",
                ],
            ), redirect_stdout(output):
                self.assertEqual(main(), 0)

            fetch.assert_called_once()
            result = json.loads(output.getvalue())
            self.assertEqual(result["jobs_new"], 1)
            self.assertEqual(result["status"], "ok")
            self.assertIsNotNone(repository.get_job("workday-JR1234567"))

    def test_location_backfill_defaults_to_preview_and_preserves_existing_values(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "locations.db")
            database.initialize()
            repository = Repository(database)
            repository.register_platform("workday", "Workday", "api")
            repository.register_site(
                "intel-cr",
                "workday",
                "Intel Careers CR",
                self.site["base_url"],
                country="CR",
            )
            location = "Liberia, Guanacaste, Costa Rica"
            repository.save_job(
                {
                    "job_id": "workday-REQ123456",
                    "title": "Software Engineer",
                    "city": "Verified City",
                    "location_raw": location,
                },
                site_id="intel-cr",
                source_job_id="REQ123456",
                raw_payload={
                    "listing": self.posting(location),
                    "detail": self.detail(location),
                },
            )
            ambiguous_location = "San Jose, Costa Rica"
            repository.save_job(
                {
                    "job_id": "workday-REQ654321",
                    "title": "Test Engineer",
                    "city": "San Jose",
                    "location_raw": ambiguous_location,
                },
                site_id="intel-cr",
                source_job_id="REQ654321",
                raw_payload={
                    "listing": self.posting(ambiguous_location),
                    "detail": self.detail(ambiguous_location),
                },
            )
            output = io.StringIO()
            command = [
                "jobpulse-v15",
                "--db",
                str(database.path),
                "location",
                "normalize-workday",
            ]
            with patch("sys.argv", command), redirect_stdout(output):
                self.assertEqual(main(), 0)

            preview = json.loads(output.getvalue())
            self.assertEqual(preview["mode"], "dry_run")
            self.assertEqual(preview["records_scanned"], 2)
            self.assertEqual(preview["skipped_unrecognized"], 1)
            self.assertEqual(len(preview["updates"]), 1)
            self.assertEqual(preview["updates"][0]["city"], "Verified City")
            self.assertEqual(preview["updates"][0]["region"], "Guanacaste")
            self.assertIsNone(repository.get_job("workday-REQ123456")["region"])

            output = io.StringIO()
            with patch("sys.argv", [*command, "--apply"]), redirect_stdout(output):
                self.assertEqual(main(), 0)

            applied = json.loads(output.getvalue())
            self.assertEqual(applied["mode"], "apply")
            self.assertTrue(applied["updates"][0]["updated"])
            job = repository.get_job("workday-REQ123456")
            self.assertEqual(job["city"], "Verified City")
            self.assertEqual(job["region"], "Guanacaste")
            ambiguous_job = repository.get_job("workday-REQ654321")
            self.assertEqual(ambiguous_job["city"], "San Jose")
            self.assertIsNone(ambiguous_job["region"])
            records = repository.list_workday_location_records("intel-cr")
            self.assertEqual(records[0]["location_raw"], location)

    def test_cli_search_can_include_manually_skipped_jobs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "managed-search.db")
            database.initialize()
            repository = Repository(database)
            repository.register_platform("workday", "Workday", "api")
            repository.register_site(
                "intel-cr",
                "workday",
                "Intel Careers CR",
                self.site["base_url"],
                country="CR",
            )
            for job_id in ("job-applied", "job-open"):
                repository.save_job(
                    {"job_id": job_id, "title": job_id},
                    site_id="intel-cr",
                    source_job_id=job_id,
                )
            repository.set_user_job_state("job-applied", "applied")
            command = [
                "jobpulse-v15",
                "--db",
                str(database.path),
                "search",
            ]

            output = io.StringIO()
            with patch("sys.argv", command), redirect_stdout(output):
                self.assertEqual(main(), 0)
            visible = json.loads(output.getvalue())
            self.assertEqual([job["job_id"] for job in visible], ["job-open"])

            output = io.StringIO()
            with patch(
                "sys.argv", [*command, "--include-manually-skipped"]
            ), redirect_stdout(output):
                self.assertEqual(main(), 0)
            included = json.loads(output.getvalue())
            self.assertEqual(
                {job["job_id"] for job in included},
                {"job-applied", "job-open"},
            )

    def test_cli_fetch_error_is_recorded_in_the_run(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "failure.db")
            database.initialize()
            repository = Repository(database)
            repository.register_platform("workday", "Workday", "api")
            repository.register_site(
                "intel-cr",
                "workday",
                "Intel Careers CR",
                self.site["base_url"],
                country="CR",
            )
            with patch(
                "jobpulse_v15.cli.WorkdayFetcher.fetch",
                side_effect=WorkdayFetchError("HTTP 503"),
            ), patch(
                "sys.argv",
                [
                    "jobpulse-v15",
                    "--db",
                    str(database.path),
                    "scrape",
                    "workday",
                    "--site",
                    "intel-cr",
                ],
            ), self.assertRaises(SystemExit):
                main()

            runs = repository.list_runs()
            self.assertEqual(len(runs), 1)
            run = repository.get_run(runs[0]["run_id"])
            self.assertEqual(run["status"], "error")
            self.assertIn("HTTP 503", run["sites"][0]["error_message"])

    def test_cli_rejects_non_workday_site_before_fetch(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "not-workday.db")
            database.initialize()
            repository = Repository(database)
            repository.register_platform("other", "Other", "api")
            repository.register_site(
                "other-site",
                "other",
                "Other site",
                "https://example.test/jobs",
            )
            with patch(
                "jobpulse_v15.cli.WorkdayFetcher.fetch"
            ) as fetch, patch(
                "sys.argv",
                [
                    "jobpulse-v15",
                    "--db",
                    str(database.path),
                    "scrape",
                    "workday",
                    "--site",
                    "other-site",
                ],
            ), self.assertRaises(SystemExit):
                main()
            fetch.assert_not_called()
            self.assertEqual(repository.list_runs(), [])


if __name__ == "__main__":
    unittest.main()
