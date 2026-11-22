"""Native zero-dependency Windows 10/11 Computer Use backend (pure ctypes Win32 + GDI+ + UI Automation COM).

Subsystems:
- Per-Monitor DPI Awareness v2 (exact physical pixel coordinates on 100%-300% scaled & multi-monitor setups).
- WinInputDriver: SendInput with exact 64-bit/32-bit INPUT union, MOUSEEVENTF_VIRTUALDESK (supports
  negative monitor offsets), KEYEVENTF_EXTENDEDKEY, hardware scan codes (MapVirtualKeyW), and
  layout-independent KEYEVENTF_UNICODE UTF-16-LE typing (surrogate-pair safe).
- WinScreenCapture: GDI BitBlt (SRCCOPY | CAPTUREBLT) + PrintWindow (PW_RENDERFULLCONTENT for background windows),
  TurboJPEG (if installed) or built-in Windows GDI+ exact-length IStream JPEG/PNG encoder with crisp
  zero-dep 5x7 bitmap labels for Set-of-Marks [ref] badges & 0..1000 coordinate grids.
- WinWindowManager: EnumWindows filtering DWMWA_CLOAKED UWP ghost windows, DWMWA_EXTENDED_FRAME_BOUNDS rects,
  per-monitor fullscreen detection, DWM-border-compensated move_resize, reliable focus, and clean launch.
- WinClipboard: OpenClipboard with transient lock retry + 64-bit safe CF_UNICODETEXT read/write.
- WinA11yTree: Native in-process MTA IUIAutomation COM vtable walker (ControlViewWalker) with leak-free
  refcounting, stable [ref] IDs, incremental AX_DIFF, and mouse-free InvokePattern / TogglePattern actions.
- WinBackgroundSession: Child-HWND aware PrintWindow + PostMessageW automation (zero cursor movement,
  zero focus stealing, zero clipboard touch).
"""

from __future__ import annotations

import atexit
import base64
import ctypes
import math
import os
import shlex
import struct
import subprocess
import sys
import time
from typing import Any, Callable

import numpy as np

from capture import Frame, TurboJpegEncoder, _apply_ignore_rects

# ---------------------------------------------------------------------------
# Pure-Python Windows keycode, coordinate, & bitmap-font helpers (testable on any OS)
# ---------------------------------------------------------------------------

WIN_VK_CODES: dict[str, int] = {
    "backspace": 0x08, "bs": 0x08,
    "tab": 0x09,
    "enter": 0x0D, "return": 0x0D,
    "shift": 0x10, "lshift": 0xA0, "leftshift": 0xA0, "rshift": 0xA1, "rightshift": 0xA1,
    "ctrl": 0x11, "control": 0x11, "lctrl": 0xA2, "leftctrl": 0xA2, "rctrl": 0xA3, "rightctrl": 0xA3,
    "alt": 0x12, "lalt": 0xA4, "leftalt": 0xA4, "ralt": 0xA5, "rightalt": 0xA5, "altgr": 0xA5,
    "pause": 0x13, "capslock": 0x14,
    "esc": 0x1B, "escape": 0x1B,
    "space": 0x20, " ": 0x20,
    "pageup": 0x21, "pgup": 0x21, "pagedown": 0x22, "pgdn": 0x22,
    "end": 0x23, "home": 0x24,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "print": 0x2C, "printscreen": 0x2C,
    "insert": 0x2D, "delete": 0x2E, "del": 0x2E,
    "0": 0x30, "1": 0x31, "2": 0x32, "3": 0x33, "4": 0x34,
    "5": 0x35, "6": 0x36, "7": 0x37, "8": 0x38, "9": 0x39,
    "a": 0x41, "b": 0x42, "c": 0x43, "d": 0x44, "e": 0x45, "f": 0x46, "g": 0x47,
    "h": 0x48, "i": 0x49, "j": 0x4A, "k": 0x4B, "l": 0x4C, "m": 0x4D, "n": 0x4E,
    "o": 0x4F, "p": 0x50, "q": 0x51, "r": 0x52, "s": 0x53, "t": 0x54, "u": 0x55,
    "v": 0x56, "w": 0x57, "x": 0x58, "y": 0x59, "z": 0x5A,
    "super": 0x5B, "meta": 0x5B, "win": 0x5B, "cmd": 0x5B, "lmeta": 0x5B, "leftmeta": 0x5B,
    "rmeta": 0x5C, "rightmeta": 0x5C, "menu": 0x5D,
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74, "f6": 0x75,
    "f7": 0x76, "f8": 0x77, "f9": 0x78, "f10": 0x79, "f11": 0x7A, "f12": 0x7B,
    "mute": 0xAD, "volumedown": 0xAE, "volumeup": 0xAF,
    "nextsong": 0xB0, "previoussong": 0xB1, "playpause": 0xB3,
    ";": 0xBA, "semicolon": 0xBA, "=": 0xBB, "equal": 0xBB, "+": 0xBB,
    ",": 0xBC, "comma": 0xBC, "-": 0xBD, "minus": 0xBD,
    ".": 0xBE, "dot": 0xBE, "period": 0xBE, "/": 0xBF, "slash": 0xBF,
    "`": 0xC0, "grave": 0xC0, "[": 0xDB, "leftbrace": 0xDB,
    "\\": 0xDC, "backslash": 0xDC, "]": 0xDD, "rightbrace": 0xDD,
    "'": 0xDE, "apostrophe": 0xDE,
}

EXTENDED_VKS: set[int] = {
    0xA3, 0xA5, 0x5B, 0x5C, 0x2D, 0x2E, 0x24, 0x23, 0x21, 0x22,
    0x25, 0x26, 0x27, 0x28, 0x2C,
}

# Crisp 5x7 bitmap font for digits 0-9 (zero-dependency Set-of-Marks [ref] & 100..900 grid labels)
_FONT_5X7: dict[str, tuple[str, ...]] = {
    "0": ("01110", "10001", "10011", "10101", "11001", "10001", "01110"),
    "1": ("00100", "01100", "00100", "00100", "00100", "00100", "01110"),
    "2": ("01110", "10001", "00001", "00110", "01000", "10000", "11111"),
    "3": ("11110", "00001", "00001", "01110", "00001", "00001", "11110"),
    "4": ("00010", "00110", "01010", "10010", "11111", "00010", "00010"),
    "5": ("11111", "10000", "11110", "00001", "00001", "10001", "01110"),
    "6": ("01110", "10000", "11110", "10001", "10001", "10001", "01110"),
    "7": ("11111", "00001", "00010", "00100", "01000", "01000", "01000"),
    "8": ("01110", "10001", "10001", "01110", "10001", "10001", "01110"),
    "9": ("01110", "10001", "10001", "01111", "00001", "00001", "01110"),
}


def blit_pill_digits_bgra(
    bgra: np.ndarray,
    x: int,
    y: int,
    text: str,
    *,
    bg_bgr: tuple[int, int, int],
    fg_bgr: tuple[int, int, int] = (255, 255, 255),
) -> None:
    """Draw a crisp filled label pill with 5x7 bitmap digits directly into a BGRA numpy buffer."""
    h, w = bgra.shape[:2]
    digits = [ch for ch in text if ch in _FONT_5X7]
    if not digits:
        return
    pw = len(digits) * 6 + 5
    ph = 11
    x0 = max(0, min(w - pw, int(x)))
    y0 = max(0, min(h - ph, int(y)))
    x1 = min(w, x0 + pw)
    y1 = min(h, y0 + ph)
    if x1 <= x0 or y1 <= y0:
        return
    bgra[y0:y1, x0:x1, 0] = bg_bgr[0]
    bgra[y0:y1, x0:x1, 1] = bg_bgr[1]
    bgra[y0:y1, x0:x1, 2] = bg_bgr[2]
    bgra[y0:y1, x0:x1, 3] = 255
    cx = x0 + 3
    cy = y0 + 2
    for ch in digits:
        glyph = _FONT_5X7[ch]
        for r_idx, row in enumerate(glyph):
            py = cy + r_idx
            if py >= y1:
                break
            for c_idx, bit in enumerate(row):
                px = cx + c_idx
                if bit == "1" and px < x1:
                    bgra[py, px, 0] = fg_bgr[0]
                    bgra[py, px, 1] = fg_bgr[1]
                    bgra[py, px, 2] = fg_bgr[2]
        cx += 6


def parse_win_key_combo(keys: str | list[str]) -> list[tuple[str, int]]:
    """Parse 'ctrl+shift+t', 'ctrl++', or ['win', 'r'] into [(name, vk_code), ...]."""
    from uinput_driver import _split_combo_tokens

    raw = _split_combo_tokens(keys)
    if not raw:
        raise ValueError("Empty key specification")
    out: list[tuple[str, int]] = []
    for tok in raw:
        low = tok.lower()
        if low in WIN_VK_CODES:
            out.append((low, WIN_VK_CODES[low]))
        else:
            raise ValueError(f"Unknown Windows key name: {tok!r}")
    return out


def win_virtual_to_abs(x: int, y: int, bounds: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    """Map physical screen pixel (x, y) to MOUSEEVENTF_ABSOLUTE|VIRTUALDESK (0..65535) range."""
    vx, vy, vw, vh = bounds
    cx = max(vx, min(vx + vw - 1, int(round(x))))
    cy = max(vy, min(vy + vh - 1, int(round(y))))
    ax = int(round((cx - vx) * 65535.0 / max(1, vw - 1)))
    ay = int(round((cy - vy) * 65535.0 / max(1, vh - 1)))
    return cx, cy, ax, ay


def text_to_utf16_units(text: str) -> list[int]:
    """Normalize CRLF/CR -> LF and encode arbitrary Unicode (including astral surrogates) to UTF-16-LE units."""
    norm = text.replace("\r\n", "\n").replace("\r", "\n")
    raw = norm.encode("utf-16-le")
    return list(struct.unpack(f"<{len(raw) // 2}H", raw)) if raw else []


# Exact SendInput structs (platform-independent layout checkable on Linux too)
_ULONG_PTR = ctypes.c_uint64 if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_uint32


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_int32),
        ("dy", ctypes.c_int32),
        ("mouseData", ctypes.c_uint32),
        ("dwFlags", ctypes.c_uint32),
        ("time", ctypes.c_uint32),
        ("dwExtraInfo", _ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_uint16),
        ("wScan", ctypes.c_uint16),
        ("dwFlags", ctypes.c_uint32),
        ("time", ctypes.c_uint32),
        ("dwExtraInfo", _ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", ctypes.c_uint32),
        ("wParamL", ctypes.c_uint16),
        ("wParamH", ctypes.c_uint16),
    ]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("_u",)
    _fields_ = [("type", ctypes.c_uint32), ("_u", _INPUT_UNION)]


# ---------------------------------------------------------------------------
# Live Win32 runtime bindings (only invoked on sys.platform == 'win32')
# ---------------------------------------------------------------------------

_DPI_INITIALIZED = False


def ensure_dpi_awareness() -> None:
    global _DPI_INITIALIZED
    if _DPI_INITIALIZED or sys.platform != "win32":
        return
    _DPI_INITIALIZED = True
    try:
        user32 = ctypes.windll.user32
        user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        user32.SetProcessDpiAwarenessContext.restype = ctypes.c_int
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except Exception:
        pass
    try:
        shcore = ctypes.windll.shcore
        shcore.SetProcessDpiAwareness.argtypes = [ctypes.c_int]
        shcore.SetProcessDpiAwareness.restype = ctypes.c_long
        shcore.SetProcessDpiAwareness(2)
        return
    except Exception:
        pass
    try:
        user32 = ctypes.windll.user32
        user32.SetProcessDPIAware.argtypes = []
        user32.SetProcessDPIAware.restype = ctypes.c_int
        user32.SetProcessDPIAware()
    except Exception:
        pass


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    @classmethod
    def from_str(cls, s: str) -> "_GUID":
        s = s.strip("{}")
        parts = s.split("-")
        d1 = int(parts[0], 16)
        d2 = int(parts[1], 16)
        d3 = int(parts[2], 16)
        d4_hex = parts[3] + parts[4]
        d4 = (ctypes.c_ubyte * 8)(*[int(d4_hex[i : i + 2], 16) for i in range(0, 16, 2)])
        return cls(d1, d2, d3, d4)


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_int32), ("top", ctypes.c_int32), ("right", ctypes.c_int32), ("bottom", ctypes.c_int32)]


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_int32), ("y", ctypes.c_int32)]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint32),
        ("rcMonitor", _RECT),
        ("rcWork", _RECT),
        ("dwFlags", ctypes.c_uint32),
    ]


# ---------------------------------------------------------------------------
# WinWindowManager
# ---------------------------------------------------------------------------


class WinWindowManager:
    """Native Win32 window manager (EnumWindows + DWMWA_CLOAKED + per-monitor fullscreen + robust focus)."""

    def __init__(self) -> None:
        ensure_dpi_awareness()
        self._user32 = ctypes.windll.user32
        self._kernel32 = ctypes.windll.kernel32
        try:
            self._dwmapi = ctypes.windll.dwmapi
        except Exception:
            self._dwmapi = None
        self._setup_sigs()

    def _setup_sigs(self) -> None:
        from ctypes import wintypes

        u = self._user32
        k = self._kernel32
        self._WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        u.GetSystemMetrics.argtypes = [ctypes.c_int]
        u.GetSystemMetrics.restype = ctypes.c_int
        u.GetCursorPos.argtypes = [ctypes.POINTER(_POINT)]
        u.GetCursorPos.restype = wintypes.BOOL
        u.GetForegroundWindow.argtypes = []
        u.GetForegroundWindow.restype = wintypes.HWND
        u.EnumWindows.argtypes = [self._WNDENUMPROC, wintypes.LPARAM]
        u.EnumWindows.restype = wintypes.BOOL
        u.IsWindowVisible.argtypes = [wintypes.HWND]
        u.IsWindowVisible.restype = wintypes.BOOL
        u.IsIconic.argtypes = [wintypes.HWND]
        u.IsIconic.restype = wintypes.BOOL
        u.IsZoomed.argtypes = [wintypes.HWND]
        u.IsZoomed.restype = wintypes.BOOL
        u.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        u.GetWindowTextLengthW.restype = ctypes.c_int
        u.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        u.GetWindowTextW.restype = ctypes.c_int
        u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(_RECT)]
        u.GetWindowRect.restype = wintypes.BOOL
        u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        u.GetWindowThreadProcessId.restype = wintypes.DWORD
        u.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        u.ShowWindow.restype = wintypes.BOOL
        u.SetForegroundWindow.argtypes = [wintypes.HWND]
        u.SetForegroundWindow.restype = wintypes.BOOL
        u.BringWindowToTop.argtypes = [wintypes.HWND]
        u.BringWindowToTop.restype = wintypes.BOOL
        u.AllowSetForegroundWindow.argtypes = [wintypes.DWORD]
        u.AllowSetForegroundWindow.restype = wintypes.BOOL
        u.SwitchToThisWindow.argtypes = [wintypes.HWND, wintypes.BOOL]
        u.SwitchToThisWindow.restype = None
        u.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
        u.AttachThreadInput.restype = wintypes.BOOL
        u.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
        u.SetWindowPos.restype = wintypes.BOOL
        u.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        u.PostMessageW.restype = wintypes.BOOL
        u.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
        u.GetWindowLongW.restype = ctypes.c_long
        u.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
        u.MonitorFromWindow.restype = wintypes.HMONITOR
        u.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(_MONITORINFO)]
        u.GetMonitorInfoW.restype = wintypes.BOOL

        k.GetCurrentThreadId.argtypes = []
        k.GetCurrentThreadId.restype = wintypes.DWORD
        k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k.OpenProcess.restype = wintypes.HANDLE
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        k.CloseHandle.restype = wintypes.BOOL
        k.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        k.QueryFullProcessImageNameW.restype = wintypes.BOOL

        if self._dwmapi is not None:
            self._dwmapi.DwmGetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
            self._dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long

    @property
    def backend(self) -> str:
        return "win32-dwm"

    def virtual_bounds(self) -> tuple[int, int, int, int]:
        vx = int(self._user32.GetSystemMetrics(76))  # SM_XVIRTUALSCREEN
        vy = int(self._user32.GetSystemMetrics(77))  # SM_YVIRTUALSCREEN
        vw = int(self._user32.GetSystemMetrics(78))  # SM_CXVIRTUALSCREEN
        vh = int(self._user32.GetSystemMetrics(79))  # SM_CYVIRTUALSCREEN
        if vw <= 0 or vh <= 0:
            vw = int(self._user32.GetSystemMetrics(0)) or 1920
            vh = int(self._user32.GetSystemMetrics(1)) or 1080
            vx, vy = 0, 0
        return vx, vy, vw, vh

    def screen_size(self) -> tuple[int, int]:
        _, _, vw, vh = self.virtual_bounds()
        return vw, vh

    def cursor_pos(self) -> tuple[int, int]:
        pt = _POINT()
        self._user32.GetCursorPos(ctypes.byref(pt))
        return int(pt.x), int(pt.y)

    def _is_cloaked(self, hwnd: int) -> bool:
        if self._dwmapi is None:
            return False
        cloaked = ctypes.c_uint32(0)
        hr = self._dwmapi.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(cloaked), 4)
        return hr == 0 and cloaked.value != 0

    def _window_rect(self, hwnd: int) -> tuple[int, int, int, int]:
        rc = _RECT()
        if self._dwmapi is not None:
            if self._dwmapi.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(rc), ctypes.sizeof(rc)) == 0:
                w, h = rc.right - rc.left, rc.bottom - rc.top
                if w > 0 and h > 0:
                    return rc.left, rc.top, w, h
        self._user32.GetWindowRect(hwnd, ctypes.byref(rc))
        return rc.left, rc.top, max(0, rc.right - rc.left), max(0, rc.bottom - rc.top)

    def _is_fullscreen(self, hwnd: int, x: int, y: int, w: int, h: int) -> bool:
        hmon = self._user32.MonitorFromWindow(hwnd, 2)  # MONITOR_DEFAULTTONEAREST
        if not hmon:
            return False
        mi = _MONITORINFO()
        mi.cbSize = ctypes.sizeof(_MONITORINFO)
        if not self._user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            return False
        mr = mi.rcMonitor
        return x <= mr.left and y <= mr.top and (x + w) >= mr.right and (y + h) >= mr.bottom

    def _proc_name(self, pid: int) -> str:
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        hproc = self._kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not hproc:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(512)
            sz = wintypes.DWORD(512)
            if self._kernel32.QueryFullProcessImageNameW(hproc, 0, buf, ctypes.byref(sz)):
                return os.path.splitext(os.path.basename(buf.value))[0]
        finally:
            self._kernel32.CloseHandle(hproc)
        return ""

    def state(self) -> dict[str, Any]:
        from ctypes import wintypes

        vx, vy, vw, vh = self.virtual_bounds()
        cx, cy = self.cursor_pos()
        fg = int(self._user32.GetForegroundWindow() or 0)
        wins: list[dict[str, Any]] = []
        GWL_EXSTYLE = -20
        WS_EX_TOOLWINDOW = 0x00000080

        def cb(hwnd, _lparam):
            if not self._user32.IsWindowVisible(hwnd):
                return True
            if self._is_cloaked(hwnd):
                return True
            n = self._user32.GetWindowTextLengthW(hwnd)
            if n <= 0:
                return True
            ex = self._user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            if ex & WS_EX_TOOLWINDOW:
                return True
            buf = ctypes.create_unicode_buffer(n + 1)
            self._user32.GetWindowTextW(hwnd, buf, n + 1)
            title = buf.value.strip()
            if not title:
                return True
            x, y, w, h = self._window_rect(hwnd)
            if w <= 1 or h <= 1:
                return True
            pid = wintypes.DWORD(0)
            self._user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            app = self._proc_name(pid.value) or "window"
            wins.append({
                "id": str(int(hwnd)),
                "hwnd": int(hwnd),
                "title": title,
                "app": app,
                "pid": int(pid.value),
                "x": x, "y": y, "w": w, "h": h,
                "active": int(hwnd) == fg,
                "minimized": bool(self._user32.IsIconic(hwnd)),
                "fullscreen": self._is_fullscreen(hwnd, x, y, w, h),
            })
            return True

        self._user32.EnumWindows(self._WNDENUMPROC(cb), 0)
        return {
            "screen": {"x": vx, "y": vy, "w": vw, "h": vh},
            "cursor": {"x": cx, "y": cy},
            "windows": wins,
            "active": next((w for w in wins if w["active"]), None),
        }

    def find_window(self, *, window_id: str | None = None, app: str | None = None, title: str | None = None) -> dict[str, Any] | None:
        wins = self.state()["windows"]
        if window_id:
            for w in wins:
                if w["id"] == str(window_id):
                    return w
        if app:
            q = app.lower()
            for w in wins:
                if q in w["app"].lower():
                    return w
        if title:
            q = title.lower()
            for w in wins:
                if q in w["title"].lower():
                    return w
        return None

    def control(
        self,
        action: str,
        *,
        window_id: str | None = None,
        app: str | None = None,
        title: str | None = None,
        x: int | None = None,
        y: int | None = None,
        w: int | None = None,
        h: int | None = None,
    ) -> dict[str, Any]:
        target = self.find_window(window_id=window_id, app=app, title=title)
        if target is None:
            raise ValueError(f"Window not found (window_id={window_id!r}, app={app!r}, title={title!r})")
        hwnd = int(target["id"])
        act = action.lower()
        if act == "focus":
            if self._user32.IsIconic(hwnd):
                self._user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            self._user32.AllowSetForegroundWindow(0xFFFFFFFF)  # ASFW_ANY
            fg = self._user32.GetForegroundWindow()
            cur_tid = self._kernel32.GetCurrentThreadId()
            fg_tid = self._user32.GetWindowThreadProcessId(fg, None) if fg else 0
            attached = False
            if fg_tid and fg_tid != cur_tid:
                attached = bool(self._user32.AttachThreadInput(cur_tid, fg_tid, True))
            try:
                self._user32.BringWindowToTop(hwnd)
                ok = bool(self._user32.SetForegroundWindow(hwnd))
            finally:
                if attached:
                    self._user32.AttachThreadInput(cur_tid, fg_tid, False)
            if not ok:
                # Fallback when foreground window is elevated (UIPI) or locked
                self._user32.SwitchToThisWindow(hwnd, True)
        elif act == "minimize":
            self._user32.ShowWindow(hwnd, 6)  # SW_MINIMIZE
        elif act == "maximize":
            self._user32.ShowWindow(hwnd, 3)  # SW_MAXIMIZE
        elif act == "unmaximize":
            self._user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        elif act == "close":
            self._user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
        elif act == "move_resize":
            if self._user32.IsIconic(hwnd) or self._user32.IsZoomed(hwnd):
                self._user32.ShowWindow(hwnd, 9)
                time.sleep(0.03)
            # Re-query restored rect so minimized (-32000,-32000) never teleports the window off-screen
            vis_x, vis_y, vis_w, vis_h = self._window_rect(hwnd)
            raw_rc = _RECT()
            self._user32.GetWindowRect(hwnd, ctypes.byref(raw_rc))
            pad_l = vis_x - raw_rc.left
            pad_t = vis_y - raw_rc.top
            pad_w = (raw_rc.right - raw_rc.left) - vis_w
            pad_h = (raw_rc.bottom - raw_rc.top) - vis_h
            nx = (vis_x if x is None else int(x)) - pad_l
            ny = (vis_y if y is None else int(y)) - pad_t
            nw = (vis_w if w is None else int(w)) + pad_w
            nh = (vis_h if h is None else int(h)) + pad_h
            self._user32.SetWindowPos(hwnd, None, nx, ny, nw, nh, 0x0004)  # SWP_NOZORDER
        else:
            raise ValueError(f"Unsupported window action: {action!r}")
        return {**target, "action": act}

    def launch(self, command: str | list[str], *, wait_app: str | None = None, timeout_ms: int = 5000) -> dict[str, Any]:
        if isinstance(command, str):
            raw_tokens = shlex.split(command, posix=False)
            argv = [t[1:-1] if len(t) >= 2 and t[0] == t[-1] and t[0] in ('"', "'") else t for t in raw_tokens]
        else:
            argv = [str(a) for a in command]
        if not argv:
            raise ValueError("Empty launch command")
        before_ids = {w["id"] for w in self.state()["windows"]}
        # CREATE_NEW_PROCESS_GROUP without DETACHED_PROCESS so console apps (cmd/powershell) create a visible window
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        proc = subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=CREATE_NEW_PROCESS_GROUP,
        )
        appeared = None
        if wait_app is not None or timeout_ms > 0:
            deadline = time.monotonic() + max(0.2, timeout_ms / 1000.0)
            q = (wait_app or os.path.splitext(os.path.basename(argv[0]))[0]).lower()
            while time.monotonic() < deadline:
                time.sleep(0.06)
                for w in self.state()["windows"]:
                    if w["id"] not in before_ids:
                        if not wait_app or q in (w["app"] + " " + w["title"]).lower():
                            appeared = w
                            break
                if appeared:
                    break
        return {"pid": proc.pid, "command": argv, "window": appeared}


# ---------------------------------------------------------------------------
# WinInputDriver (SendInput + Unicode + VirtualDesk + MapVirtualKeyW)
# ---------------------------------------------------------------------------


class WinInputDriver:
    """Native Windows SendInput pointer & layout-independent Unicode keyboard driver."""

    INPUT_MOUSE = 0
    INPUT_KEYBOARD = 1
    MOUSEEVENTF_MOVE = 0x0001
    MOUSEEVENTF_LEFTDOWN = 0x0002
    MOUSEEVENTF_LEFTUP = 0x0004
    MOUSEEVENTF_RIGHTDOWN = 0x0008
    MOUSEEVENTF_RIGHTUP = 0x0010
    MOUSEEVENTF_MIDDLEDOWN = 0x0020
    MOUSEEVENTF_MIDDLEUP = 0x0040
    MOUSEEVENTF_WHEEL = 0x0800
    MOUSEEVENTF_HWHEEL = 0x01000
    MOUSEEVENTF_VIRTUALDESK = 0x4000
    MOUSEEVENTF_ABSOLUTE = 0x8000
    KEYEVENTF_EXTENDEDKEY = 0x0001
    KEYEVENTF_KEYUP = 0x0002
    KEYEVENTF_UNICODE = 0x0004

    def __init__(self, virtual_bounds_fn: Callable[[], tuple[int, int, int, int]]) -> None:
        ensure_dpi_awareness()
        self._bounds_fn = virtual_bounds_fn
        self._user32 = ctypes.windll.user32
        self._user32.SendInput.argtypes = [ctypes.c_uint, ctypes.POINTER(INPUT), ctypes.c_int]
        self._user32.SendInput.restype = ctypes.c_uint
        self._user32.MapVirtualKeyW.argtypes = [ctypes.c_uint, ctypes.c_uint]
        self._user32.MapVirtualKeyW.restype = ctypes.c_uint
        self._last_pos: tuple[int, int] | None = None

    @property
    def backend(self) -> str:
        return "win32-sendinput"

    @property
    def layout(self) -> str:
        return "unicode"

    def close(self) -> None:
        pass

    def _send(self, items: list[INPUT]) -> None:
        if not items:
            return
        arr = (INPUT * len(items))(*items)
        self._user32.SendInput(len(items), arr, ctypes.sizeof(INPUT))

    def move(self, x: int, y: int) -> tuple[int, int]:
        cx, cy, ax, ay = win_virtual_to_abs(x, y, self._bounds_fn())
        inp = INPUT(type=self.INPUT_MOUSE)
        inp.mi = MOUSEINPUT(ax, ay, 0, self.MOUSEEVENTF_MOVE | self.MOUSEEVENTF_ABSOLUTE | self.MOUSEEVENTF_VIRTUALDESK, 0, 0)
        self._send([inp])
        self._last_pos = (cx, cy)
        return cx, cy

    def click(
        self,
        x: int | None = None,
        y: int | None = None,
        *,
        button: str = "left",
        count: int = 1,
        modifiers: list[str] | None = None,
    ) -> tuple[int, int] | None:
        b = button.lower()
        btn_flags = {
            "left": (self.MOUSEEVENTF_LEFTDOWN, self.MOUSEEVENTF_LEFTUP),
            "right": (self.MOUSEEVENTF_RIGHTDOWN, self.MOUSEEVENTF_RIGHTUP),
            "middle": (self.MOUSEEVENTF_MIDDLEDOWN, self.MOUSEEVENTF_MIDDLEUP),
        }
        if b not in btn_flags:
            raise ValueError(f"Unsupported button: {button!r}")
        down_f, up_f = btn_flags[b]
        pos = None
        if x is not None and y is not None:
            pos = self.move(x, y)
            time.sleep(0.006)
        mod_vks = [WIN_VK_CODES[m.lower()] for m in (modifiers or []) if m.lower() in WIN_VK_CODES]
        if mod_vks:
            self._send([self._key_inp(vk, False) for vk in mod_vks])
            time.sleep(0.006)
        try:
            for i in range(max(1, int(count))):
                self._send([INPUT(type=self.INPUT_MOUSE, mi=MOUSEINPUT(0, 0, 0, down_f, 0, 0))])
                time.sleep(0.008)
                self._send([INPUT(type=self.INPUT_MOUSE, mi=MOUSEINPUT(0, 0, 0, up_f, 0, 0))])
                if i + 1 < count:
                    time.sleep(0.040)
        finally:
            if mod_vks:
                time.sleep(0.005)
                self._send([self._key_inp(vk, True) for vk in reversed(mod_vks)])
        return pos

    def drag(
        self,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        *,
        button: str = "left",
        duration_ms: int = 140,
        steps: int = 14,
        on_step: Callable[[int, int], None] | None = None,
    ) -> tuple[tuple[int, int], tuple[int, int]]:
        b = button.lower()
        down_f, up_f = {
            "left": (self.MOUSEEVENTF_LEFTDOWN, self.MOUSEEVENTF_LEFTUP),
            "right": (self.MOUSEEVENTF_RIGHTDOWN, self.MOUSEEVENTF_RIGHTUP),
            "middle": (self.MOUSEEVENTF_MIDDLEDOWN, self.MOUSEEVENTF_MIDDLEUP),
        }.get(b, (self.MOUSEEVENTF_LEFTDOWN, self.MOUSEEVENTF_LEFTUP))
        start = self.move(x1, y1)
        if on_step:
            on_step(start[0], start[1])
        time.sleep(0.012)
        self._send([INPUT(type=self.INPUT_MOUSE, mi=MOUSEINPUT(0, 0, 0, down_f, 0, 0))])
        time.sleep(0.014)
        steps = max(2, min(60, int(steps)))
        dt = max(0.004, (duration_ms / 1000.0) / steps)
        end = start
        try:
            for i in range(1, steps + 1):
                t = 0.5 - 0.5 * math.cos(math.pi * i / steps)
                ix = int(round(x1 + (x2 - x1) * t))
                iy = int(round(y1 + (y2 - y1) * t))
                end = self.move(ix, iy)
                if on_step:
                    on_step(end[0], end[1])
                time.sleep(dt)
        finally:
            time.sleep(0.012)
            self._send([INPUT(type=self.INPUT_MOUSE, mi=MOUSEINPUT(0, 0, 0, up_f, 0, 0))])
        return start, end

    def scroll(self, direction: str = "down", clicks: int = 5, *, x: int | None = None, y: int | None = None) -> None:
        if x is not None and y is not None:
            self.move(x, y)
            time.sleep(0.006)
        d = direction.lower()
        n = max(1, min(100, int(clicks)))
        delta = 120 * n
        if d == "up":
            flag, data = self.MOUSEEVENTF_WHEEL, delta
        elif d == "down":
            flag, data = self.MOUSEEVENTF_WHEEL, (-delta) & 0xFFFFFFFF
        elif d == "right":
            flag, data = self.MOUSEEVENTF_HWHEEL, delta
        elif d == "left":
            flag, data = self.MOUSEEVENTF_HWHEEL, (-delta) & 0xFFFFFFFF
        else:
            raise ValueError(f"Invalid scroll direction: {direction!r}")
        self._send([INPUT(type=self.INPUT_MOUSE, mi=MOUSEINPUT(0, 0, data, flag, 0, 0))])

    def _key_inp(self, vk: int, up: bool) -> INPUT:
        scan = int(self._user32.MapVirtualKeyW(vk, 0)) & 0xFF
        flags = (self.KEYEVENTF_EXTENDEDKEY if vk in EXTENDED_VKS else 0) | (self.KEYEVENTF_KEYUP if up else 0)
        return INPUT(type=self.INPUT_KEYBOARD, ki=KEYBDINPUT(vk, scan, flags, 0, 0))

    def hotkey(self, keys: str | list[str], *, count: int = 1) -> None:
        parsed = parse_win_key_combo(keys)
        vks = [vk for _, vk in parsed]
        for i in range(max(1, int(count))):
            self._send([self._key_inp(vk, False) for vk in vks])
            time.sleep(0.008)
            self._send([self._key_inp(vk, True) for vk in reversed(vks)])
            if i + 1 < count:
                time.sleep(0.025)

    def _unit_to_inputs(self, u: int) -> list[INPUT]:
        if u == 0x000A:  # LF -> VK_RETURN
            return [self._key_inp(0x0D, False), self._key_inp(0x0D, True)]
        if u == 0x0009:  # TAB -> VK_TAB
            return [self._key_inp(0x09, False), self._key_inp(0x09, True)]
        return [
            INPUT(type=self.INPUT_KEYBOARD, ki=KEYBDINPUT(0, u, self.KEYEVENTF_UNICODE, 0, 0)),
            INPUT(type=self.INPUT_KEYBOARD, ki=KEYBDINPUT(0, u, self.KEYEVENTF_UNICODE | self.KEYEVENTF_KEYUP, 0, 0)),
        ]

    def type_keys(self, text: str, *, key_delay_ms: int = 0) -> None:
        """Type arbitrary Unicode text layout-independently via KEYEVENTF_UNICODE (UTF-16-LE surrogate-safe)."""
        if not text:
            return
        units = text_to_utf16_units(text)
        if key_delay_ms <= 0:
            i = 0
            n = len(units)
            while i < n:
                end = min(n, i + 64)
                # Never split a UTF-16 high surrogate (0xD800..0xDBFF) from its low surrogate across batches
                if end < n and 0xD800 <= units[end - 1] <= 0xDBFF:
                    end += 1
                batch: list[INPUT] = []
                for u in units[i:end]:
                    batch.extend(self._unit_to_inputs(u))
                self._send(batch)
                time.sleep(0.002)
                i = end
        else:
            delay = key_delay_ms / 1000.0
            i = 0
            n = len(units)
            while i < n:
                u = units[i]
                if i + 1 < n and 0xD800 <= u <= 0xDBFF:
                    self._send(self._unit_to_inputs(u) + self._unit_to_inputs(units[i + 1]))
                    i += 2
                else:
                    self._send(self._unit_to_inputs(u))
                    i += 1
                time.sleep(delay)

    def is_pure_ascii_typable(self, text: str) -> bool:
        return True


# ---------------------------------------------------------------------------
# WinClipboard (64-bit safe with transient lock retry)
# ---------------------------------------------------------------------------


class WinClipboard:
    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002

    def __init__(self) -> None:
        from ctypes import wintypes

        self._user32 = ctypes.windll.user32
        self._kernel32 = ctypes.windll.kernel32
        self._user32.OpenClipboard.argtypes = [wintypes.HWND]
        self._user32.OpenClipboard.restype = wintypes.BOOL
        self._user32.CloseClipboard.argtypes = []
        self._user32.CloseClipboard.restype = wintypes.BOOL
        self._user32.EmptyClipboard.argtypes = []
        self._user32.EmptyClipboard.restype = wintypes.BOOL
        self._user32.GetClipboardData.argtypes = [wintypes.UINT]
        self._user32.GetClipboardData.restype = wintypes.HANDLE
        self._user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
        self._user32.SetClipboardData.restype = wintypes.HANDLE
        self._kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
        self._kernel32.GlobalAlloc.restype = wintypes.HANDLE
        self._kernel32.GlobalLock.argtypes = [wintypes.HANDLE]
        self._kernel32.GlobalLock.restype = ctypes.c_void_p
        self._kernel32.GlobalUnlock.argtypes = [wintypes.HANDLE]
        self._kernel32.GlobalUnlock.restype = wintypes.BOOL
        self._kernel32.GlobalFree.argtypes = [wintypes.HANDLE]
        self._kernel32.GlobalFree.restype = wintypes.HANDLE

    @property
    def backend(self) -> str:
        return "win32-clipboard"

    def _open(self) -> None:
        for _ in range(6):
            if self._user32.OpenClipboard(None):
                return
            time.sleep(0.01)
        raise RuntimeError("Win32 OpenClipboard failed (locked by another process)")

    def get_text(self) -> str:
        self._open()
        try:
            hdata = self._user32.GetClipboardData(self.CF_UNICODETEXT)
            if not hdata:
                return ""
            ptr = self._kernel32.GlobalLock(hdata)
            if not ptr:
                return ""
            try:
                return ctypes.wstring_at(ptr)
            finally:
                self._kernel32.GlobalUnlock(hdata)
        finally:
            self._user32.CloseClipboard()

    def set_text(self, text: str) -> None:
        raw = (text + "\x00").encode("utf-16-le")
        self._open()
        try:
            self._user32.EmptyClipboard()
            hmem = self._kernel32.GlobalAlloc(self.GMEM_MOVEABLE, len(raw))
            if not hmem:
                raise RuntimeError("GlobalAlloc failed")
            ptr = self._kernel32.GlobalLock(hmem)
            if not ptr:
                self._kernel32.GlobalFree(hmem)
                raise RuntimeError("GlobalLock failed")
            try:
                ctypes.memmove(ptr, raw, len(raw))
            finally:
                self._kernel32.GlobalUnlock(hmem)
            if not self._user32.SetClipboardData(self.CF_UNICODETEXT, hmem):
                self._kernel32.GlobalFree(hmem)
                raise RuntimeError("SetClipboardData failed")
        finally:
            self._user32.CloseClipboard()


# ---------------------------------------------------------------------------
# WinScreenCapture (GDI BitBlt / PrintWindow + GDI+ / TurboJPEG zero-dep encoder)
# ---------------------------------------------------------------------------


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32), ("biHeight", ctypes.c_int32),
        ("biPlanes", ctypes.c_uint16), ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
        ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_int32),
        ("biYPelsPerMeter", ctypes.c_int32), ("biClrUsed", ctypes.c_uint32), ("biClrImportant", ctypes.c_uint32),
    ]


class _GdiplusStartupInput(ctypes.Structure):
    _fields_ = [
        ("GdiplusVersion", ctypes.c_uint32),
        ("DebugEventCallback", ctypes.c_void_p),
        ("SuppressBackgroundThread", ctypes.c_int32),
        ("SuppressExternalCodecs", ctypes.c_int32),
    ]


class _EncoderParameter(ctypes.Structure):
    _fields_ = [
        ("Guid", _GUID),
        ("NumberOfValues", ctypes.c_uint32),
        ("Type", ctypes.c_uint32),
        ("Value", ctypes.c_void_p),
    ]


class _EncoderParameters(ctypes.Structure):
    _fields_ = [("Count", ctypes.c_uint32), ("Parameter", _EncoderParameter * 1)]


class WinScreenCapture:
    """Zero-dependency Windows screen & background-window capture with GDI+/TurboJPEG encoding."""

    _CLSID_JPEG = _GUID.from_str("{557CF401-1A04-11D3-9A73-0000F81EF32E}")
    _CLSID_PNG = _GUID.from_str("{557CF406-1A04-11D3-9A73-0000F81EF32E}")
    _GUID_QUALITY = _GUID.from_str("{1D5BE4B5-FA4A-452D-9CDD-5DB35105E7EB}")

    def __init__(self, virtual_bounds_fn: Callable[[], tuple[int, int, int, int]]) -> None:
        ensure_dpi_awareness()
        self._bounds_fn = virtual_bounds_fn
        self._user32 = ctypes.windll.user32
        self._gdi32 = ctypes.windll.gdi32
        self._ole32 = ctypes.windll.ole32
        self._kernel32 = ctypes.windll.kernel32
        self._gdiplus = ctypes.windll.gdiplus
        self._gdip_token = _ULONG_PTR(0)
        self._last_thumb: np.ndarray | None = None
        self._last_frame_rect: tuple[int, int, int, int] | None = None
        self._tj = TurboJpegEncoder()
        self._setup_sigs()
        self._start_gdiplus()

    def _setup_sigs(self) -> None:
        from ctypes import wintypes

        u, g, gp, o, k = self._user32, self._gdi32, self._gdiplus, self._ole32, self._kernel32
        u.GetDC.argtypes = [wintypes.HWND]
        u.GetDC.restype = wintypes.HDC
        u.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
        u.ReleaseDC.restype = ctypes.c_int
        u.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
        u.PrintWindow.restype = wintypes.BOOL
        u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(_RECT)]
        u.GetWindowRect.restype = wintypes.BOOL
        u.IsIconic.argtypes = [wintypes.HWND]
        u.IsIconic.restype = wintypes.BOOL

        g.CreateCompatibleDC.argtypes = [wintypes.HDC]
        g.CreateCompatibleDC.restype = wintypes.HDC
        g.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
        g.CreateCompatibleBitmap.restype = wintypes.HBITMAP
        g.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
        g.SelectObject.restype = wintypes.HGDIOBJ
        g.BitBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD]
        g.BitBlt.restype = wintypes.BOOL
        g.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT, ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]
        g.GetDIBits.restype = ctypes.c_int
        g.DeleteObject.argtypes = [wintypes.HGDIOBJ]
        g.DeleteObject.restype = wintypes.BOOL
        g.DeleteDC.argtypes = [wintypes.HDC]
        g.DeleteDC.restype = wintypes.BOOL

        o.CreateStreamOnHGlobal.argtypes = [wintypes.HANDLE, wintypes.BOOL, ctypes.POINTER(ctypes.c_void_p)]
        o.CreateStreamOnHGlobal.restype = ctypes.c_long
        o.GetHGlobalFromStream.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.HANDLE)]
        o.GetHGlobalFromStream.restype = ctypes.c_long

        k.GlobalSize.argtypes = [wintypes.HANDLE]
        k.GlobalSize.restype = ctypes.c_size_t
        k.GlobalLock.argtypes = [wintypes.HANDLE]
        k.GlobalLock.restype = ctypes.c_void_p
        k.GlobalUnlock.argtypes = [wintypes.HANDLE]
        k.GlobalUnlock.restype = wintypes.BOOL

        gp.GdiplusStartup.argtypes = [ctypes.POINTER(_ULONG_PTR), ctypes.POINTER(_GdiplusStartupInput), ctypes.c_void_p]
        gp.GdiplusStartup.restype = ctypes.c_int
        gp.GdiplusShutdown.argtypes = [_ULONG_PTR]
        gp.GdiplusShutdown.restype = None
        gp.GdipCreateBitmapFromScan0.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
        gp.GdipCreateBitmapFromScan0.restype = ctypes.c_int
        gp.GdipDisposeImage.argtypes = [ctypes.c_void_p]
        gp.GdipDisposeImage.restype = ctypes.c_int
        gp.GdipGetImageGraphicsContext.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
        gp.GdipGetImageGraphicsContext.restype = ctypes.c_int
        gp.GdipDeleteGraphics.argtypes = [ctypes.c_void_p]
        gp.GdipDeleteGraphics.restype = ctypes.c_int
        gp.GdipSetInterpolationMode.argtypes = [ctypes.c_void_p, ctypes.c_int]
        gp.GdipSetInterpolationMode.restype = ctypes.c_int
        gp.GdipSetCompositingMode.argtypes = [ctypes.c_void_p, ctypes.c_int]
        gp.GdipSetCompositingMode.restype = ctypes.c_int
        gp.GdipDrawImageRectI.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int]
        gp.GdipDrawImageRectI.restype = ctypes.c_int
        gp.GdipCreatePen1.argtypes = [ctypes.c_uint32, ctypes.c_float, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)]
        gp.GdipCreatePen1.restype = ctypes.c_int
        gp.GdipDeletePen.argtypes = [ctypes.c_void_p]
        gp.GdipDeletePen.restype = ctypes.c_int
        gp.GdipDrawLineI.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int]
        gp.GdipDrawLineI.restype = ctypes.c_int
        gp.GdipDrawRectangleI.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int]
        gp.GdipDrawRectangleI.restype = ctypes.c_int
        gp.GdipDrawEllipseI.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int]
        gp.GdipDrawEllipseI.restype = ctypes.c_int
        gp.GdipSaveImageToStream.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(_GUID), ctypes.c_void_p]
        gp.GdipSaveImageToStream.restype = ctypes.c_int

    def _start_gdiplus(self) -> None:
        si = _GdiplusStartupInput(1, None, 0, 0)
        rc = self._gdiplus.GdiplusStartup(ctypes.byref(self._gdip_token), ctypes.byref(si), None)
        if rc != 0:
            raise RuntimeError(f"GdiplusStartup failed with status {rc}")
        atexit.register(self._shutdown_gdiplus)

    def _shutdown_gdiplus(self) -> None:
        if self._gdip_token.value:
            try:
                self._gdiplus.GdiplusShutdown(self._gdip_token)
            except Exception:
                pass
            self._gdip_token = _ULONG_PTR(0)

    @property
    def backend(self) -> str:
        enc = "turbojpeg" if self._tj.available else "gdiplus"
        return f"win32-gdi+{enc}"

    def _read_dib_rgba(self, hdc_mem, hbmp, w: int, h: int) -> np.ndarray:
        bmi = _BITMAPINFOHEADER()
        bmi.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        bmi.biWidth = w
        bmi.biHeight = -h  # top-down
        bmi.biPlanes = 1
        bmi.biBitCount = 32
        bmi.biCompression = 0  # BI_RGB
        buf = np.empty((h, w, 4), dtype=np.uint8)
        lines = self._gdi32.GetDIBits(hdc_mem, hbmp, 0, h, buf.ctypes.data, ctypes.byref(bmi), 0)
        if lines <= 0:
            raise RuntimeError("Win32 GetDIBits returned 0 lines")
        rgba = np.empty_like(buf)
        rgba[:, :, 0] = buf[:, :, 2]
        rgba[:, :, 1] = buf[:, :, 1]
        rgba[:, :, 2] = buf[:, :, 0]
        rgba[:, :, 3] = 255
        return rgba

    def capture_window_bg(self, hwnd: int) -> Frame:
        """Capture an occluded/background window via PrintWindow(PW_RENDERFULLCONTENT) without focusing it."""
        if self._user32.IsIconic(hwnd):
            raise RuntimeError("Target window is minimized; Windows DWM does not render minimized windows. Unminimize it (it can remain behind other windows).")
        t0 = time.perf_counter()
        rc = _RECT()
        if not self._user32.GetWindowRect(hwnd, ctypes.byref(rc)):
            raise RuntimeError(f"GetWindowRect failed for hwnd={hwnd}")
        w = max(1, rc.right - rc.left)
        h = max(1, rc.bottom - rc.top)
        hdc_scr = self._user32.GetDC(None)
        if not hdc_scr:
            raise RuntimeError("GetDC(None) failed")
        hdc_mem = self._gdi32.CreateCompatibleDC(hdc_scr)
        hbmp = self._gdi32.CreateCompatibleBitmap(hdc_scr, w, h)
        old = self._gdi32.SelectObject(hdc_mem, hbmp)
        try:
            PW_RENDERFULLCONTENT = 2
            if not self._user32.PrintWindow(hwnd, hdc_mem, PW_RENDERFULLCONTENT):
                self._user32.PrintWindow(hwnd, hdc_mem, 0)
            rgba = self._read_dib_rgba(hdc_mem, hbmp, w, h)
        finally:
            self._gdi32.SelectObject(hdc_mem, old)
            self._gdi32.DeleteObject(hbmp)
            self._gdi32.DeleteDC(hdc_mem)
            self._user32.ReleaseDC(None, hdc_scr)
        return Frame(rgba=rgba, origin_x=rc.left, origin_y=rc.top, width=w, height=h, scale=1.0,
                     backend="win32-printwindow", capture_ms=(time.perf_counter() - t0) * 1000.0)

    def capture(
        self,
        *,
        scope: str = "screen",
        area: tuple[int, int, int, int] | None = None,
        window_id: str | None = None,
        active_rect: tuple[int, int, int, int] | None = None,
        include_cursor: bool = True,
    ) -> Frame:
        if scope == "window" and window_id and str(window_id).isdigit():
            return self.capture_window_bg(int(window_id))

        t0 = time.perf_counter()
        vx, vy, vw, vh = self._bounds_fn()
        if scope == "area" and area is not None:
            ox, oy, w, h = int(area[0]), int(area[1]), max(1, int(area[2])), max(1, int(area[3]))
        elif scope == "active" and active_rect is not None:
            ox, oy, w, h = int(active_rect[0]), int(active_rect[1]), max(1, int(active_rect[2])), max(1, int(active_rect[3]))
        else:
            ox, oy, w, h = vx, vy, vw, vh

        hdc_scr = self._user32.GetDC(None)
        if not hdc_scr:
            raise RuntimeError("GetDC(None) failed")
        hdc_mem = self._gdi32.CreateCompatibleDC(hdc_scr)
        hbmp = self._gdi32.CreateCompatibleBitmap(hdc_scr, w, h)
        old = self._gdi32.SelectObject(hdc_mem, hbmp)
        try:
            SRCCOPY_CAPTUREBLT = 0x00CC0020 | 0x40000000
            ok = bool(self._gdi32.BitBlt(hdc_mem, 0, 0, w, h, hdc_scr, ox, oy, SRCCOPY_CAPTUREBLT))
            if not ok:
                ok = bool(self._gdi32.BitBlt(hdc_mem, 0, 0, w, h, hdc_scr, ox, oy, 0x00CC0020))
            if not ok:
                raise RuntimeError("Win32 BitBlt failed (desktop locked or Session 0)")
            rgba = self._read_dib_rgba(hdc_mem, hbmp, w, h)
        finally:
            self._gdi32.SelectObject(hdc_mem, old)
            self._gdi32.DeleteObject(hbmp)
            self._gdi32.DeleteDC(hdc_mem)
            self._user32.ReleaseDC(None, hdc_scr)

        return Frame(rgba=rgba, origin_x=ox, origin_y=oy, width=w, height=h, scale=1.0,
                     backend="win32-bitblt", capture_ms=(time.perf_counter() - t0) * 1000.0)

    def _thumbnail(self, frame: Frame) -> np.ndarray:
        step = max(1, min(frame.width, frame.height) // 160)
        return frame.rgba[::step, ::step, :3].astype(np.int16, copy=False)

    def diff_frames(self, before: Frame, after: Frame, *, ignore_rects: list[tuple[int, int, int, int]] | None = None) -> dict[str, Any]:
        if before.width != after.width or before.height != after.height:
            return {"changed_pct": 100.0, "changed_bbox": [after.origin_x, after.origin_y, after.width, after.height]}
        step = max(1, min(after.width, after.height) // 180)
        a = before.rgba[::step, ::step, :3].astype(np.int16)
        b = after.rgba[::step, ::step, :3].astype(np.int16)
        diff = np.abs(a - b).max(axis=2) > 8
        _apply_ignore_rects(diff, after, step, ignore_rects)
        changed = int(diff.sum())
        if changed == 0:
            return {"changed_pct": 0.0, "changed_bbox": None}
        ys, xs = np.nonzero(diff)
        x0 = after.origin_x + int(xs.min()) * step
        y0 = after.origin_y + int(ys.min()) * step
        x1 = after.origin_x + min(after.width, int(xs.max() + 1) * step)
        y1 = after.origin_y + min(after.height, int(ys.max() + 1) * step)
        return {"changed_pct": round(100.0 * changed / max(1, diff.size), 2), "changed_bbox": [x0, y0, max(1, x1 - x0), max(1, y1 - y0)]}

    def diff_from_last(self, frame: Frame, *, ignore_rects: list[tuple[int, int, int, int]] | None = None) -> dict[str, Any] | None:
        rect = (frame.origin_x, frame.origin_y, frame.width, frame.height)
        thumb = self._thumbnail(frame)
        res = None
        if self._last_thumb is not None and self._last_frame_rect == rect and self._last_thumb.shape == thumb.shape:
            step = max(1, min(frame.width, frame.height) // 160)
            diff = np.abs(self._last_thumb - thumb).max(axis=2) > 8
            _apply_ignore_rects(diff, frame, step, ignore_rects)
            changed = int(diff.sum())
            pct = round(100.0 * changed / max(1, diff.size), 2)
            bbox = None
            if changed > 0:
                ys, xs = np.nonzero(diff)
                x0 = frame.origin_x + int(xs.min()) * step
                y0 = frame.origin_y + int(ys.min()) * step
                x1 = frame.origin_x + min(frame.width, int(xs.max() + 1) * step)
                y1 = frame.origin_y + min(frame.height, int(ys.max() + 1) * step)
                bbox = [x0, y0, max(1, x1 - x0), max(1, y1 - y0)]
            res = {"changed_pct": pct, "changed_bbox": bbox}
        self._last_thumb = thumb
        self._last_frame_rect = rect
        return res

    def wait_until_settled(self, *, scope="screen", area=None, active_rect=None, max_wait_ms=160, poll_ms=25, include_cursor=False, ignore_rects_fn=None) -> Frame:
        deadline = time.monotonic() + (max_wait_ms / 1000.0)
        prev = self.capture(scope=scope, area=area, active_rect=active_rect, include_cursor=include_cursor)
        prev_ig = ignore_rects_fn() if ignore_rects_fn else []
        while time.monotonic() < deadline:
            time.sleep(poll_ms / 1000.0)
            cur = self.capture(scope=scope, area=area, active_rect=active_rect, include_cursor=include_cursor)
            cur_ig = ignore_rects_fn() if ignore_rects_fn else []
            d = self.diff_frames(prev, cur, ignore_rects=prev_ig + cur_ig)
            prev, prev_ig = cur, cur_ig
            if d["changed_pct"] < 0.08:
                break
        return prev

    def wait_for_change(self, *, area=None, timeout_ms=5000, threshold_pct=0.15, ignore_rects_fn=None) -> dict[str, Any]:
        scope = "area" if area else "screen"
        t0 = time.monotonic()
        deadline = t0 + (timeout_ms / 1000.0)
        base_ig = ignore_rects_fn() if ignore_rects_fn else []
        base = self.capture(scope=scope, area=area, include_cursor=False)
        while time.monotonic() < deadline:
            time.sleep(0.025)
            cur_ig = ignore_rects_fn() if ignore_rects_fn else []
            cur = self.capture(scope=scope, area=area, include_cursor=False)
            d = self.diff_frames(base, cur, ignore_rects=base_ig + cur_ig)
            if d["changed_pct"] >= threshold_pct:
                return {"changed": True, "elapsed_ms": round((time.monotonic() - t0) * 1000.0, 1), **d}
        return {"changed": False, "elapsed_ms": round((time.monotonic() - t0) * 1000.0, 1), "changed_pct": 0.0}

    def encode(
        self,
        frame: Frame,
        *,
        max_width: int = 1280,
        fmt: str = "jpeg",
        quality: int = 75,
        annotations: list[dict[str, Any]] | None = None,
        grid: bool = False,
        cursor: tuple[int, int] | None = None,
    ) -> dict[str, Any]:
        """Scale, draw GDI+ grid/Set-of-Marks/cursor + 5x7 digit badges, and encode exact-length JPEG/PNG."""
        from ctypes import wintypes

        t0 = time.perf_counter()
        src_w, src_h = frame.width, frame.height
        mw = int(max_width) if max_width is not None else 0
        if mw > 0 and src_w > mw:
            out_w = mw
            out_h = max(1, int(round(src_h * (out_w / src_w))))
        else:
            out_w, out_h = src_w, src_h
        scale_x = out_w / max(1, src_w)
        scale_y = out_h / max(1, src_h)
        out_fmt = "png" if fmt.lower() == "png" else "jpeg"

        src_bgra = np.empty((src_h, src_w, 4), dtype=np.uint8)
        src_bgra[:, :, 0] = frame.rgba[:, :, 2]
        src_bgra[:, :, 1] = frame.rgba[:, :, 1]
        src_bgra[:, :, 2] = frame.rgba[:, :, 0]
        src_bgra[:, :, 3] = 255

        PIXEL_FMT_32ARGB = 0x0026200A
        gp = self._gdiplus
        src_img = ctypes.c_void_p()
        dst_img = ctypes.c_void_p()
        gfx = ctypes.c_void_p()
        stream = ctypes.c_void_p()
        dst_buf = np.zeros((out_h, out_w, 4), dtype=np.uint8)
        try:
            rc = gp.GdipCreateBitmapFromScan0(src_w, src_h, src_w * 4, PIXEL_FMT_32ARGB, src_bgra.ctypes.data, ctypes.byref(src_img))
            if rc != 0:
                raise RuntimeError(f"GdipCreateBitmapFromScan0(src) failed: {rc}")
            rc = gp.GdipCreateBitmapFromScan0(out_w, out_h, out_w * 4, PIXEL_FMT_32ARGB, dst_buf.ctypes.data, ctypes.byref(dst_img))
            if rc != 0:
                raise RuntimeError(f"GdipCreateBitmapFromScan0(dst) failed: {rc}")
            rc = gp.GdipGetImageGraphicsContext(dst_img, ctypes.byref(gfx))
            if rc != 0:
                raise RuntimeError(f"GdipGetImageGraphicsContext failed: {rc}")
            gp.GdipSetCompositingMode(gfx, 1)  # CompositingModeSourceCopy for opaque base blit
            gp.GdipSetInterpolationMode(gfx, 3)  # Bilinear
            rc = gp.GdipDrawImageRectI(gfx, src_img, 0, 0, out_w, out_h)
            if rc != 0:
                raise RuntimeError(f"GdipDrawImageRectI failed: {rc}")
            gp.GdipSetCompositingMode(gfx, 0)  # CompositingModeSourceOver for overlays

            if grid:
                pen = ctypes.c_void_p()
                gp.GdipCreatePen1(0x5500D9FF, 1.0, 2, ctypes.byref(pen))
                try:
                    for n in range(100, 1000, 100):
                        gx = int(round(out_w * n / 1000.0))
                        gy = int(round(out_h * n / 1000.0))
                        gp.GdipDrawLineI(gfx, pen, gx, 0, gx, out_h)
                        gp.GdipDrawLineI(gfx, pen, 0, gy, out_w, gy)
                finally:
                    if pen.value:
                        gp.GdipDeletePen(pen)

            badge_jobs: list[tuple[int, int, str, tuple[int, int, int], tuple[int, int, int]]] = []
            if annotations:
                palette_argb = (0xE0F24236, 0xE02196F3, 0xE02EC25C, 0xE0FA9E0A, 0xE0AD4CEB)
                palette_bgr = ((54, 66, 242), (243, 150, 33), (92, 194, 46), (10, 158, 250), (235, 76, 173))
                for el in annotations:
                    ref = int(el.get("ref", 0))
                    bx, by, bw, bh = el.get("bbox", (0, 0, 0, 0))
                    rx = int((bx - frame.origin_x) * scale_x)
                    ry = int((by - frame.origin_y) * scale_y)
                    rw = int(bw * scale_x)
                    rh = int(bh * scale_y)
                    if rw < 4 or rh < 4 or rx + rw < 0 or ry + rh < 0 or rx > out_w or ry > out_h:
                        continue
                    pen = ctypes.c_void_p()
                    gp.GdipCreatePen1(palette_argb[ref % len(palette_argb)], 1.5, 2, ctypes.byref(pen))
                    try:
                        gp.GdipDrawRectangleI(gfx, pen, rx, ry, rw, rh)
                    finally:
                        if pen.value:
                            gp.GdipDeletePen(pen)
                    py_b = ry - 11 if ry >= 12 else ry + 1
                    badge_jobs.append((rx, py_b, str(ref), palette_bgr[ref % len(palette_bgr)], (255, 255, 255)))

            if cursor is not None:
                cx = int((cursor[0] - frame.origin_x) * scale_x)
                cy = int((cursor[1] - frame.origin_y) * scale_y)
                if 0 <= cx <= out_w and 0 <= cy <= out_h:
                    pen = ctypes.c_void_p()
                    gp.GdipCreatePen1(0xF024C4FA, 2.0, 2, ctypes.byref(pen))
                    try:
                        gp.GdipDrawEllipseI(gfx, pen, cx - 8, cy - 8, 16, 16)
                    finally:
                        if pen.value:
                            gp.GdipDeletePen(pen)

            # Flush & delete GDI+ Graphics context BEFORE direct buffer blits / TurboJPEG / SaveImageToStream!
            gp.GdipDeleteGraphics(gfx)
            gfx = ctypes.c_void_p()

            if grid:
                for n in range(100, 1000, 100):
                    gx = int(round(out_w * n / 1000.0))
                    gy = int(round(out_h * n / 1000.0))
                    blit_pill_digits_bgra(dst_buf, gx - 11, 2, str(n), bg_bgr=(24, 16, 12), fg_bgr=(255, 240, 100))
                    blit_pill_digits_bgra(dst_buf, 2, gy - 5, str(n), bg_bgr=(24, 16, 12), fg_bgr=(255, 240, 100))
            for bx, by, lbl, bg_c, fg_c in badge_jobs:
                blit_pill_digits_bgra(dst_buf, bx, by, lbl, bg_bgr=bg_c, fg_bgr=fg_c)

            if out_fmt == "jpeg" and self._tj.available:
                encoded = self._tj.compress(dst_buf, pixel_format=TurboJpegEncoder.TJPF_BGRX, quality=quality)
                mime = "image/jpeg"
            else:
                hr = self._ole32.CreateStreamOnHGlobal(None, True, ctypes.byref(stream))
                if hr != 0 or not stream.value:
                    raise RuntimeError(f"CreateStreamOnHGlobal failed: {hr:#x}")
                if out_fmt == "jpeg":
                    q_val = ctypes.c_ulong(max(20, min(95, int(quality))))
                    ep = _EncoderParameters()
                    ep.Count = 1
                    ep.Parameter[0].Guid = self._GUID_QUALITY
                    ep.Parameter[0].NumberOfValues = 1
                    ep.Parameter[0].Type = 4
                    ep.Parameter[0].Value = ctypes.addressof(q_val)
                    rc = gp.GdipSaveImageToStream(dst_img, stream, ctypes.byref(self._CLSID_JPEG), ctypes.byref(ep))
                    mime = "image/jpeg"
                else:
                    rc = gp.GdipSaveImageToStream(dst_img, stream, ctypes.byref(self._CLSID_PNG), None)
                    mime = "image/png"
                if rc != 0:
                    raise RuntimeError(f"GdipSaveImageToStream failed: {rc}")

                # Exact written byte count via IStream::Seek(0, STREAM_SEEK_END=2, &end_pos) (slot 5)
                end_pos = ctypes.c_uint64(0)
                hr_s = _com_call(stream, 5, ctypes.c_long, [ctypes.c_int64, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint64)], 0, 2, ctypes.byref(end_pos))
                hglobal = wintypes.HANDLE()
                hr_h = self._ole32.GetHGlobalFromStream(stream, ctypes.byref(hglobal))
                if hr_h != 0 or not hglobal.value:
                    raise RuntimeError(f"GetHGlobalFromStream failed: {hr_h:#x}")
                alloc_sz = int(self._kernel32.GlobalSize(hglobal))
                exact_sz = min(alloc_sz, int(end_pos.value)) if (hr_s == 0 and end_pos.value > 0) else alloc_sz
                ptr = self._kernel32.GlobalLock(hglobal)
                if not ptr:
                    raise RuntimeError("GlobalLock failed on image stream")
                try:
                    encoded = ctypes.string_at(ptr, exact_sz)
                finally:
                    self._kernel32.GlobalUnlock(hglobal)
        finally:
            if stream.value:
                _com_release(stream)
            if gfx.value:
                gp.GdipDeleteGraphics(gfx)
            if dst_img.value:
                gp.GdipDisposeImage(dst_img)
            if src_img.value:
                gp.GdipDisposeImage(src_img)

        encode_ms = (time.perf_counter() - t0) * 1000.0
        return {
            "data_b64": base64.b64encode(encoded).decode("ascii"),
            "mime_type": mime,
            "bytes": len(encoded),
            "image_w": out_w,
            "image_h": out_h,
            "screen_origin": [frame.origin_x, frame.origin_y],
            "screen_size": [src_w, src_h],
            "scale_to_screen": round(src_w / max(1, out_w), 4),
            "capture_ms": round(frame.capture_ms, 1),
            "encode_ms": round(encode_ms, 1),
            "total_ms": round(frame.capture_ms + encode_ms, 1),
        }


# ---------------------------------------------------------------------------
# WinA11yTree (Native zero-dep MTA IUIAutomation COM vtable walker)
# ---------------------------------------------------------------------------

_UIA_INTERACTIVE_TYPES: dict[int, str] = {
    50000: "btn",
    50002: "checkbox",
    50003: "combobox",
    50004: "text",
    50005: "link",
    50007: "item",
    50011: "menuitem",
    50013: "radio",
    50015: "slider",
    50016: "spin",
    50019: "tab",
    50024: "treeitem",
    50029: "cell",
    50031: "btn",
}

_UIA_TEXT_TYPES: set[int] = {50020}


def _com_call(ptr: ctypes.c_void_p, slot: int, restype, argtypes, *args):
    vtbl = ctypes.cast(ctypes.cast(ptr, ctypes.POINTER(ctypes.c_void_p))[0], ctypes.POINTER(ctypes.c_void_p))
    fn = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(vtbl[slot])
    return fn(ptr, *args)


def _com_release(ptr: ctypes.c_void_p | None) -> None:
    if ptr and getattr(ptr, "value", None):
        try:
            _com_call(ptr, 2, ctypes.c_ulong, [])
        except Exception:
            pass


class WinA11yTree:
    """Native Windows UI Automation COM walker (MTA) with leak-free refcounting, stable [ref] IDs, and Invoke/Toggle."""

    _CLSID_CUIAutomation = _GUID.from_str("{FF48DBA4-60EF-4201-AA87-54103EEF594E}")
    _IID_IUIAutomation = _GUID.from_str("{30CBE57D-D9D0-452A-AB13-7AC5AC4825EE}")
    _IID_InvokePattern = _GUID.from_str("{FB377FBE-8EA6-46D5-9C73-6499642D3059}")
    _IID_TogglePattern = _GUID.from_str("{94CF8058-9B8D-4AB9-8BFD-4CD0A33C8C70}")
    _IID_ValuePattern = _GUID.from_str("{A94CD8B1-0844-4CD6-9D2D-640537AB39E9}")

    def __init__(self) -> None:
        from ctypes import wintypes

        self._user32 = ctypes.windll.user32
        self._user32.GetForegroundWindow.argtypes = []
        self._user32.GetForegroundWindow.restype = wintypes.HWND
        self._ole32 = ctypes.windll.ole32
        self._oleaut32 = ctypes.windll.oleaut32
        self._oleaut32.SysFreeString.argtypes = [ctypes.c_void_p]
        self._oleaut32.SysFreeString.restype = None
        self._com_inited = False
        self._uia = ctypes.c_void_p()
        self._walker = ctypes.c_void_p()
        self._next_ref = 1
        self._key_to_ref: dict[tuple[Any, ...], int] = {}
        self._ref_map: dict[int, dict[str, Any]] = {}
        self._ref_ptrs: dict[int, ctypes.c_void_p] = {}
        self._last_window_key = ""
        self._last_lines: dict[int, str] = {}
        self._available = self._init_com()

    def _init_com(self) -> bool:
        try:
            # MTA avoids cross-apartment deadlocks when running on a stdio thread without a Win32 message pump
            COINIT_MULTITHREADED = 0x0
            self._ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            self._ole32.CoInitializeEx.restype = ctypes.c_long
            self._ole32.CoUninitialize.argtypes = []
            self._ole32.CoUninitialize.restype = None
            hr_ci = self._ole32.CoInitializeEx(None, COINIT_MULTITHREADED)
            # S_OK (0) or S_FALSE (1) means we must balance with CoUninitialize; RPC_E_CHANGED_MODE means already initialized
            if hr_ci in (0, 1):
                self._com_inited = True

            self._ole32.CoCreateInstance.argtypes = [
                ctypes.POINTER(_GUID), ctypes.c_void_p, ctypes.c_uint32,
                ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p),
            ]
            self._ole32.CoCreateInstance.restype = ctypes.c_long
            hr = self._ole32.CoCreateInstance(
                ctypes.byref(self._CLSID_CUIAutomation), None, 0x1,
                ctypes.byref(self._IID_IUIAutomation), ctypes.byref(self._uia),
            )
            if hr != 0 or not self._uia.value:
                return False
            hr = _com_call(self._uia, 14, ctypes.c_long, [ctypes.POINTER(ctypes.c_void_p)], ctypes.byref(self._walker))
            if hr != 0 or not self._walker.value:
                _com_release(self._uia)
                self._uia = ctypes.c_void_p()
                return False
            return True
        except Exception:
            return False

    def _clear_ref_ptrs(self) -> None:
        for ptr in self._ref_ptrs.values():
            _com_release(ptr)
        self._ref_ptrs.clear()

    def close(self) -> None:
        self._available = False
        self._clear_ref_ptrs()
        _com_release(self._walker)
        self._walker = ctypes.c_void_p()
        _com_release(self._uia)
        self._uia = ctypes.c_void_p()
        if self._com_inited:
            try:
                self._ole32.CoUninitialize()
            except Exception:
                pass
            self._com_inited = False

    @property
    def available(self) -> bool:
        return self._available

    def get_ref(self, ref_id: int) -> dict[str, Any] | None:
        return self._ref_map.get(int(ref_id))

    def find_by_text(self, query: str) -> dict[str, Any] | None:
        q = query.strip().lower()
        if not q:
            return None
        for el in self._ref_map.values():
            if el.get("name", "").lower() == q:
                return el
        for el in self._ref_map.values():
            if q in f"{el.get('name', '')} {el.get('value', '')}".lower():
                return el
        return None

    def do_action(self, ref_id: int, action_name: str | None = None) -> dict[str, Any]:
        """Invoke UIA InvokePattern or TogglePattern directly without moving the mouse."""
        ptr = self._ref_ptrs.get(int(ref_id))
        meta = self._ref_map.get(int(ref_id))
        if ptr is None or not ptr.value or meta is None:
            raise ValueError(f"Unknown or stale ref [{ref_id}]")
        patterns = [(10000, self._IID_InvokePattern, "invoke"), (10015, self._IID_TogglePattern, "toggle")]
        if action_name and "toggle" in action_name.lower():
            patterns.reverse()
        for pat_id, iid, pname in patterns:
            pat = ctypes.c_void_p()
            hr = _com_call(
                ptr, 14, ctypes.c_long,
                [ctypes.c_int, ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p)],
                pat_id, ctypes.byref(iid), ctypes.byref(pat),
            )
            if hr == 0 and pat.value:
                try:
                    hr2 = _com_call(pat, 3, ctypes.c_long, [])
                    if hr2 == 0:
                        return {"ok": True, "ref": ref_id, "action": pname, "name": meta.get("name", "")}
                finally:
                    _com_release(pat)
        hr_f = _com_call(ptr, 3, ctypes.c_long, [])
        if hr_f == 0:
            return {"ok": True, "ref": ref_id, "action": "focus", "name": meta.get("name", "")}
        raise RuntimeError(f"Element [{ref_id}] ({meta['role']}) does not support UIA Invoke/Toggle")

    def _read_bstr(self, ptr: ctypes.c_void_p, slot: int) -> str:
        bstr = ctypes.c_void_p()
        hr = _com_call(ptr, slot, ctypes.c_long, [ctypes.POINTER(ctypes.c_void_p)], ctypes.byref(bstr))
        if hr != 0 or not bstr.value:
            return ""
        try:
            return ctypes.wstring_at(bstr.value).replace("\n", " ").strip()
        finally:
            self._oleaut32.SysFreeString(bstr)

    def _read_value(self, ptr: ctypes.c_void_p) -> str:
        pat = ctypes.c_void_p()
        hr = _com_call(
            ptr, 14, ctypes.c_long,
            [ctypes.c_int, ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p)],
            10002, ctypes.byref(self._IID_ValuePattern), ctypes.byref(pat),
        )
        if hr != 0 or not pat.value:
            return ""
        try:
            return self._read_bstr(pat, 4)
        finally:
            _com_release(pat)

    def snapshot(
        self,
        *,
        app_hint: str | None = None,
        title_hint: str | None = None,
        pid_hint: int | None = None,
        window_id: str = "",
        window_rect: tuple[int, int, int, int] | None = None,
        find: str | None = None,
        max_elements: int = 120,
        budget_ms: int = 240,
        diff: str | bool = "auto",
    ) -> dict[str, Any]:
        t0 = time.perf_counter()
        win_key = window_id or f"{app_hint or ''}:{title_hint or ''}"
        self._clear_ref_ptrs()
        if win_key != self._last_window_key:
            self._key_to_ref.clear()
            self._ref_map.clear()
            self._last_lines.clear()
            self._next_ref = 1

        if not self._available or not self._uia.value or not self._walker.value:
            self._last_window_key = win_key
            return {
                "available": False, "window_key": win_key, "elements": [], "text_lines": [],
                "formatted": "(UI Automation unavailable)", "is_diff": False, "elapsed_ms": 0.0,
            }
        deadline = t0 + (budget_ms / 1000.0)
        root = ctypes.c_void_p()
        hwnd = int(window_id) if window_id and str(window_id).isdigit() else int(self._user32.GetForegroundWindow() or 0)
        if hwnd:
            _com_call(self._uia, 6, ctypes.c_long, [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)], ctypes.c_void_p(hwnd), ctypes.byref(root))
        if not root.value:
            _com_call(self._uia, 8, ctypes.c_long, [ctypes.POINTER(ctypes.c_void_p)], ctypes.byref(root))
        if not root.value:
            self._ref_map.clear()
            self._last_lines.clear()
            self._last_window_key = win_key
            return {
                "available": True, "window_key": win_key, "elements": [], "text_lines": [],
                "formatted": "(no UIA root for active window; using visual screenshot + grid)",
                "is_diff": False, "elapsed_ms": round((time.perf_counter() - t0) * 1000.0, 1),
            }

        elements: list[dict[str, Any]] = []
        new_ref_map: dict[int, dict[str, Any]] = {}
        key_counts: dict[tuple[Any, ...], int] = {}
        text_snippets: list[str] = []
        visited = 0
        max_nodes = 1200 if find else 1000
        stack: list[tuple[ctypes.c_void_p, int]] = [(root, 0)]

        try:
            while stack and visited < max_nodes and time.perf_counter() < deadline:
                node, depth = stack.pop()
                keep_ref = False
                visited += 1
                try:
                    if depth > 16:
                        continue
                    if depth > 0:
                        offscreen = ctypes.c_int(0)
                        _com_call(node, 38, ctypes.c_long, [ctypes.POINTER(ctypes.c_int)], ctypes.byref(offscreen))
                        if offscreen.value:
                            continue

                    ctype = ctypes.c_int(0)
                    _com_call(node, 21, ctypes.c_long, [ctypes.POINTER(ctypes.c_int)], ctypes.byref(ctype))
                    cid = ctype.value

                    if cid in _UIA_INTERACTIVE_TYPES:
                        rc = _RECT()
                        hr_rc = _com_call(node, 43, ctypes.c_long, [ctypes.POINTER(_RECT)], ctypes.byref(rc))
                        bw, bh = rc.right - rc.left, rc.bottom - rc.top
                        if hr_rc == 0 and bw > 1 and bh > 1:
                            role = _UIA_INTERACTIVE_TYPES[cid]
                            name = self._read_bstr(node, 23)
                            val = self._read_value(node) if cid in (50004, 50003) else ""
                            states: list[str] = []
                            focused = ctypes.c_int(0)
                            _com_call(node, 26, ctypes.c_long, [ctypes.POINTER(ctypes.c_int)], ctypes.byref(focused))
                            if focused.value:
                                states.append("focused")
                            enabled = ctypes.c_int(1)
                            _com_call(node, 28, ctypes.c_long, [ctypes.POINTER(ctypes.c_int)], ctypes.byref(enabled))
                            if not enabled.value:
                                states.append("disabled")

                            bbox = (rc.left, rc.top, bw, bh)
                            base_key = (win_key, role, name[:48], bbox[0] // 8, bbox[1] // 8, bbox[2] // 8, bbox[3] // 8)
                            occ = key_counts.get(base_key, 0)
                            key_counts[base_key] = occ + 1
                            skey = (*base_key, occ)
                            ref = self._key_to_ref.get(skey)
                            if ref is None:
                                ref = self._next_ref
                                self._next_ref += 1
                                self._key_to_ref[skey] = ref
                            item = {
                                "ref": ref,
                                "role": role,
                                "raw_role": role,
                                "name": name[:100],
                                "value": val[:120],
                                "states": states,
                                "bbox": bbox,
                                "center": [bbox[0] + bw // 2, bbox[1] + bh // 2],
                            }
                            elements.append(item)
                            new_ref_map[ref] = item
                            old_ptr = self._ref_ptrs.get(ref)
                            if old_ptr is not None:
                                _com_release(old_ptr)
                            self._ref_ptrs[ref] = node
                            keep_ref = True
                            if not find and len(elements) >= max_elements * 2:
                                break
                    elif cid in _UIA_TEXT_TYPES and len(text_snippets) < 40:
                        txt = self._read_bstr(node, 23)
                        if txt and len(txt) <= 180 and txt not in text_snippets:
                            text_snippets.append(txt)

                    # Enumerate children cleanly with zero 61st-child leak
                    children: list[ctypes.c_void_p] = []
                    try:
                        child = ctypes.c_void_p()
                        hr_c = _com_call(self._walker, 4, ctypes.c_long, [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)], node, ctypes.byref(child))
                        while hr_c == 0 and child.value:
                            children.append(child)
                            if len(children) >= 60:
                                break
                            nxt = ctypes.c_void_p()
                            hr_c = _com_call(self._walker, 6, ctypes.c_long, [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)], child, ctypes.byref(nxt))
                            child = nxt
                    except Exception:
                        for c_leak in children:
                            _com_release(c_leak)
                        children = []
                    for ch in reversed(children):
                        stack.append((ch, depth + 1))
                finally:
                    if not keep_ref:
                        _com_release(node)
        finally:
            for rem, _ in stack:
                _com_release(rem)

        self._ref_map = new_ref_map
        if find:
            q = find.strip().lower()
            elements = [e for e in elements if q in e["name"].lower() or q in e["value"].lower() or q in e["role"].lower()]
            text_snippets = [t for t in text_snippets if q in t.lower()]

        elements = elements[:max_elements]
        cur_lines = {e["ref"]: self._format_elem(e) for e in elements}

        use_diff = (diff is True) or (diff == "auto" and win_key == self._last_window_key and bool(self._last_lines) and not find)
        is_diff = False
        if use_diff:
            added = [line for r, line in cur_lines.items() if r not in self._last_lines]
            changed = [line for r, line in cur_lines.items() if r in self._last_lines and self._last_lines[r] != line]
            removed = [r for r in self._last_lines if r not in cur_lines]
            parts: list[str] = []
            if added:
                parts.append("+ ADDED:\n" + "\n".join(added[:50]))
            if changed:
                parts.append("~ CHANGED:\n" + "\n".join(changed[:50]))
            if removed:
                parts.append(f"- REMOVED REFS: {removed[:40]}")
            formatted = "\n".join(parts) if parts else "(no UIA tree changes; prior [ref] IDs still valid)"
            is_diff = True
        else:
            lines_out = list(cur_lines.values())
            if text_snippets:
                lines_out.append("--- TEXT ---")
                lines_out.extend(text_snippets[:25])
            formatted = "\n".join(lines_out) if lines_out else "(empty UIA tree)"

        self._last_window_key = win_key
        if not find:
            self._last_lines = cur_lines

        return {
            "available": True,
            "window_key": win_key,
            "elements": elements,
            "text_lines": text_snippets,
            "formatted": formatted,
            "is_diff": is_diff,
            "visited": visited,
            "elapsed_ms": round((time.perf_counter() - t0) * 1000.0, 1),
        }

    @staticmethod
    def _format_elem(e: dict[str, Any]) -> str:
        parts = [f"[{e['ref']}] {e['role']}"]
        if e["name"]:
            parts.append(f'"{e["name"]}"')
        if e["value"] and e["value"] != e["name"]:
            parts.append(f'val="{e["value"]}"')
        if e["states"]:
            parts.append("[" + ",".join(e["states"]) + "]")
        cx, cy = e["center"]
        parts.append(f"@({cx},{cy})")
        return " ".join(parts)


# ---------------------------------------------------------------------------
# WinBackgroundSession (Child-HWND aware Zero-Focus PrintWindow + PostMessageW)
# ---------------------------------------------------------------------------


class WinBackgroundSession:
    """Interact with a Windows app in the background via PrintWindow + child-aware PostMessageW."""

    WM_MOUSEMOVE = 0x0200
    WM_LBUTTONDOWN = 0x0201
    WM_LBUTTONUP = 0x0202
    WM_LBUTTONDBLCLK = 0x0203
    WM_RBUTTONDOWN = 0x0204
    WM_RBUTTONUP = 0x0205
    WM_MBUTTONDOWN = 0x0207
    WM_MBUTTONUP = 0x0208
    WM_MOUSEWHEEL = 0x020A
    WM_MOUSEHWHEEL = 0x020E
    WM_KEYDOWN = 0x0100
    WM_KEYUP = 0x0101
    WM_CHAR = 0x0102

    def __init__(self, win_mgr: WinWindowManager, capture: WinScreenCapture) -> None:
        from ctypes import wintypes

        self._wm = win_mgr
        self._cap = capture
        self._user32 = ctypes.windll.user32
        self._user32.IsWindow.argtypes = [wintypes.HWND]
        self._user32.IsWindow.restype = wintypes.BOOL
        self._user32.IsIconic.argtypes = [wintypes.HWND]
        self._user32.IsIconic.restype = wintypes.BOOL
        self._user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(_RECT)]
        self._user32.GetWindowRect.restype = wintypes.BOOL
        self._user32.ScreenToClient.argtypes = [wintypes.HWND, ctypes.POINTER(_POINT)]
        self._user32.ScreenToClient.restype = wintypes.BOOL
        self._user32.ChildWindowFromPointEx.argtypes = [wintypes.HWND, _POINT, wintypes.UINT]
        self._user32.ChildWindowFromPointEx.restype = wintypes.HWND
        self._user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        self._user32.PostMessageW.restype = wintypes.BOOL
        self._user32.MapVirtualKeyW.argtypes = [ctypes.c_uint, ctypes.c_uint]
        self._user32.MapVirtualKeyW.restype = ctypes.c_uint

        self._target_hwnd: int | None = None
        self._last_child_hwnd: int | None = None
        self._target_title: str = ""
        self._target_app: str = ""

    @property
    def active(self) -> bool:
        return self._target_hwnd is not None and bool(self._user32.IsWindow(self._target_hwnd))

    def window_rect(self) -> tuple[int, int, int, int]:
        hwnd = self._require_hwnd()
        rc = _RECT()
        self._user32.GetWindowRect(hwnd, ctypes.byref(rc))
        return rc.left, rc.top, max(1, rc.right - rc.left), max(1, rc.bottom - rc.top)

    def status(self) -> dict[str, Any]:
        return {
            "active": self.active,
            "backend": "win32-printwindow+postmessage",
            "hwnd": str(self._target_hwnd) if self._target_hwnd else None,
            "app": self._target_app,
            "title": self._target_title,
        }

    def attach(self, *, window_id: str | None = None, app: str | None = None, title: str | None = None) -> dict[str, Any]:
        w = self._wm.find_window(window_id=window_id, app=app, title=title)
        if w is None:
            raise ValueError(f"Background target window not found (window_id={window_id!r}, app={app!r}, title={title!r})")
        hwnd = int(w["id"])
        if self._user32.IsIconic(hwnd):
            raise RuntimeError("Target window is minimized; unminimize it (it can remain behind other windows) before attaching background mode.")
        self._target_hwnd = hwnd
        self._last_child_hwnd = hwnd
        self._target_app = w["app"]
        self._target_title = w["title"]
        return self.status()

    def stop(self) -> dict[str, Any]:
        self._target_hwnd = None
        self._last_child_hwnd = None
        self._target_app = ""
        self._target_title = ""
        return {"active": False}

    def _require_hwnd(self) -> int:
        if not self.active or not self._target_hwnd:
            raise RuntimeError("No Windows background window attached. Call desktop_background(action='attach', app=...) first.")
        if self._user32.IsIconic(self._target_hwnd):
            raise RuntimeError("Attached background window is minimized; unminimize it first.")
        return self._target_hwnd

    def _resolve_child_and_lparam(self, screen_x: int, screen_y: int) -> tuple[int, int]:
        """Descend child HWNDs (Edit, RichEdit, Chrome_RenderWidgetHostHWND, dialog controls) to find exact message target."""
        cur = self._require_hwnd()
        CWP_SKIPINVISIBLE_DISABLED_TRANSPARENT = 0x0001 | 0x0002 | 0x0004
        for _ in range(12):
            pt = _POINT(int(screen_x), int(screen_y))
            self._user32.ScreenToClient(cur, ctypes.byref(pt))
            child = int(self._user32.ChildWindowFromPointEx(cur, pt, CWP_SKIPINVISIBLE_DISABLED_TRANSPARENT) or 0)
            if not child or child == cur:
                break
            cur = child
        pt_final = _POINT(int(screen_x), int(screen_y))
        self._user32.ScreenToClient(cur, ctypes.byref(pt_final))
        self._last_child_hwnd = cur
        lparam = ((pt_final.y & 0xFFFF) << 16) | (pt_final.x & 0xFFFF)
        return cur, lparam

    def _key_lparam(self, vk: int, up: bool) -> int:
        scan = int(self._user32.MapVirtualKeyW(vk, 0)) & 0xFF
        ext = (1 << 24) if vk in EXTENDED_VKS else 0
        lp = 1 | (scan << 16) | ext
        if up:
            lp |= 0xC0000000
        return lp

    def capture(self, area: tuple[int, int, int, int] | None = None) -> Frame:
        hwnd = self._require_hwnd()
        frame = self._cap.capture_window_bg(hwnd)
        if area is not None:
            x0 = max(0, min(frame.width - 1, int(area[0]) - frame.origin_x))
            y0 = max(0, min(frame.height - 1, int(area[1]) - frame.origin_y))
            x1 = max(x0 + 1, min(frame.width, x0 + int(area[2])))
            y1 = max(y0 + 1, min(frame.height, y0 + int(area[3])))
            sub = np.ascontiguousarray(frame.rgba[y0:y1, x0:x1, :])
            return Frame(rgba=sub, origin_x=frame.origin_x + x0, origin_y=frame.origin_y + y0,
                         width=x1 - x0, height=y1 - y0, scale=1.0, backend=frame.backend, capture_ms=frame.capture_ms)
        return frame

    def move(self, x: int, y: int) -> tuple[int, int]:
        tgt, lp = self._resolve_child_and_lparam(x, y)
        self._user32.PostMessageW(tgt, self.WM_MOUSEMOVE, 0, lp)
        return int(x), int(y)

    def click(self, x: int, y: int, *, button: str = "left", count: int = 1) -> tuple[int, int]:
        tgt, lp = self._resolve_child_and_lparam(x, y)
        b = button.lower()
        msg_map = {
            "left": (self.WM_LBUTTONDOWN, self.WM_LBUTTONUP, 0x0001),
            "right": (self.WM_RBUTTONDOWN, self.WM_RBUTTONUP, 0x0002),
            "middle": (self.WM_MBUTTONDOWN, self.WM_MBUTTONUP, 0x0010),
        }
        down_m, up_m, mk = msg_map.get(b, msg_map["left"])
        self._user32.PostMessageW(tgt, self.WM_MOUSEMOVE, 0, lp)
        for i in range(max(1, int(count))):
            use_down = self.WM_LBUTTONDBLCLK if (b == "left" and i == 1) else down_m
            self._user32.PostMessageW(tgt, use_down, mk, lp)
            time.sleep(0.01)
            self._user32.PostMessageW(tgt, up_m, 0, lp)
            if i + 1 < count:
                time.sleep(0.03)
        return int(x), int(y)

    def drag(self, x1: int, y1: int, x2: int, y2: int, *, button: str = "left", duration_ms: int = 120, steps: int = 10) -> tuple[tuple[int, int], tuple[int, int]]:
        tgt, lp1 = self._resolve_child_and_lparam(x1, y1)
        self._user32.PostMessageW(tgt, self.WM_LBUTTONDOWN, 0x0001, lp1)
        steps = max(2, min(40, int(steps)))
        dt = max(0.004, (duration_ms / 1000.0) / steps)
        for i in range(1, steps + 1):
            t = i / steps
            ix = int(round(x1 + (x2 - x1) * t))
            iy = int(round(y1 + (y2 - y1) * t))
            pt = _POINT(ix, iy)
            self._user32.ScreenToClient(tgt, ctypes.byref(pt))
            lp = ((pt.y & 0xFFFF) << 16) | (pt.x & 0xFFFF)
            self._user32.PostMessageW(tgt, self.WM_MOUSEMOVE, 0x0001, lp)
            time.sleep(dt)
        pt2 = _POINT(int(x2), int(y2))
        self._user32.ScreenToClient(tgt, ctypes.byref(pt2))
        lp2 = ((pt2.y & 0xFFFF) << 16) | (pt2.x & 0xFFFF)
        self._user32.PostMessageW(tgt, self.WM_LBUTTONUP, 0, lp2)
        return (int(x1), int(y1)), (int(x2), int(y2))

    def scroll(self, direction: str = "down", clicks: int = 5, *, x: int | None = None, y: int | None = None) -> None:
        wx, wy, ww, wh = self.window_rect()
        sx = wx + ww // 2 if x is None else int(x)
        sy = wy + wh // 2 if y is None else int(y)
        tgt, _ = self._resolve_child_and_lparam(sx, sy)
        d = direction.lower()
        n = max(1, min(100, int(clicks)))
        if d in ("up", "down"):
            msg = self.WM_MOUSEWHEEL
            delta = (120 * n) if d == "up" else (-120 * n)
        elif d in ("left", "right"):
            msg = self.WM_MOUSEHWHEEL
            delta = (120 * n) if d == "right" else (-120 * n)
        else:
            raise ValueError(f"Invalid scroll direction: {direction!r}")
        wp = (delta & 0xFFFF) << 16
        # WM_MOUSEWHEEL / WM_MOUSEHWHEEL lParam uses SCREEN coordinates!
        lp = ((sy & 0xFFFF) << 16) | (sx & 0xFFFF)
        self._user32.PostMessageW(tgt, msg, wp, lp)

    def _kbd_target(self) -> int:
        root = self._require_hwnd()
        if self._last_child_hwnd and self._user32.IsWindow(self._last_child_hwnd):
            return self._last_child_hwnd
        return root

    def type_text(self, text: str) -> None:
        tgt = self._kbd_target()
        for u in text_to_utf16_units(text):
            if u == 0x000A:
                self._user32.PostMessageW(tgt, self.WM_KEYDOWN, 0x0D, self._key_lparam(0x0D, False))
                self._user32.PostMessageW(tgt, self.WM_KEYUP, 0x0D, self._key_lparam(0x0D, True))
            elif u == 0x0009:
                self._user32.PostMessageW(tgt, self.WM_KEYDOWN, 0x09, self._key_lparam(0x09, False))
                self._user32.PostMessageW(tgt, self.WM_KEYUP, 0x09, self._key_lparam(0x09, True))
            else:
                self._user32.PostMessageW(tgt, self.WM_CHAR, u, 1)

    def hotkey(self, keys: str | list[str], *, count: int = 1) -> None:
        tgt = self._kbd_target()
        vks = [vk for _, vk in parse_win_key_combo(keys)]
        for _ in range(max(1, int(count))):
            for vk in vks:
                self._user32.PostMessageW(tgt, self.WM_KEYDOWN, vk, self._key_lparam(vk, False))
            time.sleep(0.008)
            for vk in reversed(vks):
                self._user32.PostMessageW(tgt, self.WM_KEYUP, vk, self._key_lparam(vk, True))
