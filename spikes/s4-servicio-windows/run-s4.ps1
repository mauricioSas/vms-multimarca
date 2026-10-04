#Requires -Version 5.1
<#
.SYNOPSIS
  Prueba S4 de punta a punta en Windows (la ejecuta CI en windows-latest, que corre como administrador).

.DESCRIPTION
  1. Monta C:\vms-s4 con bin\vmshost-s4.exe y versions\1.0.0, 1.1.0 (buenas) y 2.0.0 (rota).
  2. Crea el servicio VMSS4Hello con la cuenta virtual NT SERVICE\VMSS4Hello y ACL por SID.
  3. Instala → arranca → consulta → comprueba: el hijo corre con la cuenta virtual, está dentro del
     Job Object, no puede escribir en bin\ y muere si se mata al arrancador.
  4. Versión rota a prueba → 3 caídas → vuelta atrás sola.
  5. Puntero corrupto → reconstruido desde el diario.
  6. Versión buena sin confirmar → vuelta atrás por tiempo; confirmada → se queda.
  7. Para y desinstala. Escribe s4-result.json y sale con 1 si algo falla.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)] [string] $BuildDir,
    [string] $Root = 'C:\vms-s4',
    [string] $ServiceName = 'VMSS4Hello',
    [string] $Out = 's4-result.json'
)
$ErrorActionPreference = 'Stop'
$results = [ordered]@{}
$failed = $false

function Step([string] $name, [scriptblock] $body) {
    Write-Host "== $name"
    try {
        $detail = & $body
        $script:results[$name] = [ordered]@{ ok = $true; detail = $detail }
        Write-Host "   OK: $detail"
    } catch {
        $script:results[$name] = [ordered]@{ ok = $false; detail = "$($_.Exception.Message)" }
        $script:failed = $true
        Write-Host "   FALLO: $($_.Exception.Message)" -ForegroundColor Red
    }
}

function Wait-Until([scriptblock] $cond, [int] $seconds, [string] $what) {
    $end = (Get-Date).AddSeconds($seconds)
    while ((Get-Date) -lt $end) {
        try { if (& $cond) { return } } catch { }
        Start-Sleep -Milliseconds 500
    }
    throw "tiempo agotado ($seconds s) esperando: $what"
}

function Read-Json([string] $path) { Get-Content -Raw -LiteralPath $path | ConvertFrom-Json }
function Heartbeat { Read-Json (Join-Path $Root 'state\heartbeat.json') }
function Pointer { Read-Json (Join-Path $Root 'state\active.json') }
function HostStatus { Read-Json (Join-Path $Root 'state\host-status.json') }
function Host-Exe { Join-Path $Root 'bin\vmshost-s4.exe' }

function Fresh-Heartbeat([string] $version, [int] $seconds = 30) {
    Wait-Until { $h = Heartbeat; $h.version -eq $version -and ([DateTimeOffset]::UtcNow.ToUnixTimeSeconds() - $h.ts) -le 3 } $seconds "latido de $version"
}

# ------------------------------------------------------------------ 0. limpieza de una ejecución anterior
if (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) {
    Stop-Service -Name $ServiceName -Force -ErrorAction SilentlyContinue
    & sc.exe delete $ServiceName | Out-Null
    Start-Sleep -Seconds 2
}
if (Test-Path $Root) { Remove-Item -Recurse -Force $Root }

# ------------------------------------------------------------------ 1. disposición en disco
Step 'disposicion' {
    foreach ($d in 'bin', 'state', 'logs', 'versions\1.0.0', 'versions\1.1.0', 'versions\2.0.0') {
        New-Item -ItemType Directory -Force -Path (Join-Path $Root $d) | Out-Null
    }
    Copy-Item (Join-Path $BuildDir 'vmshost-s4.exe') (Join-Path $Root 'bin\')
    foreach ($v in '1.0.0', '1.1.0', '2.0.0') { Copy-Item (Join-Path $BuildDir 'hola.exe') (Join-Path $Root "versions\$v\") }
    Set-Content -LiteralPath (Join-Path $Root 'versions\2.0.0\CRASH') -Value 'versión rota a propósito'
    '{"last_good":"1.0.0"}' | Set-Content -LiteralPath (Join-Path $Root 'state\journal.json') -Encoding ascii
    & (Host-Exe) show --root $Root | Out-Null      # crea active.json desde el diario
    if ((Pointer).active -ne '1.0.0') { throw 'active.json no se creó con 1.0.0' }
    'versions 1.0.0, 1.1.0 y 2.0.0 (rota); active.json reconstruido desde el diario'
}

# ------------------------------------------------------------------ 2. servicio con cuenta virtual + ACL por SID
Step 'instalar_servicio' {
    # Sin comillas internas a propósito (la ruta no lleva espacios): evita las diferencias de paso de
    # argumentos entre PowerShell 5.1 y 7 al llamar a sc.exe.
    if ($Root -match '\s') { throw 'Esta prueba exige una carpeta sin espacios' }
    $bin = "$(Host-Exe) service --name $ServiceName --root $Root --confirm-timeout 25"
    $out = & sc.exe create $ServiceName binPath= $bin obj= "NT SERVICE\$ServiceName" type= own start= demand
    if ($LASTEXITCODE -ne 0) { throw "sc create: $out" }
    & sc.exe failure $ServiceName reset= 86400 actions= restart/1000/restart/5000/restart/30000 | Out-Null
    $sidLine = (& sc.exe showsid $ServiceName) | Where-Object { $_ -match 'S-1-5-80-' }
    $script:sid = ([regex]::Match(($sidLine -join ' '), 'S-1-5-80-[0-9-]+')).Value
    if (-not $sid) { throw 'no se obtuvo el SID del servicio' }
    # Solo SYSTEM, Administradores y el SID del servicio. Lectura en bin\ y versions\; modificar en state\ y logs\.
    & icacls.exe $Root /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' "*${sid}:(OI)(CI)RX" | Out-Null
    foreach ($d in 'state', 'logs') { & icacls.exe (Join-Path $Root $d) /grant "*${sid}:(OI)(CI)M" | Out-Null }
    $qc = (& sc.exe qc $ServiceName) -join "`n"
    if ($qc -notmatch "NT SERVICE\\$ServiceName") { throw "cuenta inesperada: $qc" }
    "SID $sid; ImagePath con vmshost; recuperación 1/5/30 s"
}

Step 'arrancar_y_consultar' {
    Start-Service -Name $ServiceName
    Wait-Until { (Get-Service $ServiceName).Status -eq 'Running' } 20 'servicio en marcha'
    Fresh-Heartbeat '1.0.0'
    $hs = HostStatus
    if (-not $hs.child_in_job) { throw 'el hijo no está dentro del Job Object' }
    $proc = Get-CimInstance Win32_Process -Filter "ProcessId = $($hs.child_pid)"
    $owner = Invoke-CimMethod -InputObject $proc -MethodName GetOwner
    if ($owner.Domain -ne 'NT SERVICE' -or $owner.User -ne $ServiceName) { throw "propietario inesperado: $($owner.Domain)\$($owner.User)" }
    $hostProc = Get-CimInstance Win32_Process -Filter "ProcessId = $($hs.host_pid)"
    $hb = Heartbeat
    if ($hb.can_write_bin) { throw 'la cuenta virtual puede escribir en bin\ (ACL demasiado abierta)' }
    "hijo $($hs.child_pid) como $($owner.Domain)\$($owner.User), en job; arrancador $($hostProc.Name); sin escritura en bin\"
}

Step 'job_object_mata_hijos' {
    $hs = HostStatus
    $child = $hs.child_pid
    Stop-Process -Id $hs.host_pid -Force
    Wait-Until { -not (Get-Process -Id $child -ErrorAction SilentlyContinue) } 10 'que el hijo muera con el arrancador'
    # El SCM relanza el servicio por las acciones de recuperación (1 s)
    Wait-Until { (Get-Service $ServiceName).Status -eq 'Running' -and (HostStatus).host_pid -ne $hs.host_pid } 30 'reinicio por recuperación del SCM'
    Fresh-Heartbeat '1.0.0'
    "hijo $child terminado al matar el arrancador; el SCM lo relanzó (pid $((HostStatus).host_pid))"
}

Step 'version_rota_vuelve_atras' {
    & (Host-Exe) switch --root $Root --to 2.0.0 | Out-Null
    Wait-Until { $p = Pointer; $p.active -eq '1.0.0' -and -not $p.trial -and $p.previous -eq '2.0.0' } 60 'vuelta atrás tras 3 caídas'
    Fresh-Heartbeat '1.0.0'
    $hs = HostStatus
    "3 caídas de 2.0.0 → active.json vuelve a 1.0.0 (vueltas atrás: $($hs.rollbacks)); $($hs.last_event)"
}

Step 'puntero_corrupto_se_reconstruye' {
    Stop-Service -Name $ServiceName
    Set-Content -LiteralPath (Join-Path $Root 'state\active.json') -Value '{basura' -Encoding ascii
    Start-Service -Name $ServiceName
    Fresh-Heartbeat '1.0.0'
    if ((Pointer).active -ne '1.0.0') { throw 'el puntero no se reconstruyó' }
    'active.json corrupto → reconstruido con last_good del diario (1.0.0)'
}

Step 'sin_confirmar_vuelve_atras_por_tiempo' {
    & (Host-Exe) switch --root $Root --to 1.1.0 | Out-Null
    Fresh-Heartbeat '1.1.0'
    Wait-Until { $p = Pointer; $p.active -eq '1.0.0' -and $p.previous -eq '1.1.0' } 60 'vuelta atrás por plazo de confirmación (25 s)'
    Fresh-Heartbeat '1.0.0'
    '1.1.0 arrancó pero nadie la confirmó en 25 s → vuelta a 1.0.0'
}

Step 'confirmada_se_queda' {
    & (Host-Exe) switch --root $Root --to 1.1.0 | Out-Null
    Fresh-Heartbeat '1.1.0'
    & (Host-Exe) confirm --root $Root | Out-Null
    Start-Sleep -Seconds 30
    $p = Pointer
    if ($p.active -ne '1.1.0' -or $p.trial) { throw "estado inesperado: $($p | ConvertTo-Json -Compress)" }
    if ((Read-Json (Join-Path $Root 'state\journal.json')).last_good -ne '1.1.0') { throw 'el diario no apunta a 1.1.0' }
    Fresh-Heartbeat '1.1.0'
    '1.1.0 confirmada: sigue activa pasado el plazo y el diario la marca como buena'
}

Step 'parar_y_desinstalar' {
    Stop-Service -Name $ServiceName
    Wait-Until { (Get-Service $ServiceName).Status -eq 'Stopped' } 20 'servicio parado'
    Start-Sleep -Seconds 1
    $alive = Get-Process -Name 'hola' -ErrorAction SilentlyContinue
    if ($alive) { throw "quedan procesos hola: $($alive.Id -join ', ')" }
    $out = & sc.exe delete $ServiceName
    if ($LASTEXITCODE -ne 0) { throw "sc delete: $out" }
    Wait-Until { -not (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) } 20 'servicio borrado'
    'parado sin procesos huérfanos y desinstalado'
}

$results['all_ok'] = -not $failed
$results | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $Out -Encoding utf8
Write-Host '--- vmshost.log ---'
Get-Content -LiteralPath (Join-Path $Root 'logs\vmshost.log') -ErrorAction SilentlyContinue | Select-Object -Last 40
if ($failed) { exit 1 }
