#Requires -Version 5.1
<#
.SYNOPSIS
    Deja un PC con Windows 11 listo para desarrollar VMS Multimarca (lo usa Claude Code o una persona).

.DESCRIPTION
    Se puede ejecutar las veces que haga falta: lo que ya está instalado no se vuelve a instalar.

      1. Herramientas con winget (si faltan): Git, Python 3.12, rustup, Node.js LTS, Visual Studio Build Tools
         (C++, lo necesita Rust en Windows) y FFmpeg (solo para pruebas con vídeo simulado).
      2. Toolchain de Rust fijado (1.99.0, el de rust-toolchain.toml).
      3. Entorno de Python en .venv con los locks del proyecto (los de sede con SHA-256 + el de pruebas).
      4. MediaMTX (motor de vídeo) en .\bin y Chromium de Playwright (pruebas de la web).
      5. Opcional (-Compilar): compila vmshost.exe, vmsctl.exe y el visor VMS.exe.

    Al final comprueba que las pruebas rápidas pasan.

.PARAMETER Compilar
    Compila también los binarios de Rust y el visor (tarda 10-20 minutos la primera vez).

.PARAMETER SinHerramientas
    No usa winget (las herramientas ya están instaladas a mano).

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File tools\windows\preparar-entorno.ps1
#>
[CmdletBinding()]
param(
    [switch]$Compilar,
    [switch]$SinHerramientas
)

$ErrorActionPreference = 'Stop'
$Repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
Set-Location $Repo

function Paso([string]$t) { Write-Host "`n== $t" -ForegroundColor Cyan }
function Ok([string]$t) { Write-Host "   OK  $t" -ForegroundColor Green }
function Aviso([string]$t) { Write-Host "   !!  $t" -ForegroundColor Yellow }

function Refrescar-Path {
    $m = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $u = [Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = "$m;$u;$env:USERPROFILE\.cargo\bin"
}

function Ejecutar([string]$exe, [string[]]$argumentos) {
    & $exe @argumentos
    if ($LASTEXITCODE -ne 0) { throw "Falló: $exe $($argumentos -join ' ') (código $LASTEXITCODE)" }
}

function Tiene([string]$cmd) { [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }

# --------------------------------------------------------------------------------------------- 1. herramientas
if (-not $SinHerramientas) {
    Paso 'Herramientas (winget)'
    if (-not (Tiene 'winget')) {
        throw 'No está winget. Instala «App Installer» desde Microsoft Store y vuelve a ejecutar este script.'
    }
    $herramientas = @(
        @{ Id = 'Git.Git';              Prueba = { Tiene 'git' } },
        @{ Id = 'Python.Python.3.12';   Prueba = { (Tiene 'py') -and ((& py -3.12 -c 'print(1)' 2>$null) -eq '1') } },
        @{ Id = 'Rustlang.Rustup';      Prueba = { Tiene 'rustup' } },
        @{ Id = 'OpenJS.NodeJS.LTS';    Prueba = { Tiene 'node' } },
        @{ Id = 'Gyan.FFmpeg';          Prueba = { Tiene 'ffmpeg' } }
    )
    foreach ($h in $herramientas) {
        if (& $h.Prueba) { Ok "$($h.Id) ya está"; continue }
        Write-Host "   ..  instalando $($h.Id)"
        winget install --id $h.Id -e --accept-source-agreements --accept-package-agreements --silent
        Refrescar-Path
    }
    # Compilador de C++ de Microsoft: sin él Rust no enlaza en Windows (link.exe).
    $vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
    $vc = if (Test-Path $vswhere) {
        & $vswhere -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
    }
    if ($vc) { Ok 'Visual Studio Build Tools (C++) ya está' }
    else {
        Write-Host '   ..  instalando Visual Studio Build Tools con C++ (unos 5 GB, tarda un rato)'
        winget install --id Microsoft.VisualStudio.2022.BuildTools -e --silent --accept-source-agreements `
            --accept-package-agreements --override '--quiet --wait --norestart --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended'
    }
    Refrescar-Path
}

foreach ($c in 'git', 'py', 'rustup', 'node') {
    if (-not (Tiene $c)) { throw "Falta «$c». Cierra esta ventana, abre otra nueva (para que Windows lea el PATH) y repite." }
}

# --------------------------------------------------------------------------------------------- 2. Rust
Paso 'Rust 1.99.0 (rust-toolchain.toml)'
Ejecutar rustup @('toolchain', 'install', '1.99.0', '--profile', 'minimal', '--component', 'clippy,rustfmt')
Ok (& rustc +1.99.0 --version)

# --------------------------------------------------------------------------------------------- 3. Python
Paso 'Python 3.12 en .venv'
if (-not (Test-Path '.venv\Scripts\python.exe')) { Ejecutar py @('-3.12', '-m', 'venv', '.venv') }
$py = Join-Path $Repo '.venv\Scripts\python.exe'
Ejecutar $py @('-m', 'pip', 'install', '--disable-pip-version-check', '-q', '--upgrade', 'pip')
Write-Host '   ..  dependencias (locks con SHA-256; la primera vez tarda varios minutos)'
Ejecutar $py @('-m', 'pip', 'install', '--disable-pip-version-check', '-q', '--require-hashes', '--no-deps',
    '-r', 'requirements-vms.txt', '-r', 'requirements-analytics.txt', '-r', 'requirements-central.txt',
    '-r', 'requirements-updater.txt')
Ejecutar $py @('-m', 'pip', 'install', '--disable-pip-version-check', '-q', '--no-deps', '-r', 'requirements-test.txt')
Ejecutar $py @('-m', 'pip', 'install', '--disable-pip-version-check', '-q', '--no-deps', '-e', '.')
Ok (& $py --version)

# --------------------------------------------------------------------------------------------- 4. MediaMTX y Chromium
Paso 'MediaMTX (motor de vídeo, verificado con SHA-256) y Chromium para las pruebas de la web'
Ejecutar $py @('-m', 'tools.fetch_mediamtx')
Ejecutar $py @('-m', 'playwright', 'install', 'chromium')
Ok 'listo'

# --------------------------------------------------------------------------------------------- 5. compilar (opcional)
if ($Compilar) {
    Paso 'Compilar vmshost.exe y vmsctl.exe (native\)'
    Push-Location native
    try { Ejecutar cargo @('build', '--release', '--locked', '-p', 'vmshost', '-p', 'vmsctl') } finally { Pop-Location }
    Paso 'Compilar el visor VMS.exe (native\viewer)'
    Push-Location native\viewer\src-tauri
    try { Ejecutar npx @('--yes', '@tauri-apps/cli@2.12.1', 'build', '--no-bundle', '--ci') } finally { Pop-Location }
    Ok 'native\target\release\vmshost.exe, vmsctl.exe y native\viewer\src-tauri\target\release\VMS.exe'
}

# --------------------------------------------------------------------------------------------- 6. comprobación
Paso 'Comprobación rápida (lint y pruebas cortas)'
Ejecutar $py @('-m', 'ruff', 'check', '.')
Ejecutar $py @('-m', 'pytest', '-q', '-p', 'no:cacheprovider', 'tests/ops/test_diagnose.py',
    'tests/vendors/test_discovery_v2.py', 'tests/api/test_github_releases.py', 'tests/core/test_stdin_stop.py')

Write-Host "`nEntorno listo." -ForegroundColor Green
Write-Host '  Pruebas:      .venv\Scripts\python -m pytest -m "not e2e and not slow" -q'
Write-Host '  Probar la app: .venv\Scripts\python -m tools.dev_run start --sim --seed --no-analytics   (http://127.0.0.1:8600)'
Write-Host '  Instalador:   .venv\Scripts\python -m tools.build all --version 2.0.0-dev --out dist   (ver docs\EMPAQUETADO.md)'
