"""ANE (ane.cr) por BARRIDO POR FECHA.

El buscador propio de ANE esta roto: devuelve 0 incluso busquando a mano
terminos de software, asi que NO se usa `?Empleos=`. En su lugar se recorre
el listado general `/Puesto?Pagina=N`, que ya viene ordenado mas-reciente-
primero, y se corta cuando las fechas caen fuera de la ventana (o se llega al
tope de paginas). El detalle se lee por HTTP sobre la sesion de Brave
(`ctx.request`), que es rapido y no navega a cada puesto.

Solo se extraen los campos que JobPulse persiste: titulo, empresa, ubicacion,
fecha, link, descripcion, requisitos y salario. El resto del scraper
standalone (checkpoints, CSV, historial, campos extra como plazas/nivel/
correo) NO se porto a proposito: aqui lo que interesa es lo crudo para que el
gate de relevancia (core/gate.py) decida que queda.
"""
import asyncio
import logging
import re
import time
from datetime import date, timedelta
from typing import Any, Dict, List

from bs4 import BeautifulSoup

from providers.base import BaseProvider, JobOffer
from core.dates import parse_date

logger = logging.getLogger("jobpulse.ane")

BASE = "https://www.ane.cr"


def _limpiar(t: str) -> str:
    return " ".join((t or "").split())


class AneProvider(BaseProvider):
    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.login_url = f"{BASE}/Cuenta/Login"
        self.jobs_url = f"{BASE}/Puesto"
        self.user = config.get("username")
        self.password = config.get("password")

    async def fetch(self, search_term: str, location: str, pages: int, port: int,
                    dias: int = 30) -> List[JobOffer]:
        """Barrido por fecha: ignora `search_term` (el filtro lo hace el gate).

        Debe llamarse UNA sola vez por corrida, no por termino de busqueda.
        """
        return await asyncio.to_thread(self._sweep, location, pages, port, dias, search_term)

    # ------------------------------------------------------------ barrido
    def _sweep(self, location: str, pages: int, port: int, dias: int,
               search_term: str) -> List[JobOffer]:
        from playwright.sync_api import sync_playwright

        jobs: List[JobOffer] = []
        corte = date.today() - timedelta(days=dias)
        try:
            with sync_playwright() as p:
                browser = p.chromium.connect_over_cdp(f"http://localhost:{port}")
                ctx = browser.contexts[0]

                if not self._asegurar_sesion(ctx):
                    logger.warning("ANE: sin sesion de detalle; barrido omitido.")
                    return []

                pagina, vacias = 1, 0
                while pagina <= max(1, pages):
                    html = self._obtener(ctx, f"{self.jobs_url}?Pagina={pagina}")
                    cards = self._parse_listado(html) if html else []
                    if not cards:
                        vacias += 1
                        if vacias >= 2:
                            break
                        pagina += 1
                        continue
                    vacias = 0

                    cortar = False
                    for c in cards:
                        iso = self._fecha_iso(c["fecha_raw"])
                        # Listado orden descendente: una fecha fuera de ventana
                        # significa que todo lo que sigue es mas viejo -> corta.
                        if iso and date.fromisoformat(iso) < corte:
                            cortar = True
                            break

                        detalle = self._obtener(ctx, c["url"], requiere_sesion=True)
                        det = self._parse_detalle(detalle, c) if detalle else None
                        if det is None:
                            det = {}

                        jobs.append(JobOffer(
                            source="ane",
                            search_term=search_term,
                            job_id=c["pid"],
                            title=det.get("titulo") or c["titulo"],
                            company=det.get("empresa") or c["empresa"],
                            location=det.get("ubicacion") or c["ubicacion"] or location,
                            link=c["url"],
                            date_published=iso,
                            description=det.get("descripcion") or "",
                            requirements=det.get("requisitos") or "",
                            salary=det.get("salario") or "",
                        ))
                        time.sleep(0.5)

                    if cortar:
                        break
                    pagina += 1
                    time.sleep(0.6)
        except Exception as e:
            logger.error(f"ANE barrido fallido: {e}")

        logger.info("ANE barrido: %d vacantes (<=%d paginas, ventana %d dias).",
                    len(jobs), pages, dias)
        return jobs

    # ------------------------------------------------------------ sesion
    def _asegurar_sesion(self, ctx) -> bool:
        """True si hay sesion activa (o si logra iniciarla). El listado solo
        muestra enlaces reales a /Puesto/Detalles cuando hay sesion; sin ella
        apuntan a /Cuenta/RegistroUsuario."""
        html = self._obtener(ctx, self.jobs_url)
        if html and "Puesto/Detalles" in html:
            return True
        if not (self.user and self.password):
            logger.warning("ANE: sin credenciales en .env ni sesion activa en Brave.")
            return False
        logger.info("ANE: iniciando sesion...")
        return self._login(ctx)

    def _login(self, ctx) -> bool:
        page = ctx.new_page()
        try:
            page.goto(self.login_url, wait_until="domcontentloaded")
            page.wait_for_timeout(1000)
            page.fill("#NombreUsuarioLogin", self.user)
            page.fill("#ContrasennaLogin", self.password)
            page.click("button[type='submit']")
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
            page.wait_for_timeout(1200)
            ok = page.query_selector("#NombreUsuarioLogin") is None
            logger.info("ANE: login %s.", "correcto" if ok else "fallido")
            return ok
        except Exception as e:
            logger.error(f"ANE login fallo: {e}")
            return False
        finally:
            page.close()

    def _obtener(self, ctx, url: str, requiere_sesion: bool = False,
                 intentos: int = 3):
        """GET por HTTP a traves de la sesion de Brave (comparte cookies)."""
        for i in range(intentos):
            try:
                resp = ctx.request.get(url, timeout=30000, fail_on_status_code=False)
                if resp.status == 404:
                    return None
                if resp.status >= 500:
                    raise RuntimeError(f"HTTP {resp.status}")
                html = resp.text()
                if requiere_sesion and not self._sesion_valida(html):
                    raise RuntimeError("sesion ANE caducada (redirige a login)")
                return html
            except Exception as e:
                espera = (2, 5, 10)[min(i, 2)]
                logger.warning("ANE fallo %s (%s); reintento %d/%d en %ds",
                               url, e, i + 1, intentos, espera)
                if i == intentos - 1:
                    logger.error("ANE agota reintentos: %s", url)
                    return None
                time.sleep(espera)
        return None

    @staticmethod
    def _sesion_valida(html: str) -> bool:
        if "Cuenta/RegistroUsuario" in html:
            return False
        if 'id="loginForm"' in html:
            return False
        if "Iniciar sesión" in html and "dashboard-content-inner" not in html:
            return False
        if "dashboard-content-inner" in html:
            return True
        return True

    # ------------------------------------------------------------ parseo
    @staticmethod
    def _fecha_iso(txt: str) -> str:
        """'Publicado el viernes, 25 de septiembre del 2026' -> '2026-09-25'.

        Cambia ' del ' por ' de ' para que parse_date capture el anio explicito
        (evita que 'diciembre del 2025' herede el anio actual al borde de anio).
        """
        if not txt:
            return ""
        t = txt.replace("Publicado el ", "").replace(" del ", " de ").strip()
        iso = parse_date(t)
        try:
            date.fromisoformat(iso[:10])
            return iso[:10]
        except Exception:
            return ""

    def _parse_listado(self, html: str) -> List[Dict[str, str]]:
        soup = BeautifulSoup(html, "html.parser")
        cards: List[Dict[str, str]] = []
        for job in soup.select("div.job-listing"):
            box = job.select_one("div.job-listing-description")
            if not box:
                continue
            titulo = job.select_one("h3.job-listing-title")
            empresa = job.select_one("h4.job-listing-company")
            small = job.select_one("small.job-listing-description")
            link = job.select_one("a[href*='/Puesto/Detalles']")
            href = link.get("href", "") if link else ""
            m = re.search(r"Puesto=(\d+)", href)
            if not (m and titulo):
                continue
            url = href if href.startswith("http") else BASE + href.split("&")[0]
            ubic = [self._limpiar(li.get_text()) for li in box.select("ul > li")]
            cards.append({
                "pid": m.group(1),
                "titulo": self._limpiar(titulo.get_text()),
                "empresa": self._limpiar(empresa.get_text()) if empresa else "",
                "fecha_raw": self._limpiar(small.get_text()) if small else "",
                "ubicacion": "; ".join(dict.fromkeys(x for x in ubic if x)),
                "url": url,
            })
        return cards

    @staticmethod
    def _campo(cont, label: str) -> str:
        """<p><strong>ETIQUETA</strong>: valor (a veces en <span>)."""
        if cont is None:
            return ""
        for p in cont.find_all("p"):
            s = p.find("strong")
            if s and _limpiar(s.get_text()).rstrip(":").strip().lower() == label.lower():
                txt = _limpiar(p.get_text(" ", strip=True))
                return txt.split(":", 1)[1].strip() if ":" in txt else ""
        return ""

    @staticmethod
    def _lista_tras(cont, label: str) -> List[str]:
        """<strong>ETIQUETA</strong>: ... <ul class="list-3"><li>...</li></ul>"""
        if cont is None:
            return []
        for s in cont.find_all("strong"):
            if label.lower() in _limpiar(s.get_text()).lower():
                ul = s.find_next("ul", class_="list-3")
                if ul:
                    return [_limpiar(li.get_text()) for li in ul.find_all("li")
                            if _limpiar(li.get_text())]
        return []

    def _parse_detalle(self, html: str, card: Dict[str, str]):
        """Devuelve solo lo que persiste JobPulse, o None si no es un detalle."""
        soup = BeautifulSoup(html, "html.parser")
        root = soup.select_one("div.dashboard-content-inner")
        if root is None:
            return None  # muro de login
        cajas = root.select("div.row > div.col-xl-6")
        izq = der = None
        if len(cajas) >= 1:
            izq = cajas[0].select_one("div.dashboard-box > div.content.with-padding")
        if len(cajas) >= 2:
            der = cajas[1].select_one("div.dashboard-box > div.content.with-padding")

        h2 = root.select_one("div.content.with-padding h2.text-center")
        sm = root.select_one("div.content.with-padding p.text-center small")

        desc = self._campo(der, "Funciones principales del empleo") \
            or self._campo(der, "Detalle del horario")
        return {
            "titulo": self._limpiar(h2.get_text()) if h2 else card["titulo"],
            "empresa": self._limpiar(sm.get_text()) if sm else card["empresa"],
            "ubicacion": "; ".join(self._lista_tras(izq, "Ubicaciones del empleo"))
            or card["ubicacion"],
            "descripcion": desc,
            "requisitos": self._campo(der, "Requisitos adicionales para el empleo"),
            "salario": self._campo(der, "Salario"),
        }
