ALTER TABLE job_analysis
    ADD COLUMN source_site_id TEXT REFERENCES source_sites(site_id);

ALTER TABLE job_analysis
    ADD COLUMN source_config_hash TEXT;

CREATE INDEX idx_job_analysis_current_status
    ON job_analysis(is_current, analysis_status, job_id);

CREATE INDEX idx_job_requirements_lookup
    ON job_requirements(category, value_code, requirement_level, analysis_id);
