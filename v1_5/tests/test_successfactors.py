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
from jobpulse_v15.successfactors import (
    SuccessFactorsFetchError,
    SuccessFactorsFetcher,
)


RSS_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <item>
      <title><![CDATA[Logistics Coordinator - San Jose, Costa Rica]]></title>
      <link><![CDATA[https://careers.kuehne-nagel.com/job/San-Jose/1234567/]]></link>
      <pubDate>Thu, 08 Oct 2026 12:00:00 +0000</pubDate>
      <description><![CDATA[<p>Build <b>reliable</b> logistics.</p>]]></description>
    </item>
    <item>
      <title><![CDATA[Air Import Specialist]]></title>
      <link><![CDATA[https://careers.kuehne-nagel.com/job/Bogota/7654321/]]></link>
      <pubDate>Wed, 07 Oct 2026 12:00:00 +0000</pubDate>
      <description><![CDATA[<p>Based in Buenos Aires, Argentina.</p>]]></description>
    </item>
    <item>
      <title><![CDATA[Support Agent]]></title>
      <link><![CDATA[https://careers.kuehne-nagel.com/job/Heredia/1112223/]]></link>
      <pubDate>Tue, 06 Oct 2026 12:00:00 +0000</pubDate>
      <description><![CDATA[<p>Customer support role. Location: Costa Rica</p>]]></description>
    </item>
  </channel>
</rss>
"""


class TestSuccessFactorsFetcher(unittest.TestCase):
    def setUp(self):
        self.fetcher = SuccessFactorsFetcher()
        self.site = {
            "site_id": "kn-cr",
            "site_name": "Kuehne+Nagel Careers",
            "company_name": "Kuehne+Nagel",
            "country": "CR",
            "base_url": "https://careers.kuehne-nagel.com",
            "site_config": {"country_query": "Costa Rica"},
        }

    def test_fetch_maps_rss_feed_to_ingestion_format(self):
        with patch.object(
            self.fetcher, "_request_text", return_value=RSS_FEED
        ) as request:
            offers = self.fetcher.fetch(self.site)

        request.assert_called_once_with(
            "https://careers.kuehne-nagel.com/services/rss/job/"
            "?locale=en_US&keywords=Costa%20Rica"
        )
        self.assertEqual(
            [o["source_job_id"] for o in offers], ["1234567", "1112223"]
        )
        offer = offers[0]
        self.assertEqual(offer["job"]["job_id"], "successfactors-1234567")
        self.assertEqual(offer["job"]["company"], "Kuehne+Nagel")
        self.assertEqual(offer["job"]["location_raw"], "Costa Rica")
        self.assertEqual(offer["job"]["date_published"], "2026-10-08")
        self.assertEqual(offer["job"]["description_raw"], "Build reliable logistics.")
        self.assertEqual(
            offer["raw_payload"]["link"],
            "https://careers.kuehne-nagel.com/job/San-Jose/1234567/",
        )

    def test_fetch_keeps_jobs_when_country_appears_in_description_tail(self):
        with patch.object(
            self.fetcher, "_request_text", return_value=RSS_FEED
        ):
            offers = self.fetcher.fetch(self.site)

        ids = [o["source_job_id"] for o in offers]
        # El item de Bogota/Buenos Aires se descarta; el de Heredia se
        # mantiene porque menciona "Location: Costa Rica" al final.
        self.assertNotIn("7654321", ids)
        self.assertIn("1112223", ids)

    def test_fetch_respects_manually_skipped_source_ids(self):
        with patch.object(
            self.fetcher, "_request_text", return_value=RSS_FEED
        ):
            offers = self.fetcher.fetch(self.site, skip_source_job_ids={"1234567"})

        self.assertEqual(
            [o["source_job_id"] for o in offers], ["1112223"]
        )
        self.assertEqual(offers.jobs_skipped, 1)

    def test_fetch_rejects_untrusted_urls_and_malformed_feeds(self):
        for url in (
            "http://careers.kuehne-nagel.com",
            "https://user@careers.kuehne-nagel.com",
            "not-a-url",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.fetcher.fetch({**self.site, "base_url": url})

        for response in (
            "not xml at all",
            "<rss><channel><item><link/></item></channel></rss>",
        ):
            with self.subTest(response=response), patch.object(
                self.fetcher, "_request_text", return_value=response
            ), self.assertRaises(SuccessFactorsFetchError):
                self.fetcher.fetch(self.site)

    def test_successfactors_cli_imports_single_site(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "sf.db")
            database.initialize()
            repository = Repository(database)
            repository.register_platform("successfactors", "SuccessFactors", "rss")
            repository.register_site(
                "kn-cr",
                "successfactors",
                "Kuehne+Nagel Careers",
                "https://careers.kuehne-nagel.com",
                company_name="Kuehne+Nagel",
                country="CR",
                site_config={"adapter": "successfactors"},
            )
            offer = {
                "source_job_id": "1234567",
                "source_url": "https://careers.kuehne-nagel.com/job/San-Jose/1234567/",
                "job": {
                    "job_id": "successfactors-1234567",
                    "title": "Logistics Coordinator",
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
                    "successfactors",
                    "--site",
                    "kn-cr",
                ],
            ), patch(
                "jobpulse_v15.cli.SuccessFactorsFetcher.fetch",
                return_value=[offer],
            ), redirect_stdout(output):
                self.assertEqual(main(), 0)

            result = json.loads(output.getvalue())
            self.assertEqual(result["jobs_new"], 1)
            self.assertIsNotNone(repository.get_job("successfactors-1234567"))

    def test_successfactors_group_continues_after_site_failure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "sf-group.db")
            database.initialize()
            repository = Repository(database)
            repository.register_platform("successfactors", "SuccessFactors", "rss")
            for site_id in ("site-good", "site-fails"):
                repository.register_site(
                    site_id,
                    "successfactors",
                    site_id,
                    f"https://{site_id}.example.com",
                    run_groups=("directos",),
                )

            offer = {
                "source_job_id": "99",
                "job": {"job_id": "successfactors-99", "title": "Engineer"},
            }

            def fetch(site, **kwargs):
                _ = kwargs
                if site["site_id"] == "site-fails":
                    raise SuccessFactorsFetchError("portal no disponible")
                return [offer]

            output = io.StringIO()
            with patch(
                "sys.argv",
                [
                    "jobpulse-v15",
                    "--db",
                    str(database.path),
                    "scrape",
                    "successfactors",
                    "--group",
                    "directos",
                ],
            ), patch(
                "jobpulse_v15.cli.SuccessFactorsFetcher.fetch", side_effect=fetch
            ), redirect_stdout(output):
                self.assertEqual(main(), 1)

            report = json.loads(output.getvalue())
            self.assertEqual(report["sites_selected"], 2)
            self.assertEqual(len(report["results"]), 1)
            self.assertEqual(len(report["failures"]), 1)
            self.assertIsNotNone(repository.get_job("successfactors-99"))
            self.assertEqual(
                repository.list_site_runs("site-fails", limit=1)[0]["site_status"],
                "error",
            )


if __name__ == "__main__":
    unittest.main()
