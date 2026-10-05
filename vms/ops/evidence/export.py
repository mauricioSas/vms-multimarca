"""Paquete de evidencias firmado (CONTRATO §18.7).

```
LEEME.txt                      cómo verificar, en español
visor.html                     autocontenido (sin red): clips, marca de agua superpuesta y comprobación SHA-256
manifiesto.json                EvidenceManifest (schema 1) con el SHA-256 y el tamaño de CADA archivo
manifiesto.sig                 firma Ed25519 (base64) de los bytes exactos de manifiesto.json
clave-publica.pem              clave pública de la instalación
acta.html                      acta de cadena de custodia
video/<camera_id>/segments/…   segmentos fMP4 ORIGINALES de MediaMTX (el «nativo»)
video/<camera_id>/<cámara>_<AAAAMMDD-HHMMSS>.mp4   unión sin recodificar (remux de MediaMTX)
```

Nunca se recodifica (sin libx264): H.265 se entrega tal cual y el acta recomienda VLC. La marca de agua
(usuario y fecha) solo se superpone en el visor: el vídeo original no se toca y su hash no cambia.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import json
import logging
import os
import re
import secrets
import shutil
import unicodedata
import zipfile
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from importlib import resources
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..models import EvidenceExport, EvidenceFile, EvidenceManifest
from .keys import EvidenceKey
from .segments import segments_in_range

log = logging.getLogger("vms.ops.evidence.export")

MAX_EXPORT_SPAN = timedelta(hours=12)
MP4_CHUNK_S = 3600.0
COPY_CHUNK = 1024 * 1024
SPACE_MARGIN = 256 * 1024 * 1024        # acta, visor, manifiesto y holgura del sistema de archivos
MANIFEST, SIGNATURE, PUBKEY = "manifiesto.json", "manifiesto.sig", "clave-publica.pem"

AuxKind = Literal["key", "viewer", "report", "other"]
FetchMp4 = Callable[[str, datetime, float], AsyncIterator[bytes]]
Progress = Callable[[float, str], Awaitable[None] | None]


def new_export_id(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return f"ev-{now.strftime('%Y%m%d')}-{secrets.token_hex(3)}"


def ascii_name(name: str) -> str:
    plain = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^A-Za-z0-9._-]+", "_", plain).strip("_") or "camara"


def _tz(name: str) -> Any:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return timezone.utc


@dataclass
class CameraInfo:
    camera_id: str
    name: str
    device: str = ""
    codec: str = ""
    clock_skew_s: float | None = None
    clock_measured_at: str | None = None


@dataclass
class ExportContext:
    product_version: str
    site: dict[str, str]                      # id, name, code, timezone
    cameras: dict[str, CameraInfo]
    recordings_dir: Path
    protected_dir: Path
    segment_seconds: int
    key: EvidenceKey
    fetch_mp4: FetchMp4 | None = None
    pc_clock: dict[str, Any] = field(default_factory=dict)


def sha256_file(path: Path) -> tuple[str, int]:
    h = hashlib.sha256()
    n = 0
    with open(path, "rb") as f:
        while chunk := f.read(COPY_CHUNK):
            h.update(chunk)
            n += len(chunk)
    return h.hexdigest(), n


def _zinfo(name: str, size: int) -> zipfile.ZipInfo:
    zi = zipfile.ZipInfo(name, date_time=datetime.now().timetuple()[:6])
    zi.compress_type = zipfile.ZIP_STORED
    zi.file_size = size            # con el tamaño conocido, zipfile decide si hace falta ZIP64
    return zi


def _copy_into_zip(zf: zipfile.ZipFile, src: Path, name: str) -> tuple[str, int]:
    """Copia `src` dentro del ZIP calculando su SHA-256 al vuelo (sin copia intermedia en disco)."""
    h = hashlib.sha256()
    n = 0
    with open(src, "rb") as fi, zf.open(_zinfo(name, src.stat().st_size), "w") as fo:
        while chunk := fi.read(COPY_CHUNK):
            h.update(chunk)
            fo.write(chunk)
            n += len(chunk)
    return h.hexdigest(), n


def _template(name: str) -> str:
    try:
        return resources.files("vms.ops.evidence").joinpath("templates", name).read_text(encoding="utf-8")
    except FileNotFoundError as exc:   # instalación incompleta (p. ej. wheel sin los datos del paquete)
        raise ValueError(f"Falta la plantilla {name} del paquete de evidencias: reinstala el programa") from exc


class NotEnoughSpace(ValueError):
    """No cabe la exportación en el disco (mensaje para el usuario)."""


class EvidenceBuilder:
    def __init__(self, exports_dir: Path, *, disk_free: Callable[[Path], int] | None = None) -> None:
        self.exports_dir = Path(exports_dir)
        self.disk_free = disk_free or (lambda p: shutil.disk_usage(p).free)

    def zip_path(self, export_id: str) -> Path:
        return self.exports_dir / f"{export_id}.zip"

    def part_path(self, export_id: str) -> Path:
        return self.exports_dir / f"{export_id}.zip.part"

    def scratch_dir(self, export_id: str) -> Path:
        return self.exports_dir / f".{export_id}.tmp"

    def cleanup_leftovers(self) -> list[str]:
        """Borra restos de exportaciones interrumpidas (`*.zip.part` y `.*.tmp`). Devuelve lo borrado."""
        removed: list[str] = []
        if not self.exports_dir.is_dir():
            return removed
        for p in self.exports_dir.iterdir():
            try:
                if p.is_dir() and p.name.startswith(".") and p.name.endswith(".tmp"):
                    shutil.rmtree(p)
                    removed.append(p.name)
                elif p.is_file() and p.name.endswith(".zip.part"):
                    p.unlink()
                    removed.append(p.name)
            except OSError as exc:
                log.warning("No se pudo borrar el resto de exportación %s: %s", p.name, exc)
        return removed

    def check_space(self, needed: int) -> None:
        self.exports_dir.mkdir(parents=True, exist_ok=True)
        free = self.disk_free(self.exports_dir)
        if free < needed:
            raise NotEnoughSpace(
                f"No hay espacio libre suficiente para la exportación: hacen falta unos {_gb(needed)} y quedan "
                f"{_gb(free)}. Acorta el intervalo, quita cámaras o borra exportaciones antiguas.")

    async def build(self, exp: EvidenceExport, ctx: ExportContext, progress: Progress | None = None) -> tuple[Path, str, int]:
        """Crea `<exports>/<export_id>.zip` escribiendo directamente en el ZIP (sin copia intermedia de los
        segmentos). Devuelve (ruta, SHA-256 del manifiesto, número de archivos)."""
        req = exp.request
        start, end = _utc(req.start), _utc(req.end)
        if end <= start:
            raise ValueError("El final debe ser posterior al inicio")
        if end - start > MAX_EXPORT_SPAN:
            raise ValueError("Una exportación no puede pasar de 12 horas; divídela en varias")

        async def report(p: float, msg: str) -> None:
            if progress is not None:
                r = progress(round(min(1.0, max(0.0, p)), 3), msg)
                if asyncio.iscoroutine(r):
                    await r

        plan = {cid: segments_in_range(ctx.recordings_dir, ctx.protected_dir, cid, start, end, ctx.segment_seconds)
                for cid in req.camera_ids}
        if not any(plan.values()):
            raise ValueError("No hay grabación de esas cámaras en ese intervalo")
        total = sum(s.size for segs in plan.values() for s in segs) or 1
        with_mp4 = req.include_mp4 and ctx.fetch_mp4 is not None
        # Los segmentos van una vez al ZIP; el MP4 unido ocupa más o menos lo mismo otra vez (y un trozo de
        # hasta 1 h pasa por un temporal). Margen fijo para el acta, el visor y el sistema de archivos.
        self.check_space(total * (2 if with_mp4 else 1) + SPACE_MARGIN)
        part = self.part_path(exp.export_id)
        scratch = self.scratch_dir(exp.export_id)
        prefix = f"{exp.export_id}/"
        try:
            files: list[EvidenceFile] = []
            notes: list[str] = []
            with zipfile.ZipFile(part, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as zf:
                done = 0
                for cid, segs in plan.items():
                    if not segs:
                        notes.append(f"La cámara {ctx.cameras.get(cid, CameraInfo(cid, cid)).name} no tiene grabación "
                                     "en ese intervalo.")
                    for s in segs:
                        rel = f"video/{cid}/segments/{s.name}"
                        digest, size = await asyncio.to_thread(_copy_into_zip, zf, s.path, prefix + rel)
                        files.append(EvidenceFile(path=rel, sha256=digest, bytes=size, camera_id=cid, kind="segment"))
                        done += s.size
                        await report(0.75 * done / total, f"Copiando segmentos originales ({len(files)})")
                if with_mp4:
                    for cid, segs in plan.items():
                        if not segs:
                            continue
                        info = ctx.cameras.get(cid, CameraInfo(cid, cid))
                        files += await self._mp4(ctx, cid, info, start, end, zf, prefix, scratch, notes)
                        await report(0.85, f"MP4 unido de {info.name}")
                elif req.include_mp4:
                    notes.append("No se generó el MP4 unido: el servidor de reproducción no estaba disponible. Los "
                                 "segmentos originales están completos.")
                await report(0.95, "Firmando el manifiesto")
                manifest_sha, count = await asyncio.to_thread(self._finish, exp, ctx, zf, prefix, files, notes,
                                                              start, end)
            final = self.zip_path(exp.export_id)
            os.replace(part, final)
            await report(1.0, "Listo")
            return final, manifest_sha, count
        except BaseException:
            part.unlink(missing_ok=True)
            raise
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    async def _mp4(self, ctx: ExportContext, cid: str, info: CameraInfo, start: datetime, end: datetime,
                   zf: zipfile.ZipFile, prefix: str, scratch: Path, notes: list[str]) -> list[EvidenceFile]:
        """MP4 unido por trozos de 1 h: cada trozo se descarga a un temporal (si falla a medias, no queda nada
        a medio escribir en el ZIP) y se mete en el ZIP; el temporal se borra en el acto."""
        assert ctx.fetch_mp4 is not None
        out: list[EvidenceFile] = []
        tz = _tz(ctx.site.get("timezone", "UTC"))
        t = start
        scratch.mkdir(parents=True, exist_ok=True)
        while t < end:
            dur = min(MP4_CHUNK_S, (end - t).total_seconds())
            rel = f"video/{cid}/{ascii_name(info.name)}_{t.astimezone(tz).strftime('%Y%m%d-%H%M%S')}.mp4"
            tmp = scratch / "chunk.mp4"
            n = 0
            try:
                with open(tmp, "wb") as f:
                    async for chunk in ctx.fetch_mp4(cid, t, dur):
                        f.write(chunk)
                        n += len(chunk)
            except Exception as exc:  # noqa: BLE001 - el MP4 es una comodidad; los segmentos son la evidencia
                log.warning("No se pudo unir el MP4 de %s desde %s: %s", cid, t.isoformat(), exc)
                notes.append(f"No se pudo generar el MP4 unido de {info.name} desde "
                             f"{t.astimezone(tz).strftime('%H:%M:%S')}: usa los segmentos originales.")
            else:
                if n:
                    digest, size = await asyncio.to_thread(_copy_into_zip, zf, tmp, prefix + rel)
                    out.append(EvidenceFile(path=rel, sha256=digest, bytes=size, camera_id=cid, kind="mp4"))
            finally:
                tmp.unlink(missing_ok=True)
            t += timedelta(seconds=dur)
        return out

    def _finish(self, exp: EvidenceExport, ctx: ExportContext, zf: zipfile.ZipFile, prefix: str,
                files: list[EvidenceFile], notes: list[str], start: datetime, end: datetime) -> tuple[str, int]:
        req = exp.request
        tz = _tz(ctx.site.get("timezone", "UTC"))
        cams = [ctx.cameras.get(cid, CameraInfo(cid, cid)) for cid in req.camera_ids]

        def put(rel: str, data: bytes) -> None:
            zf.writestr(_zinfo(prefix + rel, len(data)), data)

        # 1) archivos auxiliares (se listan en el manifiesto con su hash)
        aux: tuple[tuple[str, AuxKind, str], ...] = (
            (PUBKEY, "key", ctx.key.public_pem()), ("visor.html", "viewer", _viewer_html(exp, ctx)),
               ("acta.html", "report", _acta_html(exp, ctx, cams, files, start, end, tz)),
               ("LEEME.txt", "other", _leeme(exp)))
        for rel, kind, text in aux:
            data = text.encode("utf-8")
            put(rel, data)
            files.append(EvidenceFile(path=rel, sha256=hashlib.sha256(data).hexdigest(), bytes=len(data),
                                      kind=kind))
        # 2) manifiesto firmado
        manifest = EvidenceManifest(
            product_version=ctx.product_version, export_id=exp.export_id, created_at=exp.created_at,
            created_by=exp.created_by, reason=req.reason, case_ref=req.case_ref, recipient=req.recipient,
            site=ctx.site, range_utc=(start, end),
            range_local=(start.astimezone(tz).isoformat(), end.astimezone(tz).isoformat()),
            cameras=[{"camera_id": c.camera_id, "name": c.name, "device": c.device, "codec": c.codec,
                      "clock_skew_s": c.clock_skew_s, "clock_measured_at": c.clock_measured_at} for c in cams],
            files=sorted(files, key=lambda f: f.path),
            signing_key={"algorithm": "ed25519", "public_key": ctx.key.public_b64, "key_id": ctx.key.key_id},
            pc_clock=ctx.pc_clock, notes_es=notes)
        data = json.dumps(manifest.model_dump(mode="json", by_alias=True), ensure_ascii=False, indent=2).encode("utf-8")
        put(MANIFEST, data)
        put(SIGNATURE, (base64.b64encode(ctx.key.sign(data)).decode("ascii") + "\n").encode("ascii"))
        return hashlib.sha256(data).hexdigest(), len(files) + 2


def _gb(n: int) -> str:
    return f"{n / 1e9:.1f} GB".replace(".", ",")


def _utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _viewer_html(exp: EvidenceExport, ctx: ExportContext) -> str:
    info = {"export_id": exp.export_id, "created_by": exp.created_by, "created_at": exp.created_at.isoformat(),
            "site": ctx.site.get("name", ""), "timezone": ctx.site.get("timezone", "UTC")}
    # «</» escapado: el JSON va dentro de un <script> y no puede cerrarlo
    blob = json.dumps(info, ensure_ascii=False).replace("</", "<\\/")
    return _template("visor.html").replace("/*__INFO__*/{}", blob)


def _acta_html(exp: EvidenceExport, ctx: ExportContext, cams: list[CameraInfo], files: list[EvidenceFile],
               start: datetime, end: datetime, tz: Any) -> str:
    e = html.escape
    req = exp.request

    def local(dt: datetime) -> str:
        return dt.astimezone(tz).strftime("%d/%m/%Y %H:%M:%S")

    h265 = any("265" in (c.codec or "") or "hevc" in (c.codec or "").lower() for c in cams)
    rows_cam = "".join(
        f"<tr><td>{e(c.name)}</td><td class=mono>{e(c.camera_id)}</td><td>{e(c.device)}</td><td>{e(c.codec or '—')}</td>"
        f"<td>{'—' if c.clock_skew_s is None else e(f'{c.clock_skew_s:+.1f} s')}</td></tr>" for c in cams)
    rows_files = "".join(
        f"<tr><td class=mono>{e(f.path)}</td><td class=num>{f'{f.bytes:,}'.replace(',', '.')}</td>"
        f"<td class='mono hash'>{f.sha256}</td></tr>"
        for f in sorted(files, key=lambda f: f.path) if f.kind in ("segment", "mp4"))
    codec_note = ("Al menos una cámara graba en H.265 (HEVC): si el visor del navegador no muestra el vídeo, "
                  "ábrelo con VLC (gratuito), que lo reproduce en cualquier equipo." if h265 else
                  "El vídeo está en el formato original de la cámara. Si un archivo no se reproduce en el navegador, "
                  "VLC (gratuito) lo abre en cualquier equipo.")
    return f"""<!doctype html>
<html lang="es"><head><meta charset="utf-8"><title>Acta de cadena de custodia · {e(exp.export_id)}</title>
<style>
body{{font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif;color:#111;margin:32px auto;max-width:960px;padding:0 16px}}
h1{{font-size:22px;margin:0 0 4px}} h2{{font-size:16px;margin:24px 0 8px;border-bottom:1px solid #ccc;padding-bottom:4px}}
table{{border-collapse:collapse;width:100%}} th,td{{border:1px solid #bbb;padding:4px 6px;text-align:left;vertical-align:top}}
th{{background:#f0f0f0}} .mono{{font-family:ui-monospace,Consolas,monospace;font-size:12px}} .hash{{word-break:break-all}}
.num{{text-align:right}} dl{{display:grid;grid-template-columns:220px 1fr;gap:4px 12px;margin:0}} dt{{font-weight:600}}
.sign{{display:grid;grid-template-columns:1fr 1fr;gap:24px;margin-top:12px}} .box{{border:1px solid #999;padding:12px;min-height:150px}}
.muted{{color:#555}} @media print{{body{{margin:0}} .noprint{{display:none}}}}
</style></head><body>
<p class="noprint muted">Imprime esta página (Ctrl+P) para firmarla en papel.</p>
<h1>Acta de cadena de custodia de grabaciones</h1>
<p class="muted">Paquete {e(exp.export_id)} · generado por VMS Multimarca {e(ctx.product_version)}</p>
<h2>1. Datos de la exportación</h2>
<dl>
<dt>Sede</dt><dd>{e(ctx.site.get("name", ""))} (código {e(ctx.site.get("code", "") or "—")}, id {e(ctx.site.get("id", ""))})</dd>
<dt>Intervalo (hora local, {e(ctx.site.get("timezone", "UTC"))})</dt><dd>{local(start)} a {local(end)}</dd>
<dt>Intervalo (UTC)</dt><dd>{start.strftime("%d/%m/%Y %H:%M:%S")} a {end.strftime("%d/%m/%Y %H:%M:%S")} UTC</dd>
<dt>Exportado por</dt><dd>{e(exp.created_by)} el {local(exp.created_at)}</dd>
<dt>Motivo</dt><dd>{e(req.reason)}</dd>
<dt>N.º de caso o atestado</dt><dd>{e(req.case_ref or "—")}</dd>
<dt>Destinatario previsto</dt><dd>{e(req.recipient or "—")}</dd>
<dt>Clave de firma (key_id)</dt><dd class="mono hash">{e(ctx.key.key_id)}</dd>
</dl>
<h2>2. Cámaras</h2>
<table><thead><tr><th>Cámara</th><th>Identificador</th><th>Equipo</th><th>Códec</th><th>Desfase horario medido</th></tr></thead>
<tbody>{rows_cam}</tbody></table>
<p class="muted">El desfase es la diferencia entre la hora de la cámara y la del PC en la última medida: súmalo o
réstalo a la hora sobreimpresa en la imagen si no coincide.</p>
<h2>3. Archivos de vídeo y huellas SHA-256</h2>
<table><thead><tr><th>Archivo</th><th>Bytes</th><th>SHA-256</th></tr></thead><tbody>{rows_files}</tbody></table>
<p>{e(codec_note)}</p>
<p>Los segmentos de <span class="mono">segments/</span> son los archivos originales tal como los grabó el sistema
(formato nativo, sin recomprimir). El MP4 unido es una copia de comodidad hecha sin recodificar.</p>
<h2>4. Cómo comprobar la integridad</h2>
<p><span class="mono">manifiesto.json</span> lista el SHA-256 y el tamaño de cada archivo del paquete y está firmado
con Ed25519 (<span class="mono">manifiesto.sig</span>, clave en <span class="mono">clave-publica.pem</span>). El
archivo <span class="mono">visor.html</span> recalcula las huellas en el navegador sin conexión; la comprobación
completa se hace con <span class="mono">python -m vms.ops.evidence verify &lt;paquete&gt; --key-id &lt;key_id&gt;</span>.
Si un solo byte cambia, la huella deja de coincidir.</p>
<p><b>La firma solo prueba el origen si la clave es la de la tienda:</b> el key_id del apartado 1 de esta acta
(impresa y firmada al entregar) tiene que coincidir con el que muestre la verificación. Y la verificación tiene
que hacerla un programa de fuente fiable (la central o <span class="mono">python -m vms.ops.evidence verify</span>):
el <span class="mono">visor.html</span> que viaja dentro del paquete lo podría haber cambiado quien manipuló el
paquete, así que sirve para ver los vídeos y una primera comprobación, no como prueba de origen.</p>
<h2>5. Entrega y recepción</h2>
<div class="sign">
<div class="box"><b>Entrega</b><br>Nombre y apellidos:<br><br>DNI / cargo:<br><br>Fecha y hora:<br><br>Firma:</div>
<div class="box"><b>Recibe</b><br>Nombre y apellidos / unidad:<br><br>TIP o DNI:<br><br>Fecha y hora:<br><br>Firma:</div>
</div>
</body></html>
"""


def _leeme(exp: EvidenceExport) -> str:
    return f"""PAQUETE DE EVIDENCIAS {exp.export_id}
================================================================

Contenido
---------
- visor.html ......... Ábrelo con Chrome o Edge (no necesita Internet). Pulsa «Abrir la carpeta del
                       paquete» y elige ESTA carpeta: comprueba que ningún archivo se ha modificado y
                       reproduce los vídeos con una marca de agua (usuario y fecha de la exportación)
                       superpuesta. La marca de agua no se graba en el vídeo.
- acta.html .......... Acta de cadena de custodia para imprimir y firmar (quien entrega y quien recibe).
- video/ ............. Por cada cámara: «segments/» con los archivos ORIGINALES tal como se grabaron y un
                       MP4 unido sin recomprimir. Si un vídeo no se ve en el navegador (por ejemplo, H.265),
                       ábrelo con VLC.
- manifiesto.json .... Lista de todos los archivos con su tamaño y su huella SHA-256.
- manifiesto.sig ..... Firma Ed25519 de manifiesto.json hecha por el sistema que exportó.
- clave-publica.pem .. Clave pública para comprobar la firma.

Cómo comprobar la integridad
----------------------------
IMPORTANTE: la clave pública viaja dentro del paquete. Una firma válida solo demuestra que el paquete
viene de la tienda si su clave (key_id) es la de la tienda: pide el key_id a la central (ficha de la
tienda) o cópialo del acta original firmada en papel, y compáralo.

1. Sin instalar nada: abre visor.html, pega el key_id de la tienda y elige la carpeta. Si todo
   coincide verás «Todo coincide». Sin el key_id, el visor avisa de que la clave no está comprobada.
   Si un archivo se ha cambiado, el visor lo marca en rojo.
   OJO: este visor.html viaja dentro del paquete y quien lo manipulara podría haberlo cambiado también.
   Sirve para ver los vídeos y como primera comprobación; como PRUEBA DE ORIGEN vale solo la de un
   programa de fuente fiable: la central o el punto 2.
2. Comprobación completa (huellas + firma + clave), en un equipo con el programa:
       python -m vms.ops.evidence verify {exp.export_id}.zip --key-id <key_id de la tienda>
   Sale con 0 si todo cuadra, 1 si algo no coincide y 3 si falta --key-id (paquete coherente pero
   clave sin comprobar).
3. A mano: calcula el SHA-256 de cada archivo (en Windows: certutil -hashfile <archivo> SHA256) y
   compáralo con manifiesto.json.

No cambies el nombre de los archivos ni los abras con programas que los guarden de nuevo: cualquier
cambio, aunque sea de un byte, hace que la huella deje de coincidir.
"""
