import asyncio
import logging
import re
import httpx
from typing import List, Dict, Any, Tuple
from urllib.parse import quote_plus
from providers.base import BaseProvider, JobOffer
from core.dates import parse_date
from core.text import split_requirements, html_to_text

logger = logging.getLogger("jobpulse.linkedin")

DATE_RE = re.compile(
    r"(hace\s+\d+\s*(min|minuto|hora|día|dias|semana|mes)|"
    r"\d+\s*(min|minute|hour|day|week|month)\s*ago)",
    re.IGNORECASE,
)


class LinkedInProvider(BaseProvider):
    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
        }

    @staticmethod
    def _extract_date(html: str) -> str:
        """Extrae la fecha relativa de publicación (p. ej. '1 day ago') del HTML de LinkedIn."""
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

    @staticmethod
    def _numeric_id(job: JobOffer) -> str:
        m = re.search(r"/jobs/view/(\d+)", job.link or "")
        if m:
            return m.group(1)
        m = re.search(r"(\d{6,})", job.job_id or "")
        return m.group(1) if m else ""

    # ------------------------------------------------------------------
    # Sesión: para LinkedIn lo PRIMERO es la SESIÓN del navegador (cookies
    # de tu Brave, logueate una vez con `python hacer_login.py`). El login
    # automatico (SSO Google / user+clave) queda como ULTIMO RECURSO opt-in:
    # LinkedIn es agresivo con logins automatizados (verificacion/CAPTCHA),
    # asi que no se usa en cada corrida sino solo si hay authwall.
    # ------------------------------------------------------------------
    def _muro_login(self, page) -> bool:
        """True si LinkedIn redirigio al authwall / login."""
        try:
            url = (page.url or "").lower()
        except Exception:
            return False
        return "authwall" in url or "/login" in url

    def _registrar_requiere_login(self, url: str):
        """Alimenta LA LISTA de sitios que piden sesión."""
        try:
            from core.credenciales import registrar_requerimiento
            registrar_requerimiento(
                "LinkedIn", url,
                "authwall: sin sesión LinkedIn corta la busqueda. Lo mejor "
                "es loguearte A MANO una vez en tu Brave (hacer_login.py) y "
                "reusar la sesión; como recurso, pone tu Gmail en Credenciales "
                "(--set LinkedIn --sso google) y activa --auto on.",
                grupo="boards", obligatorio=False,
                url_check="https://www.linkedin.com/jobs/search/")
        except Exception as e:
            logger.debug("registrar_requerimiento LinkedIn fallo: %s", e)

    def _auto_login(self, page) -> bool:
        """RECURSO opt-in: solo si el usuario guardo credencial (SSO Google
        o user+clave) y activo auto_login. Si falla (CAPTCHA/2FA de
        LinkedIn), la corrida sigue con lo que ya se extrajo."""
        try:
            from providers.login.auto_login import (hay_login_configurable,
                                                    intentar_login)
            if not hay_login_configurable("LinkedIn"):
                return False
            logger.info("LinkedIn: authwall -> intento auto-login como "
                        "ultimo recurso (lo normal es tu sesion de Brave)...")
            res = intentar_login(page, "LinkedIn", {
                "login_url": "https://www.linkedin.com/login",
                "sso": "google",
                "sso_iframe": 'iframe[src*="accounts.google.com"]',
                "user_sel": "#username",
                "pass_sel": "#password",
                "submit_sel": "button[type=submit]",
                "espera": 6000,
            })
            if not res["ok"]:
                logger.warning("LinkedIn: auto-login fallo (%s); se registra "
                               "pendiente y se sigue con los demas portales.",
                               res["motivo"])
            return bool(res["ok"])
        except Exception as e:
            logger.warning("LinkedIn auto-login fallo: %s", e)
            return False

    async def _fetch_details_guest(self, client: httpx.AsyncClient, job_id: str) -> Dict[str, Any]:
        """El endpoint público 'jobs-guest' suele devolver la descripción sin necesidad de sesión."""
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
                    desc = html_to_text(match.group(1))
                    if desc:
                        out = {
                            "description": desc,
                            "salary": "",
                            "requirements": "",
                            "date": self._extract_date(html),
                        }
                        sal = re.search(r"(?i)(salario|salary|remuneraci[oó]n|pago):\s*([^\n\.]+)", desc)
                        if sal:
                            out["salary"] = sal.group(2).strip()
                        return out
            return {}
        except Exception as e:
            logger.warning(f"LinkedIn guest detail failed for {job_id}: {e}")
            return {}

    async def _fetch_details_http(self, client: httpx.AsyncClient, url: str) -> Dict[str, Any]:
        """Fetch job details using HTTP request with real cookies to avoid blocks."""
        try:
            response = await client.get(url)
            if response.status_code != 200:
                logger.warning(f"HTTP {response.status_code} for {url}")
                return {}

            html = response.text
            details = {"description": "", "salary": "", "requirements": "", "date": self._extract_date(html)}

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
                    details["description"] = html_to_text(match.group(1))
                    break

            if not details["description"]:
                text_content = re.sub(r'<[^>]+>', ' ', html)
                desc_match = re.search(
                    r'(?i)(About the job|Descripción del puesto)(.*?)(?=Similar jobs|Recommended|Apply now|$)',
                    text_content,
                    re.DOTALL,
                )
                if desc_match:
                    details["description"] = html_to_text(desc_match.group(2))

            if details["description"]:
                salary_match = re.search(
                    r'(?i)(salario|salary|remuneración|pago):\s*([^\n\.]+)', details["description"]
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

    async def fetch(self, search_term: str, location: str, pages: int, port: int) -> List[JobOffer]:
        basic_jobs, cookies = await asyncio.to_thread(
            self._run_sync_scraping, search_term, location, pages, port
        )

        enriched_jobs = []
        async with httpx.AsyncClient(
            headers=self.headers, cookies=cookies, timeout=15.0, follow_redirects=True
        ) as client:
            for job in basic_jobs:
                if job.link:
                    jid = self._numeric_id(job)
                    details = await self._fetch_details_guest(client, jid) if jid else {}
                    if not details.get("description"):
                        details = await self._fetch_details_http(client, job.link) or details
                    job.description = details.get("description") or job.description
                    job.requirements = details.get("requirements") or job.requirements
                    job.salary = details.get("salary") or job.salary
                    if not job.date_published and details.get("date"):
                        job.date_published = parse_date(details["date"])
                if job.description and not job.requirements:
                    job.description, job.requirements = split_requirements(job.description)
                enriched_jobs.append(job)

        return enriched_jobs

    def _run_sync_scraping(self, search_term: str, location: str, pages: int, port: int) -> Tuple[List[JobOffer], Dict[str, str]]:
        from playwright.sync_api import sync_playwright
        jobs: List[JobOffer] = []
        cookies: Dict[str, str] = {}
        try:
            with sync_playwright() as p:
                browser = p.chromium.connect_over_cdp(f"http://localhost:{port}")
                context = browser.contexts[0]
                page = context.new_page()
                try:
                    url = (
                        f"https://www.linkedin.com/jobs/search/"
                        f"?keywords={quote_plus(search_term)}&location={quote_plus(location)}"
                    )
                    page.goto(url, wait_until="domcontentloaded")
                    page.wait_for_timeout(3000)

                    # Authwall: sin sesion LinkedIn corta los resultados. Lo
                    # PRIMERO es reusar la sesion de tu Brave (login manual una
                    # vez con hacer_login.py); el auto-login (SSO Google) es
                    # SOLO un recurso opt-in para cuando la sesion se cae.
                    if self._muro_login(page):
                        self._registrar_requiere_login(page.url)
                        if self._auto_login(page):
                            page.goto(url, wait_until="domcontentloaded")
                            page.wait_for_timeout(3000)

                    for _ in range(pages):
                        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                        page.wait_for_timeout(2000)

                        extracted_data = page.evaluate("""
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
                                        if (/^(hace\s+\d+|\d+\s*(min|minute|hour|day|week|month)s?\s*ago)/i.test(l)) {
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
                            jobs.append(JobOffer(
                                source="linkedin",
                                search_term=search_term,
                                job_id=item["job_id"] or item["link"],
                                title=item["titulo"],
                                company=item["empresa"],
                                location=item["ubicacion_oferta"],
                                link=item["link"],
                                date_published=parse_date(item["fecha_publicacion"]),
                                description="",
                                requirements="",
                                salary="",
                                raw_data=item
                            ))

                        try:
                            next_btn = page.query_selector('button[aria-label*="next"], .artdeco-pagination__button--next')
                            if next_btn and next_btn.is_enabled():
                                page.evaluate("el => el.click()", next_btn)
                                page.wait_for_timeout(4000)
                            else:
                                break
                        except Exception:
                            break

                    all_cookies = context.cookies()
                    cookies = {
                        c["name"]: c["value"]
                        for c in all_cookies
                        if "linkedin.com" in (c.get("domain") or "")
                    }
                finally:
                    page.close()
        except Exception as e:
            logger.error(f"LinkedIn scraping failed: {e}")

        return jobs, cookies
