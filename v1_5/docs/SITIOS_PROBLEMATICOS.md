# Sitios problemáticos: ANE, Indeed y LinkedIn

> Guía técnica de los 3 scrapers de navegador (CDP) que suelen dar problemas.
> Incluye: cómo funciona cada sitio, errores comunes con su solución exacta y
> procedimientos de recuperación. Actualizado: 2026-10-24 tras barrido completo.

---

## Arquitectura común a los 3

Todos usan **Playwright sobre CDP conectado a TU Brave real** (no abren un
navegador aparte), de modo que las cookies/sesiones que ya tienes en Brave se
reutilizan.

```
┌──────────────┐  CDP :9222  ┌──────────────────┐
│  Fetcher     │────────────▶│  Brave (perfil    │
│  (v1_5)      │  WebSocket  │  real del usuario)│──▶ sitio web
└──────────────┘             └──────────────────┘
```

### Requisito previo (el más importante)

Brave debe estar corriendo con **depuración remota en el puerto 9222** y con el
**perfil real**:

```
"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe"
    --remote-debugging-port=9222
    --user-data-dir="C:\Users\giova\AppData\Local\BraveSoftware\Brave-Browser\User Data"
```

**⚠️ Error de configuración histórico:** `--user-data-dir` debe apuntar a la
**raíz** `User Data`, **NO** a `User Data\Default`. Si apuntas a `Default`,
Chromium crea un perfil NUEVO y vacío dentro de ese directorio: Brave se abre
"como si fuera la primera vez" (sin marcadores, sin sesiones) y el login
siempre falla. Este bug estuvo en `jobpulse-v15.bat` y se corrigió.

**Verificar que CDP está vivo:**

```powershell
curl.exe -s http://127.0.0.1:9222/json/version
# Debe responder JSON con "Browser": "Chrome/..."
```

**Error típico si no está vivo:** `BrowserType.connect_over_cdp: connect
ECONNREFUSED 127.0.0.1:9222` → el barrido termina en `error`. Solución: lanzar
Brave con el comando de arriba (o usar el launcher `jobpulse-v15.bat`).

---

## 1) ANE (ane.cr) — sitio estatal, ASP.NET MVC

### Cómo funciona

| Aspecto | Detalle |
|---|---|
| URL base | `https://www.ane.cr` |
| Tipo | Sesión obligatoria (listado y detalles piden login) |
| Login | Usuario/contraseña en `/Cuenta/Login` |
| Credenciales guardadas | `data/empresas/credenciales.json` → clave `"ane"` |
| Verificación de sesión | Visita `/Puesto` y busca enlaces `Puesto/Detalles` |
| Detalles | Se piden por **HTTP** (`context.request.get`) reutilizando cookies del navegador |

Flujo del fetcher (`src/jobpulse_v15/ane.py`):

1. `connect_over_cdp(port)`
2. `_asegurar_sesion(ctx)`: abre `/Puesto`; si el HTML **no** contiene
   `Puesto/Detalles` → considera que NO hay sesión y **omite el barrido**
   (mensaje en log: `ANE: sin sesión de detalle; barrido omitido.` → la corrida
   queda `ok` con 0 ofertas, NO es error).
3. Extrae el listado de vacantes (config del sitio: `search_text` y `pages`).
4. Cada detalle se descarga con `requiere_sesion=True`; si el HTML muestra
   `loginForm` o `Cuenta/RegistroUsuario` → error `sesión ANE caducada`.

### Errores comunes y soluciones

| Síntoma | Causa | Solución |
|---|---|---|
| `sin sesión de detalle; barrido omitido` | Cookies de ANE vencidas o perfil equivocado | Re-login (ver procedimiento abajo) |
| `sesión ANE caducada (redirige a login)` | La sesión venció a mitad del barrido | Re-login y repetir |
| `connect ECONNREFUSED` | Brave sin CDP | Lanzar Brave con `--remote-debugging-port=9222` |
| `zero_results` en corrida | Sesión OK pero la búsqueda (`search_text` en site_config) no devuelve nada | Ajustar `search_text` en Administrar fuentes (ej: "Soporte" → "Soporte") o revisar el sitio a mano |
| Jobs con descripción muy corta (30–100 chars) | La descripción real está tras el detalle paginado y no se alcanzó a recuperar | Re-barrido tras re-login; el enriquecimiento IA no inventa descripciones |

### Procedimiento de re-login de ANE

**Opción automática (credenciales guardadas, `auto_login=true`):** ejecutar el
script de login contra CDP — completa usuario/contraseña en `/Cuenta/Login`
y verifica el resultado:

```
python <raíz del proyecto>/login_ane.py     # imprime "LOGIN ANE: OK"
```

Después de un login OK, reejecutar el barrido:

```
python run_scrapes.py ane     # en el barrido del 2026-10-24 devolvió 120 ofertas
```

**Opción manual:** abrir `https://www.ane.cr/Cuenta/Login` en tu Brave,
loguearte, y volver a ejecutar el barrido. La cookie queda en el perfil y el
siguiente barrido la reutiliza.

**Datos de sesión válidos** (para verificar a mano): entrar a
`https://www.ane.cr/Puesto` con tu Brave; si ves la tabla de vacantes con
enlaces a `/Puesto/Detalles/...`, hay sesión.

---

## 2) Indeed (cr.indeed.com) — SPA con overlays agresivos

### Cómo funciona

| Aspecto | Detalle |
|---|---|
| URL base | `https://cr.indeed.com/jobs` |
| Tipo | SPA (web components); listado por JS, detalles vía RPC |
| Login | **SSO Google** (sin contraseña propia): credencial `sso=google`, usuario `giovar24@gmail.com` |
| Verificación de sesión | `_hay_sesion()`: busca "Cerrar sesión"/`/settings` |
| Muro de login | `_muro_login()`: redirección a `/login` o authwall |
| Descripciones | POST a `/rpc/jobdescs` **por lotes** usando la sesión del navegador |

Flujo del fetcher (`src/jobpulse_v15/indeed.py`):

1. `goto cr.indeed.com/jobs?q=<search_text>&l=<location>`
2. **`_cerrar_popups(page)`** (nuevo): cierra overlays que interceptan clics.
3. Extrae tarjetas con `_JS_EXTRACT_JOBS` (y fallback `_JS_EXTRACT_JOBS_FALLBACK`).
4. Pagina hasta `pages` con el botón `a[data-testid='pagination-page-next']`.
5. `_enrich_descriptions()`: POST `/rpc/jobdescs` en lotes para traer la
   descripción completa de cada `jk`.

### El problema más frecuente: el popup de alertas de empleo

Indeed muestra un popup (`DesktopSERPJobAlertPopup`, dentro de un elemento
`<ifl-portal>`) con el email del usuario prellenado para "crear alerta de
empleo". Ese overlay **intercepta los pointer events** y hace que el click en
la paginación falle con:

```
ElementHandle.click: Timeout 30000ms exceeded.
<input type="email" ... id="DesktopSERPJobAlertPopup-email" ...>
  from <ifl-portal> subtree intercepts pointer events
```

**La versión anterior (v1.4) NO tenía manejo de este popup** (su
`next_btn.click()` era idéntico). Solución implementada en v1_5
(`_cerrar_popups`), que se ejecuta tras cada `goto` y antes de cada click de
paginación:

1. Tecla `Escape`.
2. Click en botones de cierre conocidos: `button[aria-label='Close']`,
   `button[aria-label='Cerrar']`, `button[data-testid='alerts-popup-close-button']`,
   `[data-testid='modal-close-button']`, `button[data-testid='serp_close_button']`.
3. Último recurso: `document.querySelectorAll('ifl-portal').forEach(el => el.remove())`
   — retira la capa de overlays de Indeed (solo contiene modales; no rompe el listado).
4. Si aun así el click falla: reintento con `page.evaluate("el => el.click()")`
   (click JS directo, inmune a overlays).

### Errores comunes y soluciones

| Síntoma | Causa | Solución |
|---|---|---|
| Timeout en click + "intercepts pointer events" | Popup de alertas | Ya resuelto con `_cerrar_popups`; si vuelve a aparecer, agregar el nuevo selector a `_POPUP_SELECTORES` |
| Muro de login al extraer 0 tarjetas | Sin sesión en Brave | SSO Google manual: abrir `cr.indeed.com` y loguearse con Google en tu Brave; el barrido reutiliza la sesión |
| `found 0 cards` sin muro de login | Indeed cambió su DOM | Revisar `_JS_EXTRACT_JOBS` y `_JS_EXTRACT_JOBS_FALLBACK` (selectores de tarjetas) |
| Descripciones vacías | `/rpc/jobdescs` rechazó el lote (sesión/cookies) | Revisar log `Indeed rpc/jobdescs no disponible`; re-login o fetch HTTP por vacante como fallback |

---

## 3) LinkedIn (linkedin.com) — el más hostil a automatización

### Cómo funciona

| Aspecto | Detalle |
|---|---|
| URL base | `https://www.linkedin.com/jobs/search/` |
| Tipo | Listado público de jobs + detalles con sesión |
| Login | **REGLA DE ORO: SESIÓN PRIMERO** — login manual UNA vez en tu Brave. El SSO Google es solo recurso opt-in porque LinkedIn dispara CAPTCHA/2FA con logins automatizados |
| Muro de login | `_muro_login()`: `"authwall" in url or "/login" in url` |
| Descripciones sin sesión | Endpoint público **`jobs-guest`**: `https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/<job_id>` |

Flujo del fetcher (`src/jobpulse_v15/linkedin.py`):

1. `goto` búsqueda con filtros de CR.
2. Si detecta **authwall**: registra pendiente de login y NO intenta auto-login
   (decisión deliberada: LinkedIn bloquea/rate-limita). Se pide login manual.
3. Extrae tarjetas del listado; si hay sesión, mejor calidad de datos.
4. Descripciones: primero `_fetch_details_guest` (endpoint público, no necesita
   sesión) y como fallback HTTP con cookies.

### Errores comunes y soluciones

| Síntoma | Causa | Solución |
|---|---|---|
| `authwall detectado. Se necesita sesión` | LinkedIn redirige a login | Login manual en tu Brave (linkedin.com), luego re-ejecutar barrido |
| Descripciones cortas/ausentes | `jobs-guest` rate-limited (HTTP 429) | Esperar minutos y re-ejecutar; el endpoint es público pero tiene límite |
| 429 / "Too many requests" | Demasiados detalles seguidos | Reducir `pages` en site_config o espaciar barridos |
| Ofertas duplicadas | El listado repite tarjetas al paginar | El fetcher deduplica por `job_id`; no es error |

---

## Checklist de diagnóstico rápido (los 3 sitios)

1. **¿Brave vive con CDP?** → `curl http://127.0.0.1:9222/json/version`
2. **¿Es el perfil real?** → si Brave se ve vacío, el launcher apunta a
   `User Data\Default` (mal) en vez de `User Data` (bien).
3. **¿Hay sesión en el sitio?** → abrir el sitio en tu Brave y verificar a mano.
4. **¿Qué dijo la última corrida?** → tabla `source_run_sites` en la BD
   (`status`, `error_message`).
5. **¿El sitio cambió su DOM?** → abrir el sitio con "Inspeccionar" y comparar
   los selectores del fetcher.

## Estado del barrido de referencia (2026-10-24)

| Fuente | Resultado | Notas |
|---|---|---|
| CINDE | 17 nuevas | API, sin navegador |
| Procomer | 1 nueva | API; antes devolvía 17 — la fuente expuso menos |
| ANE | 120 nuevas | Requirió re-login (credenciales guardadas) |
| Indeed | 30 nuevas | Requirió fix del popup de alertas |
| LinkedIn | 7 nuevas | Sin fricción esa corrida |

## Bugs sistémicos corregidos en este ciclo (memoria)

1. **`cli.py` sin guard `if __name__ == "__main__"`** → `python -m
   jobpulse_v15.cli <cualquier comando>` no hacía NADA (exit 0, sin salida).
2. **`.env` no cargado en `run_ia_enrichment`** → "IA no configurada:
   base_url requerido" al llamar el enriquecimiento desde UI/scripts.
   Ahora `load_dotenv()` corre dentro de la función.
3. **Launcher con ruta de Brave equivocada** (exe en `AppData\Local` cuando la
   instalación real está en `C:\Program Files\...`) y perfil apuntando a
   `User Data\Default`.
4. **Popups de Indeed** interceptando paginación (ver sección 2).
