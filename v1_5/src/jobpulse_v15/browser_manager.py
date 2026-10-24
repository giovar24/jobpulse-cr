"""Gestión de navegador Brave por CDP para v1.5 (scrapers que necesitan sesión)."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

from playwright.async_api import Browser, BrowserContext, Playwright, async_playwright

logger = logging.getLogger("jobpulse.browser")


class BraveManager:
    """
    Lanza Brave con depuración CDP y devuelve un contexto Playwright conectado.
    Reutiliza el perfil del usuario (cookies, sesión) por defecto.
    """

    def __init__(
        self,
        *,
        brave_bin: str | None = None,
        brave_perfil: str | None = None,
        host_resolver_rules: str | None = None,
        cdp_port: int = 9222,
        startup_timeout: float = 30.0,
    ) -> None:
        self.brave_bin = brave_bin or os.getenv("BRAVE_BIN") or self._detect_brave()
        self.brave_perfil = brave_perfil or os.getenv("BRAVE_PERFIL") or "Default"
        self.host_resolver_rules = host_resolver_rules
        self.cdp_port = cdp_port
        self.startup_timeout = startup_timeout

        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._process: subprocess.Popen | None = None
        self._user_data_dir: Path | None = None

    @staticmethod
    def _detect_brave() -> str:
        candidates = [
            r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
            r"C:\Program Files (x86)\BraveSoftware\Brave-Browser\Application\brave.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe"),
            shutil.which("brave"),
            shutil.which("brave-browser"),
        ]
        for c in candidates:
            if c and Path(c).exists():
                return c
        raise RuntimeError(
            "No se encontró Brave. Instala Brave o define BRAVE_BIN en .env"
        )

    def _get_user_data_dir(self) -> Path:
        if self._user_data_dir:
            return self._user_data_dir
        local_app = Path(os.getenv("LOCALAPPDATA", ""))
        base = local_app / "BraveSoftware" / "Brave-Browser" / "User Data"
        if not base.exists():
            raise RuntimeError(f"Directorio de perfil de Brave no existe: {base}")
        perfil_dir = base / self.brave_perfil
        if not perfil_dir.exists():
            raise RuntimeError(
                f"Perfil '{self.brave_perfil}' no existe en {base}. "
                f"Perfiles disponibles: {[p.name for p in base.iterdir() if p.is_dir()]}"
            )
        self._user_data_dir = perfil_dir
        return self._user_data_dir

    def _build_args(self) -> list[str]:
        args = [
            f"--remote-debugging-port={self.cdp_port}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-networking",
            "--disable-sync",
            "--disable-translate",
            "--hide-crash-restore-bubble",
            "--metrics-recording-only",
            "--mute-audio",
        ]
        if self.host_resolver_rules:
            args.append(f"--host-resolver-rules={self.host_resolver_rules}")
        return args

    def _wait_port(self, port: int, timeout: float) -> bool:
        start = time.time()
        while time.time() - start < timeout:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                    return True
            except OSError:
                time.sleep(0.2)
        return False

    def start_brave(self) -> int:
        """Lanza Brave y espera a que el puerto CDP esté listo. Devuelve el puerto."""
        if self._process and self._process.poll() is None:
            logger.info("Brave ya está corriendo en puerto %d", self.cdp_port)
            return self.cdp_port

        user_data = self._get_user_data_dir()
        args = [self.brave_bin, f"--user-data-dir={user_data}", *self._build_args()]

        logger.info("Iniciando Brave: %s", " ".join(args[:3]) + " ...")
        self._process = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

        if not self._wait_port(self.cdp_port, self.startup_timeout):
            # Limpieza síncrona de emergencia (sin event loop)
            if self._browser:
                try:
                    self._browser.close()
                except Exception:
                    pass
                self._browser = None
            if self._process and self._process.poll() is None:
                self._process.terminate()
                try:
                    self._process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._process.kill()
                self._process = None
            raise RuntimeError(
                f"Brave no abrió el puerto CDP {self.cdp_port} en {self.startup_timeout}s"
            )

        logger.info("Brave listo en CDP puerto %d", self.cdp_port)
        return self.cdp_port

    async def connect_playwright(self) -> BrowserContext:
        """Conecta Playwright al Brave ya lanzado y devuelve un contexto."""
        if not self._playwright:
            self._playwright = await async_playwright().start()

        if not self._browser or not self._browser.is_connected():
            self._browser = await self._playwright.chromium.connect_over_cdp(
                f"http://127.0.0.1:{self.cdp_port}"
            )

        # Usar el contexto por defecto (ya tiene cookies/sesión del usuario)
        contexts = self._browser.contexts
        if contexts:
            return contexts[0]
        return await self._browser.new_context()

    async def new_page(self) -> Any:
        """Atajo: devuelve una page lista para navegar."""
        context = await self.connect_playwright()
        return await context.new_page()

    async def stop_brave(self) -> None:
        """Cierra el navegador y limpia recursos."""
        if self._browser:
            try:
                self._browser.close()
            except Exception:
                pass
            self._browser = None
        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception:
                pass
            self._playwright = None
        if self._process and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
            self._process = None
        logger.info("Brave detenido")

    async def __aenter__(self) -> BraveManager:
        self.start_brave()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.stop_brave()


async def get_browser_context(
    brave_bin: str | None = None,
    brave_perfil: str | None = None,
    host_resolver_rules: str | None = None,
    cdp_port: int = 9222,
) -> tuple[BraveManager, BrowserContext]:
    """Helper: lanza Brave y devuelve (manager, context)."""
    mgr = BraveManager(
        brave_bin=brave_bin,
        brave_perfil=brave_perfil,
        host_resolver_rules=host_resolver_rules,
        cdp_port=cdp_port,
    )
    mgr.start_brave()
    context = await mgr.connect_playwright()
    return mgr, context