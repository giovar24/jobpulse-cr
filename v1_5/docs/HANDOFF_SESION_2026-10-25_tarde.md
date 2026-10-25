# Handoff — JobPulse CR v1.5 — Sesión 2026-10-25 (tarde)

Documento para retomar el trabajo en una sesión nueva con contexto limpio.
Reemplaza como handoff vigente al de la mañana de hoy
(`HANDOFF_SESION_2026-10-25.md`, que quedó con O1 cerrado).

## TL;DR

- **Modelo local canónico: Gemma 4 E2B QAT** (reemplaza a Qwen 3.5 2B). A/B y
  pase real: ~6.5x más rápido y llena mucho más (skills 100%, languages 97.7%).
- **O6 primera pieza entregada**: editor de perfil con forma de CV
  (harvard-ats-v1, 9 pestañas) + asistente de importación (CV y JSON) con
  revisión por fila + migración 007 (contact/summary/project). Probado EN VIVO
  sobre la BD real: datos personales y resumen ya cargados (48 facts).
- **AGENTS.md creado** — brújula de agentes, ya se inyecta automáticamente
  en cada sesión (verificado).
- Siguiente trabajo acordado: **importación real de los 4 CVs** y luego
  **O6.2 capa IA para huecos del perfil**.

## 1. Lo que se hizo en esta sesión

### Modelo IA local (Gemma 4 E2B QAT)
- **A/B Qwen vs Gemma** sobre 5 jobs en crudo, misma caché vacía
  (`tools/comparar_ia.py`, caché aislada con `JOBPULSE_IA_CACHE`):
  Gemma 50.9s/11 campos vs Qwen 90.0s/2 campos.
- **Trampa descubierta**: la caché en disco (`data/usuario/ia_cache/`) responde
  instantáneo si (job+modelo+prompt) ya se pidió — Qwen "respondía" en 3.1s
  pegando al pase anterior. Todo benchmark debe aislar caché.
- **Pase completo con Gemma** (175 jobs): 10.3 min (Qwen ~67 min). Fills:
  skills 15→0, languages 135→4, city 28→24, region 14→13.
- `start_ia_server.py` actualizado: comando real del usuario (modelo
  `gemma-4-E2B-it-qat-UD-Q4_K_XL` + mmproj + **MTP draft** `--spec-type
  draft-mtp`, **8k ctx**, KV q8_0, `--metrics`). `.env`:
  `IA_MODEL=gemma4-e2b-qat`.
- **`ia-server.bat`** creado y probado (detecta apagado, arranca, espera
  health; log en `data/ia_server.log`).
- Decisión de contexto: **8k es el punto dulce** (pipeline ≤ ~1.9k tokens por
  llamada; 16k desperdiciaba VRAM de KV). Subir a 16k solo si O6 manda CVs
  completos al prompt.
- `/metrics` requiere `--metrics` (ya va en el comando).

### O6 — Sistema de perfiles (primera pieza)
- **Decisiones acordadas con el usuario**: arquitectura **singleton + swap**
  (no profile_id real por ahora) y primer entregable = **asistente de
  importación UI**.
- **`src/jobpulse_v15/profile_model.py`** (nuevo): puente harvard-ats-v1 ↔
  facts. Secciones en orden de CV Harvard; builders (contacto, resumen,
  educación, experiencia con logros, proyectos, skills, certificaciones,
  idiomas), parsers de ida y vuelta para editar logros/emisor/rol,
  `facts_from_harvard_json()` (importa TODAS las secciones, la migración vieja
  ignoraba contacto/resumen/proyectos) y `suggestion_to_fact()`.
- **Migración 007** (`migrations/007_candidate_profile_harvard_sections.sql`):
  el CHECK de categorías ahora incluye `contact/summary/project` (SQLite no
  permite ALTERar CHECK → recreación preservando datos). Aplicada a la BD real
  (user_version 7, 42 facts intactos al migrar).
- **`src/jobpulse_v15/ui_perfil.py`** (nuevo): `CandidateProfileWindowCV` con
  9 pestañas (Datos personales, Resumen, Educación, Experiencia con logros,
  Proyectos, Habilidades, Certificaciones, Idiomas, Otros-matching) +
  `_DialogoRevision` (aceptar/editar/descartar fila por fila; todo import es
  revertible por snapshot). `LinkedIn` y `Portfolio` separados
  (`value_code='portfolio'`, listo para el link futuro — commit cdd3846).
- **Código muerto eliminado**: editor plano viejo + su ventana de import
  (~691 líneas de ui.py) y sus 6 tests migrados al editor nuevo (7 tests).
  `test_ui.py` SÍ corre en esta máquina (hay display) — el handoff anterior
  lo excluía por error de entorno.
- **Smoke en vivo sobre la BD real** (script en temp): importó datos
  personales + resumen reales desde `data/usuario/profile.json` (+6 facts),
  creó/eliminó una experiencia de prueba vía UI, todo consistente.
- El matching (`compare_job_to_candidate_profile`) no cambió: contact/
  summary/project son inertes para él.

### Documentación y ops
- **`AGENTS.md`** (nuevo, en v1_5): convención de handoffs, reglas del
  proyecto, comandos, 6 gotchas, estado de referencia. OpenCode lo inyecta
  automáticamente (verificado en esta misma sesión). Incluye sección de
  scrapers ANE/Indeed/LinkedIn apuntando a `docs/SITIOS_PROBLEMATICOS.md`.
- `DESARROLLO_v15.md`: convención de sesiones al inicio + §6 nota 7
  (Gemma canónico + warning de caché).
- `docs/IA_ENRIQUECIMIENTO.md`: sección completa del A/B + datos viejos de
  Qwen corregidos (arranque, .env).
- Herramientas nuevas: `tools/comparar_ia.py`, `tools/progreso_enriquecimiento.py`
  (% por campo), `tools/huecos_restantes.py` (clasifica basura vs recuperable).
- `jobpulse-v15.ps1` arreglado (O5.1): rutas de Brave genéricas (Program Files
  preferido; `User Data` raíz, nunca `User Data\Default`).

### API key Gemini (opcional)
- Key de AI Studio aportada por el usuario, guardada en `.env` (gitignoreado):
  `GEMINI_API_KEY=...`. Para activar: `IA_PROVEEDOR=google` (el preset lee esa
  var). **LLM gratis en AI Studio según el usuario: gemma4 26b** (verificar id
  exacto al activar). La key se pegó en chat — rotar si se comparten logs.

## 2. Decisiones de diseño (acordadas con el usuario)

1. **Perfil = singleton + swap** (imports revertibles). Si algún día se
   necesita alternar perfiles activos: añadir `profile_id` puntualmente.
2. **Editor con forma de CV** (estética harvard-ats-v1), datos personales
   primero, logros por línea en experiencia. Backend sigue siendo facts.
3. **LinkedIn y Portfolio separados** (portfolio futuro).
4. **Contexto del servidor IA local: 8k**.
5. **Benchmarks SIEMPRE con caché aislada** (`JOBPULSE_IA_CACHE`).

## 3. Objetivos (estado)

| Objetivo | Estado |
|---|---|
| O1 Enriquecimiento IA | ✅ completo (pase con Gemma hecho; ver huecos abajo) |
| O2 ATS + gate + ranking en la búsqueda UI | ⏳ no iniciado |
| O3 Validar 7 fetchers ATS huérfanos | ⏳ no iniciado |
| O4 Calibración gate-vs-IA | ⏳ depende de O1+O2 |
| O5 Higiene | 🟡 parcial: O5.1 (.ps1) ✅; queda re-barrido de ~14-19 jobs basura |
| **O6 Perfiles del candidato** | 🟢 **EN CURSO**: editor CV + asistente ✅; sigue capa IA y matching |

## 4. Pendiente inmediato en la próxima sesión

1. **Importación real de CVs** (quedó a punto de arrancar): 4 CVs en
   `data/usuario/cv/` (`Giovanni_Vargas_CV_Soporte_Tecnico_ES.docx` 28.5KB,
   `..._ES.pdf` 206KB, `..._EN.pdf` 209KB, `Technical Support.pdf` 123KB).
   Correr `extract_candidate_profile` en modo lectura, mostrar sugerencias con
   evidencia, comparar vs los 23 skills ya en BD y decidir qué importar
   (desde UI o con el diálogo automatizado).
2. **O6.2 — Capa IA para huecos del perfil** (patrón `ia_enrichment`,
   `origin='local_ai'`): completar skills con nivel, idiomas CEFR faltantes,
   autorización de trabajo, disponibilidad. Motor: Gemma local o
   Gemini/gemma4-26b con la key del .env.
3. **O6.3 — Matching afinado**: usar niveles/CEFR/work_authorization en
   `compare_job_to_candidate_profile`.
4. O5.3: re-barrido de los ~14-19 jobs con descripción basura (login-wall).

## 5. Cómo retomar (comandos)

```powershell
cd D:\Proyectos\jobpulse_cr\v1_5

# Servidor IA local (Gemma 4 E2B, puerto 8081) — verificar/arrancar
.\ia-server.bat                 # o: .\.venv\Scripts\python.exe start_ia_server.py

# Pase de enriquecimiento (idempotente)
.\.venv\Scripts\python.exe run_enriquecimiento.py

# Diagnóstico
.\.venv\Scripts\python.exe tools\estado_bd.py
.\.venv\Scripts\python.exe tools\progreso_enriquecimiento.py
.\.venv\Scripts\python.exe tools\huecos_restantes.py

# Suite COMPLETA (test_ui incluido — corre OK en este PC)
python -m pytest tests/ -q

# UI (Mi perfil = nuevo editor CV)
.\.venv\Scripts\python.exe -m jobpulse_v15.ui
```

## 6. Archivos clave

| Archivo | Qué es |
|---|---|
| `src/jobpulse_v15/ui_perfil.py` | Editor CV (CandidateProfileWindowCV) + diálogo revisión (_DialogoRevision) |
| `src/jobpulse_v15/profile_model.py` | Puente harvard-ats-v1 ↔ facts (builders/parsers/import JSON) |
| `src/jobpulse_v15/migrations/007_...sql` | Categorías contact/summary/project en facts |
| `src/jobpulse_v15/ia_api.py` / `ia_enrichment.py` | Cliente IA + pipeline enriquecimiento |
| `start_ia_server.py` / `ia-server.bat` | Arranque del servidor local (comando real del usuario) |
| `tools/comparar_ia.py` | A/B de modelos con caché aislada |
| `docs/IA_ENRIQUECIMIENTO.md` | Operación + A/B Gemma vs Qwen |
| `docs/SITIOS_PROBLEMATICOS.md` | Guía ANE/Indeed/LinkedIn |
| `AGENTS.md` | Brújula de agentes (leído automáticamente) |
| `.env` | IA_MODEL=gemma4-e2b-qat + GEMINI_API_KEY (gitignoreado) |

## 7. Datos de contexto que no hay que olvidar

- **BD**: `data/jobpulse_v15.db` — 175 jobs (ANE 120, Indeed 30, CINDE 17,
  LinkedIn 7, Procomer 1); schema_version **7**.
- **Perfil**: 48 facts — skill 23, experience 9, certification 6, contact 5,
  education 2, language 2, summary 1. Los facts 43-48 son los datos personales
  + resumen importados hoy. Portfolio sin asignar aún (a futuro).
- **Huecos restantes** (irrecuperables u honestos): city 24 (mayoría
  "Virtual/Remote"/LATAM), region 13, work_mode 'unknown' 23 (el texto no lo
  dice), languages 4, salary 82 sin texto en la fuente (no extraíble).
- **Commits de la sesión**: `625b429` (gemma+AGENTS+tools+ps1) → `2bbbbab`
  (editor CV + migración 007) → `726b0da` (limpieza 841 líneas + tests) →
  `cdd3846` (LinkedIn/Portfolio separados) → este handoff.
- **Tests**: 200 + 46 subtests pasando (test_ui incluido).
- **Gotcha recordado**: la caché IA en disco falsea benchmarks — aislarla con
  `JOBPULSE_IA_CACHE` (lo hace `tools/comparar_ia.py`).
