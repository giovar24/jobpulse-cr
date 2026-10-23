CREATE TABLE source_platforms (
    platform_id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    provider_type TEXT NOT NULL,
    default_language TEXT,
    default_config_json TEXT NOT NULL DEFAULT '{}',
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE source_sites (
    site_id TEXT PRIMARY KEY,
    platform_id TEXT NOT NULL REFERENCES source_platforms(platform_id),
    site_name TEXT NOT NULL,
    company_name TEXT,
    base_url TEXT NOT NULL,
    country TEXT,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    run_groups_json TEXT NOT NULL DEFAULT '[]',
    site_config_json TEXT NOT NULL DEFAULT '{}',
    health_status TEXT NOT NULL DEFAULT 'unknown'
        CHECK (health_status IN (
            'unknown', 'healthy', 'zero_results', 'needs_review', 'error',
            'unsupported'
        )),
    consecutive_zero_runs INTEGER NOT NULL DEFAULT 0
        CHECK (consecutive_zero_runs >= 0),
    review_required INTEGER NOT NULL DEFAULT 0 CHECK (review_required IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_source_sites_enabled
    ON source_sites(enabled, platform_id);

CREATE TABLE jobs (
    job_id TEXT PRIMARY KEY,
    title TEXT NOT NULL CHECK (length(trim(title)) > 0),
    company TEXT,
    location_raw TEXT,
    country TEXT,
    region TEXT,
    city TEXT,
    work_mode TEXT CHECK (
        work_mode IS NULL OR work_mode IN ('onsite', 'hybrid', 'remote', 'unknown')
    ),
    employment_type TEXT,
    schedule TEXT,
    posting_language TEXT NOT NULL DEFAULT 'undetermined'
        CHECK (posting_language IN ('es', 'en', 'mixed', 'undetermined')),
    posting_language_origin TEXT NOT NULL DEFAULT 'unknown'
        CHECK (posting_language_origin IN ('source', 'detected', 'unknown')),
    date_published TEXT,
    expires_at TEXT,
    salary_raw TEXT,
    salary_min REAL,
    salary_max REAL,
    salary_currency TEXT,
    salary_period TEXT,
    description_raw TEXT,
    requirements_raw TEXT,
    first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (salary_min IS NULL OR salary_max IS NULL OR salary_min <= salary_max)
);

CREATE INDEX idx_jobs_publication_date ON jobs(date_published);
CREATE INDEX idx_jobs_location ON jobs(country, region, city);
CREATE INDEX idx_jobs_work_mode ON jobs(work_mode);
CREATE INDEX idx_jobs_posting_language ON jobs(posting_language);

CREATE TABLE job_source_records (
    site_id TEXT NOT NULL REFERENCES source_sites(site_id),
    source_job_id TEXT NOT NULL,
    job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
    source_url TEXT,
    raw_payload_json TEXT,
    source_updated_at TEXT,
    first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_detail_fetch_at TEXT,
    content_hash TEXT,
    PRIMARY KEY (site_id, source_job_id)
);

CREATE INDEX idx_job_source_records_job ON job_source_records(job_id);

CREATE TABLE source_job_memory (
    site_id TEXT NOT NULL REFERENCES source_sites(site_id),
    source_job_id TEXT NOT NULL,
    first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_detail_fetch_at TEXT,
    content_hash TEXT,
    PRIMARY KEY (site_id, source_job_id)
);

CREATE INDEX idx_source_job_memory_last_seen
    ON source_job_memory(last_seen_at);

CREATE TABLE source_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'ok', 'partial', 'error', 'cancelled')),
    run_kind TEXT NOT NULL DEFAULT 'full',
    summary TEXT
);

CREATE TABLE source_run_sites (
    run_id INTEGER NOT NULL REFERENCES source_runs(run_id) ON DELETE CASCADE,
    site_id TEXT NOT NULL REFERENCES source_sites(site_id),
    status TEXT NOT NULL DEFAULT 'running'
        CHECK (status IN (
            'running', 'ok', 'zero_results', 'error', 'skipped', 'unsupported'
        )),
    jobs_found INTEGER NOT NULL DEFAULT 0 CHECK (jobs_found >= 0),
    jobs_valid INTEGER NOT NULL DEFAULT 0 CHECK (jobs_valid >= 0),
    jobs_new INTEGER NOT NULL DEFAULT 0 CHECK (jobs_new >= 0),
    jobs_duplicate INTEGER NOT NULL DEFAULT 0 CHECK (jobs_duplicate >= 0),
    error_message TEXT,
    review_required INTEGER NOT NULL DEFAULT 0 CHECK (review_required IN (0, 1)),
    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at TEXT,
    PRIMARY KEY (run_id, site_id)
);

CREATE INDEX idx_source_run_sites_site ON source_run_sites(site_id, run_id);

CREATE TABLE job_analysis (
    analysis_id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
    text_hash TEXT NOT NULL,
    extractor_version TEXT NOT NULL,
    analysis_status TEXT NOT NULL
        CHECK (analysis_status IN ('pending', 'complete', 'partial', 'error')),
    is_current INTEGER NOT NULL DEFAULT 1 CHECK (is_current IN (0, 1)),
    analyzed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    error_message TEXT,
    UNIQUE (job_id, text_hash, extractor_version)
);

CREATE UNIQUE INDEX idx_job_analysis_current
    ON job_analysis(job_id)
    WHERE is_current = 1;

CREATE TABLE job_requirements (
    requirement_id INTEGER PRIMARY KEY AUTOINCREMENT,
    analysis_id INTEGER NOT NULL REFERENCES job_analysis(analysis_id)
        ON DELETE CASCADE,
    category TEXT NOT NULL,
    value_code TEXT,
    value_text TEXT NOT NULL,
    qualifier TEXT,
    requirement_level TEXT NOT NULL
        CHECK (requirement_level IN (
            'required', 'preferred', 'inferred', 'ambiguous'
        )),
    education_status TEXT
        CHECK (
            education_status IS NULL OR education_status IN (
                'completed', 'pursuing', 'unspecified'
            )
        ),
    minimum REAL,
    maximum REAL,
    unit TEXT,
    evidence TEXT,
    origin TEXT NOT NULL
        CHECK (origin IN (
            'source_field', 'rule', 'local_ai', 'api', 'manual'
        )),
    confidence REAL CHECK (
        confidence IS NULL OR (confidence >= 0 AND confidence <= 1)
    ),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (minimum IS NULL OR maximum IS NULL OR minimum <= maximum)
);

CREATE INDEX idx_job_requirements_category_value
    ON job_requirements(category, value_code, requirement_level);
CREATE INDEX idx_job_requirements_analysis
    ON job_requirements(analysis_id);

CREATE TABLE job_field_status (
    analysis_id INTEGER NOT NULL REFERENCES job_analysis(analysis_id)
        ON DELETE CASCADE,
    category TEXT NOT NULL,
    field_status TEXT NOT NULL
        CHECK (field_status IN ('found', 'not_mentioned', 'unknown', 'ambiguous')),
    checked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (analysis_id, category)
);

CREATE TABLE user_job_state (
    job_id TEXT PRIMARY KEY,
    title_snapshot TEXT NOT NULL,
    company_snapshot TEXT,
    source_url_snapshot TEXT,
    user_status TEXT NOT NULL
        CHECK (user_status IN ('reviewed', 'applied', 'archived', 'dismissed')),
    skip_automatic INTEGER NOT NULL DEFAULT 1
        CHECK (skip_automatic IN (0, 1)),
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE applications (
    application_id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    application_status TEXT NOT NULL
        CHECK (application_status IN (
            'planned', 'applied', 'recruiter_contact', 'interview', 'offer',
            'rejected', 'withdrawn'
        )),
    applied_at TEXT,
    next_action_at TEXT,
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_applications_job ON applications(job_id);
CREATE INDEX idx_applications_status ON applications(application_status);
