"""Comandos de mantenimiento para JobPulse CR 1.5."""

import argparse
import hashlib
import io
import json
import sqlite3
from pathlib import Path
from typing import Any

from jobpulse_v15.analysis import EXTRACTOR_VERSION, RuleBasedExtractor
from jobpulse_v15.ane import AneFetcher
from jobpulse_v15.ashby import AshbyFetcher
from jobpulse_v15.cinde import CindeFetcher
from jobpulse_v15.db import Database
from jobpulse_v15.lever import LeverFetcher
from jobpulse_v15.linkedin import LinkedInFetcher
from jobpulse_v15.indeed import IndeedFetcher
from jobpulse_v15.procomer import ProcomerFetcher
from jobpulse_v15.recruitee import RecruiteeFetcher
from jobpulse_v15.successfactors import SuccessFactorsFetcher
from jobpulse_v15.workable import WorkableFetcher
from jobpulse_v15.exports import PROFILE_COMPATIBILITY_LABELS, write_job_search_csv
from jobpulse_v15.greenhouse import GreenhouseFetcher
from jobpulse_v15.ingestion import IngestionError, IngestionService
from jobpulse_v15.repository import (
    JOB_REVIEW_FIELD_PREFIXES,
    JOB_REVIEW_REASON_CODES,
    REQUIREMENT_CATEGORIES,
    Repository,
)
from jobpulse_v15.workday import MAX_PAGES, WorkdayFetcher


def _parse_requirement(value: str) -> dict[str, str]:
    category, separator, value_code = value.partition("=")
    if not separator or not category.strip() or not value_code.strip():
        raise argparse.ArgumentTypeError(
            "el requisito debe usar el formato categoria=codigo, por ejemplo language=en"
        )
    return {"category": category.strip(), "value_code": value_code.strip()}


def _extract_job_analysis(repository: Repository, job_id: str) -> dict[str, Any]:
    job = repository.get_job(job_id)
    if job is None:
        raise ValueError(f"No existe la vacante {job_id!r}.")
    text = "\n\n".join(
        value.strip()
        for value in (job.get("description_raw"), job.get("requirements_raw"))
        if isinstance(value, str) and value.strip()
    )
    if not text:
        raise ValueError(
            f"La vacante {job_id!r} no tiene descripcion ni requisitos para analizar."
        )
    result = RuleBasedExtractor().extract(text)
    source_context = repository.get_job_source_context(job_id)
    source_site_id = (
        source_context["site_id"] if source_context is not None else None
    )
    source_config_hash = (
        hashlib.sha256(source_context["site_config_json"].encode("utf-8")).hexdigest()
        if source_context is not None
        else None
    )
    analysis_id = repository.save_analysis(
        job_id,
        text_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        extractor_version=EXTRACTOR_VERSION,
        analysis_status=result.analysis_status,
        field_statuses=result.field_statuses,
        requirements=result.requirements,
        source_site_id=source_site_id,
        source_config_hash=source_config_hash,
    )
    return {
        "analysis_id": analysis_id,
        "job_id": job_id,
        "analysis_status": result.analysis_status,
        "field_statuses": result.field_statuses,
        "requirements": result.requirements,
    }


def main() -> int:
    parser = argparse.ArgumentParser(prog="jobpulse-v15")
    parser.add_argument(
        "--db",
        help="Ruta a la base v1.5; por defecto usa v1_5/data/jobpulse_v15.db.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init-db", help="Crear la base y aplicar migraciones.")
    commands.add_parser("schema-version", help="Mostrar la version del esquema.")
    backup = commands.add_parser(
        "backup", help="Crear un respaldo consistente de la base v1.5."
    )
    backup.add_argument(
        "--output", required=True, type=Path, help="Ruta nueva para el archivo SQLite."
    )
    database_commands = commands.add_parser(
        "db", help="Verificar y mantener la base v1.5."
    )
    database_subcommands = database_commands.add_subparsers(
        dest="database_command", required=True
    )
    database_subcommands.add_parser(
        "check", help="Verificar integridad SQLite y claves foraneas."
    )
    platform = commands.add_parser("platform", help="Gestionar plataformas.")
    platform_commands = platform.add_subparsers(dest="platform_command", required=True)
    platform_add = platform_commands.add_parser("add", help="Registrar una plataforma.")
    platform_add.add_argument("--id", required=True)
    platform_add.add_argument("--name", required=True)
    platform_add.add_argument("--provider-type", required=True)
    platform_add.add_argument("--default-language", choices=("es", "en", "mixed"))

    site = commands.add_parser("site", help="Gestionar sitios y tableros.")
    site_commands = site.add_subparsers(dest="site_command", required=True)
    site_add = site_commands.add_parser("add", help="Registrar un sitio.")
    site_add.add_argument("--id", required=True)
    site_add.add_argument("--platform", required=True)
    site_add.add_argument("--name", required=True)
    site_add.add_argument("--url", required=True)
    site_add.add_argument("--company")
    site_add.add_argument("--country")
    site_add.add_argument("--group", action="append", default=[])
    site_add.add_argument("--disabled", action="store_true")

    site_list = site_commands.add_parser("list", help="Listar sitios.")
    site_list.add_argument("--enabled-only", action="store_true")
    site_list.add_argument("--group")

    site_state = site_commands.add_parser(
        "set-enabled", help="Activar o pausar un sitio."
    )
    site_state.add_argument("--id", required=True)
    state = site_state.add_mutually_exclusive_group(required=True)
    state.add_argument("--enabled", action="store_true")
    state.add_argument("--disabled", action="store_true")
    site_commands.add_parser(
        "mark-reviewed", help="Limpiar la bandera de revision manual."
    ).add_argument("--id", required=True)

    run = commands.add_parser("run", help="Registrar corridas y resultados.")
    run_commands = run.add_subparsers(dest="run_command", required=True)
    run_start = run_commands.add_parser("start", help="Iniciar corrida de sitios activos.")
    run_start.add_argument("--kind", default="full")
    run_start.add_argument("--group")

    run_result = run_commands.add_parser(
        "site-result", help="Registrar el resultado de un sitio."
    )
    run_result.add_argument("--id", type=int, required=True, dest="run_id")
    run_result.add_argument("--site", required=True)
    run_result.add_argument("--found", type=int, required=True)
    run_result.add_argument("--valid", type=int, required=True)
    run_result.add_argument("--new", type=int, required=True)
    run_result.add_argument("--duplicates", type=int, required=True)
    run_result.add_argument("--error")
    run_result.add_argument("--review-after-zero-runs", type=int, default=3)

    run_finish = run_commands.add_parser("finish", help="Cerrar una corrida.")
    run_finish.add_argument("--id", type=int, required=True, dest="run_id")
    run_finish.add_argument("--summary")

    run_show = run_commands.add_parser("show", help="Mostrar corrida y sitios.")
    run_show.add_argument("--id", type=int, required=True, dest="run_id")
    run_list = run_commands.add_parser("list", help="Listar corridas recientes.")
    run_list.add_argument("--limit", type=int, default=20)

    analysis = commands.add_parser(
        "analysis", help="Extraer y consultar analisis estructurados."
    )
    analysis_commands = analysis.add_subparsers(
        dest="analysis_command", required=True
    )
    analysis_extract = analysis_commands.add_parser(
        "extract", help="Extraer requisitos con reglas locales."
    )
    analysis_extract.add_argument("--job", required=True)
    analysis_pending = analysis_commands.add_parser(
        "extract-pending",
        help="Analizar localmente vacantes que aun no tienen analisis.",
    )
    analysis_pending.add_argument(
        "--limit",
        type=int,
        default=25,
        help="Maximo de vacantes por ejecucion (1 a 500; predeterminado 25).",
    )
    analysis_pending.add_argument(
        "--include-manually-skipped",
        action="store_true",
        help="Incluir vacantes excluidas de las busquedas normales.",
    )
    analysis_pending.add_argument(
        "--dry-run",
        action="store_true",
        help="Mostrar las vacantes pendientes sin analizarlas.",
    )

    quality = commands.add_parser(
        "quality", help="Consultar la calidad de datos de la base v1.5."
    )
    quality_commands = quality.add_subparsers(
        dest="quality_command", required=True
    )
    quality_commands.add_parser(
        "report", help="Mostrar un resumen global de datos para revision."
    )
    quality_duplicates = quality_commands.add_parser(
        "duplicates", help="Listar grupos candidatos a duplicados para revision."
    )
    quality_duplicates.add_argument(
        "--limit",
        type=int,
        default=100,
        help="Maximo de grupos a mostrar (1-500).",
    )

    ingest = commands.add_parser(
        "ingest", help="Importar datos ya recolectados a la DB v1.5."
    )
    ingest_commands = ingest.add_subparsers(dest="ingest_command", required=True)
    import_json = ingest_commands.add_parser(
        "import-json", help="Importar ofertas desde un archivo JSON local."
    )
    import_json.add_argument("--site", required=True)
    import_json.add_argument("--input", required=True, type=Path)

    scrape = commands.add_parser(
        "scrape", help="Consultar explícitamente un proveedor configurado."
    )
    scrape_commands = scrape.add_subparsers(dest="scrape_command", required=True)
    scrape_workday = scrape_commands.add_parser(
        "workday", help="Consultar uno o varios tableros Workday habilitados."
    )
    scrape_workday_target = scrape_workday.add_mutually_exclusive_group(required=True)
    scrape_workday_target.add_argument("--site")
    scrape_workday_target.add_argument(
        "--group",
        help="Consultar secuencialmente los tableros Workday activos del grupo.",
    )
    scrape_workday.add_argument(
        "--pages", type=int, choices=range(1, MAX_PAGES + 1), default=1
    )
    scrape_workday.add_argument(
        "--search-text",
        help="Texto enviado a Workday (por defecto Costa Rica o la config del sitio).",
    )
    scrape_greenhouse = scrape_commands.add_parser(
        "greenhouse", help="Consultar tableros Greenhouse con su API publica."
    )
    scrape_greenhouse_target = scrape_greenhouse.add_mutually_exclusive_group(
        required=True
    )
    scrape_greenhouse_target.add_argument("--site")
    scrape_greenhouse_target.add_argument(
        "--group",
        help="Consultar secuencialmente los tableros Greenhouse activos del grupo.",
    )
    scrape_ashby = scrape_commands.add_parser(
        "ashby", help="Consultar tableros Ashby con su API publica."
    )
    scrape_ashby_target = scrape_ashby.add_mutually_exclusive_group(required=True)
    scrape_ashby_target.add_argument("--site")
    scrape_ashby_target.add_argument(
        "--group",
        help="Consultar secuencialmente los tableros Ashby activos del grupo.",
    )
    scrape_lever = scrape_commands.add_parser(
        "lever", help="Consultar tableros Lever con su API publica."
    )
    scrape_lever_target = scrape_lever.add_mutually_exclusive_group(required=True)
    scrape_lever_target.add_argument("--site")
    scrape_lever_target.add_argument(
        "--group",
        help="Consultar secuencialmente los tableros Lever activos del grupo.",
    )
    scrape_recruitee = scrape_commands.add_parser(
        "recruitee", help="Consultar tableros Recruitee con su API publica."
    )
    scrape_recruitee_target = scrape_recruitee.add_mutually_exclusive_group(
        required=True
    )
    scrape_recruitee_target.add_argument("--site")
    scrape_recruitee_target.add_argument(
        "--group",
        help="Consultar secuencialmente los tableros Recruitee activos del grupo.",
    )
    scrape_workable = scrape_commands.add_parser(
        "workable", help="Consultar tableros Workable con su API publica."
    )
    scrape_workable_target = scrape_workable.add_mutually_exclusive_group(
        required=True
    )
    scrape_workable_target.add_argument("--site")
    scrape_workable_target.add_argument(
        "--group",
        help="Consultar secuencialmente los tableros Workable activos del grupo.",
    )
    scrape_successfactors = scrape_commands.add_parser(
        "successfactors", help="Consultar portales SuccessFactors via su feed RSS."
    )
    scrape_successfactors_target = scrape_successfactors.add_mutually_exclusive_group(
        required=True
    )
    scrape_successfactors_target.add_argument("--site")
    scrape_successfactors_target.add_argument(
        "--group",
        help="Consultar secuencialmente los portales SuccessFactors activos del grupo.",
    )

    # === ANE (browser, barrido por fecha) ===
    scrape_ane = scrape_commands.add_parser(
        "ane", help="Consultar ANE/Brete (barrido por fecha, requiere Brave CDP)."
    )
    scrape_ane_target = scrape_ane.add_mutually_exclusive_group(required=True)
    scrape_ane_target.add_argument("--site")
    scrape_ane_target.add_argument(
        "--group",
        help="Consultar secuencialmente los sitios ANE activos del grupo.",
    )
    scrape_ane.add_argument(
        "--pages", type=int, default=15, help="Páginas de listado a recorrer (default: 15)."
    )
    scrape_ane.add_argument(
        "--port", type=int, default=9222, help="Puerto CDP de Brave (default: 9222)."
    )
    scrape_ane.add_argument(
        "--dias", type=int, default=30, help="Ventana de días hacia atrás (default: 30)."
    )

    # === CINDE Jobs (API GraphQL, sin browser) ===
    scrape_cinde = scrape_commands.add_parser(
        "cinde", help="Consultar CINDE Jobs (API GraphQL pública, sin browser)."
    )
    scrape_cinde_target = scrape_cinde.add_mutually_exclusive_group(required=True)
    scrape_cinde_target.add_argument("--site")
    scrape_cinde_target.add_argument(
        "--group",
        help="Consultar secuencialmente los sitios CINDE activos del grupo.",
    )
    scrape_cinde.add_argument(
        "--pages", type=int, default=10, help="Páginas de resultados (default: 10, 100 por página)."
    )

    # === Procomer (API REST) ===
    scrape_procomer = scrape_commands.add_parser(
        "procomer", help="Consultar Procomer (API REST pública, sin browser)."
    )
    scrape_procomer_target = scrape_procomer.add_mutually_exclusive_group(required=True)
    scrape_procomer_target.add_argument("--site")
    scrape_procomer_target.add_argument(
        "--group",
        help="Consultar secuencialmente los sitios Procomer activos del grupo.",
    )
    scrape_procomer.add_argument(
        "--pages", type=int, default=5, help="Páginas de resultados (default: 5, 16 por página)."
    )
    scrape_procomer.add_argument(
        "--search-text", help="Término de búsqueda (default: config del sitio o 'Soporte')."
    )

    # === LinkedIn (browser, búsqueda por término) ===
    scrape_linkedin = scrape_commands.add_parser(
        "linkedin", help="Consultar LinkedIn Jobs (requiere Brave CDP con sesión)."
    )
    scrape_linkedin_target = scrape_linkedin.add_mutually_exclusive_group(required=True)
    scrape_linkedin_target.add_argument("--site")
    scrape_linkedin_target.add_argument(
        "--group",
        help="Consultar secuencialmente los sitios LinkedIn activos del grupo.",
    )
    scrape_linkedin.add_argument(
        "--pages", type=int, default=2, help="Páginas de resultados a scroll (default: 2)."
    )
    scrape_linkedin.add_argument(
        "--port", type=int, default=9222, help="Puerto CDP de Brave (default: 9222)."
    )

    # === Indeed (browser, búsqueda por término) ===
    scrape_indeed = scrape_commands.add_parser(
        "indeed", help="Consultar Indeed CR (requiere Brave CDP con sesión)."
    )
    scrape_indeed_target = scrape_indeed.add_mutually_exclusive_group(required=True)
    scrape_indeed_target.add_argument("--site")
    scrape_indeed_target.add_argument(
        "--group",
        help="Consultar secuencialmente los sitios Indeed activos del grupo.",
    )
    scrape_indeed.add_argument(
        "--pages", type=int, default=2, help="Páginas de resultados (default: 2)."
    )
    scrape_indeed.add_argument(
        "--port", type=int, default=9222, help="Puerto CDP de Brave (default: 9222)."
    )

    search = commands.add_parser("search", help="Buscar vacantes guardadas.")
    search.add_argument("--text", help="Texto en titulo, empresa, ubicacion o descripcion.")
    search.add_argument("--country")
    search.add_argument("--region")
    search.add_argument("--city")
    search.add_argument(
        "--published-from",
        help="Fecha minima de publicacion (AAAA-MM-DD), inclusiva.",
    )
    search.add_argument(
        "--published-to",
        help="Fecha maxima de publicacion (AAAA-MM-DD), inclusiva.",
    )
    search.add_argument(
        "--work-mode", choices=("onsite", "hybrid", "remote", "unknown")
    )
    search.add_argument(
        "--employment-type",
        help="Tipo de empleo exacto tal como esta guardado, por ejemplo Full-time.",
    )
    search.add_argument(
        "--schedule",
        help="Horario exacto tal como esta guardado, por ejemplo Night shift.",
    )
    search.add_argument(
        "--salary-currency",
        help="Moneda exacta de la oferta (por ejemplo CRC o USD).",
    )
    search.add_argument(
        "--salary-period",
        help="Periodo salarial exacto (por ejemplo monthly o annual).",
    )
    search.add_argument(
        "--salary-at-least",
        type=float,
        help="Piso salarial; requiere moneda y periodo.",
    )
    search.add_argument(
        "--salary-at-most",
        type=float,
        help="Tope salarial; requiere moneda y periodo.",
    )
    search.add_argument(
        "--posting-language", choices=("es", "en", "mixed", "undetermined")
    )
    search.add_argument(
        "--requirement",
        action="append",
        type=_parse_requirement,
        default=[],
        metavar="CATEGORY=CODE",
        help="Criterio estructurado repetible, por ejemplo language=en.",
    )
    search.add_argument("--include-preferred", action="store_true")
    search.add_argument("--include-inferred", action="store_true")
    search.add_argument("--include-ambiguous", action="store_true")
    search.add_argument(
        "--include-manually-skipped",
        action="store_true",
        help="Incluir vacantes marcadas para excluir de busquedas normales.",
    )
    search.add_argument(
        "--needs-review",
        action="store_true",
        help=(
            "Mostrar vacantes con company/pais/ubicacion faltantes o analisis "
            "incompleto, con error o campos ambiguos/desconocidos."
        ),
    )
    review_reason_choices = sorted(JOB_REVIEW_REASON_CODES) + [
        f"{prefix}:{category}"
        for prefix in sorted(JOB_REVIEW_FIELD_PREFIXES)
        for category in sorted(REQUIREMENT_CATEGORIES)
    ]
    search.add_argument(
        "--review-reason",
        choices=review_reason_choices,
        help="Limitar resultados a un motivo especifico de revision.",
    )
    search.add_argument(
        "--profile-compatibility",
        choices=tuple(PROFILE_COMPATIBILITY_LABELS),
        help="Filtrar por compatibilidad con los datos aprobados del perfil.",
    )
    search.add_argument(
        "--format",
        choices=("json", "csv"),
        default="json",
        help="Formato de salida. JSON es el predeterminado.",
    )
    search.add_argument(
        "--sort-by",
        choices=("last_seen", "date_published"),
        default="last_seen",
        help="Orden: ultima actividad de la fuente o fecha de publicacion.",
    )
    search.add_argument("--limit", type=int, default=50)

    location = commands.add_parser(
        "location", help="Normalizar datos de ubicacion almacenados."
    )
    location_commands = location.add_subparsers(
        dest="location_command", required=True
    )
    normalize_workday = location_commands.add_parser(
        "normalize-workday",
        help="Completar ciudad/provincia desde el payload Workday guardado.",
    )
    normalize_workday.add_argument("--site", help="Limitar a un sitio Workday.")
    normalize_workday.add_argument(
        "--apply",
        action="store_true",
        help="Guardar cambios; sin esta opcion solo muestra una simulacion.",
    )
    args = parser.parse_args()

    database = Database(args.db)
    command_exit_code = 0
    if args.command == "init-db":
        version = database.initialize()
        print(f"Base v1.5 inicializada: {database.path} (esquema {version})")
    elif args.command == "backup":
        try:
            backup_path = database.backup_to(args.output)
        except (OSError, sqlite3.Error, ValueError) as exc:
            parser.error(f"No se pudo crear el respaldo: {exc}")
        print(json.dumps({"backup": str(backup_path)}, ensure_ascii=False))
    elif args.command == "db" and args.database_command == "check":
        try:
            report = database.check_integrity()
        except (OSError, sqlite3.Error) as exc:
            parser.error(f"No se pudo verificar la base: {exc}")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        if not report["ok"]:
            command_exit_code = 1
    else:
        database.initialize()
        if args.command == "schema-version":
            version = database.schema_version()
            print(f"Base: {database.path} (esquema {version})")
        else:
            repository = Repository(database)
            if args.command == "platform":
                repository.register_platform(
                    args.id,
                    args.name,
                    args.provider_type,
                    args.default_language,
                )
                print(f"Plataforma registrada: {args.id}")
            elif args.command == "site" and args.site_command == "add":
                repository.register_site(
                    args.id,
                    args.platform,
                    args.name,
                    args.url,
                    company_name=args.company,
                    country=args.country,
                    run_groups=args.group,
                    enabled=not args.disabled,
                )
                print(f"Sitio registrado: {args.id}")
            elif args.command == "site" and args.site_command == "list":
                sites = repository.list_sites(
                    enabled_only=args.enabled_only, run_group=args.group
                )
                print(json.dumps(sites, ensure_ascii=False, indent=2))
            elif args.command == "site" and args.site_command == "set-enabled":
                updated = repository.set_site_enabled(
                    args.id, args.enabled
                )
                if not updated:
                    parser.error(f"No existe el sitio {args.id!r}.")
                state_text = "activo" if args.enabled else "pausado"
                print(f"Sitio {args.id}: {state_text}")
            elif args.command == "site" and args.site_command == "mark-reviewed":
                if not repository.mark_site_reviewed(args.id):
                    parser.error(f"No existe el sitio {args.id!r}.")
                print(f"Sitio {args.id}: revision marcada como resuelta.")
            elif args.command == "run" and args.run_command == "start":
                run_id = repository.start_run(args.kind, args.group)
                print(f"Corrida {run_id} iniciada.")
            elif args.command == "run" and args.run_command == "site-result":
                status = repository.record_site_result(
                    args.run_id,
                    args.site,
                    jobs_found=args.found,
                    jobs_valid=args.valid,
                    jobs_new=args.new,
                    jobs_duplicate=args.duplicates,
                    error_message=args.error,
                    review_after_zero_runs=args.review_after_zero_runs,
                )
                print(f"Resultado de {args.site}: {status}.")
            elif args.command == "run" and args.run_command == "finish":
                status = repository.finish_run(args.run_id, args.summary)
                print(f"Corrida {args.run_id} cerrada: {status}.")
            elif args.command == "run" and args.run_command == "show":
                run_data = repository.get_run(args.run_id)
                if run_data is None:
                    parser.error(f"No existe la corrida {args.run_id}.")
                print(json.dumps(run_data, ensure_ascii=False, indent=2))
            elif args.command == "run" and args.run_command == "list":
                print(json.dumps(
                    repository.list_runs(args.limit),
                    ensure_ascii=False,
                    indent=2,
                ))
            elif args.command == "quality" and args.quality_command == "report":
                print(
                    json.dumps(
                        repository.get_data_quality_summary(),
                        ensure_ascii=False,
                        indent=2,
                    )
                )
            elif args.command == "quality" and args.quality_command == "duplicates":
                if not 1 <= args.limit <= 500:
                    parser.error("--limit debe estar entre 1 y 500.")
                print(
                    json.dumps(
                        repository.get_potential_duplicate_groups(args.limit),
                        ensure_ascii=False,
                        indent=2,
                    )
                )
            elif args.command == "analysis" and args.analysis_command == "extract":
                try:
                    result = _extract_job_analysis(repository, args.job)
                except ValueError as exc:
                    parser.error(str(exc))
                print(json.dumps(result, ensure_ascii=False, indent=2))
            elif (
                args.command == "analysis"
                and args.analysis_command == "extract-pending"
            ):
                if not 1 <= args.limit <= 500:
                    parser.error("--limit debe estar entre 1 y 500.")
                try:
                    pending_jobs = repository.search_jobs(
                        review_reason="analysis_missing",
                        limit=args.limit,
                        include_manually_skipped=args.include_manually_skipped,
                    )
                except (ValueError, sqlite3.Error) as exc:
                    parser.error(str(exc))
                if args.dry_run:
                    print(
                        json.dumps(
                            {
                                "mode": "dry_run",
                                "pending": len(pending_jobs),
                                "jobs": [
                                    {
                                        "job_id": job["job_id"],
                                        "title": job.get("title"),
                                    }
                                    for job in pending_jobs
                                ],
                            },
                            ensure_ascii=False,
                            indent=2,
                        )
                    )
                else:
                    analyses = []
                    failures = []
                    skipped = []
                    for job in pending_jobs:
                        job_id = str(job["job_id"])
                        try:
                            if repository.get_current_analysis(job_id) is not None:
                                skipped.append(
                                    {
                                        "job_id": job_id,
                                        "reason": "analysis_created_after_queue_load",
                                    }
                                )
                                continue
                            analyses.append(_extract_job_analysis(repository, job_id))
                        except (ValueError, sqlite3.Error) as exc:
                            failures.append(
                                {
                                    "job_id": job_id,
                                    "title": job.get("title"),
                                    "error": str(exc),
                                }
                            )
                    print(
                        json.dumps(
                            {
                                "mode": "local",
                                "selected": len(pending_jobs),
                                "analyzed": len(analyses),
                                "skipped": skipped,
                                "failures": failures,
                                "analyses": analyses,
                            },
                            ensure_ascii=False,
                            indent=2,
                        )
                    )
                    if failures:
                        command_exit_code = 1
            elif args.command == "ingest" and args.ingest_command == "import-json":
                try:
                    with args.input.open("r", encoding="utf-8") as input_file:
                        payload = json.load(input_file)
                except OSError as exc:
                    parser.error(f"No se pudo leer {args.input}: {exc}")
                except json.JSONDecodeError as exc:
                    parser.error(
                        f"JSON invalido en {args.input}, linea {exc.lineno}: "
                        f"{exc.msg}"
                    )
                if isinstance(payload, dict):
                    offers = payload.get("offers")
                else:
                    offers = payload
                try:
                    result = IngestionService(repository).import_offers(
                        args.site, offers
                    )
                except IngestionError as exc:
                    parser.error(str(exc))
                except ValueError as exc:
                    parser.error(str(exc))
                print(json.dumps(result, ensure_ascii=False, indent=2))
            elif args.command == "scrape" and args.scrape_command == "workday":
                service = IngestionService(repository)
                if args.site is not None:
                    site = repository.get_site(args.site)
                    if site is None:
                        parser.error(f"No existe el sitio {args.site!r}.")
                    config = site.get("site_config") or {}
                    is_workday = (
                        site.get("platform_id", "").casefold() == "workday"
                        or config.get("adapter") == "workday"
                    )
                    if not is_workday:
                        parser.error(
                            f"El sitio {args.site!r} no esta registrado como Workday."
                        )
                    try:
                        result = service.fetch_and_import(
                            args.site,
                            lambda: WorkdayFetcher().fetch(
                                site,
                                pages=args.pages,
                                search_text=args.search_text,
                                skip_source_job_ids=(
                                    repository.list_skipped_source_job_ids(args.site)
                                ),
                            ),
                            run_kind="workday",
                        )
                    except (IngestionError, ValueError, sqlite3.Error) as exc:
                        parser.error(str(exc))
                    print(json.dumps(result, ensure_ascii=False, indent=2))
                else:
                    sites = [
                        site
                        for site in repository.select_sites(args.group)
                        if (
                            site.get("platform_id", "").casefold() == "workday"
                            or (site.get("site_config") or {}).get("adapter")
                            == "workday"
                        )
                    ]
                    if not sites:
                        parser.error(
                            f"No hay tableros Workday habilitados para el grupo "
                            f"{args.group!r}."
                        )
                    fetcher = WorkdayFetcher()
                    results = []
                    failures = []
                    for site in sites:
                        site_id = site["site_id"]
                        try:
                            result = service.fetch_and_import(
                                site_id,
                                lambda site=site, site_id=site_id: fetcher.fetch(
                                    site,
                                    pages=args.pages,
                                    search_text=args.search_text,
                                    skip_source_job_ids=(
                                        repository.list_skipped_source_job_ids(
                                            site_id
                                        )
                                    ),
                                ),
                                run_kind="workday",
                            )
                        except (IngestionError, ValueError) as exc:
                            failures.append(
                                {
                                    "site_id": site_id,
                                    "run_id": (
                                        exc.run_id
                                        if isinstance(exc, IngestionError)
                                        else None
                                    ),
                                    "error": str(exc),
                                }
                            )
                            continue
                        results.append({"site_id": site_id, **result})
                    print(
                        json.dumps(
                            {
                                "mode": "group",
                                "group": args.group,
                                "sites_selected": len(sites),
                                "results": results,
                                "failures": failures,
                            },
                            ensure_ascii=False,
                            indent=2,
                        )
                    )
                    if failures:
                        command_exit_code = 1
            elif args.command == "scrape" and args.scrape_command == "greenhouse":
                service = IngestionService(repository)
                fetcher = GreenhouseFetcher()
                if args.site is not None:
                    site = repository.get_site(args.site)
                    if site is None:
                        parser.error(f"No existe el sitio {args.site!r}.")
                    config = site.get("site_config") or {}
                    is_greenhouse = (
                        site.get("platform_id", "").casefold() == "greenhouse"
                        or config.get("adapter") == "greenhouse"
                    )
                    if not is_greenhouse:
                        parser.error(
                            f"El sitio {args.site!r} no esta registrado como Greenhouse."
                        )
                    try:
                        result = service.fetch_and_import(
                            args.site,
                            lambda: fetcher.fetch(
                                site,
                                skip_source_job_ids=(
                                    repository.list_skipped_source_job_ids(args.site)
                                ),
                            ),
                            run_kind="greenhouse",
                        )
                    except (IngestionError, ValueError, sqlite3.Error) as exc:
                        parser.error(str(exc))
                    print(json.dumps(result, ensure_ascii=False, indent=2))
                else:
                    sites = [
                        site
                        for site in repository.select_sites(args.group)
                        if (
                            site.get("platform_id", "").casefold() == "greenhouse"
                            or (site.get("site_config") or {}).get("adapter")
                            == "greenhouse"
                        )
                    ]
                    if not sites:
                        parser.error(
                            f"No hay tableros Greenhouse habilitados para el grupo "
                            f"{args.group!r}."
                        )
                    results = []
                    failures = []
                    for site in sites:
                        site_id = site["site_id"]
                        try:
                            result = service.fetch_and_import(
                                site_id,
                                lambda site=site, site_id=site_id: fetcher.fetch(
                                    site,
                                    skip_source_job_ids=(
                                        repository.list_skipped_source_job_ids(
                                            site_id
                                        )
                                    ),
                                ),
                                run_kind="greenhouse",
                            )
                        except (IngestionError, ValueError, sqlite3.Error) as exc:
                            failures.append(
                                {
                                    "site_id": site_id,
                                    "run_id": (
                                        exc.run_id
                                        if isinstance(exc, IngestionError)
                                        else None
                                    ),
                                    "error": str(exc),
                                }
                            )
                            continue
                        results.append({"site_id": site_id, **result})
                    print(
                        json.dumps(
                            {
                                "mode": "group",
                                "group": args.group,
                                "sites_selected": len(sites),
                                "results": results,
                                "failures": failures,
                            },
                            ensure_ascii=False,
                            indent=2,
                        )
                    )
                    if failures:
                        command_exit_code = 1
            elif args.command == "scrape" and args.scrape_command == "ashby":
                service = IngestionService(repository)
                fetcher = AshbyFetcher()
                if args.site is not None:
                    site = repository.get_site(args.site)
                    if site is None:
                        parser.error(f"No existe el sitio {args.site!r}.")
                    config = site.get("site_config") or {}
                    is_ashby = (
                        site.get("platform_id", "").casefold() == "ashby"
                        or config.get("adapter") == "ashby"
                    )
                    if not is_ashby:
                        parser.error(
                            f"El sitio {args.site!r} no esta registrado como Ashby."
                        )
                    try:
                        result = service.fetch_and_import(
                            args.site,
                            lambda: fetcher.fetch(
                                site,
                                skip_source_job_ids=(
                                    repository.list_skipped_source_job_ids(args.site)
                                ),
                            ),
                            run_kind="ashby",
                        )
                    except (IngestionError, ValueError, sqlite3.Error) as exc:
                        parser.error(str(exc))
                    print(json.dumps(result, ensure_ascii=False, indent=2))
                else:
                    sites = [
                        site
                        for site in repository.select_sites(args.group)
                        if (
                            site.get("platform_id", "").casefold() == "ashby"
                            or (site.get("site_config") or {}).get("adapter")
                            == "ashby"
                        )
                    ]
                    if not sites:
                        parser.error(
                            f"No hay tableros Ashby habilitados para el grupo "
                            f"{args.group!r}."
                        )
                    results = []
                    failures = []
                    for site in sites:
                        site_id = site["site_id"]
                        try:
                            result = service.fetch_and_import(
                                site_id,
                                lambda site=site, site_id=site_id: fetcher.fetch(
                                    site,
                                    skip_source_job_ids=(
                                        repository.list_skipped_source_job_ids(
                                            site_id
                                        )
                                    ),
                                ),
                                run_kind="ashby",
                            )
                        except (IngestionError, ValueError, sqlite3.Error) as exc:
                            failures.append(
                                {
                                    "site_id": site_id,
                                    "run_id": (
                                        exc.run_id
                                        if isinstance(exc, IngestionError)
                                        else None
                                    ),
                                    "error": str(exc),
                                }
                            )
                            continue
                        results.append({"site_id": site_id, **result})
                    print(
                        json.dumps(
                            {
                                "mode": "group",
                                "group": args.group,
                                "sites_selected": len(sites),
                                "results": results,
                                "failures": failures,
                            },
                            ensure_ascii=False,
                            indent=2,
                        )
                    )
                    if failures:
                        command_exit_code = 1
            elif args.command == "scrape" and args.scrape_command == "lever":
                service = IngestionService(repository)
                fetcher = LeverFetcher()
                if args.site is not None:
                    site = repository.get_site(args.site)
                    if site is None:
                        parser.error(f"No existe el sitio {args.site!r}.")
                    config = site.get("site_config") or {}
                    is_lever = (
                        site.get("platform_id", "").casefold() == "lever"
                        or config.get("adapter") == "lever"
                    )
                    if not is_lever:
                        parser.error(
                            f"El sitio {args.site!r} no esta registrado como Lever."
                        )
                    try:
                        result = service.fetch_and_import(
                            args.site,
                            lambda: fetcher.fetch(
                                site,
                                skip_source_job_ids=(
                                    repository.list_skipped_source_job_ids(args.site)
                                ),
                            ),
                            run_kind="lever",
                        )
                    except (IngestionError, ValueError, sqlite3.Error) as exc:
                        parser.error(str(exc))
                    print(json.dumps(result, ensure_ascii=False, indent=2))
                else:
                    sites = [
                        site
                        for site in repository.select_sites(args.group)
                        if (
                            site.get("platform_id", "").casefold() == "lever"
                            or (site.get("site_config") or {}).get("adapter")
                            == "lever"
                        )
                    ]
                    if not sites:
                        parser.error(
                            f"No hay tableros Lever habilitados para el grupo "
                            f"{args.group!r}."
                        )
                    results = []
                    failures = []
                    for site in sites:
                        site_id = site["site_id"]
                        try:
                            result = service.fetch_and_import(
                                site_id,
                                lambda site=site, site_id=site_id: fetcher.fetch(
                                    site,
                                    skip_source_job_ids=(
                                        repository.list_skipped_source_job_ids(
                                            site_id
                                        )
                                    ),
                                ),
                                run_kind="lever",
                            )
                        except (IngestionError, ValueError, sqlite3.Error) as exc:
                            failures.append(
                                {
                                    "site_id": site_id,
                                    "run_id": (
                                        exc.run_id
                                        if isinstance(exc, IngestionError)
                                        else None
                                    ),
                                    "error": str(exc),
                                }
                            )
                            continue
                        results.append({"site_id": site_id, **result})
                    print(
                        json.dumps(
                            {
                                "mode": "group",
                                "group": args.group,
                                "sites_selected": len(sites),
                                "results": results,
                                "failures": failures,
                            },
                            ensure_ascii=False,
                            indent=2,
                        )
                    )
                    if failures:
                        command_exit_code = 1
            elif args.command == "scrape" and args.scrape_command == "recruitee":
                service = IngestionService(repository)
                fetcher = RecruiteeFetcher()
                if args.site is not None:
                    site = repository.get_site(args.site)
                    if site is None:
                        parser.error(f"No existe el sitio {args.site!r}.")
                    config = site.get("site_config") or {}
                    is_recruitee = (
                        site.get("platform_id", "").casefold() == "recruitee"
                        or config.get("adapter") == "recruitee"
                    )
                    if not is_recruitee:
                        parser.error(
                            f"El sitio {args.site!r} no esta registrado como Recruitee."
                        )
                    try:
                        result = service.fetch_and_import(
                            args.site,
                            lambda: fetcher.fetch(
                                site,
                                skip_source_job_ids=(
                                    repository.list_skipped_source_job_ids(args.site)
                                ),
                            ),
                            run_kind="recruitee",
                        )
                    except (IngestionError, ValueError, sqlite3.Error) as exc:
                        parser.error(str(exc))
                    print(json.dumps(result, ensure_ascii=False, indent=2))
                else:
                    sites = [
                        site
                        for site in repository.select_sites(args.group)
                        if (
                            site.get("platform_id", "").casefold() == "recruitee"
                            or (site.get("site_config") or {}).get("adapter")
                            == "recruitee"
                        )
                    ]
                    if not sites:
                        parser.error(
                            f"No hay tableros Recruitee habilitados para el grupo "
                            f"{args.group!r}."
                        )
                    results = []
                    failures = []
                    for site in sites:
                        site_id = site["site_id"]
                        try:
                            result = service.fetch_and_import(
                                site_id,
                                lambda site=site, site_id=site_id: fetcher.fetch(
                                    site,
                                    skip_source_job_ids=(
                                        repository.list_skipped_source_job_ids(
                                            site_id
                                        )
                                    ),
                                ),
                                run_kind="recruitee",
                            )
                        except (IngestionError, ValueError, sqlite3.Error) as exc:
                            failures.append(
                                {
                                    "site_id": site_id,
                                    "run_id": (
                                        exc.run_id
                                        if isinstance(exc, IngestionError)
                                        else None
                                    ),
                                    "error": str(exc),
                                }
                            )
                            continue
                        results.append({"site_id": site_id, **result})
                    print(
                        json.dumps(
                            {
                                "mode": "group",
                                "group": args.group,
                                "sites_selected": len(sites),
                                "results": results,
                                "failures": failures,
                            },
                            ensure_ascii=False,
                            indent=2,
                        )
                    )
                    if failures:
                        command_exit_code = 1
            elif args.command == "scrape" and args.scrape_command == "workable":
                service = IngestionService(repository)
                fetcher = WorkableFetcher()
                if args.site is not None:
                    site = repository.get_site(args.site)
                    if site is None:
                        parser.error(f"No existe el sitio {args.site!r}.")
                    config = site.get("site_config") or {}
                    is_workable = (
                        site.get("platform_id", "").casefold() == "workable"
                        or config.get("adapter") == "workable"
                    )
                    if not is_workable:
                        parser.error(
                            f"El sitio {args.site!r} no esta registrado como Workable."
                        )
                    try:
                        result = service.fetch_and_import(
                            args.site,
                            lambda: fetcher.fetch(
                                site,
                                skip_source_job_ids=(
                                    repository.list_skipped_source_job_ids(args.site)
                                ),
                            ),
                            run_kind="workable",
                        )
                    except (IngestionError, ValueError, sqlite3.Error) as exc:
                        parser.error(str(exc))
                    print(json.dumps(result, ensure_ascii=False, indent=2))
                else:
                    sites = [
                        site
                        for site in repository.select_sites(args.group)
                        if (
                            site.get("platform_id", "").casefold() == "workable"
                            or (site.get("site_config") or {}).get("adapter")
                            == "workable"
                        )
                    ]
                    if not sites:
                        parser.error(
                            f"No hay tableros Workable habilitados para el grupo "
                            f"{args.group!r}."
                        )
                    results = []
                    failures = []
                    for site in sites:
                        site_id = site["site_id"]
                        try:
                            result = service.fetch_and_import(
                                site_id,
                                lambda site=site, site_id=site_id: fetcher.fetch(
                                    site,
                                    skip_source_job_ids=(
                                        repository.list_skipped_source_job_ids(
                                            site_id
                                        )
                                    ),
                                ),
                                run_kind="workable",
                            )
                        except (IngestionError, ValueError, sqlite3.Error) as exc:
                            failures.append(
                                {
                                    "site_id": site_id,
                                    "run_id": (
                                        exc.run_id
                                        if isinstance(exc, IngestionError)
                                        else None
                                    ),
                                    "error": str(exc),
                                }
                            )
                            continue
                        results.append({"site_id": site_id, **result})
                    print(
                        json.dumps(
                            {
                                "mode": "group",
                                "group": args.group,
                                "sites_selected": len(sites),
                                "results": results,
                                "failures": failures,
                            },
                            ensure_ascii=False,
                            indent=2,
                        )
                    )
                    if failures:
                        command_exit_code = 1
            elif args.command == "scrape" and args.scrape_command == "successfactors":
                service = IngestionService(repository)
                fetcher = SuccessFactorsFetcher()
                if args.site is not None:
                    site = repository.get_site(args.site)
                    if site is None:
                        parser.error(f"No existe el sitio {args.site!r}.")
                    config = site.get("site_config") or {}
                    is_successfactors = (
                        site.get("platform_id", "").casefold() == "successfactors"
                        or config.get("adapter") == "successfactors"
                    )
                    if not is_successfactors:
                        parser.error(
                            f"El sitio {args.site!r} no esta registrado como "
                            f"SuccessFactors."
                        )
                    try:
                        result = service.fetch_and_import(
                            args.site,
                            lambda: fetcher.fetch(
                                site,
                                skip_source_job_ids=(
                                    repository.list_skipped_source_job_ids(args.site)
                                ),
                            ),
                            run_kind="successfactors",
                        )
                    except (IngestionError, ValueError, sqlite3.Error) as exc:
                        parser.error(str(exc))
                    print(json.dumps(result, ensure_ascii=False, indent=2))
                else:
                    sites = [
                        site
                        for site in repository.select_sites(args.group)
                        if (
                            site.get("platform_id", "").casefold()
                            == "successfactors"
                            or (site.get("site_config") or {}).get("adapter")
                            == "successfactors"
                        )
                    ]
                    if not sites:
                        parser.error(
                            f"No hay portales SuccessFactors habilitados para el "
                            f"grupo {args.group!r}."
                        )
                    results = []
                    failures = []
                    for site in sites:
                        site_id = site["site_id"]
                        try:
                            result = service.fetch_and_import(
                                site_id,
                                lambda site=site, site_id=site_id: fetcher.fetch(
                                    site,
                                    skip_source_job_ids=(
                                        repository.list_skipped_source_job_ids(
                                            site_id
                                        )
                                    ),
                                ),
                                run_kind="successfactors",
                            )
                        except (IngestionError, ValueError, sqlite3.Error) as exc:
                            failures.append(
                                {
                                    "site_id": site_id,
                                    "run_id": (
                                        exc.run_id
                                        if isinstance(exc, IngestionError)
                                        else None
                                    ),
                                    "error": str(exc),
                                }
                            )
                            continue
                        results.append({"site_id": site_id, **result})
                    print(
                        json.dumps(
                            {
                                "mode": "group",
                                "group": args.group,
                                "sites_selected": len(sites),
                                "results": results,
                                "failures": failures,
                            },
                            ensure_ascii=False,
                            indent=2,
                        )
                    )
                    if failures:
                        command_exit_code = 1

            # === ANE (browser, barrido por fecha) ===
            elif args.command == "scrape" and args.scrape_command == "ane":
                service = IngestionService(repository)
                fetcher = AneFetcher()
                db_path = repository.database.path
                if args.site is not None:
                    site = repository.get_site(args.site)
                    if site is None:
                        parser.error(f"No existe el sitio {args.site!r}.")
                    config = site.get("site_config") or {}
                    is_ane = (
                        site.get("platform_id", "").casefold() == "ane"
                        or config.get("adapter") == "ane"
                    )
                    if not is_ane:
                        parser.error(f"El sitio {args.site!r} no esta registrado como ANE.")
                    try:
                        result = service.fetch_and_import(
                            args.site,
                            lambda: fetcher.fetch(
                                site,
                                pages=args.pages,
                                skip_source_job_ids=(
                                    repository.list_skipped_source_job_ids(args.site)
                                ),
                                port=args.port,
                                dias=args.dias,
                            ),
                            run_kind="ane",
                        )
                    except (IngestionError, ValueError, sqlite3.Error) as exc:
                        parser.error(str(exc))
                    print(json.dumps(result, ensure_ascii=False, indent=2))
                else:
                    sites = [
                        site
                        for site in repository.select_sites(args.group)
                        if (
                            site.get("platform_id", "").casefold() == "ane"
                            or (site.get("site_config") or {}).get("adapter") == "ane"
                        )
                    ]
                    if not sites:
                        parser.error(
                            f"No hay sitios ANE habilitados para el grupo {args.group!r}."
                        )
                    results = []
                    failures = []
                    for site in sites:
                        site_id = site["site_id"]
                        try:
                            result = service.fetch_and_import(
                                site_id,
                                lambda site=site, site_id=site_id: fetcher.fetch(
                                    site,
                                    pages=args.pages,
                                    skip_source_job_ids=(
                                        repository.list_skipped_source_job_ids(site_id)
                                    ),
                                    port=args.port,
                                    dias=args.dias,
                                ),
                                run_kind="ane",
                            )
                        except (IngestionError, ValueError, sqlite3.Error) as exc:
                            failures.append(
                                {
                                    "site_id": site_id,
                                    "run_id": (
                                        exc.run_id
                                        if isinstance(exc, IngestionError)
                                        else None
                                    ),
                                    "error": str(exc),
                                }
                            )
                            continue
                        results.append({"site_id": site_id, **result})
                    print(
                        json.dumps(
                            {
                                "mode": "group",
                                "group": args.group,
                                "sites_selected": len(sites),
                                "results": results,
                                "failures": failures,
                            },
                            ensure_ascii=False,
                            indent=2,
                        )
                    )
                    if failures:
                        command_exit_code = 1

            # === CINDE Jobs (API GraphQL, sin browser) ===
            elif args.command == "scrape" and args.scrape_command == "cinde":
                service = IngestionService(repository)
                fetcher = CindeFetcher()
                if args.site is not None:
                    site = repository.get_site(args.site)
                    if site is None:
                        parser.error(f"No existe el sitio {args.site!r}.")
                    config = site.get("site_config") or {}
                    is_cinde = (
                        site.get("platform_id", "").casefold() == "cinde"
                        or config.get("adapter") == "cinde"
                    )
                    if not is_cinde:
                        parser.error(f"El sitio {args.site!r} no esta registrado como CINDE.")
                    try:
                        result = service.fetch_and_import(
                            args.site,
                            lambda: fetcher.fetch(
                                site,
                                pages=args.pages,
                                skip_source_job_ids=(
                                    repository.list_skipped_source_job_ids(args.site)
                                ),
                            ),
                            run_kind="cinde",
                        )
                    except (IngestionError, ValueError, sqlite3.Error) as exc:
                        parser.error(str(exc))
                    print(json.dumps(result, ensure_ascii=False, indent=2))
                else:
                    sites = [
                        site
                        for site in repository.select_sites(args.group)
                        if (
                            site.get("platform_id", "").casefold() == "cinde"
                            or (site.get("site_config") or {}).get("adapter") == "cinde"
                        )
                    ]
                    if not sites:
                        parser.error(
                            f"No hay sitios CINDE habilitados para el grupo {args.group!r}."
                        )
                    results = []
                    failures = []
                    for site in sites:
                        site_id = site["site_id"]
                        try:
                            result = service.fetch_and_import(
                                site_id,
                                lambda site=site, site_id=site_id: fetcher.fetch(
                                    site,
                                    pages=args.pages,
                                    skip_source_job_ids=(
                                        repository.list_skipped_source_job_ids(site_id)
                                    ),
                                ),
                                run_kind="cinde",
                            )
                        except (IngestionError, ValueError, sqlite3.Error) as exc:
                            failures.append(
                                {
                                    "site_id": site_id,
                                    "run_id": (
                                        exc.run_id
                                        if isinstance(exc, IngestionError)
                                        else None
                                    ),
                                    "error": str(exc),
                                }
                            )
                            continue
                        results.append({"site_id": site_id, **result})
                    print(
                        json.dumps(
                            {
                                "mode": "group",
                                "group": args.group,
                                "sites_selected": len(sites),
                                "results": results,
                                "failures": failures,
                            },
                            ensure_ascii=False,
                            indent=2,
                        )
                    )
                    if failures:
                        command_exit_code = 1

            # === Procomer (API REST) ===
            elif args.command == "scrape" and args.scrape_command == "procomer":
                service = IngestionService(repository)
                fetcher = ProcomerFetcher()
                if args.site is not None:
                    site = repository.get_site(args.site)
                    if site is None:
                        parser.error(f"No existe el sitio {args.site!r}.")
                    config = site.get("site_config") or {}
                    is_procomer = (
                        site.get("platform_id", "").casefold() == "procomer"
                        or config.get("adapter") == "procomer"
                    )
                    if not is_procomer:
                        parser.error(f"El sitio {args.site!r} no esta registrado como Procomer.")
                    try:
                        result = service.fetch_and_import(
                            args.site,
                            lambda: fetcher.fetch(
                                site,
                                pages=args.pages,
                                search_text=args.search_text,
                                skip_source_job_ids=(
                                    repository.list_skipped_source_job_ids(args.site)
                                ),
                            ),
                            run_kind="procomer",
                        )
                    except (IngestionError, ValueError, sqlite3.Error) as exc:
                        parser.error(str(exc))
                    print(json.dumps(result, ensure_ascii=False, indent=2))
                else:
                    sites = [
                        site
                        for site in repository.select_sites(args.group)
                        if (
                            site.get("platform_id", "").casefold() == "procomer"
                            or (site.get("site_config") or {}).get("adapter") == "procomer"
                        )
                    ]
                    if not sites:
                        parser.error(
                            f"No hay sitios Procomer habilitados para el grupo {args.group!r}."
                        )
                    results = []
                    failures = []
                    for site in sites:
                        site_id = site["site_id"]
                        try:
                            result = service.fetch_and_import(
                                site_id,
                                lambda site=site, site_id=site_id: fetcher.fetch(
                                    site,
                                    pages=args.pages,
                                    search_text=args.search_text,
                                    skip_source_job_ids=(
                                        repository.list_skipped_source_job_ids(site_id)
                                    ),
                                ),
                                run_kind="procomer",
                            )
                        except (IngestionError, ValueError, sqlite3.Error) as exc:
                            failures.append(
                                {
                                    "site_id": site_id,
                                    "run_id": (
                                        exc.run_id
                                        if isinstance(exc, IngestionError)
                                        else None
                                    ),
                                    "error": str(exc),
                                }
                            )
                            continue
                        results.append({"site_id": site_id, **result})
                    print(
                        json.dumps(
                            {
                                "mode": "group",
                                "group": args.group,
                                "sites_selected": len(sites),
                                "results": results,
                                "failures": failures,
                            },
                            ensure_ascii=False,
                            indent=2,
                        )
                    )
                    if failures:
                        command_exit_code = 1

            # === LinkedIn (browser, búsqueda por término) ===
            elif args.command == "scrape" and args.scrape_command == "linkedin":
                service = IngestionService(repository)
                fetcher = LinkedInFetcher()
                if args.site is not None:
                    site = repository.get_site(args.site)
                    if site is None:
                        parser.error(f"No existe el sitio {args.site!r}.")
                    config = site.get("site_config") or {}
                    is_linkedin = (
                        site.get("platform_id", "").casefold() == "linkedin"
                        or config.get("adapter") == "linkedin"
                    )
                    if not is_linkedin:
                        parser.error(f"El sitio {args.site!r} no esta registrado como LinkedIn.")
                    try:
                        result = service.fetch_and_import(
                            args.site,
                            lambda: fetcher.fetch(
                                site,
                                pages=args.pages,
                                skip_source_job_ids=(
                                    repository.list_skipped_source_job_ids(args.site)
                                ),
                                port=args.port,
                            ),
                            run_kind="linkedin",
                        )
                    except (IngestionError, ValueError, sqlite3.Error) as exc:
                        parser.error(str(exc))
                    print(json.dumps(result, ensure_ascii=False, indent=2))
                else:
                    sites = [
                        site
                        for site in repository.select_sites(args.group)
                        if (
                            site.get("platform_id", "").casefold() == "linkedin"
                            or (site.get("site_config") or {}).get("adapter") == "linkedin"
                        )
                    ]
                    if not sites:
                        parser.error(
                            f"No hay sitios LinkedIn habilitados para el grupo {args.group!r}."
                        )
                    results = []
                    failures = []
                    for site in sites:
                        site_id = site["site_id"]
                        try:
                            result = service.fetch_and_import(
                                site_id,
                                lambda site=site, site_id=site_id: fetcher.fetch(
                                    site,
                                    pages=args.pages,
                                    skip_source_job_ids=(
                                        repository.list_skipped_source_job_ids(site_id)
                                    ),
                                    port=args.port,
                                ),
                                run_kind="linkedin",
                            )
                        except (IngestionError, ValueError, sqlite3.Error) as exc:
                            failures.append(
                                {
                                    "site_id": site_id,
                                    "run_id": (
                                        exc.run_id
                                        if isinstance(exc, IngestionError)
                                        else None
                                    ),
                                    "error": str(exc),
                                }
                            )
                            continue
                        results.append({"site_id": site_id, **result})
                    print(
                        json.dumps(
                            {
                                "mode": "group",
                                "group": args.group,
                                "sites_selected": len(sites),
                                "results": results,
                                "failures": failures,
                            },
                            ensure_ascii=False,
                            indent=2,
                        )
                    )
                    if failures:
                        command_exit_code = 1

            # === Indeed (browser, búsqueda por término) ===
            elif args.command == "scrape" and args.scrape_command == "indeed":
                service = IngestionService(repository)
                fetcher = IndeedFetcher()
                if args.site is not None:
                    site = repository.get_site(args.site)
                    if site is None:
                        parser.error(f"No existe el sitio {args.site!r}.")
                    config = site.get("site_config") or {}
                    is_indeed = (
                        site.get("platform_id", "").casefold() == "indeed"
                        or config.get("adapter") == "indeed"
                    )
                    if not is_indeed:
                        parser.error(f"El sitio {args.site!r} no esta registrado como Indeed.")
                    try:
                        result = service.fetch_and_import(
                            args.site,
                            lambda: fetcher.fetch(
                                site,
                                pages=args.pages,
                                skip_source_job_ids=(
                                    repository.list_skipped_source_job_ids(args.site)
                                ),
                                port=args.port,
                            ),
                            run_kind="indeed",
                        )
                    except (IngestionError, ValueError, sqlite3.Error) as exc:
                        parser.error(str(exc))
                    print(json.dumps(result, ensure_ascii=False, indent=2))
                else:
                    sites = [
                        site
                        for site in repository.select_sites(args.group)
                        if (
                            site.get("platform_id", "").casefold() == "indeed"
                            or (site.get("site_config") or {}).get("adapter") == "indeed"
                        )
                    ]
                    if not sites:
                        parser.error(
                            f"No hay sitios Indeed habilitados para el grupo {args.group!r}."
                        )
                    results = []
                    failures = []
                    for site in sites:
                        site_id = site["site_id"]
                        try:
                            result = service.fetch_and_import(
                                site_id,
                                lambda site=site, site_id=site_id: fetcher.fetch(
                                    site,
                                    pages=args.pages,
                                    skip_source_job_ids=(
                                        repository.list_skipped_source_job_ids(site_id)
                                    ),
                                    port=args.port,
                                ),
                                run_kind="indeed",
                            )
                        except (IngestionError, ValueError, sqlite3.Error) as exc:
                            failures.append(
                                {
                                    "site_id": site_id,
                                    "run_id": (
                                        exc.run_id
                                        if isinstance(exc, IngestionError)
                                        else None
                                    ),
                                    "error": str(exc),
                                }
                            )
                            continue
                        results.append({"site_id": site_id, **result})
                    print(
                        json.dumps(
                            {
                                "mode": "group",
                                "group": args.group,
                                "sites_selected": len(sites),
                                "results": results,
                                "failures": failures,
                            },
                            ensure_ascii=False,
                            indent=2,
                        )
                    )
                    if failures:
                        command_exit_code = 1

            elif args.command == "search":
                levels = ["required"]
                if args.include_preferred:
                    levels.append("preferred")
                if args.include_inferred:
                    levels.append("inferred")
                if args.include_ambiguous:
                    levels.append("ambiguous")
                filters = {
                    key: value
                    for key, value in {
                        "country": args.country,
                        "region": args.region,
                        "city": args.city,
                        "work_mode": args.work_mode,
                        "employment_type": args.employment_type,
                        "schedule": args.schedule,
                        "salary_currency": args.salary_currency,
                        "salary_period": args.salary_period,
                        "posting_language": args.posting_language,
                    }.items()
                    if value is not None
                }
                try:
                    jobs = repository.search_jobs(
                        text=args.text,
                        requirements=args.requirement,
                        filters=filters,
                        requirement_levels=levels,
                        limit=args.limit,
                        include_manually_skipped=args.include_manually_skipped,
                        needs_review_only=args.needs_review,
                        review_reason=args.review_reason,
                        sort_by=args.sort_by,
                        published_from=args.published_from,
                        published_to=args.published_to,
                        salary_at_least=args.salary_at_least,
                        salary_at_most=args.salary_at_most,
                    )
                except ValueError as exc:
                    parser.error(str(exc))
                if args.format == "csv" or args.profile_compatibility:
                    comparisons = repository.compare_jobs_to_candidate_profile(jobs)
                    for job in jobs:
                        status = repository.candidate_profile_status(
                            comparisons[job["job_id"]]
                        )
                        job["profile_compatibility"] = (
                            PROFILE_COMPATIBILITY_LABELS[status]
                        )
                    if args.profile_compatibility:
                        jobs = [
                            job
                            for job in jobs
                            if repository.candidate_profile_status(
                                comparisons[job["job_id"]]
                            )
                            == args.profile_compatibility
                        ]
                if args.format == "csv":
                    output = io.StringIO(newline="")
                    write_job_search_csv(output, jobs)
                    print(output.getvalue(), end="")
                else:
                    print(json.dumps(jobs, ensure_ascii=False, indent=2))
            elif (
                args.command == "location"
                and args.location_command == "normalize-workday"
            ):
                try:
                    records = repository.list_workday_location_records(args.site)
                except ValueError as exc:
                    parser.error(str(exc))
                report: dict[str, Any] = {
                    "mode": "apply" if args.apply else "dry_run",
                    "records_scanned": len(records),
                    "updates": [],
                    "skipped_existing": 0,
                    "skipped_unrecognized": 0,
                    "skipped_conflicting_sources": 0,
                }
                updates_by_job: dict[str, dict[str, str | None]] = {}
                conflicts: set[str] = set()
                for record in records:
                    if record["city"] and record["region"]:
                        report["skipped_existing"] = (
                            int(report["skipped_existing"]) + 1
                        )
                        continue
                    raw_payload = record["raw_payload"]
                    if not isinstance(raw_payload, dict):
                        report["skipped_unrecognized"] = (
                            int(report["skipped_unrecognized"]) + 1
                        )
                        continue
                    detail = raw_payload.get("detail")
                    info = (
                        detail.get("jobPostingInfo")
                        if isinstance(detail, dict)
                        else None
                    )
                    listing = raw_payload.get("listing")
                    location_text = (
                        info.get("location")
                        if isinstance(info, dict)
                        else None
                    )
                    if not isinstance(location_text, str) or not location_text.strip():
                        location_text = (
                            listing.get("locationsText")
                            if isinstance(listing, dict)
                            else None
                        )
                    if not isinstance(location_text, str) or not location_text.strip():
                        location_text = record["location_raw"]
                    config = record["site_config"]
                    country = record["site_country"] or "CR"
                    country_query = (
                        config.get("country_query", "Costa Rica")
                        if isinstance(config, dict)
                        else "Costa Rica"
                    )
                    if not isinstance(country_query, str):
                        country_query = "Costa Rica"
                    normalized = WorkdayFetcher.normalize_location(
                        location_text if isinstance(location_text, str) else "",
                        country,
                        country_query,
                    )
                    city_update = (
                        normalized["city"] if not record["city"] else None
                    )
                    region_update = (
                        normalized["region"] if not record["region"] else None
                    )
                    if city_update is None and region_update is None:
                        report["skipped_unrecognized"] = (
                            int(report["skipped_unrecognized"]) + 1
                        )
                        continue
                    city = record["city"] or city_update
                    region = record["region"] or region_update
                    job_id = record["job_id"]
                    proposed = {"city": city, "region": region}
                    previous = updates_by_job.get(job_id)
                    if previous is not None and previous != proposed:
                        conflicts.add(job_id)
                    else:
                        updates_by_job[job_id] = proposed

                for job_id, normalized in updates_by_job.items():
                    if job_id in conflicts:
                        report["skipped_conflicting_sources"] = (
                            int(report["skipped_conflicting_sources"]) + 1
                        )
                        continue
                    update: dict[str, str | None | bool] = {
                        "job_id": job_id,
                        **normalized,
                    }
                    if args.apply:
                        update["updated"] = repository.update_missing_job_location(
                            job_id,
                            city=normalized["city"],
                            region=normalized["region"],
                        )
                    report["updates"].append(update)
                print(json.dumps(report, ensure_ascii=False, indent=2))
    return command_exit_code
