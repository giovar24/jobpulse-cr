"""Persistencia SQLite aislada para JobPulse CR 1.5."""

from contextlib import closing, contextmanager
from pathlib import Path
import re
import sqlite3
from typing import Iterator


_MIGRATION_NAME = re.compile(r"^(\d{3})_[a-z0-9_]+\.sql$")


class MigrationError(RuntimeError):
    """La secuencia de migraciones no es valida para esta base."""


class Database:
    """Abre e inicializa una base de datos de v1.5, nunca la base legacy."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = (
            Path(path)
            if path is not None
            else Path(__file__).resolve().parents[2] / "data" / "jobpulse_v15.db"
        )
        self.migrations_path = Path(__file__).resolve().parent / "migrations"

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """Proporciona una conexion con claves foraneas activadas y cierre seguro."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def schema_version(self) -> int:
        with self.connection() as connection:
            row = connection.execute("PRAGMA user_version").fetchone()
            return int(row[0])

    def check_integrity(self) -> dict[str, object]:
        """Reporta integridad SQLite y relaciones rotas sin modificar la base."""
        source_path = self.path.expanduser().resolve()
        if not source_path.is_file():
            raise FileNotFoundError(f"No existe la base de datos: {source_path}")
        with closing(sqlite3.connect(source_path, timeout=30)) as connection:
            integrity_results = [
                str(row[0]) for row in connection.execute("PRAGMA integrity_check")
            ]
            foreign_key_violations = [
                {
                    "table": str(row[0]),
                    "rowid": row[1],
                    "parent": str(row[2]),
                    "foreign_key_id": int(row[3]),
                }
                for row in connection.execute("PRAGMA foreign_key_check")
            ]
            schema_version = int(
                connection.execute("PRAGMA user_version").fetchone()[0]
            )
        return {
            "database": str(source_path),
            "schema_version": schema_version,
            "integrity_check": integrity_results,
            "foreign_key_violations": foreign_key_violations,
            "ok": integrity_results == ["ok"] and not foreign_key_violations,
        }

    def backup_to(self, destination: str | Path) -> Path:
        """Crea una copia SQLite consistente sin sobrescribir archivos existentes."""
        destination_path = Path(destination).expanduser().resolve()
        source_path = self.path.expanduser().resolve()
        if destination_path == source_path:
            raise ValueError("El destino del respaldo no puede ser la base activa.")
        if not source_path.is_file():
            raise FileNotFoundError(f"No existe la base de datos: {source_path}")

        destination_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with destination_path.open("xb"):
                pass
        except FileExistsError as exc:
            raise FileExistsError(
                f"El archivo de respaldo ya existe: {destination_path}"
            ) from exc

        try:
            with closing(sqlite3.connect(source_path, timeout=30)) as source:
                with closing(sqlite3.connect(destination_path, timeout=30)) as backup:
                    source.backup(backup)
                    result = backup.execute("PRAGMA integrity_check").fetchone()
                    if result is None or result[0] != "ok":
                        raise sqlite3.DatabaseError(
                            f"El respaldo no paso integrity_check: {result!r}"
                        )
        except BaseException:
            destination_path.unlink(missing_ok=True)
            raise
        return destination_path

    def initialize(self) -> int:
        """Aplica migraciones pendientes y devuelve la version resultante."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        migrations = self._migrations()

        with self.connection() as connection:
            current = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if current > len(migrations):
                raise MigrationError(
                    f"La base usa el esquema {current}, pero este codigo "
                    f"solo conoce hasta {len(migrations)}."
                )

            for expected_version, migration in enumerate(migrations, start=1):
                if expected_version <= current:
                    continue
                sql = migration.read_text(encoding="utf-8")
                try:
                    connection.executescript(
                        "BEGIN IMMEDIATE;\n"
                        f"{sql}\n"
                        f"PRAGMA user_version = {expected_version};\n"
                        "COMMIT;\n"
                    )
                except sqlite3.Error:
                    if connection.in_transaction:
                        connection.rollback()
                    raise

        return len(migrations)

    def _migrations(self) -> list[Path]:
        if not self.migrations_path.is_dir():
            raise MigrationError(f"No existe el directorio {self.migrations_path}.")

        migrations = []
        for path in sorted(self.migrations_path.glob("*.sql")):
            match = _MIGRATION_NAME.fullmatch(path.name)
            if match is None:
                raise MigrationError(f"Nombre de migracion no valido: {path.name}")
            migrations.append((int(match.group(1)), path))

        versions = [version for version, _ in migrations]
        if versions != list(range(1, len(versions) + 1)):
            raise MigrationError("Las migraciones deben ser consecutivas desde 001.")
        return [path for _, path in migrations]
