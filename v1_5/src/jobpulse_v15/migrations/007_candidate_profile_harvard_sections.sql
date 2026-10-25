-- Extiende las categorias de candidate_profile_facts para cubrir TODAS las
-- secciones harvard-ats-v1: datos personales (contact), resumen (summary)
-- y proyectos (project), ademas de las categorias de matching existentes.
-- SQLite no permite ALTERar un CHECK, por lo que se recrea la tabla
-- conservando todos los datos existentes.

CREATE TABLE candidate_profile_facts_new (
    profile_fact_id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL CHECK (
        category IN (
            'language', 'education', 'license', 'experience', 'skill',
            'certification', 'work_authorization', 'schedule', 'availability',
            'other', 'contact', 'summary', 'project'
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
    origin TEXT NOT NULL DEFAULT 'manual'
        CHECK (origin IN ('manual', 'imported')),
    source_import_id INTEGER REFERENCES candidate_profile_imports(
        profile_import_id
    ) ON DELETE SET NULL,
    UNIQUE(category, value_code)
);

INSERT INTO candidate_profile_facts_new (
    profile_fact_id, category, value_code, value_text, qualifier,
    education_status, numeric_value, unit, notes, created_at, updated_at,
    origin, source_import_id
)
SELECT
    profile_fact_id, category, value_code, value_text, qualifier,
    education_status, numeric_value, unit, notes, created_at, updated_at,
    origin, source_import_id
FROM candidate_profile_facts;

DROP TABLE candidate_profile_facts;
ALTER TABLE candidate_profile_facts_new RENAME TO candidate_profile_facts;

CREATE INDEX idx_candidate_profile_category
    ON candidate_profile_facts(category, value_code);
