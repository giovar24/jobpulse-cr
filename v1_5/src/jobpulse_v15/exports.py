"""Exportacion controlada de resultados de busqueda."""

import csv
from collections.abc import Mapping, Sequence
from typing import Any, TextIO


PROFILE_COMPATIBILITY_LABELS = {
    "matched": "Coincide",
    "not_registered": "Faltan datos",
    "below_requirement": "Brecha conocida",
    "needs_review": "Revisar",
    "not_analyzed": "Sin analisis",
    "no_required": "Sin req. obligatorios",
}


def _safe_csv_cell(value: Any) -> str:
    text = "" if value is None else str(value)
    if text.lstrip(" \t\r\n").startswith(("=", "+", "-", "@")) or text.startswith(
        ("\t", "\r", "\n")
    ):
        return "'" + text
    return text


def write_job_search_csv(output: TextIO, jobs: Sequence[Mapping[str, Any]]) -> None:
    """Escribe campos utiles de busqueda, sin texto extenso ni payloads."""
    writer = csv.writer(output)
    writer.writerow(
        (
            "job_id",
            "titulo",
            "empresa",
            "pais",
            "region",
            "ciudad",
            "modalidad",
            "idioma_anuncio",
            "fecha_publicacion",
            "salario_minimo",
            "salario_maximo",
            "moneda_salario",
            "periodo_salario",
            "compatibilidad_perfil",
            "motivos_revision",
            "url_fuente",
        )
    )
    for job in jobs:
        writer.writerow(
            tuple(
                _safe_csv_cell(value)
                for value in (
                    job.get("job_id", ""),
                    job.get("title", ""),
                    job.get("company", ""),
                    job.get("country", ""),
                    job.get("region", ""),
                    job.get("city", ""),
                    job.get("work_mode", ""),
                    job.get("posting_language", ""),
                    job.get("date_published", ""),
                    job.get("salary_min", ""),
                    job.get("salary_max", ""),
                    job.get("salary_currency", ""),
                    job.get("salary_period", ""),
                    job.get("profile_compatibility", ""),
                    ";".join(
                        str(reason) for reason in job.get("review_reasons", [])
                    ),
                    job.get("source_url", ""),
                )
            )
        )
