"""Native zero-dependency macOS (Apple Silicon arm64 & Intel x86_64) Computer Use backend.

Pure ctypes bindings to Apple system frameworks (CoreFoundation, CoreGraphics, ImageIO,
ApplicationServices AXUIElement, Objective-C runtime / Foundation / AppKit) with zero pip
dependencies beyond numpy (already shared across all platforms) and automatic dyld-shared-cache
loading (macOS 11 through macOS 26+).

Subsystems:
- MacWindowManager: CGWindowListCopyWindowInfo (~1-3 ms front-to-back Z-ordered windows),
  CGGetActiveDisplayList multi-monitor virtual bounds (negative offsets supported), CGEvent cursor,
  AXUIElement + NSRunningApplication focus/minimize/maximize/move_resize/close, and `open` launch.
- MacInputDriver: CoreGraphics CGEvent mouse (with kCGMouseEventClickState for double/triple click,
  LeftMouseDragged/RightMouseDragged for drag, non-variadic CGEventCreateScrollWheelEvent2 for arm64
  ABI safety), complete macOS virtual keycode table, and layout-independent UTF-16
  CGEventKeyboardSetUnicodeString typing (surrogate-pair safe, works on any keyboard layout).
- MacClipboard: UTF-8 forced pbpaste/pbcopy (zero AppKit main-thread restriction).
- MacScreenCapture: CoreGraphics CGWindowListCreateImage (excluding overlay window, plus occluded
  single-window background capture) with seamless `/usr/sbin/screencapture` (ScreenCaptureKit)
  fallback for macOS 15+ Sequoia / macOS 26+, CGBitmapContext hardware bilinear resize, optional
  Homebrew libturbojpeg or built-in Apple ImageIO JPEG/PNG encoding, Retina HiDPI scale tracking,
  and crisp 5x7 bitmap labels for Set-of-Marks [ref] badges & 0..1000 coordinate grids.
- MacA11yTree: Native ApplicationServices AXUIElement tree walker with messaging timeout (0.35s),
  strict CFRelease memory discipline, stable [ref] IDs, incremental AX_DIFF, and mouse-free
  AXPress / AXConfirm actions.
- MacBackgroundSession: Zero-touch background automation targeting a specific window (`attach` or
  `open -g` background `launch`): captures occluded windows via CGWindowListCreateImage /
  `screencapture -l`, triggers native controls via AXUIElementCopyElementAtPosition + AXPress, and
  posts mouse/scroll/Unicode keyboard events directly via CGEventPostToPid without moving the
  user's physical cursor or stealing focus.
"""

from __future__ import annotations

import atexit
import base64
import ctypes
import math
import os
import platform
import shlex
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable

import numpy as np

from capture import Frame, TurboJpegEncoder, _apply_ignore_rects
from win32_backend import blit_pill_digits_bgra

# ---------------------------------------------------------------------------
# Pure-Python macOS keycodes, modifier masks, & helpers (testable on any OS)
# ---------------------------------------------------------------------------

# macOS ANSI virtual keycodes (Carbon HIToolbox Events.h)
MAC_KEYCODES: dict[str, int] = {
    "a": 0x00, "s": 0x01, "d": 0x02, "f": 0x03, "h": 0x04, "g": 0x05, "z": 0x06,
    "x": 0x07, "c": 0x08, "v": 0x09, "b": 0x0B, "q": 0x0C, "w": 0x0D, "e": 0x0E,
    "r": 0x0F, "y": 0x10, "t": 0x11,
    "1": 0x12, "2": 0x13, "3": 0x14, "4": 0x15, "6": 0x16, "5": 0x17,
    "=": 0x18, "equal": 0x18, "+": 0x18,
    "9": 0x19, "7": 0x1A, "-": 0x1B, "minus": 0x1B, "8": 0x1C, "0": 0x1D,
    "]": 0x1E, "rightbrace": 0x1E, "o": 0x1F, "u": 0x20, "[": 0x21, "leftbrace": 0x21,
    "i": 0x22, "p": 0x23,
    "return": 0x24, "enter": 0x24,
    "l": 0x25, "j": 0x26, "'": 0x27, "apostrophe": 0x27, "k": 0x28,
    ";": 0x29, "semicolon": 0x29, "\\": 0x2A, "backslash": 0x2A,
    ",": 0x2B, "comma": 0x2B, "/": 0x2C, "slash": 0x2C, "n": 0x2D, "m": 0x2E,
    ".": 0x2F, "dot": 0x2F, "period": 0x2F,
    "tab": 0x30,
    "space": 0x31, " ": 0x31,
    "`": 0x32, "grave": 0x32,
    "backspace": 0x33, "bs": 0x33,
    "esc": 0x35, "escape": 0x35,
    "rmeta": 0x36, "rightmeta": 0x36, "rcmd": 0x36, "rcommand": 0x36, "rightcommand": 0x36,
    "cmd": 0x37, "command": 0x37, "super": 0x37, "meta": 0x37, "win": 0x37,
    "lmeta": 0x37, "leftmeta": 0x37, "lcmd": 0x37, "lcommand": 0x37, "leftcommand": 0x37,
    "shift": 0x38, "lshift": 0x38, "leftshift": 0x38,
    "capslock": 0x39,
    "alt": 0x3A, "option": 0x3A, "opt": 0x3A, "lalt": 0x3A, "leftalt": 0x3A, "loption": 0x3A, "leftoption": 0x3A,
    "ctrl": 0x3B, "control": 0x3B, "lctrl": 0x3B, "leftctrl": 0x3B,
    "rshift": 0x3C, "rightshift": 0x3C,
    "ralt": 0x3D, "rightalt": 0x3D, "roption": 0x3D, "rightoption": 0x3D, "altgr": 0x3D,
    "rctrl": 0x3E, "rightctrl": 0x3E,
    "fn": 0x3F,
    "f17": 0x40, "volumeup": 0x48, "volumedown": 0x49, "mute": 0x4A,
    "f18": 0x4F, "f19": 0x50, "f20": 0x5A,
    "f5": 0x60, "f6": 0x61, "f7": 0x62, "f3": 0x63, "f8": 0x64, "f9": 0x65,
    "f11": 0x67, "f13": 0x69, "f16": 0x6A, "f14": 0x6B, "f10": 0x6D, "f12": 0x6F,
    "f15": 0x71, "help": 0x72, "insert": 0x72,
    "home": 0x73, "pageup": 0x74, "pgup": 0x74,
    "delete": 0x75, "del": 0x75, "forwarddelete": 0x75,
    "f4": 0x76, "end": 0x77, "f2": 0x78,
    "pagedown": 0x79, "pgdn": 0x79, "f1": 0x7A,
    "left": 0x7B, "right": 0x7C, "down": 0x7D, "up": 0x7E,
}

# CGEventFlags modifier bitmasks
MAC_FLAG_SHIFT = 0x00020000
MAC_FLAG_CTRL = 0x00040000
MAC_FLAG_ALT = 0x00080000
MAC_FLAG_CMD = 0x00100000
MAC_FLAG_FN = 0x00800000

MAC_MODIFIER_FLAGS: dict[str, int] = {
    "shift": MAC_FLAG_SHIFT, "lshift": MAC_FLAG_SHIFT, "leftshift": MAC_FLAG_SHIFT,
    "rshift": MAC_FLAG_SHIFT, "rightshift": MAC_FLAG_SHIFT,
    "ctrl": MAC_FLAG_CTRL, "control": MAC_FLAG_CTRL,
    "lctrl": MAC_FLAG_CTRL, "leftctrl": MAC_FLAG_CTRL, "rctrl": MAC_FLAG_CTRL, "rightctrl": MAC_FLAG_CTRL,
    "alt": MAC_FLAG_ALT, "option": MAC_FLAG_ALT, "opt": MAC_FLAG_ALT,
    "lalt": MAC_FLAG_ALT, "leftalt": MAC_FLAG_ALT, "loption": MAC_FLAG_ALT, "leftoption": MAC_FLAG_ALT,
    "ralt": MAC_FLAG_ALT, "rightalt": MAC_FLAG_ALT, "roption": MAC_FLAG_ALT, "rightoption": MAC_FLAG_ALT, "altgr": MAC_FLAG_ALT,
    "cmd": MAC_FLAG_CMD, "command": MAC_FLAG_CMD, "super": MAC_FLAG_CMD, "meta": MAC_FLAG_CMD,
    "win": MAC_FLAG_CMD, "lmeta": MAC_FLAG_CMD, "leftmeta": MAC_FLAG_CMD, "rmeta": MAC_FLAG_CMD,
    "rightmeta": MAC_FLAG_CMD, "lcmd": MAC_FLAG_CMD, "rcmd": MAC_FLAG_CMD,
    "lcommand": MAC_FLAG_CMD, "leftcommand": MAC_FLAG_CMD, "rcommand": MAC_FLAG_CMD, "rightcommand": MAC_FLAG_CMD,
    "fn": MAC_FLAG_FN,
}

# CoreGraphics Event / Mouse / Scroll constants
kCGEventLeftMouseDown = 1
kCGEventLeftMouseUp = 2
kCGEventRightMouseDown = 3
kCGEventRightMouseUp = 4
kCGEventMouseMoved = 5
kCGEventLeftMouseDragged = 6
kCGEventRightMouseDragged = 7
kCGEventKeyDown = 10
kCGEventKeyUp = 11
kCGEventFlagsChanged = 12
kCGEventScrollWheel = 22
kCGEventOtherMouseDown = 25
kCGEventOtherMouseUp = 26
kCGEventOtherMouseDragged = 27

kCGMouseButtonLeft = 0
kCGMouseButtonRight = 1
kCGMouseButtonCenter = 2

kCGMouseEventClickState = 1
kCGKeyboardEventKeycode = 9
kCGMouseEventWindowUnderMousePointer = 91
kCGMouseEventWindowUnderMousePointerThatCanHandleThisEvent = 92
kCGHIDEventTap = 0
kCGSessionEventTap = 1
kCGEventSourceStatePrivate = -1
kCGEventSourceStateHIDSystemState = 1
kCGScrollEventUnitLine = 1

MAC_CODE_TO_FLAG: dict[int, int] = {
    0x36: MAC_FLAG_CMD, 0x37: MAC_FLAG_CMD,
    0x38: MAC_FLAG_SHIFT, 0x3C: MAC_FLAG_SHIFT,
    0x3A: MAC_FLAG_ALT, 0x3D: MAC_FLAG_ALT,
    0x3B: MAC_FLAG_CTRL, 0x3E: MAC_FLAG_CTRL,
    0x3F: MAC_FLAG_FN,
}

kCGWindowListOptionAll = 0
kCGWindowListOptionOnScreenOnly = 1 << 0
kCGWindowListOptionOnScreenAboveWindow = 1 << 1
kCGWindowListOptionOnScreenBelowWindow = 1 << 2
kCGWindowListOptionIncludingWindow = 1 << 3
kCGWindowListExcludeDesktopElements = 1 << 4

kCGWindowImageDefault = 0
kCGWindowImageBoundsIgnoreFraming = 1 << 0
kCGWindowImageBestResolution = 1 << 3
kCGWindowImageNominalResolution = 1 << 4

kCFStringEncodingUTF8 = 0x08000100
kCFNumberSInt32Type = 3
kCFNumberSInt64Type = 4
kCFNumberDoubleType = 13

kCGImageAlphaPremultipliedLast = 1
kCGImageAlphaPremultipliedFirst = 2
kCGImageAlphaNoneSkipLast = 5
kCGBitmapByteOrder32Little = 2 << 12
kCGBitmapByteOrder32Big = 4 << 12
kCGInterpolationLow = 2
kCGInterpolationMedium = 4

kAXErrorSuccess = 0
kAXValueTypeCGPoint = 1
kAXValueTypeCGSize = 2
kAXValueTypeCGRect = 3

MAC_AX_ROLE_MAP: dict[str, str] = {
    "AXButton": "button",
    "AXPopUpButton": "combobox",
    "AXComboBox": "combobox",
    "AXCheckBox": "checkbox",
    "AXRadioButton": "radio",
    "AXTextField": "text",
    "AXTextArea": "text",
    "AXSearchField": "search",
    "AXStaticText": "label",
    "AXLink": "link",
    "AXMenuItem": "menu_item",
    "AXMenuBarItem": "menu_item",
    "AXMenuButton": "button",
    "AXTab": "tab",
    "AXTabGroup": "tab_list",
    "AXSlider": "slider",
    "AXIncrementor": "spinbutton",
    "AXDisclosureTriangle": "toggle",
    "AXRow": "row",
    "AXOutlineRow": "tree_item",
    "AXCell": "cell",
    "AXImage": "image",
    "AXToolbar": "toolbar",
    "AXWebArea": "document",
    "AXWindow": "window",
    "AXSheet": "dialog",
    "AXDialog": "dialog",
    "AXList": "list",
    "AXTable": "table",
    "AXScrollArea": "scroll_area",
    "AXGroup": "group",
}

MAC_INTERACTIVE_ROLES: set[str] = {
    "button", "combobox", "checkbox", "radio", "text", "search",
    "link", "menu_item", "tab", "slider", "spinbutton", "toggle",
    "tree_item", "row", "cell",
}


def parse_mac_key_combo(keys: str | list[str]) -> tuple[list[int], int]:
    """Parse 'cmd+shift+s', 'cmd++', or ' ' into (mac_keycodes, cg_modifier_flags)."""
    if isinstance(keys, str):
        if keys == " ":
            parts = [" "]
        else:
            s = keys.strip()
            if s == "+":
                parts = ["+"]
            elif s.endswith("++"):
                head = [p.strip() for p in s[:-2].split("+") if p.strip()]
                parts = head + ["+"]
            elif keys.endswith("+ "):
                head = [p.strip() for p in keys[:-2].split("+") if p.strip()]
                parts = head + [" "]
            else:
                parts = [p.strip() for p in s.split("+") if p.strip()]
    else:
        parts = []
        for k in keys:
            ks = str(k)
            if ks == " ":
                parts.append(" ")
            elif ks.strip():
                parts.append(ks.strip())

    codes: list[int] = []
    flags = 0
    for p in parts:
        pl = p.lower()
        if pl in MAC_MODIFIER_FLAGS:
            flags |= MAC_MODIFIER_FLAGS[pl]
        code = MAC_KEYCODES.get(pl)
        if code is None:
            raise ValueError(f"Unknown macOS key name: {p!r}")
        codes.append(code)
    if not codes:
        raise ValueError("Empty key combo")
    return codes, flags


def split_utf16_chunks(text: str, max_units: int = 1) -> list[list[int]]:
    """Split text into UTF-16-LE code-unit chunks without breaking surrogate pairs."""
    raw = text.encode("utf-16-le")
    units = [int.from_bytes(raw[i:i + 2], "little") for i in range(0, len(raw), 2)]
    chunks: list[list[int]] = []
    i = 0
    n = len(units)
    cap = max(1, int(max_units))
    while i < n:
        end = min(n, i + cap)
        # Never split between a high surrogate (0xD800..0xDBFF) and its low surrogate (0xDC00..0xDFFF)
        if end < n and 0xD800 <= units[end - 1] <= 0xDBFF:
            end -= 1
            if end <= i:
                end = min(n, i + 2)
        chunks.append(units[i:end])
        i = end
    return chunks


def cg_top_left_to_appkit_bottom_left(
    x: float,
    y: float,
    h: float,
    primary_display_height: float,
) -> tuple[float, float]:
    """Convert CoreGraphics top-left rect (x, y, h) into AppKit bottom-left origin (ns_x, ns_y)."""
    return float(x), float(primary_display_height) - float(y) - float(h)


def normalize_mac_role(ax_role: str) -> str:
    if ax_role in MAC_AX_ROLE_MAP:
        return MAC_AX_ROLE_MAP[ax_role]
    r = ax_role
    if r.startswith("AX"):
        r = r[2:]
    return r.lower() or "unknown"


# ---------------------------------------------------------------------------
# CoreGraphics / CoreFoundation / ImageIO / ApplicationServices ctypes structs
# ---------------------------------------------------------------------------

class CGPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


class CGSize(ctypes.Structure):
    _fields_ = [("width", ctypes.c_double), ("height", ctypes.c_double)]


class CGRect(ctypes.Structure):
    _fields_ = [("origin", CGPoint), ("size", CGSize)]


# ---------------------------------------------------------------------------
# Shared macOS framework loader & ABI-safe CoreFoundation / ObjC helpers
# ---------------------------------------------------------------------------

# On arm64 macOS, ObjC BOOL is C _Bool (c_bool); on x86_64 macOS, BOOL is signed char (c_int8)
_OBJC_BOOL = ctypes.c_bool if platform.machine() == "arm64" else ctypes.c_int8


class _MacFrameworks:
    """Singleton lazy loader for macOS system frameworks (dyld-shared-cache safe)."""

    _instance: "_MacFrameworks | None" = None

    def __init__(self) -> None:
        # NEVER check os.path.exists() before CDLL on macOS 11+: frameworks live in the dyld shared cache.
        self.cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        self.cg = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
        self.ax = ctypes.CDLL("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
        self.iio = ctypes.CDLL("/System/Library/Frameworks/ImageIO.framework/ImageIO")
        self.objc = ctypes.CDLL("/usr/lib/libobjc.A.dylib")
        try:
            self.appkit = ctypes.CDLL("/System/Library/Frameworks/AppKit.framework/AppKit")
        except Exception:
            self.appkit = None
        try:
            self.quartzcore = ctypes.CDLL("/System/Library/Frameworks/QuartzCore.framework/QuartzCore")
        except Exception:
            self.quartzcore = None

        self._setup_cf()
        self._setup_cg()
        self._setup_iio()
        self._setup_ax()
        self._setup_objc()

    @classmethod
    def get(cls) -> "_MacFrameworks":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def _setup_cf(self) -> None:
        cf = self.cf
        cf.CFRelease.argtypes = [ctypes.c_void_p]
        cf.CFRelease.restype = None
        cf.CFRetain.argtypes = [ctypes.c_void_p]
        cf.CFRetain.restype = ctypes.c_void_p
        cf.CFGetTypeID.argtypes = [ctypes.c_void_p]
        cf.CFGetTypeID.restype = ctypes.c_ulong
        cf.CFStringGetTypeID.argtypes = []
        cf.CFStringGetTypeID.restype = ctypes.c_ulong
        cf.CFNumberGetTypeID.argtypes = []
        cf.CFNumberGetTypeID.restype = ctypes.c_ulong
        cf.CFBooleanGetTypeID.argtypes = []
        cf.CFBooleanGetTypeID.restype = ctypes.c_ulong
        cf.CFArrayGetTypeID.argtypes = []
        cf.CFArrayGetTypeID.restype = ctypes.c_ulong
        cf.CFDictionaryGetTypeID.argtypes = []
        cf.CFDictionaryGetTypeID.restype = ctypes.c_ulong

        cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
        cf.CFStringCreateWithCString.restype = ctypes.c_void_p
        cf.CFStringGetLength.argtypes = [ctypes.c_void_p]
        cf.CFStringGetLength.restype = ctypes.c_long
        cf.CFStringGetMaximumSizeForEncoding.argtypes = [ctypes.c_long, ctypes.c_uint32]
        cf.CFStringGetMaximumSizeForEncoding.restype = ctypes.c_long
        cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
        cf.CFStringGetCString.restype = ctypes.c_bool

        cf.CFNumberCreate.argtypes = [ctypes.c_void_p, ctypes.c_long, ctypes.c_void_p]
        cf.CFNumberCreate.restype = ctypes.c_void_p
        cf.CFNumberGetValue.argtypes = [ctypes.c_void_p, ctypes.c_long, ctypes.c_void_p]
        cf.CFNumberGetValue.restype = ctypes.c_bool
        cf.CFNumberIsFloatType.argtypes = [ctypes.c_void_p]
        cf.CFNumberIsFloatType.restype = ctypes.c_bool

        cf.CFBooleanGetValue.argtypes = [ctypes.c_void_p]
        cf.CFBooleanGetValue.restype = ctypes.c_bool

        cf.CFArrayGetCount.argtypes = [ctypes.c_void_p]
        cf.CFArrayGetCount.restype = ctypes.c_long
        cf.CFArrayGetValueAtIndex.argtypes = [ctypes.c_void_p, ctypes.c_long]
        cf.CFArrayGetValueAtIndex.restype = ctypes.c_void_p

        cf.CFDictionaryCreate.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.c_long,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        cf.CFDictionaryCreate.restype = ctypes.c_void_p
        cf.CFDictionaryGetValue.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        cf.CFDictionaryGetValue.restype = ctypes.c_void_p

        cf.CFDataCreate.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long]
        cf.CFDataCreate.restype = ctypes.c_void_p
        cf.CFDataCreateMutable.argtypes = [ctypes.c_void_p, ctypes.c_long]
        cf.CFDataCreateMutable.restype = ctypes.c_void_p
        cf.CFDataGetLength.argtypes = [ctypes.c_void_p]
        cf.CFDataGetLength.restype = ctypes.c_long
        cf.CFDataGetBytePtr.argtypes = [ctypes.c_void_p]
        cf.CFDataGetBytePtr.restype = ctypes.POINTER(ctypes.c_uint8)
        cf.CFRunLoopRunInMode.argtypes = [ctypes.c_void_p, ctypes.c_double, ctypes.c_bool]
        cf.CFRunLoopRunInMode.restype = ctypes.c_int32

        self.cf_runloop_default_mode = ctypes.c_void_p.in_dll(cf, "kCFRunLoopDefaultMode").value
        self.cf_bool_true = ctypes.c_void_p.in_dll(cf, "kCFBooleanTrue").value
        self.cf_bool_false = ctypes.c_void_p.in_dll(cf, "kCFBooleanFalse").value
        self.cf_dict_key_cbs = ctypes.addressof(ctypes.c_byte.in_dll(cf, "kCFTypeDictionaryKeyCallBacks"))
        self.cf_dict_val_cbs = ctypes.addressof(ctypes.c_byte.in_dll(cf, "kCFTypeDictionaryValueCallBacks"))

        self.tid_string = int(cf.CFStringGetTypeID())
        self.tid_number = int(cf.CFNumberGetTypeID())
        self.tid_bool = int(cf.CFBooleanGetTypeID())
        self.tid_array = int(cf.CFArrayGetTypeID())
        self.tid_dict = int(cf.CFDictionaryGetTypeID())

    def _setup_cg(self) -> None:
        cg = self.cg
        cg.CGMainDisplayID.argtypes = []
        cg.CGMainDisplayID.restype = ctypes.c_uint32
        cg.CGGetActiveDisplayList.argtypes = [
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_uint32),
        ]
        cg.CGGetActiveDisplayList.restype = ctypes.c_int32
        cg.CGDisplayBounds.argtypes = [ctypes.c_uint32]
        cg.CGDisplayBounds.restype = CGRect

        cg.CGWindowListCopyWindowInfo.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
        cg.CGWindowListCopyWindowInfo.restype = ctypes.c_void_p
        cg.CGRectMakeWithDictionaryRepresentation.argtypes = [ctypes.c_void_p, ctypes.POINTER(CGRect)]
        cg.CGRectMakeWithDictionaryRepresentation.restype = ctypes.c_bool

        self.cg_rect_null = CGRect.from_buffer_copy(CGRect.in_dll(cg, "CGRectNull"))
        self.cg_rect_infinite = CGRect.from_buffer_copy(CGRect.in_dll(cg, "CGRectInfinite"))

        self.has_preflight_screen = hasattr(cg, "CGPreflightScreenCaptureAccess")
        if self.has_preflight_screen:
            cg.CGPreflightScreenCaptureAccess.argtypes = []
            cg.CGPreflightScreenCaptureAccess.restype = ctypes.c_bool

        self.has_window_list_image = hasattr(cg, "CGWindowListCreateImage")
        if self.has_window_list_image:
            cg.CGWindowListCreateImage.argtypes = [CGRect, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32]
            cg.CGWindowListCreateImage.restype = ctypes.c_void_p

        self.has_display_image = hasattr(cg, "CGDisplayCreateImage")
        if self.has_display_image:
            cg.CGDisplayCreateImage.argtypes = [ctypes.c_uint32]
            cg.CGDisplayCreateImage.restype = ctypes.c_void_p

        cg.CGImageGetWidth.argtypes = [ctypes.c_void_p]
        cg.CGImageGetWidth.restype = ctypes.c_size_t
        cg.CGImageGetHeight.argtypes = [ctypes.c_void_p]
        cg.CGImageGetHeight.restype = ctypes.c_size_t
        cg.CGImageRelease.argtypes = [ctypes.c_void_p]
        cg.CGImageRelease.restype = None

        cg.CGColorSpaceCreateDeviceRGB.argtypes = []
        cg.CGColorSpaceCreateDeviceRGB.restype = ctypes.c_void_p
        cg.CGColorSpaceRelease.argtypes = [ctypes.c_void_p]
        cg.CGColorSpaceRelease.restype = None

        cg.CGBitmapContextCreate.argtypes = [
            ctypes.c_void_p, ctypes.c_size_t, ctypes.c_size_t,
            ctypes.c_size_t, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_uint32,
        ]
        cg.CGBitmapContextCreate.restype = ctypes.c_void_p
        cg.CGBitmapContextCreateImage.argtypes = [ctypes.c_void_p]
        cg.CGBitmapContextCreateImage.restype = ctypes.c_void_p
        cg.CGContextSetInterpolationQuality.argtypes = [ctypes.c_void_p, ctypes.c_int32]
        cg.CGContextSetInterpolationQuality.restype = None
        cg.CGContextDrawImage.argtypes = [ctypes.c_void_p, CGRect, ctypes.c_void_p]
        cg.CGContextDrawImage.restype = None
        cg.CGContextRelease.argtypes = [ctypes.c_void_p]
        cg.CGContextRelease.restype = None

        cg.CGDataProviderCreateWithData.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p,
        ]
        cg.CGDataProviderCreateWithData.restype = ctypes.c_void_p
        cg.CGDataProviderRelease.argtypes = [ctypes.c_void_p]
        cg.CGDataProviderRelease.restype = None
        cg.CGImageCreate.argtypes = [
            ctypes.c_size_t, ctypes.c_size_t, ctypes.c_size_t, ctypes.c_size_t,
            ctypes.c_size_t, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p,
            ctypes.c_void_p, ctypes.c_bool, ctypes.c_int32,
        ]
        cg.CGImageCreate.restype = ctypes.c_void_p

        # Input events
        cg.CGEventSourceCreate.argtypes = [ctypes.c_int32]
        cg.CGEventSourceCreate.restype = ctypes.c_void_p
        cg.CGEventSourceSetLocalEventsSuppressionInterval.argtypes = [ctypes.c_void_p, ctypes.c_double]
        cg.CGEventSourceSetLocalEventsSuppressionInterval.restype = None
        cg.CGWarpMouseCursorPosition.argtypes = [CGPoint]
        cg.CGWarpMouseCursorPosition.restype = ctypes.c_int32
        cg.CGAssociateMouseAndMouseCursorPosition.argtypes = [ctypes.c_bool]
        cg.CGAssociateMouseAndMouseCursorPosition.restype = ctypes.c_int32
        cg.CGEventCreate.argtypes = [ctypes.c_void_p]
        cg.CGEventCreate.restype = ctypes.c_void_p
        cg.CGEventSetType.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        cg.CGEventSetType.restype = None
        cg.CGEventGetLocation.argtypes = [ctypes.c_void_p]
        cg.CGEventGetLocation.restype = CGPoint
        cg.CGEventSetLocation.argtypes = [ctypes.c_void_p, CGPoint]
        cg.CGEventSetLocation.restype = None
        cg.CGEventCreateMouseEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint32, CGPoint, ctypes.c_uint32]
        cg.CGEventCreateMouseEvent.restype = ctypes.c_void_p
        cg.CGEventCreateKeyboardEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint16, ctypes.c_bool]
        cg.CGEventCreateKeyboardEvent.restype = ctypes.c_void_p
        cg.CGEventKeyboardSetUnicodeString.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_uint16),
        ]
        cg.CGEventKeyboardSetUnicodeString.restype = None
        cg.CGEventSetFlags.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
        cg.CGEventSetFlags.restype = None
        cg.CGEventSetIntegerValueField.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int64]
        cg.CGEventSetIntegerValueField.restype = None
        cg.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
        cg.CGEventPost.restype = None
        self.has_post_to_pid = hasattr(cg, "CGEventPostToPid")
        if self.has_post_to_pid:
            cg.CGEventPostToPid.argtypes = [ctypes.c_int32, ctypes.c_void_p]
            cg.CGEventPostToPid.restype = None

        # Non-variadic scroll wheel API (arm64 ABI safe; available since macOS 10.13)
        self.has_scroll2 = hasattr(cg, "CGEventCreateScrollWheelEvent2")
        if self.has_scroll2:
            cg.CGEventCreateScrollWheelEvent2.argtypes = [
                ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                ctypes.c_int32, ctypes.c_int32, ctypes.c_int32,
            ]
            cg.CGEventCreateScrollWheelEvent2.restype = ctypes.c_void_p

    def _setup_iio(self) -> None:
        iio = self.iio
        iio.CGImageDestinationCreateWithData.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p,
        ]
        iio.CGImageDestinationCreateWithData.restype = ctypes.c_void_p
        iio.CGImageDestinationAddImage.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        iio.CGImageDestinationAddImage.restype = None
        iio.CGImageDestinationFinalize.argtypes = [ctypes.c_void_p]
        iio.CGImageDestinationFinalize.restype = ctypes.c_bool

        iio.CGImageSourceCreateWithData.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        iio.CGImageSourceCreateWithData.restype = ctypes.c_void_p
        iio.CGImageSourceCreateImageAtIndex.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p]
        iio.CGImageSourceCreateImageAtIndex.restype = ctypes.c_void_p
        self.iio_key_quality = ctypes.c_void_p.in_dll(iio, "kCGImageDestinationLossyCompressionQuality").value

    def _setup_ax(self) -> None:
        ax = self.ax
        ax.AXIsProcessTrusted.argtypes = []
        ax.AXIsProcessTrusted.restype = ctypes.c_bool
        ax.AXUIElementCreateSystemWide.argtypes = []
        ax.AXUIElementCreateSystemWide.restype = ctypes.c_void_p
        ax.AXUIElementCreateApplication.argtypes = [ctypes.c_int32]
        ax.AXUIElementCreateApplication.restype = ctypes.c_void_p
        ax.AXUIElementGetPid.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int32)]
        ax.AXUIElementGetPid.restype = ctypes.c_int32
        ax.AXUIElementSetMessagingTimeout.argtypes = [ctypes.c_void_p, ctypes.c_float]
        ax.AXUIElementSetMessagingTimeout.restype = ctypes.c_int32
        ax.AXUIElementCopyAttributeValue.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
        ]
        ax.AXUIElementCopyAttributeValue.restype = ctypes.c_int32
        ax.AXUIElementSetAttributeValue.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        ax.AXUIElementSetAttributeValue.restype = ctypes.c_int32
        ax.AXUIElementPerformAction.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        ax.AXUIElementPerformAction.restype = ctypes.c_int32
        ax.AXUIElementCopyActionNames.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
        ax.AXUIElementCopyActionNames.restype = ctypes.c_int32
        ax.AXUIElementCopyElementAtPosition.argtypes = [
            ctypes.c_void_p, ctypes.c_float, ctypes.c_float, ctypes.POINTER(ctypes.c_void_p),
        ]
        ax.AXUIElementCopyElementAtPosition.restype = ctypes.c_int32
        ax.AXValueGetTypeID.argtypes = []
        ax.AXValueGetTypeID.restype = ctypes.c_ulong
        ax.AXValueCreate.argtypes = [ctypes.c_int32, ctypes.c_void_p]
        ax.AXValueCreate.restype = ctypes.c_void_p
        ax.AXValueGetValue.argtypes = [ctypes.c_void_p, ctypes.c_int32, ctypes.c_void_p]
        ax.AXValueGetValue.restype = ctypes.c_bool
        self.tid_ax_value = int(ax.AXValueGetTypeID())

    def _setup_objc(self) -> None:
        ob = self.objc
        ob.objc_getClass.argtypes = [ctypes.c_char_p]
        ob.objc_getClass.restype = ctypes.c_void_p
        ob.sel_registerName.argtypes = [ctypes.c_char_p]
        ob.sel_registerName.restype = ctypes.c_void_p
        ob.objc_autoreleasePoolPush.argtypes = []
        ob.objc_autoreleasePoolPush.restype = ctypes.c_void_p
        ob.objc_autoreleasePoolPop.argtypes = [ctypes.c_void_p]
        ob.objc_autoreleasePoolPop.restype = None
        self._msg_send_cache: dict[tuple, Any] = {}

    def objc_call(self, obj: int | ctypes.c_void_p, sel_name: bytes, restype, *typed_args):
        """ABI- and arm64e-PAC-safe typed objc_msgSend call via native symbol binding."""
        argtypes = tuple(t for t, _ in typed_args)
        key = (restype, argtypes)
        fn = self._msg_send_cache.get(key)
        if fn is None:
            proto = ctypes.CFUNCTYPE(restype, ctypes.c_void_p, ctypes.c_void_p, *argtypes)
            fn = proto(("objc_msgSend", self.objc))
            self._msg_send_cache[key] = fn
        sel = self.objc.sel_registerName(sel_name)
        vals = [v for _, v in typed_args]
        return fn(obj, sel, *vals)

    # -- CoreFoundation conversions --

    def cfstr(self, s: str) -> int:
        ptr = self.cf.CFStringCreateWithCString(None, s.encode("utf-8"), kCFStringEncodingUTF8)
        if not ptr:
            raise MemoryError("CFStringCreateWithCString returned NULL")
        return int(ptr)

    def cf_to_str(self, obj: int | None) -> str:
        if not obj:
            return ""
        if int(self.cf.CFGetTypeID(obj)) != self.tid_string:
            return ""
        n = int(self.cf.CFStringGetLength(obj))
        if n <= 0:
            return ""
        max_sz = int(self.cf.CFStringGetMaximumSizeForEncoding(n, kCFStringEncodingUTF8)) + 1
        buf = ctypes.create_string_buffer(max(64, max_sz))
        if self.cf.CFStringGetCString(obj, buf, len(buf), kCFStringEncodingUTF8):
            return buf.value.decode("utf-8", errors="replace")
        return ""

    def cf_to_int(self, obj: int | None, default: int = 0) -> int:
        if not obj:
            return default
        tid = int(self.cf.CFGetTypeID(obj))
        if tid == self.tid_number:
            val = ctypes.c_int64(0)
            if self.cf.CFNumberGetValue(obj, kCFNumberSInt64Type, ctypes.byref(val)):
                return int(val.value)
        elif tid == self.tid_bool:
            return 1 if self.cf.CFBooleanGetValue(obj) else 0
        return default

    def cf_to_float(self, obj: int | None, default: float = 0.0) -> float:
        if not obj:
            return default
        tid = int(self.cf.CFGetTypeID(obj))
        if tid == self.tid_number:
            val = ctypes.c_double(0.0)
            if self.cf.CFNumberGetValue(obj, kCFNumberDoubleType, ctypes.byref(val)):
                return float(val.value)
        elif tid == self.tid_bool:
            return 1.0 if self.cf.CFBooleanGetValue(obj) else 0.0
        return default

    def cf_to_bool(self, obj: int | None, default: bool = False) -> bool:
        if not obj:
            return default
        tid = int(self.cf.CFGetTypeID(obj))
        if tid == self.tid_bool:
            return bool(self.cf.CFBooleanGetValue(obj))
        if tid == self.tid_number:
            return self.cf_to_int(obj, 0) != 0
        return default

    def cf_dict_get(self, d: int, key_name: str) -> int | None:
        k = self.cfstr(key_name)
        try:
            v = self.cf.CFDictionaryGetValue(d, k)
            return int(v) if v else None
        finally:
            self.cf.CFRelease(k)

    def ax_copy_attr(self, elem: int, attr_name: str) -> int | None:
        k = self.cfstr(attr_name)
        try:
            out = ctypes.c_void_p(0)
            err = int(self.ax.AXUIElementCopyAttributeValue(elem, k, ctypes.byref(out)))
            if err == kAXErrorSuccess and out.value:
                return int(out.value)
            return None
        finally:
            self.cf.CFRelease(k)

    def ax_attr_str(self, elem: int, attr_name: str) -> str:
        v = self.ax_copy_attr(elem, attr_name)
        if not v:
            return ""
        try:
            tid = int(self.cf.CFGetTypeID(v))
            if tid == self.tid_string:
                return self.cf_to_str(v)
            if tid == self.tid_number:
                if self.cf.CFNumberIsFloatType(v):
                    return f"{self.cf_to_float(v):g}"
                return str(self.cf_to_int(v))
            if tid == self.tid_bool:
                return "true" if self.cf_to_bool(v) else "false"
            return ""
        finally:
            self.cf.CFRelease(v)

    def ax_attr_point(self, elem: int, attr_name: str) -> tuple[float, float] | None:
        v = self.ax_copy_attr(elem, attr_name)
        if not v:
            return None
        try:
            if int(self.cf.CFGetTypeID(v)) != self.tid_ax_value:
                return None
            pt = CGPoint()
            if self.ax.AXValueGetValue(v, kAXValueTypeCGPoint, ctypes.byref(pt)):
                return float(pt.x), float(pt.y)
            return None
        finally:
            self.cf.CFRelease(v)

    def ax_attr_size(self, elem: int, attr_name: str) -> tuple[float, float] | None:
        v = self.ax_copy_attr(elem, attr_name)
        if not v:
            return None
        try:
            if int(self.cf.CFGetTypeID(v)) != self.tid_ax_value:
                return None
            sz = CGSize()
            if self.ax.AXValueGetValue(v, kAXValueTypeCGSize, ctypes.byref(sz)):
                return float(sz.width), float(sz.height)
            return None
        finally:
            self.cf.CFRelease(v)

    def ax_set_point(self, elem: int, attr_name: str, x: float, y: float) -> bool:
        pt = CGPoint(float(x), float(y))
        av = self.ax.AXValueCreate(kAXValueTypeCGPoint, ctypes.byref(pt))
        if not av:
            return False
        k = None
        try:
            k = self.cfstr(attr_name)
            return int(self.ax.AXUIElementSetAttributeValue(elem, k, av)) == kAXErrorSuccess
        finally:
            if k:
                self.cf.CFRelease(k)
            self.cf.CFRelease(av)

    def ax_set_size(self, elem: int, attr_name: str, w: float, h: float) -> bool:
        sz = CGSize(float(w), float(h))
        av = self.ax.AXValueCreate(kAXValueTypeCGSize, ctypes.byref(sz))
        if not av:
            return False
        k = None
        try:
            k = self.cfstr(attr_name)
            return int(self.ax.AXUIElementSetAttributeValue(elem, k, av)) == kAXErrorSuccess
        finally:
            if k:
                self.cf.CFRelease(k)
            self.cf.CFRelease(av)

    def ax_set_bool(self, elem: int, attr_name: str, val: bool) -> bool:
        k = self.cfstr(attr_name)
        try:
            return int(self.ax.AXUIElementSetAttributeValue(elem, k, self.cf_bool_true if val else self.cf_bool_false)) == kAXErrorSuccess
        finally:
            self.cf.CFRelease(k)

    def ax_perform(self, elem: int, action_name: str) -> bool:
        k = self.cfstr(action_name)
        try:
            return int(self.ax.AXUIElementPerformAction(elem, k)) == kAXErrorSuccess
        finally:
            self.cf.CFRelease(k)

    def activate_pid(self, pid: int) -> bool:
        """Bring process pid to front via NSRunningApplication (macOS 11-26+ safe)."""
        pool = self.objc.objc_autoreleasePoolPush()
        try:
            cls = self.objc.objc_getClass(b"NSRunningApplication")
            if not cls:
                return False
            app = self.objc_call(cls, b"runningApplicationWithProcessIdentifier:", ctypes.c_void_p, (ctypes.c_int32, int(pid)))
            if not app:
                return False
            # NSApplicationActivateAllWindows (1) | NSApplicationActivateIgnoringOtherApps (2) = 3
            ok = self.objc_call(app, b"activateWithOptions:", _OBJC_BOOL, (ctypes.c_uint64, 3))
            return bool(ok)
        except Exception:
            return False
        finally:
            self.objc.objc_autoreleasePoolPop(pool)

    def frontmost_pid(self) -> int | None:
        """Return live PID of the frontmost macOS app (AXFocusedApplication IPC + RunLoop-pumped NSWorkspace)."""
        # Prefer live WindowServer AX query so stdio MCP servers never see stale NSWorkspace cache
        if self.accessibility_granted():
            sys_el = self.ax.AXUIElementCreateSystemWide()
            if sys_el:
                try:
                    app_el = self.ax_copy_attr(sys_el, "AXFocusedApplication")
                    if app_el:
                        try:
                            pid_v = ctypes.c_int32(0)
                            if int(self.ax.AXUIElementGetPid(app_el, ctypes.byref(pid_v))) == kAXErrorSuccess and pid_v.value > 0:
                                return int(pid_v.value)
                        finally:
                            self.cf.CFRelease(app_el)
                finally:
                    self.cf.CFRelease(sys_el)

        pool = self.objc.objc_autoreleasePoolPush()
        try:
            # Pump CFRunLoop briefly so NSWorkspace refreshes LaunchServices frontmost-app notifications
            self.cf.CFRunLoopRunInMode(self.cf_runloop_default_mode, 0.0, False)
            ws_cls = self.objc.objc_getClass(b"NSWorkspace")
            if not ws_cls:
                return None
            ws = self.objc_call(ws_cls, b"sharedWorkspace", ctypes.c_void_p)
            if not ws:
                return None
            app = self.objc_call(ws, b"frontmostApplication", ctypes.c_void_p)
            if not app:
                return None
            pid = self.objc_call(app, b"processIdentifier", ctypes.c_int32)
            return int(pid) if pid and int(pid) > 0 else None
        except Exception:
            return None
        finally:
            self.objc.objc_autoreleasePoolPop(pool)

    def accessibility_granted(self) -> bool:
        try:
            return bool(self.ax.AXIsProcessTrusted())
        except Exception:
            return False

    def screen_recording_granted(self) -> bool:
        if not self.has_preflight_screen:
            return True
        try:
            return bool(self.cg.CGPreflightScreenCaptureAccess())
        except Exception:
            return False


# ---------------------------------------------------------------------------
# MacWindowManager
# ---------------------------------------------------------------------------

class MacWindowManager:
    """Fast native macOS window manager (CoreGraphics CGWindowList + AXUIElement + NSWorkspace)."""

    def __init__(self) -> None:
        self._fw = _MacFrameworks.get()
        self._saved_bounds: dict[str, tuple[int, int, int, int]] = {}

    @property
    def backend(self) -> str:
        return "macos-cg+ax"

    def primary_display_bounds(self) -> tuple[int, int, int, int]:
        try:
            did = int(self._fw.cg.CGMainDisplayID())
            if did:
                r = self._fw.cg.CGDisplayBounds(did)
                w = max(1, int(round(r.size.width)))
                h = max(1, int(round(r.size.height)))
                return int(round(r.origin.x)), int(round(r.origin.y)), w, h
        except Exception:
            pass
        return 0, 0, 1440, 900

    def all_display_bounds(self) -> list[tuple[int, int, int, int]]:
        out: list[tuple[int, int, int, int]] = []
        try:
            max_d = 16
            ids = (ctypes.c_uint32 * max_d)()
            cnt = ctypes.c_uint32(0)
            err = int(self._fw.cg.CGGetActiveDisplayList(max_d, ids, ctypes.byref(cnt)))
            if err == 0 and cnt.value > 0:
                for i in range(int(cnt.value)):
                    r = self._fw.cg.CGDisplayBounds(int(ids[i]))
                    rw, rh = int(round(r.size.width)), int(round(r.size.height))
                    if rw > 0 and rh > 0:
                        out.append((int(round(r.origin.x)), int(round(r.origin.y)), rw, rh))
        except Exception:
            pass
        if not out:
            out.append(self.primary_display_bounds())
        return out

    def display_bounds_for_point(self, px: float, py: float) -> tuple[int, int, int, int]:
        for dx, dy, dw, dh in self.all_display_bounds():
            if dx <= px < dx + dw and dy <= py < dy + dh:
                return dx, dy, dw, dh
        return self.primary_display_bounds()

    def virtual_bounds(self) -> tuple[int, int, int, int]:
        """Union bounding box across all active macOS displays (supports negative offsets)."""
        displays = self.all_display_bounds()
        x0 = min(d[0] for d in displays)
        y0 = min(d[1] for d in displays)
        x1 = max(d[0] + d[2] for d in displays)
        y1 = max(d[1] + d[3] for d in displays)
        return x0, y0, max(1, x1 - x0), max(1, y1 - y0)

    def screen_size(self) -> tuple[int, int]:
        _, _, w, h = self.virtual_bounds()
        return w, h

    def cursor_pos(self) -> tuple[int, int]:
        ev = self._fw.cg.CGEventCreate(None)
        if not ev:
            return 0, 0
        try:
            pt = self._fw.cg.CGEventGetLocation(ev)
            return int(round(pt.x)), int(round(pt.y))
        finally:
            self._fw.cf.CFRelease(ev)

    def state(self) -> dict[str, Any]:
        t0 = time.perf_counter()
        fw = self._fw
        vx, vy, vw, vh = self.virtual_bounds()
        _, _, pw, ph = self.primary_display_bounds()
        cx, cy = self.cursor_pos()
        fg_pid = fw.frontmost_pid()

        windows: list[dict[str, Any]] = []
        active: dict[str, Any] | None = None

        arr = fw.cg.CGWindowListCopyWindowInfo(
            kCGWindowListOptionOnScreenOnly | kCGWindowListExcludeDesktopElements,
            0,
        )
        if arr:
            try:
                count = int(fw.cf.CFArrayGetCount(arr))
                for i in range(count):
                    d = fw.cf.CFArrayGetValueAtIndex(arr, i)
                    if not d:
                        continue
                    layer = fw.cf_to_int(fw.cf_dict_get(d, "kCGWindowLayer"), -1)
                    if layer != 0:
                        continue
                    alpha_v = fw.cf_to_float(fw.cf_dict_get(d, "kCGWindowAlpha"), 1.0)
                    if alpha_v <= 0.01:
                        continue
                    wid = fw.cf_to_int(fw.cf_dict_get(d, "kCGWindowNumber"), 0)
                    pid = fw.cf_to_int(fw.cf_dict_get(d, "kCGWindowOwnerPID"), 0)
                    owner = fw.cf_to_str(fw.cf_dict_get(d, "kCGWindowOwnerName"))
                    title = fw.cf_to_str(fw.cf_dict_get(d, "kCGWindowName"))
                    bounds_dict = fw.cf_dict_get(d, "kCGWindowBounds")
                    rect = CGRect()
                    if not bounds_dict or not fw.cg.CGRectMakeWithDictionaryRepresentation(bounds_dict, ctypes.byref(rect)):
                        continue
                    x = int(round(rect.origin.x))
                    y = int(round(rect.origin.y))
                    w = int(round(rect.size.width))
                    h = int(round(rect.size.height))
                    if w < 16 or h < 16:
                        continue
                    # Skip Apple system helper overlays / WindowServer artifacts
                    if owner in ("Window Server", "Dock", "Control Center", "Notification Center"):
                        continue

                    _, _, dw, dh = self.display_bounds_for_point(x + w / 2.0, y + h / 2.0)
                    is_fs = (w >= dw - 4 and h >= dh - 4)
                    is_active = (active is None) and (fg_pid is not None and pid == fg_pid)
                    item = {
                        "id": str(wid),
                        "title": title,
                        "app": owner or f"pid:{pid}",
                        "pid": pid,
                        "x": x,
                        "y": y,
                        "w": w,
                        "h": h,
                        "active": is_active,
                        "minimized": False,
                        "fullscreen": is_fs,
                    }
                    windows.append(item)
                    if is_active:
                        active = item
            finally:
                fw.cf.CFRelease(arr)

        if active is None and windows:
            windows[0]["active"] = True
            active = windows[0]

        return {
            "backend": self.backend,
            "screen": {"x": vx, "y": vy, "w": vw, "h": vh},
            "cursor": {"x": cx, "y": cy},
            "active": active,
            "windows": windows,
            "elapsed_ms": round((time.perf_counter() - t0) * 1000.0, 2),
        }

    def window_info_by_id(self, wid: int) -> dict[str, Any] | None:
        """O(1) single-window lookup (works even for off-Space or minimized windows)."""
        fw = self._fw
        arr = fw.cg.CGWindowListCopyWindowInfo(kCGWindowListOptionIncludingWindow, int(wid))
        if not arr:
            return None
        try:
            if int(fw.cf.CFArrayGetCount(arr)) <= 0:
                return None
            d = fw.cf.CFArrayGetValueAtIndex(arr, 0)
            if not d:
                return None
            bounds_dict = fw.cf_dict_get(d, "kCGWindowBounds")
            rect = CGRect()
            if not bounds_dict or not fw.cg.CGRectMakeWithDictionaryRepresentation(bounds_dict, ctypes.byref(rect)):
                return None
            pid = fw.cf_to_int(fw.cf_dict_get(d, "kCGWindowOwnerPID"), 0)
            owner = fw.cf_to_str(fw.cf_dict_get(d, "kCGWindowOwnerName"))
            title = fw.cf_to_str(fw.cf_dict_get(d, "kCGWindowName"))
            on_screen = fw.cf_to_bool(fw.cf_dict_get(d, "kCGWindowIsOnscreen"), False)
            return {
                "id": str(int(wid)),
                "pid": pid,
                "app": owner or f"pid:{pid}",
                "title": title,
                "x": int(round(rect.origin.x)),
                "y": int(round(rect.origin.y)),
                "w": max(1, int(round(rect.size.width))),
                "h": max(1, int(round(rect.size.height))),
                "on_screen": on_screen,
            }
        finally:
            fw.cf.CFRelease(arr)

    def find_window(
        self,
        *,
        window_id: str | None = None,
        app: str | None = None,
        title: str | None = None,
        include_offscreen: bool = False,
    ) -> dict[str, Any] | None:
        if window_id:
            try:
                exact = self.window_info_by_id(int(window_id))
                if exact is not None and (include_offscreen or exact.get("on_screen", True)):
                    return exact
            except Exception:
                pass
        st = self.state()
        for w in st["windows"]:
            if window_id and str(w["id"]) == str(window_id):
                return w
        al = app.lower() if app else None
        tl = title.lower() if title else None
        for w in st["windows"]:
            if al and al not in w["app"].lower():
                continue
            if tl and tl not in w["title"].lower():
                continue
            return w
        if include_offscreen:
            fw = self._fw
            arr = fw.cg.CGWindowListCopyWindowInfo(kCGWindowListOptionAll | kCGWindowListExcludeDesktopElements, 0)
            if arr:
                try:
                    for i in range(int(fw.cf.CFArrayGetCount(arr))):
                        d = fw.cf.CFArrayGetValueAtIndex(arr, i)
                        if not d or fw.cf_to_int(fw.cf_dict_get(d, "kCGWindowLayer"), -1) != 0:
                            continue
                        wid_v = fw.cf_to_int(fw.cf_dict_get(d, "kCGWindowNumber"), 0)
                        pid_v = fw.cf_to_int(fw.cf_dict_get(d, "kCGWindowOwnerPID"), 0)
                        owner = fw.cf_to_str(fw.cf_dict_get(d, "kCGWindowOwnerName"))
                        wtitle = fw.cf_to_str(fw.cf_dict_get(d, "kCGWindowName"))
                        if window_id and str(wid_v) != str(window_id):
                            continue
                        if al and al not in owner.lower():
                            continue
                        if tl and tl not in wtitle.lower():
                            continue
                        bdict = fw.cf_dict_get(d, "kCGWindowBounds")
                        rect = CGRect()
                        if not bdict or not fw.cg.CGRectMakeWithDictionaryRepresentation(bdict, ctypes.byref(rect)):
                            continue
                        return {
                            "id": str(wid_v), "pid": pid_v, "app": owner or f"pid:{pid_v}", "title": wtitle,
                            "x": int(round(rect.origin.x)), "y": int(round(rect.origin.y)),
                            "w": max(1, int(round(rect.size.width))), "h": max(1, int(round(rect.size.height))),
                        }
                finally:
                    fw.cf.CFRelease(arr)
        return None

    def _ax_find_window(self, pid: int, target_title: str, target_rect: tuple[int, int, int, int]) -> int | None:
        """Return retained AXUIElement for the matching window of pid (caller MUST CFRelease)."""
        fw = self._fw
        if not fw.accessibility_granted():
            return None
        app_el = fw.ax.AXUIElementCreateApplication(int(pid))
        if not app_el:
            return None
        fw.ax.AXUIElementSetMessagingTimeout(app_el, 0.35)
        try:
            wins = fw.ax_copy_attr(app_el, "AXWindows")
            if not wins:
                return None
            try:
                if int(fw.cf.CFGetTypeID(wins)) != fw.tid_array:
                    return None
                n = int(fw.cf.CFArrayGetCount(wins))
                best: int | None = None
                best_score = -1
                tx, ty, tw, th = target_rect
                for i in range(n):
                    w_el = fw.cf.CFArrayGetValueAtIndex(wins, i)
                    if not w_el:
                        continue
                    score = 0
                    wt = fw.ax_attr_str(w_el, "AXTitle")
                    if target_title and wt == target_title:
                        score += 10
                    pos = fw.ax_attr_point(w_el, "AXPosition")
                    sz = fw.ax_attr_size(w_el, "AXSize")
                    if pos and sz:
                        if abs(pos[0] - tx) < 12 and abs(pos[1] - ty) < 12:
                            score += 5
                        if abs(sz[0] - tw) < 20 and abs(sz[1] - th) < 20:
                            score += 5
                    if score > best_score:
                        best_score = score
                        best = int(w_el)
                if best is not None:
                    fw.cf.CFRetain(best)
                    return best
                return None
            finally:
                fw.cf.CFRelease(wins)
        finally:
            fw.cf.CFRelease(app_el)

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
        target = self.find_window(
            window_id=window_id,
            app=app,
            title=title,
            include_offscreen=(action in ("focus", "unmaximize")),
        )
        if target is None:
            raise RuntimeError(f"Window not found (id={window_id}, app={app}, title={title})")

        fw = self._fw
        pid = int(target["pid"])
        wid = str(target["id"])
        ax_win = self._ax_find_window(pid, target["title"], (target["x"], target["y"], target["w"], target["h"]))

        try:
            if action == "focus":
                fw.activate_pid(pid)
                if ax_win:
                    fw.ax_set_bool(ax_win, "AXMinimized", False)
                    fw.ax_set_bool(ax_win, "AXMain", True)
                    fw.ax_perform(ax_win, "AXRaise")
            elif action in ("minimize", "maximize", "unmaximize", "close", "move_resize"):
                if not ax_win:
                    perm = "" if fw.accessibility_granted() else " (grant Accessibility permission in System Settings -> Privacy & Security)"
                    raise RuntimeError(f"Could not access AX window for {target['app']!r} to perform {action!r}{perm}")
                if action == "minimize":
                    fw.ax_set_bool(ax_win, "AXMinimized", True)
                elif action == "maximize":
                    self._saved_bounds[wid] = (target["x"], target["y"], target["w"], target["h"])
                    dx, dy, dw, dh = self.display_bounds_for_point(
                        target["x"] + target["w"] / 2.0,
                        target["y"] + target["h"] / 2.0,
                    )
                    top_inset = 28 if (dx == 0 and dy == 0) else 0
                    fw.ax_set_point(ax_win, "AXPosition", dx, dy + top_inset)
                    fw.ax_set_size(ax_win, "AXSize", dw, max(100, dh - top_inset))
                elif action == "unmaximize":
                    prev = self._saved_bounds.pop(wid, None)
                    fw.ax_set_bool(ax_win, "AXMinimized", False)
                    if prev:
                        fw.ax_set_point(ax_win, "AXPosition", prev[0], prev[1])
                        fw.ax_set_size(ax_win, "AXSize", prev[2], prev[3])
                elif action == "close":
                    btn = fw.ax_copy_attr(ax_win, "AXCloseButton")
                    if btn:
                        try:
                            fw.ax_perform(btn, "AXPress")
                        finally:
                            fw.cf.CFRelease(btn)
                elif action == "move_resize":
                    nx = target["x"] if x is None else int(x)
                    ny = target["y"] if y is None else int(y)
                    nw = target["w"] if w is None else int(w)
                    nh = target["h"] if h is None else int(h)
                    fw.ax_set_point(ax_win, "AXPosition", nx, ny)
                    fw.ax_set_size(ax_win, "AXSize", nw, nh)
            else:
                raise ValueError(f"Unsupported window action: {action}")
        finally:
            if ax_win:
                fw.cf.CFRelease(ax_win)

        return {
            "ok": True,
            "action": action,
            "id": target["id"],
            "app": target["app"],
            "title": target["title"],
            "window": target,
        }

    def launch(
        self,
        command: str | list[str],
        *,
        wait_app: str | None = None,
        timeout_ms: int = 5000,
        background: bool = False,
    ) -> dict[str, Any]:
        """Launch a macOS .app bundle (`open [-g] -a <App>` / `open [-g] <path.app>`) or CLI binary."""
        if isinstance(command, str):
            cmd_str = command.strip()
            argv = shlex.split(cmd_str)
        else:
            argv = [str(a) for a in command]
            cmd_str = " ".join(argv)
        if not argv:
            raise ValueError("Empty launch command")

        # Handle .app bundle paths (e.g. "/Applications/Safari.app")
        is_app_path = (
            (cmd_str.endswith(".app") and os.path.isdir(cmd_str))
            or (len(argv) == 1 and argv[0].endswith(".app") and "/" in argv[0])
        )
        # Handle app names with or without spaces (e.g. "Safari", "Visual Studio Code", "System Settings")
        is_app_name = (
            isinstance(command, str)
            and not is_app_path
            and "/" not in cmd_str
            and not _which(argv[0])
            and not os.path.exists(argv[0])
        )
        if is_app_path:
            bundle = cmd_str if os.path.isdir(cmd_str) else argv[0]
            open_cmd = ["open"] + (["-g"] if background else []) + [bundle]
            proc = subprocess.Popen(open_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, start_new_session=True)
            search_key = wait_app or os.path.basename(bundle).removesuffix(".app")
        elif is_app_name:
            app_name = cmd_str[:-4] if cmd_str.endswith(".app") else cmd_str
            open_cmd = ["open"] + (["-g"] if background else []) + ["-a", app_name]
            proc = subprocess.Popen(open_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, start_new_session=True)
            search_key = wait_app or app_name
        else:
            if background and argv[0] == "open" and "-g" not in argv:
                argv = ["open", "-g"] + argv[1:]
            proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            search_key = wait_app or os.path.basename(argv[0])

        matched = None
        if search_key:
            deadline = time.monotonic() + (timeout_ms / 1000.0)
            while time.monotonic() < deadline:
                if (is_app_path or is_app_name) and proc.poll() is not None and proc.returncode != 0:
                    err = (proc.stderr.read().decode("utf-8", errors="replace").strip() if proc.stderr else "")
                    raise RuntimeError(f"macOS open failed (rc={proc.returncode}): {err or cmd_str}")
                matched = self.find_window(app=search_key)
                if matched is not None:
                    break
                time.sleep(0.06)
        if proc.stderr:
            try:
                proc.stderr.close()
            except Exception:
                pass

        return {"ok": True, "pid": proc.pid, "command": cmd_str, "window": matched}


def _which(name: str) -> str | None:
    import shutil
    return shutil.which(name)


# ---------------------------------------------------------------------------
# MacInputDriver (CoreGraphics CGEvent, arm64 + x86_64 safe)
# ---------------------------------------------------------------------------

class MacInputDriver:
    """Native macOS CoreGraphics CGEvent input driver (layout-independent UTF-16 Unicode typing)."""

    def __init__(self, virtual_bounds_fn: Callable[[], tuple[int, int, int, int]]) -> None:
        self._fw = _MacFrameworks.get()
        self._vbounds_fn = virtual_bounds_fn
        # Private event source prevents inheriting user's held modifiers and avoids 250ms HID suppression
        self._source = self._fw.cg.CGEventSourceCreate(kCGEventSourceStatePrivate)
        if self._source:
            self._fw.cg.CGEventSourceSetLocalEventsSuppressionInterval(self._source, 0.0)
        self._last_pos: tuple[int, int] | None = None
        atexit.register(self.close)

    @property
    def backend(self) -> str:
        trusted = "trusted" if self._fw.accessibility_granted() else "needs-accessibility"
        return f"macos-cgevent ({trusted})"

    @property
    def layout(self) -> str:
        return "macos-unicode"

    def close(self) -> None:
        src = getattr(self, "_source", None)
        if src:
            try:
                self._fw.cf.CFRelease(src)
            except Exception:
                pass
            self._source = None

    def _clamp(self, x: int, y: int) -> tuple[float, float]:
        vx, vy, vw, vh = self._vbounds_fn()
        cx = max(vx, min(vx + vw - 1, int(x)))
        cy = max(vy, min(vy + vh - 1, int(y)))
        return float(cx), float(cy)

    def _post(self, ev: int | None, *, pid: int | None = None) -> None:
        if not ev:
            return
        try:
            if pid is not None and self._fw.has_post_to_pid:
                self._fw.cg.CGEventPostToPid(int(pid), ev)
            else:
                self._fw.cg.CGEventPost(kCGHIDEventTap, ev)
        finally:
            self._fw.cf.CFRelease(ev)

    def _post_flag_change(self, keycode: int, flags: int, *, pid: int | None = None) -> None:
        """Emit proper kCGEventFlagsChanged for modifier press/release without leaking global HID flags."""
        ev = self._fw.cg.CGEventCreate(self._source)
        if not ev:
            return
        self._fw.cg.CGEventSetType(ev, kCGEventFlagsChanged)
        self._fw.cg.CGEventSetIntegerValueField(ev, kCGKeyboardEventKeycode, int(keycode))
        self._fw.cg.CGEventSetFlags(ev, int(flags))
        self._post(ev, pid=pid)

    def restore_cursor(self, x: int, y: int) -> tuple[int, int]:
        """Silently restore user's physical cursor without firing mouseExited hover flicker."""
        fx, fy = self._clamp(x, y)
        self._fw.cg.CGWarpMouseCursorPosition(CGPoint(fx, fy))
        self._fw.cg.CGAssociateMouseAndMouseCursorPosition(True)
        return int(fx), int(fy)

    def move(self, x: int, y: int) -> tuple[int, int]:
        fx, fy = self._clamp(x, y)
        ev = self._fw.cg.CGEventCreateMouseEvent(
            self._source, kCGEventMouseMoved, CGPoint(fx, fy), kCGMouseButtonLeft,
        )
        if ev:
            self._fw.cg.CGEventSetFlags(ev, 0)
        self._post(ev)
        self._last_pos = (int(fx), int(fy))
        return self._last_pos

    def click(
        self,
        x: int | None = None,
        y: int | None = None,
        *,
        button: str = "left",
        count: int = 1,
        modifiers: list[str] | None = None,
    ) -> tuple[int, int] | None:
        if x is not None and y is not None:
            fx, fy = self._clamp(x, y)
        else:
            ev0 = self._fw.cg.CGEventCreate(None)
            try:
                pt = self._fw.cg.CGEventGetLocation(ev0) if ev0 else CGPoint(0, 0)
                fx, fy = float(pt.x), float(pt.y)
            finally:
                if ev0:
                    self._fw.cf.CFRelease(ev0)

        btn_map = {
            "left": (kCGEventLeftMouseDown, kCGEventLeftMouseUp, kCGMouseButtonLeft),
            "right": (kCGEventRightMouseDown, kCGEventRightMouseUp, kCGMouseButtonRight),
            "middle": (kCGEventOtherMouseDown, kCGEventOtherMouseUp, kCGMouseButtonCenter),
        }
        down_t, up_t, btn_id = btn_map.get(button, btn_map["left"])

        mod_flags = 0
        mod_codes: list[int] = []
        for m in (modifiers or []):
            ml = m.lower()
            mod_flags |= MAC_MODIFIER_FLAGS.get(ml, 0)
            if ml in MAC_KEYCODES:
                mod_codes.append(MAC_KEYCODES[ml])

        mv = self._fw.cg.CGEventCreateMouseEvent(self._source, kCGEventMouseMoved, CGPoint(fx, fy), btn_id)
        if mv:
            self._fw.cg.CGEventSetFlags(mv, mod_flags)
        self._post(mv)
        time.sleep(0.002)

        cur_flags = 0
        try:
            for mc in mod_codes:
                cur_flags |= MAC_CODE_TO_FLAG.get(mc, 0)
                self._post_flag_change(mc, cur_flags)
            for click_num in range(1, max(1, int(count)) + 1):
                ev_d = self._fw.cg.CGEventCreateMouseEvent(self._source, down_t, CGPoint(fx, fy), btn_id)
                if ev_d:
                    self._fw.cg.CGEventSetIntegerValueField(ev_d, kCGMouseEventClickState, click_num)
                    self._fw.cg.CGEventSetFlags(ev_d, mod_flags)
                self._post(ev_d)
                time.sleep(0.005)
                ev_u = self._fw.cg.CGEventCreateMouseEvent(self._source, up_t, CGPoint(fx, fy), btn_id)
                if ev_u:
                    self._fw.cg.CGEventSetIntegerValueField(ev_u, kCGMouseEventClickState, click_num)
                    self._fw.cg.CGEventSetFlags(ev_u, mod_flags)
                self._post(ev_u)
                if click_num < count:
                    time.sleep(0.035)
        finally:
            rem_flags = cur_flags
            for mc in reversed(mod_codes):
                rem_flags &= ~MAC_CODE_TO_FLAG.get(mc, 0)
                self._post_flag_change(mc, rem_flags)

        self._last_pos = (int(fx), int(fy))
        return self._last_pos

    def drag(
        self,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        *,
        button: str = "left",
        duration_ms: int = 120,
        steps: int = 12,
        on_step: Callable[[int, int], None] | None = None,
    ) -> tuple[tuple[int, int], tuple[int, int]]:
        sx, sy = self._clamp(x1, y1)
        ex, ey = self._clamp(x2, y2)
        btn_map = {
            "left": (kCGEventLeftMouseDown, kCGEventLeftMouseDragged, kCGEventLeftMouseUp, kCGMouseButtonLeft),
            "right": (kCGEventRightMouseDown, kCGEventRightMouseDragged, kCGEventRightMouseUp, kCGMouseButtonRight),
            "middle": (kCGEventOtherMouseDown, kCGEventOtherMouseDragged, kCGEventOtherMouseUp, kCGMouseButtonCenter),
        }
        down_t, drag_t, up_t, btn_id = btn_map.get(button, btn_map["left"])

        self.move(int(sx), int(sy))
        time.sleep(0.010)
        ev_d = self._fw.cg.CGEventCreateMouseEvent(self._source, down_t, CGPoint(sx, sy), btn_id)
        if ev_d:
            self._fw.cg.CGEventSetIntegerValueField(ev_d, kCGMouseEventClickState, 1)
            self._fw.cg.CGEventSetFlags(ev_d, 0)
        self._post(ev_d)
        time.sleep(0.012)
        try:
            n = max(2, int(steps))
            dt = max(0.004, (duration_ms / 1000.0) / n)
            for i in range(1, n + 1):
                t = i / n
                ix = sx + (ex - sx) * t
                iy = sy + (ey - sy) * t
                ev_m = self._fw.cg.CGEventCreateMouseEvent(self._source, drag_t, CGPoint(ix, iy), btn_id)
                if ev_m:
                    self._fw.cg.CGEventSetFlags(ev_m, 0)
                self._post(ev_m)
                if on_step is not None:
                    try:
                        on_step(int(round(ix)), int(round(iy)))
                    except Exception:
                        pass
                time.sleep(dt)
        finally:
            ev_u = self._fw.cg.CGEventCreateMouseEvent(self._source, up_t, CGPoint(ex, ey), btn_id)
            if ev_u:
                self._fw.cg.CGEventSetIntegerValueField(ev_u, kCGMouseEventClickState, 1)
                self._fw.cg.CGEventSetFlags(ev_u, 0)
            self._post(ev_u)
        self._last_pos = (int(ex), int(ey))
        return (int(sx), int(sy)), (int(ex), int(ey))

    def scroll(
        self,
        direction: str = "down",
        clicks: int = 5,
        *,
        x: int | None = None,
        y: int | None = None,
    ) -> None:
        pt_loc: tuple[float, float] | None = None
        if x is not None and y is not None:
            pt_loc = self._clamp(x, y)
            self.move(int(pt_loc[0]), int(pt_loc[1]))
            time.sleep(0.004)
        n = max(1, int(clicks))
        dy = 0
        dx = 0
        # CoreGraphics line scroll: positive dy = scroll UP, negative dy = scroll DOWN
        if direction == "up":
            dy = n
        elif direction == "down":
            dy = -n
        elif direction == "left":
            dx = n
        elif direction == "right":
            dx = -n
        else:
            raise ValueError(f"Unsupported scroll direction: {direction}")

        if self._fw.has_scroll2:
            wheel_cnt = 1 if dx == 0 else 2
            ev = self._fw.cg.CGEventCreateScrollWheelEvent2(
                self._source, kCGScrollEventUnitLine, wheel_cnt, dy, dx, 0,
            )
            if ev:
                if pt_loc is not None:
                    self._fw.cg.CGEventSetLocation(ev, CGPoint(pt_loc[0], pt_loc[1]))
                self._fw.cg.CGEventSetFlags(ev, 0)
            self._post(ev)

    def hotkey(self, keys: str | list[str], *, count: int = 1, pid: int | None = None) -> None:
        codes, flags = parse_mac_key_combo(keys)
        non_mods = [c for c in codes if c not in MAC_CODE_TO_FLAG]
        mods = [c for c in codes if c in MAC_CODE_TO_FLAG]
        for _ in range(max(1, int(count))):
            cur_flags = 0
            try:
                for mc in mods:
                    cur_flags |= MAC_CODE_TO_FLAG.get(mc, 0)
                    self._post_flag_change(mc, cur_flags, pid=pid)
                for kc in non_mods:
                    ev_d = self._fw.cg.CGEventCreateKeyboardEvent(self._source, kc, True)
                    if ev_d:
                        self._fw.cg.CGEventSetFlags(ev_d, flags)
                    self._post(ev_d, pid=pid)
                    time.sleep(0.004)
                    ev_u = self._fw.cg.CGEventCreateKeyboardEvent(self._source, kc, False)
                    if ev_u:
                        self._fw.cg.CGEventSetFlags(ev_u, flags)
                    self._post(ev_u, pid=pid)
            finally:
                rem_flags = cur_flags
                for mc in reversed(mods):
                    rem_flags &= ~MAC_CODE_TO_FLAG.get(mc, 0)
                    self._post_flag_change(mc, rem_flags, pid=pid)
            if count > 1:
                time.sleep(0.015)

    def _send_unicode_segment(self, seg: str, *, key_delay_ms: int = 0, pid: int | None = None) -> None:
        # 1 character (1 BMP unit or 1 surrogate pair) per event ensures Chrome/Electron/Terminal/Java never truncate
        chunks = split_utf16_chunks(seg, max_units=1)
        for units in chunks:
            arr = (ctypes.c_uint16 * len(units))(*units)
            ev_d = self._fw.cg.CGEventCreateKeyboardEvent(self._source, 0, True)
            if ev_d:
                self._fw.cg.CGEventSetFlags(ev_d, 0)
                self._fw.cg.CGEventKeyboardSetUnicodeString(ev_d, len(units), arr)
                self._post(ev_d, pid=pid)
            ev_u = self._fw.cg.CGEventCreateKeyboardEvent(self._source, 0, False)
            if ev_u:
                self._fw.cg.CGEventSetFlags(ev_u, 0)
                self._fw.cg.CGEventKeyboardSetUnicodeString(ev_u, len(units), arr)
                self._post(ev_u, pid=pid)
            time.sleep(max(0.001, key_delay_ms / 1000.0))

    def type_keys(self, text: str, *, key_delay_ms: int = 0, pid: int | None = None) -> None:
        """Type arbitrary Unicode text via CGEventKeyboardSetUnicodeString (real Return/Tab keys)."""
        buf: list[str] = []
        i = 0
        n = len(text)
        while i < n:
            ch = text[i]
            if ch in ("\n", "\r", "\t"):
                if buf:
                    self._send_unicode_segment("".join(buf), key_delay_ms=key_delay_ms, pid=pid)
                    buf.clear()
                if ch == "\r" and i + 1 < n and text[i + 1] == "\n":
                    i += 1
                kc = 0x30 if ch == "\t" else 0x24
                ev_d = self._fw.cg.CGEventCreateKeyboardEvent(self._source, kc, True)
                if ev_d:
                    self._fw.cg.CGEventSetFlags(ev_d, 0)
                self._post(ev_d, pid=pid)
                ev_u = self._fw.cg.CGEventCreateKeyboardEvent(self._source, kc, False)
                if ev_u:
                    self._fw.cg.CGEventSetFlags(ev_u, 0)
                self._post(ev_u, pid=pid)
                if key_delay_ms > 0:
                    time.sleep(key_delay_ms / 1000.0)
            else:
                buf.append(ch)
            i += 1
        if buf:
            self._send_unicode_segment("".join(buf), key_delay_ms=key_delay_ms, pid=pid)

    def is_pure_ascii_typable(self, text: str) -> bool:
        # CGEventKeyboardSetUnicodeString handles any printable Unicode without clipboard touch
        return True


# ---------------------------------------------------------------------------
# MacClipboard (UTF-8 forced pbpaste / pbcopy)
# ---------------------------------------------------------------------------

class MacClipboard:
    """macOS clipboard via UTF-8 pbpaste / pbcopy (safe from any thread and headless CI)."""

    @property
    def backend(self) -> str:
        return "macos-pbcopy"

    @staticmethod
    def _utf8_env() -> dict[str, str]:
        env = os.environ.copy()
        env["LANG"] = "en_US.UTF-8"
        env["LC_ALL"] = "en_US.UTF-8"
        return env

    def get_text(self) -> str:
        r = subprocess.run(
            ["pbpaste"],
            capture_output=True,
            timeout=2.0,
            env=self._utf8_env(),
        )
        if r.returncode != 0:
            return ""
        return r.stdout.decode("utf-8", errors="replace")

    def set_text(self, text: str) -> None:
        subprocess.run(
            ["pbcopy"],
            input=text.encode("utf-8"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2.0,
            env=self._utf8_env(),
            check=True,
        )


# ---------------------------------------------------------------------------
# MacScreenCapture (CoreGraphics + ScreenCaptureKit fallback + ImageIO/TurboJPEG)
# ---------------------------------------------------------------------------

class MacScreenCapture:
    """Fast macOS screen capture and encoding (CoreGraphics + screencapture fallback + ImageIO/TurboJPEG)."""

    def __init__(self, virtual_bounds_fn: Callable[[], tuple[int, int, int, int]]) -> None:
        self._fw = _MacFrameworks.get()
        self._vbounds_fn = virtual_bounds_fn
        self._last_thumb: np.ndarray | None = None
        self._last_frame_rect: tuple[int, int, int, int] | None = None
        self._cg_null_streak = 0
        self._use_screencapture_only = os.environ.get("COMPUTER_USE_MAC_CAPTURE", "").strip().lower() == "screencapture"
        self._turbo = TurboJpegEncoder()

    @property
    def backend(self) -> str:
        enc = "turbojpeg" if self._turbo.available else "imageio"
        if self._fw.has_window_list_image and not self._use_screencapture_only:
            return f"macos-cg+{enc}"
        return f"macos-screencapture+{enc}"

    def _cgimage_to_rgba(self, cg_img: int) -> np.ndarray:
        fw = self._fw
        w = int(fw.cg.CGImageGetWidth(cg_img))
        h = int(fw.cg.CGImageGetHeight(cg_img))
        if w <= 0 or h <= 0:
            raise RuntimeError("Empty CGImage returned by macOS")
        # Zero-init avoids leaking uninitialized heap bytes into rounded window corners
        rgba = np.zeros((h, w, 4), dtype=np.uint8)
        cs = fw.cg.CGColorSpaceCreateDeviceRGB()
        try:
            # 32-bit big-endian [R, G, B, X] writes exact RGBA byte layout directly into numpy
            ctx = fw.cg.CGBitmapContextCreate(
                rgba.ctypes.data,
                w,
                h,
                8,
                w * 4,
                cs,
                kCGBitmapByteOrder32Big | kCGImageAlphaNoneSkipLast,
            )
            if not ctx:
                raise RuntimeError("CGBitmapContextCreate failed")
            try:
                fw.cg.CGContextDrawImage(ctx, CGRect(CGPoint(0.0, 0.0), CGSize(float(w), float(h))), cg_img)
            finally:
                fw.cg.CGContextRelease(ctx)
        finally:
            fw.cg.CGColorSpaceRelease(cs)
        rgba[:, :, 3] = 255
        return rgba

    def _decode_image_bytes_to_rgba(self, data: bytes) -> np.ndarray:
        fw = self._fw
        cf_data = fw.cf.CFDataCreate(None, data, len(data))
        if not cf_data:
            raise RuntimeError("CFDataCreate failed")
        try:
            src = fw.iio.CGImageSourceCreateWithData(cf_data, None)
            if not src:
                raise RuntimeError("CGImageSourceCreateWithData failed")
            try:
                img = fw.iio.CGImageSourceCreateImageAtIndex(src, 0, None)
                if not img:
                    raise RuntimeError("CGImageSourceCreateImageAtIndex returned NULL")
                try:
                    return self._cgimage_to_rgba(img)
                finally:
                    fw.cg.CGImageRelease(img)
            finally:
                fw.cf.CFRelease(src)
        finally:
            fw.cf.CFRelease(cf_data)

    def _capture_via_screencapture_cli(
        self,
        *,
        rect: tuple[int, int, int, int] | None = None,
        window_id: int | None = None,
        include_cursor: bool = False,
    ) -> np.ndarray:
        """Fallback using Apple's ScreenCaptureKit-backed `/usr/sbin/screencapture` CLI."""
        fd, path = tempfile.mkstemp(prefix="agcu_mac_", suffix=".png")
        os.close(fd)
        try:
            cmd = ["/usr/sbin/screencapture", "-x", "-t", "png"]
            if window_id is not None:
                cmd.extend(["-o", "-l", str(int(window_id))])
            elif rect is not None:
                rx, ry, rw, rh = rect
                # Joined -R flag prevents getopt ambiguity on negative multi-monitor coordinates
                cmd.append(f"-R{int(rx)},{int(ry)},{int(rw)},{int(rh)}")
            cmd.append(path)
            r = subprocess.run(cmd, capture_output=True, timeout=5.0)
            if r.returncode != 0 or not os.path.isfile(path) or os.path.getsize(path) == 0:
                stderr = r.stderr.decode("utf-8", errors="replace").strip()
                hint = ""
                if not self._fw.screen_recording_granted():
                    hint = " (grant Screen Recording permission in System Settings -> Privacy & Security -> Screen & System Audio Recording)"
                raise RuntimeError(f"screencapture failed (rc={r.returncode}): {stderr or 'no image returned'}{hint}")
            with open(path, "rb") as f:
                raw = f.read()
            return self._decode_image_bytes_to_rgba(raw)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    def capture_window_bg(self, wid: int, expected_rect: tuple[int, int, int, int] | None = None) -> Frame:
        """Capture a specific window off-screen (even when occluded behind other windows)."""
        t0 = time.perf_counter()
        fw = self._fw
        rgba: np.ndarray | None = None
        used = "macos-cg-window"

        if fw.has_window_list_image:
            img = fw.cg.CGWindowListCreateImage(
                fw.cg_rect_null,
                kCGWindowListOptionIncludingWindow,
                int(wid),
                kCGWindowImageBoundsIgnoreFraming | kCGWindowImageNominalResolution,
            )
            if img:
                try:
                    w = int(fw.cg.CGImageGetWidth(img))
                    h = int(fw.cg.CGImageGetHeight(img))
                    if w > 1 and h > 1:
                        rgba = self._cgimage_to_rgba(img)
                finally:
                    fw.cg.CGImageRelease(img)

        if rgba is None:
            rgba = self._capture_via_screencapture_cli(window_id=int(wid))
            used = "macos-screencapture-window"

        px_h, px_w = rgba.shape[:2]
        ox, oy = (expected_rect[0], expected_rect[1]) if expected_rect else (0, 0)
        lw = expected_rect[2] if (expected_rect and expected_rect[2] > 0) else px_w
        scale = float(px_w) / float(lw) if lw > 0 else 1.0
        return Frame(
            rgba=rgba,
            origin_x=ox,
            origin_y=oy,
            width=px_w,
            height=px_h,
            scale=scale,
            backend=used,
            capture_ms=(time.perf_counter() - t0) * 1000.0,
        )

    def capture(
        self,
        *,
        scope: str = "screen",
        area: tuple[int, int, int, int] | None = None,
        active_rect: tuple[int, int, int, int] | None = None,
        window_id: str | None = None,
        include_cursor: bool = False,
        nominal_res: bool = False,
    ) -> Frame:
        t0 = time.perf_counter()
        fw = self._fw
        vx, vy, vw, vh = self._vbounds_fn()

        if scope == "window" and window_id is not None:
            try:
                return self.capture_window_bg(int(window_id), expected_rect=active_rect)
            except Exception:
                pass

        if scope == "area" and area is not None:
            ox, oy, rw, rh = int(area[0]), int(area[1]), max(1, int(area[2])), max(1, int(area[3]))
        elif scope in ("active", "window") and active_rect is not None:
            ox, oy, rw, rh = int(active_rect[0]), int(active_rect[1]), max(1, int(active_rect[2])), max(1, int(active_rect[3]))
        else:
            ox, oy, rw, rh = vx, vy, vw, vh

        # Clamp sub-rects to virtual bounds so screencapture/CG never clip and skew scale
        if scope in ("area", "active", "window"):
            cx0 = max(vx, min(vx + vw - 1, ox))
            cy0 = max(vy, min(vy + vh - 1, oy))
            cx1 = max(cx0 + 1, min(vx + vw, ox + rw))
            cy1 = max(cy0 + 1, min(vy + vh, oy + rh))
            ox, oy, rw, rh = cx0, cy0, cx1 - cx0, cy1 - cy0

        rgba: np.ndarray | None = None
        used = "macos-cg"
        sr_ok = fw.screen_recording_granted()

        if fw.has_window_list_image and not self._use_screencapture_only and sr_ok:
            cg_rect = CGRect(CGPoint(float(ox), float(oy)), CGSize(float(rw), float(rh)))
            img_opt = kCGWindowImageNominalResolution if nominal_res else kCGWindowImageDefault
            img = fw.cg.CGWindowListCreateImage(
                cg_rect,
                kCGWindowListOptionOnScreenOnly,
                0,
                img_opt,
            )
            if img:
                try:
                    w = int(fw.cg.CGImageGetWidth(img))
                    h = int(fw.cg.CGImageGetHeight(img))
                    if w > 1 and h > 1:
                        rgba = self._cgimage_to_rgba(img)
                        self._cg_null_streak = 0
                finally:
                    fw.cg.CGImageRelease(img)
            if rgba is None:
                self._cg_null_streak += 1
                if self._cg_null_streak >= 2:
                    self._use_screencapture_only = True

        if rgba is None and fw.has_display_image and scope == "screen" and not self._use_screencapture_only and sr_ok:
            did = int(fw.cg.CGMainDisplayID())
            if did:
                img = fw.cg.CGDisplayCreateImage(did)
                if img:
                    try:
                        w = int(fw.cg.CGImageGetWidth(img))
                        h = int(fw.cg.CGImageGetHeight(img))
                        if w > 1 and h > 1:
                            rgba = self._cgimage_to_rgba(img)
                            used = "macos-cg-display"
                            dr = fw.cg.CGDisplayBounds(did)
                            ox, oy = int(round(dr.origin.x)), int(round(dr.origin.y))
                            rw, rh = max(1, int(round(dr.size.width))), max(1, int(round(dr.size.height)))
                    finally:
                        fw.cg.CGImageRelease(img)

        if rgba is None:
            cap_rect = (ox, oy, rw, rh) if (scope in ("area", "active", "window")) else None
            rgba = self._capture_via_screencapture_cli(rect=cap_rect, include_cursor=False)
            used = "macos-screencapture"
            if cap_rect is None:
                did = int(fw.cg.CGMainDisplayID())
                if did:
                    dr = fw.cg.CGDisplayBounds(did)
                    ox, oy = int(round(dr.origin.x)), int(round(dr.origin.y))
                    rw, rh = max(1, int(round(dr.size.width))), max(1, int(round(dr.size.height)))

        px_h, px_w = rgba.shape[:2]
        scale = float(px_w) / float(rw) if rw > 0 else 1.0
        return Frame(
            rgba=rgba,
            origin_x=ox,
            origin_y=oy,
            width=px_w,
            height=px_h,
            scale=scale,
            backend=used,
            capture_ms=(time.perf_counter() - t0) * 1000.0,
        )

    @staticmethod
    def _thumbnail(frame: Frame) -> np.ndarray:
        step = max(1, frame.width // 320)
        # Copy slice so we never pin full 20-60 MB Retina buffers in RAM
        return frame.rgba[::step, ::step, :3].copy()

    def diff_frames(
        self,
        before: Frame,
        after: Frame,
        *,
        ignore_rects: list[tuple[int, int, int, int]] | None = None,
    ) -> dict[str, Any]:
        if before.width != after.width or before.height != after.height:
            return {"changed_pct": 100.0, "changed_bbox": [after.origin_x, after.origin_y, after.logical_width, after.logical_height]}
        step = max(1, after.width // 320)
        a = before.rgba[::step, ::step, :3].astype(np.int16)
        b = after.rgba[::step, ::step, :3].astype(np.int16)
        diff = np.abs(b - a).max(axis=2) > 12
        _apply_ignore_rects(diff, after, step, ignore_rects)
        changed = int(diff.sum())
        pct = round((changed * 100.0) / max(1, diff.size), 2)
        if changed == 0:
            return {"changed_pct": 0.0, "changed_bbox": None}
        ys, xs = np.nonzero(diff)
        sc = after.scale if after.scale > 0 else 1.0
        bx = after.origin_x + int(round((int(xs.min()) * step) / sc))
        by = after.origin_y + int(round((int(ys.min()) * step) / sc))
        x1 = after.origin_x + int(round(min(after.width, (int(xs.max()) + 1) * step) / sc))
        y1 = after.origin_y + int(round(min(after.height, (int(ys.max()) + 1) * step) / sc))
        return {"changed_pct": pct, "changed_bbox": [bx, by, max(1, x1 - bx), max(1, y1 - by)]}

    def diff_from_last(
        self,
        frame: Frame,
        *,
        ignore_rects: list[tuple[int, int, int, int]] | None = None,
    ) -> dict[str, Any] | None:
        thumb = self._thumbnail(frame)
        frect = (frame.origin_x, frame.origin_y, frame.width, frame.height)
        prev = self._last_thumb
        prev_rect = self._last_frame_rect
        self._last_thumb = thumb
        self._last_frame_rect = frect
        if prev is None or prev.shape != thumb.shape or prev_rect != frect:
            return None
        step = max(1, frame.width // 320)
        diff = np.abs(thumb.astype(np.int16) - prev.astype(np.int16)).max(axis=2) > 12
        _apply_ignore_rects(diff, frame, step, ignore_rects)
        changed = int(diff.sum())
        pct = round((changed * 100.0) / max(1, diff.size), 2)
        if changed == 0:
            return {"changed_pct": 0.0, "changed_bbox": None}
        ys, xs = np.nonzero(diff)
        sc = frame.scale if frame.scale > 0 else 1.0
        bx = frame.origin_x + int(round((int(xs.min()) * step) / sc))
        by = frame.origin_y + int(round((int(ys.min()) * step) / sc))
        x1 = frame.origin_x + int(round(min(frame.width, (int(xs.max()) + 1) * step) / sc))
        y1 = frame.origin_y + int(round(min(frame.height, (int(ys.max()) + 1) * step) / sc))
        return {"changed_pct": pct, "changed_bbox": [bx, by, max(1, x1 - bx), max(1, y1 - by)]}

    def wait_until_settled(
        self,
        *,
        scope: str = "screen",
        area: tuple[int, int, int, int] | None = None,
        active_rect: tuple[int, int, int, int] | None = None,
        max_wait_ms: int = 55,
        poll_ms: int = 16,
        include_cursor: bool = False,
        ignore_rects_fn: Callable[[], list[tuple[int, int, int, int]]] | None = None,
    ) -> Frame:
        deadline = time.monotonic() + (max_wait_ms / 1000.0)
        prev = self.capture(scope=scope, area=area, active_rect=active_rect, include_cursor=include_cursor, nominal_res=True)
        while time.monotonic() < deadline:
            time.sleep(poll_ms / 1000.0)
            cur = self.capture(scope=scope, area=area, active_rect=active_rect, include_cursor=include_cursor, nominal_res=True)
            ig = ignore_rects_fn() if ignore_rects_fn else None
            if self.diff_frames(prev, cur, ignore_rects=ig)["changed_pct"] < 0.08:
                return cur
            prev = cur
        return prev

    def wait_for_change(
        self,
        *,
        area: tuple[int, int, int, int] | None = None,
        timeout_ms: int = 5000,
        threshold_pct: float = 0.15,
        ignore_rects_fn: Callable[[], list[tuple[int, int, int, int]]] | None = None,
    ) -> dict[str, Any]:
        t0 = time.perf_counter()
        scope = "area" if area is not None else "screen"
        base = self.capture(scope=scope, area=area, include_cursor=False, nominal_res=True)
        deadline = time.monotonic() + (timeout_ms / 1000.0)
        while time.monotonic() < deadline:
            time.sleep(0.035)
            cur = self.capture(scope=scope, area=area, include_cursor=False, nominal_res=True)
            ig = ignore_rects_fn() if ignore_rects_fn else None
            d = self.diff_frames(base, cur, ignore_rects=ig)
            if d["changed_pct"] >= threshold_pct:
                return {"changed": True, "waited_ms": round((time.perf_counter() - t0) * 1000.0, 1), **d}
        return {"changed": False, "waited_ms": round((time.perf_counter() - t0) * 1000.0, 1), "changed_pct": 0.0, "changed_bbox": None}

    def _resize_rgba_cg(self, rgba: np.ndarray, out_w: int, out_h: int) -> np.ndarray:
        """Hardware-accelerated bilinear resize via CoreGraphics CGBitmapContext."""
        fw = self._fw
        src_h, src_w = rgba.shape[:2]
        if src_w == out_w and src_h == out_h:
            return rgba.copy()
        src = np.ascontiguousarray(rgba, dtype=np.uint8)
        cs = fw.cg.CGColorSpaceCreateDeviceRGB()
        try:
            prov = fw.cg.CGDataProviderCreateWithData(None, src.ctypes.data, src.nbytes, None)
            if not prov:
                raise RuntimeError("CGDataProviderCreateWithData failed")
            try:
                src_img = fw.cg.CGImageCreate(
                    src_w, src_h, 8, 32, src_w * 4, cs,
                    kCGBitmapByteOrder32Big | kCGImageAlphaNoneSkipLast,
                    prov, None, True, 0,
                )
                if not src_img:
                    raise RuntimeError("CGImageCreate failed")
                try:
                    out = np.zeros((out_h, out_w, 4), dtype=np.uint8)
                    ctx = fw.cg.CGBitmapContextCreate(
                        out.ctypes.data, out_w, out_h, 8, out_w * 4, cs,
                        kCGBitmapByteOrder32Big | kCGImageAlphaNoneSkipLast,
                    )
                    if not ctx:
                        raise RuntimeError("CGBitmapContextCreate failed")
                    try:
                        fw.cg.CGContextSetInterpolationQuality(ctx, kCGInterpolationLow)
                        fw.cg.CGContextDrawImage(ctx, CGRect(CGPoint(0.0, 0.0), CGSize(float(out_w), float(out_h))), src_img)
                    finally:
                        fw.cg.CGContextRelease(ctx)
                    out[:, :, 3] = 255
                    return out
                finally:
                    fw.cg.CGImageRelease(src_img)
            finally:
                fw.cg.CGDataProviderRelease(prov)
        finally:
            fw.cg.CGColorSpaceRelease(cs)

    def _encode_imageio(self, rgba: np.ndarray, *, fmt: str, quality: int) -> bytes:
        """Zero-dependency native macOS ImageIO JPEG/PNG encoder."""
        fw = self._fw
        out_fmt = "png" if str(fmt).lower() == "png" else "jpeg"
        h, w = rgba.shape[:2]
        src = np.ascontiguousarray(rgba, dtype=np.uint8)
        cs = fw.cg.CGColorSpaceCreateDeviceRGB()
        try:
            prov = fw.cg.CGDataProviderCreateWithData(None, src.ctypes.data, src.nbytes, None)
            if not prov:
                raise RuntimeError("CGDataProviderCreateWithData failed")
            try:
                img = fw.cg.CGImageCreate(
                    w, h, 8, 32, w * 4, cs,
                    kCGBitmapByteOrder32Big | kCGImageAlphaNoneSkipLast,
                    prov, None, True, 0,
                )
                if not img:
                    raise RuntimeError("CGImageCreate failed")
                try:
                    mdata = fw.cf.CFDataCreateMutable(None, 0)
                    if not mdata:
                        raise RuntimeError("CFDataCreateMutable failed")
                    try:
                        uti = fw.cfstr("public.png" if out_fmt == "png" else "public.jpeg")
                        try:
                            dest = fw.iio.CGImageDestinationCreateWithData(mdata, uti, 1, None)
                        finally:
                            fw.cf.CFRelease(uti)
                        if not dest:
                            raise RuntimeError("CGImageDestinationCreateWithData failed")
                        try:
                            opts = None
                            v_q = None
                            try:
                                if out_fmt != "png" and fw.iio_key_quality:
                                    q_val = ctypes.c_double(max(0.1, min(1.0, float(quality) / 100.0)))
                                    v_q = fw.cf.CFNumberCreate(None, kCFNumberDoubleType, ctypes.byref(q_val))
                                    if v_q:
                                        keys = (ctypes.c_void_p * 1)(fw.iio_key_quality)
                                        vals = (ctypes.c_void_p * 1)(v_q)
                                        opts = fw.cf.CFDictionaryCreate(
                                            None, keys, vals, 1, fw.cf_dict_key_cbs, fw.cf_dict_val_cbs,
                                        )
                                fw.iio.CGImageDestinationAddImage(dest, img, opts)
                                if not fw.iio.CGImageDestinationFinalize(dest):
                                    raise RuntimeError("CGImageDestinationFinalize failed")
                            finally:
                                if opts:
                                    fw.cf.CFRelease(opts)
                                if v_q:
                                    fw.cf.CFRelease(v_q)
                        finally:
                            fw.cf.CFRelease(dest)
                        n = int(fw.cf.CFDataGetLength(mdata))
                        ptr = fw.cf.CFDataGetBytePtr(mdata)
                        if n <= 0 or not ptr:
                            raise RuntimeError("ImageIO produced empty image buffer")
                        return ctypes.string_at(ptr, n)
                    finally:
                        fw.cf.CFRelease(mdata)
                finally:
                    fw.cg.CGImageRelease(img)
            finally:
                fw.cg.CGDataProviderRelease(prov)
        finally:
            fw.cg.CGColorSpaceRelease(cs)

    def encode(
        self,
        frame: Frame,
        *,
        max_width: int = 1280,
        quality: int = 75,
        fmt: str = "jpeg",
        annotations: list[dict[str, Any]] | None = None,
        grid: bool = False,
        cursor: tuple[int, int] | None = None,
    ) -> dict[str, Any]:
        t0 = time.perf_counter()
        src_w, src_h = frame.width, frame.height
        scale = min(1.0, max_width / float(src_w)) if (max_width and src_w > max_width) else 1.0
        out_w = max(1, int(round(src_w * scale)))
        out_h = max(1, int(round(src_h * scale)))

        rgba = self._resize_rgba_cg(frame.rgba, out_w, out_h)
        need_overlay = bool(annotations or grid or cursor is not None)
        if need_overlay:
            # Convert RGBA -> BGRA only for our shared 5x7 bitmap overlay helper, then back
            bgra = np.ascontiguousarray(rgba[:, :, [2, 1, 0, 3]])
            if grid:
                for step in range(100, 1000, 100):
                    gx = int(round((step / 1000.0) * out_w))
                    gy = int(round((step / 1000.0) * out_h))
                    if 0 <= gx < out_w:
                        bgra[:, gx, 0] = (bgra[:, gx, 0].astype(np.uint16) * 2 + 255) // 3
                        bgra[:, gx, 1] = (bgra[:, gx, 1].astype(np.uint16) * 2 + 220) // 3
                        bgra[:, gx, 2] = (bgra[:, gx, 2].astype(np.uint16) * 2 + 60) // 3
                    if 0 <= gy < out_h:
                        bgra[gy, :, 0] = (bgra[gy, :, 0].astype(np.uint16) * 2 + 255) // 3
                        bgra[gy, :, 1] = (bgra[gy, :, 1].astype(np.uint16) * 2 + 220) // 3
                        bgra[gy, :, 2] = (bgra[gy, :, 2].astype(np.uint16) * 2 + 60) // 3
                    blit_pill_digits_bgra(bgra, gx + 2, 2, str(step), bg_bgr=(32, 24, 14), fg_bgr=(255, 224, 96))
                    blit_pill_digits_bgra(bgra, 2, gy + 2, str(step), bg_bgr=(32, 24, 14), fg_bgr=(255, 224, 96))

            if annotations:
                sc_x = float(out_w) / float(max(1, frame.logical_width))
                sc_y = float(out_h) / float(max(1, frame.logical_height))
                palette = [
                    (40, 95, 235), (235, 115, 25), (65, 185, 35),
                    (220, 60, 170), (20, 165, 235), (210, 185, 25),
                ]
                for idx, el in enumerate(annotations[:120]):
                    bx, by, bw, bh = el["bbox"]
                    rx = (bx - frame.origin_x) * sc_x
                    ry = (by - frame.origin_y) * sc_y
                    rw = bw * sc_x
                    rh = bh * sc_y
                    if rw < 4 or rh < 4 or (rx + rw) < 0 or (ry + rh) < 0 or rx > out_w or ry > out_h:
                        continue
                    x0 = max(0, min(out_w - 1, int(round(rx))))
                    y0 = max(0, min(out_h - 1, int(round(ry))))
                    x1 = max(x0 + 1, min(out_w - 1, int(round(rx + rw))))
                    y1 = max(y0 + 1, min(out_h - 1, int(round(ry + rh))))
                    b_c, g_c, r_c = palette[idx % len(palette)]
                    bgra[y0:min(out_h, y0 + 2), x0:x1, :3] = (b_c, g_c, r_c)
                    bgra[max(0, y1 - 1):min(out_h, y1 + 1), x0:x1, :3] = (b_c, g_c, r_c)
                    bgra[y0:y1, x0:min(out_w, x0 + 2), :3] = (b_c, g_c, r_c)
                    bgra[y0:y1, max(0, x1 - 1):min(out_w, x1 + 1), :3] = (b_c, g_c, r_c)
                    blit_pill_digits_bgra(bgra, x0, max(0, y0 - 11), str(el["ref"]), bg_bgr=(b_c, g_c, r_c))

            if cursor is not None:
                sc_x = float(out_w) / float(max(1, frame.logical_width))
                sc_y = float(out_h) / float(max(1, frame.logical_height))
                cx = int(round((cursor[0] - frame.origin_x) * sc_x))
                cy = int(round((cursor[1] - frame.origin_y) * sc_y))
                if 0 <= cx < out_w and 0 <= cy < out_h:
                    bgra[max(0, cy - 1):min(out_h, cy + 2), max(0, cx - 10):min(out_w, cx + 11), :3] = (255, 220, 40)
                    bgra[max(0, cy - 10):min(out_h, cy + 11), max(0, cx - 1):min(out_w, cx + 2), :3] = (255, 220, 40)
            rgba = np.ascontiguousarray(bgra[:, :, [2, 1, 0, 3]])

        out_fmt = "png" if str(fmt).lower() == "png" else "jpeg"
        mime = "image/png" if out_fmt == "png" else "image/jpeg"
        if out_fmt != "png" and self._turbo.available:
            try:
                raw = self._turbo.compress(rgba, pixel_format=TurboJpegEncoder.TJPF_RGBX, quality=quality)
            except Exception:
                raw = self._encode_imageio(rgba, fmt=out_fmt, quality=quality)
        else:
            raw = self._encode_imageio(rgba, fmt=out_fmt, quality=quality)

        enc_ms = (time.perf_counter() - t0) * 1000.0
        b64 = base64.b64encode(raw).decode("ascii")
        return {
            "data_b64": b64,
            "mime_type": mime,
            "bytes": len(raw),
            "image_w": out_w,
            "image_h": out_h,
            "origin_x": frame.origin_x,
            "origin_y": frame.origin_y,
            "logical_w": frame.logical_width,
            "logical_h": frame.logical_height,
            "capture_ms": round(frame.capture_ms, 2),
            "encode_ms": round(enc_ms, 2),
            "total_ms": round(frame.capture_ms + enc_ms, 2),
        }


# ---------------------------------------------------------------------------
# MacA11yTree (Native ApplicationServices AXUIElement walker)
# ---------------------------------------------------------------------------

class MacA11yTree:
    """Native macOS AXUIElement accessibility tree with stable [ref] IDs and incremental AX_DIFF."""

    _VIRTUALIZED_ROLES = {"AXTable", "AXOutline", "AXList", "AXScrollArea"}
    _CHROMIUM_APPS = ("chrome", "chromium", "brave", "edge", "arc", "vivaldi", "opera", "electron", "code", "slack", "discord")
    _SAFE_FALLBACK_ACTIONS = ("AXConfirm", "AXPick", "AXOpen", "AXShowMenu")

    def __init__(self) -> None:
        self._fw = _MacFrameworks.get()
        self._next_ref = 1
        self._key_to_ref: dict[str, int] = {}
        self._ref_map: dict[int, dict[str, Any]] = {}
        self._ref_to_elem: dict[int, int] = {}
        self._last_snapshot: dict[int, dict[str, Any]] = {}
        self._last_window_key: str | None = None
        self._ax_enabled_pids: set[int] = set()
        self._ax_enhanced_pids: set[int] = set()
        atexit.register(self.close)

    def _clear_elems(self) -> None:
        for ptr in self._ref_to_elem.values():
            try:
                self._fw.cf.CFRelease(ptr)
            except Exception:
                pass
        self._ref_to_elem.clear()

    def close(self) -> None:
        self._clear_elems()

    @property
    def available(self) -> bool:
        return self._fw.accessibility_granted()

    def get_ref(self, ref_id: int) -> dict[str, Any] | None:
        return self._ref_map.get(int(ref_id))

    def find_by_text(self, query: str) -> dict[str, Any] | None:
        q = query.strip().lower()
        if not q:
            return None
        for e in self._ref_map.values():
            if e["name"].lower() == q:
                return e
        for e in self._ref_map.values():
            if q in e["name"].lower() or q in e.get("value", "").lower():
                return e
        return None

    def do_action(self, ref_id: int, action_name: str | None = None) -> dict[str, Any]:
        rid = int(ref_id)
        elem = self._ref_to_elem.get(rid)
        if not elem:
            raise ValueError(f"Unknown or stale ref [{rid}]. Call desktop_observe first.")
        fw = self._fw
        act = action_name or "AXPress"
        if fw.ax_perform(elem, act):
            return {"ok": True, "ref": rid, "action": act}
        # Only fall back to a safe allowlist when caller didn't explicitly request a specific action
        if action_name is None:
            names_arr = ctypes.c_void_p(0)
            if int(fw.ax.AXUIElementCopyActionNames(elem, ctypes.byref(names_arr))) == kAXErrorSuccess and names_arr.value:
                try:
                    avail = [
                        fw.cf_to_str(fw.cf.CFArrayGetValueAtIndex(names_arr.value, i))
                        for i in range(int(fw.cf.CFArrayGetCount(names_arr.value)))
                    ]
                    for cand in self._SAFE_FALLBACK_ACTIONS:
                        if cand in avail and fw.ax_perform(elem, cand):
                            return {"ok": True, "ref": rid, "action": cand}
                finally:
                    fw.cf.CFRelease(names_arr.value)
        raise RuntimeError(f"AX action {act!r} failed on ref [{rid}]")

    def _copy_children(self, elem: int, raw_role: str) -> int | None:
        """Return retained non-empty CFArray of children, falling back from empty AXVisibleChildren to AXChildren."""
        fw = self._fw
        if raw_role in self._VIRTUALIZED_ROLES:
            vis = fw.ax_copy_attr(elem, "AXVisibleChildren")
            if vis:
                if int(fw.cf.CFGetTypeID(vis)) == fw.tid_array and int(fw.cf.CFArrayGetCount(vis)) > 0:
                    return vis
                fw.cf.CFRelease(vis)
        ch = fw.ax_copy_attr(elem, "AXChildren")
        if ch:
            if int(fw.cf.CFGetTypeID(ch)) == fw.tid_array and int(fw.cf.CFArrayGetCount(ch)) > 0:
                return ch
            fw.cf.CFRelease(ch)
        return None

    def snapshot(
        self,
        *,
        app_hint: str | None = None,
        title_hint: str | None = None,
        pid_hint: int | None = None,
        window_id: str = "",
        window_rect: tuple[int, int, int, int] | None = None,
        max_elements: int = 120,
        max_depth: int = 18,
        find: str | None = None,
        diff: str | bool = "auto",
    ) -> dict[str, Any]:
        t0 = time.perf_counter()
        fw = self._fw
        if not fw.accessibility_granted():
            return {
                "available": False,
                "elements": [],
                "text_lines": [],
                "formatted": "(Accessibility permission required: System Settings -> Privacy & Security -> Accessibility)",
                "is_diff": False,
                "elapsed_ms": round((time.perf_counter() - t0) * 1000.0, 2),
            }

        pid = pid_hint or fw.frontmost_pid()
        if not pid:
            return {
                "available": True,
                "elements": [],
                "text_lines": [],
                "formatted": "(no active application)",
                "is_diff": False,
                "elapsed_ms": round((time.perf_counter() - t0) * 1000.0, 2),
            }

        win_key = f"{pid}:{window_id or app_hint or ''}"
        if win_key != self._last_window_key:
            self._key_to_ref.clear()
            self._last_snapshot.clear()
            self._last_window_key = win_key
        self._clear_elems()

        app_el = fw.ax.AXUIElementCreateApplication(int(pid))
        if not app_el:
            return {"available": True, "elements": [], "text_lines": [], "formatted": "(no AX app)", "is_diff": False, "elapsed_ms": 0.0}
        fw.ax.AXUIElementSetMessagingTimeout(app_el, 0.35)

        # Unlock Electron/Chromium accessibility trees once per PID
        if int(pid) not in self._ax_enabled_pids:
            self._ax_enabled_pids.add(int(pid))
            fw.ax_set_bool(app_el, "AXManualAccessibility", True)

        root_el: int | None = None
        try:
            for attr in ("AXFocusedWindow", "AXMainWindow"):
                root_el = fw.ax_copy_attr(app_el, attr)
                if root_el:
                    break
            if not root_el:
                wins = fw.ax_copy_attr(app_el, "AXWindows")
                if wins:
                    try:
                        if int(fw.cf.CFGetTypeID(wins)) == fw.tid_array and int(fw.cf.CFArrayGetCount(wins)) > 0:
                            w0 = fw.cf.CFArrayGetValueAtIndex(wins, 0)
                            if w0:
                                fw.cf.CFRetain(w0)
                                root_el = int(w0)
                    finally:
                        fw.cf.CFRelease(wins)
            if not root_el:
                fw.cf.CFRetain(app_el)
                root_el = int(app_el)

            elements: list[dict[str, Any]] = []
            new_meta: dict[int, dict[str, Any]] = {}
            seen_skeys: dict[str, int] = {}
            find_l = find.lower() if find else None
            visited = 0
            max_visit = 1400
            deadline = time.monotonic() + 0.35

            def walk(elem: int, depth: int, path: str) -> None:
                nonlocal visited
                if depth > max_depth or len(elements) >= max_elements or visited >= max_visit or time.monotonic() > deadline:
                    return
                visited += 1

                raw_role = fw.ax_attr_str(elem, "AXRole")
                role = normalize_mac_role(raw_role)
                if role == "text" and fw.ax_attr_str(elem, "AXSubrole") == "AXSearchField":
                    role = "search"

                # Gate geometry + label Mach IPC calls behind leaf/interactive roles to skip layout containers fast
                if depth > 0 and (role in MAC_INTERACTIVE_ROLES or role == "label"):
                    sz = fw.ax_attr_size(elem, "AXSize")
                    w_px = int(round(sz[0])) if sz else 0
                    h_px = int(round(sz[1])) if sz else 0
                    if w_px >= 2 and h_px >= 2:
                        title = fw.ax_attr_str(elem, "AXTitle")
                        desc = fw.ax_attr_str(elem, "AXDescription") if not title else ""
                        val = fw.ax_attr_str(elem, "AXValue") if role in ("text", "search", "combobox", "checkbox", "radio", "slider", "label") else ""
                        name = title or desc or (val if role == "label" else "")
                        if not name and role in ("text", "search", "combobox", "button", "toggle"):
                            name = fw.ax_attr_str(elem, "AXPlaceholderValue") or fw.ax_attr_str(elem, "AXHelp")
                        if not name and role in ("text", "search", "combobox"):
                            t_el = fw.ax_copy_attr(elem, "AXTitleUIElement")
                            if t_el:
                                try:
                                    name = fw.ax_attr_str(t_el, "AXValue") or fw.ax_attr_str(t_el, "AXTitle")
                                finally:
                                    fw.cf.CFRelease(t_el)
                        name = name[:120]
                        if role in MAC_INTERACTIVE_ROLES or bool(name):
                            if find_l is None or (find_l in name.lower() or find_l in role or find_l in val.lower()):
                                pos = fw.ax_attr_point(elem, "AXPosition")
                                x_px = int(round(pos[0])) if pos else 0
                                y_px = int(round(pos[1])) if pos else 0
                                base_key = f"{win_key}|{path}|{role}|{name[:48]}"
                                occ = seen_skeys.get(base_key, 0)
                                seen_skeys[base_key] = occ + 1
                                skey = f"{base_key}#{occ}"
                                rid = self._key_to_ref.get(skey)
                                if rid is None:
                                    rid = self._next_ref
                                    self._next_ref += 1
                                    self._key_to_ref[skey] = rid
                                states: list[str] = []
                                if fw.ax_attr_str(elem, "AXFocused") == "true":
                                    states.append("focused")
                                if fw.ax_attr_str(elem, "AXSelected") == "true" or (role == "checkbox" and val == "1"):
                                    states.append("selected")
                                if fw.ax_attr_str(elem, "AXEnabled") == "false":
                                    states.append("disabled")
                                rec = {
                                    "ref": rid,
                                    "role": role,
                                    "name": name,
                                    "value": val[:80] if (val and val != name) else "",
                                    "states": states,
                                    "bbox": [x_px, y_px, w_px, h_px],
                                    "center": [x_px + w_px // 2, y_px + h_px // 2],
                                }
                                elements.append(rec)
                                new_meta[rid] = rec
                                old_ptr = self._ref_to_elem.get(rid)
                                if old_ptr:
                                    fw.cf.CFRelease(old_ptr)
                                fw.cf.CFRetain(elem)
                                self._ref_to_elem[rid] = elem

                children = self._copy_children(elem, raw_role)
                if not children:
                    return
                try:
                    n_ch = min(int(fw.cf.CFArrayGetCount(children)), 64)
                    for idx in range(n_ch):
                        if len(elements) >= max_elements or visited >= max_visit or time.monotonic() > deadline:
                            break
                        ch = fw.cf.CFArrayGetValueAtIndex(children, idx)
                        if ch:
                            walk(int(ch), depth + 1, f"{path}/{idx}")
                finally:
                    fw.cf.CFRelease(children)

            try:
                walk(root_el, 0, "0")
                # If a Chromium/Electron browser still had < 5 elements, enable AXEnhancedUserInterface once and retry
                app_l = (app_hint or "").lower()
                if len(elements) < 5 and int(pid) not in self._ax_enhanced_pids and any(k in app_l for k in self._CHROMIUM_APPS):
                    self._ax_enhanced_pids.add(int(pid))
                    fw.ax_set_bool(app_el, "AXEnhancedUserInterface", True)
            finally:
                fw.cf.CFRelease(root_el)
        finally:
            fw.cf.CFRelease(app_el)

        self._ref_map = new_meta
        want_diff = (diff is True) or (diff == "auto" and bool(self._last_snapshot) and find is None)
        is_diff = False
        if want_diff and self._last_snapshot:
            added = [e for rid, e in new_meta.items() if rid not in self._last_snapshot]
            removed = [rid for rid in self._last_snapshot if rid not in new_meta]
            changed = [
                e for rid, e in new_meta.items()
                if rid in self._last_snapshot and (
                    e["name"] != self._last_snapshot[rid]["name"]
                    or e["value"] != self._last_snapshot[rid]["value"]
                    or e["states"] != self._last_snapshot[rid]["states"]
                )
            ]
            if len(added) + len(removed) + len(changed) < max(12, len(new_meta) // 2):
                is_diff = True
                lines = []
                if added:
                    lines.append(f"+ ADDED ({len(added)}):")
                    for e in added[:40]:
                        lines.append("  + " + self._format_elem(e))
                if changed:
                    lines.append(f"~ CHANGED ({len(changed)}):")
                    for e in changed[:40]:
                        lines.append("  ~ " + self._format_elem(e))
                if removed:
                    lines.append(f"- REMOVED ({len(removed)}): " + ", ".join(f"[{r}]" for r in removed[:30]))
                formatted = "\n".join(lines) if lines else "(no AX changes; prior [ref] IDs still valid)"
            else:
                formatted = "\n".join(self._format_elem(e) for e in elements) or "(no accessible elements)"
        else:
            formatted = "\n".join(self._format_elem(e) for e in elements) or "(no accessible elements)"

        if find is None:
            self._last_snapshot = dict(new_meta)

        return {
            "available": True,
            "elements": elements,
            "text_lines": [],
            "formatted": formatted,
            "is_diff": is_diff,
            "elapsed_ms": round((time.perf_counter() - t0) * 1000.0, 2),
        }

    @staticmethod
    def _format_elem(e: dict[str, Any]) -> str:
        s = f"[{e['ref']}] {e['role']}"
        if e["name"]:
            s += f" {e['name']!r}"
        if e.get("value"):
            s += f" val={e['value']!r}"
        if e.get("states"):
            s += " [" + ",".join(e["states"]) + "]"
        cx, cy = e["center"]
        s += f" @({cx},{cy})"
        return s


# ---------------------------------------------------------------------------
# MacBackgroundSession (Zero-Touch Occluded Capture + AXPress / CGEventPostToPid)
# ---------------------------------------------------------------------------

class MacBackgroundSession:
    """Zero-touch macOS background automation (occluded window capture + AXPress / CGEventPostToPid).

    Captures an attached window even while covered by other windows, triggers native AppKit/SwiftUI
    controls via AXUIElementCopyElementAtPosition + AXPress, and delivers mouse/scroll/keyboard
    events directly to the target PID via CGEventPostToPid without moving the user's cursor or
    stealing focus.
    """

    def __init__(self, win_mgr: MacWindowManager, capture: MacScreenCapture, inp: MacInputDriver) -> None:
        self._fw = _MacFrameworks.get()
        self._wm = win_mgr
        self._capture = capture
        self._inp = inp
        self._wid: int | None = None
        self._pid: int | None = None
        self._title: str = ""
        self._app: str = ""

    @property
    def active(self) -> bool:
        return self._wid is not None and self._pid is not None

    def window_rect(self) -> tuple[int, int, int, int]:
        if self._wid is None:
            return 0, 0, 1440, 900
        w = self._wm.window_info_by_id(self._wid)
        if w:
            return int(w["x"]), int(w["y"]), max(1, int(w["w"])), max(1, int(w["h"]))
        return 0, 0, 1440, 900

    def status(self) -> dict[str, Any]:
        rect = self.window_rect() if self.active else None
        return {
            "active": self.active,
            "backend": "macos-cgwindow+postpid+ax",
            "window_id": str(self._wid) if self._wid is not None else None,
            "pid": self._pid,
            "app": self._app or None,
            "title": self._title or None,
            "rect": list(rect) if rect else None,
        }

    def _check_not_minimized(self, pid: int, title: str, rect: tuple[int, int, int, int]) -> None:
        ax_win = self._wm._ax_find_window(pid, title, rect)
        if ax_win:
            try:
                if self._fw.ax_attr_str(ax_win, "AXMinimized") == "true":
                    raise RuntimeError("Attached macOS background window is minimized; unminimize it first (it can stay behind other windows).")
            finally:
                self._fw.cf.CFRelease(ax_win)

    def attach(
        self,
        *,
        window_id: str | None = None,
        app: str | None = None,
        title: str | None = None,
    ) -> dict[str, Any]:
        w = self._wm.find_window(window_id=window_id, app=app, title=title)
        if w is None:
            raise RuntimeError(f"Background target window not found (id={window_id}, app={app}, title={title})")
        self._check_not_minimized(int(w["pid"]), w["title"], (w["x"], w["y"], w["w"], w["h"]))
        self._wid = int(w["id"])
        self._pid = int(w["pid"])
        self._app = w["app"]
        self._title = w["title"]
        return self.status()

    def stop(self) -> dict[str, Any]:
        self._wid = None
        self._pid = None
        self._app = ""
        self._title = ""
        return self.status()

    def _require(self) -> tuple[int, int]:
        if self._wid is None or self._pid is None:
            raise RuntimeError("No macOS background window attached. Call desktop_background(action='attach', app=...) first.")
        info = self._wm.window_info_by_id(self._wid)
        if info is None:
            self.stop()
            raise RuntimeError("Attached macOS background window no longer exists.")
        self._check_not_minimized(self._pid, info["title"], (info["x"], info["y"], info["w"], info["h"]))
        return self._wid, self._pid

    def capture(self, area: tuple[int, int, int, int] | None = None) -> Frame:
        wid, _ = self._require()
        rect = self.window_rect()
        frame = self._capture.capture_window_bg(wid, expected_rect=rect)
        if area is not None:
            sc = frame.scale if frame.scale > 0 else 1.0
            ax, ay, aw, ah = area
            x0 = max(0, min(frame.width - 1, int(round((ax - frame.origin_x) * sc))))
            y0 = max(0, min(frame.height - 1, int(round((ay - frame.origin_y) * sc))))
            x1 = max(x0 + 1, min(frame.width, int(round((ax + aw - frame.origin_x) * sc))))
            y1 = max(y0 + 1, min(frame.height, int(round((ay + ah - frame.origin_y) * sc))))
            sub = frame.rgba[y0:y1, x0:x1, :].copy()
            return Frame(
                rgba=sub,
                origin_x=frame.origin_x + int(round(x0 / sc)),
                origin_y=frame.origin_y + int(round(y0 / sc)),
                width=x1 - x0,
                height=y1 - y0,
                scale=frame.scale,
                backend=frame.backend,
                capture_ms=frame.capture_ms,
            )
        return frame

    def _tag_bg_mouse(self, ev: int | None, wid: int) -> None:
        if not ev:
            return
        self._fw.cg.CGEventSetFlags(ev, 0)
        self._fw.cg.CGEventSetIntegerValueField(ev, kCGMouseEventWindowUnderMousePointer, int(wid))
        self._fw.cg.CGEventSetIntegerValueField(ev, kCGMouseEventWindowUnderMousePointerThatCanHandleThisEvent, int(wid))

    def move(self, x: int, y: int) -> tuple[int, int]:
        wid, pid = self._require()
        ev = self._fw.cg.CGEventCreateMouseEvent(
            self._inp._source, kCGEventMouseMoved, CGPoint(float(x), float(y)), kCGMouseButtonLeft,
        )
        self._tag_bg_mouse(ev, wid)
        self._inp._post(ev, pid=pid)
        return int(x), int(y)

    def _try_ax_press_at(self, pid: int, x: int, y: int) -> bool:
        fw = self._fw
        if not fw.accessibility_granted():
            return False
        app_el = fw.ax.AXUIElementCreateApplication(int(pid))
        if not app_el:
            return False
        fw.ax.AXUIElementSetMessagingTimeout(app_el, 0.25)
        try:
            el_out = ctypes.c_void_p(0)
            if int(fw.ax.AXUIElementCopyElementAtPosition(app_el, float(x), float(y), ctypes.byref(el_out))) == kAXErrorSuccess and el_out.value:
                el = int(el_out.value)
                try:
                    if fw.ax_perform(el, "AXPress"):
                        return True
                    # Text inputs / search boxes focus via AXFocused rather than AXPress
                    role = fw.ax_attr_str(el, "AXRole")
                    if role in ("AXTextField", "AXTextArea", "AXSearchField", "AXComboBox"):
                        return fw.ax_set_bool(el, "AXFocused", True)
                    return False
                finally:
                    fw.cf.CFRelease(el)
            return False
        finally:
            fw.cf.CFRelease(app_el)

    def click(self, x: int, y: int, *, button: str = "left", count: int = 1) -> tuple[int, int]:
        wid, pid = self._require()
        # For single left click on native AppKit/SwiftUI controls, AXPress/AXFocused works even fully occluded
        if button == "left" and count == 1 and self._try_ax_press_at(pid, x, y):
            return int(x), int(y)
        btn_map = {
            "left": (kCGEventLeftMouseDown, kCGEventLeftMouseUp, kCGMouseButtonLeft),
            "right": (kCGEventRightMouseDown, kCGEventRightMouseUp, kCGMouseButtonRight),
            "middle": (kCGEventOtherMouseDown, kCGEventOtherMouseUp, kCGMouseButtonCenter),
        }
        down_t, up_t, btn_id = btn_map.get(button, btn_map["left"])
        pt = CGPoint(float(x), float(y))
        for click_num in range(1, max(1, int(count)) + 1):
            ev_d = self._fw.cg.CGEventCreateMouseEvent(self._inp._source, down_t, pt, btn_id)
            if ev_d:
                self._tag_bg_mouse(ev_d, wid)
                self._fw.cg.CGEventSetIntegerValueField(ev_d, kCGMouseEventClickState, click_num)
            self._inp._post(ev_d, pid=pid)
            time.sleep(0.008)
            ev_u = self._fw.cg.CGEventCreateMouseEvent(self._inp._source, up_t, pt, btn_id)
            if ev_u:
                self._tag_bg_mouse(ev_u, wid)
                self._fw.cg.CGEventSetIntegerValueField(ev_u, kCGMouseEventClickState, click_num)
            self._inp._post(ev_u, pid=pid)
            if click_num < count:
                time.sleep(0.035)
        return int(x), int(y)

    def drag(
        self,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        *,
        button: str = "left",
        duration_ms: int = 120,
        steps: int = 10,
    ) -> tuple[tuple[int, int], tuple[int, int]]:
        wid, pid = self._require()
        btn_map = {
            "left": (kCGEventLeftMouseDown, kCGEventLeftMouseDragged, kCGEventLeftMouseUp, kCGMouseButtonLeft),
            "right": (kCGEventRightMouseDown, kCGEventRightMouseDragged, kCGEventRightMouseUp, kCGMouseButtonRight),
            "middle": (kCGEventOtherMouseDown, kCGEventOtherMouseDragged, kCGEventOtherMouseUp, kCGMouseButtonCenter),
        }
        down_t, drag_t, up_t, btn_id = btn_map.get(button, btn_map["left"])
        ev_d = self._fw.cg.CGEventCreateMouseEvent(self._inp._source, down_t, CGPoint(float(x1), float(y1)), btn_id)
        if ev_d:
            self._tag_bg_mouse(ev_d, wid)
            self._fw.cg.CGEventSetIntegerValueField(ev_d, kCGMouseEventClickState, 1)
        self._inp._post(ev_d, pid=pid)
        time.sleep(0.012)
        try:
            n = max(2, int(steps))
            dt = max(0.004, (duration_ms / 1000.0) / n)
            for i in range(1, n + 1):
                t = i / n
                ix = x1 + (x2 - x1) * t
                iy = y1 + (y2 - y1) * t
                ev_m = self._fw.cg.CGEventCreateMouseEvent(self._inp._source, drag_t, CGPoint(ix, iy), btn_id)
                self._tag_bg_mouse(ev_m, wid)
                self._inp._post(ev_m, pid=pid)
                time.sleep(dt)
        finally:
            ev_u = self._fw.cg.CGEventCreateMouseEvent(self._inp._source, up_t, CGPoint(float(x2), float(y2)), btn_id)
            if ev_u:
                self._tag_bg_mouse(ev_u, wid)
                self._fw.cg.CGEventSetIntegerValueField(ev_u, kCGMouseEventClickState, 1)
            self._inp._post(ev_u, pid=pid)
        return (int(x1), int(y1)), (int(x2), int(y2))

    def scroll(
        self,
        direction: str = "down",
        clicks: int = 5,
        *,
        x: int | None = None,
        y: int | None = None,
    ) -> None:
        wid, pid = self._require()
        if x is not None and y is not None:
            self.move(x, y)
        n = max(1, int(clicks))
        dy = n if direction == "up" else (-n if direction == "down" else 0)
        dx = n if direction == "left" else (-n if direction == "right" else 0)
        if self._fw.has_scroll2:
            wheel_cnt = 1 if dx == 0 else 2
            ev = self._fw.cg.CGEventCreateScrollWheelEvent2(
                self._inp._source, kCGScrollEventUnitLine, wheel_cnt, dy, dx, 0,
            )
            if ev:
                self._tag_bg_mouse(ev, wid)
                if x is not None and y is not None:
                    self._fw.cg.CGEventSetLocation(ev, CGPoint(float(x), float(y)))
            self._inp._post(ev, pid=pid)

    def type_text(self, text: str) -> None:
        _, pid = self._require()
        self._inp.type_keys(text, pid=pid)

    def hotkey(self, keys: str | list[str], *, count: int = 1) -> None:
        _, pid = self._require()
        self._inp.hotkey(keys, count=count, pid=pid)
