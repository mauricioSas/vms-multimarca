"""Recorre el asistente del instalador y guarda una captura PNG de cada página (criterio 4 de B3).

Solo con un instalador de prueba (``TestBuild``): con ``/CAPTURESTATE=<archivo>`` el instalador escribe en ese
archivo la página que muestra («id|título»). Este módulo espera a cada página nueva, la captura con
``PrintWindow`` (sin depender de qué ventana esté delante) y pulsa el botón Siguiente/Instalar/Finalizar
enviando ``WM_COMMAND``/``BN_CLICKED`` a su ventana padre, que es lo que hace Windows al hacer clic. Sin
dependencias: ``ctypes`` y un escritor de PNG con ``zlib``.

    python -m tests.windows.wizard_capture --installer dist\\Setup.exe --out capturas -- /LOADINF=... /SECRETS=...
"""
from __future__ import annotations

import argparse
import ctypes
import json
import re
import struct
import subprocess
import sys
import time
import zlib
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path

WIZARD_CLASS = "TWizardForm"
#: Botón que avanza, sin «&» ni «>» (el estilo moderno de Inno 7 muestra «Siguiente» sin la flecha).
NEXT_CAPTIONS = ("siguiente", "instalar", "finalizar", "next", "install", "finish")
WP_INSTALLING = 12
WP_PREPARING = 11
WM_COMMAND = 0x0111
BN_CLICKED = 0
PW_RENDERFULLCONTENT = 2


def png_bytes(width: int, height: int, bgra: bytes) -> bytes:
    """PNG RGBA a partir de un búfer BGRA de arriba abajo."""
    rows = bytearray()
    stride = width * 4
    for y in range(height):
        row = bgra[y * stride:(y + 1) * stride]
        rows.append(0)
        for x in range(0, stride, 4):
            b, g, r, _ = row[x:x + 4]
            rows += bytes((r, g, b, 255))

    def chunk(tag: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(rows), 6)) + chunk(b"IEND", b""))


def normalize_caption(text: str) -> str:
    return text.replace("&", "").replace(">", "").replace("<", "").strip().lower()


def slug(text: str) -> str:
    text = text.lower()
    for a, b in (("á", "a"), ("é", "e"), ("í", "i"), ("ó", "o"), ("ú", "u"), ("ñ", "n"), ("ü", "u")):
        text = text.replace(a, b)
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")[:40] or "pagina"


class _Win32:
    def __init__(self) -> None:
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
        u, g, H = self.user32, self.gdi32, wintypes.HWND
        # Tipos explícitos: sin ellos, ctypes pasaría los manejadores de 64 bits como int de 32.
        u.GetClassNameW.argtypes = [H, wintypes.LPWSTR, ctypes.c_int]
        u.GetWindowTextW.argtypes = [H, wintypes.LPWSTR, ctypes.c_int]
        u.IsWindowVisible.argtypes = [H]
        u.IsWindowEnabled.argtypes = [H]
        u.GetWindowRect.argtypes = [H, ctypes.POINTER(wintypes.RECT)]
        u.PrintWindow.argtypes = [H, wintypes.HDC, wintypes.UINT]
        u.GetParent.argtypes = [H]
        u.GetParent.restype = H
        u.GetDlgCtrlID.argtypes = [H]
        u.PostMessageW.argtypes = [H, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        u.GetDC.argtypes = [H]
        u.GetDC.restype = wintypes.HDC
        u.ReleaseDC.argtypes = [H, wintypes.HDC]
        g.CreateCompatibleDC.argtypes = [wintypes.HDC]
        g.CreateCompatibleDC.restype = wintypes.HDC
        g.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
        g.CreateCompatibleBitmap.restype = wintypes.HBITMAP
        g.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
        g.SelectObject.restype = wintypes.HGDIOBJ
        g.BitBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HDC,
                             ctypes.c_int, ctypes.c_int, wintypes.DWORD]
        g.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT, ctypes.c_void_p,
                                ctypes.c_void_p, wintypes.UINT]
        g.DeleteObject.argtypes = [wintypes.HGDIOBJ]
        g.DeleteDC.argtypes = [wintypes.HDC]
        try:   # tamaños reales en píxeles aunque Windows escale
            self.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        except (AttributeError, OSError):
            pass

    def windows(self, class_name: str) -> list[int]:
        found: list[int] = []
        proto = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

        def cb(hwnd: int | None, _: int) -> bool:
            if hwnd and self.class_name(hwnd) == class_name and self.user32.IsWindowVisible(hwnd):
                found.append(hwnd)
            return True

        self.user32.EnumWindows(proto(cb), 0)
        return found

    def children(self, parent: int) -> list[tuple[int, str, str]]:
        out: list[tuple[int, str, str]] = []
        proto = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

        def cb(hwnd: int | None, _: int) -> bool:
            if hwnd:
                out.append((hwnd, self.class_name(hwnd), self.text(hwnd)))
            return True

        self.user32.EnumChildWindows(wintypes.HWND(parent), proto(cb), 0)
        return out

    def class_name(self, hwnd: int) -> str:
        buf = ctypes.create_unicode_buffer(256)
        self.user32.GetClassNameW(hwnd, buf, 256)
        return buf.value

    def text(self, hwnd: int) -> str:
        buf = ctypes.create_unicode_buffer(512)
        self.user32.GetWindowTextW(hwnd, buf, 512)
        return buf.value

    def capture(self, hwnd: int) -> tuple[int, int, bytes]:
        rect = wintypes.RECT()
        self.user32.GetWindowRect(hwnd, ctypes.byref(rect))
        w, h = rect.right - rect.left, rect.bottom - rect.top
        screen = self.user32.GetDC(None)
        mem = self.gdi32.CreateCompatibleDC(screen)
        bmp = self.gdi32.CreateCompatibleBitmap(screen, w, h)
        old = self.gdi32.SelectObject(mem, bmp)
        ok = self.user32.PrintWindow(hwnd, mem, PW_RENDERFULLCONTENT)
        if not ok:   # último recurso: copiar de la pantalla
            self.gdi32.BitBlt(mem, 0, 0, w, h, screen, rect.left, rect.top, 0x00CC0020)

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                        ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD),
                        ("biCompression", wintypes.DWORD), ("biSizeImage", wintypes.DWORD),
                        ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
                        ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]

        info = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
        buf = ctypes.create_string_buffer(w * h * 4)
        self.gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(info), 0)
        self.gdi32.SelectObject(mem, old)
        self.gdi32.DeleteObject(bmp)
        self.gdi32.DeleteDC(mem)
        self.user32.ReleaseDC(None, screen)
        return w, h, buf.raw

    def click(self, button: int) -> None:
        parent = self.user32.GetParent(button)
        ctrl_id = self.user32.GetDlgCtrlID(button)
        self.user32.PostMessageW(parent, WM_COMMAND, (BN_CLICKED << 16) | (ctrl_id & 0xFFFF), button)


@dataclass
class CaptureResult:
    pages: list[dict[str, object]] = field(default_factory=list)
    exit_code: int | None = None
    error: str = ""


def run(installer: Path, out: Path, args: list[str], *, timeout: float = 900, env: dict[str, str] | None = None,
        state_file: Path | None = None, stall: float = 90) -> CaptureResult:
    """Lanza el instalador, captura cada página y avanza. ``stall``: segundos sin página nueva tras un clic
    antes de darlo por atascado (p. ej. un mensaje de error de validación); entonces captura el diálogo."""
    if sys.platform != "win32":
        raise SystemExit("La captura del asistente solo funciona en Windows")
    out.mkdir(parents=True, exist_ok=True)
    state_file = state_file or out / "estado.txt"
    state_file.unlink(missing_ok=True)
    w32 = _Win32()
    result = CaptureResult()
    proc = subprocess.Popen([str(installer), f"/CAPTURESTATE={state_file}", *args], env=env)
    deadline = time.monotonic() + timeout
    last = ""
    index = 0
    waiting_since = time.monotonic()
    try:
        while True:
            if proc.poll() is not None:
                break
            now = time.monotonic()
            if now > deadline:
                result.error = f"Tiempo agotado ({timeout:.0f} s) en la página «{last}»"
                break
            state = state_file.read_text(encoding="latin-1").strip() if state_file.is_file() else ""
            wizards = w32.windows(WIZARD_CLASS)
            if not state or state == last or not wizards:
                page_id = int(last.partition("|")[0]) if last.partition("|")[0].isdigit() else -1
                if last and page_id not in (WP_INSTALLING, WP_PREPARING) and now - waiting_since > stall:
                    for i, dlg in enumerate(w32.windows("#32770")):
                        w, h, pixels = w32.capture(dlg)
                        (out / f"error-dialogo-{i}.png").write_bytes(png_bytes(w, h, pixels))
                    result.error = f"El asistente no avanzó desde «{last}» en {stall:.0f} s (¿mensaje de error?)"
                    break
                time.sleep(0.3)
                continue
            page_id_text, _, caption = state.partition("|")
            page_id = int(page_id_text) if page_id_text.isdigit() else -1
            time.sleep(1.2)                                  # que termine de pintarse
            hwnd = wizards[0]
            w, h, pixels = w32.capture(hwnd)
            index += 1
            name = f"{index:02d}-{slug(caption)}.png"
            (out / name).write_bytes(png_bytes(w, h, pixels))
            result.pages.append({"index": index, "page_id": page_id, "title": caption, "file": name,
                                 "size": [w, h]})
            last = state
            waiting_since = time.monotonic()
            if page_id in (WP_INSTALLING, WP_PREPARING):
                continue                                     # esperar a la página siguiente
            children = w32.children(hwnd)
            buttons = [c for c in children if normalize_caption(c[2]) in NEXT_CAPTIONS
                       and w32.user32.IsWindowVisible(c[0]) and w32.user32.IsWindowEnabled(c[0])]
            if not buttons:
                seen = sorted({f"{c[1]}:{c[2]}" for c in children if c[2]})[:40]
                result.error = f"No hay botón Siguiente/Instalar/Finalizar en la página «{caption}». Controles: {seen}"
                break
            w32.click(buttons[0][0])
        if result.error and proc.poll() is None:
            proc.kill()
        result.exit_code = proc.wait(timeout=120)
    finally:
        if proc.poll() is None:
            proc.kill()
    (out / "paginas.json").write_text(json.dumps(result.__dict__, indent=2, ensure_ascii=False), encoding="utf-8")
    return result


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m tests.windows.wizard_capture")
    p.add_argument("--installer", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("args", nargs="*", help="parámetros para el instalador (después de --)")
    a = p.parse_args(argv)
    res = run(a.installer, a.out, a.args)
    for page in res.pages:
        print(f"{page['index']:02d} {page['title']} -> {page['file']}")
    if res.error:
        print("ERROR: " + res.error, file=sys.stderr)
    return 0 if not res.error and res.exit_code == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
