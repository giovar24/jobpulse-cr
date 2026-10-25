# AGENTS.md — JobPulse CR v1.5

Instrucciones para agentes IA que trabajan en este fork (`v1_5`).
Objetivo: orientación instantánea sin re-explorar el proyecto.

## Al iniciar sesión

1. **Lee el handoff más reciente**: `docs/HANDOFF_SESION_*.md` — estado exacto,
   objetivos (O1–O6), decisiones y pendiente inmediato.
2. Referencia de arquitectura y comparación con el proyecto principal:
   `DESARROLLO_v15.md` (§6 = notas técnicas importantes).

## Al cerrar sesión (OBLIGATORIO)

Cuando el usuario dice "vamos a otra sesión":
- Escribir `docs/HANDOFF_SESION_YYYY-MM-DD.md` con la estructura estándar:
  TL;DR, lo que se hizo, decisiones de diseño, estado de objetivos (tabla),
  cómo retomar (comandos), archivos clave, datos de contexto, pendiente inmediato.
- Commitear los cambios de la sesión.

## Mapa de documentación

| Documento | Qué es |
|---|---|
| `docs/HANDOFF_SESION_*.md` | Bitácora por sesión (empezar siempre por el más nuevo) |
| `docs/IA_ENRIQUECIMIENTO.md` | Operación del pase IA: modelo local, caché, depuración |
| `docs/SITIOS_PROBLEMATICOS.md` | Guía ANE / Indeed / LinkedIn (login-wall, Cloudflare) |
| `DESARROLLO_v15.md` | Arquitectura vs proyecto principal, objetivos O1–O6, comandos |

## Scrapers de navegador: ANE, Indeed, LinkedIn

**Guía completa: `docs/SITIOS_PROBLEMATICOS.md`** (leer ANTES de depurar
cualquiera de estos 3 — tiene errores exactos con su solución y procedimientos
de recuperación). Reglas de oro que resumen todo el documento:

1. **Brave DEBE correr con CDP 9222 y el perfil real**: `--user-data-dir` apunta
   a la raíz `User Data`, **NUNCA** a `User Data\Default` (crea perfil vacío y
   todos los logins "fallan"). Verificar: `curl http://127.0.0.1:9222/json/version`.
2. **ANE (ane.cr)**: sesión obligatoria. Sin sesión el barrido se omite limpio
   (no es error). Re-login: `python login_ane.py` → re-barrido `run_scrapes.py ane`.
3. **Indeed (cr.indeed.com)**: SSO Google. El popup de alertas de empleo
   intercepta clics — ya resuelto con `_cerrar_popups`; si aparece un popup
   nuevo, agregar su selector en `_POPUP_SELECTORES`.
4. **LinkedIn**: REGLA DE ORO sesión primero — login MANUAL una vez en tu
   Brave (el auto-login dispara CAPTCHA/2FA). Rate-limit 429 en `jobs-guest`:
   esperar y reducir `pages`.

## Reglas del proyecto (no negociables, acordadas con el usuario)

- **Pensar en general, no "para mí"**: nada del usuario hardcodeado. Perfil =
  facts en BD; filtros de sitios = `site_config` declarativo; credenciales =
  Credential Manager (`credenciales.py`).
- **Límites IA solo opt-in**: `IA_MAX_LLAMADAS`/`IA_RPM` en `.env` o args;
  servidores locales sin tope por defecto.
- **Scrapers browser reciben `port` CDP** del caller; el fetcher nunca crea navegador.
- **Config declarativa**: plataformas/sitios en BD (`init-sources`), no en código.
- **Dedupe conservador**: marcar `dup_of`, sin automerge.
- **Cada fetcher nuevo** lleva test en `tests/test_<nombre>.py`.

## Comandos esenciales

```powershell
# Servidor IA local (Gemma 4 E2B QAT, puerto 8081) — verificar/arrancar
.\ia-server.bat                # o: .\.venv\Scripts\python.exe start_ia_server.py

# Enriquecimiento (idempotente: solo procesa lo que falta)
.\.venv\Scripts\python.exe run_enriquecimiento.py

# Diagnóstico BD
.\.venv\Scripts\python.exe tools\estado_bd.py            # resumen
.\.venv\Scripts\python.exe tools\progreso_enriquecimiento.py   # % por campo
.\.venv\Scripts\python.exe tools\huecos_restantes.py     # huecos: basura vs recuperable

# Tests — test_ui requiere display; corre OK en el PC del usuario (200 tests).
# pytest vive en el python del sistema (el venv no lo tiene instalado).
python -m pytest tests/ -q

# UI
.\.venv\Scripts\python.exe -m jobpulse_v15.ui
```

## Gotchas que cuestan horas (leer antes de depurar)

1. **Caché IA en disco** (`data/usuario/ia_cache/`, hash proveedor+modelo+mensajes):
   en pruebas/benchmarks responde instantáneo y falsea resultados. Aislar con
   `JOBPULSE_IA_CACHE` (así lo hace `tools/comparar_ia.py`).
2. **`work_mode='unknown'` es honesto**: el texto de la oferta no menciona
   modalidad. No forzarlo; esos jobs se re-seleccionan en cada pase pero la
   caché los hace instantáneos.
3. **Jobs con descripción basura** (login-wall, ~14-19): el enriquecimiento NO
   los recupera; requieren re-barrido del scraper (O5.3).
4. **`/metrics` del llama-server** requiere arrancar con `--metrics` (ya está
   en `start_ia_server.py`); sin él, el endpoint devuelve 501.
5. **`only_critical` solo filtra la SELECCIÓN de jobs**: `enrich_job_fields`
   llena TODOS los huecos de los jobs seleccionados, incluidos secundarios.
   Efecto neto benigno (idempotente), pero no te sorprendas en los logs.
6. **A/B de modelos**: usar `tools/comparar_ia.py` (caché aislada + mismos jobs).

## Estado de referencia (2026-10-25, tras pase con Gemma)

- Modelo local canónico: **Gemma 4 E2B QAT** (8k ctx, MTP draft, puerto 8081).
  Qwen 3.5 2B descartado: ~6.5x más lento y llena mucho menos (ver
  `docs/IA_ENRIQUECIMIENTO.md`).
- BD: 175 jobs. Huecos restantes: skills 0, languages 4, city 24, region 13,
  work_mode 'unknown' 23, salary sin texto en fuente 82 (no extraíble).
- Perfil: 48 facts (editor CV en `ui_perfil.py`; datos personales y resumen
  reales ya cargados; `value_code='portfolio'` reservado).
