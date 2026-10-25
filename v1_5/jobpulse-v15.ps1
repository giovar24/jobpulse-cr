<# 
.SYNOPSIS
    JobPulse CR v1.5 - Launcher (PowerShell)
    Inicia Brave con CDP (puerto 9222) y lanza la UI
#>

# ----------------------------------------------------------
# Configuración
# ----------------------------------------------------------
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Definition
# Rutas genéricas (nada del usuario hardcodeado):
# - Brave: preferir instalación en Program Files; fallback a LOCALAPPDATA.
# - user-data-dir SIEMPRE la raíz "User Data" — apuntar a "User Data\Default"
#   crea un perfil NUEVO y vacío y todos los logins fallan (bug histórico,
#   documentado en docs/SITIOS_PROBLEMATICOS.md).
$BravePath = @(
    "$env:ProgramFiles\BraveSoftware\Brave-Browser\Application\brave.exe",
    "$env:LOCALAPPDATA\BraveSoftware\Brave-Browser\Application\brave.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1
$BraveUserDataDir = "$env:LOCALAPPDATA\BraveSoftware\Brave-Browser\User Data"
$CdpPort = 9222
$ProjectRoot = Resolve-Path $ProjectRoot

Write-Host "`n============================================================" -ForegroundColor Cyan
Write-Host "  JobPulse CR v1.5 - Launcher (PowerShell)" -ForegroundColor Cyan
Write-Host "============================================================`n" -ForegroundColor Cyan

Set-Location $ProjectRoot

# ----------------------------------------------------------
# 1. Verificar/Iniciar Brave con CDP
# ----------------------------------------------------------
Write-Host "[1/3] Verificando Brave CDP (puerto $CdpPort)..." -ForegroundColor Yellow

try {
    $response = Invoke-WebRequest -Uri "http://127.0.0.1:$CdpPort/json/version" -Method Get -TimeoutSec 2 -ErrorAction Stop
    Write-Host "    Brave CDP ya está corriendo en puerto $CdpPort ✓" -ForegroundColor Green
} catch {
    Write-Host "    Iniciando Brave con CDP..." -ForegroundColor Yellow
    
    if (Test-Path $BravePath) {
        $braveArgs = @(
            "--remote-debugging-port=$CdpPort"
            "--user-data-dir=$BraveUserDataDir"
        )
        Start-Process -FilePath $BravePath -ArgumentList $braveArgs -WindowStyle Normal
        
        Write-Host "    Esperando a que Brave inicie (3 seg)..." -ForegroundColor Yellow
        Start-Sleep -Seconds 3
        
        try {
            $response = Invoke-WebRequest -Uri "http://127.0.0.1:$CdpPort/json/version" -Method Get -TimeoutSec 5 -ErrorAction Stop
            Write-Host "    Brave CDP iniciado correctamente ✓" -ForegroundColor Green
        } catch {
            Write-Warning "    ADVERTENCIA: Brave CDP no respondió. Verifica que Brave se abrió correctamente."
        }
    } else {
        Write-Error "    No se encontró Brave en: $BravePath"
        Write-Host "    Edita la variable `$BravePath` en este script si Brave está en otra ubicación." -ForegroundColor Yellow
    }
}

# ----------------------------------------------------------
# 2. Verificar/Crear entorno virtual
# ----------------------------------------------------------
Write-Host "`n[2/3] Verificando entorno Python..." -ForegroundColor Yellow

if (-not (Test-Path ".venv")) {
    Write-Host "    Creando entorno virtual..." -ForegroundColor Yellow
    python -m venv .venv
    Write-Host "    Instalando dependencias..." -ForegroundColor Yellow
    & ".venv\Scripts\pip" install -e . 2>$null | Out-Null
    & ".venv\Scripts\playwright" install chromium 2>$null | Out-Null
    Write-Host "    Entorno listo ✓" -ForegroundColor Green
} else {
    Write-Host "    Entorno virtual existe ✓" -ForegroundColor Green
}

# ----------------------------------------------------------
# 3. Lanzar UI
# ----------------------------------------------------------
Write-Host "`n[3/3] Lanzando JobPulse CR UI..." -ForegroundColor Yellow
Write-Host "`n============================================================" -ForegroundColor Cyan
Write-Host "  JobPulse CR v1.5 - UI abierta" -ForegroundColor Cyan
Write-Host "  - Configuración: botón 'Configuración' (esquina sup. der.)" -ForegroundColor Gray
Write-Host "  - Fuentes: botón 'Administrar fuentes'" -ForegroundColor Gray
Write-Host "  - Perfil: botón 'Mi perfil'" -ForegroundColor Gray
Write-Host "  - Cerrar la ventana UI para salir" -ForegroundColor Gray
Write-Host "============================================================`n" -ForegroundColor Cyan

# Usar python del venv si existe
$pythonExe = if (Test-Path ".venv\Scripts\python.exe") { ".venv\Scripts\python.exe" } else { "python" }
& $pythonExe -m jobpulse_v15.ui

Write-Host "`nJobPulse CR finalizado." -ForegroundColor Cyan
Read-Host "Presiona Enter para salir"