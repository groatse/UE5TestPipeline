"""Screenshots of the game window from outside the process, for the supervising agent.

Uses PrintWindow with PW_RENDERFULLCONTENT, which copies a DirectX window's
content through the compositor whether or not it is in front. The game does
no readback, so a screenshot doesn't cost it a frame the way an in-engine
screenshot would. Windows only; standard library only.
"""

from __future__ import annotations

import ctypes
import struct
import sys
import zlib
from pathlib import Path

PW_RENDERFULLCONTENT = 0x2


class ScreenshotError(RuntimeError):
    pass


def _user32():
    if sys.platform != "win32":
        raise ScreenshotError("screenshots need Windows")
    from ctypes import wintypes
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.EnumWindows.argtypes = [ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM), wintypes.LPARAM]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetWindowDC.argtypes = [wintypes.HWND]
    user32.GetWindowDC.restype = wintypes.HDC
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
    return user32


def find_window(pid: int) -> int | None:
    """The largest visible top-level window of process `pid`."""
    from ctypes import wintypes
    user32 = _user32()
    found: list[tuple[int, int]] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd, _):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd):
            rect = wintypes.RECT()
            user32.GetClientRect(hwnd, ctypes.byref(rect))
            found.append(((rect.right - rect.left) * (rect.bottom - rect.top), hwnd))
        return True

    user32.EnumWindows(visit, 0)
    found = [f for f in found if f[0] > 0]
    return max(found)[1] if found else None


def capture(hwnd: int) -> tuple[int, int, bytes]:
    """Client area of `hwnd` as (width, height, RGB bytes top to bottom)."""
    from ctypes import wintypes
    user32 = _user32()
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
    gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    gdi32.SelectObject.restype = wintypes.HGDIOBJ
    gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT, ctypes.c_void_p,
                                ctypes.c_void_p, wintypes.UINT]
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi32.DeleteDC.argtypes = [wintypes.HDC]

    rect = wintypes.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(rect))
    w, h = rect.right - rect.left, rect.bottom - rect.top
    if w <= 0 or h <= 0:
        raise ScreenshotError("the game window has no client area (minimized?)")

    window_dc = user32.GetWindowDC(hwnd)
    mem_dc = gdi32.CreateCompatibleDC(window_dc)
    bitmap = gdi32.CreateCompatibleBitmap(window_dc, w, h)
    old = gdi32.SelectObject(mem_dc, bitmap)
    try:
        # Flag 1 (PW_CLIENTONLY) plus full content, so borders and title bar stay out.
        if not user32.PrintWindow(hwnd, mem_dc, 0x1 | PW_RENDERFULLCONTENT):
            raise ScreenshotError("PrintWindow failed")
        # BITMAPINFOHEADER for a top-down 32-bit DIB.
        header = struct.pack("<IiiHHIIiiII", 40, w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
        info = ctypes.create_string_buffer(header + b"\0" * 4)
        pixels = ctypes.create_string_buffer(w * h * 4)
        if not gdi32.GetDIBits(mem_dc, bitmap, 0, h, pixels, info, 0):
            raise ScreenshotError("GetDIBits failed")
    finally:
        gdi32.SelectObject(mem_dc, old)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(hwnd, window_dc)

    bgra = pixels.raw
    rgb = bytearray(w * h * 3)
    rgb[0::3], rgb[1::3], rgb[2::3] = bgra[2::4], bgra[1::4], bgra[0::4]
    return w, h, bytes(rgb)


def downscale(w: int, h: int, rgb: bytes, factor: int) -> tuple[int, int, bytes]:
    """Keep every `factor`-th pixel in both directions; enough for an agent to read the screen."""
    if factor <= 1:
        return w, h, rgb
    nw, nh = w // factor, h // factor
    out = bytearray()
    row = w * 3
    for y in range(nh):
        line = rgb[y * factor * row:(y * factor + 1) * row]
        src = memoryview(line)
        px = bytearray(nw * 3)
        px[0::3], px[1::3], px[2::3] = src[0::3 * factor][:nw], src[1::3 * factor][:nw], src[2::3 * factor][:nw]
        out += px
    return nw, nh, bytes(out)


def write_png(path: Path, w: int, h: int, rgb: bytes) -> Path:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    row = w * 3
    raw = b"".join(b"\0" + rgb[y * row:(y + 1) * row] for y in range(h))
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png)
    return path


def screenshot_process(pid: int, path: Path, factor: int = 2) -> Path:
    hwnd = find_window(pid)
    if hwnd is None:
        raise ScreenshotError(f"no visible window for process {pid} yet")
    return write_png(path, *downscale(*capture(hwnd), factor))
