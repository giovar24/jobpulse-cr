@echo off
rem ------------------------------------------------------------------
rem  Alias de compatibilidad: el launcher real es jobpulse.cmd
rem  (1/3 UI de escritorio primero -> 2/3 scraping -> 3/3 listo).
rem
rem  Antes run.bat solo corria `python main.py`, asi que NUNCA levantaba
rem  la UI. Ahora delega para no duplicar logica.
rem ------------------------------------------------------------------
cd /d "%~dp0"
call jobpulse.cmd %*
exit /b %errorlevel%
