# AGENTS.md — JobPulse CR

> **⛔ NO MODIFICAR — CONGELADO.** El desarrollo activo vive en el fork
> **`v1_5/`**, con su propio `AGENTS.md`, su venv y sus comandos (leer
> `v1_5/AGENTS.md` antes de trabajar). El proyecto principal de la raíz no se
> toca: ni su código, ni sus docs, ni este archivo. Todo lo que sigue aplica
> SOLO al proyecto de la raíz; dentro de `v1_5/` NO vale.

Proyecto **público** (`giovar24/jobpulse-cr`), 100% local y en **Windows**: scraper de
vacantes de Costa Rica en Python 3.11+, embeddings en CPU, SQLite y UI Tkinter.
Toda la documentación (README, bitácora, comentarios, identificadores) está en **español**.

## Antes de tocar código
- Leer la **Guía del agente** al inicio de `DEVELOPMENT_LOG.md` (cómo funciona el
  proyecto, persistencia entre corridas, mapa de archivos). Es la fuente de verdad
  para agentes; `README.md` es para humanos.
- **Regla de oro**: si cambia el *cómo funciona* (flujo, archivos clave, comandos,
  convenciones), actualizar la Guía del agente + (si aplica) `README.md` + una
  entrada de bitácora, **todo en el mismo commit**.
- Bitácora: entradas `## N. título (fecha, hora)` **al final** de
  `DEVELOPMENT_LOG.md`, serie crece sin renumerar. No dupliques estado ahí: mirar
  la última entrada y `git log --oneline -5` antes de asumir nada.

## Entorno (Windows + PowerShell 5.1)
- Venv: `.venv\Scripts\python.exe`. No hay `pyproject.toml`; deps en `requirements.txt`.
- PowerShell 5.1 **no tiene `&&`** (usar `;`) y el quoting de `python -c` es frágil:
  preferir scripts temporales `_tmp_*.py` y **borrarlos al terminar**.
- Consola con acentos mojibake: ejecutar con `-X utf8` (los archivos sí son UTF-8).
- `run.bat` solo delega en `jobpulse.cmd` (que abre la UI con `pythonw`, sin scraping).

## Verificación (cierre de tarea)
```powershell
# 1. Compilar todos los .py (excluir .venv y data/)
# 2. python main.py --preflight   → exit 0, plan de corrida completo, SIN red ni navegador
# 3. python -m unittest discover -s tests   → sin red ni Ollama
# 4. python tools/auditar.py      → auditoría solo-lectura; exit != 0 si hay HTML residual,
#                                    entidades, fechas inválidas o duplicados sin marcar
```
- Los tests no tocan embeddings ni navegador: `--preflight` es el diagnóstico
  seguro sin efectos.

## Privacidad (repo público — regla dura)
- **Nunca commitear**: `.env`, `data/usuario/` (perfil/CV), `data/profile.json`,
  credenciales, ni datos personales. Los ejemplos usan placeholders (`yo@x.com`).
- `data/empresas/*.json` (credenciales, catálogos de ATS) están **sin trackear a
  propósito**: son locales y regenerables; los cambios ahí NO viajan a git.
- La IA externa (`--ia`, `--ia-cv`) es lo único que envía texto fuera de la máquina,
  y solo cuando se invoca. **Nunca corre dentro de una corrida normal.**
- Al cerrar: revisar el diff por datos personales antes de commit/push; árbol limpio
  salvo `data/empresas/`. Mensajes estilo `Bitacora N: ...`.

## Arquitectura mínima (lo no obvio)
- **Providers en 2 grupos** (`providers/source_groups.py`): `boards/` = agregadores
  (indeed, linkedin, ane, procomer, cinde) y `directos/` = ATS del employer
  (workday, phenom, greenhouse, tp, icims, smartrecruiters, successfactors,
  rippling, ats_ligeros, login). `core/dedupe.py` da prioridad al **directo**:
  la copia del board se descarta porque los boards **re-publican** puestos del ATS propio.
- **DB** (`data/jobs.db`, 3 tablas): `jobs_raw` (staging crudo + gate) → `jobs`
  (solo aprobados por `core/gate.py`: fecha **Y** score semántico **Y** dominio del
  título) y `jobs_excluidas` (tombstones: lo excluido NO resucita aunque el board
  lo republique). Lo crudo se ACUMULA por upsert; solo `--fresh`/poda/exclusiones borran.
- **Limpieza en la frontera**: todo lo persistido pasa por
  `core.text.limpiar_texto` dentro de `save_jobs` — nada crudo entra a la base
  aunque un provider falle. Los providers nuevos deben vivir con esta regla.
- **Navegador real**: los providers que lo necesitan abren el **Brave del usuario
  por CDP** (`core/browser_manager.py`, mata `brave.exe` previo). Regla "sesión
  primero" (Indeed/LinkedIn/login_sites): reutilizar sesión del navegador; el
  auto-login opt-in solo si NO hay sesión y máx 1 vez por corrida.
- El **scraper se lanza solo desde la pestaña "Corrida" de la UI o
  `jobpulse.cmd --correr`**: nunca corre solo ni desde la UI automáticamente; la UI
  solo observa `data/corrida_estado.json`.
- `app.py` (Streamlit) es **LEGADO**; la UI actual es `ui.py` (Tkinter, stdlib).
