# Instala (una sola vez) el recordatorio de JobPulse en el inicio de sesion.
# Ejecutar:  powershell -File instalar_recordatorio.ps1
#
# No necesita admin: la tarea se registra para el usuario actual. El
# recordatorio solo pregunta si no has corrido el scraping hoy.

$ErrorActionPreference = "Stop"
$base = Split-Path -Parent $MyInvocation.MyCommand.Path
$pyw = Join-Path $base ".venv\Scripts\pythonw.exe"

if (-not (Test-Path $pyw)) {
    Write-Host "No encontre $pyw"
    Write-Host "Crea el venv primero:  python -m venv .venv  +  pip install -r requirements.txt"
    exit 1
}

$action = New-ScheduledTaskAction -Execute $pyw `
    -Argument "`"$base\recordatorio.py`"" -WorkingDirectory $base

$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2)

Register-ScheduledTask -TaskName "JobPulse Recordatorio" `
    -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null

Write-Host "Listo: 'JobPulse Recordatorio' se abrira al iniciar sesion."
Write-Host "Solo pregunta si no has corrido el scraping hoy."
Write-Host "Prueba inmediata:  python recordatorio.py --forzar"
