# -*- coding: utf-8 -*-
"""core.sesiones — gestion de SESIONES de los sitios que requieren login.

DECISION DE DISENO (definida con el usuario, 2026-10-19): NO se construye un
formulario donde el usuario meta credenciales (se guardarian contrasenas =
riesgo innecesario, y el login automatizado es lo que mas detectan
Cloudflare/LinkedIn/Indeed: CAPTCHA, 2FA, bloqueos). En su lugar:

- La app/CLI le DA los links: "Abrir en Brave" abre el sitio real en el
  navegador del usuario (el mismo perfil con el que el scraper corre por
  CDP). El usuario se loguea a mano UNA vez (con su 2FA/cuenta normal).
- La sesion queda en el perfil de Brave del usuario: el proveedor la reusa
  en las proximas corridas. CERO credenciales en el repo.

Quien necesita sesion hoy (evidencia de corridas reales):
- Indeed: la corrida 2026-10-19 mostro "Iniciar sesion | Cuentas Indeed"
  (0 cards en 4 de 6 busquedas) -> login-wall activo para scraping guest.
- LinkedIn: guest funciona con limite; el login amplia resultados (opcional).
- login_sites.json: sitios del employer tras Cloudflare/API del SPA.
"""
import json
import os
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

RAIZ = Path(__file__).resolve().parent.parent
LOGIN_SITES_JSON = RAIZ / "data" / "empresas" / "login_sites.json"
PENDING_LOGIN_JSON = RAIZ / "data" / "empresas" / "pending_login.json"

# Sesiones de JOB BOARDS que conviene tener (el proveedor ya usa el perfil
# del usuario por CDP, asi que estas sesiones aplican automaticamente).
# url_check: la verificacion carga una BUSQUEDA real (la homepage siempre
# tiene el boton 'Iniciar sesion' aunque estes logueado; el muro real
# aparece al buscar).
BOARDS_SESION = [
    {
        "empresa": "Indeed",
        "url": "https://www.indeed.com/",
        "url_check": "https://www.indeed.com/jobs?q=soporte&l=Heredia",
        "porque": "La corrida del 2026-10-19 encontro login-wall "
                  "('Iniciar sesion | Cuentas Indeed', 0 cards en varias "
                  "busquedas). Con sesion en tu Brave, el proveedor la "
                  "reusa y las cards vuelven.",
        "grupo": "Job board",
        "recomendado": True,
    },
    {
        "empresa": "LinkedIn",
        "url": "https://www.linkedin.com/",
        "url_check": "https://www.linkedin.com/jobs/search/"
                     "?keywords=soporte&location=Costa%20Rica",
        "porque": "Guest funciona con limite; con sesion amplias el "
                  "historial y evitas bloqueos progresivos (opcional).",
        "grupo": "Job board",
        "recomendado": False,
    },
]


def _brave_bin() -> str:
    return (os.getenv("BRAVE_BIN")
            or r"C:\Program Files\BraveSoftware\Brave-Browser\Application"
               r"\brave.exe")


def _brave_perfil() -> str:
    return (os.getenv("BRAVE_PERFIL")
            or os.path.join(os.environ.get("LOCALAPPDATA", ""),
                            "BraveSoftware\\Brave-Browser\\User Data"))


def abrir_en_brave(url: str) -> bool:
    """Abre el sitio en el Brave del usuario (ventana/pestaña normal, SIN
    taskkill y SIN puerto de debug). Si Brave ya corre con el perfil, abre
    una pestana en la sesion viva — el login queda en el perfil correcto,
    que es el que el scraper monta por CDP."""
    binp = _brave_bin()
    if not Path(binp).exists():
        return False
    subprocess.Popen(
        [binp, f"--user-data-dir={_brave_perfil()}", url],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return True


def _cargar_json(path: Path) -> list:
    try:
        if path.exists():
            d = json.loads(path.read_text(encoding="utf-8"))
            return d if isinstance(d, list) else []
    except Exception:
        pass
    return []


def sitios_con_sesion() -> List[Dict]:
    """Union de: boards recomendados + login_sites.json + motivos de
    pending_login.json. Cada item: empresa, url, grupo, porque,
    pendiente (motivo|None)."""
    pend = {p.get("empresa"): p for p in
            _cargar_json(PENDING_LOGIN_JSON) if isinstance(p, dict)}
    out: List[Dict] = []

    for b in BOARDS_SESION:
        item = dict(b)
        item["pendiente"] = None
        out.append(item)

    for s in _cargar_json(LOGIN_SITES_JSON):
        if not isinstance(s, dict):
            continue
        p = pend.get(s.get("empresa")) or {}
        out.append({
            "empresa": s.get("empresa") or "?",
            "url": (s.get("urls") or [""])[0],
            "grupo": "Employer (login_sites)",
            "porque": s.get("nota") or "",
            "recomendado": False,
            "pendiente": p.get("motivo"),
            "pendiente_ts": p.get("ts"),
        })
    # pendientes que ya no estan en login_sites.json (huerfanos)
    conocidos = {i["empresa"] for i in out}
    for empresa, p in pend.items():
        if empresa not in conocidos:
            out.append({
                "empresa": empresa,
                "url": p.get("url") or "",
                "grupo": "Pendiente historico",
                "porque": "",
                "recomendado": False,
                "pendiente": p.get("motivo"),
                "pendiente_ts": p.get("ts"),
            })
    return out


def verificar_sesion(url: str) -> Dict:
    """Monta Brave por CDP (el flujo normal de una corrida: cierra y reabre
    Brave con el perfil del usuario) y revisa si la pagina cae en login-wall
    o desafio Cloudflare. Devuelve {ok, titulo, motivo}.

    Login-wall: solo marcadores en el TITULO + frases fuertes del body
    ('inicia sesion para continuar'...) — el boton 'Iniciar sesion' de una
    homepage NO cuenta (esta siempre, logueado o no).
    """
    from core.browser_manager import BraveManager
    from providers.login.browser_sites import _BLOCK_DEFAULT, _LOGIN_DEFAULT

    frases_muro = ["inicia sesion para continuar", "inicia sesion para ver",
                   "sign in to continue", "sign in to see",
                   "log in to continue", "create an account to see",
                   "crea una cuenta para ver", "registrate para ver"]
    mgr = BraveManager({"brave_bin": _brave_bin(),
                        "brave_perfil": _brave_perfil()})
    port = mgr.start_brave()
    if not port:
        return {"ok": False, "titulo": "", "motivo": "Brave no arranco"}
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(f"http://localhost:{port}")
            page = browser.contexts[0].new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            page.wait_for_timeout(4000)
            titulo = (page.title() or "")
            try:
                cuerpo = page.inner_text("body")[:800].lower()
            except Exception:
                cuerpo = ""
            bajo = titulo.lower()
            login = [m for m in _LOGIN_DEFAULT if m in bajo]
            login += [m for m in frases_muro if m in cuerpo]
            cf = [m for m in _BLOCK_DEFAULT if m in bajo or m in cuerpo]
            if cf:
                return {"ok": False, "titulo": titulo,
                        "motivo": f"Desafio Cloudflare activo: {cf[0]}"}
            if login:
                return {"ok": False, "titulo": titulo,
                        "motivo": f"Pide sesion: {login[0]}"}
            return {"ok": True, "titulo": titulo, "motivo": ""}
    except Exception as e:
        return {"ok": False, "titulo": "", "motivo": f"Error: {e}"}
    finally:
        mgr.stop_brave()


def quitar_pendiente(empresa: str) -> bool:
    """Marca un pendiente como resuelto (lo saca de pending_login.json)."""
    data = [d for d in _cargar_json(PENDING_LOGIN_JSON)
            if isinstance(d, dict) and d.get("empresa") != empresa]
    try:
        PENDING_LOGIN_JSON.write_text(
            json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        return True
    except Exception:
        return False


def pendientes() -> List[Dict]:
    return _cargar_json(PENDING_LOGIN_JSON)
