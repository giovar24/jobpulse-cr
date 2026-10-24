# -*- coding: utf-8 -*-
"""providers.login.browser_sites — portales de empresa con login/Cloudflare.

Estos sitios NO tienen API publica; se recorren en el Brave DEL USUARIO
(ya logueado) por CDP. El scraper visita la URL, espera a que cargue el
listado, y extrae vacantes usando selectores CSS configurables.

Modos soportados:
- "dom": extraccion generica por selectores CSS del JSON (card_sel, title_sel,
  company_sel, loc_sel, url_sel, date_sel, next_sel).
- "page_fetch": fetch() de la API interna del SPA **dentro de la pagina**
  (mismo origen, cookies CF incluidas) y mapeo por dot-paths.

Flujo:
1. El caller (main.py) lanza Brave CDP y pasa `port`.
2. LoginSitesProvider.fetch() recibe `site` dict del Repository (con
   `site_config` que trae `mode`, `card_sel`, `login`, `urls`, etc.).
3. Si `mode == "page_fetch"`: hace fetch a la API interna y mapea.
4. Si `mode == "dom"` (default): navega cada URL, scroll/pagina, extrae
   con selectores, sigue `next_sel` para paginar.
5. Interstitial Cloudflare: deteccion por TITULO + body ("Un momento..." solo
   aparecia en <title>) + **auto-click del Turnstile** (en Brave real cedio
   solo en las pruebas).
6. login-wall/sin datos -> registra el pendiente en
   `data/empresas/pending_login.json` con motivo y SALTA (corrida sigue).
7. Lista vacia legitima = "0 posiciones", NO pendiente.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Dict, List, Optional

from playwright.async_api import BrowserContext, Page

from jobpulse_v15.credenciales import registrar_requerimiento

logger = logging.getLogger("jobpulse.login_sites")


class LoginSitesFetchError(RuntimeError):
    """Error obteniendo vacantes de sitios con login/Cloudflare."""
    pass


class LoginSitesOffers(list[dict[str, Any]]):
    """Ofertas del barrido de login_sites con conteo de omitidas."""

    def __init__(self, offers: list[dict[str, Any]], *, jobs_skipped: int = 0) -> None:
        super().__init__(offers)
        self.jobs_skipped = jobs_skipped


def _limpiar(t: str) -> str:
    return " ".join((t or "").split())


def _parse_date_relative(txt: str) -> Optional[str]:
    """Convierte 'hace 3 dias', '2 weeks ago' a ISO YYYY-MM-DD (aprox)."""
    if not txt:
        return None
    from datetime import datetime, timedelta, timezone
    txt = txt.lower().strip()
    now = datetime.now(timezone.utc)
    m = re.search(r"(\d+)\s*(min|minute|minuto)s?", txt)
    if m:
        return (now - timedelta(minutes=int(m.group(1)))).strftime("%Y-%m-%d")
    m = re.search(r"(\d+)\s*(hour|hora)s?", txt)
    if m:
        return (now - timedelta(hours=int(m.group(1)))).strftime("%Y-%m-%d")
    m = re.search(r"(\d+)\s*(day|dia|dias|día)s?", txt)
    if m:
        return (now - timedelta(days=int(m.group(1)))).strftime("%Y-%m-%d")
    m = re.search(r"(\d+)\s*(week|semana)s?", txt)
    if m:
        return (now - timedelta(weeks=int(m.group(1)))).strftime("%Y-%m-%d")
    m = re.search(r"(\d+)\s*(month|mes)s?", txt)
    if m:
        return (now - timedelta(days=int(m.group(1)) * 30)).strftime("%Y-%m-%d")
    return None


class LoginSitesProvider:
    """Barrido de portales que requieren sesion/Cloudflare en el Brave del usuario."""

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
        pages: int = 1,
        search_text: str | None = None,
        skip_source_job_ids: set[str] | None = None,
        port: int,
    ) -> LoginSitesOffers:
        """
        Barrido de portales con login/Cloudflare.

        `port`: puerto CDP de Brave (lo pasa el caller desde CLI/main).
        """
        if isinstance(pages, bool) or not isinstance(pages, int):
            raise ValueError("pages debe ser un entero.")
        if pages < 1 or pages > 50:
            raise ValueError("pages debe estar entre 1 y 50.")

        skipped_ids = skip_source_job_ids or set()
        cfg = site.get("site_config") or {}
        mode = cfg.get("mode", "dom")  # "dom" | "page_fetch"
        urls = site.get("urls") or [cfg.get("url", "")]

        async def _run():
            from playwright.async_api import async_playwright
            async with async_playwright() as p:
                browser = await p.chromium.connect_over_cdp(f"http://localhost:{port}")
                ctx = browser.contexts[0]
                if mode == "page_fetch":
                    return await self._fetch_api(ctx, site, cfg, urls, pages, skipped_ids)
                return await self._fetch_dom(ctx, site, cfg, urls, pages, skipped_ids)

        return asyncio.run(_run())

    async def _fetch_dom(
        self,
        ctx: BrowserContext,
        site: dict[str, Any],
        cfg: dict[str, Any],
        urls: List[str],
        pages: int,
        skipped_ids: set[str],
    ) -> LoginSitesOffers:
        """Extraccion por DOM con selectores CSS."""
        card_sel = cfg.get("card_sel")
        title_sel = cfg.get("title_sel", "h2, h3, h4, a")
        company_sel = cfg.get("company_sel")
        loc_sel = cfg.get("loc_sel", "h2, h3, .location, p")
        url_sel = cfg.get("url_sel", "a")
        date_sel = cfg.get("date_sel")
        next_sel = cfg.get("next_sel")
        login_cfg = cfg.get("login") or {}

        if not card_sel:
            logger.warning("LoginSites %s: sin card_sel, no se puede extraer DOM", site.get("site_id"))
            return LoginSitesOffers([], jobs_skipped=0)

        offers = []
        skipped = 0
        seen = set()

        for base_url in urls:
            if not base_url:
                continue
            for page_num in range(1, pages + 1):
                url = base_url if page_num == 1 else f"{base_url.rstrip('/')}?page={page_num}"
                logger.info("LoginSites DOM: %s pagina %d/%d", site.get("site_id"), page_num, pages)

                page = await ctx.new_page()
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=self.timeout * 1000)
                    await page.wait_for_timeout(3000)

                    # Detectar Cloudflare / login-wall
                    if await self._es_interstitial_o_login(page, login_cfg):
                        motivo = await self._manejar_interstitial(page, login_cfg)
                        if motivo:
                            registrar_requerimiento(
                                site.get("site_id", "desconocido"),
                                url, motivo, grupo="directos", obligatorio=False,
                                detectado="cloudflare_login")
                            logger.warning("LoginSites %s: %s — se salta", site.get("site_id"), motivo)
                            break
                        # si se resolvio, reintenta extraccion
                        await page.wait_for_timeout(2000)

                    # Extraer tarjetas
                    cards = await page.query_selector_all(card_sel)
                    if not cards:
                        logger.info("LoginSites %s: sin tarjetas en %s", site.get("site_id"), url)
                        break

                    for card in cards:
                        try:
                            title_el = await card.query_selector(title_sel)
                            title = _limpiar(await title_el.inner_text()) if title_el else ""
                            if not title:
                                continue

                            company = ""
                            if company_sel:
                                comp_el = await card.query_selector(company_sel)
                                company = _limpiar(await comp_el.inner_text()) if comp_el else ""

                            loc = ""
                            if loc_sel:
                                loc_el = await card.query_selector(loc_sel)
                                loc = _limpiar(await loc_el.inner_text()) if loc_el else ""

                            date_raw = ""
                            if date_sel:
                                date_el = await card.query_selector(date_sel)
                                date_raw = _limpiar(await date_el.inner_text()) if date_el else ""

                            link = ""
                            link_el = await card.query_selector(url_sel)
                            if link_el:
                                href = await link_el.get_attribute("href")
                                if href:
                                    link = href if href.startswith("http") else f"{url.split('/')[0]}//{url.split('/')[2]}{href}"

                            # source_job_id
                            source_job_id = link or title
                            source_job_id = re.sub(r"[^a-zA-Z0-9_-]", "_", source_job_id)[:128]

                            if source_job_id in seen or source_job_id in skipped_ids:
                                skipped += 1
                                continue
                            seen.add(source_job_id)

                            offers.append({
                                "source_job_id": source_job_id,
                                "source_url": link or url,
                                "source_updated_at": _parse_date_relative(date_raw),
                                "job": {
                                    "job_id": f"ls-{site.get('site_id', 'ls')}-{source_job_id}",
                                    "title": title,
                                    "company": company or site.get("company_name", ""),
                                    "location_raw": loc or "",
                                    "country": site.get("country", "CR"),
                                    "work_mode": "unknown",
                                    "employment_type": None,
                                    "date_published": _parse_date_relative(date_raw),
                                    "expires_at": None,
                                    "posting_language": "es",
                                    "posting_language_origin": "source",
                                    "description_raw": "",
                                    "requirements_raw": "",
                                    "salary_raw": "",
                                },
                                "raw_payload": {"detail_url": link or url},
                            })

                        except Exception as e:
                            logger.warning("LoginSites error extrayendo tarjeta: %s", e)

                    # Paginacion
                    if next_sel:
                        next_btn = await page.query_selector(next_sel)
                        if not next_btn or not await next_btn.is_enabled():
                            break
                        await next_btn.click()
                        await page.wait_for_timeout(3000)
                    else:
                        break

                    if self.request_delay:
                        await asyncio.sleep(self.request_delay)

                finally:
                    await page.close()

        logger.info("LoginSites DOM %s: %d ofertas, %d omitidas", site.get("site_id"), len(offers), skipped)
        return LoginSitesOffers(offers, jobs_skipped=skipped)

    async def _fetch_api(
        self,
        ctx: BrowserContext,
        site: dict[str, Any],
        cfg: dict[str, Any],
        urls: List[str],
        pages: int,
        skipped_ids: set[str],
    ) -> LoginSitesOffers:
        """Fetch de API interna del SPA (page_fetch mode)."""
        api_cfg = cfg.get("api") or {}
        api_url = api_cfg.get("url")
        list_path = api_cfg.get("list_path", "$.data")  # JSONPath
        item_paths = api_cfg.get("item") or {}  # dot-paths por campo

        if not api_url:
            logger.warning("LoginSites %s: page_fetch mode requiere api.url en config", site.get("site_id"))
            return LoginSitesOffers([], jobs_skipped=0)

        offers = []
        skipped = 0
        seen = set()

        for base_url in urls:
            if not base_url:
                continue
            page = await ctx.new_page()
            try:
                await page.goto(base_url, wait_until="domcontentloaded", timeout=self.timeout * 1000)
                await page.wait_for_timeout(3000)

                # Detectar Cloudflare / login-wall
                login_cfg = (cfg.get("login") or {})
                if await self._es_interstitial_o_login(page, login_cfg):
                    motivo = await self._manejar_interstitial(page, login_cfg)
                    if motivo:
                        registrar_requerimiento(
                            site.get("site_id", "desconocido"),
                            base_url, motivo, grupo="directos", obligatorio=False,
                            detectado="cloudflare_login")
                        logger.warning("LoginSites %s: %s — se salta", site.get("site_id"), motivo)
                        break

                for page_num in range(1, pages + 1):
                    page_url = f"{api_url}{page_num}"
                    try:
                        resp = await page.context.request.get(page_url, timeout=self.timeout * 1000)
                        if resp.status != 200:
                            logger.warning("LoginSites API %s pagina %d: HTTP %d", site.get("site_id"), page_num, resp.status)
                            break
                        data = await resp.json()

                        # Extraer lista con JSONPath simple
                        items = data
                        for part in list_path.strip("$. ").split("."):
                            if part.startswith("["):
                                idx = int(part.strip("[]"))
                                items = items[idx] if isinstance(items, list) and len(items) > idx else []
                            else:
                                items = items.get(part, []) if isinstance(items, dict) else []
                        if not items:
                            break

                        for item in items:
                            source_job_id = str(item.get(item_paths.get("source_job_id", "id"), ""))
                            if not source_job_id or source_job_id in seen or source_job_id in skipped_ids:
                                skipped += 1
                                continue
                            seen.add(source_job_id)

                            title = str(item.get(item_paths.get("title", "title"), ""))
                            if not title:
                                continue

                            company = str(item.get(item_paths.get("company", "company"), ""))
                            loc = str(item.get(item_paths.get("location", "location"), ""))
                            date_raw = str(item.get(item_paths.get("date", "date"), ""))
                            link = str(item.get(item_paths.get("url", "url"), ""))

                            offers.append({
                                "source_job_id": source_job_id,
                                "source_url": link or base_url,
                                "source_updated_at": _parse_date_relative(date_raw),
                                "job": {
                                    "job_id": f"ls-{site.get('site_id', 'ls')}-{source_job_id}",
                                    "title": _limpiar(title),
                                    "company": _limpiar(company) or site.get("company_name", ""),
                                    "location_raw": _limpiar(loc) or "",
                                    "country": site.get("country", "CR"),
                                    "work_mode": "unknown",
                                    "employment_type": None,
                                    "date_published": _parse_date_relative(date_raw),
                                    "expires_at": None,
                                    "posting_language": "es",
                                    "posting_language_origin": "source",
                                    "description_raw": _limpiar(str(item.get(item_paths.get("description", "description"), ""))),
                                    "requirements_raw": _limpiar(str(item.get(item_paths.get("requirements", "requirements"), ""))),
                                    "salary_raw": str(item.get(item_paths.get("salary", "salary"), "")),
                                },
                                "raw_payload": item,
                            })

                        if self.request_delay:
                            await asyncio.sleep(self.request_delay)

                    except Exception as e:
                        logger.warning("LoginSites API %s pagina %d error: %s", site.get("site_id"), page_num, e)

                # fin del for page_num

            finally:
                await page.close()

        logger.info("LoginSites API %s: %d ofertas, %d omitidas", site.get("site_id"), len(offers), skipped)
        return LoginSitesOffers(offers, jobs_skipped=skipped)

    async def _es_interstitial_o_login(self, page: Page, login_cfg: dict) -> bool:
        """Detecta Cloudflare Turnstile o login-wall."""
        titulo = (await page.title() or "").lower()
        # Cloudflare "Un momento..." suele aparecer solo en <title>
        if "un momento" in titulo or "just a moment" in titulo or "checking your browser" in titulo:
            return True
        # Login-wall: buscar formulario de login
        if login_cfg.get("login_url"):
            try:
                if await page.query_selector(login_cfg.get("user_sel") or "input[type=email]") \
                   and await page.query_selector(login_cfg.get("pass_sel") or "input[type=password]"):
                    return True
            except Exception:
                pass
        # Textos tipicos
        body = (await page.inner_text("body"))[:2000].lower()
        if any(x in body for x in ("inicia sesion", "iniciar sesion", "sign in", "log in", "acceder")):
            return True
        return False

    async def _manejar_interstitial(self, page: Page, login_cfg: dict) -> Optional[str]:
        """Maneja Cloudflare Turnstile (auto-click) o login-wall."""
        # 1) Cloudflare Turnstile: buscar iframe y click en checkbox
        try:
            cf_iframe = page.frame_locator('iframe[src*="challenges.cloudflare.com"], iframe[src*="turnstile"]')
            checkbox = cf_iframe.locator('input[type="checkbox"], [role="checkbox"]').first
            if await checkbox.count() > 0:
                await checkbox.click(timeout=5000)
                await page.wait_for_timeout(3000)
                # Verificar si paso
                if not await self._es_interstitial_o_login(page, login_cfg):
                    logger.info("Cloudflare Turnstile resuelto automaticamente")
                    return None
        except Exception:
            pass

        # 2) Login-wall: si hay login_cfg, avisar pero NO hacer auto-login aqui
        # (el auto-login se maneja en providers.login.auto_login desde el scraper correspondiente)
        if login_cfg.get("login_url"):
            return "login-wall detectado (se necesita sesion manual)"

        return "interstitial Cloudflare no resuelto automaticamente"


def _limpiar(t: str) -> str:
    return " ".join((t or "").split())