@echo off
REM ============================================================
REM JobPulse CR v1.5 - Launcher (Windows)
REM ============================================================

cd /d "%~dp0"

echo.
echo ============================================================
echo  JobPulse CR v1.5 - Iniciando...
echo ============================================================
echo.

REM ----------------------------------------------------------
REM 1. Iniciar Brave con CDP
REM ----------------------------------------------------------
echo [1/3] Iniciando Brave con CDP (puerto 9222)...

rem Verificar si Brave ya responde en CDP
powershell -Command "Invoke-WebRequest -Uri 'http://127.0.0.1:9222/json/version' -TimeoutSec 2 -ErrorAction SilentlyContinue" >nul 2>&1
if %errorlevel% equ 0 (
    echo     Brave CDP ya está corriendo en puerto 9222 ✓
) else (
    echo     Iniciando Brave...
    start "Brave CDP" /B "" "C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe" --remote-debugging-port=9222 --user-data-dir="C:\Users\giova\AppData\Local\BraveSoftware\Brave-Browser\User Data"
    echo     Esperando 3 segundos...
    timeout /t 3 >nul
    echo     Brave iniciado (verifica que se abrió correctamente)
)

REM ----------------------------------------------------------
REM 2. Entorno virtual
REM ----------------------------------------------------------
echo [2/3] Verificando entorno Python...
if exist ".venv\Scripts\python.exe" (
    echo     Entorno virtual existe ✓
    set "PYTHON=.venv\Scripts\python.exe"
) else (
    echo     Creando entorno virtual...
    python -m venv .venv >nul 2>&1
    echo     Instalando dependencias...
    .venv\Scripts\pip install -e . >nul 2>&1
    .venv\Scripts\playwright install chromium >nul 2>&1
    set "PYTHON=.venv\Scripts\python.exe"
    echo     Entorno listo ✓
)

REM ----------------------------------------------------------
REM 3. Lanzar UI
REM ----------------------------------------------------------
echo [3/3] Lanzando JobPulse CR UI...
echo.
echo ============================================================
echo  JobPulse CR v1.5 - UI abierta
echo  - Configuración: botón en esquina superior derecha
echo  - Fuentes: botón "Administrar fuentes"
echo  - Perfil: botón "Mi perfil"
echo ============================================================
echo.

"%PYTHON%" -m jobpulse_v15.ui

echo.
echo JobPulse CR finalizado.
pause