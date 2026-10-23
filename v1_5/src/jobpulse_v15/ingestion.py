"""Validacion e ingesta atomica por oferta para la base v1.5."""

import hashlib
import json
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from jobpulse_v15.repository import Repository


class IngestionError(RuntimeError):
    """La corrida de ingesta fallo despues de iniciar su registro."""

    def __init__(self, run_id: int, message: str) -> None:
        super().__init__(message)
        self.run_id = run_id


class IngestionService:
    """Importa ofertas ya obtenidas sin realizar solicitudes de red."""

    def __init__(self, repository: Repository) -> None:
        self.repository = repository

    def import_offers(
        self,
        site_id: str,
        offers: Sequence[Mapping[str, Any]],
    ) -> dict[str, int | str]:
        """Valida todas las ofertas y registra una corrida para un solo sitio."""
        normalized = self._validate_offers(site_id, offers)
        run_id = self.repository.start_site_run(site_id, "json_import")
        return self._persist_in_run(site_id, normalized, run_id)

    def fetch_and_import(
        self,
        site_id: str,
        fetch_offers: Callable[[], Sequence[Mapping[str, Any]]],
        *,
        run_kind: str,
    ) -> dict[str, int | str]:
        """Registra la corrida antes de fetch para no perder errores de proveedor."""
        run_id = self.repository.start_site_run(site_id, run_kind)
        try:
            offers = fetch_offers()
            skipped = getattr(offers, "jobs_skipped", 0)
            if isinstance(skipped, bool) or not isinstance(skipped, int) or skipped < 0:
                raise ValueError("jobs_skipped debe ser un entero no negativo.")
            normalized = self._validate_offers(site_id, offers)
        except Exception as exc:
            self.repository.record_site_result(
                run_id,
                site_id,
                jobs_found=0,
                jobs_valid=0,
                jobs_new=0,
                jobs_duplicate=0,
                error_message=f"{type(exc).__name__}: {exc}",
            )
            self.repository.finish_run(run_id, "Provider fetch failed.")
            raise IngestionError(
                run_id,
                f"La obtencion fallo en la corrida {run_id}: {exc}",
            ) from exc
        return self._persist_in_run(
            site_id, normalized, run_id, jobs_skipped=skipped
        )

    def _persist_in_run(
        self,
        site_id: str,
        normalized: Sequence[Mapping[str, Any]],
        run_id: int,
        *,
        jobs_skipped: int = 0,
    ) -> dict[str, int | str]:
        created = 0
        duplicates = 0

        try:
            for offer in normalized:
                is_new_source_record = self.repository.save_job(
                    offer["job"],
                    site_id=site_id,
                    source_job_id=offer["source_job_id"],
                    source_url=offer["source_url"],
                    raw_payload=offer["raw_payload"],
                    source_updated_at=offer["source_updated_at"],
                    content_hash=offer["content_hash"],
                )
                if is_new_source_record:
                    created += 1
                else:
                    duplicates += 1
        except (sqlite3.Error, ValueError) as exc:
            self.repository.record_site_result(
                run_id,
                site_id,
                jobs_found=len(normalized) + jobs_skipped,
                jobs_valid=created + duplicates,
                jobs_new=created,
                jobs_duplicate=duplicates,
                error_message=str(exc),
            )
            self.repository.finish_run(run_id, "Import failed; see site result.")
            raise IngestionError(
                run_id,
                f"La ingesta fallo en la corrida {run_id}: {exc}",
            ) from exc

        site_status = self.repository.record_site_result(
            run_id,
            site_id,
            jobs_found=len(normalized) + jobs_skipped,
            jobs_valid=len(normalized),
            jobs_new=created,
            jobs_duplicate=duplicates,
        )
        run_status = self.repository.finish_run(run_id)
        return {
            "run_id": run_id,
            "status": run_status,
            "site_status": site_status,
            "jobs_found": len(normalized) + jobs_skipped,
            "jobs_valid": len(normalized),
            "jobs_new": created,
            "jobs_duplicate": duplicates,
            "jobs_skipped": jobs_skipped,
        }

    @classmethod
    def _validate_offers(
        cls, site_id: str, offers: Sequence[Mapping[str, Any]]
    ) -> list[dict[str, Any]]:
        if not isinstance(offers, Sequence) or isinstance(offers, (str, bytes)):
            raise ValueError("offers debe ser una lista.")
        normalized = []
        seen_source_ids = set()
        for index, offer in enumerate(offers):
            if not isinstance(offer, Mapping):
                raise ValueError(f"La oferta {index} debe ser un objeto.")
            source_job_id = cls._required_text(
                offer.get("source_job_id"), f"offers[{index}].source_job_id"
            )
            if source_job_id in seen_source_ids:
                raise ValueError(
                    f"source_job_id duplicado en el archivo: {source_job_id!r}."
                )
            seen_source_ids.add(source_job_id)

            job_data = offer.get("job")
            if not isinstance(job_data, Mapping):
                raise ValueError(f"offers[{index}].job debe ser un objeto.")
            job = dict(job_data)
            job_id = job.get("job_id")
            if job_id is None or not str(job_id).strip():
                job["job_id"] = f"{site_id}:{source_job_id}"
            if (
                "title" not in job
                or not isinstance(job["title"], str)
                or not job["title"].strip()
            ):
                raise ValueError(f"offers[{index}].job.title es obligatorio.")

            source_url = offer.get("source_url")
            if source_url is not None and not isinstance(source_url, str):
                raise ValueError(f"offers[{index}].source_url debe ser texto.")
            raw_payload = offer.get("raw_payload")
            if raw_payload is not None and not isinstance(raw_payload, Mapping):
                raise ValueError(f"offers[{index}].raw_payload debe ser un objeto.")
            source_updated_at = offer.get("source_updated_at")
            if source_updated_at is not None and not isinstance(
                source_updated_at, str
            ):
                raise ValueError(
                    f"offers[{index}].source_updated_at debe ser texto."
                )
            try:
                serialized = json.dumps(
                    dict(offer),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"La oferta {index} no se puede serializar como JSON."
                ) from exc

            normalized.append(
                {
                    "source_job_id": source_job_id,
                    "job": job,
                    "source_url": source_url,
                    "raw_payload": raw_payload,
                    "source_updated_at": source_updated_at,
                    "content_hash": hashlib.sha256(
                        serialized.encode("utf-8")
                    ).hexdigest(),
                }
            )
        return normalized

    @staticmethod
    def _required_text(value: Any, field: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field} es obligatorio.")
        return value.strip()
