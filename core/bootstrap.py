"""Arranque automatico del workspace: funciona en cualquier equipo, sin pasos manuales.

Filosofia: clonar el repo + instalar requirements es TODO lo necesario. Cada
entrypoint (main.py, search.py, app.py, cv2profile.py, ui.py) llama
`ensure_workspace()` al inicio y este crea lo que falte:

  1. `data/`                      -> datos del scraper (jobs.db, reportes...).
  2. `data/usuario/`              -> datos personales: perfil, backups, IA del CV
                                     y `data/usuario/cv/` para los CV importados.
                                     Si existen archivos del layout viejo
                                     (data/profile.json, data/cv_ia.*) se mueven.
  3. `data/usuario/profile.json`  -> copiado desde `data/profile.example.json`
                                     (perfil GENERICO de ejemplo, datos falsos).
  4. `.env`                       -> copiado desde `.env.example` (placeholders;
                                     el usuario agrega su HF_TOKEN si quiere).

Privacidad: `data/usuario/` y `.env` estan en `.gitignore`. El example SIEMPRE
lleva datos genericos (Juan Perez, ejemplos.com) -> aunque alguien clone el repo,
nunca recibe - ni expone - datos reales de ningun candidato.
"""
import shutil
from typing import Dict

from core.rutas import (BASE_DIR, DATA_DIR, PROFILE_EXAMPLE, PROFILE_PATH,
                        asegurar_dirs, migrar_legado)

ENV_EXAMPLE = BASE_DIR / ".env.example"
ENV_PATH = BASE_DIR / ".env"


def ensure_workspace(quiet: bool = True) -> Dict[str, bool]:
    """Crea directorios y archivos faltantes. Devuelve que se creo:
    {"data_dir": bool, "profile": bool, "env": bool}. Idempotente."""
    created = {"data_dir": False, "profile": False, "env": False}

    if not DATA_DIR.exists():
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        created["data_dir"] = True

    # Folder exclusivo de datos del usuario (perfil, backups, CVs importados).
    asegurar_dirs()
    migrar_legado()

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
            "data_dir": "  + data/ (directorio de trabajo del scraper)",
            "profile": f"  + {PROFILE_PATH.relative_to(BASE_DIR)} (copiado del "
                       f"generico {PROFILE_EXAMPLE.name}: editalo o genera el "
                       f"tuyo con cv2profile.py)",
            "env": "  + .env (copiado de .env.example: agrega HF_TOKEN si "
                   "quieres EmbeddingGemma)",
        }
        for k, v in msgs.items():
            if created[k]:
                print(v)
        if created["profile"] or created["env"]:
            print("  (archivos creados automaticamente; no se suben a git)")
    return created
