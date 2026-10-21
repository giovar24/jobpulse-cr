@echo off
rem ------------------------------------------------------------------
rem  Alias de compatibilidad: el launcher real es jobpulse.cmd
rem  (solo abre la UI; el scraping se lanza desde la pestana "Corrida"
rem  o con: jobpulse.cmd --correr).
rem
rem  Antes run.bat solo corria `python main.py`, asi que NUNCA levantaba
rem  la UI. Ahora delega para no duplicar logica.
rem ------------------------------------------------------------------
cd /d "%~dp0"
call jobpulse.cmd %*
exit /b %errorlevel%
