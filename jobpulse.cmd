@echo off
chcp 65001 >nul
title JobPulse CR
cd /d "%~dp0"

echo [JobPulse] 1/2 Scrapeando vacantes (main.py)...
".venv\Scripts\python.exe" main.py

echo.
echo [JobPulse] 2/2 Dashboard (http://localhost:8501)
powershell -NoProfile -Command "if (Get-NetTCPConnection -LocalPort 8501 -State Listen -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }"
if errorlevel 1 (
    echo [JobPulse] Levantando Streamlit...
    start "JobPulse Dashboard" /min ".venv\Scripts\python.exe" -m streamlit run app.py --server.headless true --server.port 8501
    timeout /t 6 /nobreak >nul
) else (
    echo [JobPulse] Dashboard ya estaba corriendo; se reutiliza.
)
start "" http://localhost:8501
echo.
echo [JobPulse] Corrida completa. Esta ventana se cierra sola en 15 s...
timeout /t 15 /nobreak >nul
