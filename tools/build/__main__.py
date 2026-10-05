"""``python -m tools.build``: ver la ayuda del paquete (``tools/build/__init__.py``)."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

from distribution.layout import (
    LayoutError,
    LayoutInputs,
    build_layout,
    compare_builds,
    numeric_version,
    parse_version,
    source_date_epoch,
)

from . import INNO_VERSION, ROOT
from .inno import BuildError, compile_installer, find_iscc, install_inno, iscc_defines, iscc_version
from .natives import NativeArtifacts, build_doubles, build_product, is_double
from .sbom import write_sbom
from .sign import sign_one_cli, sign_tree

ENGINE_DIR = ROOT / "build" / "engine"


def _say(msg: str) -> None:
    print(f"==> {msg}", flush=True)


# ------------------------------------------------------------------------------------------- piezas
def _natives(args: argparse.Namespace) -> NativeArtifacts:
    if args.vmshost and args.vmsctl and args.viewer:
        return NativeArtifacts(Path(args.vmshost), Path(args.vmsctl), Path(args.viewer), is_double(Path(args.vmsctl)))
    if args.doubles:
        _say("Dobles de prueba (tests/windows/doubles): vmsctl que registra llamadas, vmshost y visor mínimos")
        return build_doubles()
    _say("Binarios de producto (native/: vmshost y vmsctl de B1; visor de B2)")
    return build_product(Path(args.viewer) if args.viewer else None)


def _runtime(args: argparse.Namespace) -> Path | None:
    if args.runtime_dir:
        return Path(args.runtime_dir)
    if importlib.util.find_spec("distribution.runtime.build") is not None:
        out = ROOT / "build" / "runtime"
        _say(f"Runtime de Python (B1): python -m distribution.runtime.build --out {out}")
        proc = subprocess.run([sys.executable, "-m", "distribution.runtime.build", "--out", str(out)], cwd=ROOT,
                              check=False)
        if proc.returncode != 0:
            raise BuildError(f"distribution.runtime.build falló (código {proc.returncode})")
        return out
    if args.doubles:
        _say("AVISO: sin runtime de B1 (distribution/runtime/build.py aún no existe): runtime de prueba")
        return None
    raise BuildError("Falta el runtime de Python (B1). Pasa --runtime-dir o usa --doubles para una build de prueba.")


def _engine(args: argparse.Namespace) -> Path:
    if args.engine_dir:
        return Path(args.engine_dir)
    if not (ENGINE_DIR / "mediamtx.exe").is_file():
        _say("MediaMTX para Windows (tools.fetch_mediamtx, SHA-256 de la release)")
        from tools.fetch_mediamtx import main as fetch

        if fetch(["--platform", "windows_amd64", "--dest", str(ENGINE_DIR)]) != 0:
            raise BuildError("No se pudo descargar MediaMTX para Windows")
    return ENGINE_DIR


def _layout(args: argparse.Namespace, natives: NativeArtifacts, runtime: Path | None, engine: Path,
            out: Path, epoch: int) -> dict[str, object]:
    inputs = LayoutInputs(version=args.version, vmshost=natives.vmshost, vmsctl=natives.vmsctl,
                          viewer=natives.viewer, engine_dir=engine, runtime_dir=runtime, epoch=epoch,
                          build_info={"doubles": str(natives.doubles).lower(),
                                      "commit": os.environ.get("GITHUB_SHA", "")})
    result = build_layout(inputs, out)
    return {"payload": str(result.payload), "components": result.components,
            "runtime_is_stub": result.runtime_is_stub, "reproducible": result.reproducible_hashes}


def _installer(args: argparse.Namespace, payload: Path, out: Path, test_build: bool) -> Path | None:
    iscc = find_iscc(getattr(args, "iscc", None))
    if iscc is None:
        if sys.platform == "win32":
            raise BuildError(f"No encuentro ISCC.exe de Inno Setup {INNO_VERSION}: python -m tools.build inno --install")
        _say("Sin ISCC (no es Windows): el instalador no se compila aquí (PLAN-V2 §1.8)")
        return None
    version = iscc_version(iscc)
    if not version.startswith(INNO_VERSION):
        raise BuildError(f"ISCC es la versión {version!r}; el proyecto fija Inno Setup {INNO_VERSION}")
    sign_tool = None
    sign_tools: dict[str, str] = {}
    if args.sign:
        sign_tool = "vmssign"
        sign_tools[sign_tool] = f'"{sys.executable}" -m tools.build sign-file $f'
    defines = iscc_defines(version=args.version, numeric=numeric_version(args.version), payload=payload,
                           output_dir=out, base_name=f"VMSMultimarca-Setup-{args.version}", test_build=test_build,
                           sign_tool=sign_tool)
    _say(f"Instalador con ISCC {version}" + (" (build de prueba)" if test_build else ""))
    return compile_installer(iscc, defines, sign_tools=sign_tools, log=out / "iscc.log")


# ------------------------------------------------------------------------------------------- órdenes
def cmd_all(args: argparse.Namespace) -> int:
    parse_version(args.version)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    epoch = source_date_epoch(args.epoch)
    natives = _natives(args)
    runtime = _runtime(args)
    engine = _engine(args)
    _say(f"Payload {args.version} (SOURCE_DATE_EPOCH={epoch})")
    info = _layout(args, natives, runtime, engine, out / "layout", epoch)
    status, files = sign_tree(Path(str(info["payload"])))
    _say(f"Firma del payload: {status} ({len(files)} PE sin firma)"
         + (" — sin certificado todavía (decisión N1)" if status == "skipped" else ""))
    write_sbom(out / f"sbom-{args.version}.cdx.json", args.version, epoch)
    test_build = args.test_build or natives.doubles
    setup = None if args.skip_installer else _installer(args, Path(str(info["payload"])), out, test_build)
    summary = {"version": args.version, "epoch": epoch, "doubles": natives.doubles, "test_build": test_build,
               "signing": status, "installer": str(setup) if setup else None, **info}
    (out / "build-info.json").write_text(json.dumps(summary, indent=2, default=str) + "\n", encoding="utf-8")
    _say(f"Listo: {setup or out}")
    return 0


def cmd_repro(args: argparse.Namespace) -> int:
    """Dos builds limpias del payload con la misma entrada → mismo SHA-256 (PLAN-V2 §4.1)."""
    out = Path(args.out)
    epoch = source_date_epoch(args.epoch)
    natives = _natives(args)
    runtime = _runtime(args)
    engine = _engine(args)
    a, b = out / "repro-a", out / "repro-b"
    _say("Build A")
    _layout(args, natives, runtime, engine, a, epoch)
    _say("Build B (carpeta limpia)")
    _layout(args, natives, runtime, engine, b, epoch)
    diff = compare_builds(a, b)
    comps = json.loads((a / "components.json").read_text(encoding="utf-8"))["components"]
    report = {"version": args.version, "epoch": epoch, "identical": not diff, "differences": diff,
              "runtime_is_stub": runtime is None,
              "sha256": {k: v["sha256"] for k, v in comps.items()}}
    (out / "repro.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    for name, h in report["sha256"].items():
        mark = "DIFIERE" if name in diff else "igual"
        print(f"  {name:8} {h}  {mark}")
    if diff:
        print("ERROR: el payload no es reproducible: " + ", ".join(diff), file=sys.stderr)
        return 1
    _say("Payload reproducible: mismo SHA-256 en las dos builds")
    return 0


def cmd_layout(args: argparse.Namespace) -> int:
    from distribution.layout import main as layout_main

    return layout_main(args.rest)


def cmd_installer(args: argparse.Namespace) -> int:
    setup = _installer(args, Path(args.payload), Path(args.out), args.test_build)
    return 0 if setup else 1


def cmd_inno(args: argparse.Namespace) -> int:
    if args.install:
        iscc = install_inno(Path(args.dir) if args.dir else None)
    else:
        found = find_iscc()
        if found is None:
            print("No encuentro ISCC.exe", file=sys.stderr)
            return 1
        iscc = found
    print(iscc)
    print(iscc_version(iscc))
    return 0


def cmd_sign(args: argparse.Namespace) -> int:
    status, files = sign_tree(Path(args.dist), dry_run=args.dry_run)
    for f in files:
        print(f"  {status:8} {f}")
    if status == "skipped":
        print("Sin certificado configurado: nada firmado (decisión N1, PLAN-V2 §9). Ver tools/build/sign.py.")
    return 0


def cmd_sbom(args: argparse.Namespace) -> int:
    path = write_sbom(Path(args.out), args.version, source_date_epoch(args.epoch))
    print(path)
    return 0


def cmd_pascal_check(args: argparse.Namespace) -> int:
    from .pascal_check import main as pascal_main

    return pascal_main(args.rest)


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--version", required=True, help="versión SemVer, p. ej. 2.0.0 o 2.0.0-dev.5")
    p.add_argument("--out", default="dist")
    p.add_argument("--epoch", type=int, help="SOURCE_DATE_EPOCH (por defecto, la fecha del último commit)")
    p.add_argument("--doubles", action="store_true", help="dobles de prueba en lugar de los binarios de B1/B2")
    p.add_argument("--vmshost")
    p.add_argument("--vmsctl")
    p.add_argument("--viewer")
    p.add_argument("--runtime-dir")
    p.add_argument("--engine-dir")


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m tools.build")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("all", help="nativos, runtime, payload, SBOM e instalador")
    _common(a)
    a.add_argument("--skip-installer", action="store_true")
    a.add_argument("--test-build", action="store_true", help="instalador de prueba (parámetros de simulación)")
    a.add_argument("--sign", action="store_true", help="firmar instalador y desinstalador (SignTool de ISCC)")
    a.add_argument("--iscc")
    a.set_defaults(func=cmd_all)
    r = sub.add_parser("repro", help="dos builds limpias del payload y comparación de SHA-256")
    _common(r)
    r.set_defaults(func=cmd_repro)
    lay = sub.add_parser("layout", help="python -m distribution.layout (mismos argumentos)")
    lay.add_argument("rest", nargs=argparse.REMAINDER)
    lay.set_defaults(func=cmd_layout)
    ins = sub.add_parser("installer", help="compila el instalador desde un payload ya montado")
    ins.add_argument("--version", required=True)
    ins.add_argument("--payload", required=True)
    ins.add_argument("--out", default="dist")
    ins.add_argument("--test-build", action="store_true")
    ins.add_argument("--sign", action="store_true")
    ins.add_argument("--iscc")
    ins.set_defaults(func=cmd_installer)
    inno = sub.add_parser("inno", help="localiza o instala Inno Setup 7.1.0 (descarga con SHA-256 fijado)")
    inno.add_argument("--install", action="store_true")
    inno.add_argument("--dir")
    inno.set_defaults(func=cmd_inno)
    s = sub.add_parser("sign", help="firma Authenticode de los PE sin firma (se salta sin certificado)")
    s.add_argument("--dist", required=True)
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(func=cmd_sign)
    sf = sub.add_parser("sign-file", help="firma un archivo (lo llama ISCC con SignTool=)")
    sf.add_argument("path")
    sf.set_defaults(func=lambda a: sign_one_cli(a.path))
    sb = sub.add_parser("sbom", help="SBOM CycloneDX 1.6")
    sb.add_argument("--version", required=True)
    sb.add_argument("--out", required=True)
    sb.add_argument("--epoch", type=int)
    sb.set_defaults(func=cmd_sbom)
    pc = sub.add_parser("pascal-check", help="comprueba el [Code] del instalador con Free Pascal (desarrollo)")
    pc.add_argument("rest", nargs=argparse.REMAINDER)
    pc.set_defaults(func=cmd_pascal_check)
    return p


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # Órdenes que pasan sus argumentos tal cual a otro módulo (argparse no reenvía bien opciones con «--»).
    if argv and argv[0] == "layout":
        from distribution.layout import main as layout_main

        return layout_main(argv[1:])
    if argv and argv[0] == "pascal-check":
        from .pascal_check import main as pascal_main

        return pascal_main(argv[1:])
    args = parser().parse_args(argv)
    if isinstance(getattr(args, "version", None), str):
        args.version = args.version.removeprefix("v")      # etiquetas «v2.0.0»
    try:
        return int(args.func(args))
    except (BuildError, LayoutError, ValueError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
