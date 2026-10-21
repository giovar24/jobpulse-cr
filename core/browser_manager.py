import subprocess
import time
import os
import logging
import socket

logger = logging.getLogger("jobpulse.browser")

def get_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]

def wait_for_port(port, timeout=20):
    """True si el puerto abre antes del timeout (reloj monotono, sin depender
    de cuantas veces quepa el sleep)."""
    fin = time.time() + timeout
    while time.time() < fin:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.3)
    return False


def brave_running():
    """True si queda algun brave.exe vivo (tasklist, sin depender de permisos
    de Get-Process)."""
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq brave.exe", "/NH"],
            capture_output=True, text=True, errors="replace", timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return "brave.exe" in (out.stdout or "").lower()
    except Exception as e:
        logger.warning("No se pudo consultar tasklist (%s).", e)
        return False


def kill_brave(timeout=12):
    """Mata Brave y CONFIRMA que no quedo ninguno vivo.

    Es la diferencia clave: si el taskkill falla (p. ej. Brave corriendo como
    administrador) y se lanza igual una segunda instancia, Chromium se pliega
    a la que ya existe y el `--remote-debugging-port` se ignora -> el puerto
    nunca abre y la corrida muere 45 s despues sin causa visible.
    """
    for intento in range(2):
        try:
            subprocess.run(["taskkill", "/F", "/T", "/IM", "brave.exe"],
                           capture_output=True, timeout=20,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception as e:
            logger.warning("taskkill fallo (%s).", e)
        fin = time.time() + timeout
        while time.time() < fin:
            if not brave_running():
                return True
            time.sleep(0.5)
        logger.warning("Brave sigue vivo tras taskkill (intento %d/2).", intento + 1)
    return not brave_running()

class BraveManager:
    def __init__(self, config):
        self.config = config
        self.port = None
        self.proc = None

    def start_brave(self):
        # Adjusted default path for Windows Brave profile
        brave_bin = self.config.get("brave_bin", "C:\\Program Files\\BraveSoftware\\Brave-Browser\\Application\\brave.exe")
        
        if "brave_perfil" in self.config:
            brave_perfil = self.config["brave_perfil"]
        else:
            # Default Windows Brave profile path
            brave_perfil = os.path.join(os.environ.get("LOCALAPPDATA", ""), "BraveSoftware\\Brave-Browser\\User Data")

        # Cerrar Brave y CONFIRMARLO. Si sobrevive una instancia, el nuevo
        # lanzamiento se pliega a ella (single-instance de Chromium) y el
        # --remote-debugging-port se ignora: el puerto CDP nunca abre y la
        # corrida muere ~45 s despues sin causa visible (paso el 2026-10-21).
        if os.name == "nt":
            if not kill_brave():
                logger.error(
                    "No se pudo cerrar Brave: sigue abierto tras dos intentos. "
                    "Cerralo del todo (Administrador de tareas; si lo iniciaste "
                    "como administrador, cerralo desde una consola elevada) y "
                    "volve a lanzar la corrida. Con Brave ya abierto, el nuevo "
                    "proceso se pliega a esa instancia y no abre el puerto de "
                    "depuracion que necesita el scraper.")
                return None
        else:
            subprocess.run(["pkill", "-f", "brave"], capture_output=True)
            time.sleep(1)

        self.port = get_free_port()
        logger.info(f"Launching Brave using profile: {brave_perfil}")
        logger.info(f"Launching Brave on port {self.port}...")

        args = [
            brave_bin,
            f"--user-data-dir={brave_perfil}",
            f"--remote-debugging-port={self.port}",
            "--no-first-run", "--disable-notifications",
        ]
        # Permite forzar la resolucion DNS de un host (util cuando el ISP no resuelve el dominio).
        rules = self.config.get("host_resolver_rules")
        if rules:
            args.append(f"--host-resolver-rules={rules}")
            logger.info(f"Using host-resolver-rules: {rules}")

        self.proc = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )

        if not wait_for_port(self.port):
            # Pista util: si el lanzador ya termino, el perfil estaba en uso y
            # Brave se pliego a otra instancia; si sigue vivo, no abrio CDP.
            vivo = self.proc.poll() is None
            if vivo:
                logger.error(
                    "Brave no abrio el puerto CDP %s en 20 s (el proceso sigue "
                    "vivo). Se cierra el intento; revisa que el perfil no este "
                    "en uso por otra instancia.", self.port)
                self.proc.kill()
            else:
                logger.error(
                    "Brave termino con codigo %s antes de abrir el puerto CDP "
                    "%s (el perfil estaba en uso: se pliego a otra instancia "
                    "de Brave).", self.proc.returncode, self.port)
            return None
        
        return self.port

    def stop_brave(self):
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
