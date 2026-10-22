# -*- coding: utf-8 -*-
"""Rutas del workspace: datos del usuario separados de los del scraper.

data/                 -> datos del scraper (jobs.db, reportes, empresas, caches, logs)
data/usuario/         -> datos personales del candidato (perfil, backups, IA del CV)
data/usuario/cv/      -> copia local de los CV importados (PDF/DOCX/TXT/MD)

Asi un CV o el perfil real nunca se mezclan con los archivos que genera el
scraper, y borrar/regenerar data/ no obliga a reconstruir el perfil a mano.
"""
import shutil
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
USER_DIR = DATA_DIR / "usuario"
CV_DIR = USER_DIR / "cv"

PROFILE_PATH = USER_DIR / "profile.json"
PROFILE_EXAMPLE = DATA_DIR / "profile.example.json"
CV_IA_JSON = USER_DIR / "cv_ia.json"
CV_IA_MD = USER_DIR / "cv_ia.md"

# Archivos personales del esquema anterior (vivían sueltos en data/).
_LEGACY_FILES = ("profile.json", "cv_ia.json", "cv_ia.md")


def asegurar_dirs() -> None:
    """Crea data/usuario/ y data/usuario/cv/ si faltan (idempotente)."""
    USER_DIR.mkdir(parents=True, exist_ok=True)
    CV_DIR.mkdir(parents=True, exist_ok=True)


def migrar_legado() -> list:
    """Mueve perfil/backups/IA del CV del layout viejo al folder de usuario.

    Solo mueve cuando el destino no existe, para no pisar datos nuevos.
    Devuelve la lista de archivos movidos.
    """
    asegurar_dirs()
    movidos = []

    candidatos = [DATA_DIR / nombre for nombre in _LEGACY_FILES]
    candidatos += sorted(DATA_DIR.glob("profile_backup_*.json"))
    for src in candidatos:
        dst = USER_DIR / src.name
        if src.exists() and not dst.exists():
            shutil.move(str(src), str(dst))
            movidos.append(dst)

    # CV reales que quedaron sueltos en data/ (el demo sintetico no se toca).
    for patron in ("*.pdf", "*.doc", "*.docx"):
        for src in DATA_DIR.glob(patron):
            if src.name.startswith("demo_"):
                continue
            dst = CV_DIR / src.name
            if not dst.exists():
                shutil.move(str(src), str(dst))
                movidos.append(dst)

    return movidos
