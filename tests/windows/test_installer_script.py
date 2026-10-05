"""Revisión estática del instalador (``distribution/installer``) en cualquier sistema (PLAN-V2 §4.5).

ISCC solo existe en Windows (job de B3 en CI). Aquí se comprueba lo que se puede sin él: mensajes que faltan o
sobran, codificación, trampas conocidas del preprocesador y de Pascal Script, que el .inf de ejemplo y la guía
coincidan con lo que lee el código, órdenes de vmsctl conformes a CONTRATO §14.1 y textos sin «usted» ni voseo.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests.test_spanish_style import voseo_in

ROOT = Path(__file__).resolve().parents[2]
INST = ROOT / "distribution" / "installer"
ISS = INST / "VMSMultimarca.iss"
PASCAL = sorted((INST / "pascal").glob("*.pas"))
LANGS = {"es": INST / "lang" / "es.isl", "en": INST / "lang" / "en.isl"}
DOC = ROOT / "docs" / "INSTALACION-WINDOWS.md"


def _text(p: Path) -> str:
    return p.read_text(encoding="utf-8-sig")


def _code() -> str:
    return "\n".join(_text(p) for p in PASCAL)


def _isl(path: Path, section: str) -> dict[str, str]:
    out: dict[str, str] = {}
    current = None
    for line in _text(path).splitlines():
        m = re.match(r"^\[(.+)\]\s*$", line)
        if m:
            current = m.group(1)
            continue
        if current == section and "=" in line and not line.lstrip().startswith(";"):
            k, v = line.split("=", 1)
            out[k.strip()] = v
    return out


def _used_messages() -> set[str]:
    used = set(re.findall(r"CustomMessage\('(\w+)'\)", _code()))
    used |= set(re.findall(r"\{cm:(\w+)\}", _text(ISS)))
    return used


# ----------------------------------------------------------------------------------------------- mensajes
@pytest.mark.parametrize("lang", sorted(LANGS))
def test_every_message_exists_and_none_is_unused(lang: str) -> None:
    defined = set(_isl(LANGS[lang], "CustomMessages"))
    used = _used_messages()
    assert not used - defined, f"faltan en {lang}.isl: {sorted(used - defined)}"
    assert not defined - used, f"sobran en {lang}.isl: {sorted(defined - used)}"


def test_placeholders_match_between_languages() -> None:
    es, en = _isl(LANGS["es"], "CustomMessages"), _isl(LANGS["en"], "CustomMessages")
    for key, value in es.items():
        assert set(re.findall(r"%\d", value)) == set(re.findall(r"%\d", en[key])), key


def test_spanish_texts_use_tu_not_usted() -> None:
    usted = re.compile(r"\b(usted|ud\.|haga clic|desea|seleccione|introduzca|por favor|inténtelo|ciérrelo|"
                       r"espere|lea|reinicie)\b", re.IGNORECASE)
    for section in ("Messages", "CustomMessages"):
        for key, value in _isl(LANGS["es"], section).items():
            assert not usted.search(value), f"{section}.{key}: «{value}» (tuteo, no usted)"


@pytest.mark.parametrize("path", [ISS, *PASCAL, *LANGS.values()], ids=lambda p: p.name)
def test_no_voseo_in_installer_sources(path: Path) -> None:
    """``tests/test_spanish_style.py`` no revisa ``.pas``: aquí sí (registros y comentarios en español)."""
    assert not voseo_in(_text(path)), path.name


# ----------------------------------------------------------------------------------------------- codificación
@pytest.mark.parametrize("path", [ISS, *PASCAL, *LANGS.values(), *sorted((INST / "lang").glob("LICENCIA-*.txt"))],
                         ids=lambda p: p.name)
def test_utf8_with_bom(path: Path) -> None:
    """Inno Setup lee UTF-8 con BOM sin ambigüedad (tildes, «», ©)."""
    data = path.read_bytes()
    assert data.startswith(b"\xef\xbb\xbf"), f"{path.name}: falta el BOM UTF-8"
    data.decode("utf-8")


# ----------------------------------------------------------------------------------------------- trampas
@pytest.mark.parametrize("path", PASCAL, ids=lambda p: p.name)
def test_ispp_would_not_misread_a_line(path: Path) -> None:
    """ISPP toma como directiva toda línea que empieza por «#» (p. ej. una continuación «#13#10, ...»)."""
    for n, line in enumerate(_text(path).splitlines(), 1):
        s = line.strip()
        # El compilador de Inno toma «[...]» al principio de línea como sección, aunque esté dentro de [Code]
        # (pasó con una continuación «[AddBackslash(...)]»), y «;» como comentario.
        assert not s.startswith(("[", ";")), f"{path.name}:{n}: una línea no puede empezar por «[» ni «;»: {s}"
        if s.startswith("#"):
            assert re.match(r"#(ifdef|ifndef|else|endif|include|define)\b", s), f"{path.name}:{n}: {s}"


def _tokens(code: str) -> tuple[str, list[str]]:
    """(código sin comentarios ni cadenas, comentarios). Recorre el texto en orden, como el compilador: lo que
    abre primero (comentario «{», «//» o cadena «'») manda hasta su cierre."""
    out: list[str] = []
    comments: list[str] = []
    i, n = 0, len(code)
    while i < n:
        c = code[i]
        if c == "{" and not code.startswith("{#", i):
            j = code.find("}", i)
            j = n - 1 if j < 0 else j
            comments.append(code[i:j + 1])
            out.append(" ")
            i = j + 1
        elif code.startswith("//", i):
            j = code.find("\n", i)
            j = n if j < 0 else j
            comments.append(code[i:j])
            i = j
        elif c == "'":
            j = i + 1
            while j < n:
                if code.startswith("''", j):
                    j += 2
                    continue
                if code[j] == "'":
                    break
                j += 1
            out.append("''")
            i = j + 1
        else:
            out.append(c)
            i += 1
    return "".join(out), comments


@pytest.mark.parametrize("path", PASCAL, ids=lambda p: p.name)
def test_pascal_script_pitfalls(path: Path) -> None:
    code, comments = _tokens(_text(path))
    # Un «{» dentro de un comentario { ... } lo cierra antes de tiempo (pasó con «{2,39}»).
    for comment in comments:
        if comment.startswith("{"):
            assert "{" not in comment[1:], f"{path.name}: llave dentro de un comentario: {comment[:60]}"
    assert not re.search(r"\bExit\s*\(", code, re.I), "Pascal Script no admite Exit(valor)"
    assert not re.search(r"\bfor\s+\w+\s+in\b", code, re.I), "Pascal Script no admite for..in"
    assert not re.search(r"\bString\s*\(", code), "conversión String(...) de Variant: asignar a variable"
    # Sin rutinas anidadas: entre «procedure/function» de nivel 0 y su «end;» final no empieza otra.
    depth = 0
    for line in code.splitlines():
        if re.match(r"^(procedure|function)\b", line):
            assert depth == 0, f"{path.name}: rutina anidada o «end;» que falta antes de: {line.strip()}"
            depth = 1
        elif depth and re.match(r"^end;\s*$", line):
            depth = 0


def test_all_pascal_files_are_included_in_order() -> None:
    includes = re.findall(r'#include "pascal\\(\w+\.pas)"', _text(ISS))
    assert sorted(includes) == [p.name for p in PASCAL]
    assert includes[:2] == ["util.pas", "state.pas"], "util y state primero: el resto los usa"


# ----------------------------------------------------------------------------------------------- [Setup]
def test_setup_directives() -> None:
    iss = _text(ISS)
    plain = re.search(r'#define AppIdPlain "(\{[0-9A-F-]+\})"', iss)
    assert plain is not None
    assert f"AppId={{{plain.group(1)}" in iss, "AppId y AppIdPlain deben ser el mismo GUID"
    for directive in ("SetupArchitecture=x64", "PrivilegesRequired=admin", "LanguageDetectionMethod=none",
                      "DisableDirPage=yes", r"DefaultDirName={commonpf64}\VMSMultimarca", "CloseApplications=no",
                      "MinVersion=10.0.19045"):
        assert directive in iss, directive
    types = re.findall(r'^Name: "(\w+)"; Description: "\{cm:Type', iss, re.M)
    assert types == ["control", "store", "central", "viewer"], "tipos de PLAN-V2 §1.4 = roles de vmsctl"
    # La firma solo existe si la build la pide (decisión N1): nunca una SignTool fija.
    sign_lines = [ln for ln in iss.splitlines() if ln.startswith("SignTool")]
    assert sign_lines == ["SignTool={#SignToolName}"]


def test_test_build_hooks_are_only_in_test_builds() -> None:
    """Los parámetros de simulación (/SIMULATEWINBUILD, /MINFREEGB, /CAPTURESTATE) no existen en producción."""
    code = _code()
    for param in ("SIMULATEWINBUILD", "MINFREEGB", "CAPTURESTATE"):
        for m in re.finditer(param, code):
            before = code[:m.start()]
            assert before.rfind("#ifdef TestBuild") > before.rfind("#endif"), f"{param} fuera de #ifdef TestBuild"


# ----------------------------------------------------------------------------------------------- vmsctl
CONTRACT_COMMANDS = {
    # CONTRATO §14.1
    "services install", "services uninstall", "services start", "services stop", "services restart",
    "firewall apply", "firewall remove", "acl apply", "ports check", "health wait", "version switch",
    "version show", "update check", "update status", "update rollback", "tls setup", "kiosk rotate",
    "diag bundle", "migrate-from-v1",
    # pedidos a B1 en el informe de B3 (cerrojo por la CLI)
    "update lock", "update unlock",
}


def test_vmsctl_calls_follow_the_contract() -> None:
    calls = re.findall(r"RunVmsctl\([^,]+,\s*'([^']+)'", _code())
    calls += re.findall(r"VmsctlStep\([^,]+,\s*'([^']+)'", _code())
    assert calls, "no se encontraron llamadas a vmsctl"
    for args in calls:
        words = args.split()
        command = words[0] if words[0] == "migrate-from-v1" else " ".join(words[:2])
        assert command in CONTRACT_COMMANDS, args
    assert "--json" in _text(INST / "pascal" / "vmsctl.pas"), "toda llamada lleva --json (§14.2)"


# ----------------------------------------------------------------------------------------------- silenciosa
def _inf_keys(path: Path) -> set[str]:
    keys = set()
    for line in _text(path).splitlines():
        if "=" in line and not line.lstrip().startswith(";") and not line.startswith("["):
            keys.add(line.split("=", 1)[0].strip())
    return keys


def test_example_inf_matches_code_and_docs() -> None:
    code = _code()
    read = set(re.findall(r"InfGet(?:Bool)?\('(\w+)'", code))
    example = _inf_keys(INST / "ejemplos" / "tienda.inf") - {"SetupType", "Tasks"}
    assert example == read, f"ejemplo vs código: {sorted(example ^ read)}"
    doc = _text(DOC)
    for key in read:
        assert f"`{key}`" in doc, f"{key} no está documentada en docs/INSTALACION-WINDOWS.md"


def test_secrets_example_matches_code_and_docs() -> None:
    keys = set(json.loads(_text(INST / "ejemplos" / "secrets.json")))
    read = set(re.findall(r"JsonGetString\(Json, '(\w+)'\)", _text(INST / "pascal" / "state.pas")))
    assert keys == read == {"admin_password", "site_token", "pg_dsn"}
    for key in keys:
        assert f"`{key}`" in _text(DOC)


def test_documented_switches_exist() -> None:
    code, doc = _code(), _text(DOC)
    for switch in ("PURGE", "ALLOWDOWNGRADE", "SECRETS", "LOADINF"):
        assert switch in code and f"/{switch}" in doc


# ----------------------------------------------------------------------------------------------- recursos
def test_assets_are_up_to_date() -> None:
    from distribution.installer.assets.make_assets import main as make_assets

    assert make_assets(["--check"]) == 0


def _env_quote(value: str) -> str:
    """Mismo algoritmo que EnvQuote (util.pas): comillas simples, solo se escapan \\ y '."""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


@pytest.mark.parametrize("value", ["sencilla", "con espacio # y almohadilla", "comilla ' simple", 'doble "',
                                   "barra \\ final\\", "ñandú €", "$VAR $OTRA", "=igual="])
def test_env_quoting_round_trips_through_dotenv(tmp_path: Path, value: str) -> None:
    from dotenv import dotenv_values

    env = tmp_path / ".env"
    env.write_text(f"VMS_ADMIN_INITIAL_PASSWORD={_env_quote(value)}\n", encoding="utf-8")
    assert dotenv_values(env)["VMS_ADMIN_INITIAL_PASSWORD"] == value
    pas = _text(INST / "pascal" / "util.pas")
    assert "Result := Result + '\\\\'" in pas and "Result := Result + '\\'''" in pas


def test_dollar_brace_is_rejected_because_dotenv_expands_it(tmp_path: Path) -> None:
    """python-dotenv expande «${X}» incluso entre comillas simples: el instalador lo rechaza (IsEnvSafe)."""
    from dotenv import dotenv_values

    env = tmp_path / ".env"
    env.write_text("A='x${NO_EXISTE}y'\n", encoding="utf-8")
    assert dotenv_values(env)["A"] == "xy"
    util = _text(INST / "pascal" / "util.pas")
    assert "function IsEnvSafe" in util and "Pos('${', S) = 0" in util
    pages = _text(INST / "pascal" / "pages.pas")
    for value in ("AdminPassword", "SiteToken", "CentralUrl", "PgDsn"):
        assert re.search(rf"IsEnvSafe\({value}\)", pages), value
