-- Auditoria de calidad de jobpulse_cr (SOLO LECTURA).
-- Uso manual: python tools/auditar.py --db data/jobs.db
-- Las mismas consultas viven en tools/auditar.py; este archivo es la
-- referencia para revisar a mano con cualquier cliente SQLite.
--
-- Umbrales esperados tras la limpieza:
--   html_tags = 0, entities = 0, double_spaces = 0, invalid_dates = 0,
--   duplicate_groups = 0, empty_title = 0, empty_link = 0, expired = 0.

-- 1. Volumen y distribucion por fuente.
SELECT source, COUNT(*) AS filas
FROM jobs
GROUP BY source
ORDER BY filas DESC;

-- 2. Etiquetas HTML que sobrevivieron a los providers.
--    La version en tools/auditar.py usa la UDF _tiene_html() con la regex
--    '<[a-zA-Z/][^<>]*>' para NO confundir texto como "English <> Spanish"
--    con una etiqueta real; a mano, el GLOB de abajo da un limite superior.
SELECT COUNT(*) AS html_tags
FROM jobs
WHERE description GLOB '*<*>*'
   OR requirements GLOB '*<*>*'
   OR title GLOB '*<*>*'
   OR company GLOB '*<*>*';

-- 3. Entidades HTML sin decodificar.
SELECT COUNT(*) AS entities
FROM jobs
WHERE description LIKE '%&amp;%' OR description LIKE '%&lt;%'
   OR description LIKE '%&gt;%'  OR description LIKE '%&#%'
   OR requirements LIKE '%&amp;%' OR requirements LIKE '%&lt;%'
   OR requirements LIKE '%&gt;%'  OR requirements LIKE '%&#%'
   OR title LIKE '%&amp;%' OR company LIKE '%&amp;%';

-- 4. Espacios dobles (sintoma de HTML/limpieza previa).
SELECT COUNT(*) AS double_spaces
FROM jobs
WHERE description LIKE '%  %' OR requirements LIKE '%  %'
   OR title LIKE '%  %' OR company LIKE '%  %' OR location LIKE '%  %';

-- 5. Fechas no ISO o imposibles (solo columnas no vacias).
SELECT COUNT(*) AS invalid_dates
FROM jobs
WHERE (TRIM(COALESCE(date_published, '')) <> ''
       AND date(date_published) IS NULL)
   OR (TRIM(COALESCE(expires_at, '')) <> ''
       AND date(expires_at) IS NULL);

-- 6. Vencidas que siguen en la tabla curada.
SELECT COUNT(*) AS expired
FROM jobs
WHERE TRIM(COALESCE(expires_at, '')) <> ''
  AND date(expires_at) < date('now');

-- 7. Vacios criticos.
SELECT
  SUM(CASE WHEN TRIM(COALESCE(title, '')) = '' THEN 1 ELSE 0 END) AS empty_title,
  SUM(CASE WHEN TRIM(COALESCE(link,  '')) = '' THEN 1 ELSE 0 END) AS empty_link,
  SUM(CASE WHEN TRIM(COALESCE(description, '')) = '' THEN 1 ELSE 0 END) AS empty_description,
  SUM(CASE WHEN TRIM(COALESCE(requirements, '')) = '' THEN 1 ELSE 0 END) AS empty_requirements
FROM jobs;

-- 8. Grupos duplicados exactos (empresa+titulo+ubicacion normalizados).
--    norm() es una UDF registrada por tools/auditar.py; si se ejecuta a mano,
--    reemplazarla por lower(trim(...)) o registrar la funcion desde Python.
WITH k AS (
  SELECT norm(company) || '|' || norm(title) || '|' || norm(location) AS clave,
         COUNT(*) AS n
  FROM jobs
  WHERE TRIM(COALESCE(dup_of, '')) = ''
  GROUP BY clave
)
SELECT COUNT(*) AS duplicate_groups,
       COALESCE(SUM(n), 0) AS duplicate_rows
FROM k
WHERE n > 1;

-- 9. Integridad y modo de journal.
PRAGMA quick_check;
PRAGMA journal_mode;
