"""Greenhouse (boards-api): proveedor #9.

Los tableros Greenhouse exponen una API board publica documentada:

    GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true
    -> {"meta": {"total": N},
        "jobs": [{"id", "title", "absolute_url", "location": {"name"},
                  "first_published" ISO, "updated_at", "content" (HTML escapado),
                  "offices": [{"name"}], "departments", "requisition_id", ...}]}

- TODO el catalago en 1 GET por tablero (con content=true trae las
  descripciones inline; sin content solo titulos).
- Lista de tokens: la cuna es el barrido multi-ATS del directorio
  (`data/empresas/otros_ats_cr.json`, filas platform="greenhouse" con vacantes
  CR ya filtradas), con fallback constante de las confirmadas (Smartsheet 7 CR,
  Zuora 2, Fivetran...).
- Trampa: `content` viene DOUBLE-escapado (JSON + entidades HTML: &lt;p&gt;) ->
  html.unescape antes de BS4.
- Fecha de publicacion: `first_published` (no `updated_at`, que es last-edit).
"""
import asyncio
import html as _html
import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List

import httpx
from bs4 import BeautifulSoup

from providers.base import BaseProvider, JobOffer

logger = logging.getLogger("jobpulse.greenhouse")

API = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"

# (empresa_o_token, token); fallback si falta data/empresas/otros_ats_cr.json
TOKEN_FALLBACK = [
    ("Smartsheet", "smartsheet"),
    ("Zuora", "zuora"),
    ("Fivetran", "fivetran"),
    ("Nayya", "nayya"),
    ("Storyblok", "storyblok"),
    ("Torq", "torq"),
]

# tokens sin nombre legible en el directorio -> nombre real (revelado en la
# propia descripcion de sus vacantes)
COMPANY_ALIAS = {
    "12jlkfsk": "Orion Global Solutions",
    "thinkacademyus": "Think Academy US",
}

_RE_CR = r"\bcosta\s*rica\b"


class GreenhouseProvider(BaseProvider):
    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self._tokens = self._cargar_tokens()

    # ------------------------------------------------------------ tokens
    def _cargar_tokens(self) -> List[tuple]:
        """Del barrido multi-ATS: filas greenhouse con CR confirmado."""
        path = Path(__file__).resolve().parent.parent.parent / "data" / "empresas" / "otros_ats_cr.json"
        out = []
        if path.exists():
            try:
                for row in json.loads(path.read_text(encoding="utf-8")):
                    if row.get("platform") != "greenhouse":
                        continue
                    token = (row.get("token") or "").strip()
                    if token:
                        name = (row.get("company") or token).replace("_", " ").title()
                        out.append((name, token))
            except Exception as e:
                logger.warning("otros_ats_cr.json ilegible (%s): uso fallback", e)
        if not out:
            out = list(TOKEN_FALLBACK)
        out = [(COMPANY_ALIAS.get(tok, nombre), tok) for nombre, tok in out]
        return out

    # ------------------------------------------------------------ fetch
    async def fetch(self, search_term: str, location: str,
                    pages: int) -> List[JobOffer]:
        return await asyncio.to_thread(self._sweep, max(1, int(pages or 1)))

    # ------------------------------------------------------------ barrido
    def _sweep(self, pages: int) -> List[JobOffer]:
        offers: List[JobOffer] = []
        with httpx.Client(timeout=40.0, follow_redirects=True) as client:
            for nombre, token in self._tokens:
                offers.extend(self._tablero(client, nombre, token))
        logger.info("Greenhouse: %d vacantes CR totales (antes del gate).", len(offers))
        return offers

    # ------------------------------------------------------------ tablero
    def _tablero(self, client, nombre: str, token: str) -> List[JobOffer]:
        try:
            r = client.get(API.format(token=token))
        except Exception as e:
            logger.warning("Greenhouse %s fallo GET: %s", token, e)
            return []
        if not (200 <= r.status_code < 300):
            logger.warning("Greenhouse %s HTTP %s", token, r.status_code)
            return []
        try:
            data = r.json()
        except ValueError:
            logger.warning("Greenhouse %s no-JSON", token)
            return []
        vacas: List[JobOffer] = []
        for j in data.get("jobs") or []:
            o = self._oferta(nombre, token, j)
            if o:
                vacas.append(o)
        if vacas:
            logger.info("Greenhouse %s: %d vacantes CR", token, len(vacas))
        return vacas

    # ------------------------------------------------------------ oferta
    def _oferta(self, nombre: str, token: str, j: dict) -> JobOffer | None:
        import re
        titulo = (j.get("title") or "").strip()
        if not titulo:
            return None
        loc = ((j.get("location") or {}).get("name") or "").strip()
        offices = "; ".join(o.get("name") or "" for o in (j.get("offices") or []))
        blob = f"{titulo} {loc} {offices}"
        if not re.search(_RE_CR, blob, re.IGNORECASE):
            return None
        desc = ""
        content = j.get("content") or ""
        if content:
            try:
                soup = BeautifulSoup(_html.unescape(content), "html.parser")
                desc = soup.get_text(" ", strip=True)
            except Exception:
                desc = ""
        jid = j.get("id") or ""
        fecha = (j.get("first_published") or j.get("updated_at") or "")[:10] or None
        deps = "; ".join(d.get("name") or "" for d in (j.get("departments") or []))
        if deps:
            desc = f"{desc} [Departamento: {deps}]"
        return JobOffer(
            source="greenhouse",
            search_term="barrido-greenhouse",
            job_id=f"gh-{token}-{jid}" if jid else f"gh-{abs(hash(titulo + token))}",
            title=self.clean_text(titulo),
            company=self.clean_text(nombre if nombre != token.title() else token),
            location=self.clean_text(loc or offices or "Costa Rica"),
            link=j.get("absolute_url") or "",
            date_published=fecha,
            description=self.clean_text(desc),
            requirements="",
            salary="",
            raw_data={"requisition_id": j.get("requisition_id"),
                      "updated_at": j.get("updated_at")},
        )
