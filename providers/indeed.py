import asyncio
import logging
import re
import httpx
from typing import List, Dict, Any, Tuple
from urllib.parse import quote_plus
from providers.base import BaseProvider, JobOffer
from core.dates import parse_date
from core.text import split_requirements, html_to_text

logger = logging.getLogger("jobpulse.indeed")


def _epoch_to_date(ms: Any) -> str:
    """Convierte la marca de tiempo (epoch ms) de Indeed en fecha ISO YYYY-MM-DD."""
    from datetime import datetime, timezone

    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError):
        return ""


# Indeed embebe el listado completo (título, empresa, ubicación, fecha, snippet y salario)
# en window.mosaic; leerlo es mucho más robusto que raspar el DOM.
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

# Respaldo por DOM si el modelo JSON no está disponible.
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


class IndeedProvider(BaseProvider):
    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.base_url = "https://cr.indeed.com/jobs"
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
        }

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
                return {"description": html_to_text(match.group(1)), "requirements": "", "salary": ""}
            return {}
        except Exception as e:
            logger.warning(f"HTTP detail fetch failed for Indeed {url}: {e}")
            return {}

    def _enrich_descriptions(self, page, jobs: List[JobOffer]) -> None:
        """Obtiene la descripción completa vía /rpc/jobdescs usando la sesión del navegador
        (equivalente al endpoint guest de LinkedIn)."""
        targets = [j for j in jobs if re.fullmatch(r"[0-9a-f]{16}", j.job_id or "")]
        if not targets:
            return

        jks = [j.job_id for j in targets]
        got: Dict[str, str] = {}
        for i in range(0, len(jks), 20):
            batch = jks[i:i + 20]
            try:
                res = page.evaluate(
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
            raw = got.get(job.job_id)
            if raw:
                job.description = html_to_text(raw)

        filled = sum(1 for j in targets if j.description)
        logger.info(f"Indeed: descripciones completas obtenidas para {filled}/{len(targets)} vacantes")

    async def fetch(self, search_term: str, location: str, pages: int, port: int) -> List[JobOffer]:
        basic_jobs, cookies = await asyncio.to_thread(
            self._run_sync_scraping, search_term, location, pages, port
        )

        enriched_jobs = []
        async with httpx.AsyncClient(
            headers=self.headers, cookies=cookies, timeout=15.0, follow_redirects=True
        ) as client:
            for job in basic_jobs:
                # El navegador ya rellenó la descripción; HTTP solo como último recurso.
                if job.link and not job.description:
                    details = await self._fetch_details_http(client, job.link)
                    job.description = details.get("description") or job.description
                    job.requirements = details.get("requirements") or job.requirements
                    job.salary = details.get("salary") or job.salary
                # Si la fuente no separa requisitos, se intenta separar de la descripción.
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
                    page.set_extra_http_headers({"Accept-Language": "es-ES,es;q=0.9,en;q=0.8"})

                    url = f"{self.base_url}?q={quote_plus(search_term)}&l={quote_plus(location)}"
                    page.goto(url, wait_until="domcontentloaded")
                    page.wait_for_timeout(3000)

                    seen: set = set()
                    for _ in range(pages):
                        extracted = page.evaluate(_JS_EXTRACT_JOBS) or []
                        if not extracted:
                            extracted = page.evaluate(_JS_EXTRACT_JOBS_FALLBACK) or []
                        if not extracted:
                            logger.warning(f"In Indeed page found 0 cards for '{search_term}' (title: {page.title()})")

                        for item in extracted:
                            jk = item.get("jk") or ""
                            if not jk:
                                m = re.search(r"jk=([0-9a-f]{16})", item.get("link", "") or "")
                                jk = m.group(1) if m else ""
                            if not jk or jk in seen:
                                continue
                            seen.add(jk)

                            date_iso = _epoch_to_date(item.get("pubDate")) or parse_date(item.get("relative", ""))

                            jobs.append(JobOffer(
                                source="indeed",
                                search_term=search_term,
                                job_id=jk,
                                title=self.clean_text(item.get("title", "")),
                                company=self.clean_text(item.get("company", "")) or "Unknown",
                                location=self.clean_text(item.get("location", "")) or location,
                                link=f"https://cr.indeed.com/viewjob?jk={jk}",
                                date_published=date_iso,
                                description=self.clean_text(item.get("snippet", "")),
                                requirements="",
                                salary=self.clean_text(item.get("salary", "")),
                            ))

                        next_btn = page.query_selector("a[data-testid='pagination-page-next'], a[aria-label='Next Page'], a.next")
                        if next_btn and next_btn.is_enabled():
                            next_btn.click()
                            page.wait_for_timeout(3000)
                        else:
                            break

                    cookies = {
                        c["name"]: c["value"]
                        for c in context.cookies()
                        if "indeed.com" in (c.get("domain") or "")
                    }

                    self._enrich_descriptions(page, jobs)
                finally:
                    page.close()
        except Exception as e:
            logger.error(f"Indeed scraping failed: {e}")

        return jobs, cookies
