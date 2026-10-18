import asyncio
import logging
from typing import List, Dict, Any
from urllib.parse import quote_plus, urljoin
from providers.base import BaseProvider, JobOffer
from core.dates import parse_date
from core.text import split_requirements

logger = logging.getLogger("jobpulse.ane")


class AneProvider(BaseProvider):
    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.login_url = "https://www.ane.cr/Cuenta/Login"
        self.jobs_url = "https://www.ane.cr/Puesto"
        self.user = config.get("username")
        self.password = config.get("password")

    async def fetch(self, search_term: str, location: str, pages: int, port: int) -> List[JobOffer]:
        return await asyncio.to_thread(self._run_sync_scraping, search_term, location, pages, port)

    def _run_sync_scraping(self, search_term: str, location: str, pages: int, port: int) -> List[JobOffer]:
        from playwright.sync_api import sync_playwright
        jobs: List[JobOffer] = []
        try:
            with sync_playwright() as p:
                browser = p.chromium.connect_over_cdp(f"http://localhost:{port}")
                context = browser.contexts[0]
                page = context.new_page()
                try:
                    page.goto(self.jobs_url, wait_until="domcontentloaded")
                    page.wait_for_timeout(1500)

                    # Detail links only point to /Puesto/Detalles when logged in
                    can_see_details = bool(page.query_selector(".job-listing-footer a[href*='Puesto']"))
                    if not can_see_details:
                        if not self.user or not self.password:
                            logger.warning("ANE: no hay sesión ni credenciales en .env. Omitiendo ANE.")
                            return []
                        logger.info("ANE: attempt login...")
                        if not self._login(page):
                            return []

                    search_url = f"{self.jobs_url}?Empleos={quote_plus(search_term)}"
                    page.goto(search_url, wait_until="domcontentloaded")
                    page.wait_for_timeout(2000)

                    raw = []
                    for _ in range(pages):
                        cards = page.query_selector_all(".job-listing")
                        logger.info(f"ANE: found {len(cards)} .job-listing cards")
                        if not cards:
                            break

                        for card in cards:
                            try:
                                title_elem = card.query_selector("h3.job-listing-title")
                                company_elem = card.query_selector("h4.job-listing-company")
                                date_elem = card.query_selector("small.job-listing-description")
                                link_elem = card.query_selector(".job-listing-footer a[href*='Puesto']")
                                loc_elems = card.query_selector_all(".job-listing-description ul li")

                                if not title_elem or not link_elem:
                                    continue

                                title = self.clean_text(title_elem.inner_text())
                                link = link_elem.get_attribute("href") or ""
                                if link.startswith("/"):
                                    link = urljoin("https://www.ane.cr", link)

                                if not title or not link:
                                    continue

                                raw.append({
                                    "job_id": link.split("Puesto=")[-1] or link,
                                    "title": title,
                                    "company": self.clean_text(company_elem.inner_text()) if company_elem else "ANE",
                                    "date_raw": self.clean_text(date_elem.inner_text()) if date_elem else "",
                                    "location": "; ".join(self.clean_text(li.inner_text()) for li in loc_elems if li.inner_text().strip()),
                                    "link": link,
                                })
                            except Exception as e:
                                logger.warning(f"Error parsing ANE card: {e}")

                        next_btn = page.query_selector("a.pagination__next, .pagination a[aria-label*='iguiente'], ul.pagination li.active + li a")
                        if next_btn:
                            next_btn.click()
                            page.wait_for_timeout(2000)
                        else:
                            break

                    # Segunda pasada: el detalle (solo visible con sesión) trae la descripción y los requisitos.
                    for r in raw:
                        desc, req = self._fetch_detail(page, r["link"])
                        date_raw = r["date_raw"].replace("Publicado el ", "").strip()
                        jobs.append(JobOffer(
                            source="ane",
                            search_term=search_term,
                            job_id=r["job_id"],
                            title=r["title"],
                            company=r["company"],
                            location=r["location"] or location,
                            link=r["link"],
                            date_published=parse_date(date_raw),
                            description=desc,
                            requirements=req,
                            salary=""
                        ))
                finally:
                    page.close()
        except Exception as e:
            logger.error(f"ANE scraping failed: {e}")

        return jobs

    def _fetch_detail(self, page, url: str):
        """Navega al detalle (la sesión ya está activa) y extrae descripción y requisitos."""
        try:
            page.goto(url, wait_until="domcontentloaded")
            page.wait_for_timeout(700)
            text = ""
            for sel in (".dashboard-content-inner", ".dashboard-box", ".content.with-padding",
                        "#detalle-puesto", ".detalle-puesto", ".job-detail", "article"):
                el = page.query_selector(sel)
                if not el:
                    continue
                cand = "\n".join(l.strip() for l in el.inner_text().splitlines() if l.strip())
                if len(cand) > 80 and cand.lower() != url.lower():
                    text = cand
                    break
            if not text:
                body = page.query_selector("body")
                text = "\n".join(l.strip() for l in body.inner_text().splitlines() if l.strip()) if body else ""
            return split_requirements(text)
        except Exception as e:
            logger.warning(f"ANE detail failed for {url}: {e}")
            return "", ""

    def _login(self, page) -> bool:
        if not self.user or not self.password:
            logger.error("ANE credentials not provided. Skipping ANE.")
            return False

        try:
            page.goto(self.login_url, wait_until="domcontentloaded")
            page.wait_for_timeout(1000)
            page.fill("#NombreUsuarioLogin", self.user)
            page.fill("#ContrasennaLogin", self.password)
            page.click("button[type='submit']")
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(1500)

            if "Login" in page.title() or page.query_selector("#NombreUsuarioLogin"):
                logger.error("ANE: login failed (still on login page).")
                return False

            logger.info("ANE: login successful.")
            return True
        except Exception as e:
            logger.error(f"ANE login failed: {e}")
            return False
