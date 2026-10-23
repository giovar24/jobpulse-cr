CREATE TABLE job_manual_overrides (
    job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
    field_name TEXT NOT NULL CHECK (
        field_name IN ('title', 'company', 'country', 'region', 'city', 'work_mode')
    ),
    source_value_json TEXT NOT NULL,
    override_value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (job_id, field_name)
);
