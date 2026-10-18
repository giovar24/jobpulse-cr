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

def wait_for_port(port, timeout=15):
    for _ in range(timeout * 2):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return True
        except OSError:
            time.sleep(0.5)
    return False

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

        # Kill existing Brave processes
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/IM", "brave.exe"], capture_output=True)
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
            logger.error("Brave failed to start.")
            if self.proc: self.proc.kill()
            return None
        
        return self.port

    def stop_brave(self):
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
