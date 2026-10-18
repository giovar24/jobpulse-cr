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
- **Analizador de CV** (`cv2profile.py`): CV (PDF/DOCX/TXT) → perfil
  (`data/profile.json`): títulos semánticos, skills, idiomas con nivel,
  años de experiencia, educación, certificaciones y contacto.
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
```

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
- `.env` está en `.gitignore`: tus tokens nunca se suben al repo.
- `data/jobs.db` y los caches de embeddings se regeneran con `python main.py`.
- Cambio de equipo: clona + `pip install -r requirements.txt` y arranca; el
  bootstrap crea los archivos faltantes automáticamente.
