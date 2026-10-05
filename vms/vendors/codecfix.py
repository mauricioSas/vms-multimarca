"""«Corregir códec» con copia previa, «Deshacer» durante 30 días y registro de auditoría (PLAN-V2 §3.2 punto 8).

Cambiar algo en un equipo del cliente es delicado, así que:
1. **Antes de cambiar nada** se lee la configuración actual del flujo (el mismo recurso que se va a cambiar)
   y se guarda en `config/device-backups/<equipo>/<id>.xml|.txt` con un `.json` al lado (quién, cuándo,
   canal, valor anterior y nuevo).
2. Se cambia el códec (Hikvision: PUT de `/ISAPI/Streaming/channels/<N>02`; Dahua: `setConfig` de
   `Encode[N-1].ExtraFormat[0].Video.Compression`).
3. Queda una línea en `logs/audit.log` (`codec_fix` / `codec_fix_undo`).
4. «Deshacer» repone exactamente lo guardado con el mismo PUT/setConfig, mientras la copia tenga menos de
   30 días. Las copias más antiguas se borran.

La confirmación explícita la pide la interfaz y la exige la API (`confirm: true`).
"""
from __future__ import annotations

import json
import logging
import secrets
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from vms.core.atomic import atomic_write_text
from vms.core.audit import audit
from vms.core.errors import DeviceUnsupported, NotFoundError, ValidationFailed

from .codec import CodecFixClient, StreamConfigBackup, normalize_codec

log = logging.getLogger("vms.vendors.codecfix")

UNDO_DAYS = 30
_ID_OK = set("abcdefghijklmnopqrstuvwxyz0123456789-")


class CodecFixRecord(BaseModel):
    backup_id: str
    device_id: str
    channel: int
    stream: Literal["main", "sub"]
    previous_codec: str | None
    new_codec: str
    user: str
    created_at: datetime
    undone_at: datetime | None = None
    undo_available: bool = True


def _now() -> datetime:
    return datetime.now(timezone.utc)


class CodecFixer:
    def __init__(self, config_dir: Path, *, clock: Any = _now) -> None:
        self.root = Path(config_dir) / "device-backups"
        self._clock = clock

    def _dir(self, device_id: str) -> Path:
        if not device_id or set(device_id) - _ID_OK:
            raise ValidationFailed("Identificador de equipo no válido")
        return self.root / device_id

    def _meta_path(self, device_id: str, backup_id: str) -> Path:
        if not backup_id or set(backup_id) - _ID_OK:
            raise NotFoundError("La copia no existe")
        return self._dir(device_id) / f"{backup_id}.json"

    async def apply(self, device_id: str, client: object, channel: int, *, user: str, ip: str,
                    stream: Literal["main", "sub"] = "sub", codec: str = "H.264") -> CodecFixRecord:
        if not isinstance(client, CodecFixClient):
            raise DeviceUnsupported("Esta marca no permite cambiar el códec desde el programa: hazlo en el equipo")
        target = normalize_codec(codec) or codec
        before = await client.read_stream_config(channel, stream)
        if before.codec == target:
            raise ValidationFailed(f"El {'subflujo' if stream == 'sub' else 'flujo principal'} del canal {channel} "
                                   f"ya está en {target}")
        now = self._clock()
        backup_id = f"cfx-{now:%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"
        folder = self._dir(device_id)
        folder.mkdir(parents=True, exist_ok=True)
        atomic_write_text(folder / f"{backup_id}.{before.fmt}", before.raw)
        record = CodecFixRecord(backup_id=backup_id, device_id=device_id, channel=int(channel), stream=stream,
                                previous_codec=before.codec, new_codec=target, user=user, created_at=now)
        meta = {"record": json.loads(record.model_dump_json()),
                "backup": {k: v for k, v in asdict(before).items() if k != "raw"}}
        atomic_write_text(folder / f"{backup_id}.json", json.dumps(meta, ensure_ascii=False, indent=2))
        # La copia ya está en disco: ahora sí se cambia el equipo.
        await client.set_stream_codec(channel, stream, target)
        audit("codec_fix", user=user, ip=ip, device_id=device_id, channel=channel, stream=stream,
              previous=before.codec, new=target, backup_id=backup_id)
        log.info("Códec del canal %s (%s) del equipo %s cambiado de %s a %s por «%s»", channel, stream, device_id,
                 before.codec, target, user)
        return record

    def _load(self, device_id: str, backup_id: str) -> tuple[CodecFixRecord, StreamConfigBackup, dict[str, Any]]:
        path = self._meta_path(device_id, backup_id)
        if not path.is_file():
            raise NotFoundError("La copia no existe o ya caducó")
        meta = json.loads(path.read_text(encoding="utf-8"))
        record = CodecFixRecord.model_validate(meta["record"])
        b = meta["backup"]
        raw = (path.parent / f"{backup_id}.{b['fmt']}").read_text(encoding="utf-8")
        backup = StreamConfigBackup(vendor=b["vendor"], channel=int(b["channel"]), stream=b["stream"],
                                    codec=b["codec"], resource=b["resource"], raw=raw, fmt=b["fmt"])
        return record, backup, meta

    def history(self, device_id: str) -> list[CodecFixRecord]:
        folder = self._dir(device_id)
        if not folder.is_dir():
            return []
        limit = self._clock() - timedelta(days=UNDO_DAYS)
        out: list[CodecFixRecord] = []
        for meta_path in sorted(folder.glob("cfx-*.json")):
            try:
                rec = CodecFixRecord.model_validate(json.loads(meta_path.read_text(encoding="utf-8"))["record"])
            except (OSError, ValueError, KeyError):
                log.warning("Copia de códec ilegible: %s", meta_path.name)
                continue
            if rec.created_at < limit:
                for f in folder.glob(f"{rec.backup_id}.*"):
                    f.unlink(missing_ok=True)
                continue
            rec.undo_available = rec.undone_at is None
            out.append(rec)
        return sorted(out, key=lambda r: r.created_at, reverse=True)

    async def undo(self, device_id: str, client: object, backup_id: str, *, user: str, ip: str) -> CodecFixRecord:
        if not isinstance(client, CodecFixClient):
            raise DeviceUnsupported("Esta marca no permite cambiar el códec desde el programa")
        record, backup, meta = self._load(device_id, backup_id)
        if record.created_at < self._clock() - timedelta(days=UNDO_DAYS):
            raise ValidationFailed(f"La copia tiene más de {UNDO_DAYS} días: ya no se puede deshacer desde aquí")
        if record.undone_at is not None:
            raise ValidationFailed("Ese cambio ya se deshizo")
        await client.restore_stream_config(backup)
        record.undone_at = self._clock()
        record.undo_available = False
        meta["record"] = json.loads(record.model_dump_json())
        atomic_write_text(self._meta_path(device_id, backup_id), json.dumps(meta, ensure_ascii=False, indent=2))
        audit("codec_fix_undo", user=user, ip=ip, device_id=device_id, channel=record.channel, stream=record.stream,
              previous=record.new_codec, new=record.previous_codec, backup_id=backup_id)
        log.info("Cambio de códec %s del equipo %s deshecho por «%s»", backup_id, device_id, user)
        return record


__all__ = ["CodecFixRecord", "CodecFixer", "UNDO_DAYS"]
