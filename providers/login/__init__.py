# -*- coding: utf-8 -*-
"""providers.login — sitios del employer que requieren sesion de navegador.

Tercer grupo de providers (al lado de boards/ y directos/): empresas cuyo
front bloquea el HTTP crudo:

- interstitial Cloudflare (Nestle, Broadcom...) que solo se resuelve
  desde un navegador real;
- fronts SPA/widget propios que no exponen XHR parsable (Amazon, etc.);
- paginas que exigen sesion activa (session cookies del usuario).

Estrategia: **SESION PRIMERO** — el modulo reusa el Brave del usuario que
`main.py` ya lanza por CDP, asi que las cookies del perfil (login manual una
vez con `hacer_login.py`) valen en cada corrida. Como recurso OPT-IN, si hay
credencial local con auto_login ON (`core/credenciales.py`, gitignored) se
intenta el login automatico — incluso SSO Google ("Continuar con Google")
cuando la cuenta no tiene clave propia — y si el sitio responde con
CAPTCHA/2FA se registra el pendiente en data/empresas/pending_login.json y
se SALTA (nunca rompe la corrida). Los sitios que TÚ das de alta con URL
desde la pestaña Credenciales llegan aqui con origen "manual".
"""
