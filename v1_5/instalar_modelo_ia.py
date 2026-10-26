# -*- coding: utf-8 -*-
"""Instala los requisitos minimos de IA local para JobPulse CR v1.5.

Para un equipo nuevo: descarga el modelo Gemma 4 E2B QAT desde el repo de
Unsloth en Hugging Face, instala la dependencia de descarga (huggingface_hub)
y, si falta, el build de llama.cpp (llama-server.exe). Todo paso verifica
antes de actuar: re-ejecutar es seguro (las descargas se reanudan).

Requisitos minimos para correr:
  - Windows x64 (o el build de llama.cpp equivalente a tu SO).
  - GPU con Vulkan y >= 4 GB de VRAM libres (o CPU: funciona, mucho mas lento).
  - ~5 GB de disco libres (modelo 3.42 GiB + build ~1 GB + margen).
  - Python 3.11+ con las dependencias del proyecto: pip install -e .[ia]

Uso:
    python instalar_modelo_ia.py              # instala lo que falte y verifica
    python instalar_modelo_ia.py --check      # solo verifica (no descarga)
    python instalar_modelo_ia.py --solo-modelo
    python instalar_modelo_ia.py --solo-llama

Rutas personalizables en .env: IA_MODEL_DIR, IA_LLAMA_SERVER, IA_LLAMA_RELEASE.
"""
import argparse
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
sys.path.insert(0, str(RAIZ / "src"))

from jobpulse_v15.instalacion_ia import (  # noqa: E402
    BYTES_TOTALES_MODELO,
    REPO_MODELO,
    descargar_modelo,
    diagnostico,
    instalar_deps,
    instalar_llamacpp,
    modelo_dir,
    peso_legible,
    ruta_llama_server,
)


def _cargar_env() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv(RAIZ / ".env")
    except ImportError:
        pass  # sin python-dotenv se usan los valores por defecto


def _requisitos() -> None:
    print("Requisitos minimos para correr la IA local:")
    print("  - Windows x64, GPU con Vulkan >= 4 GB VRAM libres (o CPU, mas lento).")
    print(f"  - ~5 GB de disco (modelo {peso_legible(BYTES_TOTALES_MODELO)} + build llama.cpp).")
    print("  - Python 3.11+ con: pip install -e .[ia]")
    print(f"  - Modelo: {REPO_MODELO} (Hugging Face)")
    print(f"  - Carpeta modelo    : {modelo_dir()}")
    print(f"  - llama-server.exe  : {ruta_llama_server()}")
    print("    (personalizables en .env con IA_MODEL_DIR / IA_LLAMA_SERVER)")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="solo verificar")
    parser.add_argument("--solo-modelo", action="store_true")
    parser.add_argument("--solo-llama", action="store_true")
    args = parser.parse_args()

    _cargar_env()
    _requisitos()

    if args.check:
        problemas = diagnostico()
        if problemas:
            print("Pendiente de instalar:")
            for p in problemas:
                print(f"  - {p}")
            return 1
        print("Instalacion IA COMPLETA y verificada (modelo + llama-server).")
        print("Arranque: ia-server.bat  o  python start_ia_server.py")
        return 0

    if not args.solo_llama:
        instalar_deps()
        descargar_modelo()

    if not args.solo_modelo:
        instalar_llamacpp()

    problemas = diagnostico()
    if problemas:
        print("\nQuedo pendiente (re-ejecuta este comando para reanudar):")
        for p in problemas:
            print(f"  - {p}")
        return 1
    print("\nInstalacion IA COMPLETA.")
    print("Verifica integridad completa con:  python instalar_modelo_ia.py --check")
    print("Arranca el servidor con:           ia-server.bat")
    return 0


if __name__ == "__main__":
    sys.exit(main())
