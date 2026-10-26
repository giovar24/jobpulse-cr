# -*- coding: utf-8 -*-
"""Verifica y arranca el servidor IA local (llama.cpp) para v1.5.

Uso:
    python start_ia_server.py          # verifica; si no responde, lo arranca y espera
    python start_ia_server.py --check  # solo verifica (exit 0 ok / 1 apagado)

El launcher .bat lo llama antes de abrir la UI: si el servidor ya esta
levantado no hace nada (respuesta inmediata). Si esta apagado lo lanza en
proceso separado con el mismo comando que usa el usuario y espera la carga
del modelo (hasta IA_WAIT segundos).

Para usar un SERVIDOR EXTERNO en vez del local: apunte IA_BASE_URL del
.env a su URL y ponga IA_START_CMD vacio/invalido — el script detecta que
el health responde remote y no intenta arrancar nada local.
"""
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

RAIZ = Path(__file__).resolve().parent
load_dotenv(RAIZ / ".env")

BASE_URL = (os.getenv("IA_BASE_URL") or "http://127.0.0.1:8081/v1").rstrip("/")
ES_LOCAL = "127.0.0.1" in BASE_URL or "localhost" in BASE_URL
PUERTO = BASE_URL.split("/")[-2].split(":")[-1] if ":" in BASE_URL else "8081"

# Rutas compartidas con instalar_modelo_ia.py (fuente unica). Se pueden
# sobrescribir en .env con IA_MODEL_DIR e IA_LLAMA_SERVER.
from jobpulse_v15.instalacion_ia import modelo_dir, ruta_llama_server

LLAMA_SERVER = ruta_llama_server()
MODELO_DIR = modelo_dir()
CMD = [
    LLAMA_SERVER,
    "-m", rf"{MODELO_DIR}\gemma-4-E2B-it-qat-UD-Q4_K_XL.gguf",
    "--mmproj", rf"{MODELO_DIR}\mmproj-BF16.gguf",
    "--model-draft", rf"{MODELO_DIR}\mtp-gemma-4-E2B-it-Q4_0.gguf",
    "--spec-type", "draft-mtp", "--spec-draft-n-max", "2", "--spec-draft-ngl", "all",
    "--alias", "gemma4-e2b-qat",
    "--port", PUERTO,
    "--host", "127.0.0.1",
    "--gpu-layers", "all", "--flash-attn", "on",
    "--cache-type-k", "q8_0", "--cache-type-v", "q8_0", "--kv-unified",
    "--ctx-size", "8192", "--parallel", "1",
    "--threads", "4", "--threads-batch", "8",
    "--jinja", "--chat-template-kwargs", '{"enable_thinking":false}',
    "--temp", "1.0", "--top-p", "0.95", "--top-k", "64", "--min-p", "0.0",
    "--metrics", "--log-verbosity", "3",
]


def servidor_arriba(timeout: float = 3.0) -> bool:
    """True si /health del servidor IA responde."""
    health_url = BASE_URL.rsplit("/", 1)[0] if "/v1" in BASE_URL else BASE_URL
    try:
        r = httpx.get(f"{health_url}/health", timeout=timeout)
        return r.status_code == 200
    except Exception:
        return False


def esperar(tries: int = 180) -> bool:
    """Espera a que el servidor responda (1 intento/segunda)."""
    for i in range(tries):
        if servidor_arriba():
            return True
        if i and i % 30 == 0:
            print(f"  ... esperando al modelo ({i}s)...")
    return False


def main() -> int:
    if servidor_arriba():
        print(f"Servidor IA YA ARRIBA en {BASE_URL}")
        return 0

    if "--check" in sys.argv:
        print(f"Servidor IA APAGADO: {BASE_URL}")
        return 1

    if not ES_LOCAL:
        print(
            f"Servidor EXTERNO {BASE_URL} no responde. "
            "Verifica la URL en .env (IA_BASE_URL) y que el servicio este corriendo."
        )
        return 1

    if not Path(LLAMA_SERVER).exists():
        print(f"NO se encuentra llama-server: {LLAMA_SERVER}")
        print("Arranca el servidor manualmente o edita start_ia_server.py.")
        return 1

    print(f"Arrancando llama-server en puerto {PUERTO}...")
    log_path = RAIZ / "data" / "ia_server.log"
    log_path.parent.mkdir(exist_ok=True)
    with open(log_path, "ab") as log:
        # DETACHED: proceso huerfano vivo al salir del launcher
        subprocess.Popen(
            CMD,
            stdout=log,
            stderr=subprocess.STDOUT,
            cwd=str(Path(LLAMA_SERVER).parent),
            creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
        )
    ok = esperar()
    if ok:
        print(f"Servidor IA ARRIBA ({BASE_URL}) — log: {log_path}")
        return 0
    print(f"El servidor no respondio a tiempo — revisa {log_path}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
