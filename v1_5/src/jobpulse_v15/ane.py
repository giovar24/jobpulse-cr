"""Adaptador ANE (ane.cr) para v1.5 — arquitectura Repository + IngestionService.

Barrido por fecha (el buscador está roto). Requiere sesión de Brave (Playwright CDP).
Usa _conocidos() para no re-bajar detalles de vacantes ya guardadas CON descripción.
Corta por ventana de fecha + racha de CONOCIDOS_SEGUIDOS (15) + página sin fechas.

El fetcher NO gestiona navegador: se le pasa `site` dict del Repository y se espera
que el caller (CLI) inicie Brave y pase `port` via site_config.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sqlite3
import time
import unicodedata
from datetime import date, timedelta
from typing import Any, Dict, List

from playwright.async_api import BrowserContext, Page

logger = logging.getLogger("jobpulse.ane")

BASE = "https://www.ane.cr"
CONOCIDOS_SEGUIDOS = int(os.getenv("ANE_KNOWN_STREAK", "15"))
MAX_PAGES = 50


class AneFetchError(RuntimeError):
    """Error obteniendo vacantes de ANE."""


class AneOffers(list[dict[str, Any]]):
    """Ofertas del barrido ANE con conteo de omitidas."""

    def __init__(self, offers: list[dict[str, Any]], *, jobs_skipped: int = 0) -> None:
        super().__init__(offers)
        self.jobs_skipped = jobs_skipped


def _limpiar(t: str) -> str:
    return " ".join((t or "").split())


def _norm_lab(s: str) -> str:
    """Etiqueta sin acentos ni ':' final, para comparar robustamente."""
    s = unicodedata.normalize("NFD", s or "")
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return _limpiar(s).rstrip(":").strip().lower()


# Secciones <strong> + <ul class="list-3"> que ANE usa en el detalle
_REQ_UL = (
    "habilidades importantes",
    "requisitos adicionales",
    "licencias de conducir",
    "idiomas",
    "programas de computo",
    "vehiculos",
)


def _parse_date_es(txt: str) -> str:
    """'Publicado el viernes, 25 de septiembre del 2026' -> '2026-09-25'."""
    if not txt:
        return ""
    t = txt.replace("Publicado el ", "").replace(" del ", " de ").strip()
    meses = {
        "enero": "01", "febrero": "02", "marzo": "03", "abril": "04",
        "mayo": "05", "junio": "06", "julio": "07", "agosto": "08",
        "septiembre": "09", "octubre": "10", "noviembre": "11", "diciembre": "12",
    }
    try:
        parts = t.split()
        if len(parts) >= 5 and parts[1].isdigit() and parts[2] == "de" and parts[3] in meses:
            dia = int(parts[1])
            mes = meses[parts[3]]
            anio = int(parts[4])
            return f"{anio:04d}-{mes}-{dia:02d}"
    except Exception:
        pass
    return ""


class AneFetcher:
    """Barrido por fecha de ANE (lista paginada + detalle por HTTP en sesión Brave)."""

    def __init__(
        self,
        *,
        timeout: float = 30.0,
        request_delay: float = 0.5,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout debe ser mayor que cero.")
        if request_delay < 0 or request_delay > 10:
            raise ValueError("request_delay debe estar entre 0 y 10 segundos.")
        self.timeout = timeout
        self.request_delay = request_delay

    def fetch(
        self,
        site: dict[str, Any],
        *,
        pages: int = 10,
        search_text: str | None = None,
        skip_source_job_ids: set[str] | None = None,
        port: int,
        dias: int = 30,
    ) -> AneOffers:
        """
        Barrido por fecha: recorre páginas ordenadas por fecha descendente.
        `port`: puerto CDP de Brave (lo pasa el caller desde site_config o CLI).
        `dias`: ventana de vigencia hacia atrás desde hoy.
        """
        if isinstance(pages, bool) or not isinstance(pages, int):
            raise ValueError("pages debe ser un entero.")
        if pages < 1 or pages > MAX_PAGES:
            raise ValueError(f"pages debe estar entre 1 y {MAX_PAGES}.")
        if dias <= 0:
            raise ValueError("dias debe ser positivo.")

        # Conectar a Brave via CDP
        from playwright.async_api import async_playwright

        async def _run():
            async with async_playwright() as p:
                browser = await p.chromium.connect_over_cdp(f"http://localhost:{port}")
                ctx = browser.contexts[0]
                return await self._sweep(ctx, site, pages, dias, skip_source_job_ids)

        return asyncio.run(_run())

    async def _sweep(
        self,
        ctx: BrowserContext,
        site: dict[str, Any],
        pages: int,
        dias: int,
        skip_source_job_ids: set[str] | None,
    ) -> AneOffers:
        if not await self._asegurar_sesion(ctx):
            logger.warning("ANE: sin sesión de detalle; barrido omitido.")
            return AneOffers([], jobs_skipped=0)

        cutoff_date = date.today() - timedelta(days=dias)
        conocidos = self._conocidos(site.get("db_path"))
        skipped_ids = skip_source_job_ids or set()
        offers = []
        skipped = 0
        seen_ids = set()
        motivo = ""

        for page_num in range(1, pages + 1):
            logger.info("ANE: barrido página %d/%d", page_num, pages)
            page = await ctx.new_page()
            try:
                html = await self._obtener(page, f"{BASE}/Puesto?Pagina={page_num}")
                if not html:
                    break
                cards = self._parse_listado(html)
                if not cards:
                    logger.info("ANE: página %d sin tarjetas, fin del barrido", page_num)
                    break

                cortar = False
                con_fecha = 0
                for c in cards:
                    iso = _parse_date_es(c["fecha_raw"])
                    if iso:
                        con_fecha += 1
                        try:
                            if date.fromisoformat(iso) < cutoff_date:
                                cortar, motivo = True, "fecha fuera de ventana"
                                break
                        except ValueError:
                            pass

                    if c["pid"] in conocidos:
                        skipped += 1
                        continue

                    detalle_html = await self._obtener(page, c["url"], requiere_sesion=True)
                    det = self._parse_detalle(detalle_html, c) if detalle_html else {}

                    offers.append({
                        "source_job_id": c["pid"],
                        "source_url": c["url"],
                        "source_updated_at": iso,
                        "job": {
                            "job_id": f"ane-{c['pid']}",
                            "title": det.get("titulo") or c["titulo"],
                            "company": det.get("empresa") or c["empresa"],
                            "location_raw": det.get("ubicacion") or c["ubicacion"],
                            "country": "CR",
                            **self._normalize_cr_location(det.get("ubicacion") or c["ubicacion"]),
                            "work_mode": "unknown",
                            "employment_type": None,
                            "date_published": iso,
                            "expires_at": det.get("vence") or "",
                            "posting_language": "es",
                            "posting_language_origin": "source",
                            "description_raw": det.get("descripcion") or "",
                            "requirements_raw": det.get("requisitos") or "",
                            "salary_raw": det.get("salario") or "",
                        },
                        "raw_payload": {"detail_url": c["url"]},
                    })
                    seen_ids.add(c["pid"])

                    if self.request_delay:
                        await asyncio.sleep(self.request_delay)

                if not con_fecha:
                    logger.warning(
                        "ANE: página %d sin fechas legibles (ej: %r); se corta el barrido.",
                        page_num, cards[0]["fecha_raw"])
                    cortar = True
                    motivo = motivo or "pagina sin fecha"

                if cortar:
                    break

            finally:
                await page.close()

        logger.info(
            "ANE barrido: %d nuevas, %d ya guardadas saltadas (tope %d páginas, ventana %d días; corte: %s).",
            len(offers), skipped, pages, dias, motivo or "fin")
        return AneOffers(offers, jobs_skipped=skipped)

    def _conocidos(self, db_path: str | None) -> set[str]:
        """job_ids ANE ya guardados CON descripción (no hay que re-bajar detalle)."""
        if not db_path or not os.path.exists(db_path):
            return set()
        try:
            con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
            try:
                return {str(r[0]) for r in con.execute(
                    "SELECT job_id FROM jobs_raw WHERE source = 'ane' "
                    "AND description IS NOT NULL AND description <> ''")}
            finally:
                con.close()
        except Exception as e:
            logger.warning("ANE: no se pudo leer jobs_raw (%s); barrido sin memoria.", e)
            return set()

    async def _asegurar_sesion(self, ctx: BrowserContext) -> bool:
        """True si hay sesión activa (verifica que el listado muestra enlaces a /Puesto/Detalles)."""
        page = await ctx.new_page()
        try:
            await page.goto(f"{BASE}/Puesto", wait_until="domcontentloaded", timeout=self.timeout * 1000)
            html = await page.content()
            return "Puesto/Detalles" in html
        except Exception as e:
            logger.error("ANE: error verificando sesión: %s", e)
            return False
        finally:
            await page.close()

    async def _obtener(self, page: Page, url: str, requiere_sesion: bool = False) -> str | None:
        """GET por HTTP a través de la sesión de Brave (comparte cookies)."""
        for i in range(3):
            try:
                resp = await page.context.request.get(url, timeout=self.timeout * 1000, fail_on_status_code=False)
                if resp.status == 404:
                    return None
                if resp.status >= 500:
                    raise RuntimeError(f"HTTP {resp.status}")
                html = await resp.text()
                if requiere_sesion and not self._sesion_valida(html):
                    raise RuntimeError("sesión ANE caducada (redirige a login)")
                return html
            except Exception as e:
                espera = (2, 5, 10)[min(i, 2)]
                logger.warning("ANE fallo %s (%s); reintento %d/3 en %ds", url, e, i + 1, espera)
                if i == 2:
                    logger.error("ANE agota reintentos: %s", url)
                    return None
                await asyncio.sleep(espera)
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

    def _parse_listado(self, html: str) -> List[Dict[str, str]]:
        from bs4 import BeautifulSoup
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
            ubic = [_limpiar(li.get_text()) for li in box.select("ul > li")]
            cards.append({
                "pid": m.group(1),
                "titulo": _limpiar(titulo.get_text()),
                "empresa": _limpiar(empresa.get_text()) if empresa else "",
                "fecha_raw": _limpiar(small.get_text()) if small else "",
                "ubicacion": "; ".join(dict.fromkeys(x for x in ubic if x)),
                "url": url,
            })
        return cards

    @staticmethod
    def _campo(cont, label: str) -> str:
        """<p><strong>ETIQUETA</strong>: valor (a veces en <span>)."""
        if cont is None:
            return ""
        from bs4 import BeautifulSoup
        for p in cont.find_all("p"):
            s = p.find("strong")
            if s and _limpiar(s.get_text()).rstrip(":").strip().lower() == label.lower():
                txt = _limpiar(p.get_text(" ", strip=True))
                return txt.split(":", 1)[1].strip() if ":" in txt else ""
        return ""

    @staticmethod
    def _lista_tras(cont, label: str) -> List[str]:
        if cont is None:
            return []
        from bs4 import BeautifulSoup
        for s in cont.find_all("strong"):
            if label.lower() in _limpiar(s.get_text()).lower():
                ul = s.find_next("ul", class_="list-3")
                if ul:
                    return [_limpiar(li.get_text()) for li in ul.find_all("li") if _limpiar(li.get_text())]
        return []

    @staticmethod
    def _secciones_ul(cont) -> List[tuple]:
        """TODAS las secciones <strong> + <ul class="list-3"> de una columna."""
        out: List[tuple] = []
        vistos = set()
        if cont is None:
            return out
        from bs4 import BeautifulSoup
        for st in cont.find_all("strong"):
            visible = _limpiar(st.get_text()).rstrip(":").strip()
            lab = _norm_lab(visible)
            if not lab or lab in vistos:
                continue
            ul = st.find_next("ul", class_="list-3")
            if ul is None or ul.find_previous("strong") is not st:
                continue
            items = [i for i in (_limpiar(li.get_text()) for li in ul.find_all("li")) if i]
            if items:
                vistos.add(lab)
                out.append((lab, visible, items))
        return out

    def _parse_detalle(self, html: str, card: Dict[str, str]) -> Dict[str, str]:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        root = soup.select_one("div.dashboard-content-inner")
        if root is None:
            return {}  # muro de login
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
        extra = self._campo(der, "Detalles del salario")
        if extra and extra not in desc:
            desc = f"{desc} | {extra}".strip(" |") if desc else extra

        reqs = [self._campo(der, "Requisitos adicionales para el empleo")]
        for lab_n, lab_txt, items in self._secciones_ul(der):
            if lab_n.startswith(_REQ_UL):
                reqs.append(f"{lab_txt}: " + ", ".join(items))
        reqs = [r for r in reqs if r]

        return {
            "titulo": _limpiar(h2.get_text()) if h2 else card["titulo"],
            "empresa": _limpiar(sm.get_text()) if sm else card["empresa"],
            "ubicacion": "; ".join(self._lista_tras(izq, "Ubicaciones del empleo")) or card["ubicacion"],
            "descripcion": desc,
            "requisitos": ". ".join(reqs),
            "salario": self._campo(der, "Salario"),
            "vence": _parse_date_es(self._campo(der, "Fecha de vencimiento de la vacante")),
        }

    @staticmethod
    def _normalize_cr_location(location: str) -> Dict[str, str | None]:
        if not location:
            return {"city": None, "region": None}
        loc = location.lower()
        provinces = {
            "alajuela": "Alajuela", "cartago": "Cartago", "guanacaste": "Guanacaste",
            "heredia": "Heredia", "limón": "Limón", "limon": "Limón",
            "puntarenas": "Puntarenas", "san josé": "San José", "san jose": "San José",
        }
        for key, prov in provinces.items():
            if key in loc:
                parts = [p.strip() for p in location.split(",")]
                if len(parts) >= 2 and parts[-1].lower() == key:
                    return {"city": parts[0], "region": prov}
                return {"city": None, "region": prov}
        return {"city": None, "region": None}