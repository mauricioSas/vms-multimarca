"""Espejo USB o carpeta compartida para tiendas sin Internet (PLAN-V2 §1.7).

    python -m tools.release mirror --channel stable --out E:\\        (la versión que tenga ese canal)
    python -m tools.release mirror --version 2.1.0 --out E:\\         (comprueba que algún canal la tenga)

La tienda instala **lo que diga su canal** (no se elige versión en la tienda), así que un USB solo sirve si
trae la versión de su canal. Por eso solo se copian los canales que apuntan a la versión del USB; si el canal
pedido apunta a otra, se rechaza con un mensaje claro en vez de dejar un USB que daría «HTTP 404».

Copia del repositorio `offline` lo que necesita una tienda para llegar a esa versión (toda la cadena de
`root`, `targets`, el descriptor, sus componentes, los canales que apuntan a ella y la última tabla de
avisos) y firma un
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


def _channels(off: Any) -> dict[str, tuple[str, str]]:
    """{canal: (target, versión)} del repositorio offline."""
    out: dict[str, tuple[str, str]] = {}
    for name, tf in off.all_targets().items():
        c = custom_of(tf)
        if c.get("kind") == "channel":
            doc = json.loads(off.read_target(name))
            out[str(doc.get("channel"))] = (name, str(doc.get("version")))
    return out


def make_mirror(repos: Repos, version: str | None, out: Path, *, days: int = EXPIRY_DAYS["offline"],
                now: datetime | None = None, channel: str | None = None) -> dict[str, Any]:
    off = repos.offline
    channels = _channels(off)
    if channel is None and version is None:
        channel = "stable"
    if channel is not None:
        if channel not in channels:
            raise PublishError(f"El canal «{channel}» no está publicado")
        ch_version = channels[channel][1]
        if version is not None and version != ch_version:
            raise PublishError(f"El canal «{channel}» está en la {ch_version}, no en la {version}: la tienda "
                               f"instalaría la {ch_version} y el USB no la traería. Prepara el USB con "
                               f"--channel {channel} (sin --version) o mueve antes el canal")
        version = ch_version
    assert version is not None
    bundle = bundle_target(version)
    if off.get_target(bundle) is None:
        raise PublishError(f"La {version} no está en el repositorio offline")
    serving = sorted(n for n, (_, v) in channels.items() if v == version)
    if not serving:
        raise PublishError(f"Ningún canal apunta a la {version}: la tienda no la instalaría desde el USB. Mueve "
                           "un canal a esa versión o usa --channel")
    left_out = sorted(n for n in channels if n not in serving)
    off.write_snapshot_timestamp(repos.kr, days=days, now=now)
    desc = json.loads(off.read_target(bundle))
    needed = {bundle} | {ref["target"] for ref in desc["components"].values()}
    needed |= {channels[n][0] for n in serving}
    latest_adv: tuple[str, str] | None = None
    for name, info in off.all_targets().items():
        c = custom_of(info)
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
        f"Versión: {version}\nCanales: {', '.join(serving)}\n"
        f"Caduca: {expires.isoformat()} (60 días desde que se preparó)\n"
        "En la tienda: VMS_UPDATE_SOURCE=file:///<letra>:/ (lo fija el instalador en modo «sin Internet»).\n"
        "Pasada la fecha, el equipo no aplica nada y pide un USB nuevo.\n", encoding="utf-8")
    return {"version": version, "channels": serving, "channels_not_served": left_out, "files": copied,
            "expires": expires.isoformat(), "out": str(out)}
