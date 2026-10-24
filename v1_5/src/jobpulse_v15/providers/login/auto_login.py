# -*- coding: utf-8 -*-
"""providers.login.auto_login — login automatico OPT-IN con fallback manual.

El usuario pidio poder poner SU password en los sitios que lo exigen
obligatoriamente (ver `jobpulse_v15.credenciales`). Reglas:

- Solo intenta el login si (a) hay credencial guardada para la empresa, y
  (b) el sitio/registro tiene `auto_login` activo. Si no, se comporta como
  antes (sesion manual via Brave / pestaña "Sesiones").
- **SSO**: si la credencial NO tiene password sino `sso` (p.ej. Indeed,
  que se entra con la cuenta de Gmail), se usa `_login_sso()`: clic en
  "Continuar con Google" + eleccion de la cuenta en el navegador del
  usuario. Si Google pide la clave (sesion vencida) o hay CAPTCHA/2FA, el
  intento falla y se cae al fallback manual sin romper la corrida.
- El login se hace en el MISMO Brave y el MISMO perfil que ya usa el
  scraper por CDP.
- Selectores por sitio: `{login_url, user_sel, pass_sel, submit_sel,
  ok_sel, ok_negativos, sso, sso_iframe, sso_inner_sel, espera}`.
  Sin `login_url` no hay login automatico.
"""
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger("jobpulse.auto_login")

# Boton "Continuar con Google" (ES/EN) en paginas de login tipo Indeed.
_SSO_GOOGLE_SEL = (
    '[data-tn-element*="google"], [id*="google-button"], '
    'button:has-text("Google"), a:has-text("Google"), '
    '[role=button]:has-text("Google")'
)


def _sitio_en_login(page, cfg) -> str:
    """Devuelve '' si el sitio ya no esta en la pantalla de login, o el motivo."""
    url = page.url or ""
    titulo = (page.title() or "")
    if "accounts.google.com" in url:
        return f"seguia en Google ({url})"
    user_sel = cfg.get("user_sel") or "input[type=email]"
    pass_sel = cfg.get("pass_sel") or "input[type=password]"
    negativos = [m for m in (cfg.get("ok_negativos") or [])
                 if m.lower() in titulo.lower()]
    if negativos:
        return f"el sitio seguia en login: {negativos[0]}"
    if page.query_selector(user_sel) and page.query_selector(pass_sel):
        return "el formulario sigue ahi (credencial rechazada o paso extra/2FA)"
    return ""


def _clic_sso(page, cfg) -> str:
    """Clic en el boton SSO. Devuelve '' si salio bien, o el motivo.

    Google Identity Services monta el boton DENTRO de su propio iframe
    (Indeed: `#login-google-button iframe`; LinkedIn: `iframe[src*=
    "accounts.google.com"]`): clicar solo el contenedor no hace nada
    (validado en vivo 2026-10-19 para ambos)."""
    inner_sel = cfg.get("sso_inner_sel") or 'div[role="button"]'
    iframes = ([cfg["sso_iframe"]] if cfg.get("sso_iframe") else [
        "#login-google-button iframe",              # Indeed
        'iframe[src*="accounts.google.com"]',       # GIS generico (LinkedIn, etc.)
    ])
    for ifr in iframes:
        try:
            page.frame_locator(ifr).locator(inner_sel).first.click(timeout=8_000)
            return ""
        except Exception:
            continue
    sel = cfg.get("sso_sel") or _SSO_GOOGLE_SEL
    try:
        page.locator(sel).first.click(timeout=8_000)
        return ""
    except Exception as e:
        return f"no encontre el boton SSO ({sel}): {e}"


def _elegir_cuenta_google(pg, email: str) -> bool:
    """Selector de cuentas de Google ('¿Quién eres?'). Devuelve True si
    clickeo alguna cuenta (la nuestra si aparece, o la primera listada)."""
    candidatos = []
    if email:
        candidatos += [f'[data-identifier="{email}"]',
                       f'text={email}',
                       f'li:has-text("{email}")']
    candidatos += ['li[data-identifier]', '[data-identifier]',
                   'div[data-authuser]', 'li']
    for c in candidatos:
        try:
            pg.locator(c).first.click(timeout=4000)
            return True
        except Exception:
            continue
    return False


def _login_sso(page, empresa: str, cred: dict, cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Login via proveedor externo (Google) reutilizando la SESION del
    navegador del usuario: no hay password nuestro, se usa su Gmail ya
    abierto en Brave. Si Google pide la clave (sesion vencida) o no se puede
    elegir la cuenta, se aborta y el llamador cae al fallback manual sin
    romper la corrida."""
    login_url = cfg.get("login_url")
    espera = int(cfg.get("espera", 5000))
    email = (cred.get("usuario") or "").strip()
    proveedor = (cred.get("sso") or cfg.get("sso") or "google").lower()

    page.goto(login_url, wait_until="domcontentloaded", timeout=45_000)
    try:
        page.wait_for_load_state("networkidle", timeout=12_000)
    except Exception:
        pass
    page.wait_for_timeout(2000)

    previas = set(page.context.pages)
    fallo_clic = _clic_sso(page, cfg)
    if fallo_clic:
        return {"ok": False, "motivo": fallo_clic, "titulo": page.title() or ""}

    # GIS/Google abren la autenticacion en una PESTANA NUEVA (popup) o, en
    # algunos flujos, navegan la misma. Buscamos cual de las dos paso.
    try:
        pop = page.wait_for_event("popup", timeout=8_000)
    except Exception:
        pop = None
    if pop is None:
        nuevos = [p for p in page.context.pages if p not in previas]
        pop = nuevos[-1] if nuevos else None
    obj = pop or page
    try:
        obj.wait_for_load_state("domcontentloaded", timeout=20_000)
    except Exception:
        pass
    obj.wait_for_timeout(2500)

    url = obj.url or ""
    if "google.com" in url:
        # 1) Google pide la password de la cuenta: no la tenemos -> abortar
        if obj.locator('input[type=password]').count():
            return {"ok": False,
                    "motivo": f"{proveedor} pide la password de la cuenta "
                              f"(sesion vencida); se usa el login manual",
                    "titulo": obj.title() or ""}
        # 2) selector de cuentas (si la sesion de Gmail esta activa)
        if not _elegir_cuenta_google(obj, email):
            return {"ok": False,
                    "motivo": f"llegue a {proveedor} pero no encontre la "
                              f"cuenta {email or '(sin usuario)'} en el "
                              f"selector; se usa el login manual",
                    "titulo": obj.title() or ""}
        # esperar a que el popup cierre y la pagina principal vuelva login
        for _ in range(int(espera / 250)):
            try:
                if pop is not None and pop.is_closed():
                    break
            except Exception:
                break
            url_pri = page.url or ""
            if "accounts.google.com" not in url_pri and \
                    not url_pri.rstrip("/").endswith("/auth"):
                break
            page.wait_for_timeout(250)
        page.wait_for_timeout(2000)

    try:
        page.wait_for_load_state("networkidle", timeout=12_000)
    except Exception:
        pass
    page.wait_for_timeout(1500)

    url_pri = (page.url or "")
    if "accounts.google.com" in url_pri or url_pri.rstrip("/").endswith("/auth"):
        return {"ok": False,
                "motivo": f"la sesion no se completo (seguia en {url_pri})",
                "titulo": page.title() or ""}
    motivo_muro = _sitio_en_login(page, cfg)
    if motivo_muro:
        return {"ok": False, "motivo": motivo_muro, "titulo": page.title() or ""}
    logger.info("auto_login %s (SSO %s): sesion iniciada con la cuenta %s.",
                empresa, proveedor, email or "(del navegador)")
    return {"ok": True, "motivo": "", "titulo": page.title() or ""}


def _cfg_sitio(site: dict) -> dict:
    """Bloque `login` de login_sites.json (o el propio site si es plano)."""
    cfg = site.get("login")
    return cfg if isinstance(cfg, dict) else {}


def hay_login_configurable(empresa: str, site: Optional[dict] = None) -> bool:
    """True si hay credencial + auto_login + selectores para intentarlo."""
    from jobpulse_v15.credenciales import credencial, lista_sitios_login

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
    from jobpulse_v15.credenciales import credencial

    cred = credencial(empresa)
    if not cred:
        return {"ok": False, "motivo": "sin credencial guardada", "titulo": ""}
    login_url = cfg.get("login_url")
    if not login_url:
        return {"ok": False, "motivo": "sitio sin login_url configurado",
                "titulo": ""}

    # SSO (Indeed con Google/Gmail): no hay clave propia, se reutiliza la
    # sesion de la cuenta que el usuario ya tiene abierta en Brave.
    if not cred.get("password") and (cred.get("sso") or cfg.get("sso")):
        return _login_sso(page, empresa, cred, cfg)

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