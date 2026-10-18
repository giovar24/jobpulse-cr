@echo off
echo Starting JobPulse CR...
set PYTHONPATH=%~dp0
.\.venv\Scripts\python.exe main.py
pause