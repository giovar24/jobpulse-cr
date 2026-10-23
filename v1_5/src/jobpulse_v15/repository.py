"""Acceso a catalogos de sitios y vacantes estructuradas de v1.5."""

import json
from collections.abc import Mapping, Sequence
from datetime import date
import math
import unicodedata
from typing import Any
from urllib.parse import urlparse

from jobpulse_v15.db import Database


REQUIREMENT_CATEGORIES = {
    "language",
    "education",
    "license",
    "experience",
    "skill",
    "certification",
    "work_authorization",
    "schedule",
    "availability",
    "other",
}
FIELD_STATUSES = {"found", "not_mentioned", "unknown", "ambiguous"}
ANALYSIS_STATUSES = {"complete", "partial", "error"}
REQUIREMENT_LEVELS = {"required", "preferred", "inferred", "ambiguous"}
REQUIREMENT_ORIGINS = {"source_field", "rule", "local_ai", "api", "manual"}
JOB_MANUAL_OVERRIDE_FIELDS = {
    "title",
    "company",
    "country",
    "region",
    "city",
    "work_mode",
}
USER_JOB_STATUSES = {"reviewed", "applied", "archived", "dismissed"}
APPLICATION_STATUSES = {
    "planned",
    "applied",
    "recruiter_contact",
    "interview",
    "offer",
    "rejected",
    "withdrawn",
}
JOB_REVIEW_REASON_CODES = {
    "missing_company",
    "missing_country",
    "location_not_normalized",
    "analysis_missing",
    "analysis_pending",
    "analysis_partial",
    "analysis_error",
}
JOB_REVIEW_FIELD_PREFIXES = {"unknown_field", "ambiguous_field"}


def job_review_reasons(
    job: Mapping[str, Any],
    analysis_status: str | None,
    field_statuses: Mapping[str, str],
) -> list[str]:
    """Devuelve codigos legibles por maquina para problemas de calidad observados."""
    reasons = []
    if not str(job.get("company") or "").strip():
        reasons.append("missing_company")
    if not str(job.get("country") or "").strip():
        reasons.append("missing_country")
    if (
        str(job.get("location_raw") or "").strip()
        and not str(job.get("city") or "").strip()
        and not str(job.get("region") or "").strip()
    ):
        reasons.append("location_not_normalized")

    if analysis_status is None:
        reasons.append("analysis_missing")
    elif analysis_status in {"pending", "partial", "error"}:
        reasons.append(f"analysis_{analysis_status}")
    for category, status in sorted(field_statuses.items()):
        if status in {"unknown", "ambiguous"}:
            reasons.append(f"{status}_field:{category}")
    return reasons


class Repository:
    """Operaciones de persistencia usadas por CLI y futura UI/orquestador."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def register_platform(
        self,
        platform_id: str,
        name: str,
        provider_type: str,
        default_language: str | None = None,
        default_config: Mapping[str, Any] | None = None,
    ) -> None:
        self._required_text(platform_id, "platform_id")
        self._required_text(name, "name")
        self._required_text(provider_type, "provider_type")
        self._validate_language(default_language)
        config_json = self._json_object(
            default_config, "default_config", empty_object=True
        )
        self._reject_credentials(default_config or {}, "default_config")

        with self.database.connection() as connection:
            connection.execute(
                """
                INSERT INTO source_platforms (
                    platform_id, name, provider_type, default_language,
                    default_config_json
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(platform_id) DO UPDATE SET
                    name = excluded.name,
                    provider_type = excluded.provider_type,
                    default_language = excluded.default_language,
                    default_config_json = excluded.default_config_json,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (platform_id, name, provider_type, default_language, config_json),
            )

    def get_platform(self, platform_id: str) -> dict[str, Any] | None:
        """Devuelve la configuracion declarativa de una plataforma."""
        self._required_text(platform_id, "platform_id")
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM source_platforms WHERE platform_id = ?",
                (platform_id,),
            ).fetchone()
        if row is None:
            return None
        platform = dict(row)
        platform["default_config"] = json.loads(
            platform.pop("default_config_json")
        )
        platform["enabled"] = bool(platform["enabled"])
        return platform

    def register_site(
        self,
        site_id: str,
        platform_id: str,
        site_name: str,
        base_url: str,
        *,
        company_name: str | None = None,
        country: str | None = None,
        run_groups: Sequence[str] = (),
        site_config: Mapping[str, Any] | None = None,
        enabled: bool = True,
    ) -> None:
        self._required_text(site_id, "site_id")
        self._required_text(platform_id, "platform_id")
        self._required_text(site_name, "site_name")
        self._validate_url(base_url)
        self._validate_groups(run_groups)
        config_json = self._json_object(site_config, "site_config", empty_object=True)
        self._reject_credentials(site_config or {})

        with self.database.connection() as connection:
            connection.execute(
                """
                INSERT INTO source_sites (
                    site_id, platform_id, site_name, company_name, base_url,
                    country, enabled, run_groups_json, site_config_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(site_id) DO UPDATE SET
                    platform_id = excluded.platform_id,
                    site_name = excluded.site_name,
                    company_name = excluded.company_name,
                    base_url = excluded.base_url,
                    country = excluded.country,
                    enabled = excluded.enabled,
                    run_groups_json = excluded.run_groups_json,
                    site_config_json = excluded.site_config_json,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    site_id,
                    platform_id,
                    site_name,
                    self._optional_text(company_name),
                    base_url.strip(),
                    self._optional_text(country),
                    int(enabled),
                    json.dumps(list(run_groups), ensure_ascii=False, sort_keys=True),
                    config_json,
                ),
            )

    def set_site_enabled(self, site_id: str, enabled: bool) -> bool:
        """Activa o pausa un sitio; devuelve False si no existe."""
        with self.database.connection() as connection:
            cursor = connection.execute(
                """
                UPDATE source_sites
                SET enabled = ?, updated_at = CURRENT_TIMESTAMP
                WHERE site_id = ?
                """,
                (int(enabled), site_id),
            )
            return cursor.rowcount == 1

    def mark_site_reviewed(self, site_id: str) -> bool:
        """Quita la bandera de revision sin ocultar el ultimo estado observado."""
        with self.database.connection() as connection:
            cursor = connection.execute(
                """
                UPDATE source_sites
                SET review_required = 0,
                    health_status = CASE
                        WHEN health_status = 'needs_review' THEN 'zero_results'
                        ELSE health_status
                    END,
                    updated_at = CURRENT_TIMESTAMP
                WHERE site_id = ?
                """,
                (site_id,),
            )
            return cursor.rowcount == 1

    def list_sites(
        self, *, enabled_only: bool = False, run_group: str | None = None
    ) -> list[dict[str, Any]]:
        """Lista sitios; el filtro de grupo usa la configuracion validada."""
        if run_group is not None:
            self._required_text(run_group, "run_group")

        query = """
            SELECT s.*, p.name AS platform_name, p.provider_type
            FROM source_sites AS s
            JOIN source_platforms AS p ON p.platform_id = s.platform_id
        """
        conditions = []
        if enabled_only:
            conditions.append("s.enabled = 1")
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY p.name, s.site_name"

        with self.database.connection() as connection:
            rows = connection.execute(query).fetchall()

        sites = []
        for row in rows:
            site = dict(row)
            site["run_groups"] = json.loads(site.pop("run_groups_json"))
            site["site_config"] = json.loads(site.pop("site_config_json"))
            site["enabled"] = bool(site["enabled"])
            site["review_required"] = bool(site["review_required"])
            if run_group is None or run_group in site["run_groups"]:
                sites.append(site)
        return sites

    def get_site(self, site_id: str) -> dict[str, Any] | None:
        """Devuelve sitio y plataforma para validar su adaptador de ingesta."""
        self._required_text(site_id, "site_id")
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT s.*, p.name AS platform_name, p.provider_type
                FROM source_sites AS s
                JOIN source_platforms AS p ON p.platform_id = s.platform_id
                WHERE s.site_id = ?
                """,
                (site_id,),
            ).fetchone()
        if row is None:
            return None
        site = dict(row)
        site["run_groups"] = json.loads(site.pop("run_groups_json"))
        site["site_config"] = json.loads(site.pop("site_config_json"))
        site["enabled"] = bool(site["enabled"])
        site["review_required"] = bool(site["review_required"])
        return site

    def select_sites(self, run_group: str | None = None) -> list[dict[str, Any]]:
        """Seleccion central para futuras corridas: solo sitios habilitados."""
        return self.list_sites(enabled_only=True, run_group=run_group)

    def start_run(
        self,
        run_kind: str = "full",
        run_group: str | None = None,
    ) -> int:
        """Crea corrida e incluye la seleccion actual de sitios habilitados."""
        self._required_text(run_kind, "run_kind")
        sites = self.select_sites(run_group)
        if not sites:
            raise ValueError("No hay sitios habilitados para esta corrida.")

        with self.database.connection() as connection:
            cursor = connection.execute(
                "INSERT INTO source_runs (run_kind) VALUES (?)", (run_kind.strip(),)
            )
            run_id = int(cursor.lastrowid)
            connection.executemany(
                """
                INSERT INTO source_run_sites (run_id, site_id)
                VALUES (?, ?)
                """,
                [(run_id, site["site_id"]) for site in sites],
            )
        return run_id

    def start_site_run(self, site_id: str, run_kind: str = "import") -> int:
        """Crea una corrida acotada a un sitio habilitado."""
        self._required_text(site_id, "site_id")
        self._required_text(run_kind, "run_kind")
        with self.database.connection() as connection:
            site = connection.execute(
                "SELECT enabled FROM source_sites WHERE site_id = ?", (site_id,)
            ).fetchone()
            if site is None:
                raise ValueError(f"El sitio {site_id!r} no existe.")
            if not site["enabled"]:
                raise ValueError(f"El sitio {site_id!r} esta deshabilitado.")
            cursor = connection.execute(
                "INSERT INTO source_runs (run_kind) VALUES (?)", (run_kind.strip(),)
            )
            run_id = int(cursor.lastrowid)
            connection.execute(
                """
                INSERT INTO source_run_sites (run_id, site_id)
                VALUES (?, ?)
                """,
                (run_id, site_id),
            )
        return run_id

    def record_site_result(
        self,
        run_id: int,
        site_id: str,
        *,
        jobs_found: int,
        jobs_valid: int,
        jobs_new: int,
        jobs_duplicate: int,
        error_message: str | None = None,
        review_after_zero_runs: int = 3,
    ) -> str:
        """Registra el resultado por sitio y actualiza su salud sin auto-pausar."""
        counts = {
            "jobs_found": self._nonnegative_int(jobs_found, "jobs_found"),
            "jobs_valid": self._nonnegative_int(jobs_valid, "jobs_valid"),
            "jobs_new": self._nonnegative_int(jobs_new, "jobs_new"),
            "jobs_duplicate": self._nonnegative_int(
                jobs_duplicate, "jobs_duplicate"
            ),
        }
        review_after_zero_runs = self._nonnegative_int(
            review_after_zero_runs, "review_after_zero_runs"
        )
        if review_after_zero_runs == 0:
            raise ValueError("review_after_zero_runs debe ser mayor que cero.")
        if counts["jobs_valid"] > counts["jobs_found"]:
            raise ValueError("jobs_valid no puede superar jobs_found.")
        if counts["jobs_new"] + counts["jobs_duplicate"] > counts["jobs_valid"]:
            raise ValueError("jobs_new + jobs_duplicate no puede superar jobs_valid.")
        error_message = self._optional_text(error_message)

        if error_message:
            status = "error"
        elif counts["jobs_found"] == 0:
            status = "zero_results"
        else:
            status = "ok"

        with self.database.connection() as connection:
            run = connection.execute(
                "SELECT status FROM source_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if run is None:
                raise ValueError(f"La corrida {run_id} no existe.")
            if run["status"] != "running":
                raise ValueError(f"La corrida {run_id} ya fue cerrada.")

            run_site = connection.execute(
                """
                SELECT status FROM source_run_sites
                WHERE run_id = ? AND site_id = ?
                """,
                (run_id, site_id),
            ).fetchone()
            if run_site is None:
                raise ValueError(
                    f"El sitio {site_id!r} no pertenece a la corrida {run_id}."
                )
            if run_site["status"] != "running":
                raise ValueError(
                    f"Ya existe un resultado para {site_id!r} en la corrida {run_id}."
                )

            connection.execute(
                """
                UPDATE source_run_sites
                SET status = ?, jobs_found = ?, jobs_valid = ?, jobs_new = ?,
                    jobs_duplicate = ?, error_message = ?, review_required = ?,
                    finished_at = CURRENT_TIMESTAMP
                WHERE run_id = ? AND site_id = ?
                """,
                (
                    status,
                    counts["jobs_found"],
                    counts["jobs_valid"],
                    counts["jobs_new"],
                    counts["jobs_duplicate"],
                    error_message,
                    int(status == "error"),
                    run_id,
                    site_id,
                ),
            )

            site = connection.execute(
                """
                SELECT consecutive_zero_runs
                FROM source_sites
                WHERE site_id = ?
                """,
                (site_id,),
            ).fetchone()
            if status == "error":
                connection.execute(
                    """
                    UPDATE source_sites
                    SET health_status = 'error', review_required = 1,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE site_id = ?
                    """,
                    (site_id,),
                )
            elif status == "zero_results":
                zero_runs = int(site["consecutive_zero_runs"]) + 1
                needs_review = zero_runs >= review_after_zero_runs
                connection.execute(
                    """
                    UPDATE source_sites
                    SET health_status = ?, consecutive_zero_runs = ?,
                        review_required = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE site_id = ?
                    """,
                    (
                        "needs_review" if needs_review else "zero_results",
                        zero_runs,
                        int(needs_review),
                        site_id,
                    ),
                )
                connection.execute(
                    """
                    UPDATE source_run_sites
                    SET review_required = ?
                    WHERE run_id = ? AND site_id = ?
                    """,
                    (int(needs_review), run_id, site_id),
                )
            else:
                connection.execute(
                    """
                    UPDATE source_sites
                    SET health_status = 'healthy', consecutive_zero_runs = 0,
                        review_required = 0, updated_at = CURRENT_TIMESTAMP
                    WHERE site_id = ?
                    """,
                    (site_id,),
                )

        return status

    def finish_run(self, run_id: int, summary: str | None = None) -> str:
        """Cierra una corrida solo cuando todos sus sitios tienen resultado."""
        summary = self._optional_text(summary)
        with self.database.connection() as connection:
            run = connection.execute(
                "SELECT status FROM source_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if run is None:
                raise ValueError(f"La corrida {run_id} no existe.")
            if run["status"] != "running":
                raise ValueError(f"La corrida {run_id} ya fue cerrada.")

            results = connection.execute(
                "SELECT status FROM source_run_sites WHERE run_id = ?",
                (run_id,),
            ).fetchall()
            if not results:
                raise ValueError(f"La corrida {run_id} no tiene sitios.")
            statuses = [row["status"] for row in results]
            if "running" in statuses:
                raise ValueError(
                    f"La corrida {run_id} tiene sitios sin resultado."
                )

            successful = {"ok", "zero_results"}
            if all(status in successful for status in statuses):
                status = "ok"
            elif all(item == "error" for item in statuses):
                status = "error"
            else:
                status = "partial"
            connection.execute(
                """
                UPDATE source_runs
                SET status = ?, summary = ?, finished_at = CURRENT_TIMESTAMP
                WHERE run_id = ?
                """,
                (status, summary, run_id),
            )
        return status

    def get_run(self, run_id: int) -> dict[str, Any] | None:
        """Devuelve la corrida con sus resultados por sitio."""
        with self.database.connection() as connection:
            run_row = connection.execute(
                "SELECT * FROM source_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if run_row is None:
                return None
            run = dict(run_row)
            run["sites"] = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT r.*, s.site_name, s.platform_id, p.name AS platform_name
                    FROM source_run_sites AS r
                    JOIN source_sites AS s ON s.site_id = r.site_id
                    JOIN source_platforms AS p ON p.platform_id = s.platform_id
                    WHERE r.run_id = ?
                    ORDER BY p.name, s.site_name
                    """,
                    (run_id,),
                ).fetchall()
            ]
        return run

    def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        limit = self._positive_int(limit, "limit")
        with self.database.connection() as connection:
            runs = connection.execute(
                """
                SELECT r.*, COUNT(s.site_id) AS site_count,
                    SUM(CASE WHEN s.status = 'error' THEN 1 ELSE 0 END) AS error_count,
                    SUM(CASE WHEN s.status = 'zero_results' THEN 1 ELSE 0 END)
                        AS zero_result_count,
                    SUM(COALESCE(s.jobs_found, 0)) AS jobs_found,
                    SUM(COALESCE(s.jobs_new, 0)) AS jobs_new
                FROM source_runs AS r
                LEFT JOIN source_run_sites AS s ON s.run_id = r.run_id
                GROUP BY r.run_id
                ORDER BY r.run_id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in runs]

    def list_site_runs(
        self, site_id: str, limit: int = 20
    ) -> list[dict[str, Any]]:
        """Lista las corridas recientes de una fuente y sus resultados."""
        self._required_text(site_id, "site_id")
        limit = self._positive_int(limit, "limit")
        with self.database.connection() as connection:
            runs = connection.execute(
                """
                SELECT r.run_id, r.run_kind, r.status AS run_status,
                    r.started_at, r.finished_at,
                    s.status AS site_status, s.jobs_found, s.jobs_valid,
                    s.jobs_new, s.jobs_duplicate, s.error_message,
                    s.started_at AS site_started_at,
                    s.finished_at AS site_finished_at
                FROM source_run_sites AS s
                JOIN source_runs AS r ON r.run_id = s.run_id
                WHERE s.site_id = ?
                ORDER BY r.run_id DESC
                LIMIT ?
                """,
                (site_id.strip(), limit),
            ).fetchall()
        return [dict(row) for row in runs]

    def save_job(
        self,
        job: Mapping[str, Any],
        *,
        site_id: str,
        source_job_id: str,
        source_url: str | None = None,
        raw_payload: Mapping[str, Any] | None = None,
        source_updated_at: str | None = None,
        content_hash: str | None = None,
    ) -> bool:
        """Guarda la oferta y su identidad de origen de forma atomica."""
        self._required_text(site_id, "site_id")
        self._required_text(source_job_id, "source_job_id")
        job_id = self._required_text(job.get("job_id"), "job.job_id")
        title = self._required_text(job.get("title"), "job.title")
        self._validate_url(source_url, optional=True)

        posting_language = job.get("posting_language")
        if posting_language is not None and posting_language not in {
            "es", "en", "mixed", "undetermined"
        }:
            raise ValueError("posting_language no es un valor permitido.")
        language_origin = job.get("posting_language_origin")
        if language_origin is not None and language_origin not in {
            "source", "detected", "unknown"
        }:
            raise ValueError("posting_language_origin no es un valor permitido.")

        optional_fields = {
            key: self._optional_text(job.get(key))
            for key in (
                "company",
                "location_raw",
                "country",
                "region",
                "city",
                "employment_type",
                "schedule",
                "salary_raw",
                "salary_currency",
                "salary_period",
                "description_raw",
                "requirements_raw",
                "date_published",
                "expires_at",
            )
        }
        work_mode = job.get("work_mode")
        if work_mode not in {None, "onsite", "hybrid", "remote", "unknown"}:
            raise ValueError("work_mode no es un valor permitido.")

        salary_min = self._optional_number(job.get("salary_min"), "salary_min")
        salary_max = self._optional_number(job.get("salary_max"), "salary_max")
        if salary_min is not None and salary_max is not None and salary_min > salary_max:
            raise ValueError("salary_min no puede superar salary_max.")
        raw_json = self._json_object(raw_payload, "raw_payload")
        if source_url is not None:
            source_url = source_url.strip()

        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            site = connection.execute(
                "SELECT 1 FROM source_sites WHERE site_id = ?", (site_id,)
            ).fetchone()
            if site is None:
                raise ValueError(f"El sitio {site_id!r} no existe.")
            source_already_exists = connection.execute(
                """
                SELECT 1 FROM job_source_records
                WHERE site_id = ? AND source_job_id = ?
                """,
                (site_id, source_job_id),
            ).fetchone() is not None

            connection.execute(
                """
                INSERT INTO jobs (
                    job_id, title, company, location_raw, country, region, city,
                    work_mode, employment_type, schedule, posting_language,
                    posting_language_origin, date_published, expires_at,
                    salary_raw, salary_min, salary_max, salary_currency,
                    salary_period, description_raw, requirements_raw
                ) VALUES (
                    :job_id, :title, :company, :location_raw, :country, :region,
                    :city, :work_mode, :employment_type, :schedule,
                    COALESCE(:posting_language, 'undetermined'),
                    COALESCE(:posting_language_origin, 'unknown'), :date_published,
                    :expires_at, :salary_raw, :salary_min, :salary_max,
                    :salary_currency, :salary_period, :description_raw,
                    :requirements_raw
                )
                ON CONFLICT(job_id) DO UPDATE SET
                    title = excluded.title,
                    company = COALESCE(excluded.company, jobs.company),
                    location_raw = COALESCE(excluded.location_raw, jobs.location_raw),
                    country = COALESCE(excluded.country, jobs.country),
                    region = COALESCE(excluded.region, jobs.region),
                    city = COALESCE(excluded.city, jobs.city),
                    work_mode = COALESCE(excluded.work_mode, jobs.work_mode),
                    employment_type = COALESCE(excluded.employment_type, jobs.employment_type),
                    schedule = COALESCE(excluded.schedule, jobs.schedule),
                    posting_language = CASE
                        WHEN :posting_language_provided
                        THEN excluded.posting_language ELSE jobs.posting_language END,
                    posting_language_origin = CASE
                        WHEN :language_origin_provided
                        THEN excluded.posting_language_origin
                        ELSE jobs.posting_language_origin END,
                    date_published = COALESCE(excluded.date_published, jobs.date_published),
                    expires_at = COALESCE(excluded.expires_at, jobs.expires_at),
                    salary_raw = COALESCE(excluded.salary_raw, jobs.salary_raw),
                    salary_min = COALESCE(excluded.salary_min, jobs.salary_min),
                    salary_max = COALESCE(excluded.salary_max, jobs.salary_max),
                    salary_currency = COALESCE(excluded.salary_currency, jobs.salary_currency),
                    salary_period = COALESCE(excluded.salary_period, jobs.salary_period),
                    description_raw = COALESCE(excluded.description_raw, jobs.description_raw),
                    requirements_raw = COALESCE(excluded.requirements_raw, jobs.requirements_raw),
                    last_seen_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                """,
                {
                    "job_id": job_id,
                    "title": title,
                    **optional_fields,
                    "work_mode": work_mode,
                    "posting_language": posting_language,
                    "posting_language_origin": language_origin,
                    "posting_language_provided": int(posting_language is not None),
                    "language_origin_provided": int(language_origin is not None),
                    "salary_min": salary_min,
                    "salary_max": salary_max,
                },
            )
            source_values = {
                "title": title,
                "company": optional_fields["company"],
                "country": optional_fields["country"],
                "region": optional_fields["region"],
                "city": optional_fields["city"],
                "work_mode": work_mode,
            }
            for override in connection.execute(
                """
                SELECT field_name, override_value_json
                FROM job_manual_overrides WHERE job_id = ?
                """,
                (job_id,),
            ).fetchall():
                field_name = str(override["field_name"])
                source_value = source_values[field_name]
                if source_value is not None:
                    connection.execute(
                        """
                        UPDATE job_manual_overrides
                        SET source_value_json = ?, updated_at = CURRENT_TIMESTAMP
                        WHERE job_id = ? AND field_name = ?
                        """,
                        (
                            json.dumps(source_value, ensure_ascii=False),
                            job_id,
                            field_name,
                        ),
                    )
                connection.execute(
                    f"UPDATE jobs SET {field_name} = ? WHERE job_id = ?",
                    (json.loads(override["override_value_json"]), job_id),
                )
            connection.execute(
                """
                INSERT INTO job_source_records (
                    site_id, source_job_id, job_id, source_url, raw_payload_json,
                    source_updated_at, content_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(site_id, source_job_id) DO UPDATE SET
                    job_id = excluded.job_id,
                    source_url = COALESCE(excluded.source_url, job_source_records.source_url),
                    raw_payload_json = COALESCE(
                        excluded.raw_payload_json, job_source_records.raw_payload_json
                    ),
                    source_updated_at = COALESCE(
                        excluded.source_updated_at, job_source_records.source_updated_at
                    ),
                    last_seen_at = CURRENT_TIMESTAMP,
                    content_hash = COALESCE(
                        excluded.content_hash, job_source_records.content_hash
                    )
                """,
                (
                    site_id,
                    source_job_id,
                    job_id,
                    source_url,
                    raw_json,
                    source_updated_at,
                    content_hash,
                ),
            )
            connection.execute(
                """
                INSERT INTO source_job_memory (
                    site_id, source_job_id, content_hash
                ) VALUES (?, ?, ?)
                ON CONFLICT(site_id, source_job_id) DO UPDATE SET
                    last_seen_at = CURRENT_TIMESTAMP,
                    content_hash = COALESCE(
                        excluded.content_hash, source_job_memory.content_hash
                    )
                """,
                (site_id, source_job_id, content_hash),
            )
        return not source_already_exists

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        return dict(row) if row else None

    def get_data_quality_summary(self) -> dict[str, int]:
        """Cuenta faltantes y estados de revision sobre todas las vacantes."""
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT
                    COUNT(*) AS total_jobs,
                    SUM(CASE WHEN NULLIF(trim(j.company), '') IS NULL
                        THEN 1 ELSE 0 END) AS missing_company,
                    SUM(CASE WHEN NULLIF(trim(j.country), '') IS NULL
                        THEN 1 ELSE 0 END) AS missing_country,
                    SUM(CASE
                        WHEN NULLIF(trim(j.location_raw), '') IS NOT NULL
                         AND NULLIF(trim(j.city), '') IS NULL
                         AND NULLIF(trim(j.region), '') IS NULL
                        THEN 1 ELSE 0 END) AS location_not_normalized,
                    SUM(CASE WHEN ja.analysis_id IS NULL
                        THEN 1 ELSE 0 END) AS analysis_missing,
                    SUM(CASE WHEN ja.analysis_status = 'pending'
                        THEN 1 ELSE 0 END) AS analysis_pending,
                    SUM(CASE WHEN ja.analysis_status = 'partial'
                        THEN 1 ELSE 0 END) AS analysis_partial,
                    SUM(CASE WHEN ja.analysis_status = 'error'
                        THEN 1 ELSE 0 END) AS analysis_error,
                    SUM(CASE WHEN EXISTS (
                        SELECT 1 FROM job_field_status AS jfs
                        WHERE jfs.analysis_id = ja.analysis_id
                          AND jfs.field_status = 'unknown'
                    ) THEN 1 ELSE 0 END) AS unknown_fields,
                    SUM(CASE WHEN EXISTS (
                        SELECT 1 FROM job_field_status AS jfs
                        WHERE jfs.analysis_id = ja.analysis_id
                          AND jfs.field_status = 'ambiguous'
                    ) THEN 1 ELSE 0 END) AS ambiguous_fields,
                    SUM(CASE WHEN
                        NULLIF(trim(j.company), '') IS NULL
                        OR NULLIF(trim(j.country), '') IS NULL
                        OR (
                            NULLIF(trim(j.location_raw), '') IS NOT NULL
                            AND NULLIF(trim(j.city), '') IS NULL
                            AND NULLIF(trim(j.region), '') IS NULL
                        )
                        OR ja.analysis_id IS NULL
                        OR ja.analysis_status IN ('pending', 'partial', 'error')
                        OR EXISTS (
                            SELECT 1 FROM job_field_status AS jfs
                            WHERE jfs.analysis_id = ja.analysis_id
                              AND jfs.field_status IN ('unknown', 'ambiguous')
                        )
                        THEN 1 ELSE 0 END) AS jobs_needing_review
                FROM jobs AS j
                LEFT JOIN job_analysis AS ja
                    ON ja.job_id = j.job_id AND ja.is_current = 1
                """
            ).fetchone()
        summary = {key: int(row[key] or 0) for key in row.keys()}
        summary["potential_duplicate_groups"] = self.get_potential_duplicate_groups(
            limit=1
        )["total_groups"]
        return summary

    def get_potential_duplicate_groups(self, limit: int = 100) -> dict[str, Any]:
        """Agrupa vacantes con titulo, empresa, pais y ciudad normalizados iguales."""
        limit = self._positive_int(limit, "limit")
        if limit > 500:
            raise ValueError("limit no puede superar 500.")

        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT job_id, title, company, country, city
                FROM jobs
                WHERE NULLIF(trim(title), '') IS NOT NULL
                  AND NULLIF(trim(company), '') IS NOT NULL
                  AND NULLIF(trim(country), '') IS NOT NULL
                  AND NULLIF(trim(city), '') IS NOT NULL
                ORDER BY job_id
                """
            ).fetchall()

        grouped: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        for row in rows:
            title = self._normalize_duplicate_component(row["title"])
            company = self._normalize_duplicate_component(row["company"])
            country = self._normalize_duplicate_component(row["country"])
            city = self._normalize_duplicate_component(row["city"])
            key = (title, company, country, city)
            group = grouped.get(key)
            if group is None:
                group = {
                    "title": str(row["title"]).strip(),
                    "company": str(row["company"]).strip(),
                    "country": str(row["country"]).strip(),
                    "city": str(row["city"]).strip(),
                    "job_ids": [],
                }
                grouped[key] = group
            group["job_ids"].append(str(row["job_id"]))

        candidates = [
            {**group, "count": len(group["job_ids"])}
            for group in grouped.values()
            if len(group["job_ids"]) > 1
        ]
        candidates.sort(
            key=lambda group: (
                -group["count"],
                group["title"].casefold(),
                group["company"].casefold(),
                group["country"].casefold(),
                group["city"].casefold(),
            )
        )
        return {
            "total_groups": len(candidates),
            "groups": candidates[:limit],
        }

    @staticmethod
    def _normalize_duplicate_component(value: str) -> str:
        decomposed = unicodedata.normalize("NFKD", value).casefold()
        without_accents = "".join(
            char for char in decomposed if unicodedata.category(char) != "Mn"
        )
        return " ".join(
            "".join(char if char.isalnum() else " " for char in without_accents).split()
        )

    def update_job_manual_overrides(
        self, job_id: str, overrides: Mapping[str, Any]
    ) -> None:
        """Actualiza campos normalizados y conserva el dato de origen previo."""
        job_id = self._required_text(job_id, "job_id")
        if not isinstance(overrides, Mapping) or not overrides:
            raise ValueError("overrides debe ser un objeto no vacio.")
        unknown_fields = set(overrides) - JOB_MANUAL_OVERRIDE_FIELDS
        if unknown_fields:
            raise ValueError(
                "Campos no editables: " + ", ".join(sorted(unknown_fields)) + "."
            )

        normalized: dict[str, str | None] = {}
        for field_name, value in overrides.items():
            if field_name == "work_mode":
                if value is not None and (
                    not isinstance(value, str)
                    or value not in {
                    "onsite", "hybrid", "remote", "unknown"
                    }
                ):
                    raise ValueError("work_mode no es un valor permitido.")
                normalized[field_name] = value
            else:
                if value is not None and not isinstance(value, str):
                    raise ValueError(f"{field_name} debe ser texto.")
                normalized[field_name] = self._optional_text(value)
            if field_name == "title" and normalized[field_name] is None:
                raise ValueError("title es obligatorio.")

        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if current is None:
                raise ValueError(f"La vacante {job_id!r} no existe.")
            for field_name, value in normalized.items():
                existing = connection.execute(
                    """
                    SELECT source_value_json FROM job_manual_overrides
                    WHERE job_id = ? AND field_name = ?
                    """,
                    (job_id, field_name),
                ).fetchone()
                source_value_json = (
                    existing["source_value_json"]
                    if existing is not None
                    else json.dumps(current[field_name], ensure_ascii=False)
                )
                connection.execute(
                    """
                    INSERT INTO job_manual_overrides (
                        job_id, field_name, source_value_json, override_value_json
                    ) VALUES (?, ?, ?, ?)
                    ON CONFLICT(job_id, field_name) DO UPDATE SET
                        override_value_json = excluded.override_value_json,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (
                        job_id,
                        field_name,
                        source_value_json,
                        json.dumps(value, ensure_ascii=False),
                    ),
                )
                connection.execute(
                    f"UPDATE jobs SET {field_name} = ?, updated_at = CURRENT_TIMESTAMP "
                    "WHERE job_id = ?",
                    (value, job_id),
                )

    def clear_job_manual_overrides(self, job_id: str) -> int:
        """Restaura los valores normalizados mas recientes recibidos de la fuente."""
        job_id = self._required_text(job_id, "job_id")
        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute(
                "SELECT 1 FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone() is None:
                raise ValueError(f"La vacante {job_id!r} no existe.")
            overrides = connection.execute(
                """
                SELECT field_name, source_value_json FROM job_manual_overrides
                WHERE job_id = ?
                """,
                (job_id,),
            ).fetchall()
            for override in overrides:
                field_name = str(override["field_name"])
                source_value = json.loads(override["source_value_json"])
                connection.execute(
                    f"UPDATE jobs SET {field_name} = ?, updated_at = CURRENT_TIMESTAMP "
                    "WHERE job_id = ?",
                    (source_value, job_id),
                )
            connection.execute(
                "DELETE FROM job_manual_overrides WHERE job_id = ?", (job_id,)
            )
        return len(overrides)

    def get_user_job_state(self, job_id: str) -> dict[str, Any] | None:
        self._required_text(job_id, "job_id")
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM user_job_state WHERE job_id = ?",
                (job_id.strip(),),
            ).fetchone()
        if row is None:
            return None
        state = dict(row)
        state["skip_automatic"] = bool(state["skip_automatic"])
        return state

    def list_skipped_source_job_ids(self, site_id: str) -> set[str]:
        """Devuelve identidades de origen marcadas para omitir detalles."""
        self._required_text(site_id, "site_id")
        with self.database.connection() as connection:
            site = connection.execute(
                """
                SELECT platform_id FROM source_sites WHERE site_id = ?
                """,
                (site_id.strip(),),
            ).fetchone()
            if site is None:
                raise ValueError(f"No existe el sitio {site_id!r}.")
            if site["platform_id"] != "workday":
                raise ValueError(f"El sitio {site_id!r} no es Workday.")
            rows = connection.execute(
                """
                SELECT r.source_job_id
                FROM job_source_records AS r
                JOIN user_job_state AS u ON u.job_id = r.job_id
                WHERE r.site_id = ? AND u.skip_automatic = 1
                """,
                (site_id.strip(),),
            ).fetchall()
        return {str(row["source_job_id"]) for row in rows}

    def set_user_job_state(
        self,
        job_id: str,
        user_status: str,
        *,
        skip_automatic: bool = True,
        notes: str | None = None,
    ) -> None:
        """Guarda el estado manual sin ligar su duracion a la oferta."""
        self._required_text(job_id, "job_id")
        if user_status not in USER_JOB_STATUSES:
            raise ValueError("user_status no es un estado manual permitido.")
        if not isinstance(skip_automatic, bool):
            raise ValueError("skip_automatic debe ser booleano.")
        notes_provided = notes is not None
        notes = self._optional_text(notes)
        with self.database.connection() as connection:
            job = connection.execute(
                "SELECT title, company FROM jobs WHERE job_id = ?",
                (job_id.strip(),),
            ).fetchone()
            if job is None:
                raise ValueError(f"La vacante {job_id!r} no existe.")
            source = connection.execute(
                """
                SELECT source_url FROM job_source_records
                WHERE job_id = ?
                ORDER BY last_seen_at DESC, site_id
                LIMIT 1
                """,
                (job_id.strip(),),
            ).fetchone()
            connection.execute(
                """
                INSERT INTO user_job_state (
                    job_id, title_snapshot, company_snapshot, source_url_snapshot,
                    user_status, skip_automatic, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    title_snapshot = excluded.title_snapshot,
                    company_snapshot = excluded.company_snapshot,
                    source_url_snapshot = COALESCE(
                        excluded.source_url_snapshot,
                        user_job_state.source_url_snapshot
                    ),
                    user_status = excluded.user_status,
                    skip_automatic = excluded.skip_automatic,
                    notes = CASE WHEN ? THEN excluded.notes
                        ELSE user_job_state.notes END,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    job_id.strip(),
                    job["title"],
                    job["company"],
                    source["source_url"] if source is not None else None,
                    user_status,
                    int(skip_automatic),
                    notes,
                    int(notes_provided),
                ),
            )

    def clear_user_job_state(self, job_id: str) -> bool:
        self._required_text(job_id, "job_id")
        with self.database.connection() as connection:
            cursor = connection.execute(
                "DELETE FROM user_job_state WHERE job_id = ?",
                (job_id.strip(),),
            )
            return cursor.rowcount == 1

    def record_application(
        self,
        job_id: str,
        application_status: str,
        *,
        applied_at: str | None = None,
        next_action_at: str | None = None,
        notes: str | None = None,
    ) -> int:
        """Agrega un evento al seguimiento opcional de una postulacion."""
        self._required_text(job_id, "job_id")
        if application_status not in APPLICATION_STATUSES:
            raise ValueError("application_status no es un estado permitido.")
        applied_at = self._validate_optional_date(applied_at, "applied_at")
        next_action_at = self._validate_optional_date(
            next_action_at, "next_action_at"
        )
        notes = self._optional_text(notes)
        with self.database.connection() as connection:
            if connection.execute(
                "SELECT 1 FROM jobs WHERE job_id = ?", (job_id.strip(),)
            ).fetchone() is None:
                raise ValueError(f"La vacante {job_id!r} no existe.")
            cursor = connection.execute(
                """
                INSERT INTO applications (
                    job_id, application_status, applied_at, next_action_at, notes
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    job_id.strip(),
                    application_status,
                    applied_at,
                    next_action_at,
                    notes,
                ),
            )
            return int(cursor.lastrowid)

    def list_applications(self, job_id: str) -> list[dict[str, Any]]:
        """Devuelve los eventos de seguimiento en orden cronologico."""
        self._required_text(job_id, "job_id")
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM applications
                WHERE job_id = ?
                ORDER BY created_at, application_id
                """,
                (job_id.strip(),),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_workday_location_records(
        self, site_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Lista ofertas Workday junto con su payload y configuracion originales."""
        query = """
            SELECT j.job_id, j.city, j.region, j.location_raw, s.site_id,
                s.country AS site_country, s.site_config_json,
                r.raw_payload_json
            FROM job_source_records AS r
            JOIN jobs AS j ON j.job_id = r.job_id
            JOIN source_sites AS s ON s.site_id = r.site_id
            JOIN source_platforms AS p ON p.platform_id = s.platform_id
            WHERE p.platform_id = 'workday'
        """
        parameters: tuple[str, ...] = ()
        if site_id is not None:
            self._required_text(site_id, "site_id")
            query += " AND s.site_id = ?"
            parameters = (site_id.strip(),)
        query += " ORDER BY j.job_id, s.site_id"

        with self.database.connection() as connection:
            rows = connection.execute(query, parameters).fetchall()
            if site_id is not None and not rows:
                site = connection.execute(
                    """
                    SELECT p.platform_id
                    FROM source_sites AS s
                    JOIN source_platforms AS p ON p.platform_id = s.platform_id
                    WHERE s.site_id = ?
                    """,
                    (site_id.strip(),),
                ).fetchone()
                if site is None:
                    raise ValueError(f"No existe el sitio {site_id!r}.")
                if site["platform_id"] != "workday":
                    raise ValueError(f"El sitio {site_id!r} no es Workday.")

        records = []
        for row in rows:
            record = dict(row)
            try:
                record["site_config"] = json.loads(record.pop("site_config_json"))
                raw_payload = record.pop("raw_payload_json")
                record["raw_payload"] = (
                    json.loads(raw_payload) if raw_payload is not None else None
                )
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"El payload guardado para {record['site_id']}/"
                    f"{record['job_id']} no es JSON valido."
                ) from exc
            records.append(record)
        return records

    def update_missing_job_location(
        self, job_id: str, *, city: str | None, region: str | None
    ) -> bool:
        """Completa solo campos de ubicacion vacios, sin reemplazar datos existentes."""
        self._required_text(job_id, "job_id")
        city = self._optional_text(city)
        region = self._optional_text(region)
        if city is None and region is None:
            raise ValueError("Se requiere una ciudad o region normalizada.")
        with self.database.connection() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs
                SET city = COALESCE(NULLIF(trim(city), ''), ?),
                    region = COALESCE(NULLIF(trim(region), ''), ?),
                    updated_at = CURRENT_TIMESTAMP
                WHERE job_id = ?
                    AND (
                        city IS NULL OR trim(city) = ''
                        OR region IS NULL OR trim(region) = ''
                    )
                """,
                (city, region, job_id.strip()),
            )
            return cursor.rowcount == 1

    def get_job_source_context(self, job_id: str) -> dict[str, str] | None:
        """Devuelve el sitio y la configuracion de la fuente mas reciente."""
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT source_sites.site_id, source_sites.site_config_json
                FROM job_source_records
                JOIN source_sites ON source_sites.site_id = job_source_records.site_id
                WHERE job_source_records.job_id = ?
                ORDER BY job_source_records.last_seen_at DESC,
                         job_source_records.site_id
                LIMIT 1
                """,
                (job_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_job_sources(self, job_id: str) -> list[dict[str, Any]]:
        """Devuelve los enlaces de origen de una vacante."""
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT source_sites.site_id, source_sites.site_name,
                       job_source_records.source_job_id,
                       job_source_records.source_url,
                       job_source_records.last_seen_at
                FROM job_source_records
                JOIN source_sites ON source_sites.site_id = job_source_records.site_id
                WHERE job_source_records.job_id = ?
                ORDER BY job_source_records.last_seen_at DESC,
                         source_sites.site_name
                """,
                (job_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def save_analysis(
        self,
        job_id: str,
        *,
        text_hash: str,
        extractor_version: str,
        analysis_status: str,
        field_statuses: Mapping[str, str],
        requirements: Sequence[Mapping[str, Any]],
        source_site_id: str | None = None,
        source_config_hash: str | None = None,
        error_message: str | None = None,
    ) -> int:
        """Guarda una revision del analisis, evidencias y estados atomicamente."""
        self._required_text(job_id, "job_id")
        self._required_text(text_hash, "text_hash")
        self._required_text(extractor_version, "extractor_version")
        if analysis_status not in ANALYSIS_STATUSES:
            raise ValueError("analysis_status no es valido.")
        if not isinstance(field_statuses, Mapping):
            raise ValueError("field_statuses debe ser un objeto por categoria.")
        if not isinstance(requirements, Sequence) or isinstance(
            requirements, (str, bytes)
        ):
            raise ValueError("requirements debe ser una lista de requisitos.")
        normalized_statuses = {
            self._validate_category(category): status
            for category, status in field_statuses.items()
        }
        for category, status in normalized_statuses.items():
            if not isinstance(status, str) or status not in FIELD_STATUSES:
                raise ValueError(f"Estado no valido para {category}: {status!r}.")

        normalized_requirements = [
            self._validate_requirement(requirement) for requirement in requirements
        ]
        found_categories = {
            category
            for category, status in normalized_statuses.items()
            if status == "found"
        }
        recorded_categories = {
            requirement["category"] for requirement in normalized_requirements
        }
        if found_categories - recorded_categories:
            missing = ", ".join(sorted(found_categories - recorded_categories))
            raise ValueError(f"Hay campos marcados found sin requisitos: {missing}.")

        for requirement in normalized_requirements:
            category_status = normalized_statuses.get(requirement["category"])
            if category_status is None:
                raise ValueError(
                    f"Falta el estado del campo {requirement['category']}."
                )
            if category_status in {"not_mentioned", "unknown"}:
                raise ValueError(
                    f"No puede guardarse un requisito para {requirement['category']} "
                    f"cuando su estado es {category_status}."
                )
            if category_status == "ambiguous" and (
                requirement["requirement_level"] != "ambiguous"
            ):
                raise ValueError(
                    f"Un requisito ambiguo de {requirement['category']} debe "
                    "usar requirement_level='ambiguous'."
                )
            if (
                category_status == "found"
                and requirement["requirement_level"] == "ambiguous"
            ):
                raise ValueError(
                    f"Un requisito ambiguo de {requirement['category']} requiere "
                    "field_status='ambiguous'."
                )
        if analysis_status == "complete" and any(
            status in {"unknown", "ambiguous"}
            for status in normalized_statuses.values()
        ):
            raise ValueError(
                "Un analisis complete no puede contener categorias unknown "
                "o ambiguous."
            )
        if analysis_status == "error" and not self._optional_text(error_message):
            raise ValueError("error_message es obligatorio para analysis_status=error.")

        source_site_id = self._optional_text(source_site_id)
        source_config_hash = self._optional_text(source_config_hash)
        error_message = self._optional_text(error_message)

        with self.database.connection() as connection:
            if connection.execute(
                "SELECT 1 FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone() is None:
                raise ValueError(f"La vacante {job_id!r} no existe.")
            if source_site_id is not None and connection.execute(
                "SELECT 1 FROM source_sites WHERE site_id = ?", (source_site_id,)
            ).fetchone() is None:
                raise ValueError(f"El sitio {source_site_id!r} no existe.")

            connection.execute(
                "UPDATE job_analysis SET is_current = 0 WHERE job_id = ?",
                (job_id,),
            )
            existing = connection.execute(
                """
                SELECT analysis_id FROM job_analysis
                WHERE job_id = ? AND text_hash = ? AND extractor_version = ?
                """,
                (job_id, text_hash, extractor_version),
            ).fetchone()
            if existing:
                analysis_id = int(existing["analysis_id"])
                connection.execute(
                    """
                    UPDATE job_analysis
                    SET analysis_status = ?, is_current = 1,
                        analyzed_at = CURRENT_TIMESTAMP, error_message = ?,
                        source_site_id = ?, source_config_hash = ?
                    WHERE analysis_id = ?
                    """,
                    (
                        analysis_status,
                        error_message,
                        source_site_id,
                        source_config_hash,
                        analysis_id,
                    ),
                )
                connection.execute(
                    "DELETE FROM job_requirements WHERE analysis_id = ?",
                    (analysis_id,),
                )
                connection.execute(
                    "DELETE FROM job_field_status WHERE analysis_id = ?",
                    (analysis_id,),
                )
            else:
                cursor = connection.execute(
                    """
                    INSERT INTO job_analysis (
                        job_id, text_hash, extractor_version, analysis_status,
                        is_current, error_message, source_site_id, source_config_hash
                    ) VALUES (?, ?, ?, ?, 1, ?, ?, ?)
                    """,
                    (
                        job_id,
                        text_hash,
                        extractor_version,
                        analysis_status,
                        error_message,
                        source_site_id,
                        source_config_hash,
                    ),
                )
                analysis_id = int(cursor.lastrowid)

            connection.executemany(
                """
                INSERT INTO job_field_status (analysis_id, category, field_status)
                VALUES (?, ?, ?)
                """,
                [
                    (analysis_id, category, status)
                    for category, status in normalized_statuses.items()
                ],
            )
            connection.executemany(
                """
                INSERT INTO job_requirements (
                    analysis_id, category, value_code, value_text, qualifier,
                    requirement_level, education_status, minimum, maximum,
                    unit, evidence, origin, confidence
                ) VALUES (
                    :analysis_id, :category, :value_code, :value_text,
                    :qualifier, :requirement_level, :education_status,
                    :minimum, :maximum, :unit, :evidence, :origin, :confidence
                )
                """,
                [
                    {"analysis_id": analysis_id, **requirement}
                    for requirement in normalized_requirements
                ],
            )
        return analysis_id

    def search_jobs(
        self,
        *,
        text: str | None = None,
        requirements: Sequence[Mapping[str, Any]] = (),
        filters: Mapping[str, Any] | None = None,
        requirement_levels: Sequence[str] = ("required",),
        limit: int = 50,
        include_manually_skipped: bool = False,
        needs_review_only: bool = False,
        review_reason: str | None = None,
        sort_by: str = "last_seen",
        published_from: str | None = None,
        published_to: str | None = None,
        salary_at_least: float | None = None,
        salary_at_most: float | None = None,
    ) -> list[dict[str, Any]]:
        """Consulta empleos por filtros SQL y requisitos del analisis vigente.

        Cada requisito solicitado se aplica como una condicion AND sobre la
        oferta; sus coincidencias se comprueban con EXISTS para no duplicarla.
        La revision incluye campos normalizados esenciales faltantes, vacantes
        sin analisis o con analisis parcial/error y categorias desconocidas o
        ambiguas.
        """
        limit = self._positive_int(limit, "limit")
        if not isinstance(requirements, Sequence) or isinstance(
            requirements, (str, bytes)
        ):
            raise ValueError("requirements debe ser una lista.")
        if not requirement_levels or any(
            level not in REQUIREMENT_LEVELS for level in requirement_levels
        ):
            raise ValueError("requirement_levels contiene un estado no valido.")
        if filters is None:
            filters = {}
        if not isinstance(filters, Mapping):
            raise ValueError("filters debe ser un objeto.")
        if not isinstance(include_manually_skipped, bool):
            raise ValueError("include_manually_skipped debe ser booleano.")
        if not isinstance(needs_review_only, bool):
            raise ValueError("needs_review_only debe ser booleano.")
        if sort_by not in {"last_seen", "date_published"}:
            raise ValueError("sort_by debe ser 'last_seen' o 'date_published'.")
        published_from = self._validate_optional_date(
            published_from, "published_from"
        )
        published_to = self._validate_optional_date(published_to, "published_to")
        if (
            published_from is not None
            and published_to is not None
            and published_from > published_to
        ):
            raise ValueError("published_from no puede ser posterior a published_to.")
        salary_at_least = self._optional_number(
            salary_at_least, "salary_at_least"
        )
        salary_at_most = self._optional_number(salary_at_most, "salary_at_most")
        if salary_at_least is not None and salary_at_least < 0:
            raise ValueError("salary_at_least no puede ser negativo.")
        if salary_at_most is not None and salary_at_most < 0:
            raise ValueError("salary_at_most no puede ser negativo.")
        if (
            salary_at_least is not None
            and salary_at_most is not None
            and salary_at_least > salary_at_most
        ):
            raise ValueError("salary_at_least no puede superar salary_at_most.")
        has_salary_currency = (
            isinstance(filters.get("salary_currency"), str)
            and bool(filters["salary_currency"].strip())
        )
        has_salary_period = (
            isinstance(filters.get("salary_period"), str)
            and bool(filters["salary_period"].strip())
        )
        if (salary_at_least is not None or salary_at_most is not None) and not (
            has_salary_currency and has_salary_period
        ):
            raise ValueError(
                "Los limites salariales requieren moneda y periodo."
            )
        if review_reason is not None:
            if not isinstance(review_reason, str):
                raise ValueError("review_reason debe ser texto.")
            prefix, separator, category = review_reason.partition(":")
            is_field_reason = (
                prefix in JOB_REVIEW_FIELD_PREFIXES
                and separator
                and category in REQUIREMENT_CATEGORIES
            )
            if review_reason not in JOB_REVIEW_REASON_CODES and not is_field_reason:
                raise ValueError("review_reason no es un motivo permitido.")

        normalized_requirements = [
            self._validate_requirement_filter(requirement)
            for requirement in requirements
        ]
        if text is not None and not isinstance(text, str):
            raise ValueError("text debe ser una cadena.")
        search_text = text.strip() if text is not None else ""
        allowed_filters = {
            "country": "country",
            "region": "region",
            "city": "city",
            "work_mode": "work_mode",
            "employment_type": "employment_type",
            "schedule": "schedule",
            "posting_language": "posting_language",
            "salary_currency": "salary_currency",
            "salary_period": "salary_period",
        }
        conditions = []
        params: list[Any] = []
        if published_from is not None:
            conditions.append("j.date_published >= ?")
            params.append(published_from)
        if published_to is not None:
            conditions.append("j.date_published <= ?")
            params.append(published_to)
        if search_text:
            escaped_text = (
                search_text.replace("!", "!!").replace("%", "!%").replace("_", "!_")
            )
            conditions.append(
                "("
                "j.title LIKE ? ESCAPE '!' COLLATE NOCASE OR "
                "j.company LIKE ? ESCAPE '!' COLLATE NOCASE OR "
                "j.location_raw LIKE ? ESCAPE '!' COLLATE NOCASE OR "
                "j.description_raw LIKE ? ESCAPE '!' COLLATE NOCASE OR "
                "j.requirements_raw LIKE ? ESCAPE '!' COLLATE NOCASE"
                ")"
            )
            params.extend([f"%{escaped_text}%"] * 5)
        for filter_name, value in filters.items():
            column = allowed_filters.get(filter_name)
            if column is None:
                raise ValueError(f"Filtro no soportado: {filter_name}.")
            if value is None or value == "":
                continue
            if filter_name in {
                "employment_type",
                "schedule",
                "salary_currency",
                "salary_period",
            } and not isinstance(value, str):
                raise ValueError(f"{filter_name} debe ser texto.")
            if filter_name in {
                "employment_type",
                "schedule",
                "salary_currency",
                "salary_period",
            }:
                value = value.strip()
                if not value:
                    continue
            if filter_name == "work_mode" and value not in {
                "onsite", "hybrid", "remote", "unknown"
            }:
                raise ValueError("work_mode no es valido.")
            if filter_name == "posting_language" and value not in {
                "es", "en", "mixed", "undetermined"
            }:
                raise ValueError("posting_language no es valido.")
            conditions.append(f"j.{column} = ?")
            params.append(value)
        if salary_at_least is not None:
            conditions.append(
                "(j.salary_min IS NOT NULL OR j.salary_max IS NOT NULL) "
                "AND (j.salary_max IS NULL OR j.salary_max >= ?)"
            )
            params.append(salary_at_least)
        if salary_at_most is not None:
            conditions.append(
                "(j.salary_min IS NOT NULL OR j.salary_max IS NOT NULL) "
                "AND (j.salary_min IS NULL OR j.salary_min <= ?)"
            )
            params.append(salary_at_most)

        levels_placeholders = ",".join("?" for _ in requirement_levels)
        for requirement in normalized_requirements:
            predicates = [
                "ja.job_id = j.job_id",
                "ja.is_current = 1",
                f"jr.requirement_level IN ({levels_placeholders})",
                "jr.category = ?",
            ]
            requirement_params: list[Any] = list(requirement_levels)
            requirement_params.append(requirement["category"])
            if requirement.get("value_code") is not None:
                predicates.append("LOWER(jr.value_code) = LOWER(?)")
                requirement_params.append(requirement["value_code"])
            if requirement.get("qualifier") is not None:
                predicates.append("LOWER(jr.qualifier) = LOWER(?)")
                requirement_params.append(requirement["qualifier"])
            if requirement.get("education_status") is not None:
                predicates.append("jr.education_status = ?")
                requirement_params.append(requirement["education_status"])
            if requirement.get("minimum") is not None:
                predicates.append("jr.minimum >= ?")
                requirement_params.append(requirement["minimum"])
            conditions.append(
                """
                EXISTS (
                    SELECT 1
                    FROM job_analysis AS ja
                    JOIN job_requirements AS jr ON jr.analysis_id = ja.analysis_id
                    WHERE """ + " AND ".join(predicates) + """
                )
                """
            )
            params.extend(requirement_params)

        if review_reason is not None:
            reason_conditions = {
                "missing_company": "NULLIF(trim(j.company), '') IS NULL",
                "missing_country": "NULLIF(trim(j.country), '') IS NULL",
                "location_not_normalized": (
                    "NULLIF(trim(j.location_raw), '') IS NOT NULL "
                    "AND NULLIF(trim(j.city), '') IS NULL "
                    "AND NULLIF(trim(j.region), '') IS NULL"
                ),
                "analysis_missing": (
                    "NOT EXISTS (SELECT 1 FROM job_analysis AS ja "
                    "WHERE ja.job_id = j.job_id AND ja.is_current = 1)"
                ),
            }
            if review_reason in reason_conditions:
                conditions.append(reason_conditions[review_reason])
            elif review_reason.startswith("analysis_"):
                status = review_reason.removeprefix("analysis_")
                conditions.append(
                    "EXISTS (SELECT 1 FROM job_analysis AS ja "
                    "WHERE ja.job_id = j.job_id AND ja.is_current = 1 "
                    "AND ja.analysis_status = ?)"
                )
                params.append(status)
            else:
                field_status, category = review_reason.split(":", 1)
                conditions.append(
                    """
                    EXISTS (
                        SELECT 1
                        FROM job_analysis AS ja
                        JOIN job_field_status AS jfs
                            ON jfs.analysis_id = ja.analysis_id
                        WHERE ja.job_id = j.job_id AND ja.is_current = 1
                          AND jfs.category = ? AND jfs.field_status = ?
                    )
                    """
                )
                params.extend((category, field_status.removesuffix("_field")))

        if needs_review_only:
            conditions.append(
                """
                (
                    NULLIF(trim(j.company), '') IS NULL
                    OR NULLIF(trim(j.country), '') IS NULL
                    OR (
                        NULLIF(trim(j.location_raw), '') IS NOT NULL
                        AND NULLIF(trim(j.city), '') IS NULL
                        AND NULLIF(trim(j.region), '') IS NULL
                    )
                    OR
                    NOT EXISTS (
                        SELECT 1 FROM job_analysis AS ja
                        WHERE ja.job_id = j.job_id AND ja.is_current = 1
                    )
                    OR EXISTS (
                        SELECT 1 FROM job_analysis AS ja
                        WHERE ja.job_id = j.job_id AND ja.is_current = 1
                          AND (
                              ja.analysis_status IN ('pending', 'partial', 'error')
                              OR EXISTS (
                                  SELECT 1 FROM job_field_status AS jfs
                                  WHERE jfs.analysis_id = ja.analysis_id
                                    AND jfs.field_status IN ('unknown', 'ambiguous')
                              )
                          )
                    )
                )
                """
            )

        query = """
            SELECT j.*,
                (SELECT r.source_url FROM job_source_records AS r
                 WHERE r.job_id = j.job_id AND r.source_url IS NOT NULL
                 ORDER BY r.last_seen_at DESC, r.site_id LIMIT 1) AS source_url,
                (SELECT u.user_status FROM user_job_state AS u
                 WHERE u.job_id = j.job_id) AS user_status,
                COALESCE(
                    (SELECT u.skip_automatic FROM user_job_state AS u
                     WHERE u.job_id = j.job_id), 0
                ) AS skip_automatic
            FROM jobs AS j
        """
        if not include_manually_skipped:
            conditions.append(
                "NOT EXISTS ("
                "SELECT 1 FROM user_job_state AS u "
                "WHERE u.job_id = j.job_id AND u.skip_automatic = 1"
                ")"
            )
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        if sort_by == "date_published":
            query += (
                " ORDER BY (j.date_published IS NULL), j.date_published DESC, "
                "j.last_seen_at DESC, j.job_id LIMIT ?"
            )
        else:
            query += " ORDER BY j.last_seen_at DESC, j.job_id LIMIT ?"
        params.append(limit)
        with self.database.connection() as connection:
            rows = connection.execute(query, params).fetchall()
            jobs = [dict(row) for row in rows]
            analyses: dict[str, dict[str, Any]] = {}
            job_ids = [str(job["job_id"]) for job in jobs]
            for offset in range(0, len(job_ids), 500):
                batch = job_ids[offset : offset + 500]
                placeholders = ",".join("?" for _ in batch)
                for row in connection.execute(
                    f"""
                    SELECT ja.job_id, ja.analysis_status,
                           jfs.category, jfs.field_status
                    FROM job_analysis AS ja
                    LEFT JOIN job_field_status AS jfs
                        ON jfs.analysis_id = ja.analysis_id
                    WHERE ja.is_current = 1 AND ja.job_id IN ({placeholders})
                    """,
                    batch,
                ).fetchall():
                    job_id = str(row["job_id"])
                    analysis = analyses.setdefault(
                        job_id,
                        {"status": row["analysis_status"], "field_statuses": {}},
                    )
                    if row["category"] is not None and row["field_status"] is not None:
                        analysis["field_statuses"][str(row["category"])] = str(
                            row["field_status"]
                        )

        for job in jobs:
            analysis = analyses.get(str(job["job_id"]))
            job["review_reasons"] = job_review_reasons(
                job,
                analysis["status"] if analysis else None,
                analysis["field_statuses"] if analysis else {},
            )
        return jobs

    def get_current_analysis(self, job_id: str) -> dict[str, Any] | None:
        """Devuelve el analisis vigente y sus requisitos/estados."""
        with self.database.connection() as connection:
            analysis_row = connection.execute(
                """
                SELECT * FROM job_analysis
                WHERE job_id = ? AND is_current = 1
                """,
                (job_id,),
            ).fetchone()
            if analysis_row is None:
                return None
            analysis = dict(analysis_row)
            analysis["requirements"] = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT * FROM job_requirements
                    WHERE analysis_id = ?
                    ORDER BY category, requirement_id
                    """,
                    (analysis["analysis_id"],),
                ).fetchall()
            ]
            analysis["field_statuses"] = {
                row["category"]: row["field_status"]
                for row in connection.execute(
                    """
                    SELECT category, field_status FROM job_field_status
                    WHERE analysis_id = ?
                    ORDER BY category
                    """,
                    (analysis["analysis_id"],),
                ).fetchall()
            }
        return analysis

    def list_candidate_profile_facts(self) -> list[dict[str, Any]]:
        """Devuelve los datos estructurados registrados manualmente del perfil."""
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT f.*, i.profile_import_id,
                       i.source_name AS import_source_name,
                       i.imported_at AS import_timestamp
                FROM candidate_profile_facts AS f
                LEFT JOIN candidate_profile_imports AS i
                    ON i.profile_import_id = f.source_import_id
                ORDER BY f.category, f.value_text COLLATE NOCASE, f.value_code
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def save_candidate_profile_fact(
        self,
        category: str,
        value_code: str,
        value_text: str,
        *,
        qualifier: str | None = None,
        education_status: str | None = None,
        numeric_value: float | None = None,
        unit: str | None = None,
        notes: str | None = None,
    ) -> None:
        """Crea o actualiza un dato de perfil identificado por categoria y codigo."""
        self.save_candidate_profile_facts(
            [
                {
                    "category": category,
                    "value_code": value_code,
                    "value_text": value_text,
                    "qualifier": qualifier,
                    "education_status": education_status,
                    "numeric_value": numeric_value,
                    "unit": unit,
                    "notes": notes,
                }
            ]
        )

    def save_candidate_profile_facts(
        self, facts: Sequence[Mapping[str, Any]]
    ) -> None:
        """Guarda un lote de hechos de perfil atomicamente."""
        if not isinstance(facts, Sequence) or isinstance(facts, (str, bytes)):
            raise ValueError("facts debe ser una lista de datos de perfil.")
        if not facts:
            raise ValueError("Selecciona al menos un dato de perfil.")
        normalized_facts = [
            self._validate_candidate_profile_fact(fact) for fact in facts
        ]
        with self.database.connection() as connection:
            connection.executemany(
                """
                INSERT INTO candidate_profile_facts (
                    category, value_code, value_text, qualifier,
                    education_status, numeric_value, unit, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(category, value_code) DO UPDATE SET
                    value_text = excluded.value_text,
                    qualifier = excluded.qualifier,
                    education_status = excluded.education_status,
                    numeric_value = excluded.numeric_value,
                    unit = excluded.unit,
                    notes = excluded.notes,
                    origin = 'manual',
                    source_import_id = NULL,
                    updated_at = CURRENT_TIMESTAMP
                """,
                normalized_facts,
            )

    def import_candidate_profile_facts(
        self,
        source_name: str,
        facts: Sequence[Mapping[str, Any]],
    ) -> int:
        """Guarda un lote importado y sus snapshots para poder revertirlo."""
        source_name = self._required_text(source_name, "source_name")
        if not isinstance(facts, Sequence) or isinstance(facts, (str, bytes)):
            raise ValueError("facts debe ser una lista de datos de perfil.")
        if not facts:
            raise ValueError("Selecciona al menos un dato de perfil.")
        normalized = [self._validate_candidate_profile_fact(fact) for fact in facts]
        keys = [(fact[0], fact[1].casefold()) for fact in normalized]
        if len(keys) != len(set(keys)):
            raise ValueError(
                "El lote contiene codigos de perfil duplicados por categoria."
            )

        with self.database.connection() as connection:
            cursor = connection.execute(
                "INSERT INTO candidate_profile_imports (source_name) VALUES (?)",
                (source_name,),
            )
            import_id = int(cursor.lastrowid)
            for fact in normalized:
                category, value_code, *_ = fact
                previous = connection.execute(
                    """
                    SELECT value_text, qualifier, education_status,
                           numeric_value, unit, notes, origin, source_import_id
                    FROM candidate_profile_facts
                    WHERE category = ? AND value_code = ? COLLATE NOCASE
                    """,
                    (category, value_code),
                ).fetchone()
                connection.execute(
                    """
                    INSERT INTO candidate_profile_import_snapshots (
                        profile_import_id, category, value_code, was_present,
                        value_text, qualifier, education_status, numeric_value,
                        unit, notes, previous_origin, previous_source_import_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        import_id,
                        category,
                        value_code,
                        1 if previous else 0,
                        previous["value_text"] if previous else None,
                        previous["qualifier"] if previous else None,
                        previous["education_status"] if previous else None,
                        previous["numeric_value"] if previous else None,
                        previous["unit"] if previous else None,
                        previous["notes"] if previous else None,
                        previous["origin"] if previous else None,
                        previous["source_import_id"] if previous else None,
                    ),
                )
            connection.executemany(
                """
                INSERT INTO candidate_profile_facts (
                    category, value_code, value_text, qualifier,
                    education_status, numeric_value, unit, notes,
                    origin, source_import_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'imported', ?)
                ON CONFLICT(category, value_code) DO UPDATE SET
                    value_text = excluded.value_text,
                    qualifier = excluded.qualifier,
                    education_status = excluded.education_status,
                    numeric_value = excluded.numeric_value,
                    unit = excluded.unit,
                    notes = excluded.notes,
                    origin = 'imported',
                    source_import_id = excluded.source_import_id,
                    updated_at = CURRENT_TIMESTAMP
                """,
                [(*fact, import_id) for fact in normalized],
            )
        return import_id

    def list_candidate_profile_imports(
        self, *, limit: int = 20
    ) -> list[dict[str, Any]]:
        """Lista importaciones locales recientes sin incluir el texto del CV."""
        limit = self._positive_int(limit, "limit")
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT i.profile_import_id, i.source_name, i.imported_at,
                       COUNT(f.profile_fact_id) AS active_fact_count
                FROM candidate_profile_imports AS i
                LEFT JOIN candidate_profile_facts AS f
                    ON f.source_import_id = i.profile_import_id
                GROUP BY i.profile_import_id
                ORDER BY i.profile_import_id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def revert_last_candidate_profile_import(self) -> dict[str, Any] | None:
        """Revierte solo la importacion mas reciente y preserva ediciones manuales."""
        with self.database.connection() as connection:
            import_row = connection.execute(
                """
                SELECT profile_import_id, source_name, imported_at
                FROM candidate_profile_imports
                ORDER BY profile_import_id DESC
                LIMIT 1
                """
            ).fetchone()
            if import_row is None:
                return None
            import_id = int(import_row["profile_import_id"])
            snapshots = connection.execute(
                """
                SELECT * FROM candidate_profile_import_snapshots
                WHERE profile_import_id = ?
                ORDER BY category, value_code
                """,
                (import_id,),
            ).fetchall()
            restored = 0
            removed = 0
            preserved_manual = 0
            for snapshot in snapshots:
                current = connection.execute(
                    """
                    SELECT origin, source_import_id
                    FROM candidate_profile_facts
                    WHERE category = ? AND value_code = ? COLLATE NOCASE
                    """,
                    (snapshot["category"], snapshot["value_code"]),
                ).fetchone()
                if current is None or current["source_import_id"] != import_id:
                    if current is not None and current["origin"] == "manual":
                        preserved_manual += 1
                    continue
                if snapshot["was_present"]:
                    connection.execute(
                        """
                        UPDATE candidate_profile_facts
                        SET value_text = ?, qualifier = ?, education_status = ?,
                            numeric_value = ?, unit = ?, notes = ?, origin = ?,
                            source_import_id = ?, updated_at = CURRENT_TIMESTAMP
                        WHERE category = ? AND value_code = ? COLLATE NOCASE
                        """,
                        (
                            snapshot["value_text"],
                            snapshot["qualifier"],
                            snapshot["education_status"],
                            snapshot["numeric_value"],
                            snapshot["unit"],
                            snapshot["notes"],
                            snapshot["previous_origin"],
                            snapshot["previous_source_import_id"],
                            snapshot["category"],
                            snapshot["value_code"],
                        ),
                    )
                    restored += 1
                else:
                    connection.execute(
                        """
                        DELETE FROM candidate_profile_facts
                        WHERE category = ? AND value_code = ? COLLATE NOCASE
                          AND source_import_id = ?
                        """,
                        (
                            snapshot["category"],
                            snapshot["value_code"],
                            import_id,
                        ),
                    )
                    removed += 1
            connection.execute(
                "DELETE FROM candidate_profile_imports WHERE profile_import_id = ?",
                (import_id,),
            )
        return {
            "profile_import_id": import_id,
            "source_name": import_row["source_name"],
            "restored": restored,
            "removed": removed,
            "preserved_manual": preserved_manual,
        }

    @classmethod
    def _validate_candidate_profile_fact(
        cls, fact: Mapping[str, Any]
    ) -> tuple[str, str, str, str | None, str | None, float | None, str | None, str | None]:
        if not isinstance(fact, Mapping):
            raise ValueError("Cada dato de perfil debe ser un objeto.")
        allowed = {
            "category",
            "value_code",
            "value_text",
            "qualifier",
            "education_status",
            "numeric_value",
            "unit",
            "notes",
        }
        unexpected = set(fact) - allowed
        if unexpected:
            raise ValueError(
                f"Campos de perfil no soportados: {', '.join(map(str, unexpected))}."
            )
        category = cls._validate_category(fact.get("category"))
        value_code = cls._required_text(fact.get("value_code"), "value_code")
        value_text = cls._required_text(fact.get("value_text"), "value_text")
        qualifier = cls._optional_text(fact.get("qualifier"))
        unit = cls._optional_text(fact.get("unit"))
        notes = cls._optional_text(fact.get("notes"))
        education_status = fact.get("education_status")
        if education_status not in {None, "completed", "pursuing", "unspecified"}:
            raise ValueError("education_status no es valido.")
        if education_status is not None and category != "education":
            raise ValueError("education_status solo aplica a education.")
        numeric_value = cls._optional_number(fact.get("numeric_value"), "numeric_value")
        if numeric_value is not None and numeric_value < 0:
            raise ValueError("numeric_value no puede ser negativo.")
        if numeric_value is not None and category != "experience":
            raise ValueError("numeric_value solo aplica a experience.")
        return (
            category,
            value_code,
            value_text,
            qualifier,
            education_status,
            numeric_value,
            unit,
            notes,
        )

    def delete_candidate_profile_fact(self, profile_fact_id: int) -> bool:
        """Elimina un dato de perfil y devuelve si existia."""
        if isinstance(profile_fact_id, bool) or not isinstance(profile_fact_id, int):
            raise ValueError("profile_fact_id debe ser un entero.")
        with self.database.connection() as connection:
            cursor = connection.execute(
                "DELETE FROM candidate_profile_facts WHERE profile_fact_id = ?",
                (profile_fact_id,),
            )
        return cursor.rowcount > 0

    def compare_job_to_candidate_profile(self, job_id: str) -> dict[str, Any]:
        """Compara requisitos vigentes con datos exactos del perfil, sin inferir."""
        self._required_text(job_id, "job_id")
        job = self.get_job(job_id)
        if job is None:
            raise ValueError(f"La vacante {job_id!r} no existe.")
        analysis = self.get_current_analysis(job_id)
        facts = self.list_candidate_profile_facts()
        return self._compare_candidate_profile(job_id, job.get("title"), analysis, facts)

    def compare_jobs_to_candidate_profile(
        self, jobs: Sequence[Mapping[str, Any]]
    ) -> dict[str, dict[str, Any]]:
        """Compara en lote los resultados visibles con los datos del perfil."""
        if not isinstance(jobs, Sequence) or isinstance(jobs, (str, bytes)):
            raise ValueError("jobs debe ser una lista de vacantes.")
        normalized_jobs: dict[str, str] = {}
        for job in jobs:
            if not isinstance(job, Mapping):
                raise ValueError("Cada vacante debe ser un objeto.")
            job_id = self._required_text(job.get("job_id"), "job_id")
            normalized_jobs[job_id] = str(job.get("title") or "Vacante sin titulo")
        if not normalized_jobs:
            return {}

        placeholders = ",".join("?" for _ in normalized_jobs)
        job_ids = list(normalized_jobs)
        analyses: dict[str, dict[str, Any]] = {}
        with self.database.connection() as connection:
            analysis_rows = connection.execute(
                f"""
                SELECT ja.* FROM job_analysis AS ja
                WHERE ja.is_current = 1 AND ja.job_id IN ({placeholders})
                """,
                job_ids,
            ).fetchall()
            analysis_ids = {}
            jobs_by_analysis_id: dict[int, str] = {}
            for row in analysis_rows:
                analysis = dict(row)
                job_id = str(analysis["job_id"])
                analysis_id = int(analysis["analysis_id"])
                analysis_ids[job_id] = analysis_id
                jobs_by_analysis_id[analysis_id] = job_id
                analysis["requirements"] = []
                analysis["field_statuses"] = {}
                analyses[job_id] = analysis

            if analysis_ids:
                analysis_placeholders = ",".join(
                    "?" for _ in analysis_ids
                )
                ids = list(analysis_ids.values())
                for row in connection.execute(
                    f"""
                    SELECT * FROM job_requirements
                    WHERE analysis_id IN ({analysis_placeholders})
                    ORDER BY category, requirement_id
                    """,
                    ids,
                ).fetchall():
                    requirement = dict(row)
                    job_id = jobs_by_analysis_id[int(requirement["analysis_id"])]
                    analyses[job_id]["requirements"].append(requirement)

                for row in connection.execute(
                    f"""
                    SELECT analysis_id, category, field_status
                    FROM job_field_status
                    WHERE analysis_id IN ({analysis_placeholders})
                    ORDER BY category
                    """,
                    ids,
                ).fetchall():
                    job_id = jobs_by_analysis_id[int(row["analysis_id"])]
                    analyses[job_id]["field_statuses"][row["category"]] = row[
                        "field_status"
                    ]
            facts = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT * FROM candidate_profile_facts
                    ORDER BY category, value_text COLLATE NOCASE, value_code
                    """
                ).fetchall()
            ]

        return {
            job_id: self._compare_candidate_profile(
                job_id, title, analyses.get(job_id), facts
            )
            for job_id, title in normalized_jobs.items()
        }

    def _compare_candidate_profile(
        self,
        job_id: str,
        job_title: Any,
        analysis: Mapping[str, Any] | None,
        facts: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "job_id": job_id,
            "job_title": job_title or "Vacante sin titulo",
            "analysis_status": (
                "not_analyzed" if analysis is None else analysis["analysis_status"]
            ),
            "requirements": [],
            "counts": {
                "matched": 0,
                "not_registered": 0,
                "below_requirement": 0,
                "needs_review": 0,
            },
        }
        fact_index = {
            (
                str(fact["category"]),
                self._profile_match_key(str(fact["value_code"])),
            ): fact
            for fact in facts
        }
        text_index = {
            (
                str(fact["category"]),
                self._profile_match_key(str(fact["value_text"])),
            ): fact
            for fact in facts
        }
        if analysis is None:
            return result
        for requirement in analysis["requirements"]:
            category = str(requirement["category"])
            code = requirement.get("value_code")
            match_key = self._profile_match_key(str(code)) if code else ""
            fact = (
                fact_index.get((category, match_key))
                if match_key
                else text_index.get(
                    (
                        category,
                        self._profile_match_key(str(requirement["value_text"])),
                    )
                )
            )
            status = self._compare_requirement(requirement, fact)
            result["counts"][status] += 1
            result["requirements"].append(
                {
                    **requirement,
                    "comparison_status": status,
                    "profile_fact": fact,
                }
            )
        return result

    @staticmethod
    def candidate_profile_status(comparison: Mapping[str, Any]) -> str:
        """Resume el resultado para listas sin ocultar analisis incompletos."""
        if comparison.get("analysis_status") == "not_analyzed":
            return "not_analyzed"
        required = [
            requirement
            for requirement in comparison.get("requirements", [])
            if requirement.get("requirement_level") == "required"
        ]
        all_requirements = comparison.get("requirements", [])
        if not required:
            if any(
                requirement.get("comparison_status") == "needs_review"
                for requirement in all_requirements
            ):
                return "needs_review"
            return "no_required"
        statuses = {
            requirement.get("comparison_status") for requirement in required
        }
        if "below_requirement" in statuses:
            return "below_requirement"
        if (
            "needs_review" in statuses
            or any(
                requirement.get("comparison_status") == "needs_review"
                for requirement in all_requirements
            )
            or comparison.get("analysis_status") != "complete"
        ):
            return "needs_review"
        if "not_registered" in statuses:
            return "not_registered"
        return "matched"

    @staticmethod
    def _profile_match_key(value: str) -> str:
        return " ".join(value.casefold().split())

    @classmethod
    def _compare_requirement(
        cls,
        requirement: Mapping[str, Any],
        fact: Mapping[str, Any] | None,
    ) -> str:
        if requirement["requirement_level"] in {"inferred", "ambiguous"}:
            return "needs_review"
        if fact is None:
            return "not_registered"
        qualifier = requirement.get("qualifier")
        if qualifier and cls._profile_match_key(str(qualifier)) != cls._profile_match_key(
            str(fact.get("qualifier") or "")
        ):
            return "needs_review"
        education_status = requirement.get("education_status")
        if education_status == "completed" and fact.get("education_status") != "completed":
            return "needs_review"
        minimum = requirement.get("minimum")
        if minimum is not None:
            profile_value = fact.get("numeric_value")
            profile_unit = cls._profile_match_key(str(fact.get("unit") or ""))
            requirement_unit = cls._profile_match_key(str(requirement.get("unit") or ""))
            if profile_value is None or profile_unit != requirement_unit:
                return "needs_review"
            if profile_value < minimum:
                return "below_requirement"
        return "matched"

    @staticmethod
    def _validate_category(category: Any) -> str:
        if not isinstance(category, str) or category not in REQUIREMENT_CATEGORIES:
            raise ValueError(f"Categoria de requisito no valida: {category!r}.")
        return category

    @classmethod
    def _validate_requirement(cls, requirement: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(requirement, Mapping):
            raise ValueError("Cada requisito debe ser un objeto.")
        allowed = {
            "category",
            "value_code",
            "value_text",
            "qualifier",
            "requirement_level",
            "education_status",
            "minimum",
            "maximum",
            "unit",
            "evidence",
            "origin",
            "confidence",
        }
        unexpected = set(requirement) - allowed
        if unexpected:
            raise ValueError(
                f"Campos de requisito no soportados: {', '.join(map(str, unexpected))}."
            )

        category = cls._validate_category(requirement.get("category"))
        value_text = cls._required_text(requirement.get("value_text"), "value_text")
        evidence = cls._required_text(requirement.get("evidence"), "evidence")
        requirement_level = requirement.get("requirement_level")
        if requirement_level not in REQUIREMENT_LEVELS:
            raise ValueError("requirement_level no es valido.")
        origin = requirement.get("origin")
        if origin not in REQUIREMENT_ORIGINS:
            raise ValueError("origin no es valido.")

        education_status = requirement.get("education_status")
        if education_status not in {None, "completed", "pursuing", "unspecified"}:
            raise ValueError("education_status no es valido.")
        if education_status is not None and category != "education":
            raise ValueError("education_status solo aplica a education.")

        minimum = cls._optional_number(requirement.get("minimum"), "minimum")
        maximum = cls._optional_number(requirement.get("maximum"), "maximum")
        if minimum is not None and minimum < 0:
            raise ValueError("minimum no puede ser negativo.")
        if maximum is not None and maximum < 0:
            raise ValueError("maximum no puede ser negativo.")
        if minimum is not None and maximum is not None and minimum > maximum:
            raise ValueError("minimum no puede superar maximum.")
        confidence = cls._optional_number(requirement.get("confidence"), "confidence")
        if confidence is not None and not 0 <= confidence <= 1:
            raise ValueError("confidence debe estar entre 0 y 1.")

        return {
            "category": category,
            "value_code": cls._optional_text(requirement.get("value_code")),
            "value_text": value_text,
            "qualifier": cls._optional_text(requirement.get("qualifier")),
            "requirement_level": requirement_level,
            "education_status": education_status,
            "minimum": minimum,
            "maximum": maximum,
            "unit": cls._optional_text(requirement.get("unit")),
            "evidence": evidence,
            "origin": origin,
            "confidence": confidence,
        }

    @classmethod
    def _validate_requirement_filter(
        cls, requirement: Mapping[str, Any]
    ) -> dict[str, Any]:
        if not isinstance(requirement, Mapping):
            raise ValueError("Cada filtro de requisito debe ser un objeto.")
        allowed = {"category", "value_code", "qualifier", "education_status", "minimum"}
        unexpected = set(requirement) - allowed
        if unexpected:
            raise ValueError(
                f"Campos de filtro no soportados: {', '.join(map(str, unexpected))}."
            )
        category = cls._validate_category(requirement.get("category"))
        value_code = requirement.get("value_code")
        qualifier = requirement.get("qualifier")
        if value_code is not None:
            value_code = cls._required_text(value_code, "value_code")
        if qualifier is not None:
            qualifier = cls._required_text(qualifier, "qualifier")
        education_status = requirement.get("education_status")
        if education_status not in {None, "completed", "pursuing", "unspecified"}:
            raise ValueError("education_status no es valido.")
        if education_status is not None and category != "education":
            raise ValueError("education_status solo aplica a education.")
        minimum = cls._optional_number(requirement.get("minimum"), "minimum")
        if minimum is not None and minimum < 0:
            raise ValueError("minimum no puede ser negativo.")
        return {
            "category": category,
            "value_code": value_code,
            "qualifier": qualifier,
            "education_status": education_status,
            "minimum": minimum,
        }

    @staticmethod
    def _required_text(value: Any, field: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field} es obligatorio.")
        return value.strip()

    @staticmethod
    def _optional_text(value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @staticmethod
    def _optional_number(value: Any, field: str) -> float | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{field} debe ser numerico.")
        number = float(value)
        if not math.isfinite(number):
            raise ValueError(f"{field} debe ser finito.")
        return number

    @staticmethod
    def _nonnegative_int(value: Any, field: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{field} debe ser un entero mayor o igual a cero.")
        return value

    @classmethod
    def _positive_int(cls, value: Any, field: str) -> int:
        parsed = cls._nonnegative_int(value, field)
        if parsed == 0:
            raise ValueError(f"{field} debe ser mayor que cero.")
        return parsed

    @classmethod
    def _validate_optional_date(cls, value: str | None, field: str) -> str | None:
        text = cls._optional_text(value)
        if text is None:
            return None
        try:
            parsed = date.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(f"{field} debe usar el formato AAAA-MM-DD.") from exc
        if parsed.isoformat() != text:
            raise ValueError(f"{field} debe usar el formato AAAA-MM-DD.")
        return text

    @staticmethod
    def _validate_language(language: str | None) -> None:
        if language is not None and language not in {"es", "en", "mixed", "undetermined"}:
            raise ValueError("default_language no es un valor permitido.")

    @staticmethod
    def _validate_url(url: str | None, *, optional: bool = False) -> None:
        if url is None and optional:
            return
        if not isinstance(url, str):
            raise ValueError("base_url debe ser una URL HTTP o HTTPS.")
        parsed = urlparse(url.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("base_url debe ser una URL HTTP o HTTPS.")

    @staticmethod
    def _validate_groups(groups: Sequence[str]) -> None:
        if isinstance(groups, (str, bytes)):
            raise ValueError("run_groups debe ser una lista de nombres.")
        if any(not isinstance(group, str) or not group.strip() for group in groups):
            raise ValueError("run_groups contiene un valor vacio o no textual.")

    @staticmethod
    def _json_object(
        value: Mapping[str, Any] | None,
        field: str,
        *,
        empty_object: bool = False,
    ) -> str | None:
        if value is None:
            return "{}" if empty_object else None
        if not isinstance(value, Mapping):
            raise ValueError(f"{field} debe ser un objeto JSON.")
        try:
            return json.dumps(dict(value), ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field} no se puede serializar como JSON.") from exc

    @classmethod
    def _reject_credentials(cls, value: Any, path: str = "site_config") -> None:
        forbidden = {"password", "passwd", "secret", "cookie", "authorization"}
        if isinstance(value, Mapping):
            for key, item in value.items():
                normalized = str(key).strip().lower().replace("-", "_")
                if normalized in forbidden:
                    raise ValueError(
                        f"{path} no puede almacenar credenciales ({key!r})."
                    )
                cls._reject_credentials(item, f"{path}.{key}")
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            for index, item in enumerate(value):
                cls._reject_credentials(item, f"{path}[{index}]")
