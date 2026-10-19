# 💼 JobPulse CR

Buscador de empleos **100% local** para Costa Rica: scrapea vacantes de los
principales portales, las indexa con embeddings en tu propia CPU y las rankea
contra tu perfil de candidato con un **score tipo ATS** (Applicant Tracking
System). Sin APIs de pago, sin nube, sin LLMs.

## Características

- **Scraper** (`main.py`): Indeed, LinkedIn, ANE y Procomer → `data/jobs.db`
  (SQLite). Temáticas de búsqueda derivadas del perfil del candidato y
  expandidas semánticamente (ES↔EN).
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
- **Dashboard Streamlit** (`app.py`): ranking de mejores opciones con
  desglose ATS + búsqueda libre, todo local.

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

# 5. Dashboard
streamlit run app.py

# 6. Diagnosticar la extraccion de un CV (texto, regex, IA, revision, perfil)
python reporte_cv.py "MiCV.pdf"

# 7. Benchmark de modelos de embeddings (compara candidatos locales)
python bench_modelos.py
```

### Corrida con un clic y recordatorio (Windows)

- **`jobpulse.cmd`** — doble clic: scraping completo + dashboard + navegador.
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

## Estructura

```
core/
  search_engine.py   # motor de embeddings + cache incremental
  ats.py             # score ATS ponderado + keywords faltantes
  cv_analysis.py     # extractores de CV (regex + embeddings)
  profile.py         # CandidateProfile (fuente única de verdad)
  database.py        # SQLite de vacantes
providers/           # scrapers por portal
data/profile.json    # perfil del candidato
main.py · search.py · cv2profile.py · app.py
```

## Notas

- Todo corre en **CPU local**; nada de tu perfil o CV sale de tu máquina.
- Modelo de embeddings elegido por benchmark (`bench_modelos.py`, 16 tareas
  reales): gana `paraphrase-multilingual-mpnet-base-v2` (16/16) frente a
  `e5-small` y `MiniLM` multilingües.
- `.env` está en `.gitignore`: tus tokens nunca se suben al repo.
- `data/jobs.db` y los caches de embeddings se regeneran con `python main.py`.
- Cambio de equipo: clona + `pip install -r requirements.txt` y arranca; el
  bootstrap crea los archivos faltantes automáticamente.
