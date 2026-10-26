# Handoff — JobPulse CR v1.5 — Sesión 2026-10-25 (noche)

Reemplaza como handoff vigente al de la tarde
(`HANDOFF_SESION_2026-10-25_tarde.md`).

## TL;DR

- **Instalador IA para equipo nuevo**: `instalar-modelo-ia.bat` /
  `instalar_modelo_ia.py` — descarga el modelo desde **Unsloth (Hugging Face)**
  con SHA256 verificados, instala `huggingface_hub` (pip) y llama.cpp Vulkan si
  falta. Idempotente, reanuda descargas, rutas configurables en `.env`.
  Verificado en vivo contra la instalación real (hashes OK).
- **Capa IA para CVs (O6.2 primera pieza)**: `src/jobpulse_v15/cv_ia.py`
  convierte CV (PDF/DOCX/TXT) → harvard-ats-v1 con el motor del `.env`.
  **Probado E2E con 2 motores (Gemma local + Gemini) × 4 formatos**: ambos
  extraen 39 facts consistentes; TXT validado vía fixture (caché instantánea
  confirma que el texto es idéntico al DOCX).
- **Import + revert probado sobre la BD real**: import id=3 (+26 nuevos, 13
  updates) y `revert_last_candidate_profile_import` dejó la BD EXACTA
  (48 facts). Suite completa: **214 passed + 46 subtests**.
- Los 4 CVs son el mismo contenido (ES/EN) → quedan como **material de
  pruebas**; el perfil real vive en BD (48 facts de profile.json).
- Siguiente trabajo acordado: integrar el flujo IA al asistente de import de
  la UI (hoy el asistente usa solo el extractor de reglas) y luego O6.3.

## 1. Lo que se hizo

### Instalador IA (equipo nuevo, requisitos mínimos)
- `src/jobpulse_v15/instalacion_ia.py` (lógica) + `instalar_modelo_ia.py`
  (CLI) + `instalar-modelo-ia.bat` (wrapper doble-clic).
- 3 pasos con verificación previa: (1) `huggingface_hub` pip si falta,
  (2) modelo desde `unsloth/gemma-4-E2B-it-qat-GGUF` — main 2.62 GB +
  mmproj-BF16 987 MB + MTP Q4_0 59 MB (subcarpeta `MTP/` se aplana), hashes
  SHA256 fijados y verificados contra la instalación local byte a byte,
  (3) llama.cpp build Vulkan solo si no existe (zip con reanudación Range).
- `--check` solo verifica (imprime requisitos mínimos: Windows x64, GPU Vulkan
  ≥4 GB VRAM o CPU, ~5 GB disco, Python 3.11+ con `pip install -e .[ia]`).
- `start_ia_server.py` ahora comparte rutas con el instalador
  (`IA_MODEL_DIR` / `IA_LLAMA_SERVER` en `.env`, defaults = las del usuario).
- pyproject: `huggingface_hub>=0.34` declarado en el extra `ia`.
- Tests: `tests/test_instalacion_ia.py` (7, offline).

### Capa IA para CVs (cv_ia.py)
- `extraer_cv_ia(texto)` async (patrón ia_enrichment) + `sanea_harvard`
  (tolerante: cv ausente, strings sueltos en secciones → van al campo
  identificador, escalares → texto, anios "14"/"no número" → float/None).
- Prompt exige NO inventar (null si falta), fechas tal cual, CEFR solo si se
  declara, `busqueda.overrides.experiencia_anios` solo si es textual.
- Refactor `profile_model.facts_desde_perfil_dict()`: el cuerpo de
  `facts_from_harvard_json` ahora acepta dict en memoria (para la salida IA)
  sin pasar por archivo; equivalencia probada dict vs JSON.
- **E2E (script temp, solo lectura)**: 8 corridas —
  | Motor | DOCX | PDF ES | PDF EN | TXT |
  |---|---|---|---|---|
  | gemma4-e2b-qat local | 114.8s / 39 facts | 108.5s / 39 | 90.8s / 39 | 0.0s (caché) |
  | gemini-2.5-flash | 80.9s / 39 | 81.8s / 39 | 71.4s / 39 | 0.0s (caché) |
- Reparto idéntico en ambos: contact 5, summary 1, education 3, experience 7,
  skill 19, certification 2, language 2. El PDF EN detecta la sección idiomas.
- JSONs de cada corrida en temp (`cv_ia_json/*.json`) para inspección.

### Prueba de import + revert (BD real)
- Fixture TXT generado: `data/usuario/cv/Giovanni_Vargas_CV_Soporte_Tecnico_ES.txt`
  (mismo texto que el DOCX → probó el camino `.txt` y demostró la caché).
- `import_candidate_profile_facts("prueba-cv-ia (docx, gemma-local)", 39)` →
  id=3, BD 48→74 (26 nuevos + 13 updates con snapshot).
- `revert_last_candidate_profile_import()` → restored 13, removed 26 →
  **BD exactamente como antes** (48 facts, conteo por categoría igual).

### Decisión de alcance
- Los 4 CVs (mismo CV en ES y EN) = material de pruebas; NO se importan como
  datos reales (el perfil ya está completo desde profile.json).

## 2. Decisiones de diseño

1. **Instalador = proyecto, no script personal**: lógica en el paquete
   (`instalacion_ia`), CLI raíz delgado, rutas por `.env` con defaults
   (fuente única compartida con `start_ia_server.py`).
2. **Unsloth como fuente del modelo** (decisión del usuario: "los más
   confiables"); hashes SHA256 fijados en código y verificables con `--check`.
3. **cv_ia es agnóstico al motor**: funciona con local (privacidad, sin tope)
   o Gemini (mejor calidad, CV viaja a la nube, tope opt-in). Cambio por .env.
4. **El modelo no inventa + saneo tolerante + revisión UI**: tres capas antes
   de escribir en BD.
5. **Los CVs son solo pruebas**; el perfil real es el de la BD.

## 3. Objetivos (estado)

| Objetivo | Estado |
|---|---|
| O1 Enriquecimiento IA | ✅ completo (Gemma canónico) |
| O2 ATS + gate + ranking en la búsqueda UI | ⏳ no iniciado |
| O3 Validar 7 fetchers ATS huérfanos | ⏳ no iniciado |
| O4 Calibración gate-vs-IA | ⏳ depende de O1+O2 |
| O5 Higiene | 🟡 O5.1 ✅; queda re-barrido de ~14-19 jobs basura (O5.3) |
| **O6 Perfiles del candidato** | 🟢 editor CV + import reglas ✅ (tarde); **capa IA de CVs ✅ probada E2E (noche)**; sigue: integración al asistente UI + O6.3 matching |

## 4. Pendiente inmediato en la próxima sesión

1. **Integrar `extraer_cv_ia` al asistente de import de la UI**
   (`ui_perfil.py::_importar_cv`): hoy ese botón usa solo el extractor de
   reglas (`extract_candidate_profile`), que de CVs saca casi nada. Propuesta:
   probar reglas primero; si sugiere poco (< umbral), ofrecer el pase IA
   (con aviso de privacidad si el motor es externo). Los facts ya llegan
   listos para `_DialogoRevision`.
2. O6.3: matching afinado (niveles/CEFR/work_authorization).
3. Dedupe semántico opcional: slugs ES/EN distintos para el mismo hecho
   (p.ej. `auxis-analista-de-soporte-de-ti-y-aplicaciones` vs
   `auxis-it-support-application-support-analyst`) — decidir si se normaliza
   en import o se deja al usuario en el diálogo.
4. O5.3: re-barrido de los ~14-19 jobs con descripción basura.

## 5. Cómo retomar (comandos)

```powershell
cd D:\Proyectos\jobpulse_cr\v1_5

# Servidor IA local (Gemma 4 E2B, puerto 8081) — verificar/arrancar
.\ia-server.bat

# Probar la capa IA de CVs (solo lectura; 2 motores x 4 formatos)
.\.venv\Scripts\python.exe C:\Users\giova\AppData\Local\Temp\opencode\prueba_cv_ia_e2e.py

# Probar import + revert (BD real; queda intacta)
.\.venv\Scripts\python.exe C:\Users\giova\AppData\Local\Temp\opencode\prueba_import_revert.py

# Instalador (equipo nuevo / verificación)
.\instalar-modelo-ia.bat --check

# Suite COMPLETA (214 tests)
python -m pytest tests/ -q

# UI (Mi perfil → Importar CV/JSON)
.\.venv\Scripts\python.exe -m jobpulse_v15.ui
```

## 6. Archivos clave

| Archivo | Qué es |
|---|---|
| `src/jobpulse_v15/cv_ia.py` | NUEVO: CV texto → harvard-ats-v1 (prompt + saneo + extraer_cv_ia async) |
| `src/jobpulse_v15/instalacion_ia.py` | NUEVO: requisitos mínimos IA (modelo Unsloth + llama.cpp + deps) |
| `instalar_modelo_ia.py` / `instalar-modelo-ia.bat` | CLI + wrapper del instalador |
| `src/jobpulse_v15/profile_model.py` | REFACTORIZADO: `facts_desde_perfil_dict` (dict en memoria) |
| `start_ia_server.py` | Rutas ahora por `.env` (IA_MODEL_DIR / IA_LLAMA_SERVER) |
| `tests/test_cv_ia.py` / `tests/test_instalacion_ia.py` | 8 + 7 tests nuevos (offline) |
| `docs/IA_ENRIQUECIMIENTO.md` | Secciones: instalación equipo nuevo + extracción CV IA |

## 7. Datos de contexto que no hay que olvidar

> **Addendum 2026–10–26**: el pendiente inmediato #1 (integrar `extraer_cv_ia`
> al asistente de import de la UI) quedó hecho en `ui_perfil.py`: reglas
> primero; si sugieren < 5 facts se ofrece el pase IA (progreso no-modal en
> hilo + marshaling por `after(60)` poll del main thread — Tcl rechaza
> `after`/`createcommand` cross-thread si mainloop no corre, el patrón
> `window.after(...)` desde el hilo worker de `configuration_window.py` NO
> sirve para tests). Suite 217 tests: mis módulos OK; la clase flaky
> `TestSourceManagerWindow` (hilos + `after` cross-thread en `ui.py`)
> falla intermitente preexistente, pendiente de estabilizar.
  (skill 23, experience 9, certification 6, contact 5, education 2, language 2,
  summary 1). Tras las pruebas quedó EXACTA (revert verificado).
- **CVs de prueba** (`data/usuario/cv/`): DOCX ES, PDF ES, PDF EN y TXT
  (generado). Mismo contenido; el EN trae sección idiomas y nombres de puesto
  en inglés (slugs distintos).
- **Caché IA**: claves por (proveedor+modelo+mensajes) — el TXT respondió 0.0s
  por texto idéntico al DOCX. En benchmarks aislar con `JOBPULSE_IA_CACHE`.
- **Gemini**: key en `.env` (GEMINI_API_KEY); al instanciar para google pasar
  `modelo` explícito (`IA_MODEL` del .env es el local). Extracción ~80s/CV.
- **Gemma local**: ~90-115s por CV (8k ctx; texto ~1k tokens, salida ~1.5-2k).
- **Commits de la sesión**: `9d376e5` (instalador) → este (cv_ia + handoff).
- **Tests**: 214 + 46 subtests pasando.
