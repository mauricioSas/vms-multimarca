"""Genera los iconos que exige tauri-build (icons/icon.ico e icon.png) sin guardar binarios en el repo.

    python spikes/s1-webview2/app/make_icons.py
Necesita Pillow (BSD; ya está en el entorno de desarrollo y en el runner de CI se instala aparte).
"""
from pathlib import Path

from PIL import Image, ImageDraw

out = Path(__file__).resolve().parent / "src-tauri" / "icons"
out.mkdir(parents=True, exist_ok=True)
img = Image.new("RGBA", (256, 256), (17, 24, 39, 255))
d = ImageDraw.Draw(img)
d.rounded_rectangle((28, 60, 228, 196), radius=24, outline=(96, 165, 250, 255), width=14)
d.ellipse((104, 104, 152, 152), fill=(96, 165, 250, 255))
img.save(out / "icon.png")
img.save(out / "icon.ico", sizes=[(16, 16), (32, 32), (48, 48), (256, 256)])
print("iconos en", out)
