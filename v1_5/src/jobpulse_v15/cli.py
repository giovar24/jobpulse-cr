"""Comandos de mantenimiento para JobPulse CR 1.5."""

import argparse
import hashlib
import io
import json
import sqlite3
from pathlib import Path
from typing import Any

from jobpulse_v15.analysis import EXTRACTOR_VERSION, RuleBasedExtractor
from jobpulse_v15.db import Database
from jobpulse_v15.exports import PROFILE_COMPATIBILITY_LABELS, write_job_search_csv
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
                    except (IngestionError, ValueError) as exc:
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
