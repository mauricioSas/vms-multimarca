"""Genera el icono de la aplicación del visor (src-tauri/icons/icon.ico e icon.png).

    python native/viewer/tools/make_icons.py

Los iconos se guardan en el repositorio (pesan pocos KB) para que `cargo build` y `cargo test` funcionen sin
pasos previos; este script es la fuente y se vuelve a ejecutar solo si cambia el diseño. Salida determinista
(sin metadatos de fecha). Los iconos de la bandeja (verde, ámbar, rojo y gris) no son archivos: los dibuja el
propio visor en `src/tray_icons.rs`.

Necesita Pillow (BSD, solo en el equipo de desarrollo).
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parents[1] / "src-tauri" / "icons"
BG = (17, 24, 39, 255)        # gris pizarra
ACCENT = (56, 189, 248, 255)  # azul claro
WHITE = (241, 245, 249, 255)


def draw(size: int = 1024) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    s = size / 256
    d.rounded_rectangle((8 * s, 8 * s, 248 * s, 248 * s), radius=48 * s, fill=BG)
    # Cuadrícula 2x2 (el muro) con una celda resaltada
    cells = [(44, 60, 124, 120), (132, 60, 212, 120), (44, 128, 124, 188), (132, 128, 212, 188)]
    for i, (x0, y0, x1, y1) in enumerate(cells):
        d.rounded_rectangle((x0 * s, y0 * s, x1 * s, y1 * s), radius=10 * s,
                            fill=ACCENT if i == 0 else None, outline=WHITE, width=round(8 * s))
    # Punto de «en vivo»
    d.ellipse((112 * s, 200 * s, 144 * s, 232 * s), fill=(239, 68, 68, 255))
    return img


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    big = draw()
    png = big.resize((256, 256), Image.Resampling.LANCZOS)
    png.save(OUT / "icon.png", optimize=True)
    png.save(OUT / "icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (256, 256)])
    print("iconos en", OUT)


if __name__ == "__main__":
    main()
