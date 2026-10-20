# -*- coding: utf-8 -*-
"""providers.login — sitios del employer que requieren sesion de navegador.

Tercer grupo de providers (al lado de boards/ y directos/): empresas cuyo
front bloquea el HTTP crudo:

- interstitial Cloudflare (Nestle, PayPal, Broadcom...) que solo se resuelve
  desde un navegador real;
- fronts SPA/widget propios que no exponen XHR parsable (Alorica, Amazon);
- paginas que exigen sesion activa (session cookies del usuario).

Estrategia (SIN guardar contrasenas ni credenciales): el modulo reusa el
Brave del usuario que `main.py` ya lanza por CDP — el perfil del usuario
trae sus cookies. Si una pagina exige login y NO hay sesion, el provider
registra el pendiente en data/empresas/pending_login.json y SALTA (nunca
rompe la corrida). Para resolver el pendiente: `python hacer_login.py` abre
las URLs para loguearse a mano; la sesion queda en el perfil del navegador
y es reutilizada por las proximas corridas.
"""
