"""Espejo USB o carpeta compartida para tiendas sin Internet (PLAN-V2 §1.7).

    python -m tools.release mirror --version 2.1.0 --out E:\\

Copia del repositorio `offline` lo que necesita una tienda para llegar a esa versión (toda la cadena de
`root`, `targets`, el descriptor, sus componentes, los canales y la última tabla de avisos) y firma un
`snapshot`/`timestamp` nuevos con `offline-timestamp` que caducan a los **60 días**. Un USB sirve 60 días
desde que se prepara; pasado ese plazo la tienda dice «prepara un USB nuevo» y no aplica nada.
La tienda lo usa con `VMS_UPDATE_SOURCE=file:///E:/` (o la ruta de la carpeta compartida).
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from vms_updater.models import bundle_target

from .publish import PublishError, Repos
from .tuf_repo import EXPIRY_DAYS, custom_of


def make_mirror(repos: Repos, version: str, out: Path, *, days: int = EXPIRY_DAYS["offline"],
                now: datetime | None = None) -> dict[str, Any]:
    off = repos.offline
    bundle = bundle_target(version)
    if off.get_target(bundle) is None:
        raise PublishError(f"La {version} no está en el repositorio offline")
    off.write_snapshot_timestamp(repos.kr, days=days, now=now)
    desc = json.loads(off.read_target(bundle))
    needed = {bundle} | {ref["target"] for ref in desc["components"].values()}
    latest_adv: tuple[str, str] | None = None
    for name, tf in off.all_targets().items():
        c = custom_of(tf)
        if c.get("kind") == "channel":
            needed.add(name)
        if c.get("kind") == "data" and c.get("data") == "advisories":
            gen = str(c.get("generated_at", ""))
            if latest_adv is None or gen > latest_adv[1]:
                latest_adv = (name, gen)
    if latest_adv:
        needed.add(latest_adv[0])
    out = Path(out)
    (out / "metadata").mkdir(parents=True, exist_ok=True)
    (out / "targets").mkdir(parents=True, exist_ok=True)
    for p in off.meta_dir.glob("*.root.json"):
        shutil.copy2(p, out / "metadata" / p.name)
    tv = off.targets.signed.version
    sv = off.snapshot.signed.version
    for name in (f"{tv}.targets.json", f"{sv}.snapshot.json"):
        shutil.copy2(off.meta_dir / name, out / "metadata" / name)
    copied = 0
    for name in sorted(needed):
        tf = off.get_target(name)
        assert tf is not None
        src = off.target_file_path(name, tf.hashes["sha256"])
        dst = out / "targets" / src.relative_to(off.tgt_dir)
        dst.parent.mkdir(parents=True, exist_ok=True)
        if not dst.exists() or dst.stat().st_size != src.stat().st_size:
            shutil.copy2(src, dst)
        copied += 1
    # timestamp.json el último: un USB a medio copiar no parece completo
    shutil.copy2(off.meta_dir / "timestamp.json", out / "metadata" / "timestamp.json")
    expires = off.timestamp.signed.expires
    (out / "LEEME.txt").write_text(
        "Espejo de actualizaciones de VMS Multimarca\n"
        f"Versión: {version}\nCaduca: {expires.isoformat()} (60 días desde que se preparó)\n"
        "En la tienda: VMS_UPDATE_SOURCE=file:///<letra>:/ (lo fija el instalador en modo «sin Internet»).\n"
        "Pasada la fecha, el equipo no aplica nada y pide un USB nuevo.\n", encoding="utf-8")
    return {"version": version, "files": copied, "expires": expires.isoformat(), "out": str(out)}
