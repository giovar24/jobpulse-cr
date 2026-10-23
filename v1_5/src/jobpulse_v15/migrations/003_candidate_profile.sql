CREATE TABLE candidate_profile_facts (
    profile_fact_id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL CHECK (
        category IN (
            'language', 'education', 'license', 'experience', 'skill',
            'certification', 'work_authorization', 'schedule', 'availability',
            'other'
        )
    ),
    value_code TEXT NOT NULL COLLATE NOCASE CHECK (length(trim(value_code)) > 0),
    value_text TEXT NOT NULL CHECK (length(trim(value_text)) > 0),
    qualifier TEXT,
    education_status TEXT CHECK (
        education_status IS NULL OR education_status IN (
            'completed', 'pursuing', 'unspecified'
        )
    ),
    numeric_value REAL CHECK (numeric_value IS NULL OR numeric_value >= 0),
    unit TEXT,
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(category, value_code)
);

CREATE INDEX idx_candidate_profile_category
    ON candidate_profile_facts(category, value_code);
