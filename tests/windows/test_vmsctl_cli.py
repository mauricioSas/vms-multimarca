"""Cada llamada del instalador a ``vmsctl`` se comprueba contra la CLI del ``vmsctl`` REAL (``cli_spec.json``).

Antes el e2e de Windows usaba un doble que aceptaba órdenes que el real no tenía (``ports check --http-port``,
``update lock``) y CI estaba verde con el flujo real roto (hallazgos C1, C2, M1 y M2 de la revisión). Ahora:

- ``native/vmsctl/src/cli_spec.json`` es la tabla del real (``vmsctl`` rechaza lo que no esté en ella);
- el doble (``tests/windows/doubles``) la incluye tal cual: no puede aceptar más que el real;
- aquí se leen las llamadas del ``[Code]`` del instalador (``RunVmsctl`` y ``VmsctlStep``) y se validan con la
  misma semántica que ``native/vmsctl/src/clispec.rs``.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC_FILE = ROOT / "native" / "vmsctl" / "src" / "cli_spec.json"
PASCAL = sorted((ROOT / "distribution" / "installer" / "pascal").glob("*.pas"))
DOUBLE = ROOT / "tests" / "windows" / "doubles" / "src" / "lib.rs"

#: Valores de ejemplo de las expresiones de Pascal que no son literales (el resto se sustituye por «X»).
SAMPLES: dict[str, list[str]] = {
    "Role": ["store", "control", "central", "viewer"],
    "FirewallProfiles": ["private", "private,domain"],
    "Purge": ["", " --purge"],
    "RecordingsArg": ["", ' --recordings-dir "D:\\Grabaciones CCTV"'],
    "HttpPort": ["8600"],
    "HttpsPort": ["8643"],
}


def spec() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(SPEC_FILE.read_text(encoding="utf-8"))
    return data


def check(argv: list[str], cli: dict[str, Any] | None = None) -> str | None:
    """Lo mismo que ``clispec::validate`` (Rust). Devuelve el error o None."""
    cli = cli or spec()
    g_values, g_switches = set(cli["global"]["values"]), set(cli["global"]["switches"])
    takes_value = g_values | {v for c in cli["commands"] for v in c.get("values", [])}
    words: list[str] = []
    values: dict[str, str] = {}
    switches: list[str] = []
    it = iter(argv)
    for tok in it:
        if tok.startswith("--") and "=" in tok:
            k, v = tok.split("=", 1)
            values[k] = v
        elif tok in takes_value:
            nxt = next(it, None)
            if nxt is None:
                return f"falta el valor de {tok}"
            values[tok] = nxt
        elif tok.startswith("--"):
            switches.append(tok)
        else:
            words.append(tok)
    cands = [c for c in cli["commands"] if c["path"] == words[:len(c["path"])]]
    if not cands:
        return f"orden desconocida {words}"
    cmd = max(cands, key=lambda c: len(c["path"]))
    name = " ".join(cmd["path"])
    if len(words) - len(cmd["path"]) != cmd.get("positional", 0):
        if not cmd.get("positional") and len(cmd["path"]) < 2 and cmd["path"] != ["help"]:
            return f"orden desconocida {words}"
        return f"«{name}»: argumentos {words[len(cmd['path']):]}"
    for sw in switches:
        if sw not in g_switches | set(cmd.get("switches", [])):
            return f"opción desconocida para «{name}»: {sw}"
    for v in values:
        if v not in g_values | set(cmd.get("values", [])):
            return f"opción desconocida para «{name}»: {v}"
    for r in cmd.get("required", []):
        if r not in values:
            return f"«{name}» necesita {r}"
    return None


# ------------------------------------------------------------------------------------------- llamadas del [Code]
def _strip_comments(code: str) -> str:
    out, i, n = [], 0, len(code)
    while i < n:
        if code[i] == "'":
            j = i + 1
            while j < n and not (code[j] == "'" and not code.startswith("''", j)):
                j += 2 if code.startswith("''", j) else 1
            out.append(code[i:j + 1])
            i = j + 1
        elif code[i] == "{" and not code.startswith("{#", i):
            i = code.find("}", i) + 1 or n
        elif code.startswith("//", i):
            i = code.find("\n", i) if code.find("\n", i) >= 0 else n
        else:
            out.append(code[i])
            i += 1
    return "".join(out)


def _split_top(expr: str, sep: str) -> list[str]:
    parts, depth, cur, quoted = [], 0, "", False
    for ch in expr:
        if ch == "'":
            quoted = not quoted
        elif not quoted and ch == "(":
            depth += 1
        elif not quoted and ch == ")":
            depth -= 1
        if ch == sep and depth == 0 and not quoted:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    parts.append(cur)
    return parts


@dataclass(frozen=True)
class Call:
    where: str
    func: str
    expr: str

    def expansions(self) -> list[str]:
        """Cada valor posible de la expresión de argumentos (literales + ejemplos de las variables)."""
        results = [""]
        for term in _split_top(self.expr, "+"):
            t = term.strip()
            if t.startswith("'") and t.endswith("'"):
                options = [t[1:-1].replace("''", "'")]
            else:
                key = re.sub(r"\(.*", "", t).strip()
                if key == "AddQuotes":
                    options = ['"X Y"']
                elif key in ("IntToStr", "Lowercase"):
                    options = ["x"]
                else:
                    options = SAMPLES.get(key, ["X"])
            results = [r + o for r in results for o in options]
        return results


def installer_calls() -> list[Call]:
    calls: list[Call] = []
    for path in PASCAL:
        code = _strip_comments(path.read_text(encoding="utf-8-sig"))
        for m in re.finditer(r"\b(RunVmsctl|VmsctlStep)\s*\(", code):
            if code[:m.start()].rstrip().endswith(("function", "procedure")):
                continue
            depth, j = 1, m.end()
            quoted = False
            while depth:
                ch = code[j]
                if ch == "'":
                    quoted = not quoted
                elif not quoted and ch == "(":
                    depth += 1
                elif not quoted and ch == ")":
                    depth -= 1
                j += 1
            args = _split_top(code[m.end():j - 1], ",")
            if "'" not in args[-1]:
                continue        # VmsctlStep pasa su parámetro «Args» tal cual: ya se comprueba en cada llamada
            line = code[:m.start()].count("\n") + 1
            calls.append(Call(f"{path.name}:{line}", m.group(1), " ".join(args[-1].split())))
    return calls


def argv_of(text: str) -> list[str]:
    """Como la línea de órdenes de Windows para estos casos: comillas dobles agrupan."""
    return [a.strip('"') for a in re.findall(r'"[^"]*"|\S+', text)]


def test_the_parser_finds_the_installer_calls() -> None:
    calls = installer_calls()
    assert len(calls) >= 15, [c.expr for c in calls]
    names = {" ".join(argv_of(c.expansions()[0])[:2]) for c in calls}
    assert {"services install", "acl apply", "ports check", "update lock", "services uninstall"} <= names, names


@pytest.mark.parametrize("call", installer_calls(), ids=lambda c: c.where)
def test_every_installer_call_is_accepted_by_the_real_vmsctl(call: Call) -> None:
    for text in call.expansions():
        err = check(argv_of(text) + ["--json"])
        assert err is None, f"{call.where}: «vmsctl {text}» → {err}"


def _calls_by_command() -> dict[str, list[list[str]]]:
    out: dict[str, list[list[str]]] = {}
    for c in installer_calls():
        for text in c.expansions():
            argv = argv_of(text)
            words = [a for a in argv if not a.startswith("--")][:2]
            out.setdefault(" ".join(words), []).append(argv)
    return out


def test_ports_check_passes_the_role_and_the_ports_of_the_net_page() -> None:
    """C1: en una instalación nueva el registro aún no tiene ``Role`` (lo escribe ``WriteRegistryState`` después):
    sin ``--role`` el ``vmsctl`` real responde «indica el tipo de puesto» (2) y el instalador no sigue."""
    for argv in _calls_by_command()["ports check"]:
        assert "--role" in argv and "--http-port" in argv and "--https-port" in argv, argv


def test_no_version_switch_on_a_new_install() -> None:
    """C2: ``version switch`` necesita un puntero previo (``active.json`` o ``journal.json``) y en una instalación
    nueva no hay: fallaba con 20 y se saltaban servicios, ACL, firewall y arranque. ``services install`` ya escribe
    el puntero a su versión (``point_to``)."""
    assert "version switch" not in _calls_by_command()
    code = "\n".join(_strip_comments(p.read_text(encoding="utf-8-sig")) for p in PASCAL)
    post = code[code.index("procedure PostInstall"):code.index("procedure FillResultPage")]
    order = [post.index(f"'{c}") for c in ("services install", "acl apply", "firewall apply", "services start")]
    assert order == sorted(order), "services install escribe el puntero: va antes que el resto"


def test_recordings_dir_reaches_services_install_and_acl() -> None:
    """M2: la carpeta de grabaciones externa (p. ej. D:\\Grabaciones) recibe su ACL por SID."""
    by_cmd = _calls_by_command()
    for cmd in ("services install", "acl apply"):
        assert any("--recordings-dir" in argv for argv in by_cmd[cmd]), cmd


def test_lock_uses_the_real_options() -> None:
    """M1: ``update lock --owner installer --ttl 3600`` y ``update unlock --owner installer``."""
    by_cmd = _calls_by_command()
    assert by_cmd["update lock"] and all(check(a) is None for a in by_cmd["update lock"] + by_cmd["update unlock"])


@pytest.mark.parametrize("line, why", [
    ("ports check --http-port", "falta el valor"),
    ("acl apply --profiles private", "opción desconocida"),
    ("version switch", "argumentos"),
    ("update lock --ttl 5", "necesita --owner"),
    ("frobnicate", "orden desconocida"),
])
def test_the_checker_rejects_like_the_real_one(line: str, why: str) -> None:
    err = check(line.split())
    assert err is not None and why in err, err


def test_old_installer_calls_would_have_been_caught() -> None:
    """Las llamadas de antes de la revisión, con la CLI que tenía entonces el real (sin --http-port ni lock)."""
    old = spec()
    for c in old["commands"]:
        if c["path"] == ["ports", "check"]:
            c["values"] = ["--role"]
    old["commands"] = [c for c in old["commands"] if c["path"][:2] not in (["update", "lock"], ["update", "unlock"])]
    assert check("ports check --http-port 8600 --https-port 8643".split(), old) is not None
    assert check("update lock --owner installer --ttl 3600".split(), old) is not None


def test_the_double_takes_the_real_cli() -> None:
    """El doble no puede aceptar nada que el real no acepte: incluye la tabla del real y no tiene otra."""
    src = DOUBLE.read_text(encoding="utf-8")
    assert 'include_str!("../../../../native/vmsctl/src/cli_spec.json")' in src
    assert "const SPECS" not in src, "el doble no puede llevar su propia tabla de órdenes"
