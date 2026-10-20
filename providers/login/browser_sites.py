# -*- coding: utf-8 -*-
"""providers.login.browser_sites — provider de sitios con sesion/Cloudflare.

`LoginSitesProvider`: recorre `data/empresas/login_sites.json`, cada entrada
es un sitio del employer que requiere navegador real:

    {
      "empresa": "Nestle",
      "urls": ["https://jobs.nestle.com/jobs"],
      "interstitial": true,              # espera desafio Cloudflare/SPA
      "interstitial_max": 45,            # segs max esperando el desafio
      "block_text": ["checking your browser", "un momento", "verifying"],
      "login_markers": ["iniciar sesion", "sign in"],  # deteccion de login-wall
      "card_sel": ".job-card",
      "title_sel": "h3",
      "company_sel": ".org-name",        # opcional (si falta usa empresa)
      "loc_sel": ".location",
      "link_sel": "a.joblink",           # o "href" = el <a> del card
      "date_sel": ".date"                # opcional
    }

Si el sitio carga pero no tiene sesion (login wall) o el desafio no se
resuelve, se registra el pendiente y se salta — la corrida sigue.
"""
import asyncio
import json
import logging
import re
from datetime import datetime
from hashlib import md5
from pathlib import Path
from typing import Any, Dict, List

from providers.base import BaseProvider, JobOffer

logger = logging.getLogger("jobpulse.login_sites")

LOGIN_SITES_JSON = Path(__file__).resolve().parent.parent.parent / \
    "data" / "empresas" / "login_sites.json"
PENDING_LOGIN_JSON = Path(__file__).resolve().parent.parent.parent / \
    "data" / "empresas" / "pending_login.json"

_RE_CR = re.compile(r"costa\s*rica", re.IGNORECASE)
_BLOCK_DEFAULT = ["checking your browser", "un momento", "verifying you",
                  "un paso mas", "one more step",
                  "just a quick security check",
                  "verifying the security", "attention required"]
_LOGIN_DEFAULT = ["iniciar sesi", "sign in", "create an account",
                  "create a profile", "crea tu cuenta"]


class LoginSitesProvider(BaseProvider):
    source = "login_sites"

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self._sites = self._cargar()

    # ------------------------------------------------------------- config
    def _cargar(self) -> List[dict]:
        if not LOGIN_SITES_JSON.exists():
            logger.info("login_sites.json no existe: provider sin sitios.")
            return []
        try:
            data = json.loads(LOGIN_SITES_JSON.read_text(encoding="utf-8"))
            return [s for s in data if s.get("empresa") and s.get("urls")]
        except Exception as e:
            logger.error("login_sites.json ilegible: %s", e)
            return []

    # --------------------------------------------------------------- fetch
    async def fetch(self, search_term: str, location: str, pages: int,
                    port: int | None = None,
                    dias: int = 30) -> List[JobOffer]:
        return await asyncio.to_thread(self._sweep, port, dias)

    # -------------------------------------------------------------- sweep
    def _sweep(self, port: int | None, dias: int) -> List[JobOffer]:
        if not port:
            logger.warning("login_sites: sin puerto CDP (Brave off) — skip.")
            return []
        from playwright.sync_api import sync_playwright

        offers: List[JobOffer] = []
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(f"http://localhost:{port}")
            context = browser.contexts[0]   # contexto del PERFIL del usuario (cookies)
            page = context.new_page()
            for site in self._sites:
                try:
                    offers.extend(self._sitio(page, site))
                except Exception as e:
                    logger.error("login_sites %s fallo: %s",
                                 site.get("empresa"), e)
                finally:
                    page.close()
                    page = context.new_page()
        logger.info("login_sites: %d vacantes CR totales (antes del gate).",
                    len(offers))
        return offers

    # -------------------------------------------------------------- sitio
    def _sitio(self, page, site: dict) -> List[JobOffer]:
        empresa = site["empresa"]
        card_sel = site.get("card_sel") or ""

        for url in site["urls"]:
            host_tag = re.sub(r"[^a-zA-Z0-9]+", "",
                              (url.split("//")[-1].replace("www.", "")
                               .split(".")[0])).upper()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            except Exception as e:
                logger.warning("[LOGIN-SITES] %s goto fallo %s: %s",
                               empresa, url, e)
                continue
            page.wait_for_timeout(2500)

            if site.get("interstitial", False):
                if not self._esperar_desafio(page, site):
                    self._registrar_pendiente(empresa, url,
                                              "desafio Cloudflare no resuelto")
                    continue

            modo = (site.get("mode") or "dom").lower()

            def _extraer_todo():
                if modo == "page_fetch":
                    return self._fetch_en_pagina(page, site)
                return self._extraer(page, site)

            raw = _extraer_todo()
            if modo == "page_fetch":
                if raw is None:
                    self._registrar_pendiente(
                        empresa, url,
                        "API in-page fallo (CSRF/autorizacion del SPA)")
                    continue
                if not raw:
                    logger.info("login_sites %s: 0 posiciones (API ok, "
                                "sin resultados en el filtro)", empresa)
                    continue

            if not raw:
                # sin cards: distinguir login wall vs listado vacio
                titulo = (page.title() or "").lower()
                cuerpo = page.inner_text("body")[:1200].lower()
                c1 = any(m in titulo for m in (site.get("login_markers")
                                               or _LOGIN_DEFAULT))
                c2 = any(m in cuerpo for m in (site.get("login_markers")
                                               or _LOGIN_DEFAULT))
                if c1 or c2:
                    # pide sesion: primero el login automatico con la
                    # credencial guardada (si la hay y esta activo); si no
                    # funciona, fallback manual (pendiente) sin cortar la corrida.
                    if self._relogin(page, site, url):
                        raw = _extraer_todo()
                    if not raw:
                        self._registrar_pendiente(
                            empresa, url, "login-wall: sin sesion en el perfil")
                        continue
                elif card_sel:
                    self._registrar_pendiente(empresa, url,
                                              "sin cards (selector a revisar)")
                    continue
                else:
                    logger.info("login_sites %s: 0 vacantes", empresa)
                    continue

            vacas = []
            for r in raw:
                lugar = (r.get("location") or "").strip()
                t = (r.get("title") or "").strip()
                if not t:
                    continue
                if not _RE_CR.search(lugar) and not _RE_CR.search(t) \
                        and not _RE_CR.search(r.get("other", "")):
                    continue
                link = (r.get("link") or "").strip()
                if not link.startswith("http"):
                    link = url.split("//")[0] + "//" + \
                        url.split("//")[-1].split("/")[0] + link
                digest = md5((t + '|' + lugar)
                             .encode('utf-8')).hexdigest()[:9]
                vacas.append(JobOffer(
                    source=self.source,
                    search_term="barrido-login-sites",
                    job_id=f"login-{host_tag}-{digest}",
                    title=self.clean_text(t),
                    company=self.clean_text(site["empresa"]),
                    location=self.clean_text(lugar),
                    link=link,
                    date_published=self._fecha_iso(r.get("date")),
                    description=self.clean_text(r.get("desc") or ""),
                    requirements="",
                    salary="",
                ))
            if vacas:
                logger.info("login_sites %s: %d vacantes CR", empresa,
                            len(vacas))
            return vacas
        return []

    # ------------------------------------------------- login automatico
    def _relogin(self, page, site: dict, url: str) -> bool:
        """Login automatico OPT-IN: solo si hay credencial guardada para la
        empresa, auto_login activo en el registro y selectores en el JSON
        (bloque `login: {login_url, user_sel, pass_sel, submit_sel}`).
        Si falla (CAPTCHA/2FA/CSRF), devuelve False y el sitio queda como
        pendiente manual — la corrida no se corta."""
        from providers.login.auto_login import (_cfg_sitio,
                                                hay_login_configurable,
                                                intentar_login)
        empresa = site["empresa"]
        cfg = _cfg_sitio(site)
        if not cfg.get("login_url"):
            logger.info("login_sites %s: pide sesion y no tiene bloque "
                        "`login` en login_sites.json (agregalo para poder "
                        "poner tu password).", empresa)
            return False
        if not hay_login_configurable(empresa, site):
            return False
        logger.info("login_sites %s: pide sesion -> intento auto-login...",
                    empresa)
        res = intentar_login(page, empresa, cfg)
        if not res["ok"]:
            logger.warning("login_sites %s: auto-login fallo (%s); queda "
                           "pendiente de login manual.", empresa, res["motivo"])
            return False
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            page.wait_for_timeout(2500)
            if site.get("interstitial", False):
                self._esperar_desafio(page, site)
        except Exception as e:
            logger.warning("login_sites %s: vuelta al listado fallo: %s",
                           empresa, e)
        return True

    # ------------------------------------------------- desafio cloudflare
    def _esperar_desafio(self, page, site: dict) -> bool:
        """Espera que el interstitial desaparezca (o timeout). True=libre.

        Chequea TITULO y body (el CF de Nestle solo decia 'Un momento…'
        en el <title>), y a los ~4s intenta el click del checkbox Turnstile:
        en un Brave real (perfil del usuario) el desafio suele ceder solo.
        """
        patrones = site.get("block_text") or _BLOCK_DEFAULT
        max_s = int(site.get("interstitial_max", 45))
        intento_click = False
        for i in range(max_s):
            titulo = ""
            cuerpo = ""
            try:
                titulo = (page.title() or "").lower()
                cuerpo = page.inner_text("body")[:600].lower()
            except Exception:
                pass
            bloqueado = any(p in titulo for p in patrones) or \
                any(p in cuerpo for p in patrones)
            if not bloqueado:
                return True
            if i == 4 and not intento_click:
                intento_click = True
                self._click_turnstile(page)
            page.wait_for_timeout(1000)
        logger.warning("[LOGIN-SITES] %s: desafio NO cedio en %ds",
                       site["empresa"], max_s)
        return False

    @staticmethod
    def _click_turnstile(page):
        """Click al checkbox de challenges.cloudflare.com (best effort)."""
        try:
            for frame in page.frames:
                if "challenges.cloudflare.com" in (frame.url or ""):
                    try:
                        frame.click("input[type=checkbox]", timeout=1500)
                    except Exception:
                        try:
                            frame.click("body", timeout=1500)
                        except Exception:
                            pass
                    logger.info("login_sites: click en Turnstile intentado")
                    return
        except Exception as e:
            logger.debug("login_sites: turnstile click fallo: %s", e)

    # --------------------------------------------------------- page_fetch
    def _fetch_en_pagina(self, page, site: dict) -> List[dict] | None:
        """Ejecuta fetch(api_url) DENTRO de la pagina (mismo origen/cookies
        del SPA) y mapea la respuesta con dot-paths del JSON de config."""
        api = site.get("api_url") or ""
        if not api:
            return None
        try:
            data = page.evaluate(
                """async (u) => {
                     const r = await fetch(u,
                        {headers: {"Accept": "application/json"}});
                     if (!r.ok) return {__status: r.status};
                     return await r.json();
                   }""", api)
        except Exception as e:
            logger.warning("login_sites %s: fetch in-page fallo: %s",
                           site.get("empresa"), e)
            return None
        if not isinstance(data, dict) or "__status" in data:
            logger.warning("login_sites %s: API respondio %s",
                           site.get("empresa"),
                           (data or {}).get("__status"))
            return None
        lista = self._get_path(data, site.get("list_path") or "")
        if not isinstance(lista, list):
            return []

        def _as_str(v):
            if isinstance(v, list):
                return "; ".join(str(x) for x in v if x)
            return "" if v is None else str(v)

        f = {k: site.get(k) for k in ("f_title", "f_loc", "f_link",
                                      "f_date", "f_desc")}
        out = []
        for it in lista:
            if not isinstance(it, dict):
                continue
            out.append({
                "title": _as_str(self._get_path(it, f["f_title"]
                                                 or "name")),
                "location": _as_str(self._get_path(it, f["f_loc"]
                                                   or "locations")),
                "link": _as_str(self._get_path(it, f["f_link"]
                                               or "positionUrl")),
                "date": _as_str(self._get_path(it, f["f_date"]
                                               or "postedTs")),
                "desc": _as_str(self._get_path(it, f["f_desc"] or "")),
            })
        return out

    @staticmethod
    def _get_path(obj, path: str):
        """'location.name' -> obj['location']['name'] (None si falta)."""
        cur = obj
        for parte in (path or "").split("."):
            if isinstance(cur, dict):
                cur = cur.get(parte)
            else:
                return None
        return cur

    @staticmethod
    def _fecha_iso(v):
        """Normaliza a YYYY-MM-DD: epoch ms/seg o string ISO."""
        if v in (None, "",):
            return None
        s = str(v).strip()
        if s.isdigit():
            n = int(s)
            if n > 10**12:      # epoch ms
                n /= 1000.0
            elif n > 10**9:     # epoch s
                n = float(n)
            try:
                from datetime import datetime as _dt
                return _dt.fromtimestamp(n).date().isoformat()
            except (ValueError, OSError, OverflowError):
                return None
        return s[:10] if s[:4].isdigit() else None

    # ------------------------------------------------------------- cards
    def _extraer(self, page, site: dict) -> List[dict]:
        """Evaluacion generica guiada por los selectores del JSON."""
        card_sel = site.get("card_sel")
        if not card_sel:
            return []
        title_sel = site.get("title_sel", "h2, h3")
        company_sel = site.get("company_sel")
        loc_sel = site.get("loc_sel")
        link_sel = site.get("link_sel")
        date_sel = site.get("date_sel")
        js = """
        ([cardSel, titleSel, companySel, locSel, linkSel, dateSel]) => {
          const out = [];
          document.querySelectorAll(cardSel).forEach(el => {
            const t = el.querySelector(titleSel);
            if (!t) return;
            const a = linkSel ? el.querySelector(linkSel)
                              : el.querySelector("a[href]");
            const c = companySel ? el.querySelector(companySel) : null;
            const l = locSel ? el.querySelector(locSel) : null;
            const d = dateSel ? el.querySelector(dateSel) : null;
            out.push({
              title: t.innerText.trim(),
              link: a ? a.getAttribute("href") : "",
              location: l ? l.innerText.trim() : "",
              company: c ? c.innerText.trim() : "",
              date: d ? (d.getAttribute("datetime") || d.innerText.trim()) : "",
              desc: ""
            });
          });
          return out;
        }"""
        try:
            return page.evaluate(
                js, [card_sel, title_sel, company_sel, loc_sel, link_sel,
                     date_sel]) or []
        except Exception as e:
            logger.warning("login_sites %s: extract fallo: %s",
                           site.get("empresa"), e)
            return []

    # ---------------------------------------------------------- pendings
    def _registrar_pendiente(self, empresa: str, url: str, motivo: str):
        logger.warning("[LOGIN-SITES][PENDIENTE] %s -> %s (%s)", empresa,
                       url, motivo)
        # alimenta LA LISTA de sitios que piden password/sesion
        # (data/empresas/login_requerido.json -> pestaña Credenciales)
        if "login" in (motivo or "").lower():
            try:
                from core.credenciales import registrar_requerimiento
                registrar_requerimiento(empresa, url, motivo,
                                        grupo="directos", obligatorio=True)
            except Exception:
                pass
        data = []
        if PENDING_LOGIN_JSON.exists():
            try:
                data = json.loads(
                    PENDING_LOGIN_JSON.read_text(encoding="utf-8"))
                if not isinstance(data, list):
                    data = []
            except Exception:
                data = []
        data = [d for d in data if d.get("empresa") != empresa]
        data.append({"empresa": empresa, "url": url, "motivo": motivo,
                     "ts": datetime.now().isoformat(timespec="seconds")})
        try:
            PENDING_LOGIN_JSON.write_text(
                json.dumps(data, ensure_ascii=False, indent=1),
                encoding="utf-8")
        except Exception as e:
            logger.error("pending_login.json no escribible: %s", e)
