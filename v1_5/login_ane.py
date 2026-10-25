"""Login de ANE en Brave real (CDP 9222) con credenciales guardadas."""
from playwright.sync_api import sync_playwright

LOGIN_URL = "https://www.ane.cr/Cuenta/Login"
CFG = {
    "login_url": LOGIN_URL,
    "user_sel": "input[name='UserName'], input#UserName, input[name='username'], "
                "input[type=text], input[type=email]",
    "pass_sel": "input[type=password]",
    "submit_sel": "button[type=submit], input[type=submit], button:has-text('Ingresar')",
    "espera": 5000,
}
USUARIO = "giovar24"
PASSWORD = "8aT4XKBYI305"

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
