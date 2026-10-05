"""Genera el icono y las imágenes del asistente (sin dependencias: struct + zlib). Dueño: B3.

    python -m distribution.installer.assets.make_assets          # reescribe vms.ico, wizard.bmp, wizard-small.bmp
    python -m distribution.installer.assets.make_assets --check  # falla si los archivos no coinciden (lo usa pytest)

Motivo: una cámara de vigilancia estilizada (cuerpo redondeado y objetivo con anillos) sobre azul noche. Todo
se dibuja con geometría y suavizado por supermuestreo, así el resultado es idéntico en cualquier equipo.
"""
from __future__ import annotations

import argparse
import math
import struct
import sys
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
NAVY_TOP = (16, 33, 62)
NAVY_BOTTOM = (9, 18, 36)
ACCENT = (56, 189, 248)       # azul claro (estado «en vivo»)
WHITE = (240, 244, 250)
RED = (239, 68, 68)            # piloto de grabación

Color = tuple[int, int, int]
Pixel = tuple[int, int, int, int]


def _mix(a: Color, b: Color, t: float) -> Color:
    return (round(a[0] + (b[0] - a[0]) * t), round(a[1] + (b[1] - a[1]) * t), round(a[2] + (b[2] - a[2]) * t))


def _rounded_rect(x: float, y: float, x0: float, y0: float, x1: float, y1: float, r: float) -> bool:
    cx = min(max(x, x0 + r), x1 - r)
    cy = min(max(y, y0 + r), y1 - r)
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r and x0 <= x <= x1 and y0 <= y <= y1


def _camera_shape(u: float, v: float) -> Color | None:
    """Color del motivo en coordenadas normalizadas (0..1), o None si es fondo."""
    lx, ly = 0.42, 0.55          # centro del objetivo
    d = math.hypot(u - lx, v - ly)
    if d <= 0.075:
        return WHITE
    if d <= 0.12:
        return NAVY_BOTTOM
    if d <= 0.165:
        return ACCENT
    if math.hypot(u - 0.61, v - 0.42) <= 0.032:                # piloto de grabación
        return RED
    if _rounded_rect(u, v, 0.14, 0.33, 0.70, 0.77, 0.09):
        return _mix(WHITE, (200, 210, 225), (v - 0.33) / 0.44)
    if _rounded_rect(u, v, 0.70, 0.43, 0.88, 0.67, 0.04):   # visera / soporte
        return (200, 210, 225)
    return None


def render(width: int, height: int, *, background: bool, motif_box: tuple[float, float, float, float],
           ss: int = 4) -> list[list[Pixel]]:
    """Imagen RGBA. ``motif_box`` (x, y, lado, —) en fracciones del ancho/alto donde va el motivo cuadrado."""
    bx, by, side, _ = motif_box
    rows: list[list[Pixel]] = []
    for y in range(height):
        row: list[Pixel] = []
        for x in range(width):
            acc = [0, 0, 0, 0]
            for sy in range(ss):
                for sx in range(ss):
                    px = x + (sx + 0.5) / ss
                    py = y + (sy + 0.5) / ss
                    u = (px - bx * width) / (side * width)
                    v = (py - by * height) / (side * width)
                    col = _camera_shape(u, v) if 0 <= u <= 1 and 0 <= v <= 1 else None
                    if col is not None:
                        a = 255
                    elif background:
                        col = _mix(NAVY_TOP, NAVY_BOTTOM, py / height)
                        a = 255
                    else:
                        col, a = (0, 0, 0), 0
                    acc[0] += col[0] * a
                    acc[1] += col[1] * a
                    acc[2] += col[2] * a
                    acc[3] += a
            n = ss * ss
            alpha = acc[3] // n
            if acc[3]:
                row.append((acc[0] // acc[3], acc[1] // acc[3], acc[2] // acc[3], alpha))
            else:
                row.append((0, 0, 0, 0))
        rows.append(row)
    return rows


def bmp24(rows: list[list[Pixel]]) -> bytes:
    """BMP de 24 bits sin compresión (lo que espera Inno Setup)."""
    height, width = len(rows), len(rows[0])
    stride = (width * 3 + 3) & ~3
    data = bytearray()
    for row in reversed(rows):                       # BMP: de abajo arriba
        line = bytearray()
        for r, g, b, _ in row:
            line += bytes((b, g, r))
        line += b"\0" * (stride - len(line))
        data += line
    header = struct.pack("<2sIHHI", b"BM", 54 + len(data), 0, 0, 54)
    info = struct.pack("<IiiHHIIiiII", 40, width, height, 1, 24, 0, len(data), 2835, 2835, 0, 0)
    return header + info + bytes(data)


def png(rows: list[list[Pixel]]) -> bytes:
    height, width = len(rows), len(rows[0])
    raw = bytearray()
    for row in rows:
        raw.append(0)
        for px in row:
            raw += bytes(px)

    def chunk(tag: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b""))


def ico(sizes: tuple[int, ...] = (16, 24, 32, 48, 64, 256)) -> bytes:
    """Icono con imágenes PNG por tamaño (formato admitido desde Windows Vista)."""
    images = [png(render(s, s, background=True, motif_box=(0.0, 0.0, 1.0, 1.0), ss=4 if s <= 64 else 2))
              for s in sizes]
    header = struct.pack("<HHH", 0, 1, len(sizes))
    offset = 6 + 16 * len(sizes)
    entries = b""
    for s, img in zip(sizes, images, strict=True):
        dim = 0 if s >= 256 else s
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(img), offset)
        offset += len(img)
    return header + entries + b"".join(images)


def build() -> dict[str, bytes]:
    return {
        "vms.ico": ico(),
        # Asistente moderno de Inno: imagen grande 164x314 y pequeña 55x55 (al 100 %).
        "wizard.bmp": bmp24(render(164, 314, background=True, motif_box=(0.12, 0.36, 0.76, 0.0), ss=3)),
        "wizard-small.bmp": bmp24(render(55, 55, background=True, motif_box=(0.0, 0.0, 1.0, 1.0), ss=4)),
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m distribution.installer.assets.make_assets")
    p.add_argument("--check", action="store_true", help="comprobar sin escribir")
    args = p.parse_args(argv)
    bad = []
    for name, data in build().items():
        path = HERE / name
        if args.check:
            if not path.is_file() or path.read_bytes() != data:
                bad.append(name)
        else:
            path.write_bytes(data)
            print(f"{name}: {len(data)} B")
    if bad:
        print("Desactualizados (python -m distribution.installer.assets.make_assets): " + ", ".join(bad),
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
