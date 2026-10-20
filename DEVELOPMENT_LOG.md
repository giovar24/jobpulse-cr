# Bitácora de Desarrollo: JobPulse Costa Rica

Este documento registra el proceso de creación, los desafíos técnicos enfrentados y las soluciones implementadas para el sistema de scraping de empleos JobPulse CR.

## 🎯 Objetivo del Proyecto
Crear un sistema eficiente de scraping de empleos para Costa Rica que consolide múltiples fuentes (LinkedIn, Indeed, ANE, PROCOMER), evite la detección de bots y genere reportes diarios de vacantes nuevas utilizando una base de datos persistente.

---

## 🛠️ Arquitectura Implementada
- **Lenguaje**: Python 3.11+
- **Orquestación**: `asyncio` para ejecución paralela de scrapers.
- **Base de Datos**: SQLite (`aiosqlite`) para almacenamiento de historial y detección de duplicados.
- **Motor de Navegación**: Playwright conectado vía CDP (Chrome DevTools Protocol) a Brave Browser.
- **Estrategia Híbrida**: 
    - API JSON para Procomer.
    - HTML Estático para Indeed (inicialmente).
    - Navegador Dinámico para LinkedIn y ANE.

---

## 📉 Registro de Problemas y Soluciones

### 1. Gestión de Sesiones y Bloqueos Anti-Bot
- **Problema**: Los sitios (especialmente Indeed y LinkedIn) bloqueaban las peticiones HTTP simples (Error 403 Forbidden) y requerían login constante.
- **Solución**: Implementación de conexión vía CDP al navegador Brave. Esto permite reutilizar la sesión real del usuario (cookies y huella digital), haciendo que el bot sea indistinguible de un humano.

### 2. Acceso al Perfil de Usuario en Windows
- **Problema**: El sistema no tomaba las credenciales/cookies porque buscaba el perfil de Brave en rutas de Linux.
- **Solución**: Se actualizó `BraveManager` para utilizar la variable de entorno `%LOCALAPPDATA%`, apuntando correctamente a `BraveSoftware\Brave-Browser\User Data`.

### 3. Errores de Ejecución del Navegador (Puerto 9222)
- **Problema**: El usuario debía lanzar Brave manualmente con flags técnicos (`--remote-debugging-port=9222`), lo cual era ineficiente y propenso a errores.
- **Solución**: Creación de `BraveManager` que automatiza el cierre de procesos previos de Brave y lo lanza automáticamente con un puerto de depuración dinámico y el perfil de usuario correcto.

### 4. Errores de Sintaxis y Tipado
- **Problema**: Errores de `NameError` (Dict, Optional no definidos) y `SyntaxError` debido a comentarios mal cerrados durante el desarrollo rápido.
- **Solución**: Limpieza profunda del código, estandarización de imports de `typing` y revisión de sintaxis en `main.py` y `providers`.

### 5. Persistencia de Datos (OperationalError)
- **Problema**: El sistema no podía abrir el archivo de base de datos SQLite debido a rutas relativas incorrectas.
- **Solución**: Implementación de rutas absolutas basadas en el directorio raíz del proyecto en `JobDatabase`.

---

### 7. Optimización de Extracción y Autenticación HTTP (LinkedIn e Indeed)
- **Problema**: Persistencia de datos vacíos debido a bloqueos de LinkedIn y la inestabilidad de la UI dinámica (paneles laterales que no cargaban).
- **Solución**: Implementación de un sistema de extracción basado en sesiones reales:
    1. **Inyección de Cookies**: El sistema ahora extrae las cookies activas del navegador Brave y las inyecta en las peticiones `httpx`, simulando un usuario autenticado.
    2. **Análisis de HTML Bruto**: Se implementó la extracción de descripciones mediante la búsqueda de marcadores de texto en el `innerText` y HTML, eliminando la dependencia total de selectores CSS.
    3. **Estandarización de Indeed**: Se aplicó la misma estrategia híbrida (Playwright para lista + HTTP para detalles) a Indeed, homogeneizando la calidad de los datos en todos los portales.

---

### 8. Procomer: API Respondía 202 y el Sistema la Descartaba
- **Problema**: La API `talento.procomer.com/api/candidate/jobs-available` responde con código HTTP **202 Accepted** (no 200) cuando hay resultados válidos. El código en `procomer.py` tenía un chequeo `if response.status_code != 200: break` que descartaba toda respuesta que no fuera exactamente 200, resultando en **0 empleos de Procomer en cada ejecución**.
- **Solución**: Cambiar el chequeo a `if not (200 <= response.status_code < 300)` para aceptar cualquier 2xx. También se limpió el HTML de `description` y `requirements` con BeautifulSoup (la API devuelve etiquetas `<p>`, `<ol>`, etc.), se extrajo el nombre real de la organización, y se manejó `salary = item.get("salary") or ""` para evitar `None`.

### 9. LinkedIn: Filas Vacías en el Reporte (Placeholders Lazy-Loaded)
- **Problema**: El reporte CSV y la DB contenían filas con solo "linkedin" y todos los demás campos vacíos. Causa: el extractor JavaScript de `linkedin.py` agregaba tarjetas aunque solo tuvieran el atributo `data-occludable-job-id` sin título ni link renderizados (placeholders lazy-loaded de LinkedIn). Además, esas filas vacías **envenenaban la DB**: al ocupar el `job_id` como PRIMARY KEY, impedían que la versión completa del mismo empleo se guardara después (fallaba por duplicado).
- **Solución**: 
    1. En el JS del extractor: `if (!titulo || !linkHref) return;` — descarta tarjetas sin título o sin link.
    2. Deduplicación con `Set` por `job_id` o link dentro del mismo page.
    3. Fallback de fecha desde `innerText` con regex que cubre plurales (`2 weeks ago`, `hace 3 días`).
    4. URL-encode de `keywords`/`location` con `quote_plus` (los espacios y acentos rompían la búsqueda).
    5. Cierre de `page` en `finally` para no acumular pestañas en Brave.
    6. Eliminación de `sync_playwright()` dentro de `fetch()` async — ahora el scraping corre en `asyncio.to_thread` y el enriquecimiento de detalles usa solo `httpx` con cookies del contexto, sin bloquear el event loop.

### 10. LinkedIn: Error 431 "Request Header Fields Too Large"
- **Problema**: Al pasar todas las cookies del navegador Brave a `httpx` para las peticiones de detalle, se incluían cookies de todos los dominios visitados (decenas de sitios), superando el límite de tamaño de headers y provocando error 431 en cada petición a LinkedIn.
- **Solución**: Filtrar cookies por dominio destino: `if "linkedin.com" in (c.get("domain") or "")`. Lo mismo se aplicó a Indeed (`"indeed.com" in domain`).

### 11. Indeed: Selectores Obsoletos (0 resultados)
- **Problema**: Los selectores CSS de `indeed.py` (`.jobsearch-JobResultCard`, `.resultContent`, etc.) ya no coincidían con la estructura actual de cr.indeed.com, devolviendo 0 tarjetas.
- **Solución**: Reescritura del extractor usando un enfoque JS resiliente basado en el atributo `data-jk` (identificador estable de Indeed) y selectores actualizados (`.job_seen_beacon`, `h2.jobTitle a`, `[data-testid='company-name']`, etc.). Los enlaces de tracking gigantes se normalizan a `https://cr.indeed.com/viewjob?jk={id}`. El detalle vía HTTP recibe 403 de Indeed, por lo que la extracción de lista es la fuente principal de datos.

### 12. ANE: URL de Login Incorrecta (404) y Selectores de Tarjetas
- **Problema**: La URL de login en `ane.py` era `https://www.ane.cr/Account/Login`, que devuelve **404**. La URL real es `https://www.ane.cr/Cuenta/Login`. Los campos del formulario tampoco coincidían: se esperaban `UserName`/`Password` pero los reales son `NombreUsuarioLogin`/`ContrasennaLogin`. Además, los selectores de tarjetas (`div.job-listing`, `h3.job-listing-title`, etc.) no se ajustaban a la estructura real de la página.
- **Solución**: 
    1. Login URL corregida a `/Cuenta/Login`.
    2. Campos actualizados a `#NombreUsuarioLogin` y `#ContrasennaLogin`.
    3. Detección de sesión: verificar si `.job-listing-footer a[href*='Puesto']` existe (solo los usuarios logueados ven enlaces de detalle; los anónimos ven `/Cuenta/RegistroUsuario`).
    4. Extracción reescrita: tarjetas `.job-listing`, título en `h3.job-listing-title`, empresa en `h4.job-listing-company`, ubicación en `.job-listing-description ul li`, fecha en `small.job-listing-description`, y enlace de detalle en `.job-listing-footer a`.
    5. Credenciales ahora se cargan desde `.env` con `python-dotenv` (variables `ANE_USER` y `ANE_PASSWORD`).

### 13. Base de Datos: Upsert y Limpieza de Placeholders
- **Problema**: El `INSERT` en `save_jobs` fallaba silenciosamente con `IntegrityError` cuando un `job_id` ya existía, lo que impedía actualizar empleos que se habían guardado previamente vacíos.
- **Solución**: 
    1. Verificación previa con `SELECT` para distinguir inserciones nuevas de actualizaciones.
    2. `UPDATE` condicional que solo completa campos vacíos (`CASE WHEN title = '' THEN excluded.title ELSE title END`), preservando datos ya existentes.
    3. Nuevo método `clean_empty_jobs()` que elimina al arrancar las filas con `title = '' AND link = ''` (se limpiaron 17 placeholders tóxicos de ejecuciones anteriores).
    4. Filtrado en `main.py`: `valid = [j for j in res if j.title and j.link]` antes de guardar.

### 14. Reporte CSV: Columnas Ampliadas
- **Problema**: El reporte solo contenía Fuente, Puesto, Empresa, Link y Fecha — sin ubicación, salario ni descripción.
- **Solución**: Columnas ampliadas a: **Fuente, Puesto, Empresa, Ubicación, Link, Fecha, Salario, Descripción** (resumida a 200 caracteres). El HTML se limpia con regex antes de incluirlo.

### 15. Configuración vía .env y Limpieza de Archivos
- **Problema**: Las credenciales de ANE y rutas de Brave estaban como placeholders `"..."` o comentadas en `main.py`. Existían archivos basura `providers\linkedin.py`` y `providers\indeed.py`` (con backtick en el nombre) generados por un redirect de shell mal cerrado.
- **Solución**: 
    1. Creado `.env` con credenciales ANE y `.env.example` como plantilla versionable.
    2. `main.py` carga variables con `load_dotenv()`: `ANE_USER`, `ANE_PASSWORD`, `BRAVE_BIN`, `BRAVE_PERFIL`.
    3. Eliminados los archivos con backtick.
    4. Silenciado el logger de `httpx` a WARNING para reducir ruido en consola.

### 16. Fechas de publicación ausentes (ANE e Indeed)
- **Problema**: ANE calculaba la fecha en la tarjeta pero **no la asignaba** al `JobOffer`; Indeed nunca la capturaba. Resultado: **0 fechas** en la DB para ambas fuentes.
- **Solución**: nueva utilidad `core/dates.py` (`parse_date`) que normaliza a ISO tanto fechas relativas ES/EN ("hace 3 días", "2 weeks ago") como absolutas. ANE asigna `date_published`; Indeed la extrae de la tarjeta (`.date` / coincidencia "Hace N ...").

### 17. Descripciones y requisitos vacíos (ANE, Indeed, LinkedIn)
- **Problema**: la descripción quedaba vacía en ANE (nunca se buscaba), Indeed (el detalle responde 403) y LinkedIn (dependía de las cookies de sesión).
- **Solución**:
    1. **ANE**: segunda pasada que navega el detalle autenticado y extrae descripción + requisitos.
    2. **LinkedIn**: descripción desde el endpoint público `jobs-guest/jobs/api/jobPosting/{id}` (no requiere sesión); respaldo a HTTP con cookies.
    3. **Indeed**: se guarda el snippet de la tarjeta como descripción base.
    4. `core/text.py` (`split_requirements`): separa requisitos de la descripción por encabezados ("Requisitos", "Requirements", "Qualifications").
    5. **Reporte**: nueva columna **Requisitos** (el CSV pasa a 9 columnas).

### 18. Corrección de Indeed (fecha, descripción y requisitos)
- **Problema**: la lista usaba selectores DOM frágiles; el `data-jk` estaba en el título (`h2.jcs-JobTitle`), no en la tarjeta; muchas tarjetas eran anuncios sin `jk`; no había fecha y la descripción por HTTP daba 403 (Cloudflare).
- **Solución**:
    1. La lista se lee del JSON embebido `window.mosaic.providerData["mosaic-provider-jobcards"].metaData.mosaicProviderJobCardsModel.results`: `title`, `company`, `formattedLocation`, `pubDate` (epoch), `formattedRelativeTime`, `snippet` y `salarySnippet`. Hay respaldo por DOM.
    2. La fecha se toma de `pubDate` (epoch ms → ISO), con respaldo en la fecha relativa.
    3. La descripción completa se obtiene del endpoint interno `/rpc/jobdescs` **dentro de la sesión del navegador** (httpx recibe 403).
    4. `split_requirements` separa los requisitos.
- **Verificado**: corrida real con Brave → **15/15** vacantes con fecha y descripción completa.

### 19. Corrección de ANE (DNS/ISP, detalle y separación de requisitos)
- **Problema**:
    1. El ISP a veces deja de resolver `www.ane.cr` (aunque `ane.cr`/`brete.cr` sí resuelven), y ANE quedaba en **0 filas**.
    2. El detalle tomaba el **menú lateral** ("Mi Perfil…") como descripción (42 chars).
    3. `split_requirements` no partía textos de una sola línea y el patrón `perfil` matcheaba "Mi Perfil".
- **Solución**:
    1. `core/browser_manager.py`: soporte de `--host-resolver-rules`; `main.py` resuelve un alias que sí funcione (`brete.cr`) y fuerza `MAP www.ane.cr <IP>`.
    2. `providers/ane.py`: el detalle se extrae de `.dashboard-content-inner` (sin el menú).
    3. `core/text.py`: nuevo `html_to_text` (conserva saltos de línea); `split_requirements` parte también una sola línea y `perfil` exige sufijo ("del puesto", "requerido", …).
- **Extra**: flag `main.py --fresh` (vaciado previo) y reporte de **últimas 24 h** (`data/report_24h_*.csv`, excluye fechas vacías o futuras).
- **Verificado**: corrida en limpio → **91** vacantes (Indeed 64, LinkedIn 16, ANE 10, Procomer 1), 100 % con fecha y descripción, **12** en las últimas 24 h.

### 20. Verificación de URLs de vacante
- **Objetivo**: confirmar que la columna `link` apunta a la **vacante** (no a la página de resultados).
- **Resultado**: los 91 registros tienen `link` coherente con su `job_id` (**0 inconsistencias**).
    - `procomer` -> `.../interview/<id>` -> **200** (público).
    - `linkedin` -> `/jobs/view/<id>/` -> **200** (vista pública).
    - `indeed` -> `.../viewjob?jk=<id>` -> **401** por HTTP puro (anti-bot); abre correctamente en el navegador.
    - `ane` -> `.../Puesto/Detalles?Puesto=<id>` -> **200** pero requiere sesión (redirige a login y vuelve a la vacante).
- **Nota**: la página de lista/búsqueda **no** se almacena; del origen solo se guarda `source` y `search_term`.

---

## 🚀 Estado Actual del Sistema
- [x] Core asíncrono operativo.
- [x] Base de datos SQLite integrada (con upsert y limpieza de placeholders).
- [x] Automatización de lanzamiento de Brave Browser.
- [x] Procomer (API 2xx + BeautifulSoup) $\rightarrow$ Funcional.
- [x] LinkedIn (CDP + httpx con cookies filtradas) $\rightarrow$ Funcional, sin filas vacías.
- [x] Indeed (CDP + selectores data-jk) $\rightarrow$ Funcional, links canónicos.
- [x] ANE (CDP + login corregido `/Cuenta/Login`) $\rightarrow$ Funcional con credenciales `.env`.
- [x] ANE: DNS del ISP esquivado con `--host-resolver-rules` (`www.ane.cr` -> IP de `brete.cr`).
- [x] Indeed actualizado: lista desde `window.mosaic` + descripción vía `/rpc/jobdescs` (ya no usa selectores DOM frágiles).
- [x] URL de vacante en columna `link`, coherente con `job_id` (verificado: 0 inconsistencias).
- [x] DB actual: 91 vacantes, 0 filas vacías; reporte de 24 h: 12 (esto corrige la cifra "136" anterior).
- [x] Fechas normalizadas a ISO (`core/dates.py`) en LinkedIn/Indeed/ANE $\rightarrow$ Funcional.
- [x] Descripciones/requisitos: LinkedIn (endpoint guest), ANE (detalle autenticado), Indeed (`mosaic` + `/rpc/jobdescs`) $\rightarrow$ Funcional.
- [x] Reporte ampliado a **9 columnas** (incluye Requisitos).
- [x] Corrida en limpio (`--fresh`) y reporte de **últimas 24 h** $\rightarrow$ `data/report_24h_*.csv`.
- [x] Cobertura tras corrida limpia: 91 vacantes, fecha y descripción 100 %, requisitos en 68.
- [x] Reporte diario ampliado (8 columnas) $\rightarrow$ Funcional.
- [x] Variables de entorno vía `.env` $\rightarrow$ Integrado.
- [x] DB limpia: 136 empleos, 0 filas vacías (verificado en última ejecución).

---

## 🔎 Motor de Búsqueda Semántica (embeddings locales)

### 21. Búsqueda semántica 100% local + expansión de términos

**Contexto**: con el scraping funcionando y la DB consolidada, se monto la propuesta del "Buscador de Empleo Local con Embeddings" (EmbeddingGemma) sobre la infraestructura existente, adaptado de CSV a SQLite.

**Implementación**:
1. **`core/search_engine.py`** — clase `SemanticSearchEngine`:
   - Genera embeddings ("Puesto | Empresa | Ubicación | Requisitos | Descripción" combinados, recortados a 2400 chars).
   - **Cache incremental** en `data/embeddings_cache.npy` + `embeddings_meta.json` (ids alineados con el vector): solo se vectorizan los `job_id` nuevos; se invalida si cambia el modelo o la dimensión. Con 115 vacantes el índice completo toma ~20 s en CPU; las corridas siguientes son instantáneas.
   - **Bug corregido (union del cache)**: `load()` reescribía el cache con el subconjunto del filtro actual (ej. solo linkedin), borrando los vectores del resto. Fix: el cache siempre se reescribe con la **unión** (ids previos + nuevos); las consultas filtradas solo toman filas del cache, nunca lo recortan. Además, el meta guarda `configured` + `model` resuelto para que el fallback de modelo no invalide el cache en cada corrida.
   - Consultas con prompt oficial de EmbeddingGemma (`task: search result | query: ...`) y similitud de coseno vía producto punto (vectores L2-normalizados).
   - **Fallback automático**: EmbeddingGemma es un modelo *gated* (licencia Gemma). Sin `HF_TOKEN` aceptada, cae a `sentence-transformers/paraphrase-multilingual-mpnet-base-v2` sin interrumpir el flujo. Para usar EmbeddingGemma: aceptar licencia en HF, poner `HF_TOKEN` en `.env` (opcional `EMBEDDING_MODEL` para fijar otro modelo).
   - `rank_terms()` / `expand_searches()`: vocabularios ES/EN (~30 + ~34 términos) rankeados por cercanía semántica a una temática base, para multiplar los términos fijos del scraper.
2. **`search.py`** — CLI/REPL del buscador:
   - Consultas en lenguaje natural sobre `jobs.db`: `python search.py "soporte tecnico bilingue para centro de datos"`.
   - Filtros duros aplicados ANTES del orden semántico (`--fuente`, `--dias`), como recomienda la propuesta. REPL con comandos `dias N` / `fuente X`.
3. **`core/database.py`** — nuevo `get_all_jobs(days=...)` que alimenta el índice (solo filas con título y descripción/requisitos).
4. **`main.py`** — flag `--expand`: expande las 3 temáticas fijas en variantes semánticas por idioma (`puesto` para ANE/Procomer, `puesto_en` para LinkedIn/Indeed) antes de hacer scraping. Cada reemplazo garantiza retener la temática original.
5. **Validado**: "soporte tecnico bilingue para centro de datos" → 8/8 resultados relevantes (Service Desk trilingüe, Soporte NOC, Customer Support Lead, IT Helpdesk...), scores 53-58% con el modelo fallback.

---

## 👤 Perfil del Candidato (integración completa)

### 22. El perfil como fuente única de verdad

**Objetivo**: pasar de temáticas hardcodeadas y búsquedas sueltas a un sistema dirigido por el perfil del candidato, dejando preparada la interfaz para un futuro analizador de CV.

**Implementación**:
1. **`core/profile.py`** (nuevo) — `CandidateProfile` sobre `data/profile.json`:
   - Secciones: `titulos`, `skills`, `temas` (ES/EN para scraping), `pesos` (peso relativo de cada sección), `filtros` (dias/fuentes por defecto).
   - `to_searches()` alimenta al scraper; `sections()` alimenta al ranking.
   - **Contrato del futuro analizador de CV**: su única tarea será leer un CV (PDF/DOCX) y escribir/actualizar `data/profile.json` en este formato.
2. **`core/search_engine.py`** — `search_profile()`:
   - Cada item de cada sección se convierte en consulta (`task: search result | query:`); score de sección = promedio de sus items; score del empleo = suma ponderada de secciones según `pesos` del perfil.
   - Devuelve desglose `{titulo, skills, tema}` por vacante (se muestra en la salida).
   - `expand_searches()` acepta `vocab_es`/`vocab_en`: el scraper prioriza los títulos del propio candidato al elegir variantes.
3. **`search.py`** — `--perfil` (o comando `perfil` en el REPL): ranking completo de "mejores opciones para el candidato" con desglose; respeta `filtros` del perfil.
4. **`main.py`** — integración total: si existe el perfil, sus `temas` reemplazan la lista hardcodeada del scraping y la **expansión semántica corre por defecto** (`--sin-expand` para apagarla).
5. **`data/profile.json`** (nuevo) — perfil inicial (enfoque Soporte TI / Help Desk CR, en línea con las búsquedas originales).
6. **Fix de consola Windows**: la salida de `search.py` tubiada con PowerShell rompía en caracteres Unicode (cp1252); se fuerza `sys.stdout` UTF-8 con `errors="replace"`.

**Validado** (`--perfil --top 6`): ranking coherente — 1° TÉCNICO SOPORTE N1 (titulo 45% | skills 36% | tema 57%), Soporte Técnico ANE, Service Desk Analyst trilingüe, Customer Support Lead...

### 23. Dashboard Streamlit (`app.py`)

**Objetivo**: desplegar la informacion (ranking del perfil + busqueda libre) en una web local sin tocar consola.

**Implementacion**:
1. Dos pestañas: **"🎯 Ranking de mejores opciones"** (perfil con desglose titulo/skills/tema en columnas) y **"🔍 Busqueda libre"** (texto natural + expanders con descripcion y requisitos de los 3 primeros).
2. Sidebar: slider de dias, selector de portal, top N, ruta de perfil editable, boton **Reindexar vacantes nuevas** (limpia el cache de DB) y expander "Modelo" (muestra configurado vs activo, avisa si falta HF_TOKEN).
3. Cache de Streamlit: `st.cache_resource` para el modelo (se carga una vez por proceso), `st.cache_data(ttl=120)` para la lectura de la DB. Boton "Calcular ranking" pagina el modelo solo al primer uso.
4. `to_df()` transforma resultados a DataFrame con columnas Match % y desglose, y `LinkColumn` clicable.
5. API deprecada `use_container_width` reemplazada por `width="stretch"`.

**Problemas detectados y corregidos**:
- **`ModuleNotFoundError: No module named 'torchvision'`**: el watcher de archivos de Streamlit recorre la lista completa de `transformers` y varios `image_processing_*.py` (ZoeDepth, SAM, Qwen-VL...) importan `torchvision`. En consola no afectaba (`search.py` corre igual sin el), pero en Streamlit rompia la corrida del ranking. Fix: `pip install torchvision` + agregado a `requirements.txt`.
- Deprecacion `use_container_width` (se retira en 2025-12-31): reemplazado por `width="stretch"`.

**Ejecucion**: `streamlit run app.py` (o `run.bat` posterior). Servidor en http://localhost:8501.

**Validado en navegador**: filtros activos, perfil "Candidato CR" detectado, ranking real en tabla (TÉCNICO SOPORTE N1 44.6% con desglose 45/36/57, CRI Customer Support Specialist 42.9%, Soporte Tecnico ANE 42.6%, ...), 100% local.

---

## 📄 Analizador de CV (cv2profile.py)

### 24. CV -> perfil: cerrar el ciclo candidato

**Objetivo**: que el perfil (`data/profile.json`) se genere/actualice automaticamente desde el CV del candidato, cerrando el ciclo completo deseado (CV -> perfil -> scraping -> ranking -> dashboard).

**Implementacion**:
1. **`core/cv_analysis.py`** (nuevo):
   - `extract_text()`: PDF (pypdf), DOCX (python-docx), TXT/MD.
   - `detect_languages()`: idiomas + nivel ("ingles ... intermedio/avanzado/..." -> skills "Ingles Intermedio").
   - `SKILL_LEXICON`: lexicon curado de ~70 skills TI/negocio (ES+EN) con match por frontera de palabra.
   - `CVAnalyzer._suggest_titles()`: rankea los vocabularios de puestos del motor contra el encabezado del CV (sin contactos) usando embeddings; titulos citados literalmente en el CV se priorizan primero.
   - `analyze()`: titulos ES+EN, skills, y pares ES/EN de temas alineados por orden de cercania.
   - `merge_profile()`: fusion (titulos/skills se suman sin duplicados; temas derivados del CV se reemplazan) o `--replace`.
2. **`cv2profile.py`** (CLI): `python cv2profile.py MiCV.pdf [--save] [--replace] [--out X]`; dry-run por defecto, backup del perfil anterior con timestamp, y recordatorio de re-verificar con `search.py --perfil` / Streamlit.
3. **Validado** con `data/demo_cv.docx` (CV sintetico soporte): titulos coherentes ("Analista de Soporte", "Atencion al Cliente Bilingue", "Soporte Tecnico"...), 20 skills con idioma con nivel, temas ES/EN emparejados (Analista de Soporte/Support Desk Technician, ...). Uso con el CV real: `python cv2profile.py "MiCV.pdf" --save`.






---

## 📇 Capa ATS (Applicant Tracking System)

### 25. Comportamiento tipo ATS: score ponderado + keywords faltantes

**Objetivo**: que el sistema evalucue perfiles como los ATS reales (Oracle AI Rating, Eightfold, experimentos LiveCareer), no solo por similitud semantica.

**Investigacion previa (web)**: los ATS reales evaluan: palabras clave/hard skills (~40% del score; ~75% de CVs se rechazan por keywords faltantes), titulo del puesto (~30%), experiencia en anos vs minimo requerido (~15%), educacion y certificaciones (~7.5%), idiomas con nivel explicito (~7.5%). Umbrales tipicos: >=85 fuerte, 70-84 ajustar, <70 debil. Pesos configurables que suman 1.

**Que es IA y que no** (hibrido deliberado):
- IA (embeddings locales, CPU): unico componente semantico = similitud titulo candidato vs titulo vacante (coseno con QUERY_PROMPT/DOC_PROMPT). Nada de LLM ni APIs de pago.
- Reglas puras (regex + lexicon + aritmetica): keywords faltantes, anios requeridos, nivel de educacion, idiomas requeridos, score ponderado, veredicto por umbrales.

**Implementacion**:
1. **`core/ats.py`** (nuevo):
   - `ats_score(profile, job, engine, weights)`: score 0-100 con pesos por defecto `skills .40 | titulo .30 | experiencia .15 | educacion .075 | idiomas .075` (sobrescribibles via `profile["pesos_ats"]`); renormaliza pesos de componentes ausentes (ej. vacante sin anios exigidos).
   - `job_keywords()`: hard skills de la oferta = coincidencias del lexicon en titulo+descripcion+requisitos (los tokens genericos del titulo no cuentan: los cubre el componente semantico).
   - `_covered()`: cobertura multipalabra ("Soporte Tecnico" cubre "soporte"; "Help Desk" cubre "helpdesk") para no marcar falsos keywords faltantes.
   - `extract_years_required/education_required/languages_required()`: requisitos de la vacante por regex.
   - `title_semantic_match()`: coseno embeddings (componente IA).
   - `rank_with_ats()`: reordena resultados como `0.6 semantico + 0.4 ATS`.
   - `missing`: keywords de la oferta ausentes en el perfil ("keyword gaps").
2. **`core/profile.py`**: campos ATS opcionales (`experiencia_anios`, `educacion_nivel`, `idiomas` structurados, `certificaciones`, `contacto`, `pesos_ats`) + `to_dict()`.
3. **`core/cv_analysis.py`**: extractores de CV - `detect_experience_years()` (frase "N anios" o rangos YYYY-YYYY deduplicados, tope 45), `detect_education_level()` (jerarquia maestria>universitario>tecnologo>tecnico>bachiller), `detect_certifications()` (lexicon ISTQB/ITIL/PMP/CCNA/...), `detect_contact()` (email/tel/linkedin), idiomas con nivel (sin acentos, deduplicados). `merge_profile()` fusiona campos ATS.
4. **`cv2profile.py`**: imprime "Ficha ATS" extraida del CV y la guarda en profile.json (con `pesos_ats` por defecto).
5. **`search.py --perfil`**: linea `ATS nn.n% [veredicto] | combinado nn.n%` + `keywords faltantes:` por vacante.
6. **`app.py`**: tab ranking con columnas `Total % / ATS % / Veredicto / ATS skills % / ATS titulo % / Faltan`; sidebar con "Ficha ATS" del candidato; caption explica pesos y umbrales.

**Validado**: unit test `ats_score` (61.5 Debil con missing Jira/Cypress/Pruebas de Regresion...), extractores CV (8 anios, universitario, ISTQB, contacto), CV ficticio PDF real (Ficha ATS: experiencia 8 anios, educacion universitario, idiomas, contacto email+linkedin).

**Problemas detectados y corregidos**:
- Nivel de idioma "no especificado" por acentos: regex de nivel ahora busca sobre texto sin acentos.
- Idiomas duplicados en la ficha: dedupe por nombre base.
- `Exited with code 1` en pipelines PowerShell con `2>&1`: es el manejo de stderr de PS, no del script (exit real = 0).
- mojibake previo en `app.py` por Get-Content/Set-Content de PowerShell: reescrito completo en UTF-8 con el tool write.


### 26. Arranque automatico del workspace + datos 100% genericos (portabilidad)

**Objetivo**: (a) que el sistema nunca contenga/exponga datos personales del candidato, (b) que cualquier equipo (o persona) pueda clonar el repo y ejecutar sin pasos manuales.

**Privacidad**:
- `data/profile.example.json` (nuevo, SI en git): plantilla generica 100% falsa (nombre "Juan Perez", email `juan.perez@example.com`, linkedin de ejemplo, 3 anios, universitario, Ingles Intermedio, ITIL) con todos los campos ATS.
- `data/profile.json` (SALIO de git: `git rm --cached` + `.gitignore`): es la copia local que el usuario personaliza con `cv2profile.py --save`. Sus datos reales jamas viajan al repo publico.
- Auditoria: grep de usuario, email y rutas locales sobre los archivos trackeados -> 0 coincidencias.
- El `profile.json` local existente se re-escribio con los mismos datos genericos de la plantilla.

**Portabilidad (`core/bootstrap.py`)**:
- `ensure_workspace()`: idempotente; crea `data/` si falta, copia `profile.example.json -> profile.json` si falta, y `.env.example -> .env` si falta (solo placeholders).
- Los 4 entrypoints lo llaman al inicio (antes de `load_dotenv()`): `main.py`, `search.py`, `app.py`, `cv2profile.py`.
- `app.py` ahora ademas llama `load_dotenv()` (antes no lo hacia: el dashboard nunca veia `HF_TOKEN` de `.env`, bug corregido).
- `jobs.db` ya se auto-crea via `CREATE TABLE IF NOT EXISTS` una vez existe `data/` (garantizado por bootstrap).
- Flujo "equipo nuevo": `git clone` + `pip install -r requirements.txt` + arrancar. Cero copias manuales.

**Validado**: py_compile de los 4 entrypoints; test real - `Remove-Item data/profile.json` + `ensure_workspace()` -> recreado desde la plantilla con "Juan Perez" y pesos_ats correctos.


### 27. Revision IA del CV (evidencia por frase) + extraccion bilingue ES|EN + benchmark de modelos

**Objetivo**: (a) que la revision IA de la extraccion sea *explicable* (evidencia frase a frase, no un score opaco), (b) que el analizador de CV funcione igual de bien en español que en inglés (requisito del usuario), (c) elegir el modelo de embeddings con datos y no por intuición.

**Revision IA por frase (`CVAnalyzer.review()`)**:
- v1 (coseno campo vs documento completo, 1500 chars) se descarto por no discriminar: `JavaScript` 0.153 vs `Analista Ciberseguridad` 0.424 en el mismo CV.
- v2 (la implementada): trocear el CV en frases (`_split_phrases`), embedarlas como documentos y usar cada campo extraido como query. Para cada campo se reporta `(frase encontrada, coseno)`.
- **Preferencia a la evidencia literal**: si un regex confirma la presencia literal del valor en el texto, el veredicto es `ok` aunque el coseno sea bajito (los embeddings castigan siglas: `js` vs `JavaScript`). Los umbrales (0.40 aceptar / 0.35 dudoso) solo aplican cuando NO hay presencia literal.
- `reporte_cv.py` (nuevo): reporte de 5 secciones - texto crudo, extracciones regex, IA local (titulo sugerido), revision IA y perfil JSON final. Es la herramienta oficial de validación del analizador.

**Extraccion bilingue ES|EN (`core/cv_analysis.py`)**:
- `_LANG_RULES` (antes `_LANG_PATTERNS`): nombre de idioma en ES o EN (`inglés|english`, `español|spanish`...) y nivel en ES o EN (`nativo|native`, `fluido|fluent`, `c1`...). `detect_languages_struct()` devuelve `[{"idioma", "nivel"}]` y `detect_languages()` las etiquetas para skills (`"Ingles Avanzado"`).
- **Trampa de ventana**: "Inglés: Profesional Avanzado C1 Español: Nativo C2" (líneas pegadas por el PDF) hacia que la ventana de 40 chars de `inglés` viera el `Nativo` del `español` y reportara el nivel equivocado. Fix: el segmento de nivel se **recorta en el primer nombre de otro idioma**.
- Experiencia EN: `N years of experience` ademas de `N años de experiencia` (regex unificada, `de|of` y `experiencia|experience` opcionales).
- Contacto EN: `Location:` paralelo a `Ubicación:`; `Phone:` ya cubierto.
- Duplicado eliminado: quedaban **dos** definiciones de `detect_languages()` (la vieja con `_LANG_PATTERNS` muerto ganaba por ser la ultima y reventaba con NameError).
- Lado vacante (`core/ats.py`): `_LANG_REQ` con aliases EN (`english`, `portuguese`...) para `extract_languages_required`; `extract_years_required` ya cubría `years`; `extract_education_required` ahora entiende `degree` con guarda `(?<!high )...(?!\s*of)` para no confundir "a high degree of autonomy".
- Plantilla `profile.example.json` + `profile.json` local: idiomas ahora incluyen `Espanol Nativo`.

**Benchmark de modelos (`bench_modelos.py`)**: 4 modelos locales CPU evaluados en 16 tareas reales (títulos ES, vacantes ES, consultas EN, cruce ES↔EN y evidencia de skills), cada uno con el envoltorio recomendado por su fabricante (raw / `query:`+`passage:` / prompts de EmbeddingGemma).

| modelo | hits | cross | evidencia | margen | encode |
|---|---|---|---|---|---|
| **paraphrase-multilingual-mpnet-base-v2** | **16/16** | **4/4** | **3/3** | **+0.442** | 1289ms |
| intfloat/multilingual-e5-small | 15/16 | 3/4 | 3/3 | +0.082 | 428ms |
| paraphrase-multilingual-MiniLM-L12-v2 | 14/16 | 3/4 | 2/3 | +0.395 | 340ms |
| Alibaba-NLP/gte-multilingual-base | crash del proceso (no completó) | | | | |
| google/embeddinggemma-300m | gated 401 sin `HF_TOKEN` (no evaluable) | | | | |

**Decision**: se mantiene `paraphrase-multilingual-mpnet-base-v2` como fallback. gana en exactitud (perfecto), cruce ES↔EN y margen; `e5-small` es 3.7× más rápido pero pierde un cruce y su margen (+0.082) es tan estrecho que roza el fallo en varias tareas - para indexar cientos de vacantes la diferencia de 1.3s→0.4s es irrelevante vs. arruinar el ranking. Para desbloquear EmbeddingGemma solo hace falta un `HF_TOKEN` en `.env` + aceptar la licencia en huggingface.co (si no, fallback automático y silencioso salvo el warning 401).

**Validado**: `reporte_cv.py` con los 2 CVs de prueba - ES (8 años, universitario, Ingles Avanzado, Espanol Nativo, contacto completo) e EN (`demo_cv_en.txt`: 9 años, senior, `Location:`, `Bachelor's`, Español Nativo + Inglés Avanzado, email/teléfono/LinkedIn); prueba ATS cruzada perfil EN vs vacante EN y perfil ES vs vacante ES (100 Fuerte cuando pide inglés/inglés, 0 Rechazo probable para "Auxiliar de Bodega", `languages_score` 1.0/None/0.0).

**Correcciones tras validar el ciclo completo (cv2profile --save + review)**:
- `_literal_idx` usaba boundaries `(?<![\w\-/])` que rechazaban `JavaScript` en `JavaScript/TypeScript` (sigue `/`) y `github` en `.../github.com/...` (viene `/`) -> falsos avisos de "sin presencia literal". Frontera ahora = no-adyacente a `\w`.
- `_split_phrases` cortaba por `.` a secas: `Node.js` -> `Node` + `js...` y `github.com` -> `github` + `com...`, destruyendo el termino; ademas el min-len 12 descartaba frases cortas utiles (`y Redis.`, `GitHub`). Fix: el punto solo corta si viene seguido de espacio/final (`[.;:!?]+(?=\s|$)`) y min-len 6. Resultado: **19/19 skills con evidencia literal** en el CV ES (antes 16/19 con 3 falsos avisos: JavaScript, Redis, GitHub).


### 28. Ciclo QA end-to-end completo (CV -> scraping -> ranking -> dashboard)

**Objetivo**: validar el flujo entero tal como lo usaria una persona, con el CV ficticio ES como perfil real.

**Flujo ejecutado y verificado**:
1. `cv2profile.py <PDF> --save`: perfil guardado con backup automatico (`data/profile_backup_*.json`, gitignore). Ficha ATS: 8 anios, universitario, senior, Ingles Avanzado + Espanol Nativo, contacto completo.
2. `main.py` (scraping con Brave/CDP): **27 vacantes nuevas** de tematica software (Indeed 141 / LinkedIn 81 / ANE 10 / Procomer 1 en total); reports `report_20261018.csv` y `report_24h_*`. Indeed presenta muro de login en algunas consultas (0 cards) pero obtiene 15/15 descripciones completas de las si visibles; ANE devolvio 0 `.job-listing` en todas las busquedas de esta corrida (posible cambio de layout del sitio - vigilar); Procomer 0 (temporada).
3. `search.py --perfil`: ranking con vacantes de software (Software Engineer Expert AI, Forward Deployed Engineer, Senior .NET Engineer...), linea `ATS nn.n% [veredicto] | combinado nn.n%` + keywords faltantes reales (Teams, Testing, Terraform, C#, .NET).
4. **Dashboard en navegador** (`streamlit run app.py`, puerto 8501): tabla con columnas `Total % / ATS % / Veredicto / Semantico % / ATS skills % / ATS titulo % / Faltan`, encabezado "Mejores opciones para: Juan Perez", sidebar con "Perfil: Juan Perez" + expander "Ficha ATS" + "Modelo". Verificado leyendo el DOM (grid role=gridcell/columnheader) porque el canvas del dataframe no entra en el snapshot de accesibilidad; screenshot bloqueado (ventana no visible).

**Bugs corregidos durante el ciclo**:
- Emparejamiento ES/EN de temas: `Desarrollador Full Stack / Cloud Support Engineer` -> el pool EN se recortaba a top6 y dejaba fuera `Full Stack Developer` (que SI era el mejor match, coseno 0.317 vs 0.245). Fix: rankear cada titulo ES contra TODO `VOCABULARY_EN` (38 terminos) con dedupe. Resultado: `Full Stack Developer`, `Software Engineer`, `Senior Software Engineer`.
- Nota de operacion: el primer intento de `main.py` salio con exit 1 y sin salida (stderr suprimido con `2>$null`); el segundo (con `2>&1`) corrio completo - se desconoce la causa del primero; siempre correr scraping con stderr visible.
- Nota: el dashboard arrancado antes de un cambio en `app.py` sirve codigo viejo hasta recargar/reiniciar; reiniciado limpio (verificado hora de start vs ultima modificacion del archivo).

### 29. Gate de relevancia + ANE por barrido de fecha + perfil con limpieza por defecto

**Decision de diseno**: ANE-loader deja resultados en busquedas manuales pero 0 en las del buscador (incluso a mano), asi que el proveedor ANE se reescribe como **barrido por fecha**: recorre el listado publico `/Puesto?Pagina=N` (orden desc por fecha, 10/pagina) hasta la ventana temporal y descarga el detalle de cada puesto con login programatico (`ANE_USER`/`ANE_PASSWORD` en `.env`). Corre UNA vez por corrida (`search_term="barrido-fecha"`); paginas configurables con env `ANE_MAX_PAGES` (default 15).

**Gate "Fecha Y relevancia"** (`core/gate.py`): filtro aplicado a TODOS los providers - una oferta entra a la tabla curada `jobs` solo si es reciente (ventana `--dias`, default 30) Y supera el umbral semantico (`--score-min`, default 0.30) contra el perfil. Los descartados NO se pierden: van a la tabla staging `jobs_raw` con su `gate_score`/`gate_razon` (re-filtrables con `--refiltrar` sin rescarpear). Migracion de la BD vieja verificada en real (PRAGMA + ALTER, filas preservadas).

**Perfil con limpieza por defecto** (`core/cv_analysis.py` + `cv2profile.py`): `merge_profile` ahora funciona con `replace=True` por defecto - el CV es la fuente de verdad y LIMPIA titulos/skills/temas/nombre del perfil anterior (preserva pesos/pesos_ats/filtros; secciones ausentes del CV no dejan huerfanos). El flag optativo `--merge` mantiene el comportamiento historico (suma sin limpiar) para quien lo quiera. `cv2profile` muestra la evidencia de limpieza (`- titulos (limpieza, no vienen del CV)`) y saca el nombre de la primera linea del CV. Causa raiz del drift historico resuelta: un CV nuevo ya no arrastra temas del CV anterior.

**Bugs corregidos**:
- `detect_contact`: el fallback de telefono sin etiqueta (`+506 8888-8888`) era un regex SIN grupo de captura y `tel.group(1)` lanceaba `IndexError` con cualquier CV que usara ese formato (reproducido con `demo_cv.docx`). Fix: dos regex separados, `group(1)` etiquetado / `group(0)` fallback.
- Emparejamiento ES/EN de temas con frases cortas: mpnet elegia vecinos absurdos (`Analista de Soporte` -> `Content Moderator` coseno 0.336 vs `Technical Support Analyst` 0.248; `Mesa de Ayuda` -> `QA Automation Tester`). Fix: `PARES_ES_EN` mapa curado determinista (gana por defecto), embeddings solo como fallback para titulos fuera del mapa.

**Extraccion bilingue validada con CV real**: version ES y EN del mismo CV producen el mismo nucleo tematico (`Atencion al Cliente Bilingue / Bilingual Customer Service Representative`, `Soporte Tecnico Bilingue / Bilingual IT Support Technician`, `Mesa de Ayuda / Help Desk`) y la misma ficha ATS (experiencia, certificaciones, contacto).

**Privacidad / portabilidad**:
- `.gitignore` con patrones globales `*.pdf`/`*.doc`/`*.docx` (raiz incluida) + `~$*`: el CV real nunca viaja a git; el demo sintetico trackeado no se ve afectado.
- `privacy_check.py`: auditoria pre-push sobre archivos TRACKED (emails reales excluyendo dominios demo, rutas `C:\Users\...`, lista negra opcional `data/privacy_blacklist.txt` gitignored). Exit 1 = no commitear.
- Reset de datos: `jobs.db`/backups/caches/reportes borrados (arranque desde cero); `data/` queda solo con plantilla ejemplo + demo + CVs locales (gitignored).

**Pendiente**: corrida e2e completa con el nuevo pipeline (barrido ANE + gate) para calibrar `min_score` con datos reales; el threshold 0.30 ensayado con frases sinteticas mostro poca discriminacion como puerta binaria (posible senal combinada con titulos/temas).

### 30. Calibracion con datos reales: veto lexico de dominio + refiltrar que reconstruye + fix ANE

**Corrida e2e con perfil real (CV ES)**: 145 crudas (LinkedIn 61 + Indeed 84) -> 71 curadas. ANE aporto 0 por un bug; tras arreglarlo, segunda corrida: 266 crudas -> barrido ANE 120 vacantes (login programatico + 15 paginas en ~2 min). ANE queda como el proveedor #1 del sistema.

**Bugs corregidos**:
- ANE: 6 llamadas usaban `self._limpiar` pero la funcion es de modulo -> `AttributeError` justo despues del login correcto; el barrido aportaba 0 silenciosamente. Fix verificado en real.
- `--refiltrar` solo hacia upsert en `jobs`: lo aprobado con criterios viejos quedaba atrapado en la tabla curada (199 filas de las cuales 128 eran basura). Fix: `clear_curated()` (vacía SOLO `jobs`; `jobs_raw` intacta) y refiltrar reconstruye la curada desde jobs_raw + criterios actuales. Verificado: 199 -> 71.

**Calibracion del gate con datos reales**: la distribution real es sana (masa en 0.4-0.5, solo 5 descartes por score), PERO el barrido por fecha trae oficios cuyas descripciones puntuan 0.30-0.44 (`AYUDANTE DE BODEGA` 0.419, `Ayudante de Cocina` 0.421, `Lavadores de vehiculos` 0.416) y el umbral 0.30 las deja pasar. Experimento con la senal del TITULO por embeddings: peor que la descripcion (`Mecanico Automotriz` title-score 0.371 > su desc-score 0.304; mpnet comprime frases cortas en 0.31-0.46 sin discriminar). La senal que SI separa es lexica -> **tercer criterio del gate: veto lexico de dominio**: el titulo debe contener un token del dominio (soporte|support|help desk|service desk|mesa de ayuda|customer|cliente|informatic|biling|asistencia tecnica|TI|IT|sistemas|redes). Tokens de ROL ("tecnico", "analista", "representante") no cuentan: "Tecnico de Aire Acondicionado" debe caer. Escape: score >= 0.45 pasa sin importar el titulo (protege titulos creativos). Verificado 26/26 en casos etiquetados; en datos reales veto 128 (`Agente de Ventas`, `Supervisor de Proyectos`, `Senior Consultant`, `Remote Recruiter`, `Auxiliar Contable`, `Content Moderator`...).

**?Cambiar de modelo de embedding arreglaria la separacion? No (medido)**. Mini-benchmark replicando la formula exacta de search_profile sobre 85 vacantes reales etiquetadas (39 ruido / 46 valor):

| configuracion | ruido max | valor min | margen |
|---|---|---|---|
| mpnet + prompts produccion | 0.440 | 0.300 | -0.140 (solapa) |
| mpnet texto pelado | 0.371 | 0.296 | -0.075 (solapa) |
| e5-small + prefijos nativos (query:/passage:) | 0.825 | 0.805 | -0.020 (solapa) |

Ningun modelo separa: el problema es estructural (las descripciones de oficios comparten superficie semantica con los empleos de servicio). e5-small comprime todo en 0.79-0.84 (rango operativo ~0.02). EmbeddingGemma sigue sin ser evaluable (gated: licencia + HF_TOKEN). Decision de arquitectura: **hibrido** - los embeddings rankean dentro de lo aprobado (donde mpnet gano el benchmark 16/16 y el cruce ES-EN), el lexico fija el dominio de la puerta.

**Estado final de la corrida**: jobs 71 (solo dominio soporte/atencion al cliente) / jobs_raw 241 con razon de descarte por fila (fecha / relevancia / dominio).

### 31. Validacion del usuario: el ciclo completo funciona con el perfil real

Primer ciclo validado de punta a punta con el CV real del candidato (ES, soporte bilingue). El usuario confirmo sobre el top-10 del ranking (`search.py --perfil`):

- **Relevancia**: SI se aplicaria a las vacantes del top-10 - "esta muy de acuerdo con el perfil" (Bilingual Technical Support Advisor via ANE, Technical Support, Bilingual Interpreter, Analista de Soporte TI, Cognizant On-site Heredia, everis HR Help Desk, Procore...). El barrido ANE aporta el #1 del ranking: la inversion del proveedor paga.
- **Scores**: sensatos para el perfil; la busqueda trae puestos relevantes para el CV.

Conclusion del QA: gate (fecha + relevancia + dominio) y ranking funcionan con datos reales. Los veredictos ATS honestos (mejor 75.3% "Ajustable", sin "Fuerte") reflejan gaps REALES del CV detectados por keywords faltantes (`Teams`, `Help Desk` literal) e "Ingles (no especificado)" - accionable para el candidato: declarar el nivel de ingles y las herramientas usadas; re-extraer con `cv2profile --save` sube los veredictos. El sistema hace su trabajo: dice que le falta al CV para el ATS.

**Pendiente**: automatizar la corrida diaria (Task Scheduler) para que la ventana de 30 dias se mantenga fresca sin ejecucion manual.

### 32. Recordatorio interactivo al iniciar sesion (sin servidor 24h)

**Restriccion real del usuario**: su equipo no esta encendido todo el dia, asi que una tarea programada a hora fija no sirve (la corrida se perderia). Solucion: **el recordatorio se dispara al INICIAR SESION** - cuando el equipo SI esta encendido - y solo pregunta si hace falta.

- `recordatorio.py` (tkinter, `pythonw` para no mostrar consola): lee la ultima corrida real (`MAX(created_at)` de `jobs_raw`); si ya corrio hoy o el usuario descarto el recordatorio hoy, sale en silencio. Si no, dialogo con 3 caminos: **Correr ahora** (lanza `jobpulse.cmd` en su propia ventana) / **Mas tarde...** (pide la hora HH:MM y registra una corrida UNICA con `StartWhenAvailable`: si el equipo estaba apagado a esa hora, corre en cuanto arranque; si la hora ya paso, se programa para manana) / **Hoy no** (marca el descarte del dia en `data/recordatorio_estado.json`, local-only).
- `instalar_recordatorio.ps1`: registra la tarea "JobPulse Recordatorio" al logon del usuario actual (sin admin).
- `jobpulse.cmd` (launcher de un clic): scraping completo + dashboard (reutiliza el que ya este corriendo en 8501 via `Get-NetTCPConnection`, robusto en Windows en espanol) + abre el navegador. Cierre automatico a los 15 s (sin `pause`: las corridas programadas no deben dejar ventanas atascadas).
- `app.py`: banner de frescura - caption "datos frescos" o warning si la ultima corrida tiene >= 24 h.

**Pendiente**: nada - el usuario decide con el dialogo; el sistema nunca corre sin permiso y nunca depende de un equipo 24/7.

### 33. Credenciales por portal: la primera corrida de un usuario nuevo

Revision del estado real de autenticacion por provider (para el objetivo de portabilidad: cualquier persona debe poder arrancar):

- **LinkedIn**: sin credenciales en el codigo - usa el endpoint publico `jobs-guest` + el perfil real de Brave del usuario. Loguearse en LinkedIn dentro de Brave mejora el acceso; sin login funciona degradado.
- **Indeed**: sin credenciales; el sitio muestra muro de login INTERMITENTE en algunas busquedas (observado en la corrida real: unas consultas devuelven "Iniciar sesion" con 0 cards y otras 15/15 con descripciones completas). Es comportamiento del sitio, no configuracion.
- **ANE**: el unico que exige credencial. Ya degradaba bien sin ellas (warning "sin credenciales en .env ni sesion activa en Brave" -> barrido omitido -> los demas portales siguen), pero nadie le decia al usuario nuevo que existe. Ademas tiene atajo: `_asegurar_sesion` detecta sesion activa de ane.cr en el Brave del usuario (enlaces reales a /Puesto/Detalles en el listado) y la reutiliza sin .env.

Cierre del hueco de onboarding:
- `.env.example`: comentarios explicando que ANE es opcional-recomendado, cuenta gratuita en ane.cr, el atajo de la sesion de Brave, y que sin credenciales el resto del sistema funciona.
- README: seccion "Credenciales de los portales (primera corrida)" con la matriz LinkedIn/Indeed/ANE y el resumen "con .env vacio corres LinkedIn+Indeed; ANE se desbloquea con credenciales o sesion en Brave".

**Decision**: no se agregan credenciales de LinkedIn/Indeed al sistema (no existen en el codigo y no hacen falta); ANE queda documentado como unico requisito opcional.

**Pendiente (backlog)**: sistema de primera corrida (`setup` interactivo) que configure el proyecto de punta a punta para un usuario nuevo. Alcance tentativo:
- Deteccion de primera corrida: `profile.json` generico (Juan Perez), `.env` con placeholders, `jobs.db` inexistente.
- Wizard interactivo en consola: CV del candidato (ofrecer `cv2profile --save` alli mismo) o edicion manual minima, ubicacion de busqueda (default: Costa Rica), credenciales ANE opcionales con validacion de login, `HF_TOKEN` opcional, verificacion de venv y Brave.
- Escritura del `.env` con lo capturado + resumen final + corrida de prueba corta (1 pagina) para validar credenciales antes de la corrida completa.
- Integracion con el flujo existente: al terminar, sugerir `instalar_recordatorio.ps1` y `jobpulse.cmd`.
- Reemplazaria la seccion "Credenciales de los portales" del README como primer contacto (el texto queda como referencia).

### 34. Nuevo proveedor: CINDE Jobs (cindejobs.com)

**Objetivo**: agregar el portal de empleo de CINDE (open 24/7, multinacionales instaladas en Costa Rica) como quinta fuente del scraper, sin navegador ni credenciales.

**Descubrimiento del backend**: el sitio es Next.js renderizado con Apollo Client contra una **API GraphQL publica** en `api.cindejobs.com/graphql`. Proceso de hallazgo: el HTML de `/en/job-offers` no contiene vacantes (client-rendered) -> se escanearon los chunks JS de `/_next/static/chunks/*` buscando URLs -> aparecio `https://api.cindejobs.com` y la referencia a los developer tools de Apollo -> POST GraphQL con `__typename` respondio 200 -> la **introspection est abierta**, lo que expuso el esquema completo: `Query.viewJobOffers(search, page, limit, categories, modalities, englishLevels...) -> [ViewJobOffer]` con `jobOfferId, name/nameEs, slug, companySlug/companyName, officeName, description/descriptionEs, technicalSkillsRequired, languagesEs, academicLevelRequiredEs, wfhEs...`.

**Implementacion**:
1. **`providers/cinde.py`** (nuevo, patron de `procomer.py`: solo httpx, cero Playwright e indexable al pipeline sin toques extra):
   - Query GraphQL con variables (`limit: 100`, `page: N`) y loop de paginacion hasta respuesta vacia o tope de paginas.
   - Barrido tipo ANE (`search_term="barrido-todos"`): trae TODO el tablero activo en una pasada; el gate semantico filtra contra el perfil. Razon: el buscador del API es debil en espanol ("ingeniero"/"desarrollador" devuelven 0, "it support"/"soporte" funcionan) y el tablero tiene pocas vacantes (~20 hoy).
   - `job_id="cinde-{jobOfferId}"` con prefijo OBLIGATORIO: `job_id` es PRIMARY KEY global en `jobs`/`jobs_raw` y Procomer/ANE ya usan ids numericos crudos (colision real).
   - Link de detalle construido con el patron publico verificado (HTTP 200): `https://cindejobs.com/en/fair/{companySlug}/{slug}`.
   - Descripcion: version ES si existe, si no EN (los embeddings son multilingues; casi todo el contenido del tablero esta EN).
2. **`main.py`**: bloque 2b despues del barrido de ANE (una sola vez por corrida), tope `CINDE_MAX_PAGES` (10 por defecto, cada pagina hasta 100 ofertas); comentario "los 4 portales" -> 5.
3. **Docs**: README (seccion credenciales: fila "CINDE Jobs - No - nunca pide nada, API GraphQL publica"), `.env.example` (variable opcional documentada), `core/gate.py` (docstring sensible al listado de portales actualizado).

**Trampas del API encontradas**:
- **Sin fecha de publicacion**: el esquema no expone createdAt/updatedAt en `ViewJobOffer` (tablero "abierto 24/7"). `date_published=None`; el gate ya trata fechas no parseables como "beneficio de la duda" (verificado: `es_reciente(None, 1) = True`), asi que la relevancia semantica decide sola. Contrapartida: CINDE nunca se descarta por ventana temporal (aceptable: son pocas y activas).
- **Campos duplicados con el mismo HTML**: `description == descriptionEs == features == technicalSkillsRequired` en la mayoria de vacantes (baseline ~8 KB repetidos 4 veces). Fix: solo se mapea la descripcion (ES||EN) y `features`/`academicLevelRequiredEs`/`languagesEs` van a `_adicional` en `raw_data`; `requirements` queda vacio para no hinchar la DB ni doble-contar keywords ATS.
- **`nameEs` suele venir vacio**: title con fallback `nameEs || name`; `slug` se conserva EN (es el que usa la ruta publica).

**Validado**:
- Proveedor aislado: 19/19 vacantes con titulo, link, empresa, ubicacion y descripcion (ej. "IT Systems Engineer II" - World Fuel - Virtual; "Technical Architect" - HCLTech - Belen Heredia).
- `py_compile` OK en `main.py`, `providers/cinde.py`, `core/gate.py`.
- Gate sin fecha: las vacantes de CINDE pasan el filtro temporal y son rankeadas por score semantico + veto lexico del dominio como cualquier otra fuente.

**Captura**: solo datos publicos del tablero (titulos/empresas/links/descripciones visibles a cualquier visitante); no se tocan datos de usuarios ni endpoints autenticados; 1-2 HTTP requests por corrida total.

**Pendiente**: si el usuario lo quiere, segundo proveedor para practicas (`Query.viewInterships`, mismas args, ya documentado en la introspection).

---

## 📌 NOTA IMPORTANTE (revisar mañana): Workday como fuente candidata

**Contexto**: surgió en conversación (el asistente original con el que se inicio el proyecto sugirió que "las grandes empresas están en Workday", pero nunca documentó el método). Investigado y **verificado en vivo** hoy; falta decidir si se integra como proveedor #6.

**El método (CXS API de Workday)**: los portales de `*.myworkdayjobs.com` exponen una API JSON publica y estable (es la misma que usa el navegador):

```
POST https://{tenant}.wd{n}.myworkdayjobs.com/wday/cxs/{tenant}/{Sitio}/jobs
Body: {"appliedFacets": {}, "limit": 10, "offset": 0, "searchText": "Costa Rica"}
```

- Respuesta JSON: `total` + `jobPostings[]` (titulo, `locationsText`, `postedOn` "Posted 2 Days Ago", `externalPath` → link de la vacante). Paginacion = incrementar `offset`.
- Como localizar el tenant: el sitio de carreras enlaza a `{tenant}.wd{n}.myworkdayjobs.com/{Sitio}`; o googlear `site:myworkdayjobs.com {empresa}`.
- Ventajas vs los agregadores actuales: sin muros de login ni 403 (estilo Procomer/CINDE), portal PROPIO de cada empresa, **fechas de publicacion exactas** (parseables a ISO con `core/dates.py`; algo que CINDE no da).

**Verificado hoy (datos reales)**:
- ✅ `intel.wd1.myworkdayjobs.com/External` (Intel CR): POST CXS con `searchText: "Costa Rica"` devolvio 200 JSON con 5 vacantes (SoC Logic Design Engineer, IT Intern...).
- ✅ `syniverse.wd1.../SyniverseCareers` (TI CR), `confluentmedical.wd503.../Confluent_Medical` (83 vacantes en Alajuela), `smithnephew.wd5.../External` (Alajuela) — listados encontrados via busqueda web.
- ❌ **El consejo original tenia el ATS equivocado en ejemplos clave**: P&G (`pgcareers.com`) y Thermo Fisher (`jobs.thermofisher.com/global/en`) usan **Phenom People**, NO Workday; Bridgestone publica en `bebridgestone.com` (portal propio, 403 por Cloudflare contra HTTP crudo). Leccion: verificar el ATS real de cada empresa antes de asumir.

**Decision pendiente**: agregar `providers/workday.py` con lista curada de tenants con presencia en CR (Intel, Syniverse, Confluent Medical, Smith+Nephew como base; el usuario puede ir sumando). El patron ya existe (`procomer.py`): query paginada a JSON, `job_id = "workday-{...}"`, fecha ISO, link directo. Mucho mas rapido y estable que empleado navigator ( scrappeo de UI): si CXS deja de responder, fallback al HTML renderizado.

---

## 26. Barrido de empresas candidatas + deteccion de ATS Workday (2026-10-19)

**Objetivo**: localizar empresas (medianas/grandes) que publican puestos fuera de LinkedIn/Indeed, empezando por ATS Workday.

**Herramientas nuevas**:
- `barrido_empresas.py`: barrido de 4 fuentes en una corrida (`--no-ane` sin navegador, `--solo ane`, `--solo merge`):
  1. **CINDE** GraphQL `viewCompanies(limit: 1000)` (196 fichas) + `viewJobOffers` (tope real: 16/pag; limit alto devuelve pag 1 con pocos y resto vacias).
  2. **Procomer** `jobs-available` (433 vacantes, 28 pag): la lista NO trae nombre de organizacion (solo id); se resuelve con 1 GET por org_id unico a `/api/candidate/organizations/{id}` (responde 2xx, tipicamente 201) -> `organization.name` + `legal_name`.
  3. **Zonas Francas** procomer.com/zonas-franca/ (server-rendered, 104 entradas; `?paged=` no pagina).
  4. **ANE** listado `/Puesto?Pagina=N` (150 pags, ~1s/pag) solo tomando `h4.job-listing-company` de cada tarjeta (sin abrir detalles): 444 empresas.
- `workday_cr.py`: sondea la CXS API de un directorio publico de tableros Workday
  (kalebconfer-sys.github.io/job-board-directory -> `workday.csv`, 1362 tableros
  validados, check 2026-10-17; columnas tenant/api_url/roles).
  POST `{api_url}` con `searchText: "Costa Rica"`, limit 20; un hit solo cuenta si
  `locationsText|title` del resultado contiene "costa rica" (evita textos que solo
  MENCIONAN el pais). SQLite no interviene; salida JSON de hits/fallos.
- El merge normaliza nombres (sin acentos, quita sufijos legales), agrupa por
  `razon_social` (Procomer legal_name casa con los nombres legales del directorio ZF),
  clasifica tamano con semillas de multinacionales + conteo de vacantes observadas,
  y marca tipo `recruiter`/`confidencial`.

**Resultado**: `data/empresas/empresas_candidatas.csv` (782 empresas; 25 grandes,
98 mediana?, 645 por_evaluar; recruiter 4, confidencial 1). **51 empresas con
Workday confirmado** y vacantes activas en CR: HPE 20, TD Synnex 20, Edwards 18,
Thermo Fisher 17 (usa Workday y Phenom a la vez), Abbott 16, Citi 16, J&J 15,
Stryker 14, Huntsman 11, Four Seasons 11, Roche 10 (retry http1), Unisys 10...
39 de ellas NO estaban en ningun barrido previo (incl. Intel, Metropolis...).

**Fallos Workday**: 44 iniciales de los 1362 (37 "error" = HTTP/2/timeouts ->
reintento secuencial http1.1 recupera Roche(10) e ING(1); 7 persistentes:
403 x3 (bloqueo del host), 429 x2 (salesforce, rate limit), 422 x2).

**Pendiente**: `providers/workday.py` (el patron CXS ya esta en workday_cr.py);
la lista esta como CSV para elegir entreprises; otros ATS del mismo directorio
(el sitio publica "one file for every platform at once": Phenom/Greenhouse/...).

## 27. Proveedor Workday (providers/workday.py) (2026-10-19)

**Implementacion** (sigue el patron CINDE: barrido unico, gate decide):
- Lista curada de 51 tenants con CR (data/empresas/workday_cr.json, regenerable con
  `workday_cr.py`; fallback constante embebido si el json falta).
- Listado: POST `{api}/jobs` con `searchText: "Costa Rica"`, pag de 20
  (WORKDAY_MAX_PAGES por tenant, default 3); se guarda solo externalPath donde
  `locationsText|title` contiene "costa rica".
- Detalle (la lista no trae descripcion): GET `{api}/job/{externalPath tras /job/}`
  -> `jobPostingInfo.jobDescription` (HTML limpiado con BS4), `startDate` (fecha
  exacta ISO; fallback: `postedOn` "Posted 7 Days Ago" via core/dates), `externalUrl`.
- job_id estable: `workday-{jobPostingId}` del detalle; fallback `workday-{JRxxxxxx}`
  del path y por ultimo hash del externalPath.

**Validado en vivo** (1 pag por tenant): 299 vacantes CR de 51 tenants --
298 con descripcion (6915-7722 chars en HPE), 299 con fecha ISO exacta. Lento
pero estable (~4.5 min), sin navegador ni login, sleeps de cortesia (0.15s).

**Integracion**: `main.py` barrido "2c" (WORKDAY_MAX_PAGES, default 3);
`app.py` SOURCES += workday (y se agrego cinde que faltaba). Fechas viejas
(el listado trae vacantes de hace meses) quedan fuera de la ventana del gate -- correcto:
el gate filtra por fecha de verdad con este proveedor (algo que CINDE no da).

**Leccion**: el `total` de la CXS satura a 2000 y sirve paginas mas alla del fin
(no paginar a ciegas; el break `len(items) < PAGE_SIZE` + tope de paginas lo evita).


## 28. Barrido multi-ATS (Greenhouse, Ashby, Workable, Lever, Recruitee, Personio, Rippling) (2026-10-19)

**Objetivo**: extender la cobertura mas alla de Workday, usando el mismo directorio
publico con las direcciones reales de los tableros (12,235 en total; 10,873 sin
contar los 1362 Workday ya cubiertos).

**Fuente**: `data/empresas/ats_dir.csv` = `job-board-directory.csv`
(kalebconfer-sys.github.io/job-board-directory). Filas: platform, token, company,
board_url, api_url, open_roles, region. Greenhouse 3576, Ashby 2486, Workable
1875, Recruitee 1043, Personio 866, Rippling 864, Lever 163.

**Herramienta**: `barrido_ats_otros.py` (--muestra, --plataforma, --workers).
Para cada tablero hace GET del api_url y filtra vacantes con "Costa Rica" en
campos de ubicacion/titulo. Extractores generales (jobs/postings/offers) mas
casos especiales:
- Personio: sirve XML `<workzag-jobs>` con nodos `<position>/<name>/<office>`
  (no JSON); se parsea a mano con regex.
- Lever: el titulo viene en campo `text` y el link en `applyUrl` (no `title`/url).
- Recruitee: lista con `locationText`.

**Resultados** (pasada 1 con 14 workers + pasada 2 con throttle 0.5s para los
~1800 que cayeron por 429): **67 tableros con vacantes CR activas** -> 65 empresas
nuevas en el CSV maestro. Fallos restantes 249 (404 tableros muertos, 302, 403).

**Hallazgos relevantes**:
- Empresas de producto con presencia/remoto CR via Greenhouse: Smartsheet (7
  "Remote Eligible - Costa Rica"), Zuora (2), Fivetran, Nayya, Storyblok, Torq.
- Lever: Westernacher (4), Sysdig. Ashby: Mindvalley, JBS-dev, Owner...
- Workable/Lever/Recruitee estan dominados por firmas de STAFF REMOTO LatAm
  (Pavago 230, Uptalent 53, Lago 22, cynet, Zipdev, Valatam, SupportYourApp,
  Workana Premium...), no empleadores directos: sus vacantes CR solo sirven si
  el gate las filtra bien. Muchas citan "Costa Rica" como mercado de contratacion
  remota.

**Integracion**: `barrido_empresas.py --solo merge` ahora tambien consume
`data/empresas/otros_ats_cr.json` (nuevo bloque "OTROS ATS", con `ats_token`
tipo `platform::token` y `vacantes_otros_ats_cr`). CSV maestro: 847 empresas
unicas (grandes 25, mediana? 109, por_evaluar 694, recruiter 4, confidencial 1).

**Pendientes (siguiente fase)**:
- Phenom People e iCIMS/SuccessFactors NO estan en el directorio (y son los ATS
  de las grandes con sede CR): caza manual de tenants por empresa.
- Greenhouse/Lever valen un provider propio si el gate saca hits (patron
  providers/workday.py). Candidatas concretas: Smartsheet, Zuora, Westernacher.
- Renombrar "Cnx" (posible Concentrix) y el barrido Procomer completo siguen
  pendientes de decision.


## 29. Proveedor Phenom People (providers/phenom.py) + caza de ATS grandes (2026-10-19)

**Objetivo**: las multinationales con sede CR que NO usan Workday (P&G,
Zimmer Biomet, Cisco, Fiserv, UPS, Snowflake...) usan Phenom People, iCIMS,
SuccessFactors o Taleo, que NO estan en el directorio publico de tableros. Se
implemento caza por huella frente a las paginas de carreras.

**Caza** (`caza_ats_grandes.py`): lista curada de ~40 empleadores grandes con
URLs candidatas; huella por marcadores en el HTML (phenompeople/
CareerConnectResources/csrfToken, icims, successfactors, taleo,
smartrecruiters, myworkday...). Resultado en `data/empresas/
ats_grandes_huellas.json`:
- Phenom: Cisco, Fiserv, UPS, P&G, Snowflake, Zimmer Biomet, Thermo, HPE.
- SmartRecruiters: Western Digital, Bosch. SuccessFactors: Microsoft, EY,
  Foundever, Kuehne+Nagel. iCIMS: DocuSign. Workday extra: Rockwell, BD.
- Sin firma (403 o front custom): Amazon (amazon.jobs propio), Dell, Autodesk,
  Oracle, J&J, IBM, Deloitte, Salesforce, Walmart, Instacart, Bimbo, NCR,
  KPMG-atento etc.

**Descubrimiento del patron Phenom "canvas" (SSR)**, capturado con el
navegador contra jobs.thermofisher.com:
- El listado SSR `/global/en/search-results?keywords=Costa%20Rica&from=N`
  embebe un JSON `"jobs":[...]` (10 por pagina) con title/multi_location/
  reqId/applyUrl/postedDate y `"totalHits":N`. Paginacion por `from=0,10,20...`
  (break al recibir < 10 items nuevos o al alcanzar totalHits).
- El detalle SSR `{locale}/job/{reqId}/{slug}` trae `"description"` HTML
  (`raw_decode`) y `"postedDate"` ISO exacto. Sin cookies ni tokens: el
  x-csrf-token solo lo exigen los POST /widgets del front SPA.
- El locale-path (global/en, us/en, cr/en...) se detecta del propio home
  (P&G publica hasta sites por-pais: /cr/en) y se calan candidatos fijos.

**Cliente**: `phenom.py` (PhenomSite: descubrir/pagina/buscar; tiene_cr en
multi_location + lugar_txt). El POST /widgets con ddoKey searchJobs necesita
csrf ligado a la sesion del navegador -> falla desde httpx: quedan pendientes
por esa via Cisco/Fiserv/UPS/Snowflake (su SSR devuelve jobs vacio).

**Provider**: `providers/phenom.py` (#7), mismo patron que workday.py:
digiere `data/empresas/phenom_cr.json` (via sondeo_phenom.py) con lista
fallback constante; keywords CR por sitio (PHENOM_MAX_PAGES, default 3) y
detalle completo de cada CR. HPE/Thermo se SALTAN por solapamiento con
Workday (su apply real va a su propio Workday, ya cubierto). Integrado en
`main.py` (barrido "2d", PHENOM_MAX_PAGES) y `app.py` (SOURCES += phenom).

**Validado**: smoke test con P&G (5 vacantes CR: Consumer Relations
bilingues) y Zimmer Biomet (5 CR: Grecia, Alajuela - tecnicos); desc de
3-16k chars, postedDate ISO; HPE/Thermo correctamente excluidas.


## 32. Proveedor TP Umbraco (providers/tp.py) (2026-10-19)

**Objetivo**: cubrir Teleperformance, el mayor empleador pendiente de la ronda
de relevancia (seccion 31). Su board CR real no es `www.jobs-tp.cr` (formulario
WP sin vacantes) sino la **API Umbraco de tp.com**:

    GET /Umbraco/Api/Careers/GetCareersBase?node=1780&country=Costa%20Rica
        &culture=en-us&pageSize=50&page=0&accumulated=true
    -> {resultado: [{title, description(HTML), url, nodeId, country, location,
        category, language, opportunityType, opportunityModel, workFromHome,
        source(Umbraco|icims), date}], countFirst}

Descubierto leyendo `scripts/modules/careersFilter.js` (el front de
tp.com). Trampita: el `node` correcto es el 2do argumento del constructor
`careersFilter` en el HTML de `/en-us/careers/job-opportunities/` (el 1ro es un
hashCode que cambia y devuelve 0 resultados).

**Provider** (`providers/tp.py`, sigue el patron phenom.py):
- Lista curada `data/empresas/tp_umbraco_sites.json` con fallback constante
  (`("Teleperformance Costa Rica", 1780, "en-us")`).
- Paginado 50/pag (TP_MAX_PAGES default 2), dedupe por `nodeId` intra-pagina
  (bug dedupe corregido en smoke test: el mismo posting aparece duplicado por
  cultura `/es-cr/` y `/`).
- job_id `tp-{nodeId}`; link absoluto; si `source=icims` se agrega
  `?mode=job&iis=Teleperformance.com`; descripcion completa del listing
  (limpiada con BS4) + extras entre corchetes; `date` ISO directly.
- Sin detalle extra: el listing trae TODO (ni siquiera abre la pagina de apply).

**Integracion**: `main.py` barrido "2e" (TP_MAX_PAGES default 2); `app.py`
SOURCES += "tp". README: fila de TOC + estructura.

**Validado en vivo**: 1 vacante CR (Representante de atencion al cliente,
evergreen 2021-06-29, desc completa con requisitos). Las evergreen con fechas
viejas las descarta el gate por ventana (comportamiento correcto); los roles
frescos de TP entran cuando tp.com integre sus requisiciones iCIMS (source
`icims`, fechas nuevas).


## 33. Proveedor Greenhouse (providers/greenhouse.py) (2026-10-19)

**Objetivo**: cubrir los tableros Greenhouse confirmados con CR por el barrido
multi-ATS (seccion 28): Smartsheet (7 CR), Zuora (2) y compania, con vacantes
de calidad recurrentes que ningun portal cubria.

**API board publica** (documentada por el vendor):
    GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true
    -> {meta: {total}, jobs: [{id, title, absolute_url, location{name},
        offices, departments, first_published ISO, updated_at, content(HTML),
        requisition_id, company_name}]}

TOMO el catalogo COMPLETO en 1 GET (con content=true el HTML de la descripcion
viene inline; sin `content` solo titulos).

**Provider** (patron tp.py):
- Tokens de `data/empresas/otros_ats_cr.json` (filas platform=greenhouse);
  fallback constante con las confirmadas. Alias de compania para tokens
  anonimos (`12jlkfsk` = Orion Global Solutions, hallado en la propia
  descripcion de sus vacantes; `thinkacademyus` = Think Academy US).
- Filtro CR con regex `\bcosta\s*rica\b` sobre titulo+location+offices.
- `first_published` como fecha (NO `updated_at`, que es last-edit).
- Trampa HTML: `content` viene doblemente escapado (JSON + entidades:
  `&lt;p&gt;`) -> html.unescape antes de BS4.

**Integracion**: `main.py` barrido "2f" (GREENHOUSE_MAX_PAGES default 1; el
parametro `pages` probaria reintentos, rara vez necesario); `app.py`
SOURCES += "greenhouse". README: fila de portales + estructura.

**Validado en vivo (smoke test)**: 8 tableros -> 16 vacantes CR: Smartsheet 7
(+ una publicada HOY 2026-10-19, Content Designer II), Zuora 2 (incl. una de
2026-10-14), Orion Global Solutions 2, Fivetran 1 (CRI - Remote), Nayya,
Storyblok, Think Academy US, Torq. Todas con desc completa y fecha ISO
first_published; las mas viejas (Smartsheet 2022, Think Academy 2026-04) las
decidira el gate por ventana.


## 34. Proveedor iCIMS (providers/icims.py) (2026-10-19)

**Objetivo**: cubrir los boards iCIMS confirmados (Teleperformance USA/global,
DocuSign por region). Patron decodificado con probes:

- Listado: `GET https://{host}/jobs/search?search=true&in_iframe=1` -> HTML
  server-side con `<li class="iCIMS_JobCardItem">` por vacante: ubicacion
  ("MY-Kuala Lumpur-..."), post date en `title="M/D/YYYY H:MM AM"` y titulo en
  `/jobs/{id}/{slug}/job?in_iframe=1` (class iCIMS_Anchor + h3).
- Paginacion: **`&pr=N`** (pr=1,2,...: 50 nuevos por pag). Trampas pagadas:
  `&pageNum=N` NO pagina (devuelve siempre la misma); el form lleva
  `hashed=` POST (anti- forged) y el filtro keyword por GET
  (`&in_0=`/`&searchKeyword=`) no filtra -> se barre paginado y se filtra
  "costa rica" client-side (filas trae ubic + titulo).
- Detalle: GET del href -> descripcion (iCIMS_JobDescription) + fecha ISO via
  `datetime.strptime("%m/%d/%Y %I:%M %p")`.

**Estado CR (2026-10-19)**: **0 vacantes CR hoy**. Barre comprobado: board
base TP se agota en ~56 (sin CR en sus paginas), el global EN trae ~650 sin CR
en 9 pags, TP-USA puro US, DocuSign root/us/emea/latam responden 200 PERO sin
filas (front migrado a SPA propia, quedo el legacy). TP CR publica via tp.com
(provider tp.py ya lo cubre) y DocuSign cuando vuelva a publicar LatAm se
cacharia automaticamente. Provider queda integrado y barato (~12 req/corrida):
`main.py` barrido "2g" (ICIMS_MAX_PAGES default 8), `app.py` SOURCES += icims;
boards editables en `data/empresas/icims_boards.json`.


## 35. Proveedores SmartRecruiters (providers/smartrecruiters.py) y SuccessFactors (providers/successfactors.py) (2026-10-19)

### SmartRecruiters (#11)

API publica documentada del vendor, sin key:

- `GET api.smartrecruiters.com/v1/companies/{cid}/postings?q=Costa Rica&limit=100&offset=0`
  -> `{"totalFound": N, "content": [...]}` con `releasedDate` ISO, `location`
  (city/region/country/address), `postingUrl`, `function`, `typeOfEmployment`.
- El `q` busca en TODOS los datos del posting (encontro la CR de Bosch aunque
  el titulo no menciona el pais).
- Detalle: `.../postings/{id}` -> `jobAd.sections` = {titulo: {title,
  text(HTML)}}; se ensambla la desc con BS4.
- Trampa: ids de compania case-sensitive y con formato exacto: `westerndigital`
  (335 global) y `BoschGroup` (5,083 global) funcionan; `western-digital`,
  `bosch`, `bosch-group`, `BshGroup` dan 200 con total=0.

**Estado CR**: Bosch 1 (Senior SAP Commerce/Hybris Developer, Heredia,
2026-08-13); Western Digital 0 hoy (aparecio en el barrido del directorio,
rotara). Config: `data/empresas/smartrecruiters_companies.json`. Integrado
`main.py` "2h" (SR_MAX_PAGES default 10), `app.py` SOURCES += smartrecruiters.

### SuccessFactors (#12)

Los portales SAP SuccessFactors "Career Section" (stack j2w) exponen **feed
RSS publico sin key ni cookies**:

- `GET {base}/services/rss/job/?locale=en_US&keywords=(Costa Rica)` -> XML
  con `<item>`: title (incluye ubicacion), link `/job/{slug}/{id}/`,
  pubDate RFC 2822 y description CDATA con el HTML completo de la vacante
  (3-20 KB) -> NO hace falta GET de detalle.
- Trampa: el `keywords` NO es estricto (KN trae Santo Domingo DO, EY trae
  Buenos Aires) -> filtro client (`costa rica|barreal|heredia|san jose|
  cartago|alajuela|(cr)`) sobre title+link+cola de desc.
- Microsoft (jobs.careers.microsoft.com) NO es j2w: SPA con su API
  GCSServices que hoy responde 404 -> queda en bloqueados/front propio.

**Validado en vivo (smoke test)**: **13 vacantes CR** — Foundever 5 (Customer
Service Reps On-Site Moravia/San Pedro/Heredia + Back Office, sept-oct 2026),
Kuehne+Nagel 4 (Global Services Air Logistics/AI Solutions Developer, Barreal
de Heredia, sept-oct 2026), EY 4 (San Jose: Transfer pricing intern,
Pasantia Auditoria, Ingeniero y Staff en Telecomunicaciones, sep-oct 2026).
Mejor yield fresco junto a Greenhouse. Integrado `main.py` "2i",
`app.py` SOURCES += successfactors; sites editables en
`data/empresas/successfactors_sites.json`.


## 36. Proveedor Rippling (providers/rippling.py) + scans masivos Rippling/Personio (2026-10-19)

### Scans del directorio (8 plataformas que faltaban)

El directorio de kalebconfer-sys today cubre **8 plataformas**: workday (1,362),
greenhouse (3,576), ashby (2,486), lever (163), workable (1,875), recruitee
(1,043), **rippling (864)** y **personio (866)**. El barrido multi-ATS previo
(otros_ats_cr.json) ya habia cubierto greenhouse/ashby/lever/workable/recruitee
con CR; quedaban por hacer recuento los ultimos dos:

- **Personio** (866 tenants, patron publico `{tenant}.jobs.personio.de/xml`):
  scan completo con 16 concurrencia -> **0 vacantes CR** (50s, 0 errores).
  Resultado: `data/empresas/personio_scan_cr.json`.
- **Rippling** (864 boards): scan completo HTML -> 10 tableros con "Costa
  Rica" en el texto; al filtrar de nuevo por REGISTRO (title+locations del
  mismo jobPost, no texto libre del HTML), **8 tableros con CR real ->
  43 vacantes**. Guardado en `data/empresas/rippling_scan_cr.json`.

### Patron Rippling decodificado con pruebas y browser

1. Los boards `ats.rippling.com/{slug}/jobs` son Next.js con el listado
   **SSR en el HTML** (sin XHR visible en el browser: los puestos vienen
   serializados). Cada jobPost embebe:
   `"title":"...","url":".../{uuid}","department":{"name":...},"locations":
   [{"name":"Costa Rica","countryCode":"CR",...}]`.
2. Trampa pagada: el HTML tambien trae listas de countries del dropdown
   (Rippling dice que hay "Costa Rica" remoto para el employer) -> filtro
   SOLO por `countryCode == "CR"` dentro del blob del mismo registro.
3. Detalle: `/{slug}/jobs/{uuid}` -> `__NEXT_DATA__` ->
   `apiData.jobPost` con `description.{company, role}` (HTML 2-9 KB),
   `createdOn` (ISO TZ), `employmentType.label`, `workLocations` ->
   BS4 limpia la desc.

### Provider

`providers/rippling.py` con boards en `data/empresas/rippling_boards.json`
(8 activos con CR real). Smoke test: **43 vacantes CR** exactas al scan:
Aubrant Digital 15 (Escazu/remoto LATAM), CommandLink 8 (CS/Billing/NetOps/
SWE), Sparq 8 (Senior Devs), Lisinski Law Firm 6 (Salesforce/QA), Pythian 2
(DevOps/AI-ML Lead), Venn 2 (RevOps), Havenly 1, InAir 1. Integrado
`main.py` barrido "2j" (RIPPLING_MAX_PAGES), `app.py` SOURCES += rippling.

### Sutherland (SeekOut CareerCompass) - pausado

El front de Sutherland (jobs.sutherlandglobal.com) es un site **Duda
estatico** (sin XHR de vacantes); el widget de empleo no se monta sin
interaccion del usuario. La huella era SmartRecruiters (chat winstonchat) y
el company id `sutherland` en la API tiene 404 postings globales y 0 CR.
Queda en bloqueados (front propio), igual que Microsoft.


## 37. Proveedor ATS ligeros (providers/ats_ligeros.py): Ashby/Lever/Recruitee/Workable (2026-10-19)

Del directorio: plataformas ashby (2,486), lever (163), recruitee (1,043),
workable (1,875) ya barriereadas con CR en `otros_ats_cr.json` (67 tableros
incluida workable 46). Faltaba el proveedor que las consuma en la corrida.

### Endpoints publicos (sin key)

- **Ashby**: `GET api.ashbyhq.com/posting-api/job-board/{tok}` ->
  `{"jobs":[{id,title,location,secondaryLocations,publishedAt (ISO),
  jobUrl,descriptionHtml,descriptionPlain}]}`. Filtro CR en
  location/secondary/title (CR-as-multi-loc detectado en MindValley: primary
  Brazil, CR en secundarias -> etiqueta "Costa Rica (multi-loc)").
- **Lever**: `GET api.lever.co/v0/postings/{tok}?mode=json` (los boards de
  jobs.eu.lever.co usan `api.eu.lever.co`) -> lista [{text, country ("CR")
  o text/hosts, createdAt epoch, hostedUrl, description(tx)}].
- **Recruitee**: `GET {biz}.recruitee.com/api/offers/` ->
  `{"offers":[{position, location, published_at "YYYY-MM-DD..." UTC,
  careers_url, description/requirements}]}` (ojo: `position` es el titulo,
  a veces trae int -> cast str).
- **Workable**: POST `apply.workable.com/api/v1/accounts/{tok}/jobs` con
  header `X-Workable-Shortcode: {tok}` (el shortcode en body JSON da
  "Not allowed"); paginado de 10/pag con el cursor `nextPage` (header
  `X-Workable-Page`); detail GET `.../jobs/{shortcode}` trae description
  (5-10 KB HTML).

### Validado en vivo (smoke test)

**Ashby 10 + Lever 6 + Recruitee 9 = 25 vacantes CR** con fechas frescas
(ashby-JBS Senior PM/AI-ML dev oct 2026, Owner CS Representative nov 3 2026,
MindValley sept 24, etc.). 

### Trampa Workable: 429 global por IP

La API de Workable aplica **throttle global por IP** (no per-board): la
primera corrida completa (huzzle/etc) dio 166 CR, pero con 46 tableros
rapido llego al 429 y luego responde 200 con results vacios. El provider
maneja:
1. cooldown 120s + 1 retry; si persiste -> `self._wk_paused` y **se salta
   el resto de workable** en la corrida (no sigue castigando el IP);
2. sleep 1.2s entre tableros;
3. cadencia de produccion (1 corrida/dia) respeta el throttle.
El HTML publico del board es una SPA vacia (5 KB) -> no hay ruta alternativa
sin la API.

### Personio: scan completo -> 0 CR

`{tenant}.jobs.personio.de/xml` (XML publico con todas las posiciones):
866 tenants escaneados en 50s -> **0 menciones Costa Rica**. Guardado en
`data/empresas/personio_scan_cr.json`. Personio queda sin proveedor (nada
que dar en CR).

Integrado `main.py` barrido "2k" (AL_MAX_PAGES default 6), `app.py` SOURCES
+= ats_ligeros.


## 38. Reorganizacion: providers/boards y providers/directos + dedupe cross-source (2026-10-19)

**Razon del pedido del usuario**: "lo mas probable es que algunos de los
puestos se dupliquen en los job boards" — el mismo job posting se publica
en LinkedIn/Indeed/ANE (agregador) y en el ATS propio de la empresa
(Workday, Greenhouse, Rippling...). Regla confirmada: **prioridad a los
providers directos; si el puesto ya esta en ellos, se descarta la copia de
los job boards**.

### Cambios de codigo

1. **Subpaquetes**: `providers/boards/` (indeed, linkedin, ane, procomer,
   cinde) y `providers/directos/` (workday, phenom, tp, greenhouse, icims,
   smartrecruiters, successfactors, rippling, ats_ligeros). `providers/
   base.py` queda en la raiz (core/ sigue importandolo igual).
2. **`providers/source_groups.py`**: mapeo canonical `source -> boards|
   directos` (`GROUP_BOARDS`, `GROUP_DIRECTOS`, `source_group()`); fallback
   de sources desconocidos = "boards".
3. **`core/dedupe.py`**: `dedupe_boards_vs_directos()` — registra las keys
   de TODO lo directo primero y descarta los boards que ya existen con
   matching conservador:
   - normalizacion NFKD→ASCII, lowercase, tokens orden-INSENSITIVE ("Engineer,
     Senior" == "Senior Software Engineer");
   - ruido de ubicacion/remoto fuera del matching (los agregadores taggean
     "Costa Rica"/"Remote" en el titulo);
   - company sin sufijos legales ni tokens <=2 ("Kuehne+Nagel S.A." ==
     "Kuehne + Nagel (AG & Co.) KG");
   - matching tambien SIN ubicacion cuando una fuente no la trae;
   - NUNCA deduplica intra-grupo (eso lo cubre el PK job_id por fuente).
   Test con casos reales (Workday K+N vs LinkedIn K+N, indeed reordenado,
   Huzzle board vs ATS directos): descarta los 2 duplicados exactos,
   conserva variantes legítimas.
4. **Hook en `main.py`** (paso "2z", antes del gate): `dedupe_boards_vs_
   directos(raw_offers)` con log "N copias de job boards descartadas por
   existir ya en providers directos".
5. Imports actualizados en `main.py` (top-level + inner 2c-2k),
   `barrido_empresas.py` (providers.boards.ane), `search.py` (SOURCES
   ampliada a las 14 fuentes) y `app.py` (BOARDS_SOURCES + DIRECTOS_SOURCES
   separados; SOURCES = union para el dropdown).

Todo compila e importa (py_compile + `import main`).


## 30. Caza ATS ronda 2 (URLs alternativas + navegador) (2026-10-19)

**Objetivo**: resolver las ~25 empresas grandes que en ronda 1 quedaron
"sin firma" (403/404/DNS) o con 200 sin marcadores.

**Herramienta**: `caza_ats_grandes.py` actualizado (ronda 2) con paths
alternativos: `/us/en/search-results` (patron Phenom SSR), `/c/en/search-results`
(patron iCIMS), endpoints myworkday directos; y probe con **navegador** para
los sites que bloquean httpx (huella sobre el HTML final renderizado).

**Nuevos confirmados (validados con API real)**:
- **J&J = Workday** (tenant `jj.wd5/JJ`): CXS ok, 5+ vacantes CR (Grecia,
  Alajuela: Finance Senior Supervisor, Produccion/Ortopedia, Facilities, Mfg
  Engineer B, IT Analyst). Ya estaba en workday_cr.json / TENANT_FALLBACK.
- **Intel = Workday** (`intel.wd1/External`): CXS ok, CR San Jose (Student
  Intern Collaboration, IT Intern, SoC Logic Design). Ya integrado.
- **Concentrix = Workday**: el portal real es `jobs.concentrix.com`
  (`jobs.concentrixcareers.com` DNS MUERTO) y agrega vacantes de
  `cnx.wd1.myworkdayjobs.com/external_global`. **RESUELVE el misterio del
  token "cnx"** del CSV maestro: es Concentrix (vendor, 1680 roles), NO CNX
  Resources - y el barrido Workday ya trae sus CR (2). Renombrar en el CSV
  de `cnx` a `Concentrix (tenant workday cnx/external_global)`.
- **Kimberly-Clark, Medtronic, Autodesk**: sus tenant (wd1/kimberlyclark/GLOBAL,
  wd1/medtronic/MedtronicCareers, wd1/autodesk/Ext) ya estaban en
  workday_cr.json; el 403/406 de la caza era pura proteccion del careers-site.
  CUBIERTAS por el provider Workday.
- **Nuevas firmas**: DHL = Phenom (careers.dhl.com/global/en/search-results,
  SSR ok); PayPal = Phenom (careers.pypl.com; SSR 403 por httpx - Cloudflare -
  pendiente variante); Teleperformance = iCIMS USA
  (careers-teleperformance.icims.com) + Phenom por pais (jobs.teleperformance.pt).

**ATS nuevos descubiertos (sin provider)**:
- Dell = **Oracle Recruiting Cloud** (`enterpriseplatform.dell.com/hcmUI/
  CandidateExperience/`). Primera firma ORC del proyecto.
- Sutherland = **SeekOut CareerCompass** (careercompass.seekout.io). ATS nuevo.
- Alorica = funnel propio por pais (`alorica.com/careers` ->
  `alorica-hire.bling-ai.com` + `recruit-latam.alorica.com`); no CR visible.

**Siguen bloqueados/pending**:
- Nestle: interstitial Cloudflare ("Un momento...") incluso en navegador.
- IBM/Deloitte/Koch/CC-FEMSA/Instacart/Walmart/Broadcom/Salesforce: 200 sin
  firma (SPA propia o front custom); requieren caza de API propia site por site.
- **Taleo: sigue con 0 empresas confirmadas** (huella .ftl sin hits en 2 rondas).
- Dominios muertos (DNS, 2 rondas): jobs.concentrixcareers.com,
  jobs.grupobimbo.com, jobs.teleperformance.com, careers.ncrvoyix.com;
  medtronic.wd5 (el real es wd1).

**Respaldo**: `data/empresas/caza_ronda2_navegador.json` (hallazgos del probe
con navegador + validaciones CXS/Phenom). PayPal agregado a `phenom_cr.json`
como `pendiente-SSR-403`.

**Pendientes de decision**: provider iCIMS (DocuSign/TP); provider
Oracle-Recruiting (Dell) y SeekOut (Sutherland); re-probar PayPal Phenom con
headers de browser; variante Phenom /widgets para Cisco/Fiserv/UPS/Snowflake;
renombrar `cnx` -> Concentrix en el CSV maestro.


## 31. Ronda 3 por relevancia (empleos CR): TP resuelto, Dell/Deloitte/DocuSign sondeados (2026-10-19)

**Orden de trabajo pedido**: de mas a menos empleados en CR. Respaldo completo en
`data/empresas/caza_ronda3_relevancia.json`.

1. **Teleperformance (resuelto, provider trivial)**: el board CR real es
   `www.jobs-tp.cr` pero es solo formulario WP SIN vacantes; el listing real vive
   en **tp.com (Umbraco)**: `GET /Umbraco/Api/Careers/GetCareersBase?node=1780
   &country=Costa%20Rica&culture=en-us&pageSize=50&page=0&accumulated=true`
   -> JSON `{resultado: [{title, location, description, url, nodeId,
   opportunityType, source(Umbraco|icims)}], countFirst}` + helpers
   `GetLocationList/GetCategoryList/GetLanguageList`. Sin Cloudflare (httpx ok).
   **Verificado**: 2 vacantes CR hoy (Representante de atencion al cliente,
   Full Time, link /es-cr/...). El `node=1780` sale del constructor
   `careersFilter` del HTML de `/en-us/careers/job-opportunities/` (el 1er arg es
   hashCode, trampita).
2. **Dell (ORC confirmado pero sin CR)**: el board global
   `enterpriseplatform.dell.com/hcmUI/CandidateExperience` (site CX_1001) da
   **0 resultados** con keyword "Costa Rica" (verificado en DOM). Provider ORC
   sin ganancia de momento.
3. **Deloitte**: board propio server-side
   `apply.deloitte.com/en_US/careers/SearchJobs` - publico pero el filtro GET
   `?searchTerms=` no aplica (0 hits): hay que decodificar el POST/form.
4. **Salesforce**: retry CXS `wd1/salesforce/External_Career_Site` -> HTTP 422
   (nombre de site equivocado); revisar la fila del directorio.
5. **DocuSign**: iCIMS multi-board - existe `latamcareers-docusign.icims.com`
   pero con 0 CR hoy. Provider iCIMS sigue siendo util (DocuSign-LatAm + TP-USA).
6. **Sutherland/SeekOut, Nestle/Walmart/CC-FEMSA**: pendientes (endpoint
   publico por verificar / Cloudflare y SPA).

**Siguiente**: provider TP-Umbraco (el mayor empleador CR de todo lo pendiente,
con API JSON publica ya decodificada) y provider Greenhouse (Smartsheet 7 CR,
Zuora 2, Fivetran - API board publica).


## 39. Modulo providers/login/ para sitios con sesion/Cloudflare + Nestle resuelto por via RSS (2026-10-19)

**Pedido del usuario**: "armar algo para las empresas/paginas que necesitan
log in" — y luego: "creo que necesito registrarme en las empresas, de lo
contrario no funcionara en los sitios que necesitan login". Confirmacion del
flujo: el registro/login lo hace el usuario A MANO, una vez, en su propio
Brave; el sistema NUNCA guarda credenciales.

### Arquitectura

1. **`providers/login/browser_sites.py`** (`LoginSitesProvider`, source
   `login_sites`, grupo **directos**): recorre `data/empresas/login_sites.json`
   con el Brave del usuario por CDP (mismo mecanismo que LinkedIn/ANE/Indeed):
   - modo **dom**: extraccion generica por selectores CSS del JSON;
   - modo **page_fetch**: `fetch()` de la API del SPA **dentro de la pagina**
     (mismo origen, cookies CF incluidas) y mapeo por dot-paths;
   - interstitial Cloudflare: deteccion por TITULO + body ("Un momento..." solo
     aparecia en <title>) + **auto-click del Turnstile** (en Brave real cedio
     solo en las pruebas);
   - login-wall/sin datos -> registra el pendiente en
     `data/empresas/pending_login.json` con motivo y SALTA (corrida sigue);
   - lista vacia legitima = "0 posiciones", NO pendiente.
2. **`hacer_login.py`** (CLI): abre cada pendiente en el navegador del
   usuario; tras confirmar "[s]", lo quita de la lista. La sesion queda en el
   perfil y las corridas la reutilizan.

### Hallazgos por empresa (ronda de sondas en vivo)

- **Nestle**: el front `www.nestle.com/jobs` es Drupal + **SearchStax**
  (widget JS, country facet "Costa Rica (3)") tras Cloudflare — PERO las URLs
  de detalle son `jobdetails.nestle.com/job/{slug}/{req}` = huella de
  **SAP SuccessFactors j2w**. El feed RSS
  `https://jobdetails.nestle.com/services/rss/job/?keywords=(Costa Rica)`
  responde **200 por httpx plano** con 3 CR (Mercaderista, Tecnico de Taller,
  Especialista de Proyectos — Heredia, fechas oct-2026). Solucion: entrada
  en `successfactors_sites.json`; SF paso de 13 a **16 CR**. Sin login, sin
  navegador, desc completa. Nestle sale de "bloqueados".
- **PayPal**: careers.pypl.com es un CMS corporativo (Q4), NO el board. El
  ATS real es **Eightfold AI** (`paypal.eightfold.ai`): `/api/apply/v2/jobs`
  da 403 "Not authorized for PCSX" incluso in-page; el endpoint real del SPA
  (visto por XHR-sniffing) es **`/api/pcsx/search?domain=paypal.com`** que si
  responde 200 con fetch in-page. Shape decodificado (name, locations[],
  positionUrl, postedTs epoch). Facets: **PayPal no tiene presencia CR hoy**
  (solo US/UK/Singapore/Berlin/Bengaluru). Provider queda listo: si PayPal
  abre CR, las vacantes fluyen sin cambios. (El redirect geo-IP a "Heredia,
  CR" era solo localizacion por IP, no evidencia de vacantes.)
- **Alorica**: funnel de hiring = **bling-ai** (`alorica-hire.bling-ai.com/
  alorica/career`): app de entrevistas AI (bundle de 6.2MB revisado:
  /api/interview/start, /api/req/detail, /api/candidates/* — **sin endpoint
  publico de listado**). Los funnels `recruit-latam.alorica.com/*-career`
  son por campana y solo MX (gdl/leon/pbl; sjo = 404). Queda pendiente
  documentado hasta hallar el funnel CR vigente.

### Bug de la reorg corregido (regression silenciosa)

Los 9 providers de `providers/directos/` resolvian sus JSON con
`Path(__file__).parent.parent` — correcto cuando vivian en `providers/`,
roto tras el Move-Item (apuntaba a `providers/data/...` que no existe) y
caian **en silencio** a sus constantes SITE_FALLBACK (por eso las corridas
parecian normales: fallbacks estaban sincronizados con los JSON). Corregido
a `parent.parent.parent` en los 9 (workday, phenom, tp, greenhouse, icims,
smartrecruiters, successfactors, rippling, ats_ligeros). Evidencia: al
corregir, SuccessFactors tomo la entrada nueva de Nestle (16 CR vs 13).
**Leccion**: todo provider que cargue JSON debe validar que el archivo
exista y LOGUEAR si usa fallback.

### Estado

- `main.py` paso "2l" (login-sites) integrado tras ats_ligeros, dentro del
  bloque del navegador (recibe `port`).
- `app.py`/`search.py`: source `login_sites` en los desplegables/filtros;
  mapeado a grupo directos en `source_groups.py` (su copia en boards se
  deduplica igual que los demas ATS).
- Smoke final: PayPal "0 posiciones (API ok)" sin pendiente; Alorica
  pendiente con motivo. `py_compile` OK en todo lo tocado.


## 40. Definicion del flujo de LOGINS: "tu navegador es la boveda" (2026-10-19)

**Pregunta del usuario**: "tenemos que definir que hace con los login, si me
das las paginas para hacer log in o creas una pagina de login para yo poner
las credenciales".

**Decision**: NO se construye formulario de credenciales (opcional B).
Razones: (1) guardaria contrasenas = responsabilidad de seguridad que el
proyecto no necesita — hoy es cero credenciales y asi se queda; (2) el login
automatizado es exactamente lo que Cloudflare/LinkedIn/Indeed mas detectan
(CAPTCHA, 2FA, bloqueos) — un formulario no lo resuelve, lo empeora; (3) cada
sitio tiene flujo distinto (2FA, OAuth de Google) → alta fragilidad.

Se implementa la **Opcion A mejorada**: el sistema TE DA los links y
verifica.

### Piezas nuevas

1. **`core/sesiones.py`** (logica compartida):
   - `sitios_con_sesion()`: union de BOARDS_SESION (Indeed ⭐, LinkedIn
     opcional) + `login_sites.json` (PayPal, Alorica) + motivos de
     `pending_login.json`.
   - `abrir_en_brave(url)`: abre el sitio en el Brave del usuario (Popen
     normal, SIN taskkill ni puerto de debug → pestana en la sesion viva;
     el login cae en el perfil correcto, el mismo que el scraper monta por
     CDP).
   - `verificar_sesion(url)`: monta Brave por CDP (flujo normal de corrida)
     y detecta muro real. Afinado tras falsos positivos: los marcadores de
     login van en el **titulo** + frases fuertes del body ("inicia sesion
     para continuar"...), porque el boton "Iniciar sesion" esta SIEMPRE en
     la homepage (logueado o no). Por eso cada board trae `url_check`
     (una BUSQUEDA real: es ahi donde salta el muro, no en la homepage).
2. **Tab "Sesiones" en `app.py`**: lista sitios con estado (⭐ recomendado /
   🟡 pendiente / 🟢 configurado), boton "Abrir en Brave", boton "Verificar
   sesion" (Brave se reinicia unos segundos), "Marcar resuelto" que limpia
   el pendiente de `pending_login.json`.
3. **`hacer_login.py` v2** (CLI equivalente): recorre sitios, abre en Brave,
   confirma [s]/[n] y verifica con [v].

### Hallazgo de la verificacion en vivo

El muro de Indeed de la corrida 2026-10-19 ("Iniciar sesion | Cuentas
Indeed", 0 cards en 4/6 busquedas) es **intermitente por carga**: una
carga aislada de la busqueda pasa sin muro ("Trabajos de Soporte ... |
Indeed"), bajo trafico de scraping lo activan. Conclusion: el login en
Indeed (recomendado, ⭐) blinda las corridas pesadas; la verificacion
reporta el estado del momento. LinkedIn guest OK.

### Smoke final

- `verificar_sesion` Indeed busqueda → ok (sin muro ahora); LinkedIn → ok.
- `hacer_login.py --list` muestra los 4 sitios con estado y motivo.
- `py_compile` OK en core/sesiones.py, app.py, hacer_login.py.


### Nota de cierre de sesion (2026-10-19, corrida final)

Corrida completa de cierre con todo integrado: 713 crudas -> dedupe 14 ->
699 -> gate -> 104 aprobadas -> 10 nuevas; 5 en las ultimas 24h
(data/report_24h_20261019.csv). jobs_raw: 858 (617 de hoy). Nestle persistida
via successfactors (3 CR con fecha). Distribucion de hoy: workday 334, ane 90,
linkedin 47, rippling 42, ats_ligeros 25 (workable en pausa 429), cinde 19,
greenhouse 17, successfactors 16, indeed 15, phenom 10, tp 1, sr 1.

**Known-issue menor (no bloqueante)**: main.py en corrida de scraping termina
con exit code 1 en Windows PERO sin traceback y con TODO persistido (los
reportes son lo ultimo que corre; la base y los CSV quedan bien). `--help`
y `--refiltrar` salen limpios; el exit no-0 aparece solo tras usar
Brave/CDP + torch en el mismo proceso — sospechoso: teardown de
Playwright/greenlet o torch al exit. A investigar en otra sesion (cosmetico:
no afecta datos; si se agenda por Task Scheduler, ignorar el codigo o
envolver con `; exit 0`).

---

## 2026-10-19 (tarde) — 4 bugs: credenciales, grupos, GUI primero y progreso en vivo

### Bug 1 — No existía una forma de poner passwords ni una lista de sitios que los piden

- **Solución**: almacén local `core/credenciales.py` sobre
  `data/empresas/credenciales.json` (escritura atómica, en `.gitignore`:
  **nunca viaja a git**). Expone `lista_sitios_login()` (la lista),
  `credencial/upsert/borrar`, `marcar_auto_login()` y
  `registrar_requerimiento()` que fusiona seed + detecciones runtime +
  `pending_login.json` + `login_sites.json`.
- **CLI** `credenciales.py`: sin argumentos imprime la lista (empresa,
  OBLIGATORIO/opcional, con/sin credencial, auto-login, grupo, motivo);
  `--set EMPRESA [--user --pass]`, `--borrar`, `--auto EMPRESA on|off`
  (y `--set X --auto on` para hacer ambas en una tanda).
- **Auto-login con fallback** (`providers/login/auto_login.py`): solo si el
  sitio está en `auto_login ON` (opt-in). Intenta 1 paso o 2 pasos
  (email → password) según el sitio; si hay CAPTCHA/2FA o el selector
  cambia, se cae a login manual/sesión existente **sin romper la corrida**.
- Integración: ANE usa el almacén con fallback `ANE_USER/ANE_PASSWORD`;
  Indeed detecta su muro de login, se registra en la lista e intenta
  auto-login; `browser_sites.py` reintenta con auto-login antes de dejar
  el sitio como pendiente.
- Semilla actual (5 sitios): ANE (obligatorio, credencial vía .env),
  Indeed (obligatorio), LinkedIn (opcional), Alorica y PayPal (opcionales,
  detectados). `main.py --preflight` reporta credenciales faltantes.

### Bug 2 — No se distinguía un job board de un portal de empresa

- Toda corrida ahora etiqueta cada fase/vacante con su grupo via
  `providers/source_groups.py` (boards vs directos). Etiquetas visibles:
  **Job board (agregador)** (ANE, CINDE, Indeed, LinkedIn) y
  **Portal de la empresa** (Workday, iCIMS, Greenhouse, Phenom, TP,
  SmartRecruiters, SuccessFactors, Rippling, ATS ligeros, login sites).
- `main.py` suma `encontradas.boards` / `encontradas.directos` separados,
  `job_row()` agrega columna "Grupo" y `app.py` ofrece filtro de grupo en
  sidebar + columnas en la tabla.

### Bug 3/4 — La GUI cargaba tarde (o nunca) y no mostraba progreso

- **`jobpulse.cmd` reescrito: primero el dashboard.** Arranca Streamlit,
  espera HTTP 200 (hasta 120 s), abre el navegador y **después** lanza el
  scraping; pasa argumentos (`jobpulse.cmd --preflight`) y no duplica
  corridas (chequeo de PID vivo). `timeout` final con fallback a `ping`
  para que salga 0 en consolas sin stdin.
- **Pestaña "🚀 Corrida"** en `app.py`: fragmento
  `@st.fragment(run_every="2s")` que lee `data/corrida_estado.json`
  (fuente única de verdad, escrita atómica por `core/progreso.py`): estado,
  barra de progreso `paso/total_fases`, chips de grupo, contadores
  encontradas/resumen, botones Iniciar/Preflight/Detener/Reindexar y tail de
  `data/corrida.log`. Funciona aunque la corrida la lance la consola,
  `jobpulse.cmd` o el propio dashboard (stdout crudo en
  `data/corrida_stdout.log`).
- **Pestaña "🔑 Credenciales"**: resumen (total/obligatorios/con
  credencial/pendientes), tabla de la lista, alta/edición con campo
  password `type="password"`, toggle auto-login y borrado.
- `main.py` ganó `--preflight` (sin red ni navegador: plan de corrida,
  grupos y credenciales faltantes) y el conteo de fases quedó exacto
  (22/22 reales, calculado después de la expansión semántica).

### Verificación

- Corrida real completa lanzada desde el botón del dashboard: `estado ok`,
  290 boards + 434 directos crudas, 1 nueva; barra y grupos visibles en vivo.
- `python main.py --preflight` → exit 0, FASE 22/22 sin pasarse.
- `cmd /c jobpulse.cmd --preflight` → dashboard primero (reutiliza 8501 si
  ya corre), navegador, corrida y exit 0.
- Round-trip CLI `--set/--auto/--borrar` OK y `git check-ignore` confirma
  `data/empresas/credenciales.json` ignorado (sin password residual tras
  la prueba). `py_compile` OK en todos los archivos tocados.

---

## 41. Reconciliación de la bitácora: dónde se quedó la sesión anterior (2026-10-19 noche)

Objetivo de esta entrada: responder "¿hasta dónde llegamos?" con **evidencia del repo**
(código + historial de git), aprovechando lo que ya existe en vez de rehacer trabajo.

### Dónde se quedó

- El repositorio tenía **16 commits** y el último era `fce6397` ("Bitácora: nota
  importante — Workday CXS"). Todo lo posterior vivió **solo en disco, sin commitear**:
  la reorganización `providers/boards|directos|login`, los providers nuevos
  (Workday, Phenom, TP, Greenhouse, iCIMS, SmartRecruiters, SuccessFactors,
  Rippling, ATS ligeros), `core/sesiones.py`, `core/dedupe.py`, las herramientas
  (`barrido_empresas.py`, `barrido_ats_otros.py`, `caza_ats_grandes.py`,
  `sondeo_phenom.py`, `workday_cr.py`), `hacer_login.py`, `ATS_ACCESOS.md`,
  el `SOURCES` ampliado de `search.py` **y las secciones 26–40 de esta bitácora**.
- En `ff4031e` se commiteó todo junto (41 archivos): desde ahora el código y la
  bitácora están alineados. `data/empresas/` (catálogos regenerables, ~2.5 MB)
  sigue sin trackear, como hasta ahora.

### Gaps detectados al reconciliar (y cómo quedaron)

1. **`ATS_ACCESOS.md` no estaba referenciado en esta bitácora.** Es el documento
   maestro de cómo se consiguió cada ATS: Vía A (fuente original
   `job-board-directory` → `data/empresas/ats_dir.csv`), Vía B (barrido de
   tableros → `otros_ats_cr.json`), Vía C (caza por huella de las grandes →
   `ats_grandes_huellas.json`), checklist de recreación desde cero, y el cierre
   de la ronda `providers/login/` (Nestle por RSS, PayPal=Eightfold/PCSX,
   Alorica pendiente). Quien continúe empieza por ahí.
2. **`search.py` → 15 fuentes.** `SOURCES` pasó de `["indeed","linkedin","ane",
   "procomer"]` a las 15 fuentes actuales (la sección 38 decía 14; después entró
   `login_sites`). Búsqueda libre y `--perfil` cubren todos los providers.
3. **Numeración.** La serie 1 salta de **5 → 7**: no existe el `### 6` (se perdió
   en un reordenamiento viejo; no se renumera para no romper referencias). Además
   las series se solapan (26–34 aparecen dos veces) porque la segunda tanda
   reanudó desde 26. **A partir de esta entrada: una sola serie, continúa en #42.**
4. **`phenom.py` en la raíz** (cliente que descubrió el patrón canvas SSR) no
   tenía mención explícita: el log hablaba de `providers/directos/phenom.py`
   (el provider) y `sondeo_phenom.py` (el sondeo). El de la raíz es la librería.

### Qué queda abierto (para la próxima sesión)

- **Known-issue**: `main.py` termina con exit 1 tras una corrida con Brave/CDT +
  torch, sin traceback y con todo persistido (ver "Nota de cierre de sesión").
- **Backlog**: wizard de setup de primera corrida (sección 33).
- **Credenciales faltantes**: Indeed (obligatorio, SIN credencial → auto-login
  OFF), Alorica (pendiente: selector del funnel a revisar), LinkedIn y PayPal
  (opcionales). Se resuelven con `python credenciales.py --set "Empresa"`.
- **Pausas técnicas**: Workable (throttle 429 con auto-pausa), Sutherland
  (SeekOut), iCIMS (0 vacantes CR hoy), Personio (scan 0 CR).

### Estado verificado al cierre (2026-10-19)

- Commit `ff4031e` (17 commits en total); sin datos personales en el repo ni en
  el historial (verificado: solo plantillas de ejemplo; `data/profile.json`
  nunca tuvo datos reales; CVs/`.env`/credenciales jamás commiteados).
- Corrida real completa desde el botón del dashboard: `estado ok`,
  290 boards + 434 directos crudas, 1 nueva.
- `python main.py --preflight` → exit 0, **22/22 fases**; `jobpulse.cmd
  --preflight` → dashboard primero (8501), navegador, corrida, exit 0.
- Dashboard con 5 pestañas: Ranking · Búsqueda libre · 🚀 Corrida ·
  🔑 Credenciales · 🔐 Sesiones.
