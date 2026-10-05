"""Respaldo y restauración de la configuración (PLAN-V2 §2.5 pasos 5 y 8).

`backups\\pre-<X.Y.Z>-<AAAAMMDDTHHMMSSZ>\\` con `config\\` (config.json, users.json y sus copias) y
`secrets\\` (blobs cifrados con DPAPI de máquina: siguen sirviendo en el mismo equipo). En la vuelta atrás
se restaura `config\\` archivo a archivo con escritura atómica (el resultado es idéntico byte a byte).
Los secretos **no** se restauran solos (un token rotado después no debe volver atrás); quedan en el
respaldo para soporte técnico. `pg_dump`: solo en la central (§2.8), no en las tiendas.

Durabilidad: cada archivo del respaldo se copia con fsync y la carpeta se sincroniza antes del renombrado
final, que va antes de marcar `backed_up` en el diario. Al restaurar, un `.json` del respaldo que no sea JSON
válido NO se repone (se deja el actual y se registra): nunca se cambia una configuración buena por una rota.
"""
from __future__ import annotations

import json
import logging
import shutil
import time
from pathlib import Path

from ._atomic import atomic_write_bytes, copy_durable, fsync_dirs, rename_durable, write_durable

log = logging.getLogger("vms_updater.backup")

KEEP_BACKUPS = 3
MANIFEST = "backup.json"


def backup_name(version: str, now: float) -> str:
    return f"pre-{version}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime(now))}"


def create_backup(*, data_dir: Path, backups_dir: Path, name: str, extra: dict[str, object] | None = None) -> Path:
    """Copia completa en una carpeta temporal y renombrado final (idempotente: si ya existe, se reutiliza)."""
    dest = backups_dir / name
    if (dest / MANIFEST).is_file():
        return dest
    tmp = backups_dir / f"{name}.tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    files: list[str] = []
    for sub in ("config", "secrets"):
        src = data_dir / sub
        if not src.is_dir():
            continue
        for p in sorted(src.rglob("*")):
            if not p.is_file() or ".tmp-" in p.name:
                continue
            rel = p.relative_to(data_dir)
            out = tmp / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            copy_durable(p, out)
            files.append(rel.as_posix())
    meta = {"schema": 1, "files": files, "created_unix": int(time.time()), **(extra or {})}
    write_durable(tmp / MANIFEST, json.dumps(meta, ensure_ascii=False, indent=2).encode("utf-8"))
    fsync_dirs(tmp)
    shutil.rmtree(dest, ignore_errors=True)
    rename_durable(tmp, dest)
    return dest


def _is_json_name(name: str) -> bool:
    return name.endswith(".json") or name.endswith(".json.bak")


def _valid_json(data: bytes) -> bool:
    try:
        json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return False
    return True


def restore_config(*, data_dir: Path, backup_dir: Path) -> list[str]:
    """Repone `config\\` del respaldo. Los archivos de config que no estaban en el respaldo se apartan
    (`<nombre>.after-rollback`), nunca se borran. Devuelve las rutas restauradas."""
    src = backup_dir / "config"
    if not (backup_dir / MANIFEST).is_file():
        raise FileNotFoundError(f"El respaldo {backup_dir.name} está incompleto")
    restored: list[str] = []
    in_backup: set[str] = set()
    damaged: set[str] = set()
    if src.is_dir():
        for p in sorted(src.rglob("*")):
            if p.is_file():
                rel = p.relative_to(backup_dir)
                data = p.read_bytes()
                if _is_json_name(p.name) and not _valid_json(data):
                    log.error("El respaldo %s trae %s dañado: no se repone y se deja el actual",
                              backup_dir.name, rel.as_posix())
                    damaged.add(rel.as_posix())
                    continue
                in_backup.add(rel.as_posix())
                atomic_write_bytes(data_dir / rel, data)
                restored.append(rel.as_posix())
    cfg = data_dir / "config"
    for name in ("config.json", "users.json"):
        rel_name = f"config/{name}"
        if rel_name not in in_backup and rel_name not in damaged and (cfg / name).is_file():
            (cfg / name).replace(cfg / f"{name}.after-rollback")
    return restored


def prune_backups(backups_dir: Path, keep: int = KEEP_BACKUPS) -> None:
    if not backups_dir.is_dir():
        return
    done = sorted((p for p in backups_dir.iterdir() if p.is_dir() and (p / MANIFEST).is_file()),
                  key=lambda p: p.stat().st_mtime, reverse=True)
    for p in done[keep:]:
        shutil.rmtree(p, ignore_errors=True)
    for p in backups_dir.glob("*.tmp"):
        shutil.rmtree(p, ignore_errors=True)
