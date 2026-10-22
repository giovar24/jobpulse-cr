# -*- coding: utf-8 -*-
"""Rutas del workspace: datos del usuario separados de los del scraper.

data/                 -> datos del scraper (jobs.db, empresas, caches, logs)
data/usuario/         -> datos personales del candidato (perfil, backups, IA del CV)
data/usuario/cv/      -> copia local de los CV importados (PDF/DOCX/TXT/MD)
informes/             -> entregables (auditorias, reportes de vacantes, IA)

Asi un CV o el perfil real nunca se mezclan con los archivos que genera el
scraper, y borrar/regenerar data/ no obliga a reconstruir el perfil a mano.
Los informes viven fuera de data/ para que sobrevivan a --fresh y a cualquier
limpieza de las tablas. Se pueden redirigir con JOBPULSE_INFORMES_DIR.
"""
import os
import shutil
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
USER_DIR = DATA_DIR / "usuario"
CV_DIR = USER_DIR / "cv"

INFORMES_DIR = Path(
    os.environ.get("JOBPULSE_INFORMES_DIR") or (BASE_DIR / "informes")
).expanduser()
INFORMES_CALIDAD = INFORMES_DIR / "calidad"
INFORMES_VACANTES = INFORMES_DIR / "vacantes"
INFORMES_IA = INFORMES_DIR / "ia"

_INFORMES_CATEGORIAS = {
    "calidad": INFORMES_CALIDAD,
    "vacantes": INFORMES_VACANTES,
    "ia": INFORMES_IA,
}

PROFILE_PATH = USER_DIR / "profile.json"
PROFILE_EXAMPLE = DATA_DIR / "profile.example.json"
CV_IA_JSON = USER_DIR / "cv_ia.json"
CV_IA_MD = USER_DIR / "cv_ia.md"

# Archivos personales del esquema anterior (vivían sueltos en data/).
_LEGACY_FILES = ("profile.json", "cv_ia.json", "cv_ia.md")


def asegurar_dirs() -> None:
    """Crea data/usuario/, su carpeta de CV y las de informes (idempotente)."""
    USER_DIR.mkdir(parents=True, exist_ok=True)
    CV_DIR.mkdir(parents=True, exist_ok=True)
    for carpeta in (INFORMES_DIR, *_INFORMES_CATEGORIAS.values()):
        carpeta.mkdir(parents=True, exist_ok=True)


def ruta_informe(categoria: str, nombre: str) -> Path:
    """Devuelve (y crea) la ruta de un informe dentro de informes/<categoria>.

    Categorias: calidad | vacantes | ia. Una categoria desconocida cae en la
    raiz de informes/ para no romper llamadas antiguas.
    """
    base = _INFORMES_CATEGORIAS.get(categoria, INFORMES_DIR)
    base.mkdir(parents=True, exist_ok=True)
    return base / nombre


def migrar_informes() -> list:
    """Mueve los informes historicos de data/ a informes/ (idempotente).

    Solo toca reportes y calibracion IA; NO mueve configuracion como
    ia_modelo.txt / ia_modelos.json / ia_proveedor.txt, que siguen en data/.
    """
    asegurar_dirs()
    movidos = []
    destinos = [
        ("report*.csv", "vacantes"),
        ("ia_????????_????.csv", "ia"),
        ("ia_calibracion.md", "ia"),
        ("ia_calibracion.json", "ia"),
    ]
    for patron, categoria in destinos:
        for src in sorted(DATA_DIR.glob(patron)):
            dst = ruta_informe(categoria, src.name)
            if src.exists() and not dst.exists():
                shutil.move(str(src), str(dst))
                movidos.append(dst)
    return movidos


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
