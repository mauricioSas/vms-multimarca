#Requires -Version 5.1
#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Desinstala VMS Multimarca de este PC.

.DESCRIPTION
    - Para y elimina los servicios VMSBackend, VMSAnalytics, VMSHeartbeat y VMSCentral.
    - Elimina las reglas del Firewall del grupo «VMS Multimarca».
    - Elimina la tarea programada del kiosco y su arranque automático.
    - Borra la carpeta de instalación.
    - La carpeta de datos (configuración, contraseñas cifradas, GRABACIONES y registros) se
      CONSERVA salvo que indiques -RemoveData. Borrarla no se puede deshacer.

.EXAMPLE
    .\uninstall.ps1
    Desinstala el programa y conserva los datos y las grabaciones.

.EXAMPLE
    .\uninstall.ps1 -RemoveData
    Desinstala y, tras pedir confirmación, borra también datos y grabaciones.
#>
[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'High')]
param(
    [string]$InstallDir = (Join-Path $env:ProgramFiles 'VMSMultimarca'),
    [string]$DataDir = (Join-Path $env:ProgramData 'VMSMultimarca'),
    [switch]$RemoveData,
    [switch]$Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Write-Step([string]$Text) { Write-Host ''; Write-Host "==> $Text" -ForegroundColor Cyan }
function Write-Ok([string]$Text) { Write-Host "    OK  $Text" -ForegroundColor Green }
function Write-Warn([string]$Text) { Write-Host "    AVISO  $Text" -ForegroundColor Yellow }

Write-Step 'Servicios'
foreach ($id in @('VMSAnalytics', 'VMSHeartbeat', 'VMSCentral', 'VMSBackend')) {
    $svc = Get-Service -Name $id -ErrorAction SilentlyContinue
    if (-not $svc) { continue }
    if ($svc.Status -ne 'Stopped') {
        Stop-Service -Name $id -Force -ErrorAction SilentlyContinue
        $svc.WaitForStatus('Stopped', [TimeSpan]::FromSeconds(45))
    }
    $wrapper = Join-Path $InstallDir "services\$id.exe"
    if (Test-Path -LiteralPath $wrapper) {
        & $wrapper uninstall | Out-Null
    } else {
        & sc.exe delete $id | Out-Null
    }
    if ($LASTEXITCODE -ne 0) { Write-Warn "No se pudo eliminar el servicio $id (código $LASTEXITCODE)" }
    else { Write-Ok "Servicio $id eliminado" }
}
# Por si quedara algún MediaMTX huérfano de esta instalación
Get-Process -Name mediamtx -ErrorAction SilentlyContinue |
    Where-Object { $_.Path -and $_.Path.StartsWith($InstallDir, [StringComparison]::OrdinalIgnoreCase) } |
    Stop-Process -Force

Write-Step 'Firewall'
$rules = @(Get-NetFirewallRule -Group 'VMS Multimarca' -ErrorAction SilentlyContinue)
$rules | Remove-NetFirewallRule
Write-Ok "$($rules.Count) reglas eliminadas"

Write-Step 'Tareas programadas (kiosco e informe semanal)'
foreach ($task in @(Get-ScheduledTask -TaskName 'VMS Multimarca - *' -ErrorAction SilentlyContinue)) {
    Unregister-ScheduledTask -TaskName $task.TaskName -Confirm:$false
    Write-Ok "Tarea «$($task.TaskName)» eliminada"
}
Get-CimInstance Win32_Process -Filter "Name = 'msedge.exe' OR Name = 'chrome.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and $_.CommandLine -like '*VMSMultimarca*kiosk-profile*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

Write-Step "Carpeta de instalación $InstallDir"
if (Test-Path -LiteralPath $InstallDir) {
    Remove-Item -LiteralPath $InstallDir -Recurse -Force
    Write-Ok 'Eliminada'
}

if ($RemoveData) {
    Write-Step "Carpeta de datos $DataDir"
    $msg = "Se borrarán la configuración, las contraseñas guardadas, los registros y TODAS LAS GRABACIONES de $DataDir"
    if ($Force -or $PSCmdlet.ShouldProcess($DataDir, $msg)) {
        if (Test-Path -LiteralPath $DataDir) {
            Remove-Item -LiteralPath $DataDir -Recurse -Force
            Write-Ok 'Datos eliminados'
        }
    } else {
        Write-Warn 'Datos conservados'
    }
} else {
    Write-Host ''
    Write-Host "Los datos y las grabaciones se conservan en $DataDir (usa -RemoveData para borrarlos)."
}
Write-Host 'Desinstalación terminada.' -ForegroundColor Green
