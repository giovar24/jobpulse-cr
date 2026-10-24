"""Adaptador LinkedIn (linkedin.com) para v1.5 — versión probada del proyecto principal.

Usa endpoint público 'jobs-guest' para descripción sin sesión + HTTP con cookies.
REGLA DE ORO: SESIÓN PRIMERO — se reusa la sesión de tu Brave (login manual una vez
con `hacer_login.py`). El SSO Google queda como RECURSO opt-in solo si la sesión
se cae (LinkedIn es agresivo con logins automatizados).
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

logger = logging.getLogger("jobpulse.linkedin")

DATE_RE = re.compile(
    r"(hace\s+\d+\s*(min|minuto|hora|día|dias|semana|mes)|"
    r"\d+\s*(min|minute|hour|day|week|month)\s*ago)",
    re.IGNORECASE,
)

_MAX_PAGES = 10


class LinkedInFetchError(RuntimeError):
    """Error obteniendo vacantes de LinkedIn."""


class LinkedInOffers(list[dict[str, Any]]):
    def __init__(self, offers: list[dict[str, Any]], *, jobs_skipped: int = 0) -> None:
        super().__init__(offers)
        self.jobs_skipped = jobs_skipped


def _extract_date(html: str) -> str:
    m = re.search(r"(?is)posted-time-ago[^>]*>([^<]+)<", html)
    if m and m.group(1).strip():
        return m.group(1).strip()
    m = re.search(r"(?i)(\d+\s+(?:minute|hour|day|week|month)s?\s+ago)", html)
    if m:
        return m.group(1)
    m = re.search(r"(?i)(hace\s+\d+\s+(?:minuto|hora|d[ií]a|semana|mes)\w*)", html)
    if m:
        return m.group(1)
    return ""


def _numeric_id(job_id: str | None, link: str | None) -> str:
    if job_id:
        m = re.search(r"(\d{6,})", job_id)
        if m:
            return m.group(1)
    if link:
        m = re.search(r"/jobs/view/(\d+)", link)
        if m:
            return m.group(1)
    return ""


def _parse_date_relative(txt: str) -> str | None:
    """Convierte '1 day ago', 'hace 2 días' a ISO YYYY-MM-DD (aprox)."""
    if not txt:
        return None
    from datetime import datetime, timedelta, timezone
    txt = txt.lower().strip()
    now = datetime.now(timezone.utc)
    # Patrones: "X days ago", "hace X días", "X hours ago", etc.
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


def _split_requirements(text: str) -> tuple[str, str]:
    """Separa descripción de requisitos si vienen juntos."""
    if not text:
        return "", ""
    # Buscar separadores comunes
    for sep in ["\n\nRequisitos", "\n\nRequirements", "\nRequisitos:", "\nRequirements:"]:
        if sep in text:
            parts = text.split(sep, 1)
            return parts[0].strip(), sep.replace("\n", "").strip() + " " + parts[1].strip()
    return text, ""


class LinkedInFetcher:
    """Scraper LinkedIn con sesión Brave + enriquecimiento HTTP."""

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
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
        }

    def _muro_login(self, page: Page) -> bool:
        try:
            url = (page.url or "").lower()
        except Exception:
            return False
        return "authwall" in url or "/login" in url

    def _registrar_requiere_login(self, url: str):
        try:
            # En v1.5 usamos Repository para registrar
            from jobpulse_v15.repository import Repository
            # Se hace desde el caller pasando repository
            pass
        except Exception as e:
            logger.debug("registrar_requerimiento LinkedIn fallo: %s", e)

    async def _fetch_details_guest(self, client: httpx.AsyncClient, job_id: str) -> Dict[str, Any]:
        """Endpoint público 'jobs-guest' suele devolver descripción sin sesión."""
        url = f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"
        try:
            response = await client.get(url)
            if response.status_code != 200:
                return {}
            html = response.text
            patterns = [
                r'<div class="[^"]*show-more-less-html__markup[^"]*"[^>]*>(.*?)</div>',
                r'<div class="[^"]*description__text[^"]*"[^>]*>(.*?)</div>',
                r'<section class="[^"]*description[^"]*"[^>]*>(.*?)</section>',
            ]
            for pattern in patterns:
                match = re.search(pattern, html, re.DOTALL | re.IGNORECASE)
                if match:
                    desc = self._html_to_text(match.group(1))
                    if desc:
                        return {
                            "description": desc,
                            "salary": "",
                            "requirements": "",
                            "date": _extract_date(html),
                        }
            return {}
        except Exception as e:
            logger.warning(f"LinkedIn guest detail failed for {job_id}: {e}")
            return {}

    async def _fetch_details_http(self, client: httpx.AsyncClient, url: str) -> Dict[str, Any]:
        """Fetch detalles usando HTTP con cookies reales."""
        try:
            response = await client.get(url)
            if response.status_code != 200:
                logger.warning(f"HTTP {response.status_code} for {url}")
                return {}

            html = response.text
            details = {"description": "", "salary": "", "requirements": "", "date": _extract_date(html)}

            desc_patterns = [
                r'<div class="[^"]*jobs-description-content__text[^"]*".*?>(.*?)</div>',
                r'<div class="[^"]*description__text[^"]*".*?>(.*?)</div>',
                r'<div class="[^"]*show-more-less-html__markup[^"]*".*?>(.*?)</div>',
                r'<section class="[^"]*description[^"]*".*?>(.*?)</section>',
                r'<div id="job-details".*?>(.*?)</div>',
            ]

            for pattern in desc_patterns:
                match = re.search(pattern, html, re.DOTALL | re.IGNORECASE)
                if match:
                    details["description"] = self._html_to_text(match.group(1))
                    break

            if not details["description"]:
                text_content = re.sub(r'<[^>]+>', ' ', html)
                desc_match = re.search(
                    r'(?i)(About the job|Descripci[oó]n del puesto)(.*?)(?=Similar jobs|Recommended|Apply now|$)',
                    text_content,
                    re.DOTALL,
                )
                if desc_match:
                    details["description"] = self._html_to_text(desc_match.group(2))

            if details["description"]:
                salary_match = re.search(
                    r'(?i)(salario|salary|remuneraci[oó]n|pago):\s*([^\n\.]+)', details["description"]
                )
                if salary_match:
                    details["salary"] = salary_match.group(2).strip()

                req_match = re.search(
                    r'(?i)(requirements|requisitos|calificaciones)(.*?)(?=\n\n|$)',
                    details["description"],
                    re.DOTALL,
                )
                if req_match:
                    details["requirements"] = req_match.group(2).strip()

            return details
        except Exception as e:
            logger.warning(f"HTTP detail fetch failed for {url}: {e}")
            return {}

    @staticmethod
    def _html_to_text(markup: str) -> str:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(markup, "html.parser")
        for tag in soup(["script", "style"]):
            tag.decompose()
        text = soup.get_text(" ", strip=True)
        return " ".join(text.split())

    async def fetch(
        self,
        site: dict[str, Any],
        *,
        pages: int = 2,
        search_text: str | None = None,
        skip_source_job_ids: set[str] | None = None,
        port: int = 9222,
    ) -> LinkedInOffers:
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
            basic_jobs, cookies = await self._run_sync_scraping(
                ctx, search_term, location, pages, skipped_ids
            )
        if not basic_jobs:
            return LinkedInOffers([], jobs_skipped=0)

        # Enriquecer con HTTP (guest + cookies)
        enriched_jobs = []
        async with httpx.AsyncClient(
            headers=self.headers, cookies=cookies, timeout=15.0, follow_redirects=True
        ) as client:
            for job in basic_jobs:
                if job.get("source_url"):
                    jid = _numeric_id(job.get("source_job_id"), job.get("source_url"))
                    details = await self._fetch_details_guest(client, jid) if jid else {}
                    if not details.get("description"):
                        details = await self._fetch_details_http(client, job.get("source_url")) or details

                    job["job"]["description_raw"] = details.get("description") or job["job"]["description_raw"]
                    job["job"]["requirements_raw"] = details.get("requirements") or job["job"]["requirements_raw"]
                    job["job"]["salary_raw"] = details.get("salary") or job["job"]["salary_raw"]
                    if not job["job"]["date_published"] and details.get("date"):
                        job["job"]["date_published"] = _parse_date_relative(details["date"])

                # Separar descripción/requisitos si vienen juntos
                if job["job"]["description_raw"] and not job["job"]["requirements_raw"]:
                    job["job"]["description_raw"], job["job"]["requirements_raw"] = _split_requirements(
                        job["job"]["description_raw"]
                    )
                enriched_jobs.append(job)

        logger.info("LinkedIn: %d ofertas enriquecidas", len(enriched_jobs))
        return LinkedInOffers(enriched_jobs, jobs_skipped=0)

    async def _run_sync_scraping(
        self, ctx: BrowserContext, search_term: str, location: str, pages: int,
        skipped_ids: set[str] | None = None,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, str]]:
        jobs: List[Dict[str, Any]] = []
        cookies: Dict[str, str] = {}
        page = await ctx.new_page()
        try:
            await page.set_extra_http_headers({"Accept-Language": "es-ES,es;q=0.9,en;q=0.8"})

            url = (
                f"https://www.linkedin.com/jobs/search/"
                f"?keywords={quote_plus(search_term)}&location={quote_plus(location)}"
            )
            await page.goto(url, wait_until="domcontentloaded")
            await page.wait_for_timeout(3000)

            # Authwall: SESIÓN PRIMERO — reusa sesión de Brave (login manual con hacer_login.py)
            if self._muro_login(page):
                logger.warning("LinkedIn: authwall detectado. Se necesita sesión en Brave (ejecuta hacer_login.py).")
                self._registrar_requiere_login(page.url)
                # NO auto-login aquí; LinkedIn es agresivo con CAPTCHA/2FA
                # El caller decidirá si intentar auto-login como recurso opt-in

            seen: set = set()
            for _ in range(pages):
                # Scroll para cargar más tarjetas
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await page.wait_for_timeout(2000)

                extracted_data = await page.evaluate("""
                () => {
                    const jobs = [];
                    const seen = new Set();
                    let els = document.querySelectorAll('[data-occludable-job-id], li[data-job-id], div[data-job-id], .job-card-container');
                    els.forEach(el => {
                        let link = el.querySelector('a[href*="/jobs/view/"]');
                        if (!link) link = el.querySelector('a[href*="/jobs/"]');

                        const jobId = el.getAttribute('data-occludable-job-id') || el.getAttribute('data-job-id') || '';
                        const lines = el.innerText.split('\\n').map(l => l.trim()).filter(Boolean);

                        let titulo = '';
                        if (link) {
                            const sp = link.querySelector('span, h3');
                            titulo = (sp ? sp.innerText : link.innerText || '').trim();
                        }
                        if (!titulo) titulo = lines[0] || '';

                        let empresa = '';
                        const subtitle = el.querySelector('.artdeco-entity-lockup__subtitle, .job-card-container__company-name, .job-card-list__company-name');
                        if (subtitle) {
                            empresa = subtitle.innerText.trim();
                        } else {
                            for (const l of lines) {
                                if (l === titulo) continue;
                                if (/^(promoted|destacado|patrocinado|easy\\s*apply|postulaci)/i.test(l)) continue;
                                if (/^\\d+/i.test(l) && /ago|hace|min|hour|day|week|month/i.test(l)) continue;
                                if (/^(presencial|remoto|h[ií]brido|hybrid|remote|on\\s*site)$/i.test(l)) continue;
                                empresa = l;
                                break;
                            }
                        }

                        let ubicacionOferta = '';
                        const caption = el.querySelector('.artdeco-entity-lockup__caption, .job-card-container__metadata, .job-card-list__location');
                        if (caption) {
                            ubicacionOferta = caption.innerText.trim();
                        } else {
                            for (const l of lines) {
                                if (/\\b(Costa Rica|Heredia|San Jos[eé]|Alajuela|Lim[oó]n|Cartago|Puntarenas|Guanacaste|Remote|Remoto|Hybrid|H[ií]brido)\\b/i.test(l) && l !== titulo && l !== empresa) {
                                    ubicacionOferta = l;
                                    break;
                                }
                            }
                        }

                        let fecha = '';
                        const timeEl = el.querySelector('time');
                        if (timeEl) fecha = (timeEl.innerText || timeEl.getAttribute('datetime') || '').trim();
                        if (!fecha) {
                            for (const l of lines) {
                                if (/^(hace\\s+\\d+|\\d+\\s*(min|minute|hour|day|week|month)s?\\s*ago)/i.test(l)) {
                                    fecha = l;
                                    break;
                                }
                            }
                        }

                        const linkHref = link ? link.href.split('?')[0] : '';
                        const id = jobId || linkHref;
                        if (!id || seen.has(id)) return;
                        if (!titulo || !linkHref) return;
                        seen.add(id);

                        jobs.push({
                            job_id: jobId || '',
                            titulo,
                            empresa,
                            ubicacion_oferta: ubicacionOferta,
                            fecha_publicacion: fecha,
                            link: linkHref,
                        });
                    });
                    return jobs;
                }
                """)

                for item in extracted_data:
                    job_id = item["job_id"] or item["link"]
                    if job_id in seen:
                        continue
                    if job_id in skipped_ids:
                        continue
                    seen.add(job_id)

                    date_pub = _parse_date_relative(item["fecha_publicacion"])

                    location_raw = item["ubicacion_oferta"]
                    jobs.append({
                        "source_job_id": job_id,
                        "source_url": item["link"],
                        "source_updated_at": date_pub,
                        "job": {
                            "job_id": f"linkedin-{job_id}",
                            "title": item["titulo"],
                            "company": item["empresa"],
                            "location_raw": location_raw,
                            "country": "CR",
                            **normalize_cr_location(location_raw),
                            "work_mode": self._detect_work_mode(location_raw),
                            "employment_type": None,
                            "date_published": date_pub,
                            "expires_at": None,
                            "posting_language": "undetermined",
                            "posting_language_origin": "unknown",
                            "description_raw": "",
                            "requirements_raw": "",
                            "salary_raw": "",
                        },
                        "raw_payload": item,
                    })

                # Paginación
                try:
                    next_btn = await page.query_selector('button[aria-label*="next"], .artdeco-pagination__button--next')
                    if next_btn and await next_btn.is_enabled():
                        await next_btn.click()
                        await page.wait_for_timeout(4000)
                    else:
                        break
                except Exception:
                    break

            all_cookies = await ctx.cookies()
            cookies = {
                c["name"]: c["value"]
                for c in all_cookies
                if "linkedin.com" in (c.get("domain") or "")
            }
        finally:
            await page.close()

        return jobs, cookies

    @staticmethod
    def _detect_work_mode(location: str | None) -> str:
        if not location:
            return "unknown"
        loc = location.lower()
        if any(w in loc for w in ("remoto", "remote", "teletrabajo", "100% remoto")):
            return "remote"
        if any(w in loc for w in ("híbrido", "hibrido", "hybrid", "mixto", "semipresencial")):
            return "hybrid"
        if any(w in loc for w in ("presencial", "onsite", "oficina", "en sitio")):
            return "onsite"
        return "unknown"