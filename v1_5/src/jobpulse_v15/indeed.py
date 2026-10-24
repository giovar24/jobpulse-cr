"""Adaptador Indeed (cr.indeed.com) para v1.5 — versión probada del proyecto principal.

Extrae el listado desde `window.mosaic` (JSON embebido) — mucho más robusto que DOM.
Obtiene descripción completa vía `/rpc/jobdescs` usando la sesión del navegador.
REGLA DE ORO: SESIÓN PRIMERO — reusa sesión de Brave; SSO Google solo si NO hay sesión
y credencial con auto_login activo (una vez por corrida).
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Dict, List, Tuple
from urllib.parse import quote_plus

import httpx
from playwright.async_api import Page

from jobpulse_v15.location_utils import normalize_cr_location

logger = logging.getLogger("jobpulse.indeed")

_MAX_PAGES = 10


class IndeedFetchError(RuntimeError):
    """Error obteniendo vacantes de Indeed."""


class IndeedOffers(list[dict[str, Any]]):
    def __init__(self, offers: list[dict[str, Any]], *, jobs_skipped: int = 0) -> None:
        super().__init__(offers)
        self.jobs_skipped = jobs_skipped


def _epoch_to_date(ms: Any) -> str:
    """Convierte epoch ms de Indeed a fecha ISO YYYY-MM-DD."""
    from datetime import datetime, timezone
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError):
        return ""


def _parse_date_relative(txt: str) -> str | None:
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
    m = re.search(r"(\d+)\s*(day|día|dias)s?", txt)
    if m:
        return (now - timedelta(days=int(m.group(1)))).strftime("%Y-%m-%d")
    m = re.search(r"(\d+)\s*(week|semana)s?", txt)
    if m:
        return (now - timedelta(weeks=int(m.group(1)))).strftime("%Y-%m-%d")
    m = re.search(r"(\d+)\s*(month|mes)s?", txt)
    if m:
        return (now - timedelta(days=int(m.group(1)) * 30)).strftime("%Y-%m-%d")
    return None


def _limpiar_texto(text: str, conservar_saltos: bool = False) -> str:
    if not text:
        return ""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(text, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    if conservar_saltos:
        text = soup.get_text("\n", strip=True)
    else:
        text = soup.get_text(" ", strip=True)
    return " ".join(text.split())


def _split_requirements(text: str) -> tuple[str, str]:
    if not text:
        return "", ""
    for sep in ["\n\nRequisitos", "\n\nRequirements", "\nRequisitos:", "\nRequirements:"]:
        if sep in text:
            parts = text.split(sep, 1)
            return parts[0].strip(), sep.replace("\n", "").strip() + " " + parts[1].strip()
    return text, ""


# Indeed embebe el listado en window.mosaic — leerlo es robusto
_JS_EXTRACT_JOBS = """
() => {
    const pd = window.mosaic && window.mosaic.providerData && window.mosaic.providerData['mosaic-provider-jobcards'];
    const model = pd && pd.metaData && pd.metaData.mosaicProviderJobCardsModel;
    const results = (model && model.results) || (pd && pd.results) || [];
    return results.map(r => ({
        jk: r.jobkey || '',
        title: r.displayTitle || r.title || '',
        company: r.company || '',
        location: r.formattedLocation || '',
        pubDate: r.pubDate || r.createDate || 0,
        relative: r.formattedRelativeTime || '',
        snippet: r.snippet || '',
        salary: (r.salarySnippet && (r.salarySnippet.text || r.salarySnippet.formattedRange)) || '',
        link: r.viewJobLink || r.link || ''
    }));
}
"""

# Respaldo por DOM si el modelo JSON no está disponible
_JS_EXTRACT_JOBS_FALLBACK = """
() => {
    const out = [];
    const seen = new Set();
    document.querySelectorAll('.cardOutline.tapItem, .jobsearch-JobResultCard, .job_seen_beacon').forEach(card => {
        let jk = card.getAttribute('data-jk') || '';
        if (!jk) { const m = (card.className || '').match(/job_([0-9a-f]{16})/); if (m) jk = m[1]; }
        if (!jk) { const a = card.querySelector('a[href*="jk="]'); if (a) { const m = a.getAttribute('href').match(/jk=([0-9a-f]{16})/); if (m) jk = m[1]; } }
        if (!jk || seen.has(jk)) return;
        seen.add(jk);
        const titleEl = card.querySelector('h2.jcs-JobTitle, h2.jobTitle, [data-testid="job-title"]');
        const companyEl = card.querySelector('[data-testid="company-name"], .companyName');
        const locationEl = card.querySelector('[data-testid="text-location"], .companyLocation');
        const snipEl = card.querySelector('.job-snippet, [data-testid="belowJobSnippet"], .summary');
        const m = (card.innerText || '').match(/(hace\\s+\\d+\\+?\\s*(?:minuto|hora|d[ií]a|semana|mes)\\w*)/i);
        out.push({
            jk: jk,
            title: titleEl ? titleEl.innerText.trim() : '',
            company: companyEl ? companyEl.innerText.trim() : '',
            location: locationEl ? locationEl.innerText.trim() : '',
            pubDate: 0,
            relative: m ? m[1] : '',
            snippet: snipEl ? snipEl.innerText.trim() : '',
            salary: '',
            link: 'https://cr.indeed.com/viewjob?jk=' + jk
        });
    });
    return out;
}
"""


class IndeedFetcher:
    """Scraper Indeed con sesión Brave + rpc/jobdescs para descripción completa."""

    def __init__(
        self,
        *,
        timeout: float = 30.0,
        request_delay: float = 0.5,
        brave_bin: str | None = None,
        brave_perfil: str | None = None,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout debe ser mayor que cero.")
        if request_delay < 0 or request_delay > 10:
            raise ValueError("request_delay debe estar entre 0 y 10 segundos.")
        self.timeout = timeout
        self.request_delay = request_delay
        self.brave_bin = brave_bin
        self.brave_perfil = brave_perfil
        self.base_url = "https://cr.indeed.com/jobs"
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
        }
        self._login_intents = 0  # SSO se intenta UNA vez por corrida

    # Marcadores de login-wall (rotan; cubrimos dos idiomas)
    _MURO_TITULO = ["iniciar sesion", "inicia sesion", "sign in", "log in",
                    "create an account", "cuentas indeed"]
    _MURO_CUERPO = ["inicia sesion para ver", "iniciar sesion para ver",
                    "sign in to see", "create an account to see",
                    "crea una cuenta para ver", "crea tu cuenta para ver"]

    async def _muro_login(self, page: Page) -> bool:
        try:
            titulo = (await page.title() or "").lower()
            cuerpo = (await page.inner_text("body"))[:1500].lower()
        except Exception:
            return False
        return (any(m in titulo for m in self._MURO_TITULO)
                or any(m in cuerpo for m in self._MURO_CUERPO))

    async def _hay_sesion(self, page: Page) -> bool:
        """True SOLO con evidencia POSITIVA de sesión Indeed."""
        try:
            await page.goto("https://secure.indeed.com/account",
                          wait_until="domcontentloaded", timeout=30_000)
            try:
                await page.wait_for_url(re.compile(r"/(auth|settings)"), timeout=8_000)
            except Exception:
                pass
            url = (page.url or "").lower()
            if "/auth" in url or "/account/login" in url:
                return False
            if await page.query_selector("input[type=password], input[name=email]"):
                return False
            cuerpo = (await page.inner_text("body"))[:2000].lower()
            if any(m in cuerpo for m in ("crea una cuenta o inicia sesi",
                                          "continuar con google",
                                          "direccion de email",
                                          "dirección de email",
                                          "create an account or sign in")):
                return False
            return "/settings" in url or "cerrar sesi" in cuerpo \
                or "sign out" in cuerpo
        except Exception:
            return False   # sin poder verificar -> se asume sin sesión

    async def _auto_login(self, page: Page) -> bool:
        """REGLA 'sesión primero' (igual que LinkedIn):
        1. Si hay sesión activa -> NO se toca login.
        2. Solo si NO hay sesión, y hay credencial + auto_login, se intenta SSO Google UNA vez.
        3. Si falla (CAPTCHA/2FA), la corrida sigue y el sitio queda pendiente manual.
        """
        try:
            # 1) Sesión activa -> nada que loguear
            if await self._hay_sesion(page):
                logger.info("Indeed: hay SESIÓN activa en el navegador -> no se usa el SSO.")
                return True
            # 2) Sin sesión: ¿credencial + auto_login? ¿ya se intentó hoy?
            # En v1.5 la gestión de credenciales va en providers/login/auto_login.py
            # Por ahora solo logueamos warning
            if self._login_intents >= 1:
                logger.info("Indeed: SSO ya intentado en esta corrida; no se reintenta.")
                return False
            self._login_intents += 1
            logger.info("Indeed: SIN sesión -> auto-login SSO Google NO implementado aún en v1.5.")
            return False
        except Exception as e:
            logger.warning("Indeed auto-login fallo: %s", e)
            return False

    async def _enrich_descriptions(self, page: Page, jobs: List[Dict[str, Any]]) -> None:
        """Obtiene descripción completa vía /rpc/jobdescs usando la sesión del navegador."""
        targets = [j for j in jobs if re.fullmatch(r"[0-9a-f]{16}", j["source_job_id"] or "")]
        if not targets:
            return

        jks = [j["source_job_id"] for j in targets]
        got: Dict[str, str] = {}
        for i in range(0, len(jks), 20):
            batch = jks[i:i + 20]
            try:
                res = await page.evaluate(
                    """async (jks) => {
                        try {
                            const r = await fetch('/rpc/jobdescs?jks=' + encodeURIComponent(jks.join(',')), 
                                {headers: {'Accept': 'application/json'}});
                            if (!r.ok) return {ok: false, status: r.status};
                            return {ok: true, data: await r.json()};
                        } catch (e) { return {ok: false, error: String(e)}; }
                    }""",
                    batch,
                )
            except Exception as e:
                logger.warning(f"Indeed rpc/jobdescs batch failed: {e}")
                res = {"ok": False}
            if res.get("ok") and isinstance(res.get("data"), dict):
                got.update(res["data"])
            else:
                logger.warning(f"Indeed rpc/jobdescs no disponible: {res}")

        for job in targets:
            raw = got.get(job["source_job_id"])
            if raw:
                job["job"]["description_raw"] = _limpiar_texto(raw, conservar_saltos=True)

        filled = sum(1 for j in targets if j["job"]["description_raw"])
        logger.info(f"Indeed: descripciones completas obtenidas para {filled}/{len(targets)} vacantes")

    async def fetch(
        self,
        site: dict[str, Any],
        *,
        pages: int = 2,
        search_text: str | None = None,
        skip_source_job_ids: set[str] | None = None,
        port: int = 9222,
    ) -> IndeedOffers:
        if isinstance(pages, bool) or not isinstance(pages, int):
            raise ValueError("pages debe ser un entero.")
        if pages < 1 or pages > _MAX_PAGES:
            raise ValueError(f"pages debe estar entre 1 y {_MAX_PAGES}.")

        search_term = search_text or site.get("site_config", {}).get("search_text") or "Soporte Técnico"
        location = site.get("country") or "Costa Rica"
        skipped_ids = skip_source_job_ids or set()

        # Conectar a Brave via CDP usando el puerto pasado por el caller
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(f"http://localhost:{port}")
            ctx = browser.contexts[0]
            basic_jobs, cookies = await self._run_sync_scraping(ctx, search_term, location, pages, skipped_ids)
        if not basic_jobs:
            return IndeedOffers([], jobs_skipped=0)

        # Enriquecer con HTTP (último recurso, Indeed responde 403 a httpx)
        enriched_jobs = []
        async with httpx.AsyncClient(
            headers=self.headers, cookies=cookies, timeout=15.0, follow_redirects=True
        ) as client:
            for job in basic_jobs:
                source_url = job.get("source_url")
                if source_url and not job["job"].get("description_raw"):
                    details = await self._fetch_details_http(client, source_url)
                    job["job"]["description_raw"] = details.get("description") or job["job"].get("description_raw")
                    job["job"]["requirements_raw"] = details.get("requirements") or job["job"].get("requirements_raw")
                    job["job"]["salary_raw"] = details.get("salary") or job["job"].get("salary_raw")
                if job["job"]["description_raw"] and not job["job"]["requirements_raw"]:
                    job["job"]["description_raw"], job["job"]["requirements_raw"] = _split_requirements(
                        job["job"]["description_raw"]
                    )
                enriched_jobs.append(job)

        logger.info("Indeed: %d ofertas enriquecidas", len(enriched_jobs))
        return IndeedOffers(enriched_jobs, jobs_skipped=0)

    async def _fetch_details_http(self, client: httpx.AsyncClient, url: str) -> Dict[str, Any]:
        """Último recurso: intenta la descripción por HTTP (Indeed responde 403 a httpx)."""
        try:
            response = await client.get(url)
            if response.status_code != 200:
                return {}
            match = re.search(
                r'<div[^>]+id=["\']jobDescriptionText["\'][^>]*>(.*?)</div>',
                response.text,
                re.DOTALL | re.IGNORECASE,
            )
            if match:
                return {"description": _limpiar_texto(match.group(1), conservar_saltos=True),
                        "requirements": "", "salary": ""}
            return {}
        except Exception as e:
            logger.warning(f"HTTP detail fetch failed for Indeed {url}: {e}")
            return {}

    async def _run_sync_scraping(
        self, ctx: BrowserContext, search_term: str, location: str, pages: int, skipped_ids: set[str]
    ) -> Tuple[List[Dict[str, Any]], Dict[str, str]]:
        jobs: List[Dict[str, Any]] = []
        cookies: Dict[str, str] = {}
        page = await ctx.new_page()
        try:
            await page.set_extra_http_headers({"Accept-Language": "es-ES,es;q=0.9,en;q=0.8"})

            url = f"{self.base_url}?q={quote_plus(search_term)}&l={quote_plus(location)}"
            await page.goto(url, wait_until="domcontentloaded")
            await page.wait_for_timeout(3000)

            seen: set = set()
            for _ in range(pages):
                extracted = await page.evaluate(_JS_EXTRACT_JOBS) or []
                if not extracted:
                    extracted = await page.evaluate(_JS_EXTRACT_JOBS_FALLBACK) or []
                if not extracted:
                    logger.warning(f"Indeed page found 0 cards for '{search_term}' (title: {await page.title()})")
                    if not seen and self._muro_login(page):
                        logger.warning("Indeed: muro de login detectado.")
                        # Registrar en lista de sitios que piden login (caller se encarga)
                        if await self._auto_login(page):
                            await page.goto(url, wait_until="domcontentloaded")
                            await page.wait_for_timeout(3500)
                            extracted = await page.evaluate(_JS_EXTRACT_JOBS) or []

                for item in extracted:
                    jk = item.get("jk") or ""
                    if not jk:
                        m = re.search(r"jk=([0-9a-f]{16})", item.get("link", "") or "")
                        jk = m.group(1) if m else ""
                    if not jk or jk in seen:
                        continue
                    if jk in skipped_ids:
                        continue
                    seen.add(jk)

                    date_iso = _epoch_to_date(item.get("pubDate")) or _parse_date_relative(item.get("relative", ""))
                    loc_raw = _limpiar_texto(item.get("location", "")) or location

                    jobs.append({
                        "source_job_id": jk,
                        "source_url": f"https://cr.indeed.com/viewjob?jk={jk}",
                        "source_updated_at": date_iso,
                        "job": {
                            "job_id": f"indeed-{jk}",
                            "title": _limpiar_texto(item.get("title", "")),
                            "company": _limpiar_texto(item.get("company", "")) or "Unknown",
                            "location_raw": loc_raw,
                            "country": "CR",
                            **normalize_cr_location(loc_raw),
                            "work_mode": "unknown",
                            "employment_type": None,
                            "date_published": date_iso,
                            "expires_at": None,
                            "posting_language": "es",
                            "posting_language_origin": "source",
                            "description_raw": _limpiar_texto(item.get("snippet", ""), conservar_saltos=True),
                            "requirements_raw": "",
                            "salary_raw": _limpiar_texto(item.get("salary", "")),
                        },
                        "raw_payload": item,
                    })

                next_btn = await page.query_selector("a[data-testid='pagination-page-next'], a[aria-label='Next Page'], a.next")
                if next_btn and await next_btn.is_enabled():
                    await next_btn.click()
                    await page.wait_for_timeout(3000)
                else:
                    break

            all_cookies = await ctx.cookies()
            cookies = {
                c["name"]: c["value"]
                for c in all_cookies
                if "indeed.com" in (c.get("domain") or "")
            }

            # Enriquecer descripciones via rpc/jobdescs (usa la sesión del navegador)
            await self._enrich_descriptions(page, jobs)

        finally:
            await page.close()

        return jobs, cookies

    