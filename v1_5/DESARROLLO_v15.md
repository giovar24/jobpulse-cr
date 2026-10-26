# JobPulse CR v1.5 — Guía de Desarrollo y Estado del Proyecto

> **Propósito**: Documento único de referencia para continuar el desarrollo si se cambia de IA/agente.
> Contiene: comparación arquitecturas, pasos ejecutados, pendientes, objetivos y comandos clave.

> **⚠️ CONVENCIÓN DE SESIONES (obligatoria)**: cuando el usuario dice "vamos a otra sesión", se debe
> **escribir `docs/HANDOFF_SESION_YYYY-MM-DD.md`** antes de cerrar, con la misma estructura del handoff
> de 2026-10-25: TL;DR, lo que se hizo, decisiones de diseño, estado de objetivos (tabla), cómo retomar
> (comandos), archivos clave, datos de contexto y pendiente inmediato. La sesión nueva debe **empezar
> leyendo el handoff más reciente** para no arrastrar contexto acumulado.

---

## 1. COMPARACIÓN ARQUITECTURAL: PRINCIPAL vs v1_5

| Aspecto | **Principal (raíz)** | **v1_5 (fork)** | Notas |
|---------|---------------------|-----------------|-------|
| **BD** | `data/jobs.db` (SQLite simple) | `data/jobpulse_v15.db` (migraciones versionadas, FK, `integrity_check`, backup atómico) | v1_5 es **más robusta** |
| **Persistencia** | `core/database.py` + `save_jobs()` | `Repository.save_job()` + `IngestionService` (runs, health tracking, dedupe atómico) | v1_5 **auditable y transaccional** |
| **Scrapers** | `providers/boards/` + `providers/directos/` (clases `BaseProvider`) | Módulos planos `ane.py`, `cinde.py`, `workday.py`... con `fetch(site, ...) → XxxOffers` | v1_5 **declarativo y testeable** |
| **Orquestación** | `main.py` (asyncio.gather, fases hardcoded en `BARRIDOS`) | CLI `scrape <provider> --site/--group` + `IngestionService.fetch_and_import()` | v1_5 **modular, cada fuente independiente** |
| **Fuentes** | Hardcoded en `main.py` | Declarativas: `platform add` + `site add` (JSON config, sin credenciales) | v1_5 **config en BD, no en código** |
| **Dedupe** | `core/dedupe.py` (boards vs directos cross-source) | `job_field_status` + `source_job_memory` + review manual (no automerge) | v1_5 **más conservador** |
| **Búsqueda** | Semántica (embeddings) + ATS score + keywords | SQL estructurada (texto + filtros + requisitos + `profile_compatibility`) + embeddings (`search_engine.py`) | Paridad razonable |
| **IA/Embeddings** | `sentence-transformers` + `core/ia_api.py` (Gemini/NVIDIA/OpenRouter) | `ia_api.py` (OpenAI-compatible) + **enriquecimiento IA local** (`ia_enrichment.py`, Gemma en 8081) + `ia_calibracion.py` | ✅ Cliente externo + pase IA local |
| **CV Parsing** | `cv2profile.py` + `reporte_cv.py` (PDF→perfil Harvard/ATS completo) | Editor harvard-ats-v1 (`ui_perfil.py`) + reglas + **pase IA `cv_ia.py`** (E2E Gemma/Gemini, integrado al asistente de import) | ✅ Gap cerrado (O6.2) |
| **Auth/Session** | `core/browser_manager.py` + `hacer_login.py` + `credenciales.py` + `providers/login/` | ✅ FASE 3 completa: `browser_manager` + `credenciales` (keyring) + `providers/login/auto_login.py` + `browser_sites.py` | Cerrado |
| **Scheduler** | `recordatorio.py` + `jobpulse.cmd` (login + recordatorio Windows) | Parcial: launcher `jobpulse-v15.bat` (venv + IA + Brave CDP + UI); sin recordatorio | Gap medio |
| **Tests** | 7 unitarios (texto, dedupe, gate) | 17 suites / 217 tests + 46 subtests (pytest, python del sistema) | v1_5 **mejor cobertura** |
| **UI** | Streamlit (`app.py`) legacy + Tkinter (`ui.py`) completa | Tkinter (`ui.py`) completa (filtros, edición, seguimiento, perfil, calidad, duplicados, admin fuentes) | Similares |

---

## 2. ESTADO ACTUAL DE v1_5 (2026-10-26)

### ✅ YA IMPLEMENTADO Y FUNCIONAL

| Componente | Archivo(s) | Estado |
|------------|------------|--------|
| BD + migraciones (7 esquemas) | `db.py`, `migrations/001-007.sql` | ✅ Completo |
| `Repository` (CRUD sitios, jobs, analysis, profile) | `repository.py` | ✅ Completo |
| `IngestionService` (runs, health, validación, idempotencia) | `ingestion.py` | ✅ Completo |
| CLI base (`init-db`, `backup`, `db check`, `platform`, `site`, `run`, `search`, `quality`, `ingest`, `location`) | `cli.py` | ✅ Completo |
| Scrapers API-only | `workday.py`, `greenhouse.py`, `ashby.py`, `lever.py`, `recruitee.py`, `workable.py`, `successfactors.py` | ✅ Completo + tests |
| Análisis estructurado (`rules-1`) | `analysis.py` | ✅ Completo |
| UI Tkinter completa | `ui.py` | ✅ Completa |
| Tests (13 suites) | `tests/test_*.py` | ✅ Pasan |
| Config declarativa (platform/site) | `repository.py` + CLI | ✅ Funcional |

### ✅ Scrapers CR browser (antes EN PROGRESO — completados 2026-10-24/25)

ANE, CINDE, Procomer, LinkedIn e Indeed: lógica portada E integrados con la CLI
(`scrape ane|cinde|procomer|linkedin|indeed`), la UI y `run_scrapes.py`; los
browser reciben `port` CDP del caller (nunca crean navegador).

### ❌ PENDIENTE (gaps reales al 2026-10-26)

| Componente | Prioridad | Notas |
|------------|-----------|-------|
| **Dedupe cross-source (boards ↔ directos)** | 🟡 Alta | Hoy solo idempotencia misma-fuente + `quality duplicates` (revisión manual); `dup_of` previsto, sin columna |
| **Scheduler / recordatorio Windows** | 🟢 Media | Launcher `jobpulse-v15.bat` ya existe; falta portar `recordatorio.py` |
| **O2 / O3 / O4** (ATS + ranking en búsqueda UI; validar 7 fetchers ATS huérfanos; calibración gate-vs-IA) | 🟡 | Ver handoff vigente |

---

## 3. PLAN DE EJECUCIÓN (ORDEN DE PRIORIDAD)

### FASE 1: Scrapers Browser + API CR (Semana 1-2) — **COMPLETADO**
**Objetivo**: Traer el 80%+ de vacantes de Costa Rica.

| Tarea | Archivo(s) | Comando CLI | Estado |
|-------|------------|-------------|--------|
| Completar ANE fetcher + CLI | `ane.py`, `cli.py` | `scrape ane --site ane-cr --pages 15 --port 9222` | ✅ 100% |
| Completar CINDE fetcher + CLI | `cinde.py`, `cli.py` | `scrape cinde --site cinde-cr --pages 10` | ✅ 100% |
| Completar Procomer fetcher + CLI | `procomer.py`, `cli.py` | `scrape procomer --site procomer-cr --pages 5` | ✅ 100% |
| Completar LinkedIn fetcher + CLI | `linkedin.py`, `cli.py` | `scrape linkedin --site linkedin-cr --pages 2 --port 9222` | ✅ 100% |
| Completar Indeed fetcher + CLI | `indeed.py`, `cli.py` | `scrape indeed --site indeed-cr --pages 2 --port 9222` | ✅ 100% |
| **Bootstrap fuentes CR** | `cli.py` (comando `init-sources`) | `jobpulse-v15 init-sources` | ✅ **Completo** |

**Patrón obligatorio para cada fetcher**:
```python
def fetch(self, site: dict, *, pages: int, search_text: str | None, 
          skip_source_job_ids: set[str] | None, port: int | None = None, **kwargs) -> XxxOffers:
    # 1. Validar params
    # 2. Si browser: conectar a CDP en `port` (pasado por caller)
    # 3. Scrapear → lista de dicts con keys: source_job_id, source_url, source_updated_at, job, raw_payload
    # 4. job dict con TODOS los campos normalizados (ver schema en repository.py)
    # 5. Devolver XxxOffers(offers, jobs_skipped=N)
```

### FASE 2: Capa de Inteligencia (Semana 2-3) — **COMPLETADO**
| Tarea | Archivo(s) | Notas |
|-------|------------|-------|
| `ia_api.py` | **Nuevo** | ✅ Cliente LLM compatible OpenAI (OpenRouter/NVIDIA/Gemini/Ollama/LM Studio/llama.cpp) |
| `search_engine.py` | Nuevo | Embeddings locales + cache incremental + expansión ES↔EN |
| `ats.py` | Nuevo | Score ponderado (skills 40%, título 30%, exp 15%, educ 7.5%, idiomas 7.5%) |
| `gate.py` | Nuevo | Gate Fecha Y Relevancia → staging + curado |
| `ia_calibracion.py` | Nuevo | Pearson/Spearman/MAE gate vs IA + umbral sugerido |
| `cv_analysis.py` | Nuevo | Extractores regex + embeddings para CV → perfil Harvard/ATS |
| `cv2profile.py` | Nuevo | CLI: `cv2profile.py "cv.pdf" --save`; conv CV → profile dict JSON |

### FASE 3: Auth/Session/Browser Manager — **COMPLETADO**
| Tarea | Archivo(s) | Notas |
|-------|------------|-------|
| `browser_manager.py` | Nuevo/portado | Lanza Brave CDP, comparte cookies, host_resolver_rules |
| `credenciales.py` | **Nuevo/portado** | ✅ Almacén local (gitignored) + lista sitios login + auto-login fallback |
| `providers/login/auto_login.py` | **Nuevo/portado** | ✅ SSO Google (Indeed/LinkedIn), CAPTCHA/2FA detection, fallback manual |
| `providers/login/browser_sites.py` | **Nuevo/portado** | ✅ Cloudflare Turnstile + login-wall, modos DOM/API |

**Tests:** 127/127 pasan

### FASE 4: Dedupe Cross-Source + Bootstrap + Scheduler (Semana 4)
| Tarea | Archivo(s) | Notas |
|-------|------------|-------|
| Dedupe boards↔directos | `ingestion.py` o job post-ingest | Prioridad ATS empresa; marca `dup_of` sin borrar |
| `init-sources` command | `cli.py` | Registra plataformas + sitios CR por defecto |
| `recordatorio.py` | Portado | Login startup + recordatorio Windows (Task Scheduler) |
| `jobpulse-v15.cmd` | Nuevo | Launcher UI + corrida con un clic |

---

## 4. COMANDOS CLAVE PARA DESARROLLO

```powershell
# === ENTORNO ===
cd D:\Proyectos\jobpulse_cr\v1_5
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\playwright install chromium  # para scrapers browser

# === BD ===
.\.venv\Scripts\python.exe -m jobpulse_v15 init-db
.\.venv\Scripts\python.exe -m jobpulse_v15 db check

# === FUENTES ===
# Bootstrap CR (12 plataformas + 5 sitios, idempotente):
.\.venv\Scripts\python.exe -m jobpulse_v15 init-sources

# Alta manual de plataformas/sitios NUEVOS (p. ej. otro tablero ATS):
.\.venv\Scripts\python.exe -m jobpulse_v15 platform add --id ane --name "ANE/Brete" --provider-type browser --default-language es
.\.venv\Scripts\python.exe -m jobpulse_v15 platform add --id cinde --name "CINDE Jobs" --provider-type api --default-language es
.\.venv\Scripts\python.exe -m jobpulse_v15 platform add --id linkedin --name "LinkedIn" --provider-type browser --default-language es
.\.venv\Scripts\python.exe -m jobpulse_v15 platform add --id indeed --name "Indeed CR" --provider-type browser --default-language es
.\.venv\Scripts\python.exe -m jobpulse_v15 platform add --id procomer --name "Procomer" --provider-type api --default-language es
.\.venv\Scripts\python.exe -m jobpulse_v15 platform add --id workday --name "Workday" --provider-type api --default-language en

# Sitios
.\.venv\Scripts\python.exe -m jobpulse_v15 site add --id ane-cr --platform ane --name "ANE Costa Rica" --url https://www.ane.cr --country CR --group boards
.\.venv\Scripts\python.exe -m jobpulse_v15 site add --id cinde-cr --platform cinde --name "CINDE Jobs" --url https://cindejobs.com --country CR --group boards
.\.venv\Scripts\python.exe -m jobpulse_v15 site add --id linkedin-cr --platform linkedin --name "LinkedIn CR" --url https://www.linkedin.com/jobs/search/ --country CR --group boards --config '{"search_text": "Soporte Técnico", "pages": 2}'
.\.venv\Scripts\python.exe -m jobpulse_v15 site add --id indeed-cr --platform indeed --name "Indeed CR" --url https://cr.indeed.com/jobs --country CR --group boards --config '{"search_text": "Soporte Técnico", "pages": 2}'
.\.venv\Scripts\python.exe -m jobpulse_v15 site add --id procomer-cr --platform procomer --name "Procomer CR" --url https://talento.procomer.com --country CR --group boards --config '{"search_text": "Soporte", "pages": 5}'

# === SCRAPING ===
# Brave DEBE estar corriendo con CDP en puerto 9222 para scrapers browser,
# con el PERFIL REAL: --user-data-dir apunta a la RAIZ "User Data", NUNCA a
# "User Data\Default" (crea perfil vacio y todos los logins "fallan"):
# brave.exe --remote-debugging-port=9222 --user-data-dir="C:\Users\<user>\AppData\Local\BraveSoftware\Brave-Browser\User Data"

# ANE (browser)
.\.venv\Scripts\python.exe -m jobpulse_v15 scrape ane --site ane-cr --pages 15 --port 9222

# CINDE (API, sin browser)
.\.venv\Scripts\python.exe -m jobpulse_v15 scrape cinde --site cinde-cr --pages 10

# LinkedIn (browser)
.\.venv\Scripts\python.exe -m jobpulse_v15 scrape linkedin --site linkedin-cr --pages 2 --port 9222

# Indeed (browser)
.\.venv\Scripts\python.exe -m jobpulse_v15 scrape indeed --site indeed-cr --pages 2 --port 9222

# Procomer (API)
.\.venv\Scripts\python.exe -m jobpulse_v15 scrape procomer --site procomer-cr --pages 5

# Grupo completo
.\.venv\Scripts\python.exe -m jobpulse_v15 scrape ane --group boards --pages 15 --port 9222

# === BÚSQUEDA ===
.\.venv\Scripts\python.exe -m jobpulse_v15 search --text "soporte técnico" --country CR --work-mode hybrid --format csv --limit 50

# === UI ===
.\.venv\Scripts\python.exe -m jobpulse_v15.ui

# === TESTS ===
python -m pytest tests/ -q    # pytest vive en el python del sistema (el venv no lo trae)
```

---

## 5. PRÓXIMA ACCIÓN INMEDIATA

Pendientes vivos (handoff vigente `docs/HANDOFF_SESION_2026-10-25_noche.md`):

1. **O6.3**: matching afinado (niveles/CEFR/work_authorization).
2. **O5.3**: re-barrido de los ~14-19 jobs con descripción basura (login-wall).
3. **Dedupe semántico del perfil**: slugs ES/EN distintos para el mismo hecho —
   decidir si se normaliza en import o se deja al usuario en el diálogo.
4. Luego: O2 (ATS + gate + ranking en la búsqueda UI), O3 (validar 7 fetchers
   ATS huérfanos), O4 (calibración gate-vs-IA).

---

## 5. ARCHIVOS CLAVE PARA MODIFICAR

| Archivo | Qué tocar |
|---------|-----------|
| `ane.py` | `fetch(site, pages, search_text, skip_source_job_ids, port, dias)` → `AneOffers` |
| `cinde.py` | `fetch(site, pages, search_text, skip_source_job_ids)` → `CindeOffers` |
| `procomer.py` | `fetch(site, pages, search_text, skip_source_job_ids)` → `ProcomerOffers` |
| `linkedin.py` | `fetch(site, pages, search_text, skip_source_job_ids, port)` → `LinkedInOffers` |
| `indeed.py` | `fetch(site, pages, search_text, skip_source_job_ids, port)` → `IndeedOffers` |
| `cli.py` | Agregar subcomandos `scrape ane|cinde|procomer|linkedin|indeed` |
| `pyproject.toml` | Ya actualizado con deps: `httpx`, `playwright`, `beautifulsoup4`, `sentence-transformers`, `torch`, `python-dotenv`, `aiosqlite` |

---

## 6. NOTAS TÉCNICAS IMPORTANTES

1. **NO crear navegador en el fetcher**: el caller (CLI) pasa `port` CDP. El fetcher hace `p.chromium.connect_over_cdp(f"http://localhost:{port}")`.
2. **`site` dict viene del Repository**: incluye `site_config` (JSON), `db_path`, `base_url`, `company_name`, `country`, `run_groups`.
3. **`skip_source_job_ids`**: IDs que el usuario marcó para omitir manualmente (`skip_automatic=1`). El fetcher NO debe procesarlos.
4. **`jobs_skipped`**: Contador de ofertas omitidas por `skip_source_job_ids` (se pasa a `IngestionService` para health tracking).
5. **Campos `job` obligatorios** (ver `Repository.save_job`): `job_id`, `title`, `company`, `location_raw`, `country`, `city`, `region`, `work_mode`, `employment_type`, `date_published`, `expires_at`, `posting_language`, `posting_language_origin`, `description_raw`, `requirements_raw`, `salary_raw`, `sector` (opcional).
6. **Tests**: Cada fetcher nuevo debe tener test en `tests/test_<nombre>.py` (mock HTTP/Playwright, BD temporal).
7. **Modelo IA local: Gemma 4 E2B QAT (canónico desde 2026-10-25)**. A/B y pase real demostraron que es
   ~6.5x más rápido que Qwen 3.5 2B y llena mucho más (detalle completo en `docs/IA_ENRIQUECIMIENTO.md`):
   pase completo 175 jobs en 10.3 min (Qwen: ~67 min); skills 100%, languages 97.7%. Config viva en
   `.env` (`IA_MODEL=gemma4-e2b-qat`), `start_ia_server.py` (con `--metrics`) y `ia-server.bat`.
   ⚠️ Al probar/benchmarkar: la caché en disco (`data/usuario/ia_cache/`) responde instantáneo si el
   (job+modelo+prompt) ya se pidió — aislarla con `JOBPULSE_IA_CACHE` (así lo hace `tools/comparar_ia.py`).

---

## 7. DECISIONES DE DISEÑO TOMADAS

- **No migrar datos legacy**: v1_5 empieza limpio; `data/jobpulse_v15.db` independiente.
- **No automerge dedupe**: v1_5 marca `dup_of` y deja review manual (evita pérdida de datos).
- **Config declarativa only**: No credenciales en `site_config`; credenciales en almacén local aparte (`credenciales.py`).
- **Scrapers browser reciben `port`**: Separación de responsabilidades (browser manager ≠ fetcher).
- **Health tracking por defecto**: 3 ceros = `review_required` (no desactiva solo).

---

*Última actualización: 2026-10-26*
*Próxima revisión: tras cerrar O6.3 / O5.3 (ver §5)*

---

## 8. HISTORIAL DE CAMBIOS RECENTES

### 2026-10-24 — IA Externa: cliente OpenAI-compatible (`ia_api.py`) + `init-sources` completado

**Implementado:**
- **`ia_api.py`** — Cliente IA externa compatible OpenAI con 6 presets:
  - `openrouter` — openrouter.ai/api/v1 (modelo default: `~openai/gpt-latest`)
  - `nvidia` — integrate.api.nvidia.com/v1 (modelo default: `openai/gpt-oss-20b`)
  - `google` — generativelanguage.googleapis.com/v1beta/openai/ (modelo default: `gemini-2.5-flash`)
  - `ollama` — ollama.com/v1 o localhost:11434/v1 (auto-detecta modelo via `/models`)
  - `lmstudio` — localhost:1234/v1 (auto-detecta modelo via `/models`)
  - `custom` — cualquier endpoint OpenAI-compatible (**llama.cpp**, vLLM, etc.)

**Características clave:**
- Cache persistente en disco (hash determinista: proveedor + modelo + mensajes + PROMPT_VERSION)
- Rate limiting configurable (RPM) + reintentos automáticos en 429/5xx
- `chat_json()` con `response_format` json_schema/json_object para respuestas estructuradas
- `listar_modelos()` consulta GET /models en vivo (sin hardcodear modelos)
- Errores tipados: `IAModeloError` (404/410 fatal), `IACuotaError` (402/429)
- Hash de vacante/CV para cache determinista
- `PROMPT_VERSION = "v3"` invalida cache al cambiar prompts

**Configuración llama.cpp / LM Studio (local):**
```bash
# .env
IA_PROVEEDOR=custom
IA_BASE_URL=http://localhost:8080/v1
IA_MODEL=llama-3.1-8b-instruct
IA_API_KEY=sk-no-key-needed

# Levantar llama.cpp
llama-server -m modelo.gguf -c 8192 --port 8080
```

**Comando `init-sources` completado:**
- Registra 12 plataformas + 5 sitios CR automáticamente
- `--skip-existing` para idempotencia
- Grupos configurables: `--group-gov cr-gov` / `--group-jobs cr-jobs`

**Tests:** 127/127 pasan (excluyendo test_ui por entorno Tcl/Tk)

**Próximos pasos FASE 2:** `search_engine.py` (embeddings), `ats.py`, `gate.py`, `ia_calibracion.py`, `cv_analysis.py`, `cv2profile.py` — **Todos completados. Pasar a priorizar fetchers CR o FASE 3.

---

### 2026-10-24 — Credenciales + Login manual (`credenciales.py`, CLI `credenciales`/`login`)

**Implementado:**
- **`credenciales.py`** — Almacén local completo (gitignored):
  - `credenciales.json` — credenciales usuario/password/SSO
  - `login_requerido.json` — LA LISTA maestra (seeds + runtime + manual)
  - `pending_login.json` — pendientes detectados en corridas
  - `login_sites.json` — portales employer para barrido con navegador

**Seeds configurados:**
- **ANE** — obligatorio, auto_login, user/password (fallback .env: ANE_USER/ANE_PASSWORD)
- **Indeed** — obligatorio, auto_login, SSO Google (cuenta Gmail, no password propia)
- **LinkedIn** — opcional, SSO Google, prioridad login manual en Brave

**CLI `credenciales`:**
- `list` / `list --resumen` / `list --solo-faltantes`
- `set --empresa --usuario [--password|--sso google] [--auto on|off] [--url]`
- `del --empresa`
- `auto --empresa --estado on|off`
- `add-site --empresa [--url] [--grupo] [--obligatorio]`
- `rm-site --empresa`

**CLI `login`:**
- `login --list` — lista pendientes/recomendados
- `login` — abre cada sitio en TU Brave (pestaña en sesión viva), tú te logueas, confirmas, y la sesión queda para el scraper
- Reusa `BraveManager` para lanzar Brave con CDP

**Tests:** 127/127 pasan

**Completado FASE 3 (Auth/Session):**
- **`providers/login/auto_login.py`** — SSO Google Indeed/LinkedIn (iframe GIS, popup, selector cuenta), fallback a login manual si Google pide password/CAPTCHA/2FA; user/pass 1-2 pasos con selectores configurables; `hay_login_configurable()`
- **`providers/login/browser_sites.py`** — LoginSitesProvider con 2 modos:
  - `dom`: extraccion por selectores CSS (card_sel, title_sel, etc.) + paginacion + deteccion Cloudflare Turnstile (auto-click checkbox) + login-wall -> registra pendiente
  - `page_fetch`: fetch API interna del SPA (mismo origen, cookies CF) + JSONPath + dot-paths
- Ambos usan `credenciales.py` para auto-login opt-in

**Tests:** 127/127 pasan

**Próximos pasos FASE 2:** `search_engine.py` (embeddings), `ats.py`, `gate.py`, `ia_calibracion.py`, `cv_analysis.py`, `cv2profile.py` — **Todos completados. Pasar a priorizar fetchers CR o FASE 3.

---

### 2026-10-25/26 — IA local canónica, instalador, capa IA de CVs, repo al día

- **Gemma 4 E2B QAT canónico** (8k ctx + MTP draft, puerto 8081): A/B vs Qwen
  3.5 2B — ~6.5x más rápido; pase completo 175 jobs en 10.3 min (detalle en
  `docs/IA_ENRIQUECIMIENTO.md`). `ia-server.bat` + `start_ia_server.py` (`--metrics`).
- **Instalador IA equipo nuevo**: `instalar-modelo-ia.bat` / `instalacion_ia.py`
  (modelo Unsloth con SHA256 verificados + llama.cpp Vulkan; `--check` = verificar).
- **Capa IA de CVs (O6.2)**: `cv_ia.py` (PDF/DOCX/TXT → harvard-ats-v1, E2E con
  Gemma local y Gemini) e **integrada** al asistente "Importar CV…" de la UI
  (reglas primero; pase IA si sugieren <5 facts; revisión en `_DialogoRevision`).
- **Suite**: 217 tests + 46 subtests (pytest del sistema; el venv no trae
  pytest). Flaky de Tk estabilizado con `_tkroot_reintentos()` (tests/test_ui.py).
- **Seguridad**: `login_ane.py` SIN credenciales en el código — lee del almacén
  (`credencial("ANE")`: keyring de Windows + `data/empresas/credenciales.json`,
  fallback `.env` ANE_USER/ANE_PASSWORD). `keyring` instalado en el venv y
  credencial ANE migrada al almacén. **Pendiente del usuario: rotar la
  contraseña de ANE** (quedó expuesta en el historial git público) y
  actualizarla con `credenciales set`.
- **Docs al día y verificados contra el código**: `AGENTS.md`, este documento
  (estado, FASE 1, gaps, comandos) y `README.md` (tests con pytest).