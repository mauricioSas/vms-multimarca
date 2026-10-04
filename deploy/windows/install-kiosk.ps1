#Requires -Version 5.1
#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Prepara el PC de control para abrir los muros de vídeo en los 4 monitores al iniciar sesión.

.DESCRIPTION
      1. Copia el token del kiosco (VMS_KIOSK_TOKEN del .env) a un archivo que SOLO puede leer
         el usuario del kiosco (además de SYSTEM y Administradores).
      2. Crea la tarea programada «VMS Multimarca - Kiosco» que, al iniciar sesión ese usuario,
         ejecuta deploy\kiosk\start-kiosk.ps1 (una ventana en modo kiosco por monitor y
         vigilancia para reabrirlas si se cierran).
      3. Desactiva el apagado de pantalla y la suspensión con corriente (plan de energía activo).
      4. Opcional (por defecto sí): directivas de Edge para que no muestre la bienvenida y permita
         reproducir vídeo sin interacción. Usa -SkipEdgePolicies para no tocarlas.

    El inicio de sesión automático de Windows no se activa aquí: si lo quieres, configúralo con
    las herramientas de Windows (ver docs\INSTALACION-WINDOWS.md).

.EXAMPLE
    .\install-kiosk.ps1 -KioskUser 'PC-CONTROL\muros'
#>
[CmdletBinding()]
param(
    [string]$KioskUser = "$env:USERDOMAIN\$env:USERNAME",
    [string]$InstallDir = (Join-Path $env:ProgramFiles 'VMSMultimarca'),
    [string]$DataDir = (Join-Path $env:ProgramData 'VMSMultimarca'),
    [int]$HttpPort = 8600,
    [ValidateSet('Auto', 'Edge', 'Chrome')]
    [string]$Browser = 'Auto',
    [ValidateRange(0, 4)]
    [int]$Monitors = 0,
    [switch]$SkipEdgePolicies,
    [switch]$Remove
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$TaskName = 'VMS Multimarca - Kiosco'

function Write-Ok([string]$Text) { Write-Host "    OK  $Text" -ForegroundColor Green }

if ($Remove) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Ok "Tarea «$TaskName» eliminada"
    return
}

# --------------------------------------------------------------------------- 1. token
$envFile = Join-Path $DataDir '.env'
$token = ''
if (Test-Path -LiteralPath $envFile) {
    foreach ($line in Get-Content -LiteralPath $envFile -Encoding UTF8) {
        if ($line -match '^\s*VMS_KIOSK_TOKEN\s*=(.*)$') { $token = $Matches[1].Trim() }
    }
}
if (-not $token) { throw "VMS_KIOSK_TOKEN está vacío en $envFile. Ejecuta antes install.ps1." }
try {
    $sid = (New-Object System.Security.Principal.NTAccount($KioskUser)).Translate([System.Security.Principal.SecurityIdentifier]).Value
} catch {
    throw "No existe el usuario «$KioskUser». Indica -KioskUser 'EQUIPO\usuario'."
}
$kioskDir = Join-Path $DataDir 'kiosk'
New-Item -ItemType Directory -Force -Path $kioskDir | Out-Null
$tokenFile = Join-Path $kioskDir 'kiosk.token'
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($tokenFile, $token, $utf8NoBom)
& icacls.exe $kioskDir /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' "*$($sid):(OI)(CI)R" | Out-Null
if ($LASTEXITCODE -ne 0) { throw "icacls falló al proteger $kioskDir" }
& icacls.exe $tokenFile /inheritance:r /grant:r '*S-1-5-18:F' '*S-1-5-32-544:F' "*$($sid):R" | Out-Null
if ($LASTEXITCODE -ne 0) { throw "icacls falló al proteger $tokenFile" }
Write-Ok "Token del kiosco en $tokenFile (solo lectura para $KioskUser)"

# --------------------------------------------------------------------------- 2. tarea programada
$script = Join-Path $InstallDir 'deploy\kiosk\start-kiosk.ps1'
if (-not (Test-Path -LiteralPath $script)) { throw "No existe $script. ¿Está instalado VMS Multimarca en $InstallDir?" }
$argList = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$script`" -BaseUrl http://127.0.0.1:$HttpPort " +
           "-TokenFile `"$tokenFile`" -Browser $Browser -Monitors $Monitors"
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $argList
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $KioskUser
$principal = New-ScheduledTaskPrincipal -UserId $KioskUser -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings `
    -Description 'Abre los muros de vídeo de VMS Multimarca en todos los monitores' -Force | Out-Null
Write-Ok "Tarea «$TaskName» creada para $KioskUser (al iniciar sesión)"

# --------------------------------------------------------------------------- 3. energía
& powercfg.exe /change monitor-timeout-ac 0 | Out-Null
& powercfg.exe /change standby-timeout-ac 0 | Out-Null
& powercfg.exe /change hibernate-timeout-ac 0 | Out-Null
Write-Ok 'Pantalla y suspensión: nunca (con corriente)'

# --------------------------------------------------------------------------- 4. directivas de Edge
if (-not $SkipEdgePolicies) {
    $key = 'HKLM:\SOFTWARE\Policies\Microsoft\Edge'
    New-Item -Path $key -Force | Out-Null
    New-ItemProperty -Path $key -Name HideFirstRunExperience -Value 1 -PropertyType DWord -Force | Out-Null
    New-ItemProperty -Path $key -Name AutoplayAllowed -Value 1 -PropertyType DWord -Force | Out-Null
    New-ItemProperty -Path $key -Name TranslateEnabled -Value 0 -PropertyType DWord -Force | Out-Null
    Write-Ok 'Directivas de Edge: sin pantalla de bienvenida, vídeo sin interacción, sin traductor'
}

Write-Host ''
Write-Host "Listo. Cierra sesión y vuelve a entrar como $KioskUser para ver los muros, o ejecuta ahora:" -ForegroundColor Green
Write-Host "  Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "Para salir del kiosco en mantenimiento: powershell -File `"$script`" -Stop"
