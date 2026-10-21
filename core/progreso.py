# -*- coding: utf-8 -*-
"""core.progreso — estado de la corrida EN VIVO para la interfaz grafica.

Arregla dos bugs del launcher (2026-10-19):

- El dashboard se abria DESPUES del scraping (5+ minutos de consola en
  negro): ahora el launcher abre la UI primero y la pestaña "Corrida"
  muestra que esta haciendo cada proveedor.
- No habia forma de saber que estaba pasando: cada paso escribe
  `data/corrida_estado.json` (atomico) y `data/corrida.log`.

Uso en main.py:

    prog = iniciar_corrida()          # None si ya hay una corrida viva
    prog.total(n_fases)
    prog.fase("Workday", grupo="directos")
    prog.sumar("directos", 12)
    prog.finalizar("ok", resumen={...})

La GUI (ui.py) solo LEE: `leer_estado()`, `corriendo()`, `tail()`.
`detener()` mata el proceso de la corrida (taskkill /T /F).
"""
import json
import logging
import os
import subprocess
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

RAIZ = Path(__file__).resolve().parent.parent
DATA = RAIZ / "data"
ESTADO_JSON = DATA / "corrida_estado.json"
LOG_PATH = DATA / "corrida.log"
STDOUT_PATH = DATA / "corrida_stdout.log"

_LOCK = threading.Lock()
_INSTANCIA: Optional["Progreso"] = None

# Los grupos PUBLICOS son 2 (ver providers/source_groups.py): el trio
# estatal y todo lo demas. boards/directos solo existen por dentro (dedupe y
# alcance de corrida), asi que se rotulan con la misma etiqueta del resto.
GRUPO_RESTO = "Resto (agregadores + ATS)"
GRUPO_ESTATALES = "Portal estatal (ANE/CINDE/Procomer)"
GRUPO_LABEL = {
    "boards": GRUPO_RESTO,
    "directos": GRUPO_RESTO,
    "estatales": GRUPO_ESTATALES,
}


# ------------------------------------------------------------- utilidades
def _ahora() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _leer(path: Path) -> dict:
    try:
        if path.exists():
            d = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                return d
    except Exception:
        pass
    return {}


def _escribir(path: Path, data: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        pass


def pid_vivo(pid) -> bool:
    """Windows: tasklist es la unica forma segura (os.kill en Windows mata)."""
    if not pid:
        return False
    try:
        r = subprocess.run(["tasklist", "/FI", f"PID eq {int(pid)}", "/NH"],
                           capture_output=True, text=True, timeout=10)
        out = (r.stdout or "")
        return "No tasks" not in out and str(pid) in out
    except Exception:
        return False


def leer_estado() -> dict:
    est = _leer(ESTADO_JSON)
    est.setdefault("estado", "nunca")
    est.setdefault("encontradas", {})
    est.setdefault("fases_completadas", [])
    # una corrida cuyo proceso ya no existe esta "interrumpida", no "corriendo"
    if est["estado"] == "corriendo" and not pid_vivo(est.get("pid")):
        est["estado"] = "interrumpida"
    return est


def corriendo() -> bool:
    return leer_estado()["estado"] == "corriendo"


def tail(lineas: int = 120, path: Path = LOG_PATH) -> List[str]:
    try:
        if not path.exists():
            return []
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.readlines()[-lineas:]
    except Exception:
        return []


def tail_stdout(lineas: int = 60) -> List[str]:
    """Salida cruda del proceso (tracebacks/prints) si se lanzo desde la GUI."""
    return tail(lineas, STDOUT_PATH)


def detener() -> bool:
    """Mata el arbol de la corrida actual (main.py + Brave)."""
    est = leer_estado()
    pid = est.get("pid")
    if not pid:
        return False
    ok = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                        capture_output=True).returncode == 0
    if ok:
        _escribir(ESTADO_JSON, dict(est, estado="detenida", fin_ts=_ahora()))
    return ok


class _LogHandler(logging.Handler):
    """Cada registro de logging tambien cae en data/corrida.log."""

    def __init__(self, path: Path):
        super().__init__(level=logging.INFO)
        self.path = path
        self.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s"))

    def emit(self, record):
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(self.format(record) + "\n")
        except Exception:
            pass


class Progreso(object):
    """Escribiente de corrida_estado.json (una instancia por proceso)."""

    def __init__(self, titulo: str = "corrida"):
        self.titulo = titulo
        self._estado = {
            "pid": os.getpid(),
            "estado": "corriendo",
            "titulo": titulo,
            "inicio_ts": _ahora(),
            "fin_ts": None,
            "fase_actual": "Preparación",
            "detalle": "",
            "grupo": "",
            "paso": 0,
            "total_fases": 0,
            "fases_completadas": [],
            "encontradas": {"boards": 0, "directos": 0, "estatales": 0},
            "resumen": {},
            "ultimo_log": "",
        }
        self._grabar()
        handler = _LogHandler(LOG_PATH)
        logging.getLogger().addHandler(handler)
        self._handler = handler

    # ------------------------------------------------------------ escritura
    def _grabar(self):
        with _LOCK:
            _escribir(ESTADO_JSON, self._estado)

    def total(self, n: int):
        self._estado["total_fases"] = int(n)
        self._grabar()

    def fase(self, nombre: str, grupo: str = "", detalle: str = ""):
        """Marca el inicio de un paso. Cierra el anterior como completado."""
        prev = self._estado.get("fase_actual")
        if prev and prev not in self._estado["fases_completadas"]:
            self._estado["fases_completadas"].append(prev)
        self._estado["fase_actual"] = nombre
        self._estado["grupo"] = grupo
        self._estado["detalle"] = detalle
        self._estado["paso"] = self._estado["paso"] + 1
        self._estado["ts"] = _ahora()
        self._grabar()
        etiqueta = GRUPO_LABEL.get(grupo, "sistema")
        logging.getLogger("jobpulse").info("FASE %s/%s [%s] %s%s",
                                           self._estado["paso"],
                                           self._estado["total_fases"] or "?",
                                           etiqueta, nombre,
                                           f" — {detalle}" if detalle else "")

    def nota(self, texto: str):
        self._estado["detalle"] = texto
        self._estado["ts"] = _ahora()
        self._grabar()

    def sumar(self, grupo: str, n: int):
        # Los portales estatales tienen su propia cubeta; lo demas (o un
        # grupo desconocido) sigue contando como "boards".
        g = grupo if grupo in ("directos", "estatales") else "boards"
        self._estado["encontradas"][g] = self._estado["encontradas"].get(g, 0) + int(n)
        self._grabar()

    def log_ultimo(self, texto: str):
        self._estado["ultimo_log"] = texto[:400]
        self._grabar()

    def finalizar(self, estado: str = "ok", resumen: Optional[Dict] = None):
        """estado: ok | error | detenida | preflight."""
        prev = self._estado.get("fase_actual")
        if prev and prev not in self._estado["fases_completadas"]:
            self._estado["fases_completadas"].append(prev)
        self._estado["estado"] = estado
        self._estado["fase_actual"] = ("Completada" if estado == "ok"
                                       else estado.capitalize())
        self._estado["detalle"] = ""
        self._estado["fin_ts"] = _ahora()
        if resumen:
            self._estado["resumen"] = resumen
        self._grabar()
        try:
            logging.getLogger().removeHandler(self._handler)
        except Exception:
            pass

    # alias legible desde main.py
    fin = finalizar


def iniciar_corrida(titulo: str = "corrida", exclusiva: bool = True) -> Optional[Progreso]:
    """Crea la instancia de progreso. None si ya hay una corrida VIVA
    (evita dos `python main.py` a la vez: la GUI y el launcher)."""
    global _INSTANCIA
    est = leer_estado()
    if exclusiva and est["estado"] == "corriendo" and pid_vivo(est.get("pid")):
        logging.getLogger("jobpulse").error(
            "Ya hay una corrida en curso (PID %s, desde %s). Si quieres "
            "reemplazarla: python -c \"from core.progreso import detener; "
            "detener()\" o usa el boton Detener de la pestaña Corrida.",
            est.get("pid"), est.get("inicio_ts"))
        return None
    if LOG_PATH.exists() and LOG_PATH.stat().st_size > 2_000_000:
        try:
            LOG_PATH.unlink()
        except Exception:
            pass
    _INSTANCIA = Progreso(titulo)
    return _INSTANCIA
