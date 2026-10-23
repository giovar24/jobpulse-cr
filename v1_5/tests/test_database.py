import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from jobpulse_v15.db import Database, MigrationError


class TestDatabaseMigrations(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "isolated.db"
        self.database = Database(self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_initializes_schema_and_is_idempotent(self):
        self.assertEqual(self.database.initialize(), 5)
        self.assertEqual(self.database.schema_version(), 5)
        self.assertEqual(self.database.initialize(), 5)

        with self.database.connection() as connection:
            tables = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        self.assertTrue({
            "jobs",
            "source_platforms",
            "source_sites",
            "job_requirements",
            "user_job_state",
            "applications",
            "candidate_profile_facts",
            "candidate_profile_imports",
            "candidate_profile_import_snapshots",
            "job_manual_overrides",
        }.issubset(tables))
        with self.database.connection() as connection:
            analysis_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(job_analysis)")
            }
        self.assertTrue(
            {"source_site_id", "source_config_hash"}.issubset(analysis_columns)
        )

    def test_backup_creates_consistent_database_snapshot(self):
        self.database.initialize()
        with self.database.connection() as connection:
            connection.execute(
                """
                INSERT INTO source_platforms (platform_id, name, provider_type)
                VALUES ('workday', 'Workday', 'api')
                """
            )
        destination = Path(self.temp_dir.name) / "backups" / "snapshot.db"

        backup_path = self.database.backup_to(destination)

        self.assertEqual(backup_path, destination.resolve())
        with closing(sqlite3.connect(backup_path)) as connection:
            self.assertEqual(
                connection.execute("PRAGMA integrity_check").fetchone()[0], "ok"
            )
            self.assertEqual(
                connection.execute("PRAGMA user_version").fetchone()[0], 5
            )
            self.assertEqual(
                connection.execute(
                    "SELECT name FROM source_platforms WHERE platform_id='workday'"
                ).fetchone()[0],
                "Workday",
            )

    def test_backup_rejects_existing_or_active_database_destination(self):
        self.database.initialize()
        with self.assertRaisesRegex(ValueError, "base activa"):
            self.database.backup_to(self.db_path)

        existing = Path(self.temp_dir.name) / "existing.db"
        existing.write_bytes(b"keep this file")
        with self.assertRaisesRegex(FileExistsError, "ya existe"):
            self.database.backup_to(existing)
        self.assertEqual(existing.read_bytes(), b"keep this file")

    def test_backup_requires_an_existing_source_database(self):
        with self.assertRaises(FileNotFoundError):
            self.database.backup_to(Path(self.temp_dir.name) / "backup.db")

    def test_site_requires_a_registered_platform(self):
        self.database.initialize()
        with self.assertRaises(sqlite3.IntegrityError):
            with self.database.connection() as connection:
                connection.execute(
                    """
                    INSERT INTO source_sites (site_id, platform_id, site_name, base_url)
                    VALUES ('intel-jobs', 'missing', 'Intel Careers', 'https://example.test')
                    """
                )

    def test_manual_state_survives_without_job_foreign_key(self):
        self.database.initialize()
        with self.database.connection() as connection:
            state_fks = connection.execute(
                "PRAGMA foreign_key_list(user_job_state)"
            ).fetchall()
            memory_fks = connection.execute(
                "PRAGMA foreign_key_list(source_job_memory)"
            ).fetchall()

        self.assertEqual(state_fks, [])
        self.assertEqual(len(memory_fks), 1)
        self.assertEqual(memory_fks[0]["table"], "source_sites")

    def test_migration_from_v3_preserves_existing_manual_profile_facts(self):
        migrations = self.database._migrations()
        with self.database.connection() as connection:
            for version, migration in enumerate(migrations[:3], start=1):
                sql = migration.read_text(encoding="utf-8")
                connection.executescript(
                    "BEGIN IMMEDIATE;\n"
                    f"{sql}\n"
                    f"PRAGMA user_version = {version};\n"
                    "COMMIT;\n"
                )
            connection.execute(
                """
                INSERT INTO candidate_profile_facts (
                    category, value_code, value_text, notes
                ) VALUES ('skill', 'python', 'Python', 'Existing manual data')
                """
            )

        self.assertEqual(self.database.initialize(), 5)
        with self.database.connection() as connection:
            fact = connection.execute(
                """
                SELECT value_text, notes, origin, source_import_id
                FROM candidate_profile_facts WHERE value_code = 'python'
                """
            ).fetchone()
        self.assertEqual(fact["value_text"], "Python")
        self.assertEqual(fact["notes"], "Existing manual data")
        self.assertEqual(fact["origin"], "manual")
        self.assertIsNone(fact["source_import_id"])

    def test_rejects_gapped_migration_sequence(self):
        self.database.initialize()
        migration = self.database.migrations_path / "000_gap.sql"
        migration.write_text("SELECT 1;", encoding="utf-8")
        try:
            with self.assertRaises(MigrationError):
                self.database.initialize()
        finally:
            migration.unlink()


if __name__ == "__main__":
    unittest.main()
