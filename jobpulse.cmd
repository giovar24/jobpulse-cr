@echo off
chcp 65001 >nul
title JobPulse CR
cd /d "%~dp0"

rem ------------------------------------------------------------------
rem 1/3  La UI PRIMERO (Tkinter: proceso propio, sin navegador ni
rem      servidor; cerrar Brave ya no cierra nada). Si ya hay una
rem      ventana de JobPulse viva, no se lanza otra.
rem ------------------------------------------------------------------
echo [JobPulse] 1/3 UI de escritorio (ui.py)...
powershell -NoProfile -Command "$p = Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -like '*ui.py*' }; if ($p) { exit 0 } else { exit 1 }"
if errorlevel 1 (
    echo [JobPulse] Abriendo la ventana de JobPulse...
    powershell -NoProfile -Command "Start-Process -FilePath '.venv\Scripts\python.exe' -ArgumentList '-X','utf8','ui.py' -WindowStyle Minimized"
    powershell -NoProfile -Command "Start-Sleep -Seconds 3; $p = Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -like '*ui.py*' }; if ($p) { exit 0 } else { exit 1 }"
    if errorlevel 1 (
        echo [JobPulse] AVISO: la UI no arranco. Mira la consola minimizada de python.
    ) else (
        echo [JobPulse] UI lista en su propia ventana.
    )
) else (
    echo [JobPulse] La UI ya estaba abierta; se reutiliza.
)

rem ------------------------------------------------------------------
rem 2/3  Scraping. El progreso se publica en data\corrida_estado.json,
rem      asi que la pestana "Corrida" de la UI lo muestra en vivo
rem      (fase actual, grupos, vacantes encontradas); tambien sale en
rem      esta consola. Si ya hay una corrida viva, no se lanza otra.
rem      Paso extra de argumentos: jobpulse.cmd --preflight
rem      Corridas separadas:   jobpulse.cmd --solo ats     (portales de empresa)
rem                             jobpulse.cmd --solo boards  (Indeed, LinkedIn,
rem                                                          Procomer, ANE, CINDE)
rem ------------------------------------------------------------------
echo.
echo [JobPulse] 2/3 Scraping (seguilo en la pestana Corrida de la UI o aqui)...
powershell -NoProfile -Command "if (Test-Path data\corrida_estado.json) { $p = Get-Content data\corrida_estado.json -Raw | ConvertFrom-Json; if ($p.estado -eq 'corriendo' -and $p.pid -and (Get-Process -Id $p.pid -ErrorAction SilentlyContinue)) { exit 0 } }; exit 1"
if errorlevel 1 (
    ".venv\Scripts\python.exe" -X utf8 main.py %*
) else (
    echo [JobPulse] Ya hay una corrida en curso; no se lanza otra.
)

rem ------------------------------------------------------------------
rem 3/3
rem ------------------------------------------------------------------
echo.
echo [JobPulse] 3/3 Listo. La UI sigue abierta en su propia ventana;
echo            puedes cerrar esta consola cuando quieras.
rem `timeout` necesita stdin interactivo; si falla, esperamos con ping.
timeout /t 15 /nobreak >nul 2>nul
if errorlevel 1 ping -n 16 127.0.0.1 >nul
exit /b 0
