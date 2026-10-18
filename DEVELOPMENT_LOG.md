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
