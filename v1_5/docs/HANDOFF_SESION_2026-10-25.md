# Handoff — JobPulse CR v1.5 — Sesión 2026-10-25

Documento para retomar el trabajo en una sesión nueva con contexto limpio.
Estado exacto del proyecto al cerrar esta sesión.

## TL;DR

- **O1 (Enriquecimiento IA) quedó operativo**: servidor auto-arrancable, pipeline robusto, límite de llamadas opt-in, 11 tests nuevos con IA simulada.
- **El pase completo está CORRIENDO en segundo plano** al momento de cerrar esta sesión (shell `sh_173e5d0d900186rND50gL0vxu5`). Ver resultado con `python estado_bd.py` (faltan city/work_mode/skills) o releer el log de ese shell.
- Siguiente trabajo acordado con el usuario: **O6 — sistema de perfiles del candidato** (varios perfiles, tipos de documento, qué se extrae, cómo se guarda).

## 1. Lo que se hizo en esta sesión

### Fixes de `ia_api.py` (cliente IA)
- **El límite de llamadas era `max_llamadas=12` por defecto** — era la causa raíz de las 158 fallas del pase anterior (junto al server apagado). Un pase de 175 jobs quemaba 12 llamadas y el resto fallaba al instante.
- **Nuevo comportamiento (decisión del usuario)**: **sin límite por defecto, local o externo**. El límite es **manual/opt-in**:
  - `.env`: `IA_MAX_LLAMADAS` (tope de llamadas por corrida) y `IA_RPM` (rate limit). Ninguno definido = sin tope.
  - Código: pasar `IAClient(max_llamadas=N, rpm=N)` explícito gana siempre.
  - En `.env` quedaron documentados como comentarios para quien use una API de pago.
- Guarda de compatibilidad: `achat` solo valida el tope si `max_llamadas is not None`.

### Fixes de `ia_enrichment.py` (pipeline)
- **Preflight** (`verificar_servidor_ia`): aborta el pase de inmediato con error claro si el servidor no responde (antes: avalancha de fallos job por job).
- **Reintentos por campo**: 3 intentos con backoff 1s/2s (antes 2 intentos, 0.5s).
- **Bug**: `only_critical=False` filtraba con `needs_critical_enrichment` y **nunca veía** jobs que solo faltaban campos secundarios → el "pase completo" era imposible. Ahora: críticos OR secundarios (`needs_secondary_enrichment`).
- **Bug de guardado**: el UPDATE reescribía TODAS las columnas → skills/languages quedaban con el string `'null'` y salary_max/currency/period se perdían. Ahora UPDATE dinámico **solo de columnas tocadas** (`MAPEO_SAVE`).
- `skills`: max_tokens 300→600 + **prompt endurecido** (solo skills literales del texto, máx 10, lista vacía si no hay; el modelo 2B inventaba 20+ buzzwords y truncaba el JSON).
- Log por lote para seguir progreso.

### Operación (scripts nuevos en raíz v1_5)
- `start_ia_server.py` — verifica health del servidor IA; si está apagado lo **arranca automáticamente** con el comando real del usuario (build Vulkan `D:\llama-b11662-bin-win-vulkan-x64`, modelo `D:\Models\Qwen352B\Qwen3.5-2B-UD-Q8_K_XL.gguf`, puerto 8081), en proceso separado, log en `data/ia_server.log`. `--check` = solo verificar. **Soporta servidor externo**: si `IA_BASE_URL` no es local, no intenta arrancar nada.
- `run_enriquecimiento.py` — pase crítico en ciclos (para cuando deja de avanzar) + pase completo con secundarios. `--solo-criticos` para omitir secundarios.
- `jobpulse-v15.bat` — paso nuevo [2/4]: verifica/arranca servidor IA antes de la UI. Orden: [1] venv, [2] IA, [3] Brave CDP, [4] UI.
- `tools/estado_bd.py` — diagnóstico rápido de la BD (jobs, faltantes por campo, por fuente).
- `tools/ver_sitio.py <patron>` — inspecciona config de un sitio (`python tools/ver_sitio.py %procomer%`).
- `tools/limpiar_null.py` — limpió 5 jobs contaminados con `'null'` legado (3 skills, 2 languages). Ejecutado ✅ (queda por si reaparece).

### Diagnósticos cerrados
- **Procomer no es bug ni cambio de fuente**: está configurado con `search_text="Soporte"` (site_config_json, editable en "Administrar fuentes") y hoy el portal tiene 1 sola oferta con esa palabra. La API reporta 95 con "Costa Rica", 524 sin filtro. **Decisión del usuario**: mantener, el filtro es configuración declarativa por perfil/situación.
- `docs/IA_ENRIQUECIMIENTO.md` — nueva guía: arquitectura, arranque (auto/manual/externo), pase, `.env`, tabla de depuración.

### Tests
- `tests/test_ia_enrichment.py` — 11 tests con IA simulada (FakeIAClient): reintentos, preflight, pase crítico, pase completo con solo-secundarios, salary completo, persistencia, aborto con server apagado.
- **Suite: 138/138 + 46 subtests pasando.**

## 2. Decisiones de diseño (acordadas con el usuario)

1. **Pensar en general, no "para mí"**: nada específico del usuario hardcodeado en código. Perfil = datos en BD; filtros de sitios = site_config declarativo; credenciales = por empresa en Credential Manager.
2. **Límite IA manual**: nunca un tope por defecto; solo si el usuario lo activa (env/args). Aplica igual a local y externo.
3. **Multi-perfil**: por ahora un perfil global con swap via imports revertibles (revertir + importar). Si se necesita alternar perfiles activos: añadir `profile_id` + selector UI (cambio puntual, no construido a propósito).

## 3. Objetivos (estado)

| Objetivo | Estado |
|---|---|
| O1 Enriquecimiento IA | ✅ salvo 1.3: **pase completo corriendo** al cierre (ver abajo) |
| O2 ATS + gate + ranking en la búsqueda UI | ⏳ no iniciado |
| O3 Validar 7 fetchers ATS huérfanos (workday/greenhouse/ashby/lever/recruitee/workable/successfactors — 0 sitios) | ⏳ no iniciado |
| O4 Calibración gate-vs-IA | ⏳ depende de O1+O2 |
| O5 Higiene (`.ps1` roto: rutas viejas Brave+`User Data\Default`; scripts sueltos → `tools/`; re-barrido de ~10 jobs con descripción basura) | ⏳ no iniciado |
| **O6 Sistema de perfiles del candidato** | 🎯 **acordado como siguiente** — borrador abajo |

## 4. O6 — borrador acordado (siguiente sesión)

El usuario confirmó que quiere analizar/crear el sistema de perfiles: pueden ser varios, con distintos tipos de documento de entrada.

1. **Modelo de perfiles**: singleton con swap (revertir/importar) vs `profile_id` + selector en UI.
2. **Fuentes de datos**: tipos de documento — CV PDF/DOCX/TXT (ya soportados por `candidate_profile.py` con límites sanos), JSON harvard-ats-v1, captura manual UI. Posible login/LinkedIn más adelante.
3. **Extracción**: dos capas ya existentes — `RuleBasedExtractor` (analysis.py) genera sugerencias **con evidencia textual**; capa IA (patrón `ia_enrichment.py`) para completar huecos, marcando `origin='local_ai'`.
4. **Qué extraer** (para matching): skills con nivel, idiomas CEFR, títulos objetivo, años de experiencia, autorización de trabajo, disponibilidad/schedule. Aquí caen las recomendaciones de agregar/reducir campos (pendientes de discutir).
5. **Persistencia**: facts con origen (`rule`/`local_ai`/`manual`), snapshots revertibles por importación (ya existe).
6. **UI**: asistente "Mi perfil" — importar documento → sugerencias con evidencia → aceptar/editar/descartar → guardar como manual.
7. **Tests** del pipeline con y sin IA.

## 5. Cómo retomar (comandos)

```powershell
# Verificar el estado del pase que quedó corriendo:
python tools/estado_bd.py            # total jobs, faltan city/work_mode/skills
# (o releer el log del shell sh_173e5d0d900186rND50gL0vxu5 si sigue vivo)

# Servidor IA:
python start_ia_server.py --check   # 0=arriba / 1=apagado
python start_ia_server.py            # verifica y arranca si hace falta

# Re-ejecutar el pase (idempotente, solo procesa lo que falta):
python run_enriquecimiento.py

# Suite de tests:
python -m pytest tests/ -q --ignore=tests/test_ui.py
```

## 6. Archivos clave

| Archivo | Qué es |
|---|---|
| `src/jobpulse_v15/ia_api.py` | Cliente IA OpenAI-compat; límites opt-in (IA_MAX_LLAMADAS/IA_RPM) |
| `src/jobpulse_v15/ia_enrichment.py` | Pipeline de enriquecimiento (preflight, reintentos, guardado selectivo) |
| `src/jobpulse_v15/candidate_profile.py` | Lector de CV (PDF/DOCX/TXT) + sugerencias con evidencia (base de O6) |
| `src/jobpulse_v15/analysis.py` | `RuleBasedExtractor` (extracción por reglas, base de O6) |
| `src/jobpulse_v15/repository.py` | `candidate_profile_facts` / imports / snapshots (líneas ~1900-2250) |
| `start_ia_server.py` / `run_enriquecimiento.py` / `tools/estado_bd.py` | Operación del pase IA |
| `docs/IA_ENRIQUECIMIENTO.md` | Guía de operación y depuración del enriquecimiento |
| `docs/SITIOS_PROBLEMATICOS.md` | Guía ANE/Indeed/LinkedIn (sesión anterior) |
| `.env` | IA_PROVEEDOR/BASE_URL/MODEL/API_KEY + límites opt-in comentados |
| `tests/test_ia_enrichment.py` | Tests del pipeline con IA simulada |

## 7. Datos de contexto que no hay que olvidar

- BD: `data/jobpulse_v15.db` — 175 jobs (ANE 120, Indeed 30, CINDE 17, LinkedIn 7, Procomer 1).
- ~10 jobs (9 ANE + 1 CINDE) con descripción basura de login-wall (30-100 chars): el enriquecimiento NO los recupera; hace falta re-barrido (O5.3).
- Perfil migrado: 42 facts (import 1, revertible) desde `data/usuario/profile.json` (harvard-ats-v1) + CVs en `data/usuario/cv/`.
- Commits de la sesión: `f4c232a` (robustez pipeline + arranque IA) + commit final de cierre (límites opt-in, prompt skills, `tools/`, este handoff).

## 8. Pendiente inmediato en la próxima sesión

1. **Verificar el pase completo terminó bien** (log del shell o `python tools/estado_bd.py`); si quedaron fallas por descripción basura, re-barrido (O5.3).
2. Arrancar **O6** con el borrador de §4 (primera pregunta al usuario: singleton+swap vs profile_id real).

Nota: los fixes de esta sesión (límites opt-in en `ia_api.py`, prompt de skills, scripts `tools/`, este handoff) se commitean AL FINAL de esta sesión — ver historial de git para confirmar.
