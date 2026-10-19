"""Recordatorio interactivo de corrida (sin servidor 24h).

Task Scheduler lo dispara AL INICIAR SESION (instalar_recordatorio.ps1). El
equipo no necesita estar encendido a una hora fija: cuando arranca, el
recordatorio revisa y solo pregunta si hace falta.

Flujo:
  1. Ultima corrida real = MAX(created_at) en jobs_raw.
  2. Si ya se corrio hoy, o el usuario ya descarto el recordatorio hoy ->
     sale silencioso (nada en pantalla).
  3. Si no, dialogo tkinter:
       [ Correr ahora ]   -> ejecuta jobpulse.cmd (scraping + dashboard).
       [ Mas tarde... ]   -> pide la hora (HH:MM) y programa UNA corrida para
                             ese momento (Task Scheduler ONCE +
                             StartWhenAvailable: si el equipo estaba apagado,
                             corre en cuanto arranque). Si la hora ya paso
                             hoy, se programa para manana a esa hora.
       [ Hoy no ]         -> no vuelve a molestar hasta manana.

Prueba manual:  python recordatorio.py --forzar   (ignora "ya corrio hoy")
"""
import json
import sqlite3
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent
ESTADO = BASE / "data" / "recordatorio_estado.json"
TAREA_DIFERIDA = "JobPulse Corrida Diferida"

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def ultima_corrida():
    """MAX(created_at) de jobs_raw: la ultima vez que el sistema scrapeo."""
    db = BASE / "data" / "jobs.db"
    if not db.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        (val,) = con.execute("select max(created_at) from jobs_raw").fetchone()
        con.close()
    except sqlite3.Error:
        return None
    try:
        return datetime.fromisoformat((val or "")[:19])
    except ValueError:
        return None


def _estado():
    try:
        return json.loads(ESTADO.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def descartado_hoy() -> bool:
    return _estado().get("descartado") == date.today().isoformat()


def marcar_descartado():
    ESTADO.parent.mkdir(parents=True, exist_ok=True)
    ESTADO.write_text(json.dumps({"descartado": date.today().isoformat()}),
                      encoding="utf-8")


def ya_corrio_hoy(ult) -> bool:
    return bool(ult) and ult.date() == date.today()


def programar_diferida(hh: int, mm: int):
    """Registra la corrida unica para hoy (o manana) a la hora dada."""
    ahora = datetime.now()
    objetivo = ahora.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if objetivo <= ahora:
        objetivo += timedelta(days=1)  # la hora ya paso: manana a esa hora
    ps = (
        "$a = New-ScheduledTaskAction -Execute 'cmd.exe' "
        f"-Argument '/c \"{BASE / 'jobpulse.cmd'}\"' -WorkingDirectory '{BASE}'; "
        "$t = New-ScheduledTaskTrigger -Once -At "
        f"'{objetivo.strftime('%Y-%m-%dT%H:%M:%S')}'; "
        "$s = New-ScheduledTaskSettingsSet -StartWhenAvailable "
        "-AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
        "-ExecutionTimeLimit (New-TimeSpan -Hours 2); "
        f"Register-ScheduledTask -TaskName '{TAREA_DIFERIDA}' "
        "-Action $a -Trigger $t -Settings $s -Force | Out-Null"
    )
    r = subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps],
        capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout or "").strip()[:200])
    return objetivo


def lanzar_corrida():
    """Ejecuta jobpulse.cmd en su propia ventana (scraping + dashboard)."""
    subprocess.Popen(
        ["cmd", "/c", str(BASE / "jobpulse.cmd")],
        cwd=str(BASE),
        creationflags=subprocess.CREATE_NEW_CONSOLE,
        close_fds=False,
    )


# --------------------------------------------------------------- dialogo UI
def dialogo(ult):
    import tkinter as tk
    from tkinter import ttk

    raiz = tk.Tk()
    raiz.title("JobPulse CR")
    raiz.resizable(False, False)
    raiz.attributes("-topmost", True)
    raiz.update_idletasks()

    hace = "nunca" if not ult else f"hace {hace_texto(ult)}"
    marco = ttk.Frame(raiz, padding=18)
    marco.grid()

    ttk.Label(marco, text="JobPulse CR — recordatorio de corrida",
              font=("Segoe UI", 11, "bold")).grid(column=0, row=0, columnspan=3,
                                                   sticky="w", pady=(0, 8))
    ttk.Label(marco, text=f"Última corrida: {hace}.").grid(
        column=0, row=1, columnspan=3, sticky="w", pady=(0, 2))
    ttk.Label(marco, text="¿Correr el scraping ahora? (lleva ~5 minutos)").grid(
        column=0, row=2, columnspan=3, sticky="w", pady=(0, 12))

    resultado = {"accion": "hoy-no"}

    def correr():
        resultado["accion"] = "correr"
        raiz.destroy()

    def despues():
        resultado["accion"] = "mas-tarde"
        raiz.destroy()

    def hoy_no():
        resultado["accion"] = "hoy-no"
        raiz.destroy()

    botones = ttk.Frame(marco)
    botones.grid(column=0, row=3, columnspan=3)
    ttk.Button(botones, text="✅ Correr ahora", command=correr
               ).grid(column=0, row=0, padx=4)
    ttk.Button(botones, text="⏰ Más tarde...", command=despues
               ).grid(column=1, row=0, padx=4)
    ttk.Button(botones, text="😴 Hoy no", command=hoy_no
               ).grid(column=2, row=0, padx=4)

    raiz.eval("tk::PlaceWindow . center")
    raiz.mainloop()
    return resultado["accion"]


def hace_texto(ult) -> str:
    delta = datetime.now() - ult
    horas = delta.total_seconds() / 3600
    if horas < 1:
        return "hace un rato"
    if horas < 24:
        return f"hace {int(horas)} h"
    return f"hace {int(horas // 24)} día(s)"


def dialogo_hora():
    """Pide HH:MM para la corrida diferida. None si cancela."""
    import tkinter as tk
    from tkinter import ttk

    raiz = tk.Tk()
    raiz.title("JobPulse CR — ¿a qué hora?")
    raiz.resizable(False, False)
    raiz.attributes("-topmost", True)
    marco = ttk.Frame(raiz, padding=18)
    marco.grid()

    ttk.Label(marco, text="¿A qué hora quieres que corra?",
              font=("Segoe UI", 11, "bold")).grid(column=0, row=0,
                                                   columnspan=2, sticky="w",
                                                   pady=(0, 8))
    ttk.Label(marco, text="Hora (HH:MM). Si ya pasó hoy, se programa\n"
                          "para mañana; y si el equipo estaba apagado,\n"
                          "corre en cuanto arranque."
              ).grid(column=0, row=1, columnspan=2, sticky="w", pady=(0, 8))

    var = tk.StringVar(value=datetime.now().strftime("%H:%M"))
    entrada = ttk.Entry(marco, textvariable=var, width=10, justify="center")
    entrada.grid(column=0, row=2, padx=(0, 8))
    entrada.focus_set()
    entrada.select_range(0, "end")

    resultado = {"hora": None}
    msg = tk.StringVar()

    def aceptar(_evento=None):
        try:
            hh, mm = (int(x) for x in var.get().strip().split(":"))
            if not (0 <= hh <= 23 and 0 <= mm <= 59):
                raise ValueError
            resultado["hora"] = (hh, mm)
            raiz.destroy()
        except ValueError:
            msg.set("Formato inválido: usa HH:MM (ej. 18:30)")

    def cancelar():
        resultado["hora"] = None
        raiz.destroy()

    ttk.Button(marco, text="Programar", command=aceptar).grid(column=1, row=2)
    ttk.Button(marco, text="Cancelar", command=cancelar).grid(
        column=0, row=4, columnspan=2, pady=(10, 0))
    ttk.Label(marco, textvariable=msg, foreground="#b00").grid(
        column=0, row=5, columnspan=2)
    raiz.bind("<Return>", aceptar)
    raiz.bind("<Escape>", cancelar)
    raiz.eval("tk::PlaceWindow . center")
    raiz.mainloop()
    return resultado["hora"]


# --------------------------------------------------------------------- main
def main():
    forzar = "--forzar" in sys.argv
    ult = ultima_corrida()
    if not forzar and (ya_corrio_hoy(ult) or descartado_hoy()):
        return  # todo en orden: nada en pantalla

    accion = dialogo(ult)
    if accion == "correr":
        lanzar_corrida()
    elif accion == "mas-tarde":
        hora = dialogo_hora()
        if hora:
            try:
                objetivo = programar_diferida(*hora)
                marcar_descartado()  # el recordatorio ya cumplio su turno
                print(f"Corrida programada para {objetivo:%H:%M de %d/%m}.")
            except Exception as exc:  # powershell fallo: fallback silencioso
                print(f"No se pudo programar la corrida: {exc}")
    else:  # hoy-no
        marcar_descartado()


if __name__ == "__main__":
    main()
