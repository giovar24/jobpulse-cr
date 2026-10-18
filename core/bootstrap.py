"""Arranque automatico del workspace: funciona en cualquier equipo, sin pasos manuales.

Filosofia: clonar el repo + instalar requirements es TODO lo necesario. Cada
entrypoint (main.py, search.py, app.py, cv2profile.py) llama `ensure_workspace()`
al inicio y este crea lo que falte:

  1. `data/`                      -> si no existe (junto con jobs.db despues).
  2. `data/profile.json`          -> copiado desde `data/profile.example.json`
                                     (perfil GENERICO de ejemplo, datos falsos).
  3. `.env`                       -> copiado desde `.env.example` (placeholders;
                                     el usuario agrega su HF_TOKEN si quiere).

Privacidad: `data/profile.json` y `.env` estan en `.gitignore`. El example SIEMPRE
lleva datos genericos (Juan Perez, ejemplos.com) -> aunque alguien clone el repo,
nunca recibe - ni expone - datos reales de ningun candidato.
"""
import shutil
from pathlib import Path
from typing import Dict

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
PROFILE_EXAMPLE = DATA_DIR / "profile.example.json"
PROFILE_PATH = DATA_DIR / "profile.json"
ENV_EXAMPLE = BASE_DIR / ".env.example"
ENV_PATH = BASE_DIR / ".env"


def ensure_workspace(quiet: bool = True) -> Dict[str, bool]:
    """Crea directorios y archivos faltantes. Devuelve que se creo:
    {"data_dir": bool, "profile": bool, "env": bool}. Idempotente."""
    created = {"data_dir": False, "profile": False, "env": False}

    if not DATA_DIR.exists():
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        created["data_dir"] = True

    # Perfil: desde la plantilla generica. Si no hay plantilla, no hay nada
    # que copiar (CandidateProfile.load devolvera None y los entrypoints
    # mostraran su mensaje de error).
    if not PROFILE_PATH.exists() and PROFILE_EXAMPLE.exists():
        shutil.copyfile(PROFILE_EXAMPLE, PROFILE_PATH)
        created["profile"] = True

    # Entorno: solo placeholders; nunca tokens reales.
    if not ENV_PATH.exists() and ENV_EXAMPLE.exists():
        shutil.copyfile(ENV_EXAMPLE, ENV_PATH)
        created["env"] = True

    if not quiet:
        msgs = {
            "data_dir": "  + data/ (directorio de trabajo)",
            "profile": f"  + {PROFILE_PATH.name} (copiado del generico "
                       f"{PROFILE_EXAMPLE.name}: editalo o genera el tuyo con "
                       f"cv2profile.py)",
            "env": "  + .env (copiado de .env.example: agrega HF_TOKEN si "
                   "quieres EmbeddingGemma)",
        }
        for k, v in msgs.items():
            if created[k]:
                print(v)
        if created["profile"] or created["env"]:
            print("  (archivos creados automaticamente; no se suben a git)")
    return created
