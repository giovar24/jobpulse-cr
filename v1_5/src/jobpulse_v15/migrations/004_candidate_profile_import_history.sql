CREATE TABLE candidate_profile_imports (
    profile_import_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_name TEXT NOT NULL CHECK (length(trim(source_name)) > 0),
    imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

ALTER TABLE candidate_profile_facts
    ADD COLUMN origin TEXT NOT NULL DEFAULT 'manual'
    CHECK (origin IN ('manual', 'imported'));

ALTER TABLE candidate_profile_facts
    ADD COLUMN source_import_id INTEGER REFERENCES candidate_profile_imports(
        profile_import_id
    ) ON DELETE SET NULL;

CREATE TABLE candidate_profile_import_snapshots (
    profile_import_id INTEGER NOT NULL REFERENCES candidate_profile_imports(
        profile_import_id
    ) ON DELETE CASCADE,
    category TEXT NOT NULL,
    value_code TEXT NOT NULL,
    was_present INTEGER NOT NULL CHECK (was_present IN (0, 1)),
    value_text TEXT,
    qualifier TEXT,
    education_status TEXT,
    numeric_value REAL,
    unit TEXT,
    notes TEXT,
    previous_origin TEXT CHECK (
        previous_origin IS NULL OR previous_origin IN ('manual', 'imported')
    ),
    previous_source_import_id INTEGER,
    PRIMARY KEY (profile_import_id, category, value_code)
);
