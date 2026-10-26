@echo off
REM ============================================================
REM JobPulse CR v1.5 - Instalador de IA local (equipo nuevo)
REM Delega en instalar_modelo_ia.py:
REM   1) huggingface_hub (pip si falta)
REM   2) Modelo Gemma 4 E2B QAT desde Unsloth (Hugging Face)
REM   3) llama.cpp build Vulkan (si llama-server.exe no existe)
REM Uso: instalar-modelo-ia.bat           (instala lo que falte)
REM      instalar-modelo-ia.bat --check   (solo verifica)
REM ============================================================
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
    set "PYTHON=.venv\Scripts\python.exe"
) else (
    set "PYTHON=python"
)

"%PYTHON%" instalar_modelo_ia.py %*

REM Sin argumentos (uso interactivo): dejar la ventana abierta.
if "%~1"=="" pause
