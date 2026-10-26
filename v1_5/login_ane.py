"""Login de ANE en Brave real (CDP 9222) con credenciales guardadas.

Las credenciales NUNCA viven en este archivo (regla del proyecto: el repo no
contiene secretos). Se leen del almacen local con `credencial("ANE")`:
  - keyring de Windows (Administrador de Credenciales) + metadatos en
    `data/empresas/credenciales.json` (gitignored), o
  - fallback `.env`: ANE_USER / ANE_PASSWORD.

Guardar/actualizar la credencial:
    .\.venv\Scripts\python.exe -m jobpulse_v15 credenciales set --empresa ANE --usuario <usuario> --password <password>
"""
import sys

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

from jobpulse_v15.credenciales import credencial

load_dotenv()  # asegura ANE_USER/ANE_PASSWORD disponibles desde .env

LOGIN_URL = "https://www.ane.cr/Cuenta/Login"
CFG = {
    "login_url": LOGIN_URL,
    "user_sel": "input[name='UserName'], input#UserName, input[name='username'], "
                "input[type=text], input[type=email]",
    "pass_sel": "input[type=password]",
    "submit_sel": "button[type=submit], input[type=submit], button:has-text('Ingresar')",
    "espera": 5000,
}

cred = credencial("ANE")
if not cred or not cred.get("usuario") or not cred.get("password"):
    print("ERROR: no hay credencial ANE usable (usuario + password).")
    print("Guardala con:")
    print("  .\\.venv\\Scripts\\python.exe -m jobpulse_v15 credenciales set "
          "--empresa ANE --usuario <usuario> --password <password>")
    print("o define ANE_USER/ANE_PASSWORD en .env.")
    raise SystemExit(1)

USUARIO = cred["usuario"]
PASSWORD = cred["password"]

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
    ctx = browser.contexts[0]
    page = ctx.new_page()

    try:
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=45_000)
        page.wait_for_timeout(2_000)

        u = page.query_selector(CFG["user_sel"])
        if u is None:
            print(f"ERROR: no encontre campo usuario. Titulo: {page.title()}")
            print("Inputs visibles:")
            for inp in page.query_selector_all("input")[:10]:
                print("  ", inp.get_attribute("name"), inp.get_attribute("type"), inp.get_attribute("id"))
            raise SystemExit(1)
        u.fill(USUARIO)

        pw = page.query_selector(CFG["pass_sel"])
        if pw is None:
            print("ERROR: no encontre campo password")
            raise SystemExit(1)
        pw.fill(PASSWORD)

        btn = page.query_selector(CFG["submit_sel"])
        if btn is None:
            print("ERROR: no encontre boton submit")
            raise SystemExit(1)
        btn.click()
        page.wait_for_timeout(5_000)

        html = page.content()
        if "Puesto/Detalles" in html or "dashboard" in html.lower():
            print("LOGIN ANE: OK")
        elif "loginForm" in html or "Cuenta/RegistroUsuario" in html:
            print("LOGIN ANE: FALLO (sigue en login). Titulo:", page.title())
        else:
            print("LOGIN ANE: INCIERTO. Titulo:", page.title())
            print("URL:", page.url)
    finally:
        page.close()
