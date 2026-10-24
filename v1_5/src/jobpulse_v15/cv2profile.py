# -*- coding: utf-8 -*-
"""
jobpulse_v15.cv2profile — Convierte archivos CV (PDF/DOCX) al perfil standard
y ofrece utilidades de consola/cli.

Uso típico:
    python -m jobpulse_v15.cv2profile /ruta/al/cv.pdf
    python -m jobpulse_v15.cv2profile /ruta/al/cv.docx --output json

O también como módulo importable:
    from jobpulse_v15.cv2profile import cv_a_perfil
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from jobpulse_v15.cv_analysis import cv_a_perfil, DEFAULT_WEIGHTS

__all__ = ["cv_a_perfil", "DEFAULT_WEIGHTS", "main"]


def main(argv: Optional[List[str]] = None) -> int:
    """Punto de entrada CLI para convertir CV a perfil JSON."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Convierte un archivo CV (PDF/DOCX) a perfil jobpulse JSON."
    )
    parser.add_argument("archivo", type=str, help="Ruta al archivo CV (PDF o DOCX)")
    parser.add_argument(
        "--output", "-o", type=str, default=None,
        help="Archivo de salida JSON. Si no se especifica, imprime en stdout.",
    )
    parser.add_argument(
        "--nombre", "-n", type=str, default=None,
        help="Nombre del candidato. Si no se usa, se deriva del filename.",
    )

    args = parser.parse_args(argv)

    filepath = Path(args.archivo)
    if not filepath.exists():
        print(f"Error: archivo no encontrado: {filepath}", file=sys.stderr)
        return 1

    perfil = cv_a_perfil(filepath, nombre=args.nombre)

    output = json.dumps(perfil, ensure_ascii=False, indent=2)

    if args.output:
        try:
            with Path(args.output).open("w", encoding="utf-8") as f:
                f.write(output)
            print(f"Perfil guardado en: {args.output}")
        except Exception as e:
            print(f"Error guardando output: {e}", file=sys.stderr)
            return 1
    else:
        print(output)

    return 0


if __name__ == "__main__":
    sys.exit(main())