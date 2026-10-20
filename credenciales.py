# -*- coding: utf-8 -*-
"""credenciales.py — CLI: LA LISTA de sitios que piden password + gestion de credenciales.

    python credenciales.py                      # la lista + estado (faltantes)
    python credenciales.py --set "Indeed"       # pedir usuario/password por teclado
    python credenciales.py --set "Indeed" --user "yo@x.com" --pass "S3creto"
    python credenciales.py --borrar "Indeed"    # borrar la credencial guardada
    python credenciales.py --auto "Indeed" off  # activa/desactiva el login auto
    python credenciales.py --set "Indeed" --auto on   # guardar y activar auto-login

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
                               registrar_requerimiento, resumen,
                               upsert_credencial)


def imprimir_lista():
    s = resumen()
    print("\n== SITIOS QUE PIDEN PASSWORD / SESION (JobPulse CR) ==")
    print(f"   {s['total']} sitios | {s['obligatorios']} obligatorios | "
          f"{s['con_credencial']} con credencial | {s['pendientes']} pendientes\n")
    for it in lista_sitios_login():
        estado = []
        estado.append("OBLIGATORIO" if it["obligatorio"] else "opcional")
        estado.append("credencial ✓" if it["tiene_credencial"]
                      else "SIN credencial")
        if it.get("auto_login"):
            estado.append("auto-login")
        if it.get("pendiente"):
            estado.append(f"pendiente: {it['pendiente']}")
        grupo = GRUPO_LABEL.get(it.get("grupo") or "", "—")
        print(f"  • {it['empresa']:<22} [{estado[0]}] {', '.join(estado[1:])}")
        print(f"      grupo: {grupo}")
        if it.get("usuario"):
            print(f"      usuario: {it['usuario']} "
                  f"({it.get('password_fuente') or 'json'})")
        if it.get("motivo"):
            print(f"      motivo: {it['motivo'][:150]}")
    print()
    print("Para poner tu password:  python credenciales.py --set \"Empresa\"")


def cmd_set(empresa: str, usuario: str | None, password: str | None):
    if not usuario:
        usuario = input(f"  Usuario de {empresa}: ").strip()
    if not password:
        password = getpass.getpass(f"  Password de {empresa} (no se muestra): ")
    if not usuario or not password:
        print("  Cancelado: faltan usuario o password.")
        return
    reg = upsert_credencial(empresa, usuario, password)
    print(f"  ✓ Guardado para {reg['empresa']} (auto-login ON, solo en esta "
          f"máquina; el archivo está en .gitignore).")


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
    args = ap.parse_args()

    if args.set:
        cmd_set(args.set, args.usuario, args.password)
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
    if not (args.set or args.borrar or args.auto):
        imprimir_lista()


if __name__ == "__main__":
    main()
