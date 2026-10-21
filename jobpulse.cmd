@echo off
chcp 65001 >nul
title JobPulse CR
cd /d "%~dp0"

rem ------------------------------------------------------------------
rem 1/2  SOLO la UI, SIN scraping y SIN ventanas de comandos.
rem      pythonw abre la UI sin consola propia - antes se lanzaba
rem      python minimizado y quedaba una ventana negra en la barra.
rem      El scraping se lanza a mano desde la pestana "Corrida" de la
rem      UI, o con:  jobpulse.cmd --correr
rem ------------------------------------------------------------------
echo [JobPulse] Abriendo la UI de escritorio - el scraping NO se lanza solo...
powershell -NoProfile -Command "$p = Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^pythonw?\.exe$' -and $_.CommandLine -like '*ui.py*' }; if ($p) { exit 0 } else { exit 1 }"
if errorlevel 1 (
    powershell -NoProfile -Command "Start-Process -FilePath '.venv\Scripts\pythonw.exe' -ArgumentList '-X','utf8','ui.py' -WindowStyle Hidden"
    powershell -NoProfile -Command "Start-Sleep -Seconds 3; $p = Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^pythonw?\.exe$' -and $_.CommandLine -like '*ui.py*' }; if ($p) { exit 0 } else { exit 1 }"
    if errorlevel 1 (
        echo [JobPulse] AVISO: la UI no arranco. Prueba a mano:
        echo            .venv\Scripts\pythonw.exe -X utf8 ui.py
        pause
    ) else (
        echo [JobPulse] UI abierta en su propia ventana, sin consolas.
    )
) else (
    echo [JobPulse] La UI ya estaba abierta; se reutiliza.
)

rem ------------------------------------------------------------------
rem 2/2  Scraping SOLO con --correr. Los argumentos extra se le pasan
rem      a main.py:
rem          jobpulse.cmd --correr                 corrida completa
rem          jobpulse.cmd --correr --solo ats       portales de empresa
rem          jobpulse.cmd --correr --solo boards   Indeed/LinkedIn/estatales
rem          jobpulse.cmd --correr --preflight     plan sin red
rem      La corrida abre su PROPIA ventana de consola VERDE - titulo
rem      "JobPulse CR - Scraping", texto verde color 0A - para distinguirla
rem      de cualquier otro proceso, y se cierra sola al terminar. Esta
rem      consola negra se cierra inmediatamente: no queda nada visible.
rem ------------------------------------------------------------------
setlocal enabledelayedexpansion
set "CORRER=0"
set "ARGS="
for %%a in (%*) do (
    if /i "%%a"=="--correr" (set "CORRER=1") else (set "ARGS=!ARGS! %%a")
)
if "!CORRER!"=="0" (
    echo [JobPulse] Listo. Para correr el scraping: pestana "Corrida" de la UI
    echo            o bien:  jobpulse.cmd --correr
    endlocal & exit /b 0
)

powershell -NoProfile -Command "if (Test-Path data\corrida_estado.json) { $p = Get-Content data\corrida_estado.json -Raw | ConvertFrom-Json; if ($p.estado -eq 'corriendo' -and $p.pid -and (Get-Process -Id $p.pid -ErrorAction SilentlyContinue)) { exit 0 } }; exit 1"
if not errorlevel 1 (
    echo [JobPulse] Ya hay una corrida en curso; no se lanza otra.
    endlocal & exit /b 0
)
start "JobPulse CR - Scraping" cmd /d /s /c "chcp 65001 >nul && color 0A && title JobPulse CR - Scraping && ".venv\Scripts\python.exe" -u -X utf8 main.py!ARGS!"
endlocal & exit /b 0
