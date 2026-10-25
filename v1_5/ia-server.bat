@echo off
REM ============================================================
REM JobPulse CR v1.5 - Servidor IA local (puerto 8081)
REM Delega en start_ia_server.py (fuente unica del comando):
REM   - Si ya responde /health: no hace nada.
REM   - Si esta apagado: arranca Gemma 4 E2B QAT (8k ctx, MTP)
REM     en proceso separado, log en data\ia_server.log.
REM Uso: ia-server.bat            (verifica y arranca si hace falta)
REM      ia-server.bat --check    (solo verifica, no arranca)
REM ============================================================
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
    set "PYTHON=.venv\Scripts\python.exe"
) else (
    set "PYTHON=python"
)

"%PYTHON%" start_ia_server.py %*

REM Sin argumentos (uso interactivo): dejar la ventana abierta.
if "%~1"=="" pause
