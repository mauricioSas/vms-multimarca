"""Validación de deploy/: scripts de PowerShell y bash, unidades systemd, versiones fijadas y textos.

Herramientas externas opcionales (si faltan, la prueba se salta diciendo por qué):
  - PowerShell 7 (`pwsh` en el PATH o VMS_TEST_PWSH): análisis sintáctico real con el parser de
    PowerShell y ejecución de las funciones auxiliares del instalador.
  - PSScriptAnalyzer (VMS_TEST_PSSA = carpeta del módulo): reglas de calidad.
  - shellcheck (PATH o VMS_TEST_SHELLCHECK).
systemd-analyze no existe en macOS/Windows: las unidades se validan con un analizador propio
(secciones, directivas conocidas, rutas absolutas, coherencia con install.sh).
"""
from __future__ import annotations

import hashlib
import http.server
import json
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy"
PS_SCRIPTS = sorted(DEPLOY.rglob("*.ps1"))
SH_SCRIPTS = sorted(DEPLOY.rglob("*.sh"))
UNITS = sorted((DEPLOY / "linux" / "systemd").glob("*.service"))
TIMERS = sorted((DEPLOY / "linux" / "systemd").glob("*.timer"))


def _tool(env: str, name: str) -> str | None:
    explicit = os.environ.get(env)
    if explicit and Path(explicit).is_file():
        return explicit
    return shutil.which(name)


def _pwsh() -> str:
    exe = _tool("VMS_TEST_PWSH", "pwsh")
    if not exe:
        pytest.skip("No hay PowerShell 7 (pwsh). Define VMS_TEST_PWSH para validar los .ps1 de verdad")
    return exe


def _run_pwsh(script: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    f = tmp_path / "run.ps1"
    f.write_text(script, encoding="utf-8")
    env = {**os.environ, "HOME": str(tmp_path)}
    return subprocess.run([_pwsh(), "-NoProfile", "-NonInteractive", "-File", str(f)], capture_output=True,
                          text=True, timeout=120, env=env)


# =========================================================================== PowerShell
def test_expected_scripts_exist() -> None:
    names = {p.relative_to(DEPLOY).as_posix() for p in PS_SCRIPTS + SH_SCRIPTS}
    assert {"windows/install.ps1", "windows/uninstall.ps1", "windows/install-kiosk.ps1", "kiosk/start-kiosk.ps1",
            "linux/install.sh", "linux/uninstall.sh"} <= names
    assert {u.name for u in UNITS} == {"vms.service", "vms-analytics.service", "vms-heartbeat.service",
                                       "vms-central.service", "vms-central-reports.service"}
    assert [t.name for t in TIMERS] == ["vms-central-reports.timer"]


def test_powershell_parse_and_analyzer(tmp_path: Path) -> None:
    pwsh = _pwsh()
    args = [pwsh, "-NoProfile", "-NonInteractive", "-File", str(Path(__file__).with_name("ps_check.ps1"))]
    pssa = os.environ.get("VMS_TEST_PSSA")
    if pssa:
        args += ["-AnalyzerPath", pssa]
    r = subprocess.run(args + [str(p) for p in PS_SCRIPTS], capture_output=True, text=True, timeout=300,
                       env={**os.environ, "HOME": str(tmp_path)})
    assert r.returncode == 0 and "ALL OK" in r.stdout, r.stdout + r.stderr


def test_powershell_scripts_are_windows_powershell_5_compatible() -> None:
    """Windows 10/11 trae PowerShell 5.1: nada de sintaxis exclusiva de PowerShell 7."""
    for p in PS_SCRIPTS:
        text = p.read_text(encoding="utf-8")
        code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
        assert "#Requires -Version 5.1" in text, p
        assert "Set-StrictMode -Version Latest" in text, p
        assert not re.search(r"\?\?|\?\.|\s&&\s|\s\|\|\s", code), f"{p}: operador solo de PowerShell 7"
        assert not re.search(r"\$\w+\s*\?\s*\S+\s*:", code), f"{p}: operador ternario (solo PowerShell 7)"
        assert "-Parallel" not in code, p


def _functions_from(script: Path, names: list[str]) -> str:
    """Código PowerShell que carga solo ciertas funciones de un script (sin ejecutar el script)."""
    quoted = ", ".join(f"'{n}'" for n in names)
    return f"""
$ErrorActionPreference = 'Stop'
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile('{script}', [ref]$tokens, [ref]$errors)
$wanted = @({quoted})
foreach ($f in $ast.FindAll({{ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] }}, $true)) {{
    if ($wanted -contains $f.Name) {{ . ([ScriptBlock]::Create($f.Extent.Text)) }}
}}
"""


def test_install_ps1_helpers_behave(tmp_path: Path) -> None:
    """Ejecuta de verdad Set-EnvValue, Get-EnvValue, New-RandomToken y Get-VerifiedFile."""
    payload = b"contenido de prueba"
    good = hashlib.sha256(payload).hexdigest()
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    (downloads / "ok.bin").write_bytes(payload)
    (downloads / "bad.bin").write_bytes(b"manipulado")

    served = tmp_path / "served"
    served.mkdir()
    (served / "remote.bin").write_bytes(payload)
    handler = lambda *a, **kw: http.server.SimpleHTTPRequestHandler(*a, directory=str(served), **kw)  # noqa: E731
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}/remote.bin"
    env = tmp_path / "test.env"
    env.write_text("# comentario\nVMS_SITE_ID=site-local\nVMS_KIOSK_TOKEN=\nVMS_PG_DSN=postgresql://a@b/c?x=1\n",
                   encoding="utf-8")
    script = _functions_from(DEPLOY / "windows" / "install.ps1",
                             ["Get-FileSha256", "Get-VerifiedFile", "New-RandomToken", "Set-EnvValue", "Get-EnvValue",
                              "Write-Warn"]) + f"""
$script:DownloadsDir = '{downloads}'
$cache = '{tmp_path / "cache"}'
$out = @{{}}
Set-EnvValue '{env}' 'VMS_KIOSK_TOKEN' (New-RandomToken) -OnlyIfEmpty
$first = Get-EnvValue '{env}' 'VMS_KIOSK_TOKEN'
Set-EnvValue '{env}' 'VMS_KIOSK_TOKEN' 'otro' -OnlyIfEmpty
$out.token_kept = ((Get-EnvValue '{env}' 'VMS_KIOSK_TOKEN') -eq $first)
$out.token_len = $first.Length
Set-EnvValue '{env}' 'VMS_SITE_ID' 'site-bcn-001'
Set-EnvValue '{env}' 'VMS_NUEVA' 'valor=con=iguales'
$out.site = Get-EnvValue '{env}' 'VMS_SITE_ID'
$out.nueva = Get-EnvValue '{env}' 'VMS_NUEVA'
$out.dsn = Get-EnvValue '{env}' 'VMS_PG_DSN'
$out.local = Split-Path -Leaf (Get-VerifiedFile 'ok.bin' 'http://127.0.0.1:9/nada' '{good}' $cache)
$out.remote = (Get-FileSha256 (Get-VerifiedFile 'remote.bin' '{url}' '{good}' $cache)) -eq '{good}'
try {{ Get-VerifiedFile 'bad.bin' '{url.replace("remote", "bad")}' '{good}' $cache | Out-Null; $out.bad = 'aceptado' }}
catch {{ $out.bad = $_.Exception.Message }}
try {{ Get-VerifiedFile 'remote.bin' '{url}' ('0' * 64) (Join-Path $cache 'otro') | Out-Null; $out.mismatch = 'aceptado' }}
catch {{ $out.mismatch = $_.Exception.Message }}
$out | ConvertTo-Json -Compress
"""
    try:
        r = _run_pwsh(script, tmp_path)
    finally:
        httpd.shutdown()
    assert r.returncode == 0, r.stdout + r.stderr
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["token_kept"] is True and out["token_len"] >= 40
    assert out["site"] == "site-bcn-001" and out["nueva"] == "valor=con=iguales"
    assert out["dsn"] == "postgresql://a@b/c?x=1"
    assert out["local"] == "ok.bin"           # usa la descarga previa sin ir a la red
    assert out["remote"] is True              # descarga y verifica
    assert "No se pudo descargar" in out["bad"]  # el archivo local manipulado se descarta y no hay fuente buena
    assert "SHA-256 incorrecto" in out["mismatch"]
    text = env.read_text(encoding="utf-8")
    assert text.startswith("# comentario\n") and "﻿" not in text   # UTF-8 sin BOM, comentarios intactos


def test_kiosk_url_is_escaped(tmp_path: Path) -> None:
    token_file = tmp_path / "kiosk.token"
    token_file.write_text("a+b/c=d&e\n", encoding="utf-8")
    script = _functions_from(DEPLOY / "kiosk" / "start-kiosk.ps1", ["Get-KioskUrl"]) + f"""
$BaseUrl = 'http://127.0.0.1:8600'
$TokenFile = '{token_file}'
Get-KioskUrl 3
"""
    r = _run_pwsh(script, tmp_path)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "http://127.0.0.1:8600/api/auth/kiosk?token=a%2Bb%2Fc%3Dd%26e&next=%2Fwall%2F3"


# =========================================================================== bash
@pytest.mark.parametrize("script", SH_SCRIPTS, ids=lambda p: p.name)
def test_bash_syntax(script: Path) -> None:
    r = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert script.read_text(encoding="utf-8").startswith("#!/usr/bin/env bash\n")
    assert os.access(script, os.X_OK), f"{script} debe ser ejecutable"


def test_shellcheck() -> None:
    sc = _tool("VMS_TEST_SHELLCHECK", "shellcheck")
    if not sc:
        pytest.skip("shellcheck no está instalado (define VMS_TEST_SHELLCHECK)")
    r = subprocess.run([sc, "-S", "style", *map(str, SH_SCRIPTS)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout


def test_linux_install_validates_arguments_before_touching_anything() -> None:
    sh = DEPLOY / "linux" / "install.sh"
    for args, msg in ((["--components", "foo"], "componente desconocido"),
                      (["--site-id", "MAL ID"], "--site-id no válido"),
                      (["--central-url", "ftp://x"], "--central-url"),
                      (["--nada"], "opción desconocida")):
        r = subprocess.run(["bash", str(sh), *args], capture_output=True, text=True, timeout=30)
        assert r.returncode != 0 and msg in r.stderr, (args, r.stderr)
    r = subprocess.run(["bash", str(sh), "--help"], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and "--components" in r.stdout


# =========================================================================== systemd
KNOWN = {
    "Unit": {"Description", "Documentation", "After", "Wants", "Requires", "Before", "PartOf", "BindsTo"},
    "Service": {"Type", "User", "Group", "WorkingDirectory", "Environment", "EnvironmentFile", "ExecStart",
                "Restart", "RestartSec", "TimeoutStopSec", "KillMode", "StateDirectory", "StateDirectoryMode",
                "LimitNOFILE", "PrivateDevices", "NoNewPrivileges", "ProtectSystem", "ProtectHome", "PrivateTmp",
                "ProtectKernelTunables", "ProtectKernelModules", "ProtectKernelLogs", "ProtectControlGroups",
                "ProtectClock", "ProtectHostname", "RestrictSUIDSGID", "RestrictRealtime", "RestrictNamespaces",
                "LockPersonality", "RestrictAddressFamilies", "UMask", "Nice", "ReadWritePaths", "TimeoutStartSec"},
    "Timer": {"OnCalendar", "Persistent", "RandomizedDelaySec", "Unit"},
    "Install": {"WantedBy"},
}
BOOL = {"yes", "no", "true", "false", "on", "off", "1", "0"}


def parse_unit(path: Path) -> dict[str, list[tuple[str, str]]]:
    sections: dict[str, list[tuple[str, str]]] = {}
    current = None
    for n, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("["):
            assert line.endswith("]"), f"{path.name}:{n}: cabecera de sección mal cerrada"
            current = line[1:-1]
            assert current not in sections, f"{path.name}:{n}: sección repetida"
            sections[current] = []
            continue
        assert current is not None, f"{path.name}:{n}: directiva fuera de sección"
        assert "=" in line, f"{path.name}:{n}: falta «=»"
        key, value = (s.strip() for s in line.split("=", 1))
        assert not line.endswith("\\"), f"{path.name}:{n}: continuación de línea no soportada aquí"
        sections[current].append((key, value))
    return sections


@pytest.mark.parametrize("unit", UNITS, ids=lambda p: p.name)
def test_systemd_unit_is_valid(unit: Path) -> None:
    s = parse_unit(unit)
    expected_sections = {"Unit", "Service", "Install"} if "Restart" in dict(s.get("Service", [])) else {"Unit", "Service"}
    assert set(s) == expected_sections
    for section, items in s.items():
        for key, value in items:
            assert key in KNOWN[section], f"{unit.name}: directiva desconocida o no revisada [{section}] {key}"
            assert value, f"{unit.name}: {key} vacío"
    svc = dict(s["Service"])
    exec_start = svc["ExecStart"].split()
    assert exec_start[0].startswith("/opt/vms-multimarca/.venv/bin/python") and exec_start[1] == "-m"
    module = exec_start[2]
    assert svc["User"] == "vms" and svc["Group"] == "vms"
    if svc["Type"] == "oneshot":   # lo lanza un temporizador
        assert "Restart" not in svc and unit.with_suffix(".timer").is_file()
    else:
        assert svc["Type"] == "simple" and svc["Restart"] == "always" and int(svc["RestartSec"]) > 0
        assert dict(s["Install"])["WantedBy"] == "multi-user.target"
    assert svc["StateDirectory"] == "vms-multimarca"
    # El .env lo lee la aplicación (python-dotenv) y no systemd: EnvironmentFile= quitaría las comillas
    # de valores JSON como VMS_MTX_WEBRTC_ADDITIONAL_HOSTS=["100.64.0.12"].
    assert "EnvironmentFile" not in svc
    envs = [v for k, v in s["Service"] if k == "Environment"]
    assert "VMS_ENV_FILE=/etc/vms-multimarca/vms.env" in envs
    assert svc["ProtectSystem"] == "strict" and svc["NoNewPrivileges"] in BOOL
    assert re.fullmatch(r"0[0-7]{3}", svc["UMask"])
    for key in ("TimeoutStopSec", "RestartSec", "TimeoutStartSec"):
        assert key not in svc or re.fullmatch(r"\d+", svc[key])
    assert "network-online.target" in dict(s["Unit"])["After"]
    # el módulo que arranca existe en el repositorio
    target = ROOT.joinpath(*module.split("."))
    candidates = [target / "__main__.py", target.with_suffix(".py")]
    if not any(c.is_file() for c in candidates):
        pytest.skip(f"{unit.name}: el módulo {module} todavía no existe en el repositorio (lo construye otro módulo)")


@pytest.mark.parametrize("timer", TIMERS, ids=lambda p: p.name)
def test_systemd_timer_is_valid(timer: Path) -> None:
    s = parse_unit(timer)
    assert set(s) == {"Unit", "Timer", "Install"}
    for section, items in s.items():
        for key, _ in items:
            assert key in KNOWN[section], f"{timer.name}: directiva desconocida [{section}] {key}"
    t = dict(s["Timer"])
    assert re.fullmatch(r"Mon \*-\*-\* 06:00:00 Europe/Madrid", t["OnCalendar"])
    assert (timer.parent / t["Unit"]).is_file() and t["Persistent"] == "true"
    assert dict(s["Install"])["WantedBy"] == "timers.target"


def test_install_sh_installs_every_unit() -> None:
    text = (DEPLOY / "linux" / "install.sh").read_text(encoding="utf-8")
    for unit in UNITS:
        if unit.stem != "vms-central-reports":
            assert f'UNITS+=("{unit.stem}")' in text
    assert '"vms-central-reports.service" "vms-central-reports.timer"' in text
    assert "/var/lib/vms-multimarca" in text and "/etc/vms-multimarca" in text


# =========================================================================== versiones y sumas fijadas
def _pinned() -> dict[str, str]:
    ps = (DEPLOY / "windows" / "install.ps1").read_text(encoding="utf-8")
    sh = (DEPLOY / "linux" / "install.sh").read_text(encoding="utf-8")
    vals = dict(re.findall(r"^\s+(\w+)\s+=\s+'([^']*)'", ps, re.M))
    vals.update({k: v for k, v in re.findall(r'^(MEDIAMTX_\w+)="([^"]*)"', sh, re.M)})
    return vals


def test_pinned_versions_are_consistent() -> None:
    from tools.fetch_mediamtx import VERSION

    v = _pinned()
    assert v["MediaMtxVersion"] == VERSION == v["MEDIAMTX_VERSION"]
    assert VERSION in v["MediaMtxFile"] and v["MediaMtxUrl"].endswith(v["MediaMtxFile"])
    assert v["PythonUrl"].endswith(v["PythonFile"]) and v["PythonVersion"] in v["PythonFile"]
    assert v["PythonVersion"].startswith("3.12.")
    assert v["PipUrl"].endswith(v["PipFile"]) and v["WinSwUrl"].endswith(v["WinSwFile"])
    for key in ("MediaMtxSha256", "WinSwSha256", "PythonSha256", "PipSha256",
                "MEDIAMTX_SHA256_AMD64", "MEDIAMTX_SHA256_ARM64"):
        assert re.fullmatch(r"[0-9a-f]{64}", v[key]), key
    for url in (v["MediaMtxUrl"], v["WinSwUrl"], v["PythonUrl"], v["PipUrl"]):
        assert url.startswith("https://")


@pytest.mark.slow
def test_mediamtx_checksums_match_official_release() -> None:
    """Compara las sumas fijadas con checksums.sha256 de la release oficial (necesita Internet)."""
    import httpx

    v = _pinned()
    try:
        r = httpx.get(f"https://github.com/bluenviron/mediamtx/releases/download/{v['MediaMtxVersion']}/"
                      "checksums.sha256", follow_redirects=True, timeout=20)
        r.raise_for_status()
    except httpx.HTTPError as exc:
        pytest.skip(f"Sin acceso a GitHub: {exc}")
    sums = {name.lstrip("*"): h for h, name in (line.split() for line in r.text.splitlines() if line.strip())}
    ver = v["MediaMtxVersion"]
    assert sums[f"mediamtx_{ver}_windows_amd64.zip"] == v["MediaMtxSha256"]
    assert sums[f"mediamtx_{ver}_linux_amd64.tar.gz"] == v["MEDIAMTX_SHA256_AMD64"]
    assert sums[f"mediamtx_{ver}_linux_arm64.tar.gz"] == v["MEDIAMTX_SHA256_ARM64"]


def test_third_party_notices_up_to_date() -> None:
    """THIRD_PARTY_NOTICES.txt debe corresponder a los archivos de bloqueo actuales."""
    from deploy.third_party_notices import main

    assert main(["--check"]) == 0, "Regenera con «python -m deploy.third_party_notices»"


def test_firewall_only_private_profile() -> None:
    ps = (DEPLOY / "windows" / "install.ps1").read_text(encoding="utf-8")
    assert "$profiles = @('Private')" in ps and "'Public'" not in ps.replace("-eq 'Public'", "")
    assert "New-NetFirewallRule" in ps and "-Profile $profiles" in ps
    sh = (DEPLOY / "linux" / "install.sh").read_text(encoding="utf-8")
    assert 'PRIVATE_NETS=("10.0.0.0/8" "172.16.0.0/12" "192.168.0.0/16" "100.64.0.0/10")' in sh
    assert "ufw allow from \"$net\"" in sh and "ufw allow 8600" not in sh


# =========================================================================== textos de entrega
DELIVERABLE_TEXT = [ROOT / "LEEME.md", ROOT / "THIRD_PARTY_NOTICES.txt", *sorted(p for p in (ROOT / "docs").glob("*.md") if p.name != "CONTRATO.md"),
                    *PS_SCRIPTS, *SH_SCRIPTS, *UNITS, *sorted((ROOT / "central").rglob("*.py")),
                    *sorted((ROOT / "central" / "web").rglob("*.*"))]
VOSEO = re.compile(r"\b(vos|tenés|querés|podés|sabés|hacé|poné|fijate|andá|mirá|probá|elegí|escribí|"
                   r"necesitás|tenés que|usá|abrí|cerrá|instalá|ejecutá|revisá)\b", re.IGNORECASE)
FORBIDDEN = re.compile(r"\b(Claude|ChatGPT|Copilot|generad[oa] (por|con) IA|inteligencia artificial generativa)\b",
                       re.IGNORECASE)


@pytest.mark.parametrize("path", [p for p in DELIVERABLE_TEXT if p.suffix not in {".png", ".ico"}],
                         ids=lambda p: str(p.relative_to(ROOT)))
def test_deliverable_text_rules(path: Path) -> None:
    if not path.is_file():
        pytest.skip(f"{path.name} no existe")
    text = path.read_text(encoding="utf-8")
    if path.name == "THIRD_PARTY_NOTICES.txt":  # los textos de licencia de terceros no son nuestros
        text = text.split("3. TEXTOS DE LICENCIA", 1)[0]
    # En Markdown, lo que va entre comillas invertidas es una cita (p. ej. PLAN-V2 §4.1 lista las formas
    # prohibidas). Misma excepción que tests/test_spanish_style.py, que es la comprobación completa.
    plain = re.sub(r"`[^`\n]*`", "``", text) if path.suffix == ".md" else text
    m = VOSEO.search(plain)
    assert not m, f"voseo en {path.name}: «{m.group(0) if m else ''}»"
    m2 = FORBIDDEN.search(text)
    assert not m2, f"mención no permitida en {path.name}: «{m2.group(0) if m2 else ''}»"


# =========================================================================== revisión de seguridad
def test_windows_data_dir_is_locked_down() -> None:
    """Toda la carpeta de datos (grabaciones, users.json, config.json, registros) solo para SYSTEM y
    Administradores; antes solo secrets\\ y .env, y el resto heredaba la lectura de BUILTIN\\Users."""
    ps = (DEPLOY / "windows" / "install.ps1").read_text(encoding="utf-8")
    lock = ps.index("Protect-Path $DataDir\n")
    assert lock < ps.index("Protect-Path (Join-Path $DataDir 'secrets')")
    assert lock > ps.index("New-Item -ItemType Directory -Force -Path (Join-Path $DataDir $sub)")
    body = ps[ps.index("function Protect-Path"):ps.index("function Invoke-Checked")]
    assert "/inheritance:r" in body and "*S-1-5-18" in body and "*S-1-5-32-544" in body
    assert "S-1-5-32-545" not in body   # BUILTIN\\Users nunca


def test_model_weights_are_not_distributed() -> None:
    ps = (DEPLOY / "windows" / "install.ps1").read_text(encoding="utf-8")
    assert "/XD weights /XF *.pth *.pt" in ps
    sh = (DEPLOY / "linux" / "install.sh").read_text(encoding="utf-8")
    assert 'rm -rf "${PREFIX:?}/models/weights"' in sh


def test_notices_include_rfdetr_models() -> None:
    text = (ROOT / "THIRD_PARTY_NOTICES.txt").read_text(encoding="utf-8")
    head = text.split("2. PAQUETES DE PYTHON", 1)[0]
    assert "RF-DETR Nano y Small" in head and "Apache-2.0" in head and "DINOv2" in head
    assert "rfdetr" in text.split("3. TEXTOS DE LICENCIA", 1)[1] and "Apache License" in text


@pytest.mark.parametrize("extra", ["vms", "analytics", "central"])
def test_site_locks_are_pinned_with_hashes(extra: str) -> None:
    """Lo que se instala en las sedes va fijado con «==» y con SHA-256 de PyPI (pip entra en modo
    --require-hashes y rechaza cualquier archivo alterado). Comprobado además con
    `pip download --require-hashes --platform win_amd64|manylinux` de los tres archivos."""
    text = (ROOT / f"requirements-{extra}.txt").read_text(encoding="utf-8")
    entries = re.split(r"\n(?=[a-z0-9])", text.split("\n\n", 1)[1].strip())
    assert len(entries) > 30
    for entry in entries:
        head = entry.split(" \\", 1)[0]
        assert re.match(r"^[a-z0-9][a-z0-9._-]*==[0-9][^ ;]*( ; .+)?$", head), head
        hashes = re.findall(r"--hash=sha256:([0-9a-f]{64})", entry)
        assert hashes, f"{head}: sin hash"
    for script, flag in (("windows/install.ps1", "'--require-hashes'"), ("linux/install.sh", "--require-hashes")):
        assert flag in (DEPLOY / script).read_text(encoding="utf-8")


def test_notices_ignore_hash_lines() -> None:
    from deploy.third_party_notices import locked_packages

    names = locked_packages()
    assert "fastapi" in names and not any(n.startswith("-") for n in names)


def test_powershell_scripts_have_utf8_bom() -> None:
    """Windows PowerShell 5.1 lee los .ps1 sin BOM como ANSI y rompe las tildes."""
    root = Path(__file__).resolve().parents[2] / "deploy"
    scripts = sorted(root.rglob("*.ps1"))
    assert scripts
    for script in scripts:
        assert script.read_bytes().startswith(b"\xef\xbb\xbf"), f"{script} sin BOM UTF-8"


def test_native_probes_do_not_redirect_stderr_under_stop() -> None:
    """Con ErrorActionPreference=Stop, `2>$null` sobre un ejecutable aborta el script en PS 5.1."""
    text = (Path(__file__).resolve().parents[2] / "deploy/windows/install.ps1").read_text(encoding="utf-8-sig")
    body = text.split("function Find-SystemPython312", 1)[1].split("\n}\n", 1)[0]
    assert "2>$null" not in body and "Invoke-Probe" in body
