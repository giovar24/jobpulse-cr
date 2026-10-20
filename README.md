# 💼 JobPulse CR

Buscador de empleos **100% local** para Costa Rica: scrapea vacantes de los
principales portales, las indexa con embeddings en tu propia CPU y las rankea
contra tu perfil de candidato con un **score tipo ATS** (Applicant Tracking
System). Sin APIs de pago, sin nube, sin LLMs.

## Características

- **Scraper** (`main.py`): Indeed, LinkedIn, ANE, Procomer, CINDE Jobs (cindejobs.com)
  y **Workday** (tenants curados con presencia en CR: Intel, HPE, Edwards, Stryker,
  Thermo Fisher...) → `data/jobs.db` (SQLite). Temáticas de búsqueda derivadas del
  perfil del candidato y expandidas semánticamente (ES↔EN).
- **Búsqueda semántica** (`search.py`): consulta en lenguaje natural sobre
  las vacantes con embeddings locales
  (EmbeddingGemma 300M si tienes `HF_TOKEN`, si no fallback multilingüe
  abierto `paraphrase-multilingual-mpnet-base-v2`). Cache incremental de
  embeddings.
- **Score tipo ATS** (`core/ats.py`): punto porcentual estilo ATS real,
  ponderado y configurable en el perfil:
  `skills 40% · título 30% · experiencia 15% · educación 7.5% · idiomas 7.5%`
  con **keywords faltantes** por vacante y umbrales
  (🟢 ≥85 fuerte · 🟡 70-84 ajustar · 🔴 <70 débil).
- **Analizador de CV** (`cv2profile.py`): CV (PDF/DOCX/TXT/MD) en **español o
  inglés** → perfil (`data/profile.json`): títulos semánticos, skills, idiomas
  con nivel, años de experiencia, educación, certificaciones y contacto.
  Inspección completa del proceso con `reporte_cv.py "MiCV.pdf"` (texto crudo,
  extracciones, IA local, revisión con evidencia por frase y perfil final).
- **Dashboard Streamlit** (`app.py`): **Ranking** (desglose ATS), **Búsqueda
  libre**, **🚀 Corrida** (progreso en vivo de un scraping: fase, barra,
  vacantes por grupo *boards* vs *portales directos*, log y botones
  Iniciar/Preflight/Detener), **🔑 Credenciales** (sitios que piden password,
  guardar/borrar, auto-login) y **🔐 Sesiones** (login manual en tu Brave).
- **Grupos de fuente** (`providers/source_groups.py`): cada vacante se
  etiqueta como *Job board (agregador)* (Indeed, LinkedIn, ANE, CINDE) o
  *Portal de la empresa* (Workday, iCIMS, Greenhouse...), lo que alimenta
  el dedupe cross-source y los contadores de la pestaña Corrida.

## Instalación

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

**Eso es todo.** En el primer arranque, `core/bootstrap.py` crea lo que falte:

- `data/` (directorio de trabajo)
- `data/profile.json` ← copia del **ejemplo genérico** (`Juan Perez`, datos falsos)
- `.env` ← copia de `.env.example` (placeholders; pon tu `HF_TOKEN` si quieres EmbeddingGemma)

### Privacidad

- `data/profile.json` y `.env` están en `.gitignore`: **tus datos personales nunca se suben a git**.
- La plantilla `data/profile.example.json` solo contiene datos de ejemplo (`juan.perez@example.com`).
- Para generar tu perfil real desde tu CV: `python cv2profile.py "MiCV.pdf" --save`
  (queda solo en tu máquina).

## Uso

```powershell
# 1. Scrapear vacantes
python main.py

# 2. Generar tu perfil desde tu CV
python cv2profile.py "MiCV.pdf" --save

# 3. Ranking de mejores opciones (CLI)
python search.py --perfil

# 4. Búsqueda libre interactiva
python search.py "mesa de ayuda bilingüe remoto"

# 5. Dashboard (Ranking · Búsqueda libre · 🚀 Corrida · 🔑 Credenciales · 🔐 Sesiones)
streamlit run app.py

# 6. Diagnostico SIN red ni navegador (plan de corrida + credenciales faltantes)
python main.py --preflight

# 7. Lista de sitios que piden password / gestionar credenciales (local)
python credenciales.py                       # la lista + estado
python credenciales.py --set "Indeed"        # pedir usuario/password
python credenciales.py --set "Indeed" --user "yo@x.com" --pass "S3creto"
python credenciales.py --auto "Indeed" on    # login automatico con fallback
python credenciales.py --borrar "Indeed"

# 8. Diagnosticar la extraccion de un CV (texto, regex, IA, revision, perfil)
python reporte_cv.py "MiCV.pdf"

# 9. Benchmark de modelos de embeddings (compara candidatos locales)
python bench_modelos.py
```

### Corrida con un clic y recordatorio (Windows)

- **`jobpulse.cmd`** — doble clic: **primero** arranca el dashboard
  (espera a que responda http://localhost:8501 y abre el navegador),
  **después** lanza el scraping. Así la GUI nunca se queda esperando 5+
  minutos de consola ni "nunca se activa" si `main.py` falla. Acepta
  argumentos extra (`jobpulse.cmd --preflight`) y **no lanza una segunda
  corrida** si ya hay una viva.
- **Pestaña "🚀 Corrida"** del dashboard: progreso en vivo (fase actual,
  barra, conteo de vacantes por grupo *Job board (agregador)* vs *Portal de
  la empresa*, log final) y botones **Iniciar / Preflight / Detener /
  Reindexar**. Sirve igual si la corrida la lanzaste desde la consola: todo
  pasa por `data/corrida_estado.json` + `data/corrida.log`.
- **`recordatorio.py`** — sin servidor 24 h: al **iniciar sesión** pregunta
  si quieres correr el scraping (si ya corriste hoy, no molesta). Con
  "Más tarde..." tú fijas la hora; si el equipo estaba apagado a esa hora,
  la corrida se ejecuta en cuanto arranque.

```powershell
# Instalar el recordatorio (una sola vez)
powershell -File instalar_recordatorio.ps1

# Probarlo a mano (ignora el chequeo de "ya corriste hoy")
python recordatorio.py --forzar
```

### Formatos de CV soportados

| Formato | Vía | Notas |
|---|---|---|
| `.pdf` ✅ **recomendado** | `pypdf` | Repara ligaduras tipográficas (`ﬁ` → `fi`) y palabras partidas por salto de línea (`usuario-`⏎`ficticio` → `usuario-ficticio`). |
| `.docx` | `python-docx` | Extrae solo párrafos: el texto dentro de **tablas no se lee**. Si tu Word usa tablas, exporta a PDF y usa el PDF. |
| `.txt` / `.md` | lectura directa UTF-8 | Tolerante a codificaciones raras. |

**No soportados**: `.doc` (Word binario viejo), `.odt`, `.rtf` y **PDFs
escaneados/imágenes** — sin OCR un CV escaneado sale vacío (todo corre en CPU
local, sin servicios externos ni motores OCR).

**Privacidad**: cualquier CV real que dejes en `data/` (pdf/doc/docx/txt/md)
queda **local-only** — el `.gitignore` lo excluye; solo los demos sintéticos
(`data/demo_cv.docx`, `data/demo_cv_en.txt`) viajan al repo.

## Credenciales de los portales (primera corrida)

### La lista de sitios que piden password

`python credenciales.py` (sin argumentos) — o la pestaña **🔑 Credenciales**
del dashboard — muestra **la lista viva** de sitios que requieren sesión.
Se alimenta de `data/empresas/login_requerido.json` (seed + lo que los
scrappers detectan en runtime: muros de login, `pending_login.json`,
`login_sites.json`). Cada entrada indica si es **OBLIGATORIO u opcional**,
si ya tiene credencial, si el auto-login está activo, el grupo de la fuente
(*Job board (agregador)* / *Portal de la empresa*) y el motivo.

**Guardado local**: las credenciales viven en
`data/empresas/credenciales.json`, que está en `.gitignore` (nunca viajan a
git) con escritura atómica. **Auto-login con fallback** (opcional por sitio,
`--auto ... on`): se intenta login con lo guardado en tu Brave; si aparece
CAPTCHA/2FA o falla, el scraper **no se rompe** — cae a login manual /
sesión existente y sigue con el resto de la corrida.

| Portal | ¿Credenciales? | Primera corrida |
|---|---|---|
| **LinkedIn** | No | Funciona solo: el scraper usa el endpoint público *guest* y tu navegador Brave real. Si inicias sesión en LinkedIn **dentro de Brave**, el acceso mejora (sin sesión puedes ver menos resultados o el muro "Iniciar sesión"). |
| **Indeed** | **Recomendado** | Funciona sin login, pero el sitio muestra un **muro de login intermitente** en algunas búsquedas (0 cards con título "Iniciar sesión | Cuentas Indeed"). Ahora se puede resolver: `python credenciales.py --set "Indeed" --auto on` (o pestaña 🔑 Credenciales). Sin credenciales, el scraper lo detecta, lo registra en la lista y sigue con el resto. |
| **ANE** | **Sí** | Es el proveedor con más vacantes. Crea tu cuenta gratuita en [ane.cr](https://www.ane.cr) y ponla en el almacén local: `python credenciales.py --set "ANE"` (o `ANE_USER`/`ANE_PASSWORD` en `.env`, que sigue funcionando como fallback) — el scraper inicia sesión solo. **Atajo**: si ya iniciaste sesión en ane.cr dentro de Brave, no necesitas nada (reutiliza esa sesión). Sin credenciales ni sesión, el barrido se **omite con un aviso** y los demás portales siguen. |
| **CINDE Jobs** | No | Nunca pide nada: barre las vacantes activas de [cindejobs.com](https://cindejobs.com) por su API GraphQL pública (sin navegador ni login). El tablero no expone fecha de publicación, así que el gate las trata como vigentes y las rankea por relevancia con tu perfil. |
| **Workday** | No | Lista curada de tenants con presencia en CR (HPE, TD Synnex, Edwards Lifesciences, Thermo Fisher, Stryker, Roche...): empresas grandes que **no publican todo en LinkedIn/Indeed**. API CXS pública (`*.myworkdayjobs.com`), sin muros; descripcion completa y fecha exacta del propio ATS. Lista actualizable con `workday_cr.py`. |
| **Phenom** | No | Sitios Phenom "canvas" confirmados con CR (P&G, Zimmer Biomet...; HPE/Thermo saltados por solapar con Workday). El SSR de sus páginas de resultados trae listado + descripción completa y fecha exacta, sin cookies ni tokens. Lista curada en `data/empresas/phenom_cr.json` (regenerable con `sondeo_phenom.py`). |
| **Otros ATS** | No | Sondeo de 10,873 tableros Greenhouse/Ashby/Workable/Lever/Recruitee/Personio/Rippling filtrando "Costa Rica" (67 tableros con CR): Smartsheet, Zuora, Fivetran, Westernacher + firmas de staff remoto LatAm. Herramienta: `barrido_ats_otros.py`; resultado en `data/empresas/otros_ats_cr.json`, consolidado en el CSV maestro `data/empresas/empresas_candidatas.csv` (sin provider propio aún). |
| **TP (Teleperformance)** | No | El board CR real es la API Umbraco de tp.com (`GetCareersBase?node=1780&country=Costa Rica`): JSON público paginado con descripción completa, fecha ISO y link de apply. `jobs-tp.cr` (el sitio que la gente conoce) es solo formulario. Incluye requisiciones iCIMS frescas cuando TP las integra (`source: icims`). Las evergreen con fechas viejas las descarta el gate por ventana — correcto. |
| **Greenhouse** | No | 1 GET por tablero (`boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true`) trae TODO el catálogo con descripción inline y `first_published` ISO. Tokens confirmados con CR: Smartsheet (7, San José remoto), Zuora (2), Orion Global Solutions, Fivetran, Nayya, Storyblok, Think Academy US, Torq. Tokens del barrido multi-ATS (`otros_ats_cr.json`); regenerable invitando más tokens al master CSV. |
| **iCIMS** | No | Patrón decodificado: listado paginado `GET {host}/jobs/search?search=true&in_iframe=1&pr=N` (50/pág; `pageNum` NO pagina ni hay filtro keyword por GET). Por hoy **0 vacantes CR** en los boards TP/DocuSign (TP CR contrata vía tp.com — ya cubierto por el provider TP — y DocuSign archivó sus boards regionales); el provider queda listo y barato (~12 requests/corrida) para cuando DocuSign-LatAm o iCIMS de TP publiquen CR. Boards en `data/empresas/icims_boards.json`. |
| **SmartRecruiters** | Sí (1) | API pública `api.smartrecruiters.com/v1/companies/{cid}/postings?q=Costa Rica` (pag de 100) + detalle `.../postings/{id}` con `jobAd.sections` HTML y `releasedDate` ISO. Ids de compañía case-sensitive: `westerndigital` (335 global, 0 CR hoy), `BoschGroup` (5,083 global, 1 CR: Senior SAP Hybris Heredia). |
| **SuccessFactors (j2w)** | Sí (13) | Los portales SAP SuccessFactors "Career Section" exponen **feed RSS público sin key**: `{base}/services/rss/job/?keywords=(Costa Rica)` con título+link+pubDate+descripción HTML completa inline (3-20 KB). El keyword NO es estricto (trae Buenos Aires/Dominican Rep igualmente) → filtro client. Confirmed: **Foundever 5** (CS Reps Moravia/San Pedro/Heredia, sept-oct 2026), **K+N 4-5** (Barreal de Heredia, GS roles), **EY 4** (San José). Microsoft no es j2w (SPA, GCSServices da 404): bloqueado. |
| **Rippling** | Sí (43) | Boards `ats.rippling.com/{slug}/jobs` renderizan el listado **server-side** con JSON embebido por jobPost (`"title"`→`"url"`→`"locations":[{name, countryCode}]`). Filtro CR por registro (`countryCode == "CR"`); NO filtrar por texto libre del HTML (trae listas de países del dropdown que son ruido). Detalle `/{slug}/jobs/{uuid}` → `__NEXT_DATA__` → `apiData.jobPost.description.{role, company}` + `createdOn` ISO. **43 vacantes CR** (2026-10-19): Aubrant Digital 15, CommandLink 8, Sparq 8, Lisinski Law Firm 6, Pythian 2, Venn 2, Havenly 1, InAir 1. Scan completo de 864 boards en `data/empresas/rippling_scan_cr.json`; 8 activos en `rippling_boards.json`. |
| **ATS ligeros (Ashby/Lever/Recruitee/Workable)** | Sí (25+ / 166 con Workable apagado) | APIs públicas del vendor, tokens con CR pre-scaneados en `otros_ats_cr.json` (ashby 7, lever 3, recruitee 3, workable 46 tableros). Validado en smoke: **Ashby 10** (JBS 3 Sr, MindValley, Owner CS Support nov 3, Possible Finance, Greenlight, Mimica), **Lever 6** (Westernacher 4 CR, Sysdig, Omnidian), **Recruitee 9**. **Workable** (huzzle 60 CR multi-loc, etc.) funciona pero su API `apply.workable.com/api/v1/...` aplica **throttle global por IP** (429≫): POST con headers `X-Workable-Shortcode`/`X-Workable-Page` (cursor `nextPage`, 10/pág) + detalle GET de desc; el provider auto-pausa el resto de la corrida si el 429 persiste tras cooldown de 120s — en producción (cadencia lenta) corre entero (166 CR de la primera corrida antes del throttle). Personio (866 tenants via `/xml`): scan completo **0 CR**. |

**Resumen**: con un `.env` vacío el sistema corre y trae LinkedIn + Indeed +
CINDE Jobs; para desbloquear ANE (el proveedor #1) rellenas la credencial
local o `ANE_USER`/`ANE_PASSWORD`, o inicias sesión en ane.cr en tu Brave.
`python main.py --preflight` te dice exactamente qué falta antes de correr.

## Estructura

```
core/
  search_engine.py   # motor de embeddings + cache incremental
  ats.py             # score ATS ponderado + keywords faltantes
  cv_analysis.py     # extractores de CV (regex + embeddings)
  profile.py         # CandidateProfile (fuente única de verdad)
  database.py        # SQLite de vacantes
providers/           # scrapers por fuente, en DOS grupos (razon: dedupe
                     # cross-source — los boards republican puestos del ATS
                     # propio: core/dedupe.py descarta la copia del board
                     # cuando el puesto ya existe en un provider directo)
   base.py            # BaseProvider/JobOffer (contrato comun) +
                      # source_groups.py (mapeo source -> boards/directos)
   boards/            # JOB BOARDS y portales locales (agregadores)
    cinde.py          # CINDE Jobs (cindejobs.com): API GraphQL publica, sin navegador
    indeed.py         # Indeed (scrape SSR)
    linkedin.py       # LinkedIn guest
    ane.py            # ANE (ane.cr)
    procomer.py       # Procomer / Zona Franca
   directos/          # PROVIDERS DIRECTOS al ATS del employer (canonica)
    workday.py        # Workday: CXS API de myworkdayjobs.com, lista de tenants en
                      # data/empresas/workday_cr.json (regenerable con workday_cr.py)
    phenom.py         # Phenom "canvas": SSR de {lang}/search-results, lista de
                      # sitios en data/empresas/phenom_cr.json (sondeo_phenom.py)
    tp.py             # TP Umbraco (tp.com GetCareersBase, node=1780 country=CR);
                      # JSON publico con desc/fecha/link, incluye source icims
    greenhouse.py     # boards-api greenhouse: 1 GET por tablero con content=true,
                      # tokens con CR desde data/empresas/otros_ats_cr.json
    icims.py          # iCIMS: listado /jobs/search?in_iframe=1 paginado pr=N,
                      # filtro CR client-side; boards en icims_boards.json
    smartrecruiters.py # SmartRecruiters: API publica v1/companies/{cid}/postings
                       # q=Costa Rica; companies en smartrecruiters_companies.json
    successfactors.py # SAP SuccessFactors j2w: feed RSS publico
                      # /services/rss/job/?keywords=(Costa Rica); sites en
                      # successfactors_sites.json (KN, Foundever, EY, Nestle)
    rippling.py       # Rippling ATS: listado SSR en HTML con locations JSON
                      # embebidas por jobPost; detalle via __NEXT_DATA__;
                      # boards en rippling_boards.json (scan del directorio)
    ats_ligeros.py    # Ashby/Lever/Recruitee/Workable: APIs publicas del
                      # vendor, 1 GET por tablero; tokens en otros_ats_cr.json
                      # (workable paginado con X-Workable-Shortcode/Page)
   login/             # SITIOS QUE REQUIEREN NAVEGADOR REAL (sesion/Cloudflare):
                      # usa el Brave del usuario por CDP (cookies del perfil).
                      # Detecta login-wall: primero intenta AUTO-LOGIN con la
                      # credencial local si el sitio esta en auto_login ON, y
                      # si falla (CAPTCHA/2FA) cae a login manual sin romper
                      # la corrida; registra el caso en login_requerido.json.
    browser_sites.py  # LoginSitesProvider: sitios en data/empresas/
                      # login_sites.json; modos "dom" (selectores CSS) y
                      # "page_fetch" (API del SPA via fetch in-page, p.ej.
                      # PayPal/Eightfold pcsx/search); interstitial Cloudflare
                      # con deteccion title+body y auto-click del Turnstile
  hacer_login.py      # CLI: recorre sitios que conviene loguear (Indeed sobre
                      # todo) + pendientes de login; abre cada uno en TU Brave
                      # (login manual UNA vez; la credencial local es opcional)
                      # y verifica con [v] si la sesion tomo
core/credenciales.py # ALMACEN local de credenciales (gitignored) + LA LISTA
                      # de sitios que piden login: lista_sitios_login,
                      # credencial/upsert/borrar, marcar_auto_login,
                      # registrar_requerimiento (seed + runtime + pendientes)
core/progreso.py     # progreso de la corrida para el dashboard: escribe
                      # data/corrida_estado.json (atomico) + data/corrida.log;
                      # Progreso.fase/sumar/total/finalizar
credenciales.py      # CLI de esa lista: sin argumentos la muestra; --set,
                      # --borrar, --auto EMPRESA on|off, --user/--pass
core/sesiones.py     # logica de sesiones compartida por app/hacer_login:
                      # sitios_con_sesion() (boards + login_sites + motivos),
                      # abrir_en_brave() y verificar_sesion() (detecta muro
                      # real cargando una BUSQUEDA, no la homepage)
core/dedupe.py       # dedupe boards<->directos: prioridad al ATS del
                      # employer; si el puesto ya esta en directos se
                      # descarta la copia de los boards (match empresa+titulo)
data/profile.json    # perfil del candidato
main.py · search.py · cv2profile.py · app.py · credenciales.py
```

## Notas

- **Sesiones de login (tab "🔐 Sesiones" del dashboard)**: los sitios que
  necesitan sesion se resuelven A MANO — la app te da el link "Abrir en
  Brave", te logueas una vez y el scraper reusa esa sesion del perfil.
  Es el camino por defecto: el login automatico dispara CAPTCHAs/2FA.

- **Credenciales (tab "🔑 Credenciales" / `python credenciales.py`)**:
  opcional y por sitio. Si guardas usuario/password y activas `--auto on`,
  el scraper intenta el login solo y, si el sitio responde con CAPTCHA/2FA
  o 2 pasos que no puede completar, **cae al login manual sin romper la
  corrida**. Todo queda en `data/empresas/credenciales.json`, que está en
  `.gitignore`: nunca viaja a git y solo vive en esta máquina.

- **Dedupe boards↔directos**: la misma vacante suele publicarse en LinkedIn/
  Indeed (agregador) y en el ATS de la empresa (Workday, Greenhouse...).
  `core/dedupe.py` conserva la version del ATS (req id/fecha/desc canonicos)
  y descarta la copia del board antes del gate.
- Todo corre en **CPU local**; nada de tu perfil o CV sale de tu máquina.
- Modelo de embeddings elegido por benchmark (`bench_modelos.py`, 16 tareas
  reales): gana `paraphrase-multilingual-mpnet-base-v2` (16/16) frente a
  `e5-small` y `MiniLM` multilingües.
- `.env` está en `.gitignore`: tus tokens nunca se suben al repo.
- `data/jobs.db` y los caches de embeddings se regeneran con `python main.py`.
- Cambio de equipo: clona + `pip install -r requirements.txt` y arranca; el
  bootstrap crea los archivos faltantes automáticamente.
