# -*- coding: utf-8 -*-
"""credenciales.py — CLI: LA LISTA de sitios que piden password + gestion de credenciales.

    python credenciales.py                      # la lista + estado (faltantes)
    python credenciales.py --set "Indeed"       # pedir usuario/password por teclado
    python credenciales.py --set "Indeed" --user "yo@x.com" --pass "S3creto"
    python credenciales.py --set "Indeed" --user "mi@gmail.com" --sso google
                                             # Indeed no tiene clave: entra con Google
    python credenciales.py --set "MiPortal" --user "u" --pass "p" \
        --url "https://miportal.com/jobs"
                                             # sitio NUEVO: se da de alta solo en la
                                             # lista y en el barrido de portales
    python credenciales.py --borrar "Indeed"    # borrar la credencial guardada
    python credenciales.py --auto "Indeed" off  # activa/desactiva el login auto
    python credenciales.py --set "Indeed" --auto on   # guardar y activar auto-login
    python credenciales.py --quitar-sitio "MiPortal"  # quitar un sitio que tú agregaste

Las credenciales quedan en `data/empresas/credenciales.json` (LOCAL y en
`.gitignore`: nunca viajan a git). Equivalente grafico: pestaña
"🔑 Credenciales" del dashboard (streamlit run app.py).
"""
import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()   # el fallback ANE_USER/ANE_PASSWORD del .env se refleja en la lista

from core.credenciales import (GRUPO_LABEL, borrar_credencial,  # noqa: E402
                               credencial, lista_sitios_login, marcar_auto_login,
                               quitar_sitio_login, registrar_requerimiento,
                               resumen, sitio_en_flujo, upsert_credencial)


def imprimir_lista():
    s = resumen()
    print("\n== SITIOS QUE PIDEN PASSWORD / SESION (JobPulse CR) ==")
    print(f"   {s['total']} sitios | {s['obligatorios']} obligatorios | "
          f"{s['con_credencial']} con credencial | {s['pendientes']} pendientes\n")
    for it in lista_sitios_login():
        estado = []
        estado.append("OBLIGATORIO" if it["obligatorio"] else "opcional")
        if it["tiene_credencial"]:
            estado.append("vía " + it["sso"].capitalize() if it.get("sso")
                          else "credencial ✓")
        else:
            estado.append("SIN credencial")
        if it.get("auto_login"):
            estado.append("auto-login")
        if it.get("pendiente"):
            estado.append(f"pendiente: {it['pendiente']}")
        grupo = GRUPO_LABEL.get(it.get("grupo") or "", "—")
        print(f"  • {it['empresa']:<22} [{estado[0]}] {', '.join(estado[1:])}")
        print(f"      grupo: {grupo}")
        if it.get("usuario"):
            print(f"      usuario: {it['usuario']} "
                  f"({it.get('password_fuente') or 'json'}"
                  f"{', sin clave: ' + it['sso'] if it.get('sso') else ''})")
        if it.get("motivo"):
            print(f"      motivo: {it['motivo'][:150]}")
    print()
    print('Para poner tu password:   python credenciales.py --set "Empresa"')
    print('Sitio nuevo con URL:      python credenciales.py --set "X" --url '
          '"https://..."' + '  (se da de alta solo en la lista y el barrido)')


def cmd_set(empresa: str, usuario: str | None, password: str | None,
            sso: str = "", url: str = ""):
    if not usuario:
        usuario = input(f"  Usuario de {empresa}: ").strip()
    if not password and not sso:
        password = getpass.getpass(f"  Password de {empresa} (no se muestra): ")
    if not usuario or (not password and not sso):
        print("  Cancelado: falta usuario, o password y metodo SSO "
              "(usa --sso google si la cuenta no tiene clave propia).")
        return
    nuevo = not sitio_en_flujo(empresa)
    reg = upsert_credencial(empresa, usuario, password or "", sso=sso,
                            url=url)
    if sso:
        print(f"  ✓ Guardado para {reg['empresa']}: acceso vía {sso} "
              f"(sin password; solo en esta máquina, el archivo está en "
              f".gitignore).")
    else:
        print(f"  ✓ Guardado para {reg['empresa']} (auto-login ON, solo en "
              f"esta máquina; el archivo está en .gitignore).")
    if nuevo and url:
        print(f"  ✓ Sitio NUEVO dado de alta en LA LISTA y en el barrido de "
              f"portales (login_sites.json): cada corrida lo visita en tu "
              f"Brave.")
    elif nuevo:
        print(f"  ✓ Sitio NUEVO en LA LISTA. Sin --url no se barre: repite "
              f"con --url \"https://...\" para que el scraper lo recorra.")


def cmd_quitar_sitio(empresa: str):
    if not sitio_en_flujo(empresa):
        print(f"  {empresa} no está en la lista ni en el barrido.")
        return
    res = quitar_sitio_login(empresa)
    partes = []
    if res["quitado_de_lista"]:
        partes.append("lista")
    if res["quitado_de_barrido"]:
        partes.append("barrido")
    print(f"  ✓ {empresa}: quitado de {' y '.join(partes) or 'nada (es un sitio fijo del sistema)'}"
          f" (la credencial guardada se conserva).")


def cmd_del(empresa: str):
    if borrar_credencial(empresa):
        print(f"  ✓ Credencial de {empresa} borrada.")
    else:
        print(f"  No había credencial guardada para {empresa}.")


def cmd_auto(empresa: str, valor: str):
    v = valor.strip().lower() in ("on", "1", "true", "si", "sí", "yes")
    if not v:
        reg = credencial(empresa)
        if not reg:
            print(f"  Nota: {empresa} no tiene credencial guardada (solo se "
                  f"apaga el auto-login).")
    if marcar_auto_login(empresa, v):
        print(f"  ✓ auto-login de {empresa}: {'ON' if v else 'OFF'}")
    else:
        print(f"  {empresa} no está en la lista de sitios que piden login; "
              f"se registra ahora.")
        registrar_requerimiento(empresa, "", "registrado desde CLI", obligatorio=False)
        marcar_auto_login(empresa, v)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--set", metavar="EMPRESA", help="guardar usuario/password")
    ap.add_argument("--borrar", "--del", metavar="EMPRESA", dest="borrar",
                    help="borrar credencial")
    ap.add_argument("--auto", nargs="+", metavar="EMPRESA|on|off",
                    help="activar/desactivar el login automatico: "
                         "--auto EMPRESA on|off  (o --set EMPRESA ... --auto on)")
    ap.add_argument("--usuario", "--user", dest="usuario",
                    help="usuario (junto con --set, sin prompt)")
    ap.add_argument("--password", "--pass", "--pwd", dest="password",
                    help="password (junto con --set, sin prompt)")
    ap.add_argument("--sso", metavar="PROVEEDOR", dest="sso",
                    help="acceso sin password propio (junto con --set): "
                         "google | apple | ... (p.ej. Indeed usa Gmail)")
    ap.add_argument("--url", metavar="URL", dest="url",
                    help="URL del portal (junto con --set): si la empresa es "
                         "nueva, se da de alta en la lista y en el barrido")
    ap.add_argument("--quitar-sitio", metavar="EMPRESA", dest="quitar_sitio",
                    help="quitar de la lista/barrido un sitio que TÚ agregaste "
                         "(los fijos del sistema no se quitan)")
    args = ap.parse_args()

    if args.set:
        cmd_set(args.set, args.usuario, args.password, sso=args.sso or "",
                url=args.url or "")
    if args.quitar_sitio:
        cmd_quitar_sitio(args.quitar_sitio)
    if args.borrar:
        cmd_del(args.borrar)
    if args.auto:
        if len(args.auto) == 2:
            cmd_auto(args.auto[0], args.auto[1])
        elif len(args.auto) == 1 and args.set:
            cmd_auto(args.set, args.auto[0])
        else:
            ap.error("--auto necesita: EMPRESA on|off "
                     "(o una sola palabra si va junto con --set)")
    if not (args.set or args.borrar or args.auto or args.quitar_sitio):
        imprimir_lista()


if __name__ == "__main__":
    main()
