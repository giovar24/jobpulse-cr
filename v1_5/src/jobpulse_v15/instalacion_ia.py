# -*- coding: utf-8 -*-
"""Instalacion de los requisitos minimos de IA local para JobPulse CR v1.5.

Pensado para replicar la instalacion en un equipo nuevo sin pasos manuales.
Orden de instalacion (cada paso verifica antes de actuar):

  1) huggingface_hub  — dependencia de descarga desde Hugging Face (pip si falta).
  2) Modelo local     — 3 GGUF del repo de Unsloth (fuente de confianza acordada):
                        gemma-4-E2B-it-qat-UD-Q4_K_XL.gguf  (2.62 GB)
                        mmproj-BF16.gguf                    (987 MB)
                        MTP/mtp-gemma-4-E2B-it-Q4_0.gguf    (59 MB, se aplana a raiz)
     Los SHA256 quedan fijados aqui; `verificar_integridad` los comprueba.
  3) llama.cpp        — build Vulkan de ggml-org (llama-server.exe); solo se
                        descarga si no existe en la ruta configurada.
  4) Hardware/SO      — Windows x64, GPU con Vulkan >= 4 GB VRAM libres (o CPU,
                        mucho mas lento), ~5 GB de disco libres.

Rutas configurables en .env (fuente unica, compartidas con start_ia_server.py):
  IA_MODEL_DIR      carpeta destino del modelo
  IA_LLAMA_SERVER   ruta completa de llama-server.exe
  IA_LLAMA_RELEASE  URL del zip del release de llama.cpp (opcional)
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path

import httpx

# ─── Configuración (editar aqui o sobrescribir en .env) ─────────────────────

REPO_MODELO = "unsloth/gemma-4-E2B-it-qat-GGUF"  # Unsloth: fuente de confianza

LLAMA_RELEASE_DEFECTO = (
    "https://github.com/ggml-org/llama.cpp/releases/download/"
    "b11662/llama-b11662-bin-win-vulkan-x64.zip"
)

MODELO_DIR_DEFECTO = r"D:\Models\gemma-4-E2B-it-qat-UD-Q4_K_XL"
LLAMA_SERVER_DEFECTO = r"D:\llama-b11662-bin-win-vulkan-x64\llama-server.exe"

# Margen de disco extra sobre el peso de los archivos (zip temporal + extracto).
MARGEN_DISCO_LLAMA = 2 * 1024**3  # 2 GB
MARGEN_DISCO_MODELO = 512 * 1024**2  # 512 MB


def modelo_dir() -> Path:
    """Carpeta destino del modelo (env IA_MODEL_DIR o defecto)."""
    return Path(os.getenv("IA_MODEL_DIR") or MODELO_DIR_DEFECTO)


def ruta_llama_server() -> Path:
    """Ruta completa de llama-server.exe (env IA_LLAMA_SERVER o defecto)."""
    return Path(os.getenv("IA_LLAMA_SERVER") or LLAMA_SERVER_DEFECTO)


def url_llama_release() -> str:
    """URL del zip del build de llama.cpp (env IA_LLAMA_RELEASE o defecto)."""
    return os.getenv("IA_LLAMA_RELEASE") or LLAMA_RELEASE_DEFECTO


# ─── Modelo: archivos exactos verificados contra la instalacion real ────────


@dataclass(frozen=True)
class ArchivoModelo:
    ruta_repo: str        # ruta del archivo dentro del repo HF
    nombre_local: str     # nombre final en modelo_dir() (MTP/ se aplana)
    bytes_esperados: int
    sha256: str


ARCHIVOS_MODELO: tuple[ArchivoModelo, ...] = (
    ArchivoModelo(
        "gemma-4-E2B-it-qat-UD-Q4_K_XL.gguf",
        "gemma-4-E2B-it-qat-UD-Q4_K_XL.gguf",
        2_620_370_976,
        "e531007218dfab990486a5de7676a6932d6ea8dea233d1f698d7c21cf8a16889",
    ),
    ArchivoModelo(
        "mmproj-BF16.gguf",
        "mmproj-BF16.gguf",
        986_833_728,
        "38b33846f56426cd650e0e574d78de125abdfcedf35c0d7f6929f6ffe26efe02",
    ),
    ArchivoModelo(
        "MTP/mtp-gemma-4-E2B-it-Q4_0.gguf",
        "mtp-gemma-4-E2B-it-Q4_0.gguf",
        59_235_648,
        "586f2460b909008640981ec34060aa864e03c144fbabfb3173c4335087e4aae0",
    ),
)

BYTES_TOTALES_MODELO = sum(a.bytes_esperados for a in ARCHIVOS_MODELO)


def peso_legible(n: int | float) -> str:
    """Bytes -> texto legible (ej. 3.42 GiB)."""
    valor = float(n)
    for unidad in ("B", "KiB", "MiB", "GiB"):
        if valor < 1024 or unidad == "GiB":
            return f"{valor:.2f} {unidad}" if unidad != "B" else f"{int(valor)} B"
        valor /= 1024
    return f"{valor:.2f} GiB"


# ─── Paso 1: dependencia huggingface_hub ─────────────────────────────────────


def deps_ia_ok() -> bool:
    try:
        import huggingface_hub  # noqa: F401
    except ImportError:
        return False
    return True


def instalar_deps(log=print) -> None:
    """Instala huggingface_hub si falta (unico requisito Python extra de IA)."""
    if deps_ia_ok():
        log("[deps] huggingface_hub presente.")
        return
    log("[deps] huggingface_hub NO esta. Instalando (pip)...")
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", "huggingface_hub>=0.34"]
    )
    log("[deps] huggingface_hub instalado.")


# ─── Paso 2: modelo desde Hugging Face (Unsloth) ─────────────────────────────


def _hay_espacio(ruta: Path, necesidad: int) -> bool:
    """True si la unidad de `ruta` tiene al menos `necesidad` bytes libres."""
    unidad = ruta if ruta.exists() else ruta.parent
    while not unidad.exists():
        unidad = unidad.parent
    return shutil.disk_usage(unidad).free >= necesidad


def estado_modelo(
    directorio: Path, archivos: tuple[ArchivoModelo, ...] = ARCHIVOS_MODELO
) -> tuple[list[ArchivoModelo], list[ArchivoModelo]]:
    """Clasifica (presentes, faltantes/corruptos) segun tamano esperado."""
    presentes: list[ArchivoModelo] = []
    faltantes: list[ArchivoModelo] = []
    for archivo in archivos:
        destino = directorio / archivo.nombre_local
        if destino.exists() and destino.stat().st_size == archivo.bytes_esperados:
            presentes.append(archivo)
        else:
            faltantes.append(archivo)
    return presentes, faltantes


def verificar_integridad(
    directorio: Path,
    archivos: tuple[ArchivoModelo, ...] = ARCHIVOS_MODELO,
    log=print,
) -> list[str]:
    """Verifica SHA256 de los archivos presentes; devuelve problemas."""
    problemas: list[str] = []
    for archivo in archivos:
        destino = directorio / archivo.nombre_local
        if not destino.exists():
            continue
        log(f"  SHA256 {archivo.nombre_local} ...")
        h = hashlib.sha256()
        with open(destino, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 21), b""):
                h.update(chunk)
        if h.hexdigest() != archivo.sha256:
            problemas.append(
                f"{archivo.nombre_local}: SHA256 no coincide "
                f"(esperado {archivo.sha256[:12]}..., obtenido {h.hexdigest()[:12]}...)"
            )
    return problemas


def descargar_modelo(directorio: Path | None = None, log=print) -> None:
    """Descarga desde el repo de Unsloth lo que falte (con reanudacion)."""
    directorio = directorio or modelo_dir()
    try:
        from huggingface_hub import hf_hub_download
    except ImportError as exc:  # pragma: no cover - instalar_deps cubre esto
        raise RuntimeError(
            "huggingface_hub no esta instalado. Ejecuta instalar_deps() o "
            "pip install huggingface_hub"
        ) from exc

    presentes, faltantes = estado_modelo(directorio)
    for archivo in presentes:
        log(f"[modelo] OK {archivo.nombre_local} ({peso_legible(archivo.bytes_esperados)})")
    if not faltantes:
        log(f"[modelo] Completo: {len(presentes)}/{len(ARCHIVOS_MODELO)} archivos.")
        return

    peso_falta = sum(a.bytes_esperados for a in faltantes)
    log(
        f"[modelo] Faltan {len(faltantes)} archivo(s) "
        f"({peso_legible(peso_falta)}) desde {REPO_MODELO}"
    )
    if not _hay_espacio(directorio, peso_falta + MARGEN_DISCO_MODELO):
        raise RuntimeError(
            f"Espacio insuficiente en disco para {directorio} "
            f"(se necesitan {peso_legible(peso_falta + MARGEN_DISCO_MODELO)} libres)."
        )
    directorio.mkdir(parents=True, exist_ok=True)

    for archivo in faltantes:
        log(f"[modelo] Descargando {archivo.ruta_repo} ...")
        descargado = Path(
            hf_hub_download(
                repo_id=REPO_MODELO,
                filename=archivo.ruta_repo,
                local_dir=directorio,
            )
        )
        destino = directorio / archivo.nombre_local
        if descargado != destino:
            # MTP/ queda en subcarpeta en el repo; se aplana a la raiz.
            destino.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(descargado), destino)
            padre = descargado.parent
            if padre != directorio and not any(padre.iterdir()):
                padre.rmdir()
        real = destino.stat().st_size
        if real != archivo.bytes_esperados:
            raise RuntimeError(
                f"{archivo.nombre_local}: tamano inesperado tras descargar "
                f"({real} != {archivo.bytes_esperados}). Reintenta el comando; "
                "la descarga se reanuda donde quedo."
            )
        log(f"[modelo] OK {archivo.nombre_local}")


# ─── Paso 3: llama.cpp build Vulkan ──────────────────────────────────────────


def estado_llama(ruta_server: Path | None = None) -> bool:
    """True si llama-server.exe existe en la ruta configurada."""
    ruta_server = ruta_server or ruta_llama_server()
    return ruta_server.exists()


def _localizar_llama_server(base: Path) -> Path | None:
    """Busca llama-server.exe en base o un nivel debajo (estructura del zip)."""
    candidatos = [base / "llama-server.exe", *base.glob("*/llama-server.exe")]
    for candidato in candidatos:
        if candidato.exists():
            return candidato
    return None


def _descargar_zip(url: str, destino: Path, log=print) -> Path:
    """Descarga con reanudacion via Range (si el servidor la soporta)."""
    destino.parent.mkdir(parents=True, exist_ok=True)
    ya_hay = destino.stat().st_size if destino.exists() else 0
    headers = {"Range": f"bytes={ya_hay}-"} if ya_hay else {}
    with httpx.stream(
        "GET", url, headers=headers, follow_redirects=True, timeout=60
    ) as r:
        if r.status_code == 416:  # ya estaba completo
            return destino
        modo = "ab" if r.status_code == 206 else "wb"
        total = int(r.headers.get("content-length") or 0)
        if r.status_code == 206:
            total += ya_hay
        log(f"[llama.cpp] {url} ({peso_legible(total) if total else 'tamano ?'})")
        with open(destino, modo) as f:
            for chunk in r.iter_bytes(1 << 21):
                f.write(chunk)
    return destino


def instalar_llamacpp(ruta_server: Path | None = None, log=print) -> Path:
    """Descarga y extrae el build de llama.cpp si llama-server no existe.

    Devuelve la ruta real donde quedo llama-server.exe (puede diferir de la
    configurada; en ese caso se informa como ajustar .env).
    """
    ruta_server = ruta_server or ruta_llama_server()
    if ruta_server.exists():
        log(f"[llama.cpp] OK {ruta_server}")
        return ruta_server

    url = url_llama_release()
    nombre_zip = url.rsplit("/", 1)[-1]
    destino_base = ruta_server.parent.parent  # p.ej. D:\ (el zip trae carpeta raiz)
    destino_zip = destino_base / nombre_zip

    necesidad = MARGEN_DISCO_LLAMA
    if not _hay_espacio(destino_base, necesidad):
        raise RuntimeError(
            f"Espacio insuficiente en disco para {destino_base} "
            f"(se necesitan ~{peso_legible(necesidad)} libres)."
        )

    log(f"[llama.cpp] llama-server no esta en {ruta_server}")
    _descargar_zip(url, destino_zip, log)
    log(f"[llama.cpp] Extrayendo en {destino_base} ...")
    with zipfile.ZipFile(destino_zip) as z:
        z.extractall(destino_base)
    destino_zip.unlink(missing_ok=True)

    encontrado = _localizar_llama_server(destino_base)
    if not encontrado:
        raise RuntimeError(
            f"El zip se extrajo pero no se encontro llama-server.exe bajo "
            f"{destino_base}. Ubicalo manualmente y ajusta IA_LLAMA_SERVER en .env."
        )
    if encontrado != ruta_server:
        log(
            "[llama.cpp] ATENCION: quedo en una ruta distinta a la configurada.\n"
            f"  Ruta configurada : {ruta_server}\n"
            f"  Ruta real        : {encontrado}\n"
            f"Agrega al .env:  IA_LLAMA_SERVER={encontrado}"
        )
    else:
        log(f"[llama.cpp] OK {encontrado}")
    return encontrado


# ─── Diagnóstico global ──────────────────────────────────────────────────────


def diagnostico(log=print) -> list[str]:
    """Lista los problemas pendientes de la instalacion IA (vacios = listo)."""
    problemas: list[str] = []
    directorio = modelo_dir()
    ruta_server = ruta_llama_server()

    if not deps_ia_ok():
        problemas.append("huggingface_hub no instalado (paso 1).")

    presentes, faltantes = estado_modelo(directorio)
    for archivo in faltantes:
        problemas.append(
            f"Falta {archivo.nombre_local} en {directorio} "
            f"({peso_legible(archivo.bytes_esperados)})"
        )
    if faltantes:
        log(f"[modelo] {len(presentes)}/{len(ARCHIVOS_MODELO)} archivos presentes.")
    else:
        problemas.extend(verificar_integridad(directorio, log=log))

    if not estado_llama(ruta_server):
        problemas.append(f"Falta llama-server.exe en {ruta_server} (paso 3).")

    return problemas
