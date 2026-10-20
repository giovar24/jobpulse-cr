# -*- coding: utf-8 -*-
"""hacer_login.py — resolver los LOGINS pendientes A MANO (sesion en tu Brave).

Flujo por defecto (sin guardar nada): abrimos cada sitio en TU Brave, te
logueas con tu cuenta/2FA normal, y la sesion queda en el perfil — el mismo
que el scraper monta por CDP, asi que las corridas la reusan.

    python hacer_login.py          # recorre pendientes + boards uno por uno
    python hacer_login.py --list   # solo muestra la lista

Si un sitio EXIGE password (obligatorio) y quieres que el scraper lo haga
solo: pone tu credencial localmente en la pestaña "🔑 Credenciales" del
dashboard o con `python credenciales.py --set "Empresa"`. Queda en
`data/empresas/credenciales.json` (LOCAL, en `.gitignore`), y si el login
automatico falla (CAPTCHA/2FA) el sitio vuelve a aparecer aqui como
pendiente manual.

(Equivalente grafico: pestañas "Credenciales" y "Sesiones" del dashboard)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.sesiones import (abrir_en_brave, pendientes,  # noqa: E402
                           quitar_pendiente, sitios_con_sesion,
                           verificar_sesion)


def main():
    sitios = sitios_con_sesion()
    pend = pendientes()

    print("\n== SESIONES DE NAVEGADOR DE JOB PULSE CR ==")
    print("Por defecto el login lo haces A MANO en tu Brave. Para los sitios\n"
          "que exigen password puedes guardar tu credencial localmente:\n"
          "  python credenciales.py --set \"Empresa\"  (o pestaña Credenciales)\n")
    for s in sitios:
        est = ("PENDIENTE: " + (s.get("pendiente") or "")
               if s.get("pendiente")
               else ("recomendado" if s.get("recomendado") else "configurado"))
        print(f"  [{est}] {s['empresa']} — {s.get('url','')}")
        if s.get("porque"):
            print(f"      {s['porque'][:150]}")
    if not pend:
        print("\nSin pendientes bloqueantes. Los 'recomendados' (Indeed) "
              "suman cobertura si los activas.")
    if "--list" in sys.argv:
        return

    print("\nSe abrira cada sitio en TU Brave (pestaña en tu sesion viva).")
    print("Logueate con tu cuenta normal y vuelve aqui para confirmar.\n")
    for s in sitios:
        url = s.get("url") or ""
        if not url:
            continue
        if not abrir_en_brave(url):
            print(f"xx No encontre brave.exe para {s['empresa']} "
                  "(configura BRAVE_BIN en .env)")
            continue
        print(f">> {s['empresa']}: {url}")
        r = input("   Logueado/con sesion activa? [s]=resuelto "
                  "[v]=verificar [n]=sigue pendiente "
                  "[enter]=siguiente: ").strip().lower()
        if r == "s" and s.get("pendiente"):
            quitar_pendiente(s["empresa"])
            print("   pendiente resuelto ✓")
        elif r == "n":
            print("   queda pendiente (se reintentara en la proxima corrida)")
        elif r == "v":
            res = verificar_sesion(s.get("url_check") or url)
            print("   verificar:", "OK —" if res["ok"] else "SIN sesion —",
                  res.get("titulo", "")[:60] if res["ok"]
                  else res.get("motivo", ""))
    print("\nListo. La proxima corrida (python main.py) reusa estas sesiones.")


if __name__ == "__main__":
    main()
