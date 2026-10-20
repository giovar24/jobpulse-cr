# Los accesos a los ATS: fuente original y proceso completo

Documento de referencia, independiente del código, que explica **cómo se obtuvo el
acceso a cada ATS**, **cuál fue la fuente original** de cada lista y **todo el
proceso** para poder recrearlo desde cero en el futuro.

Fecha de construcción de los datos: **2026-10-17 a 2026-10-19**.
Todo lo descrito aquí usa **solo endpoints públicos** (sin login, sin cookies de
sesión, sin API keys). Los métodos que sí exigen sesión activa están aparte, al
final (punto 8), porque quedaron en pausa por decisión del usuario.

---

## 1. Panorama general: 3 vías de acceso

| Vía | Qué da | ATS cubiertos | Fuel |
|---|---|---|---|
| **A. Directorio público de tableros** | token/API URL de cada tablero, ya validados | Workday, Greenhouse, Ashby, Workable, Recruitee, Personio, Rippling, Lever | `job-board-directory.csv` (fuente externa, descarga directa) |
| **B. Barrido de esos tableros contra CR** | qué empresas del directorio tienen vacantes activas en Costa Rica | las 8 plataformas anteriores | `barrido_ats_otros.py` (local) |
| **C. Caza por huella HTML** | qué ATS usa una empresa grande concreta si **no** está en el directorio | Phenom, SmartRecruiters, iCIMS, SuccessFactors, Taleo + confirmaciones | `caza_ats_grandes.py` (local) |

Idea central: **el ATS se descubre, nunca se asume** (lección de la fase Workday:
Thermo Fisher usa Workday **y** Phenom a la vez).

---

## 2. VÍA A — La fuente original: `job-board-directory` (Kaleb Confer)

### 2.1 Qué es y dónde está

- Sitio público (gratuito, sin registro ni atribución):
  **https://kalebconfer-sys.github.io/job-board-directory/**
- El directorio completo es un archivo, no un scrape:
  `job-board-directory.csv` (o el mismo en `.json`) servido desde el propio sitio.
- Última compilación vista: **2026-10-19** (se re-construye seguido; al recrear el
  proceso, volver a descargar para refrescar los tableros muertos/nuevos).

### 2.2 Por qué existe (tal como lo expone el dueño)

Cada una de estas plataformas sirve las vacantes de una empresa desde un endpoint
público, y el "parto difícil" es la **dirección**: un token de Greenhouse, un slug
de Lever y sobre todo el trípode `host-tenant-site` de Workday **no se pueden
deducir del nombre de la empresa** (un guess equivocado en Workday da un error de
servidor, no un "no existe"). Este sitio publica las direcciones: cada una fue
**observada en un rastreo/index público y confirmada contra la API pública del
propio vendor** antes de aparecer en la lista.

### 2.3 Contenido y volúmenes (check del 2026-10-19)

12,235 tableros confirmados vivos en total:

| Plataforma | Tableros | Nota sobre su API |
|---|---|---|
| Greenhouse | 3,576 | JSON público `/v1/boards/{token}/jobs` |
| Ashby | 2,486 | JSON público `/api/v1/postings` |
| Workable | 1,875 | JSON `?details=true` |
| Workday | 1,362 | POST CXS `host/wd/cxs/{tenant}/{site}/jobs` |
| Recruitee | 1,043 | JSON `/{company}/jobs` (campo `locationText`) |
| Personio | 866 | **XML** `<workzag-jobs>` (no JSON) |
| Rippling | 864 | JSON |
| Lever | 163 | JSON (título en `text`, link en `applyUrl`) |

### 2.4 Columnas del CSV / cómo se usa

`platform, token, company, board_url, api_url, open_roles, region`

- `api_url` es la dirección exacta del endpoint que devuelve las vacantes — el
  barrido solo tiene que hacerle GET (Workday es POST; verProviders).
- Archivo local de este proyecto: **`data/empresas/ats_dir.csv`** (descarga
  directa, sin transformación). Los 1,362 tableros workday se consumieron primero
  (sección 26 del log) y el barrido de la vía B filtra `platform != "workday"`
  (10,873 restantes).

### 2.5 Cómo recrear la fuente

```
1. Abrir https://kalebconfer-sys.github.io/job-board-directory/
2. Descargar job-board-directory.csv  ->  data/empresas/ats_dir.csv
   (mismo archivo en JSON si se prefiere)
3. Alternativa paga del mismo dueño (NO usada, solo referencia):
   RapidAPI "nightly-ats-hiring-index" (posts diarios por tablero) — gratis
   100 req/mes; y Actors de Apify por plataforma (blooming_gator/*-jobs-api).
```
No se firmó nada ni se usó ninguna llave; el CSV es descarga directa.

---

## 3. VÍA B — Del directorio a CR: barrido de tableros

Script: **`barrido_ats_otros.py`** (documentado en el log, sección 28).

### 3.1 Algoritmo

1. Lee `data/empresas/ats_dir.csv`, descarta `platform == "workday"`.
2. Para cada fila hace **GET del `api_url`** (timeout 25 s, headers UA ligero
   tipo `JobPulseCR/1.0`).
3. Respuesta JSON: recorre `jobs`/`postings`/`offers` (o lista raíz según
   plataforma) y recoge vacantes donde el **texto concatenado de campos de
   ubicación** (`*location*`, country/region/city, `locationText`, título) haga
   match con regex `costa\s+rica` (case-insensitive).
   - Se minan solo campos públicos (título/ubicación/link), nada autenticado.
4. Casos especiales de formato (por eso se probaron primero muestras por
   plataforma: `--muestra` lanza 4 tableros por plataforma):
   - **Personio**: sirve XML `<workzag-jobs>` con nodos `<position>` →
     `<name>`/`<office>`; se parsea con regex (no es JSON).
   - **Lever**: el título viene en `text` y el link en `applyUrl`.
   - **Recruitee**: la ubicación viene en `locationText`.

### 3.2 Ejecución (cómo se corrió de verdad)

```powershell
.venv\Scripts\python.exe -X utf8 barrido_ats_otros.py --muestra          # sanear extractores
.venv\Scripts\python.exe -X utf8 barrido_ats_otros.py                    # pasada 1
.venv\Scripts\python.exe -X utf8 barrido_ats_otros.py --plataforma greenhouse,lever   # reintentos
```

- Pasada 1: **14 workers** en paralelo → ~1,800 tableros cayeron por **HTTP 429**.
- Pasada 2 de reintentos solo con los estados que empiezan por `"HTTP 429"`
  (¡los estados se guardan con el prefijo, no como `429` pelado!), con
  **throttle 0.5 s** entre peticiones → recuperados casi todos.
- Fallos legítimos restantes: ~249 (404 tableros muertos, 302, 403).

### 3.3 Salidas

- `data/empresas/otros_ats_cr.json` — hits: tablero con ≥1 vacante CR activa
  (67 tableros / 65 empresas nuevas al 2026-10-19). Hallazgos: Smartsheet (7 CR),
  Zuora (2), Fivetran, Westernacher (4), Sysdig, Mindvalley; y que Workable/Lever/
  Recruitee están dominados por firmas de **staff-remoto LatAm** (Pavago 230,
  Uptalent 53, Lago 22...), que solo valen si el gate las filtra bien.
- `data/empresas/otros_ats_fallos.json` — errores por tablero.
- Los hits alimentan el CSV maestro via `barrido_empresas.py --solo merge`
  (columnas `ats_token` tipo `platform::token` y `vacantes_otros_ats_cr`).

---

## 4. VÍA C — Caza por huella (los ATS que NO están en el directorio)

**Problema que resolvió**: las multinationales con sede CR usan Phenom People,
iCIMS, SuccessFactors, Taleo o SmartRecruiters — y **ninguno de esos está en el
directorio público**. No hay fuente externa; se cazó puerta por puerta.

Script: **`caza_ats_grandes.py`** (log, sección 29).

### 4.1 Algoritmo

1. **Lista curada** de ~40 empleadores grandes con presencia en CR → en el código,
   dict `CANDIDATOS`: `empresa -> [URLs candidatas de páginas de carreras]`
   (se llenan a mano: `jobs.empresa.com`, `careers.empresa.com`, sección
   `/careers` del sitio corporativo; se exploran en orden, primera 2xx gana).
2. GET de cada URL con httpx (follow_redirects, UA Chrome normal,
   `Accept-Language: en-US,en`), 1 hilo por empresa (spawn cada 0.08 s).
3. **Huella**: contra el HTML se aplican estas regex de firma (`FIRMA` en el
   script):

| ATS | Marcadores en el HTML |
|---|---|
| phenom | `phenompeople` / `CareerConnectResources` / `csrfToken` / `everbe` |
| icims | `icims.com`, `\bicims\b`, `/c/jobs` |
| successfactors | `successfactors`, `career-section`, `sap.com/` |
| taleo | `taleo`, `.ftl` |
| smartrecruiters | `smartrecruiters` |
| workday | `myworkdayjobs.com`, `wd\d\.myworkday` |
| greenhouse / lever / workable | sus dominios |

### 4.2 Resultado (2026-10-19, tras ronda 2) → `data/empresas/ats_grandes_huellas.json`

- **Phenom**: Cisco, Fiserv, UPS, P&G, Snowflake, Zimmer Biomet, Thermo Fisher, HPE,
  **DHL** (r2: careers.dhl.com/global/en/search-results), **PayPal** (r2:
  careers.pypl.com — huella por navegador; su SSR da 403 por httpx/Cloudflare).
- **SmartRecruiters**: Western Digital, Bosch.
- **SuccessFactors**: Microsoft, EY, Foundever, Kuehne+Nagel.
- **iCIMS**: DocuSign; **Teleperformance** (r2: boards USA
  careers-teleperformance.icims.com; el Phenom jobs.teleperformance.com por país
  está DEAD en DNS).
- **Workday extra**: Rockwell, BD — y **validados por navegador r2** (los 403 de
  la caza eran pura protección del careers site): Intel (`intel.wd1/External`,
  CR San Jose ok por CXS), J&J (`jj.wd5/JJ`, CR Grecia/Alajuela ok), Kimberly-Clark
  (`wd1/kimberlyclark/GLOBAL`), Medtronic (`wd1/medtronic`), Autodesk
  (`wd1/autodesk/Ext`), **Concentrix** (el portal jobs.concentrix.com agrega el
  tenant `cnx.wd1/external_global` — esto resuelve que el token `cnx` del CSV
  maestro ES Concentrix, no CNX Resources).
- **Oracle Recruiting Cloud (ATS nuevo)**: Dell (`enterpriseplatform.dell.com/hcmUI/CandidateExperience/`).
- **SeekOut CareerCompass (ATS nuevo)**: Sutherland.
- **Front propio**: Amazon (amazon.jobs), Alorica (recruit-latam.alorica.com /
  bling-ai por país), Nestlé (interstitial Cloudflare), IBM, Deloitte, Broadcom,
  Koch, Coca-Cola FEMSA, Instacart, Walmart, Salesforce, Oracle (careers propio).
- **Taleo**: tras 2 rondas, 0 empresas confirmadas (huella `.ftl` sin hits).

### 4.3 Cómo recrear

```powershell
.venv\Scripts\python.exe -X utf8 caza_ats_grandes.py                 # todo
.venv\Scripts\python.exe -X utf8 caza_ats_grandes.py --solo cisco    # una empresa
```
Recetas para la ronda 2: añadir empresas al dict `CANDIDATOS` con 2-3 URLs
candidatas cada una; correr; los `403` grandes casi siempre tienen un front
custom (Amazon-style) y requieren tratar su site como ATS propio, no genérico.

---

## 5. La fuente Workday ya cubierta (antecedente)

- Mismo directorio (sección 2), archivo `workday.csv` → 1,362 tableros; sondeo
  con `workday_cr.py`: **POST CXS** `{api_url}` con `{"searchText": "Costa Rica",
  "limit": 20}`; un hit solo cuenta si `locationsText|title` contiene
  "costa rica" (evita vacantes que solo *mencionan* el país).
- Resultado: **51 tenants con CR confirmado** → `data/empresas/workday_cr.json`
  (fallback constante embebido en `providers/workday.py`).
- Lecciones operativas: 37 errores HTTP/2/timeout se recuperan reintentando con
  **http1.1** secuencial (fogonazo: Roche); el `total` de la CXS satura a 2,000 y
  sirve páginas más allá del fin del catálogo → no paginar a ciegas
  (`break` cuando `len(items) < PAGE_SIZE` + tope de páginas).
- Detalle de la vacante: GET `{api}/job/{externalPath}` →
  `jobPostingInfo.jobDescription` + `startDate` ISO exacta.

---

## 6. Interacción con el ATS una vez identificado: el caso Phenom (patrón canvas SSR)

Descubierto **con el navegador del proyecto** contra `jobs.thermofisher.com`
(abriendo la búsqueda y leyendo el HTML que el server manda), luego generalizado
en **`phenom.py`** (cliente `PhenomSite`) y **`providers/phenom.py`** (provider #7).

### 6.1 El patrón, sin cookies ni tokens

- Listado SSR: `GET {base}/{locale_path}/search-results?keywords=Costa%20Rica&from=N`
  - El HTML **embebe un JSON** con `"jobs":[...]` (10/página: jobSeqNo, title,
    multi_location, applyUrl, reqId, postedDate) y un `"totalHits":N`.
  - Extracción: `json.JSONDecoder().raw_decode()` sobre la **última** ocurrencia
    de `'"jobs":['` en el texto.
  - Paginación: `from=0,10,20,...` (cada página añade `N`); se corta cuando
    `totalHits` cubre o la página no trae jobSeqNo nuevos.
- Detalle: `GET {base}/{locale_path}/job/{reqId}/{slug}` (slug = título con
  reemplazos de espacios/puntuación) → embebe `"description"` (HTML, extraído con
  `raw_decode` en el índice tras `"description":"`) y `"postedDate"` ISO (usar
  `[:10]`).
- El `x-csrf-token` que trae el home **solo se exige en los POST
  `/widgets`** (ver punto 8) — para el SSR no hace falta nada.
- Locale paths que funcionaron: P&G `pgcareers.com` = `cr/en`; Thermo
  `jobs.thermofisher.com` = `global/en`; HPE `careers.hpe.com` = `us/en`;
  Zimmer `careers.zimmerbiomet.com` = `us/en`. El cliente los hace probar en
  orden (`PATHS_STD`) y también los detecta del home.

### 6.2 Filtros CR específicos de Phenom

- **Estricto**: solo `multi_location` + el texto de lugar; NO confiar en el campo
  `country` genérico (se colaban vacantes de Tijuana).

### 6.3 Comandos

```powershell
# sondeo de sitios Phenom curados -> data/empresas/phenom_cr.json
.venv\Scripts\python.exe -X utf8 sondeo_phenom.py
```

---

## 7. Recrear todo el proceso desde cero (checklist)

```powershell
# 0. Entorno
.venv\Scripts\python.exe -X utf8 <script>   # Windows + UTF-8

# 1. Refrescar la fuente original (Vía A)
#    descargar de kalebconfer-sys.github.io/job-board-directory ->
#    data/empresas/ats_dir.csv

# 2. Workday (ya provider): sondear tenants con CR
#    workday_cr.py -> data/empresas/workday_cr.json

# 3. Barrido no-Workday (Vía B)
#    barrido_ats_otros.py  (pasada 429s con throttle: ver 3.2)
#    -> data/empresas/otros_ats_cr.json / otros_ats_fallos.json

# 4. Caza por huella de las grandes (Vía C)
#    caza_ats_grandes.py -> data/empresas/ats_grandes_huellas.json

# 5. Sondeo Phenom de los hits
#    sondeo_phenom.py -> data/empresas/phenom_cr.json

# 6. Merge al CSV maestro
#    barrido_empresas.py --solo merge  -> data/empresas/empresas_candidatas.csv

# 7. Producción
#    python main.py    (barridos 2c Workday / 2d Phenom; gate decide)
```

Trampas conocidas (lecciones ya pagadas):
- 429 = throttle, no muerte permanente; reintento lento.
- Estados "HTTP 429" van con prefijo en los JSON de fallos — filtrar con
  `startswith("HTTP 429")`.
- PowerShell 5.1 no soporta `&&` ni comillas complejas inline: para consultas
  multi-línea (p. ej. sqlite), escribir un `.py` temporal en
  `C:\Users\giova\AppData\Local\Temp\opencode\` y correrlo.
- Se verifica el ATS real de cada empresa siempre (no se asume).

---

## 8. ACCESOS QUE SÍ REQUIEREN SESIÓN ACTIVA — pausados, para el final

Lo único de esta lista quedó **en pausa a petición del usuario** ("los que
requiera una sesión activa déjalo para último"):

1. **Phenom variante `/widgets`** (Cisco, Fiserv, UPS, Snowflake) y
   **PayPal** (careers.pypl.com su SSR da 403 por Cloudflare a httpx): estos
   sites exigen csrf ligado a sesión/cookies de navegador — recrear requeriría
   un cliente con cookies reales (browser/CDP) o replicar el handshake completo.
2. **Sites con front propio/cazados a mano** (Amazon amazon.jobs, Dell =
   Oracle Recruiting Cloud, Oracle, IBM, Deloitte, Broadcom, Koch, Coca-Cola
   FEMSA, Instacart, Walmart, Salesforce, Nestlé con Cloudflare, Alorica con
   funnel por país, NCR Voyix/Dominio muerto): cada uno hay que tratarlo como
   ATS propio; el paso previo sin sesión es extender `CANDIDATOS` y correr la
   caza, y para los SPA hacer la caza de su API con el navegador (tab Network).
3. **SeekOut (Sutherland)**: ATS nuevo — verificar si tiene endpoint público.
4. **Providers aún no implementados** para los ATS ya identificados (suelen
   sondearse sin login, como Greenhouse/Lever):
   SmartRecruiters (Western Digital, Bosch), iCIMS (DocuSign-LatAm,
   Teleperformance-USA), SuccessFactors (Microsoft, EY, Foundever, Kuehne+Nagel),
   Dell-ORC, SeekOut (Sutherland). Y en cola por API ya decodificada:
   **TP-Umbraco** (tp.com `GetCareersBase?node=1780&country=Costa Rica` — JSON
   público, verificado con 2 vacantes CR; ver `data/empresas/
   caza_ronda3_relevancia.json`) y **Greenhouse** (Smartsheet/Zuora/Fivetran).
5. **Opciones pagadas del dueño del directorio** (RapidAPI
   `nightly-ats-hiring-index`, Actors Apify por plataforma): nunca se usaron;
   útiles si algún día se quiere el feed nocturno de aperturas/cierres en vez
   de sondear los tableros directamente.

---

### Resumen de fuentes, en una línea

| Dato | Fuente original |
|---|---|
| Direcciones de tableros ATS (8 plataformas) | `job-board-directory.csv` — kalebconfer-sys.github.io/job-board-directory (descarga directa, gratis, sin key) |
| Tenants Workday con CR | Mismo archivo (workday.csv) + POST CXS con `searchText "Costa Rica"` |
| Empresas no-Workday con CR en Greenhouse/Ashby/Lever/etc. | Barrido propio `barrido_ats_otros.py` sobre ese CSV |
| ATS de las grandes (Phenom, iCIMS, SF, Taleo, SmartRecruiters) | No hay directorio público: huella HTML con `caza_ats_grandes.py` sobre páginas de carreras |
| Contraseña/cookie/API key | Ninguna: todo lo implementado usa solo endpoints públicos |


### Providers implementados DESPUES de redactar el punto 4 (2026-10-19)

Todos con endpoints publicos, sin key; detalles en DEVELOPMENT_LOG secc. 33-35
y en los providers:

- **SmartRecruiters**: API publica `api.smartrecruiters.com/v1/companies/`
  (sin key); ids de compania exactos: `westerndigital`, `BoschGroup`.
- **iCIMS**: listado `{host}/jobs/search?search=true&in_iframe=1&pr=N`
  (paginado pr=N, 50/pag; pageNum NO pagina; sin filtro keyword por GET);
  boards TP/DocuSign in `icims_boards.json` (0 CR hoy, infra lista).
- **SuccessFactors (j2w)**: feed RSS publico
  `{base}/services/rss/job/?keywords=(Costa Rica)` -> title + link +
  pubDate + description HTML inline; KN/Foundever/EY (13 CR en smoke).
  Microsoft NO es j2w (SPA, GCSServices 404): fica en bloqueados.


---

## Ronda 2026-10-19: modulo providers/login/ (sitios con sesion/Cloudflare)

Infra nueva: `providers/login/browser_sites.py` (LoginSitesProvider) + CLI
`hacer_login.py` + `data/empresas/login_sites.json` (sitios) y
`data/empresas/pending_login.json` (pendientes con motivo). Usa el Brave del
usuario por CDP; cero credenciales en el repo. Modos "dom" (selectores) y
"page_fetch" (API del SPA via fetch in-page). Interstitial Cloudflare con
deteccion title+body y auto-click del Turnstile (cedio en las pruebas).

### Nestle — RESUELTO por via publica (sale de bloqueados)

- Front `www.nestle.com/jobs`: Drupal + SearchStax (widget JS) tras
  Cloudflare. Country facet: "Costa Rica (3)". Cards `.jobs-card`
  (.jobs-title a / .jobs-business / .jobs-location).
- PERO las URLs de detalle son `jobdetails.nestle.com/job/{slug}/{req}` =
  SAP SuccessFactors j2w. Feed RSS publico por httpx plano:
  `https://jobdetails.nestle.com/services/rss/job/?keywords=(Costa Rica)`
  -> 200 con 3 CR (Mercaderista, Tecnico de Taller, Especialista de
  Proyectos; Heredia; oct-2026). Entrada agregada a
  `successfactors_sites.json`. Sin login.

### PayPal — ATS identificado: Eightfold AI (producto PCSX)

- careers.pypl.com = CMS corporativo (Q4 Inc), NO es el board.
- Board real: `paypal.eightfold.ai/careers` (Eightfold).
- `/api/apply/v2/jobs` -> 403 {"message": "Not authorized for PCSX"} (httpx
  e in-page).
- Endpoint real del SPA (XHR-sniffing):
  `/api/pcsx/search?domain=paypal.com&query=&location=...&start=0&num=N`
  -> 200 con fetch in-page. Shape: data.positions[] con name, locations[],
  standardizedLocations[], positionUrl (relativo), postedTs (epoch s),
  displayJobId, department, workLocationOption.
- Facets locations: SIN Costa Rica (US/UK/Singapore/Berlin/Bengaluru).
  Provider listo en login_sites.json (modo page_fetch); cuando PayPal abra
  CR fluye solo. El redirect geo-IP a "Heredia, CR" del SPA es solo
  localizacion por IP.

### Alorica — pendiente (funnel por campana)

- Hub `alorica.com/careers/` -> funnels `recruit-latam.alorica.com/*-career`
  por campana (gdl/leon/pbl = Mexico; sjo/san-jose = 404).
- Funnel global: `alorica-hire.bling-ai.com/alorica/career` = app de
  entrevistas AI "bling-ai" (bundle 6.2MB revisado: /api/interview/start,
  /api/req/detail, /api/candidates/*, alorica-api.bling-ai.com). Sin
  endpoint publico de listado de vacantes. Registrar pendiente hasta hallar
  el funnel CR vigente.

### Nota tecnica (bug reorg corregido)

Los 9 providers de `providers/directos/` quedaron con `Path(__file__).parent.parent` roto tras el Move-Item (buscaban `providers/data/...` que no
existe) y caian EN SILENCIO a sus SITE_FALLBACK. Corregido a
`parent.parent.parent`. Al corregir, successfactors tomo la entrada nueva de
Nestle (13 -> 16 CR). Leccion: loguear siempre el uso de fallback.
