#Requires -Version 5.1
#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Instala VMS Multimarca en Windows 10/11 como servicios de Windows.

.DESCRIPTION
    Pasos (todos idempotentes: puedes volver a ejecutarlo para actualizar o reparar):
      1. Comprueba Windows de 64 bits y permisos de administrador.
      2. Copia la aplicación a -InstallDir (por defecto C:\Program Files\VMSMultimarca).
      3. Python 3.12: usa el instalado en el equipo (py -3.12) creando un venv, o descarga el
         Python embebible oficial (versión fijada, SHA-256 comprobado).
      4. Instala las dependencias con los archivos de bloqueo (pip --no-deps, solo wheels).
      5. Descarga MediaMTX para Windows (versión fijada, SHA-256 comprobado).
      6. Crea el archivo .env en la carpeta de datos desde .env.example (sin pisar uno existente)
         y restringe los permisos de .env y secrets\ a SYSTEM y Administradores.
      7. Registra los servicios con WinSW (licencia MIT; versión fijada, SHA-256 comprobado),
         con reinicio automático ante fallos y registros rotativos.
      8. Abre en el Firewall de Windows SOLO los puertos necesarios y SOLO en el perfil de red
         privada (opcionalmente también dominio).
      9. Arranca los servicios y comprueba /api/health.

    Componentes (-Components):
      Backend    servicio VMSBackend: python -m vms (supervisa MediaMTX como proceso hijo)
      Analytics  servicio VMSAnalytics: python -m analytics (conteo de puerta y cola)
      Heartbeat  servicio VMSHeartbeat: python -m central.agent (latido HTTP al panel central)
      Central    servicio VMSCentral: python -m central (panel central multi-sede, puerto 8700)

    MediaMTX NO se registra como servicio aparte: el backend lo arranca, le registra las cámaras
    por su API (las contraseñas solo viven en memoria) y lo relanza si se cae. Un segundo
    servicio de MediaMTX ocuparía los mismos puertos y no tendría las cámaras.

.PARAMETER Components
    Lista de componentes a instalar. Por defecto: Backend.

.PARAMETER DownloadsDir
    Carpeta con descargas previas (para equipos sin Internet). Si un archivo está ahí con el
    nombre esperado, se usa en lugar de descargarlo; el SHA-256 se comprueba igual.

.PARAMETER WheelhouseDir
    Carpeta con las wheels de Python ya descargadas (pip download) para instalar sin Internet.

.EXAMPLE
    .\install.ps1
    Instala el backend con los valores por defecto.

.EXAMPLE
    .\install.ps1 -Components Backend,Analytics,Heartbeat -SiteId site-bcn-001 -CentralUrl https://central.vpn:8700
    Tienda completa con analítica y latido hacia la central.
#>
[Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSReviewUnusedParameter', 'DownloadsDir',
    Justification = 'Se usa dentro de Get-VerifiedFile ($script:DownloadsDir)')]
[CmdletBinding()]
param(
    [ValidateSet('Backend', 'Analytics', 'Heartbeat', 'Central')]
    [string[]]$Components = @('Backend'),
    [string]$InstallDir = (Join-Path $env:ProgramFiles 'VMSMultimarca'),
    [string]$DataDir = (Join-Path $env:ProgramData 'VMSMultimarca'),
    [string]$SourceDir = (Join-Path $PSScriptRoot '..\..'),
    [ValidateSet('Auto', 'System', 'Embedded')]
    [string]$PythonMode = 'Auto',
    [string]$DownloadsDir = (Join-Path $PSScriptRoot 'downloads'),
    [string]$WheelhouseDir = '',
    [ValidatePattern('^$|^[a-z0-9][a-z0-9\-]{2,39}$')]
    [string]$SiteId = '',
    [ValidatePattern('^$|^https?://')]
    [string]$CentralUrl = '',
    [int]$HttpPort = 8600,
    [int]$IcePort = 8189,
    [int]$CentralPort = 8700,
    [switch]$AllowDomainProfile,
    [switch]$SkipFirewall,
    [switch]$NoStart
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'   # Invoke-WebRequest es muy lento con la barra de progreso

# --------------------------------------------------------------------------- versiones fijadas
# Cambiar una versión exige actualizar su SHA-256 (se comprueba antes de usar el archivo).
$Pinned = @{
    MediaMtxVersion = 'v1.21.1'
    MediaMtxFile    = 'mediamtx_v1.21.1_windows_amd64.zip'
    MediaMtxUrl     = 'https://github.com/bluenviron/mediamtx/releases/download/v1.21.1/mediamtx_v1.21.1_windows_amd64.zip'
    MediaMtxSha256  = 'faa97974861eb75a68b5aa326c78e7e7a6f670b5ef191bace78e715130381f23'
    WinSwFile       = 'WinSW.NET461.exe'
    WinSwUrl        = 'https://github.com/winsw/winsw/releases/download/v2.12.0/WinSW.NET461.exe'
    WinSwSha256     = 'b5066b7bbdfba1293e5d15cda3caaea88fbeab35bd5b38c41c913d492aadfc4f'
    PythonVersion   = '3.12.10'
    PythonFile      = 'python-3.12.10-embed-amd64.zip'
    PythonUrl       = 'https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip'
    PythonSha256    = '4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3'
    PipFile         = 'pip-26.2.1-py3-none-any.whl'
    PipUrl          = 'https://files.pythonhosted.org/packages/f3/6e/1736e5b4ae2b778ef2f81c47d797de9f891d4d8acb047a24ca37a60294dd/pip-26.2.1-py3-none-any.whl'
    PipSha256       = '71138adf1f4ca900cdb7d289c21b7494329f2332b6d85f0e1c42108c0384ed3e'
}

$ServiceDefs = @{
    Backend   = @{ Id = 'VMSBackend'; Name = 'VMS Multimarca - Backend y video'; Args = '-m vms'
                   Description = 'Backend web, vista en vivo, grabacion 24/7 y reproduccion (MediaMTX incluido).'
                   Requirements = 'requirements-vms.txt' }
    Analytics = @{ Id = 'VMSAnalytics'; Name = 'VMS Multimarca - Analitica'; Args = '-m analytics'
                   Description = 'Conteo anonimo de personas en puerta y ocupacion de cola de cajas.'
                   Requirements = 'requirements-analytics.txt' }
    Heartbeat = @{ Id = 'VMSHeartbeat'; Name = 'VMS Multimarca - Latido de sede'; Args = '-m central.agent'
                   Description = 'Envia el estado de la sede (camaras, disco, version) al panel central.'
                   Requirements = 'requirements-central.txt' }
    Central   = @{ Id = 'VMSCentral'; Name = 'VMS Multimarca - Panel central'; Args = '-m central'
                   Description = 'Panel central multi-sede (lee la PostgreSQL central).'
                   Requirements = 'requirements-central.txt' }
}

# --------------------------------------------------------------------------- utilidades
function Write-Step([string]$Text) { Write-Host ''; Write-Host "==> $Text" -ForegroundColor Cyan }
function Write-Ok([string]$Text) { Write-Host "    OK  $Text" -ForegroundColor Green }
function Write-Warn([string]$Text) { Write-Host "    AVISO  $Text" -ForegroundColor Yellow }

function Get-FileSha256([string]$Path) {
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Get-VerifiedFile {
    <# Devuelve la ruta de un archivo verificado por SHA-256: de -DownloadsDir o descargado. #>
    param([string]$FileName, [string]$Url, [string]$Sha256, [string]$CacheDir)
    New-Item -ItemType Directory -Force -Path $CacheDir | Out-Null
    $candidates = @((Join-Path $script:DownloadsDir $FileName), (Join-Path $CacheDir $FileName))
    foreach ($c in $candidates) {
        if (Test-Path -LiteralPath $c) {
            if ((Get-FileSha256 $c) -eq $Sha256) { return $c }
            Write-Warn "El archivo $c no coincide con el SHA-256 esperado; se descarta."
        }
    }
    $target = Join-Path $CacheDir $FileName
    $tmp = "$target.part"
    Write-Host "    Descargando $FileName ..."
    try {
        Invoke-WebRequest -Uri $Url -OutFile $tmp -UseBasicParsing -TimeoutSec 600
    } catch {
        throw "No se pudo descargar $FileName desde $Url. Si el equipo no tiene Internet, copia el archivo a $($script:DownloadsDir). Detalle: $($_.Exception.Message)"
    }
    $got = Get-FileSha256 $tmp
    if ($got -ne $Sha256) {
        Remove-Item -LiteralPath $tmp -Force
        throw "SHA-256 incorrecto para $FileName (esperado $Sha256, obtenido $got). Descarga cancelada por seguridad."
    }
    Move-Item -LiteralPath $tmp -Destination $target -Force
    return $target
}

function New-RandomToken([int]$Bytes = 32) {
    $buf = New-Object byte[] $Bytes
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($buf) } finally { $rng.Dispose() }
    return ([Convert]::ToBase64String($buf)).TrimEnd('=').Replace('+', '-').Replace('/', '_')
}

function Set-EnvValue {
    <# Fija CLAVE=valor en un archivo .env (sustituye la línea o la añade). #>
    param([string]$Path, [string]$Key, [string]$Value, [switch]$OnlyIfEmpty)
    $lines = @()
    if (Test-Path -LiteralPath $Path) { $lines = @(Get-Content -LiteralPath $Path -Encoding UTF8) }
    $pattern = '^\s*' + [regex]::Escape($Key) + '\s*='
    $found = $false
    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($lines[$i] -match $pattern) {
            $found = $true
            $current = ($lines[$i] -split '=', 2)[1].Trim()
            if ($OnlyIfEmpty -and $current) { return }
            $lines[$i] = "$Key=$Value"
        }
    }
    if (-not $found) { $lines += "$Key=$Value" }
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllLines($Path, [string[]]$lines, $utf8NoBom)
}

function Get-EnvValue([string]$Path, [string]$Key) {
    if (-not (Test-Path -LiteralPath $Path)) { return '' }
    foreach ($line in Get-Content -LiteralPath $Path -Encoding UTF8) {
        if ($line -match ('^\s*' + [regex]::Escape($Key) + '\s*=(.*)$')) { return $Matches[1].Trim() }
    }
    return ''
}

function Protect-Path {
    <# Deja una carpeta o archivo solo para SYSTEM y Administradores (por SID: vale en Windows en español). #>
    param([string]$Path, [string[]]$ExtraReaders = @())
    $isDir = (Get-Item -LiteralPath $Path).PSIsContainer
    $inh = ''
    if ($isDir) { $inh = '(OI)(CI)' }
    $grants = @("*S-1-5-18:$($inh)F", "*S-1-5-32-544:$($inh)F")
    foreach ($r in $ExtraReaders) { $grants += "$($r):$($inh)R" }
    $icaclsArgs = @($Path, '/inheritance:r')
    foreach ($g in $grants) { $icaclsArgs += @('/grant:r', $g) }
    & icacls.exe @icaclsArgs | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "icacls falló al proteger $Path (código $LASTEXITCODE)" }
}

function Invoke-Checked {
    <# Ejecuta un programa externo y lanza un error claro si devuelve código distinto de 0. #>
    param([string]$FilePath, [string[]]$Arguments, [string]$What)
    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$What falló (código $LASTEXITCODE)." }
}

function Invoke-Probe {
    <# Ejecuta un programa externo solo para consultar algo. En Windows PowerShell 5.1, con
       ErrorActionPreference=Stop, cualquier texto en stderr redirigido se convierte en un error
       que detiene el script (p. ej. «No suitable Python runtime found» de py.exe). Aquí un fallo
       solo significa «no disponible». #>
    param([string]$FilePath, [string[]]$Arguments)
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $out = & $FilePath @Arguments 2>$null
        if ($LASTEXITCODE -eq 0 -and $out) { return (($out | Select-Object -First 1) -as [string]).Trim() }
    } catch {
        Write-Verbose "Consulta fallida: $FilePath $($Arguments -join ' '): $_"
    } finally {
        $ErrorActionPreference = $prev
    }
    return $null
}

function Find-SystemPython312 {
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($launcher) {
        $exe = Invoke-Probe $launcher.Source @('-3.12', '-c', 'import sys; print(sys.executable)')
        if ($exe) { return $exe }
    }
    foreach ($name in @('python.exe', 'python3.12.exe')) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd -and $cmd.Source -notlike '*\WindowsApps\*') {   # el alias de la Microsoft Store no sirve
            $ver = Invoke-Probe $cmd.Source @('-c', 'import sys; print(*sys.version_info[:2], sep=chr(46))')
            if ($ver -eq '3.12') { return $cmd.Source }
        }
    }
    return $null
}

# --------------------------------------------------------------------------- 1. comprobaciones
Write-Step 'Comprobando el equipo'
if (-not [Environment]::Is64BitOperatingSystem) { throw 'Se necesita Windows de 64 bits.' }
$os = [Environment]::OSVersion.Version
if ($os.Major -lt 10) { throw "Se necesita Windows 10 u 11 (este equipo tiene $os)." }
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
$SourceDir = (Resolve-Path -LiteralPath $SourceDir).Path
if (-not (Test-Path -LiteralPath (Join-Path $SourceDir 'vms\__init__.py'))) {
    throw "No encuentro la aplicación en $SourceDir (falta vms\__init__.py). Ejecuta el script desde deploy\windows del paquete."
}
$Components = @($Components | Select-Object -Unique)
Write-Ok "Windows $os de 64 bits; componentes: $($Components -join ', ')"

$runningServices = @()
foreach ($c in $Components) {
    $svc = Get-Service -Name $ServiceDefs[$c].Id -ErrorAction SilentlyContinue
    if ($svc -and $svc.Status -eq 'Running') {
        Write-Host "    Parando $($svc.Name) para actualizar..."
        Stop-Service -Name $svc.Name -Force
        $runningServices += $svc.Name
    }
}

# --------------------------------------------------------------------------- 2. copia de la aplicación
Write-Step "Copiando la aplicación a $InstallDir"
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
$installFull = (Resolve-Path -LiteralPath $InstallDir).Path
if ($installFull.TrimEnd('\') -ne $SourceDir.TrimEnd('\')) {
    foreach ($dir in @('vms', 'analytics', 'central', 'deploy')) {
        $src = Join-Path $SourceDir $dir
        if (Test-Path -LiteralPath $src) {
            & robocopy.exe $src (Join-Path $InstallDir $dir) /MIR /NFL /NDL /NJH /NJS /NP /XD __pycache__ downloads /XF '*.pyc' | Out-Null
            if ($LASTEXITCODE -ge 8) { throw "robocopy falló copiando $dir (código $LASTEXITCODE)" }
        }
    }
    foreach ($f in @('pyproject.toml', '.env.example', 'LEEME.md', 'THIRD_PARTY_NOTICES.txt', 'requirements-vms.txt',
                     'requirements-analytics.txt', 'requirements-central.txt')) {
        $src = Join-Path $SourceDir $f
        if (Test-Path -LiteralPath $src) { Copy-Item -LiteralPath $src -Destination $InstallDir -Force }
    }
    $models = Join-Path $SourceDir 'models'
    if ((Test-Path -LiteralPath $models) -and ($Components -contains 'Analytics')) {
        # Solo los modelos exportados (ONNX/OpenVINO + ficha): los checkpoints .pth de models\weights
        # (750 MB) solo sirven para exportar y no se distribuyen.
        & robocopy.exe $models (Join-Path $InstallDir 'models') /MIR /XD weights /XF *.pth *.pt /NFL /NDL /NJH /NJS /NP | Out-Null
        if ($LASTEXITCODE -ge 8) { throw "robocopy falló copiando models (código $LASTEXITCODE)" }
    }
}
$global:LASTEXITCODE = 0
Write-Ok 'Aplicación copiada'
$Cache = Join-Path $InstallDir 'downloads'

# --------------------------------------------------------------------------- 3. Python
Write-Step 'Preparando Python 3.12'
$sysPython = $null
if ($PythonMode -ne 'Embedded') { $sysPython = Find-SystemPython312 }
if ($PythonMode -eq 'System' -and -not $sysPython) {
    throw 'No se encontró Python 3.12 en el equipo. Instálalo desde python.org o usa -PythonMode Embedded.'
}
if ($sysPython) {
    $venv = Join-Path $InstallDir '.venv'
    if (-not (Test-Path -LiteralPath (Join-Path $venv 'Scripts\python.exe'))) {
        Invoke-Checked $sysPython @('-m', 'venv', $venv) 'La creación del entorno virtual'
    }
    $Python = Join-Path $venv 'Scripts\python.exe'
    $sitePackages = Join-Path $venv 'Lib\site-packages'
    Set-Content -LiteralPath (Join-Path $sitePackages 'vms-multimarca.pth') -Value $InstallDir -Encoding ASCII
    $PipCmd = @($Python, '-m', 'pip')
    Write-Ok "Python del sistema ($sysPython) con entorno virtual en $venv"
} else {
    $pyDir = Join-Path $InstallDir 'python'
    $Python = Join-Path $pyDir 'python.exe'
    $zip = Get-VerifiedFile $Pinned.PythonFile $Pinned.PythonUrl $Pinned.PythonSha256 $Cache
    if (-not (Test-Path -LiteralPath $Python)) {
        Expand-Archive -LiteralPath $zip -DestinationPath $pyDir -Force
    }
    # El Python embebible ignora PYTHONPATH: las rutas se declaran en python312._pth.
    $pth = Join-Path $pyDir 'python312._pth'
    $pthLines = @('python312.zip', '.', 'Lib\site-packages', '..', 'import site')
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllLines($pth, [string[]]$pthLines, $utf8NoBom)
    New-Item -ItemType Directory -Force -Path (Join-Path $pyDir 'Lib\site-packages') | Out-Null
    $pipWheel = Get-VerifiedFile $Pinned.PipFile $Pinned.PipUrl $Pinned.PipSha256 $Cache
    $sitePackages = Join-Path $pyDir 'Lib\site-packages'
    if (-not (Test-Path -LiteralPath (Join-Path $sitePackages 'pip'))) {
        # Una wheel es un zip: se descomprime tal cual en site-packages. No se usa «python pip.whl/pip
        # install pip.whl» porque en Windows pip se niega a modificarse si no se invoca con «-m pip».
        Get-ChildItem -LiteralPath $sitePackages -Filter 'pip*' -ErrorAction SilentlyContinue |
            Remove-Item -Recurse -Force
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        [System.IO.Compression.ZipFile]::ExtractToDirectory($pipWheel, $sitePackages)
    }
    Invoke-Checked $Python @('-m', 'pip', '--version') 'La comprobación de pip'
    $PipCmd = @($Python, '-m', 'pip')
    Write-Ok "Python embebible $($Pinned.PythonVersion) en $pyDir"
}

# --------------------------------------------------------------------------- 4. dependencias
Write-Step 'Instalando dependencias (archivos de bloqueo, sin dependencias transitivas no revisadas)'
$reqFiles = @($Components | ForEach-Object { $ServiceDefs[$_].Requirements } | Select-Object -Unique)
foreach ($req in $reqFiles) {
    $reqPath = Join-Path $InstallDir $req
    if (-not (Test-Path -LiteralPath $reqPath)) { throw "Falta $req en el paquete." }
    $pipArgs = @($PipCmd[1..($PipCmd.Count - 1)]) + @('install', '--no-deps', '--require-hashes', '--only-binary=:all:', '--disable-pip-version-check',
                                                      '--no-warn-script-location', '-r', $reqPath)
    if ($WheelhouseDir) { $pipArgs += @('--no-index', '--find-links', $WheelhouseDir) }
    Invoke-Checked $PipCmd[0] $pipArgs "pip install -r $req"
    Write-Ok $req
}
Invoke-Checked $Python @('-c', 'import vms, central; print(vms.__version__)') 'La comprobación de la instalación'

# --------------------------------------------------------------------------- 5. MediaMTX
if ($Components -contains 'Backend') {
    Write-Step "MediaMTX $($Pinned.MediaMtxVersion) para Windows"
    $binDir = Join-Path $InstallDir 'bin'
    New-Item -ItemType Directory -Force -Path $binDir | Out-Null
    $mtxZip = Get-VerifiedFile $Pinned.MediaMtxFile $Pinned.MediaMtxUrl $Pinned.MediaMtxSha256 $Cache
    $tmpDir = Join-Path $Cache 'mediamtx-extract'
    if (Test-Path -LiteralPath $tmpDir) { Remove-Item -LiteralPath $tmpDir -Recurse -Force }
    Expand-Archive -LiteralPath $mtxZip -DestinationPath $tmpDir -Force
    Copy-Item -LiteralPath (Join-Path $tmpDir 'mediamtx.exe') -Destination (Join-Path $binDir 'mediamtx.exe') -Force
    Copy-Item -LiteralPath (Join-Path $tmpDir 'LICENSE') -Destination (Join-Path $binDir 'MEDIAMTX-LICENSE.txt') -Force
    Remove-Item -LiteralPath $tmpDir -Recurse -Force
    Write-Ok "MediaMTX verificado en $binDir"
}

# --------------------------------------------------------------------------- 6. datos y .env
Write-Step "Carpeta de datos $DataDir"
foreach ($sub in @('', 'config', 'secrets', 'logs', 'logs\service', 'recordings', 'mediamtx', 'analytics', 'kiosk')) {
    New-Item -ItemType Directory -Force -Path (Join-Path $DataDir $sub) | Out-Null
}
$envFile = Join-Path $DataDir '.env'
if (-not (Test-Path -LiteralPath $envFile)) {
    Copy-Item -LiteralPath (Join-Path $InstallDir '.env.example') -Destination $envFile
    Write-Ok '.env creado desde .env.example'
} else {
    Write-Ok '.env ya existía: se conserva (solo se completan valores vacíos)'
}
Set-EnvValue $envFile 'VMS_DATA_DIR' $DataDir -OnlyIfEmpty
Set-EnvValue $envFile 'VMS_HTTP_PORT' "$HttpPort"
Set-EnvValue $envFile 'VMS_MTX_WEBRTC_ICE_UDP' ":$IcePort"
Set-EnvValue $envFile 'VMS_MTX_WEBRTC_ICE_TCP' ":$IcePort"
# Los servicios corren como SYSTEM, sin sesión de usuario: almacén cifrado en archivo (secrets\ protegido).
Set-EnvValue $envFile 'VMS_CREDENTIAL_BACKEND' 'file'
Set-EnvValue $envFile 'VMS_KIOSK_TOKEN' (New-RandomToken) -OnlyIfEmpty
if ($SiteId) { Set-EnvValue $envFile 'VMS_SITE_ID' $SiteId }
if ($CentralUrl) { Set-EnvValue $envFile 'VMS_CENTRAL_URL' $CentralUrl }
if ($Components -contains 'Heartbeat') {
    Set-EnvValue $envFile 'VMS_CENTRAL_URL' '' -OnlyIfEmpty
    Set-EnvValue $envFile 'VMS_SITE_TOKEN' '' -OnlyIfEmpty
}
if ($Components -contains 'Central') {
    Set-EnvValue $envFile 'VMS_CENTRAL_HTTP_PORT' "$CentralPort"
    Set-EnvValue $envFile 'VMS_CENTRAL_DATA_DIR' (Join-Path $DataDir 'central') -OnlyIfEmpty
}
# Toda la carpeta de datos (grabaciones, users.json, config.json, registros, analítica) queda solo para
# SYSTEM y Administradores: por defecto ProgramData deja leer a cualquier usuario local, y las
# grabaciones son datos personales (RGPD). kiosk\ tiene su propia ACL (install-kiosk.ps1).
Protect-Path $DataDir
Protect-Path (Join-Path $DataDir 'secrets')
Protect-Path $envFile
Write-Ok 'Carpeta de datos, .env y secrets\ solo accesibles por SYSTEM y Administradores'

# --------------------------------------------------------------------------- 7. servicios (WinSW)
Write-Step 'Registrando servicios de Windows (WinSW, MIT)'
$winsw = Get-VerifiedFile $Pinned.WinSwFile $Pinned.WinSwUrl $Pinned.WinSwSha256 $Cache
$svcDir = Join-Path $InstallDir 'services'
New-Item -ItemType Directory -Force -Path $svcDir | Out-Null
foreach ($c in $Components) {
    $def = $ServiceDefs[$c]
    $exe = Join-Path $svcDir "$($def.Id).exe"
    $xml = Join-Path $svcDir "$($def.Id).xml"
    $esc = { param($s) [System.Security.SecurityElement]::Escape($s) }
    $content = @"
<service>
  <id>$($def.Id)</id>
  <name>$(& $esc $def.Name)</name>
  <description>$(& $esc $def.Description)</description>
  <executable>$(& $esc $Python)</executable>
  <arguments>$($def.Args)</arguments>
  <workingdirectory>$(& $esc $InstallDir)</workingdirectory>
  <env name="VMS_DATA_DIR" value="$(& $esc $DataDir)"/>
  <env name="PYTHONUNBUFFERED" value="1"/>
  <env name="PYTHONUTF8" value="1"/>
  <startmode>Automatic</startmode>
  <delayedAutoStart/>
  <onfailure action="restart" delay="5 sec"/>
  <onfailure action="restart" delay="30 sec"/>
  <onfailure action="restart" delay="60 sec"/>
  <resetfailure>1 hour</resetfailure>
  <stoptimeout>30 sec</stoptimeout>
  <stopparentprocessfirst>true</stopparentprocessfirst>
  <logpath>$(& $esc (Join-Path $DataDir 'logs\service'))</logpath>
  <log mode="roll-by-size">
    <sizeThreshold>10240</sizeThreshold>
    <keepFiles>8</keepFiles>
  </log>
</service>
"@
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($xml, $content, $utf8NoBom)
    Copy-Item -LiteralPath $winsw -Destination $exe -Force
    if (-not (Get-Service -Name $def.Id -ErrorAction SilentlyContinue)) {
        Invoke-Checked $exe @('install') "El registro del servicio $($def.Id)"
    } else {
        Invoke-Checked $exe @('refresh') "La actualización del servicio $($def.Id)"
    }
    Write-Ok "$($def.Id): $($def.Name)"
}

# --------------------------------------------------------------------------- 7b. informe semanal
if ($Components -contains 'Central') {
    Write-Step 'Informe semanal (tarea programada: lunes 06:00, hora de este equipo)'
    $reportsDir = Join-Path $DataDir 'reports'
    New-Item -ItemType Directory -Force -Path $reportsDir | Out-Null
    $action = New-ScheduledTaskAction -Execute $Python -WorkingDirectory $InstallDir `
        -Argument "-m analytics.reports --all-sites --last-week --out `"$reportsDir`""
    $trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At '06:00'
    $principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
    $taskSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
        -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 15)
    Register-ScheduledTask -TaskName 'VMS Multimarca - Informe semanal' -Action $action -Trigger $trigger `
        -Principal $principal -Settings $taskSettings -Force `
        -Description 'Informe semanal por sede (semana ISO anterior)' | Out-Null
    $tz = (Get-TimeZone).Id
    if ($tz -ne 'Romance Standard Time') {
        Write-Warn "La zona horaria del equipo es $($tz): la tarea corre a las 06:00 de esa zona, no de Madrid."
    }
    Write-Ok "Tarea «VMS Multimarca - Informe semanal» creada; los informes también se guardan en $reportsDir"
}

# --------------------------------------------------------------------------- 8. firewall
if (-not $SkipFirewall) {
    Write-Step 'Firewall de Windows (solo red privada)'
    $profiles = @('Private')
    if ($AllowDomainProfile) { $profiles += 'Domain' }
    $group = 'VMS Multimarca'
    Get-NetFirewallRule -Group $group -ErrorAction SilentlyContinue | Remove-NetFirewallRule
    # En un venv de Windows, Scripts\python.exe lanza el intérprete base: la regla va al proceso real.
    $processExe = (& $Python -c 'import sys; print(sys._base_executable)' | Select-Object -First 1).Trim()
    $mtxExe = Join-Path $InstallDir 'bin\mediamtx.exe'
    $rules = @()
    if ($Components -contains 'Backend') {
        $rules += @{ Name = "VMS Multimarca - Web (TCP $HttpPort)"; Protocol = 'TCP'; Port = $HttpPort; Program = $processExe }
        $rules += @{ Name = "VMS Multimarca - Video WebRTC (UDP $IcePort)"; Protocol = 'UDP'; Port = $IcePort; Program = $mtxExe }
        $rules += @{ Name = "VMS Multimarca - Video WebRTC (TCP $IcePort)"; Protocol = 'TCP'; Port = $IcePort; Program = $mtxExe }
    }
    if ($Components -contains 'Central') {
        $rules += @{ Name = "VMS Multimarca - Panel central (TCP $CentralPort)"; Protocol = 'TCP'; Port = $CentralPort; Program = $processExe }
    }
    foreach ($r in $rules) {
        New-NetFirewallRule -DisplayName $r.Name -Group $group -Direction Inbound -Action Allow `
            -Protocol $r.Protocol -LocalPort $r.Port -Profile $profiles -Program $r.Program | Out-Null
        Write-Ok "$($r.Name) [$($profiles -join ', ')]"
    }
    $public = @(Get-NetConnectionProfile -ErrorAction SilentlyContinue | Where-Object { $_.NetworkCategory -eq 'Public' })
    foreach ($p in $public) {
        Write-Warn "La red «$($p.Name)» ($($p.InterfaceAlias)) es PÚBLICA: estas reglas no se aplican en ella."
        Write-Warn "Si es la red de la tienda o la VPN, márcala como privada: Set-NetConnectionProfile -InterfaceAlias '$($p.InterfaceAlias)' -NetworkCategory Private"
    }
}

# --------------------------------------------------------------------------- 9. arranque
if (-not $NoStart) {
    Write-Step 'Arrancando servicios'
    foreach ($c in $Components) {
        $id = $ServiceDefs[$c].Id
        $missing = $false
        if ($c -eq 'Heartbeat' -and (-not (Get-EnvValue $envFile 'VMS_SITE_TOKEN') -or -not (Get-EnvValue $envFile 'VMS_CENTRAL_URL'))) {
            Write-Warn "$id no se arranca: completa VMS_CENTRAL_URL y VMS_SITE_TOKEN en $envFile y luego: Start-Service $id"
            $missing = $true
        }
        if ($c -eq 'Central' -and -not (Get-EnvValue $envFile 'VMS_PG_DSN') -and -not (Get-EnvValue $envFile 'VMS_CENTRAL_PG_DSN')) {
            Write-Warn "$id no se arranca: completa VMS_PG_DSN (o VMS_CENTRAL_PG_DSN) en $envFile y luego: Start-Service $id"
            $missing = $true
        }
        if (-not $missing) {
            Start-Service -Name $id
            Write-Ok "$id en marcha"
        }
    }
    if ($Components -contains 'Backend') {
        Write-Host '    Esperando a que el backend responda...'
        $healthy = $false
        for ($i = 0; $i -lt 60; $i++) {
            try {
                $h = Invoke-RestMethod -Uri "http://127.0.0.1:$HttpPort/api/health" -TimeoutSec 3
                Write-Ok "Backend: estado $($h.status), versión $($h.version)"
                $healthy = $true
                break
            } catch { Start-Sleep -Seconds 2 }
        }
        if (-not $healthy) {
            Write-Warn "El backend no responde todavía. Revisa $DataDir\logs\vms.log y $DataDir\logs\service\"
        }
    }
}

if ($Components -contains 'Analytics') {
    Write-Step 'Diagnóstico de la analítica (python -m analytics check)'
    if (-not (Get-ChildItem -Path (Join-Path $InstallDir 'models') -Filter 'rfdetr-*' -ErrorAction SilentlyContinue)) {
        Write-Warn "No hay modelo de detección en $InstallDir\models. Copia allí los archivos rfdetr-nano.* exportados (ver docs\INSTALACION-WINDOWS.md)."
    }
    $env:VMS_DATA_DIR = $DataDir
    & $Python -m analytics check
    if ($LASTEXITCODE -ne 0) { Write-Warn 'El diagnóstico de la analítica indica problemas (revisa los mensajes de arriba).' }
    else { Write-Ok 'Analítica lista' }
    $global:LASTEXITCODE = 0
}

Write-Host ''
Write-Host 'Instalación terminada.' -ForegroundColor Green
if ($Components -contains 'Backend') {
    Write-Host "  Abre http://127.0.0.1:$HttpPort/ en este PC para crear el primer administrador."
    Write-Host "  Muros en 4 monitores: ejecuta deploy\windows\install-kiosk.ps1 (como administrador)."
}
Write-Host "  Configuración: $envFile    Registros: $DataDir\logs"
