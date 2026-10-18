import httpx
from typing import List, Dict, Any
from bs4 import BeautifulSoup
from providers.base import BaseProvider, JobOffer


def _strip_html(text: str) -> str:
    if not text:
        return ""
    return BeautifulSoup(text, "html.parser").get_text(" ", strip=True)


class ProcomerProvider(BaseProvider):
    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.api_url = "https://talento.procomer.com/api/candidate/jobs-available"

    async def fetch(self, search_term: str, location: str, pages: int) -> List[JobOffer]:
        jobs = []
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            for page in range(1, pages + 1):
                params = {"search": search_term, "page": page, "per_page": 16}
                response = await client.get(self.api_url, params=params)
                # La API responde 202 (Accepted) con JSON valido
                if not (200 <= response.status_code < 300):
                    break

                try:
                    data = response.json()
                except ValueError:
                    break

                items = data.get("data", [])
                if not items:
                    break

                for item in items:
                    title = self.clean_text(item.get("name", ""))
                    if not title:
                        continue

                    org = item.get("organization") or {}
                    company = (
                        self.clean_text(item.get("anonymous_name", ""))
                        if item.get("external_visibility") == "anonymous"
                        else ""
                    ) or self.clean_text(org.get("name", "")) or "PROCOMER"

                    jobs.append(JobOffer(
                        source="procomer",
                        search_term=search_term,
                        job_id=str(item.get("id")),
                        title=title,
                        company=company,
                        location=self.clean_text(item.get("provincia", "")),
                        link=f"https://talento.procomer.com/interview/{item.get('id')}",
                        date_published=item.get("publish_date"),
                        description=_strip_html(item.get("description", "")),
                        requirements=_strip_html(item.get("requirements", "")),
                        salary=item.get("salary") or "",
                        raw_data=item
                    ))
        return jobs
