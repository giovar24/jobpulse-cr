@echo off
chcp 65001 >nul
title JobPulse CR
cd /d "%~dp0"

rem ------------------------------------------------------------------
rem 1/3  El DASHBOARD PRIMERO (bug: antes el scraping tomaba 5+ minutos
rem      de consola y la GUI solo aparecia al final, o nunca si main.py
rem      fallaba). Arrancamos Streamlit, esperamos a que responda y
rem      abrimos el navegador ANTES de tocar la red.
rem ------------------------------------------------------------------
echo [JobPulse] 1/3 Dashboard (http://localhost:8501)...
powershell -NoProfile -Command "if (Get-NetTCPConnection -LocalPort 8501 -State Listen -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }"
if errorlevel 1 (
    echo [JobPulse] Arrancando Streamlit... la primera vez tarda unos segundos.
    powershell -NoProfile -Command "Start-Process -FilePath '.venv\Scripts\python.exe' -ArgumentList '-m','streamlit','run','app.py','--server.headless','true','--server.port','8501','--browser.gatherUsageStats','false' -RedirectStandardOutput 'data\streamlit.log' -RedirectStandardError 'data\streamlit_err.log' -WindowStyle Minimized"
) else (
    echo [JobPulse] El dashboard ya estaba corriendo en 8501; se reutiliza.
)

echo [JobPulse] Esperando a que el dashboard responda (hasta 2 min)...
powershell -NoProfile -Command "$ok=$false; $d=(Get-Date).AddSeconds(120); while((Get-Date) -lt $d){ try{ $r=Invoke-WebRequest 'http://localhost:8501' -UseBasicParsing -TimeoutSec 3; if($r.StatusCode -eq 200){ $ok=$true; break } }catch{}; Start-Sleep -Seconds 2 }; if($ok){ exit 0 } else { exit 1 }"
if errorlevel 1 (
    echo [JobPulse] AVISO: el dashboard no respondio. Mira data\streamlit_err.log
) else (
    echo [JobPulse] Dashboard listo: abriendo el navegador.
    start "" http://localhost:8501
)

rem ------------------------------------------------------------------
rem 2/3  Scraping. El progreso se publica en data\corrida_estado.json,
rem      asi que la pestana "Corrida" del dashboard lo muestra en vivo
rem      (fase actual, grupo boards vs portales, vacantes encontradas).
rem      Si ya hay una corrida viva, no se lanza otra.
rem      Paso extra de argumentos: jobpulse.cmd --preflight
rem ------------------------------------------------------------------
echo.
echo [JobPulse] 2/3 Scraping (seguilo en la pestana Corrida del dashboard)...
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
echo [JobPulse] 3/3 Listo. El dashboard sigue abierto; cierra esta ventana
echo            cuando quieras (no afecta al dashboard).
rem `timeout` necesita stdin interativo; si falla, esperamos con ping.
timeout /t 15 /nobreak >nul 2>nul
if errorlevel 1 ping -n 16 127.0.0.1 >nul
exit /b 0
