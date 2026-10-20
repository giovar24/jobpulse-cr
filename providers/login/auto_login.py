# -*- coding: utf-8 -*-
"""providers.login.auto_login — login automatico OPT-IN con fallback manual.

El usuario pidio poder poner SU password en los sitios que lo exigen
obligatoriamente (ver `core/credenciales.py`). Reglas:

- Solo intenta el login si (a) hay credencial guardada para la empresa, y
  (b) el sitio/registro tiene `auto_login` activo. Si no, se comporta como
  antes (sesion manual via Brave / pestaña "Sesiones").
- El login se hace en el MISMO Brave y el MISMO perfil que ya usa el
  scraper por CDP: si el sitio tiene CAPTCHA/2FA/CSRF-token-no-circulado,
  el intento falla y se cae al fallback manual (queda en
  `pending_login.json` con el motivo) sin romper la corrida.
- Selectores por sitio: `{login_url, user_sel, pass_sel, submit_sel,
  ok_sel, ok_negativos}`. Sin `login_url` no hay login automatico.
"""
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger("jobpulse.auto_login")


def _cfg_sitio(site: dict) -> dict:
    """Bloque `login` de login_sites.json (o el propio site si es plano)."""
    cfg = site.get("login")
    return cfg if isinstance(cfg, dict) else {}


def hay_login_configurable(empresa: str, site: Optional[dict] = None) -> bool:
    """True si hay credencial + auto_login + selectores para intentarlo."""
    from core.credenciales import credencial, lista_sitios_login

    if credencial(empresa) is None:
        return False
    for s in lista_sitios_login():
        if s["empresa"].casefold() == (empresa or "").casefold():
            if not s.get("auto_login"):
                return False
            break
    if site is not None:
        return bool(_cfg_sitio(site).get("login_url"))
    return True


def intentar_login(page, empresa: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Intenta iniciar sesion en `page` con la credencial guardada.

    cfg: {login_url, user_sel, pass_sel, submit_sel, ok_sel, ok_negativos,
          espera}. Devuelve {ok, motivo, titulo}.
    """
    from core.credenciales import credencial

    cred = credencial(empresa)
    if not cred:
        return {"ok": False, "motivo": "sin credencial guardada", "titulo": ""}
    login_url = cfg.get("login_url")
    if not login_url:
        return {"ok": False, "motivo": "sitio sin login_url configurado",
                "titulo": ""}

    user_sel = cfg.get("user_sel") or "input[type=email], input[name=email], " \
                                      "input[name=username], #email, #user"
    pass_sel = cfg.get("pass_sel") or "input[type=password]"
    submit_sel = cfg.get("submit_sel") or "button[type=submit], input[type=submit]"
    espera = int(cfg.get("espera", 4000))

    try:
        page.goto(login_url, wait_until="domcontentloaded", timeout=45_000)
        page.wait_for_timeout(2000)

        # campos: se llenan los que existan (algunos sitios tienen 2 pasos)
        u = page.query_selector(user_sel)
        if u is None:
            return {"ok": False, "motivo": f"no encontre el campo usuario ({user_sel})",
                    "titulo": page.title() or ""}
        u.fill(cred["usuario"])
        p = page.query_selector(pass_sel)
        password_escrito = p is not None
        if password_escrito:
            p.fill(cred["password"])
        s = page.query_selector(submit_sel)
        if s is None:
            return {"ok": False, "motivo": "no encontre el boton de envio",
                    "titulo": page.title() or ""}
        s.click()
        page.wait_for_timeout(espera)

        # login en 2 pasos (Indeed, varios ATS): el password aparece despues
        if not password_escrito:
            p2 = page.query_selector(pass_sel)
            if p2 is not None:
                p2.fill(cred["password"])
                s2 = page.query_selector(submit_sel)
                if s2 is not None:
                    s2.click()
                    page.wait_for_timeout(espera)
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass

        titulo = (page.title() or "")
        # exito = el formulario de login ya no esta montado
        user_sigue = page.query_selector(user_sel) is not None
        pass_sigue = page.query_selector(pass_sel) is not None
        negativos = [m for m in (cfg.get("ok_negativos") or [])
                     if m.lower() in titulo.lower()]
        if negativos:
            return {"ok": False,
                    "motivo": f"el sitio seguia en login: {negativos[0]}",
                    "titulo": titulo}
        if user_sigue and pass_sigue:
            return {"ok": False, "motivo": "el formulario sigue ahi (credencial "
                                           "rechazada o paso extra/2FA)",
                    "titulo": titulo}
        if cfg.get("ok_sel") and page.query_selector(cfg["ok_sel"]) is None:
            return {"ok": False, "motivo": f"no aparecio {cfg['ok_sel']}",
                    "titulo": titulo}
        logger.info("auto_login %s: sesion iniciada.", empresa)
        return {"ok": True, "motivo": "", "titulo": titulo}
    except Exception as e:
        logger.warning("auto_login %s fallo: %s", empresa, e)
        return {"ok": False, "motivo": f"error: {e}", "titulo": ""}
