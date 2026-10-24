-- Migration 006: Add skills and languages columns to jobs table
-- For storing IA-enriched secondary fields

ALTER TABLE jobs ADD COLUMN skills TEXT;
ALTER TABLE jobs ADD COLUMN languages TEXT;

-- Add indexes for querying
CREATE INDEX IF NOT EXISTS idx_jobs_skills ON jobs(skills);
CREATE INDEX IF NOT EXISTS idx_jobs_languages ON jobs(languages);