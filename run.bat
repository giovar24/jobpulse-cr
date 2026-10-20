@echo off
rem ------------------------------------------------------------------
rem  Alias de compatibilidad: el launcher real es jobpulse.cmd
rem  (1/3 dashboard primero -> 2/3 scraping -> 3/3 listo).
rem
rem  Antes run.bat solo corria `python main.py`, asi que NUNCA levantaba
rem  el dashboard y http://localhost:8501 quedaba sin servidor (el
rem  "error de bloqueo" en la UI). Ahora delega para no duplicar logica.
rem ------------------------------------------------------------------
cd /d "%~dp0"
call jobpulse.cmd %*
exit /b %errorlevel%
