"""Comprobación del ``[Code]`` del instalador con Free Pascal, sin Windows (herramienta de desarrollo, B3).

ISCC solo existe en Windows; para no gastar una ejecución de CI por cada errata de Pascal Script, esta orden
compila el ``[Code]`` (con los ``#include`` y ``#ifdef`` resueltos) junto a un «doble» de la API de Inno Setup
7.1.0 generado a partir de su propia documentación (``ISHelp/isxclasses.pas`` e ``ISHelp/isxfunc.xml`` de la
etiqueta ``is-7_1_0``, descargados con SHA-256 fijado). Detecta erratas, identificadores sin declarar, tipos que
no encajan y llamadas con argumentos de más o de menos.

No sustituye a ISCC: Pascal Script admite cosas que Free Pascal no y al revés. La prueba definitiva es la
compilación con ISCC en ``windows-latest`` (job de B3).

    VMS_FPC=/ruta/a/ppca64 VMS_FPC_UNITS=<units>/rtl:<units>/rtl-objpas python -m tools.build pascal-check [--keep]
"""
from __future__ import annotations

import argparse
import hashlib
import html
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from . import ISS_FILE, ROOT

ISSRC_TAG = "is-7_1_0"
SOURCES = {
    "isxclasses.pas": "10cc00fbe161869ca9bacd085876737e612ac9e532690deef30db6d960074fd9",
    "isxfunc.xml": "e68171bc32b8075fff4b36cbbd762f73af2c415046b26b39593b48f26bb485c8",
}
CACHE = ROOT / ".tmp" / "pascal-check"

#: Funciones que Free Pascal ya trae de serie (no se redeclaran).
BUILTINS = {"chr", "ord", "copy", "length", "delete", "insert", "pos", "setlength", "low", "high", "assigned",
            "round", "trunc", "inc", "dec", "abs", "exit", "break", "continue", "sizeof", "str", "val", "odd",
            "succ", "pred", "random", "randomize", "include", "exclude"}

PRELUDE = r"""
{$mode delphi}{$H+}
{$WARN 5024 OFF}{$WARN 5025 OFF}{$WARN 5026 OFF}{$WARN 5027 OFF}{$WARN 5028 OFF}{$WARN 5036 OFF}{$WARN 5057 OFF}
{$WARN 5091 OFF}{$WARN 5092 OFF}{$WARN 4046 OFF}{$WARN 3018 OFF}{$WARN 4056 OFF}{$WARN 4082 OFF}
program innocheck;
type
  AnyString = String;
  Ordinal = Int64;
  TArrayOfString = array of String;
  TArrayOfChar = array of Char;
  TArrayOfBoolean = array of Boolean;
  TArrayOfInteger = array of Integer;
  HKEY = NativeUInt;
  HMODULE = NativeUInt;
  HRESULT = Longint;
  LONG = Longint;
  TExecWait = (ewNoWait, ewWaitUntilTerminated, ewWaitUntilIdle);
  TExecOutput = record StdOut: TArrayOfString; StdErr: TArrayOfString; Error: Boolean; end;
  TMsgBoxType = (mbInformation, mbConfirmation, mbError, mbCriticalError);
  TSetupStep = (ssPreInstall, ssInstall, ssPostInstall, ssDone);
  TUninstallStep = (usAppMutexCheck, usUninstall, usPostUninstall, usDone);
  TSplitType = (stAll, stExcludeEmpty, stExcludeLastEmpty);
  TWindowsVersion = record Major, Minor, Build, ServicePackMajor, ServicePackMinor: Cardinal; NTPlatform: Boolean;
    ProductType: Byte; SuiteMask: Word; end;
  TFindRec = record Name: String; Attributes: LongWord; SizeHigh, SizeLow: LongWord; Size: Int64;
    CreationTime, LastAccessTime, LastWriteTime: Int64; AlternateName: String; FindHandle: NativeUInt; end;
  TOnLog = procedure(const S: String; const Error, FirstLine: Boolean);
  TOnDownloadProgress = function(const Url, FileName: String; const Progress, ProgressMax: Int64): Boolean;
  TOnExtractionProgress = function(const ArchiveName, FileName: String; const Progress, ProgressMax: Int64): Boolean;
  TPathRedirTargetProcess = (tpCurrent, tpNativeBit, tp32Bit);
  AnyMethod = Pointer;
  TFileTime = Int64;
  TRect = record Left, Top, Right, Bottom: Integer; end;
  TSetupMessageID = Integer;
  TArrayOfGraphic = array of Pointer;
  TSetupProcessorArchitecture = (paUnknown, paX86, paX64, paArm32, paArm64);
  TDotNetVersion = (net11, net20, net30, net35, net4Client, net4Full, net45, net451, net452, net46, net461,
    net462, net47, net471, net472, net48, net481);
  WPARAM = NativeUInt;
  LPARAM = NativeInt;
  LRESULT = NativeInt;
const
  HKLM = 1; HKCU = 2; HKCR = 3; HKU = 4; HKCC = 5; HKA = 6; HKLM32 = 7; HKLM64 = 8; HKCU32 = 9; HKCU64 = 10;
  HKEY_LOCAL_MACHINE = 1; HKEY_CURRENT_USER = 2;
  wpWelcome = 1; wpLicense = 2; wpPassword = 3; wpInfoBefore = 4; wpUserInfo = 5; wpSelectDir = 6;
  wpSelectComponents = 7; wpSelectProgramGroup = 8; wpSelectTasks = 9; wpReady = 10; wpPreparing = 11;
  wpInstalling = 12; wpInfoAfter = 13; wpFinished = 14;
  SW_SHOW = 5; SW_SHOWNORMAL = 1; SW_SHOWMAXIMIZED = 3; SW_SHOWMINIMIZED = 2; SW_SHOWMINNOACTIVE = 7; SW_HIDE = 0;
  MB_OK = 0; MB_OKCANCEL = 1; MB_ABORTRETRYIGNORE = 2; MB_YESNOCANCEL = 3; MB_YESNO = 4; MB_RETRYCANCEL = 5;
  MB_DEFBUTTON1 = 0; MB_DEFBUTTON2 = $100; MB_DEFBUTTON3 = $200; MB_SETFOREGROUND = $10000;
  IDOK = 1; IDCANCEL = 2; IDABORT = 3; IDRETRY = 4; IDIGNORE = 5; IDYES = 6; IDNO = 7;
  VER_NT_WORKSTATION = 1; VER_NT_DOMAIN_CONTROLLER = 2; VER_NT_SERVER = 3;
var
  WizardForm: TWizardForm;
  UninstallProgressForm: TUninstallProgressForm;
"""


def _fetch(name: str) -> Path:
    path = CACHE / name
    if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == SOURCES[name]:
        return path
    import httpx

    url = f"https://raw.githubusercontent.com/jrsoftware/issrc/{ISSRC_TAG}/ISHelp/{name}"
    data = httpx.get(url, follow_redirects=True, timeout=60).raise_for_status().content
    got = hashlib.sha256(data).hexdigest()
    if got != SOURCES[name]:
        raise SystemExit(f"SHA-256 inesperado para {url}: {got}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


# ------------------------------------------------------------------------------------------- clases
_PROP = re.compile(r"^\s*property\s+(\w+)(\[[^\]]*\])?\s*:\s*([^;]+);\s*(read)?\s*(write)?\s*;?\s*$", re.I)
_ENUM = re.compile(r"^(\w+)\s*=\s*\(([^)]*)\);\s*$")
_CLASS = re.compile(r"^(\w+)\s*=\s*class(\((\w+)\))?\s*$")


def classes_to_pascal(text: str, taken: set[str]) -> tuple[str, str, list[str]]:
    """``isxclasses.pas`` (documentación) → declaraciones que Free Pascal compila.

    Devuelve (tipos sencillos, clases, nombres de clase): primero van los tipos sencillos (enumerados,
    conjuntos, alias y procedimientos) y después los cuerpos de las clases, con declaraciones adelantadas.
    """
    text = re.sub(r"\{[^}]*\}", "", text)
    out: list[str] = []
    simple: list[str] = []
    class_names: list[str] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        short = re.match(r"^(\w+)\s*=\s*class\((\w+)\);\s*$", line.strip())
        if short:                                      # «TX = class(TY);»: clase sin miembros
            class_names.append(short.group(1))
            out += [f"  {short.group(1)} = class({short.group(2)})", "  end;"]
            i += 1
            continue
        m = _CLASS.match(line.strip())
        if m:
            name, parent = m.group(1), m.group(3)
            class_names.append(name)
            fields, members = [], []
            i += 1
            while i < len(lines) and lines[i].strip() != "end;":
                member = lines[i].strip()
                i += 1
                if not member:
                    continue
                pm = _PROP.match(member)
                if pm:
                    pname, index, ptype, rd, wr = pm.groups()
                    ptype = ptype.strip()
                    if " " in ptype:                   # errata de la documentación («Integer Integer Integer»)
                        continue
                    if index:
                        params = index.strip("[]")
                        members.append(f"    function Get{pname}({params}): {ptype}; virtual; abstract;")
                        accessors = f"read Get{pname}"
                        if wr:
                            members.append(f"    procedure Set{pname}({params}; Value: {ptype}); virtual; abstract;")
                            accessors += f" write Set{pname}"
                        members.append(f"    property {pname}[{params}]: {ptype} {accessors};")
                    else:
                        fields.append(f"    F{pname}: {ptype};")
                        members.append(f"    property {pname}: {ptype} read F{pname}"
                                       + (f" write F{pname};" if wr else ";"))
                    continue
                if re.match(r"^(function|procedure|constructor)\b", member, re.I):
                    decl = member.rstrip(";")
                    if decl.lower().startswith("constructor"):
                        members.append(f"    {decl}; reintroduce; virtual;")
                    else:
                        members.append(f"    {decl}; virtual; abstract;")
                    continue
            i += 1
            head = f"  {name} = class({parent})" if parent and name != "TObject" else f"  {name} = class"
            body = ["  private", *fields, "  public", *members, "  end;"]
            out += [head, *body]
            continue
        em = _ENUM.match(line.strip())
        if em:
            name, items = em.group(1), [x.strip() for x in em.group(2).split(",")]
            fixed = []
            for it in items:
                key = it.lower()
                if key in taken:
                    it = f"{it}_{name}"
                taken.add(it.lower())
                fixed.append(it)
            simple.append(f"  {name} = ({', '.join(fixed)});")
        elif "=" in line and line.strip().endswith(";"):
            simple.append("  " + line.strip())
        i += 1
    return "\n".join(simple), "\n".join(out), class_names


def _constructor_bodies(text: str) -> str:
    """Los constructores no pueden ser abstractos: se les da cuerpo vacío."""
    bodies = []
    current = None
    for line in text.splitlines():
        m = re.match(r"^\s*(\w+) = class", line)
        if m:
            current = m.group(1)
        c = re.match(r"^\s*constructor (\w+)(\([^)]*\))?; reintroduce; virtual;", line)
        if c and current:
            bodies.append(f"constructor {current}.{c.group(1)}{c.group(2) or ''}; begin end;")
    return "\n".join(bodies)


# ------------------------------------------------------------------------------------------- funciones
def functions_to_pascal(xml: str) -> str:
    out = []
    seen = set()
    for proto in re.findall(r"<prototype>(.*?)</prototype>", xml, re.S):
        proto = html.unescape(re.sub(r"<[^>]+>", "", proto)).strip()
        m = re.match(r"^(function|procedure)\s+(\w+)", proto, re.I)
        if not m or m.group(2).lower() in BUILTINS or m.group(2).lower() in seen:
            continue
        seen.add(m.group(2).lower())
        decl = proto.rstrip(";")
        # «var Arr: Array» (sin tipo en Pascal Script) → parámetro sin tipo en Free Pascal.
        decl = re.sub(r"\bvar (\w+): Array\b", r"var \1", decl)
        decl = re.sub(r"\b(var |const )?Result:", r"\1AResult:", decl)
        # Un parámetro con el mismo nombre que su tipo («WParam: WPARAM») no compila en Free Pascal.
        decl = re.sub(r"\b(\w+): (\w+)\b", lambda m: f"A{m.group(1)}: {m.group(2)}"
                      if m.group(1).lower() == m.group(2).lower() else m.group(0), decl)
        out.append(f"{decl}; begin end;")
    return "\n".join(out)


# ------------------------------------------------------------------------------------------- [Code]
def preprocess(iss: Path, defines: dict[str, str]) -> str:
    """Saca el [Code] y resuelve #include, #ifdef/#ifndef/#else/#endif y {#Nombre} (subconjunto de ISPP)."""
    text = iss.read_text(encoding="utf-8-sig")
    code = re.split(r"(?m)^\[Code\]\s*$", text, maxsplit=1)[1]
    lines: list[str] = []
    for raw in code.splitlines():
        inc = re.match(r'^\s*#include\s+"([^"]+)"', raw)
        if inc:
            path = iss.parent / inc.group(1).replace("\\", "/")
            lines += path.read_text(encoding="utf-8-sig").splitlines()
        else:
            lines.append(raw)
    out: list[str] = []
    stack: list[bool] = []
    for line in lines:
        s = line.strip()
        d = re.match(r"^#(ifdef|ifndef)\s+(\w+)", s)
        if d:
            cond = (d.group(2) in defines) == (d.group(1) == "ifdef")
            stack.append(cond)
            out.append("")
            continue
        if s.startswith("#else"):
            stack[-1] = not stack[-1]
            out.append("")
            continue
        if s.startswith("#endif"):
            stack.pop()
            out.append("")
            continue
        if all(stack):
            out.append(re.sub(r"\{#(\w+)\}", lambda m: defines.get(m.group(1), m.group(0)), line))
        else:
            out.append("")
    return "\n".join(out)


def build_program(defines: dict[str, str]) -> str:
    classes_src = _fetch("isxclasses.pas").read_text(encoding="utf-8-sig")
    funcs_src = _fetch("isxfunc.xml").read_text(encoding="utf-8-sig")
    taken = {x.lower() for x in re.findall(r"\b(ew\w+|mb\w+|ss\w+|us\w+|st\w+|tp\w+)\b",
                                            PRELUDE.split("const")[0])}
    simple, types, names = classes_to_pascal(classes_src, taken)
    forwards = "\n".join(f"  {n} = class;" for n in names)
    prelude = PRELUDE.replace("const\n", "\n".join(["", forwards, simple, types, "const", ""]), 1)
    return (prelude + "\n" + _constructor_bodies(types) + "\n" + functions_to_pascal(funcs_src) + "\n"
            + preprocess(ISS_FILE, defines) + "\nbegin\nend.\n")


def find_fpc() -> str | None:
    explicit = os.environ.get("VMS_FPC")
    if explicit and Path(explicit).is_file():
        return explicit
    for name in ("ppca64", "ppcx64", "fpc"):
        found = shutil.which(name)
        if found:
            return found
    return None


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m tools.build pascal-check")
    p.add_argument("--keep", action="store_true", help="deja el programa generado en .tmp/pascal-check")
    args = p.parse_args(argv)
    fpc = find_fpc()
    if fpc is None:
        print("No encuentro Free Pascal (VMS_FPC). Es una herramienta de desarrollo opcional: la comprobación "
              "definitiva es ISCC en windows-latest.", file=sys.stderr)
        return 2
    units = os.environ.get("VMS_FPC_UNITS", "")
    failures = 0
    for label, defines in (("normal", {}), ("TestBuild", {"TestBuild": "1"})):
        defs = {"AppVersion": "2.0.0", "AppIdPlain": "{8D3F0C52-6A1B-4E7C-9B2D-5F4A3C2E1D07}", **defines}
        src = CACHE / f"innocheck-{label}.pas"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text(build_program(defs), encoding="utf-8")
        cmd = [fpc, "-s", "-vewn", f"-FE{CACHE}", str(src)]
        for unit_dir in filter(None, units.split(os.pathsep)):
            cmd.insert(1, f"-Fu{unit_dir}")
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        errors = [ln for ln in proc.stdout.splitlines() if "Error:" in ln or "Fatal:" in ln]
        print(f"[{label}] {'OK' if proc.returncode == 0 else 'ERRORES'}")
        for ln in errors:
            print("  " + ln)
        if proc.returncode != 0:
            failures += 1
        if not args.keep:
            src.unlink(missing_ok=True)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
