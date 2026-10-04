#Requires -Version 5.1
<#
.SYNOPSIS
    Abre un muro de vídeo a pantalla completa (modo kiosco) en cada monitor y lo mantiene abierto.

.DESCRIPTION
    Se ejecuta en la sesión del usuario del PC de control (lo lanza la tarea programada que crea
    deploy\windows\install-kiosk.ps1 al iniciar sesión). Hace lo siguiente:
      1. Detecta los monitores (System.Windows.Forms.Screen), ordenados de izquierda a derecha y
         de arriba abajo: el primero muestra /wall/1, el segundo /wall/2... (máximo 4).
      2. Evita que la pantalla se apague o el equipo se suspenda mientras el kiosco está abierto.
      3. Espera a que el backend responda en /api/health.
      4. Abre una ventana de Edge (o Chrome) en modo kiosco por monitor, cada una con su propio
         perfil (--user-data-dir) y en la posición de su pantalla.
      5. Vigila las ventanas: si una se cierra o el navegador se cae, la vuelve a abrir.
      6. Cada -RefreshHours horas (6 por defecto) reabre cada muro, de uno en uno y con 20 s de
         separación, para renovar la sesión de kiosco antes de que caduque (VMS_SESSION_HOURS,
         12 h por defecto). Pon un valor menor que VMS_SESSION_HOURS; 0 lo desactiva.

    El token del kiosco se lee de un archivo protegido (solo lo puede leer este usuario). El
    backend solo lo acepta desde el propio PC (127.0.0.1).

    Para salir del kiosco en mantenimiento: .\start-kiosk.ps1 -Stop

.EXAMPLE
    .\start-kiosk.ps1
.EXAMPLE
    .\start-kiosk.ps1 -Monitors 2 -Browser Chrome
.EXAMPLE
    .\start-kiosk.ps1 -Stop
#>
[Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSReviewUnusedParameter', '',
    Justification = 'Los parámetros se usan dentro de las funciones del script')]
[CmdletBinding()]
param(
    [string]$BaseUrl = 'http://127.0.0.1:8600',
    [string]$TokenFile = (Join-Path $env:ProgramData 'VMSMultimarca\kiosk\kiosk.token'),
    [ValidateSet('Auto', 'Edge', 'Chrome')]
    [string]$Browser = 'Auto',
    [ValidateRange(0, 4)]
    [int]$Monitors = 0,
    [string]$ProfileRoot = (Join-Path $env:LOCALAPPDATA 'VMSMultimarca\kiosk-profile'),
    [int]$CheckSeconds = 5,
    [ValidateRange(0, 720)]
    [double]$RefreshHours = 6,
    [switch]$NoWatchdog,
    [switch]$Stop
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$StateDir = Join-Path $env:LOCALAPPDATA 'VMSMultimarca'
$LogFile = Join-Path $StateDir 'kiosk.log'
$PidFile = Join-Path $StateDir 'kiosk.pid'
New-Item -ItemType Directory -Force -Path $StateDir | Out-Null

function Write-KioskLog([string]$Text) {
    $line = '{0} {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Text
    Write-Host $line
    try {
        if ((Test-Path -LiteralPath $LogFile) -and (Get-Item -LiteralPath $LogFile).Length -gt 2MB) {
            Move-Item -LiteralPath $LogFile -Destination "$LogFile.1" -Force
        }
        Add-Content -LiteralPath $LogFile -Value $line -Encoding UTF8
    } catch {
        Write-Host "No se pudo escribir el registro del kiosco: $($_.Exception.Message)"
    }
}

function Stop-KioskBrowser {
    $procs = @(Get-CimInstance Win32_Process -Filter "Name = 'msedge.exe' OR Name = 'chrome.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine.Contains($ProfileRoot) })
    foreach ($p in $procs) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
    return $procs.Count
}

# --------------------------------------------------------------------------- -Stop
if ($Stop) {
    if (Test-Path -LiteralPath $PidFile) {
        $old = Get-Content -LiteralPath $PidFile -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($old -and (Get-Process -Id ([int]$old) -ErrorAction SilentlyContinue)) {
            Stop-Process -Id ([int]$old) -Force
        }
        Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
    }
    $n = Stop-KioskBrowser
    Write-KioskLog "Kiosco detenido ($n procesos de navegador cerrados)"
    return
}

# --------------------------------------------------------------------------- Win32
Add-Type -AssemblyName System.Windows.Forms
if (-not ('VmsKiosk.Native' -as [type])) {
    Add-Type -Namespace VmsKiosk -Name Native -MemberDefinition @'
[DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
[DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr hWnd, IntPtr after, int x, int y, int cx, int cy, uint flags);
[DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int cmd);
[DllImport("kernel32.dll")] public static extern uint SetThreadExecutionState(uint flags);
'@
}
# Coordenadas reales en píxeles aunque Windows tenga escalado (125 %, 150 %...)
[void][VmsKiosk.Native]::SetProcessDPIAware()

# ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED: ni suspensión ni pantalla apagada
$ES_FLAGS = [uint32]2147483651   # 0x80000003

function Disable-ScreenSleep {
    [void][VmsKiosk.Native]::SetThreadExecutionState($ES_FLAGS)
    try {
        Set-ItemProperty -Path 'HKCU:\Control Panel\Desktop' -Name ScreenSaveActive -Value '0'
        & powercfg.exe /change monitor-timeout-ac 0 | Out-Null
        & powercfg.exe /change standby-timeout-ac 0 | Out-Null
    } catch {
        Write-KioskLog "AVISO: no se pudo desactivar el ahorro de energía: $($_.Exception.Message)"
    }
}

function Find-Browser {
    $edge = @("${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe",
              "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe")
    $chrome = @("$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
                "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
                "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe")
    $order = switch ($Browser) { 'Edge' { $edge } 'Chrome' { $chrome } default { $edge + $chrome } }
    foreach ($p in $order) { if ($p -and (Test-Path -LiteralPath $p)) { return $p } }
    throw "No se encontró el navegador ($Browser). Instala Microsoft Edge o Google Chrome."
}

function Get-OrderedScreen {
    $all = @([System.Windows.Forms.Screen]::AllScreens | Sort-Object { $_.Bounds.X }, { $_.Bounds.Y })
    $count = $all.Count
    if ($Monitors -gt 0) { $count = [Math]::Min($Monitors, $count) }
    return @($all | Select-Object -First ([Math]::Min($count, 4)))
}

function Wait-Backend {
    $delay = 2
    while ($true) {
        try {
            $h = Invoke-RestMethod -Uri "$BaseUrl/api/health" -TimeoutSec 3
            if ($h.status) { return }
        } catch {
            Write-KioskLog "Esperando al backend en $BaseUrl ... ($($_.Exception.Message))"
        }
        Start-Sleep -Seconds $delay
        $delay = [Math]::Min($delay * 2, 30)
    }
}

function Get-KioskUrl([int]$Wall) {
    if (-not (Test-Path -LiteralPath $TokenFile)) {
        throw "No existe el token del kiosco en $TokenFile. Ejecuta deploy\windows\install-kiosk.ps1 como administrador."
    }
    $token = (Get-Content -LiteralPath $TokenFile -Raw).Trim()
    $t = [Uri]::EscapeDataString($token)
    $next = [Uri]::EscapeDataString("/wall/$Wall")
    return "$BaseUrl/api/auth/kiosk?token=$t&next=$next"
}

function Start-Wall {
    param([int]$Wall, [System.Windows.Forms.Screen]$Screen, [string]$Exe)
    $b = $Screen.Bounds
    $profileDir = Join-Path $ProfileRoot "monitor-$Wall"
    New-Item -ItemType Directory -Force -Path $profileDir | Out-Null
    $browserArgs = @(
        "--user-data-dir=`"$profileDir`"",
        "--window-position=$($b.X),$($b.Y)",
        "--window-size=$($b.Width),$($b.Height)",
        '--no-first-run', '--no-default-browser-check', '--disable-session-crashed-bubble',
        '--hide-crash-restore-bubble', '--noerrdialogs', '--disable-infobars',
        '--autoplay-policy=no-user-gesture-required', '--disable-features=Translate',
        '--kiosk'
    )
    if ($Exe -like '*msedge.exe') { $browserArgs += '--edge-kiosk-type=fullscreen' }
    $browserArgs += "`"$(Get-KioskUrl $Wall)`""
    $proc = Start-Process -FilePath $Exe -ArgumentList $browserArgs -PassThru
    Write-KioskLog "Muro $Wall abierto en la pantalla $($Screen.DeviceName) ($($b.Width)x$($b.Height) en $($b.X),$($b.Y)); PID $($proc.Id)"
    return $proc
}

function Confirm-WallPosition {
    <# Si el navegador abrió la ventana en otra pantalla, la mueve a la suya. #>
    param([System.Diagnostics.Process]$Proc, [System.Windows.Forms.Screen]$Screen, [int]$Wall)
    for ($i = 0; $i -lt 20; $i++) {
        $Proc.Refresh()
        if ($Proc.HasExited) { return }
        if ($Proc.MainWindowHandle -ne [IntPtr]::Zero) { break }
        Start-Sleep -Milliseconds 500
    }
    if ($Proc.HasExited -or $Proc.MainWindowHandle -eq [IntPtr]::Zero) { return }
    $actual = [System.Windows.Forms.Screen]::FromHandle($Proc.MainWindowHandle)
    if ($actual.DeviceName -ne $Screen.DeviceName) {
        $b = $Screen.Bounds
        Write-KioskLog "AVISO: el muro $Wall apareció en $($actual.DeviceName); se mueve a $($Screen.DeviceName)"
        [void][VmsKiosk.Native]::ShowWindow($Proc.MainWindowHandle, 9)      # SW_RESTORE
        [void][VmsKiosk.Native]::SetWindowPos($Proc.MainWindowHandle, [IntPtr]::Zero, $b.X, $b.Y, $b.Width, $b.Height, 0x0040)
        [void][VmsKiosk.Native]::ShowWindow($Proc.MainWindowHandle, 3)      # SW_MAXIMIZE
    }
}

# --------------------------------------------------------------------------- inicio
if (Test-Path -LiteralPath $PidFile) {
    $old = Get-Content -LiteralPath $PidFile -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($old -and [int]$old -ne $PID -and (Get-Process -Id ([int]$old) -ErrorAction SilentlyContinue)) {
        Write-KioskLog "Ya hay un kiosco en marcha (PID $old). Usa -Stop para cerrarlo."
        return
    }
}
Set-Content -LiteralPath $PidFile -Value $PID
try {
    $exe = Find-Browser
    $screens = Get-OrderedScreen
    Write-KioskLog "Kiosco: $($screens.Count) monitores, navegador $exe"
    Disable-ScreenSleep
    Wait-Backend
    [void](Stop-KioskBrowser)   # ventanas que hubieran quedado de una ejecución anterior

    $walls = @{}
    for ($i = 0; $i -lt $screens.Count; $i++) {
        $n = $i + 1
        $walls[$n] = @{ Screen = $screens[$i]; Proc = (Start-Wall $n $screens[$i] $exe); Restarts = 0
                        OpenedAt = (Get-Date) }
        Confirm-WallPosition $walls[$n].Proc $screens[$i] $n
    }
    if ($NoWatchdog) { return }

    while ($true) {
        Start-Sleep -Seconds $CheckSeconds
        [void][VmsKiosk.Native]::SetThreadExecutionState($ES_FLAGS)
        foreach ($n in @($walls.Keys)) {
            $w = $walls[$n]
            if ($w.Proc.HasExited) {
                $w.Restarts++
                $wait = [Math]::Min(60, [Math]::Pow(2, [Math]::Min($w.Restarts, 6)))
                Write-KioskLog "El muro $n se cerró (código $($w.Proc.ExitCode)); se reabre en $wait s"
                Start-Sleep -Seconds $wait
                Wait-Backend
                $w.Proc = Start-Wall $n $w.Screen $exe
                $w.OpenedAt = Get-Date
                Confirm-WallPosition $w.Proc $w.Screen $n
            } elseif ($RefreshHours -gt 0 -and
                      ((Get-Date) - $w.OpenedAt).TotalSeconds -ge ($RefreshHours * 3600 + ($n - 1) * 20)) {
                Write-KioskLog "Renovando la sesión del muro $n (cada $RefreshHours h)"
                Stop-Process -Id $w.Proc.Id -Force -ErrorAction SilentlyContinue
                Wait-Backend
                $w.Proc = Start-Wall $n $w.Screen $exe
                $w.OpenedAt = Get-Date
                Confirm-WallPosition $w.Proc $w.Screen $n
            } elseif ($w.Restarts -gt 0 -and ((Get-Date) - $w.OpenedAt).TotalMinutes -gt 10) {
                $w.Restarts = 0
            }
        }
    }
} catch {
    Write-KioskLog "ERROR del kiosco: $($_.Exception.Message)"
    throw
} finally {
    Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
}
