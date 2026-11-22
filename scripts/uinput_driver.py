"""Direct Linux kernel /dev/uinput absolute pointer & keyboard driver with layout detection and CLI fallbacks.

Features:
- Persistent QEMU usb-tablet style absolute pointer (EV_ABS 0..65535 without BTN_TOUCH/INPUT_PROP_POINTER)
  mapped across full multi-monitor virtual desktop bounds (including negative monitor offsets).
- 1-unit EV_ABS deduplication guard so repeated targets after physical mouse movement never get dropped by the kernel.
- Automatic keyboard layout detection (US QWERTY, UK GB QWERTY, or international/dead-key fallback to atomic paste).
"""

from __future__ import annotations

import atexit
import errno
try:
    import fcntl
except ImportError:
    fcntl = None  # type: ignore[assignment]
import math
import os
import re
import shutil
import struct
import subprocess
import threading
import time
from typing import Callable

# Linux input event constants (linux/input-event-codes.h)
EV_SYN = 0x00
EV_KEY = 0x01
EV_REL = 0x02
EV_ABS = 0x03
SYN_REPORT = 0x00

REL_HWHEEL = 0x06
REL_WHEEL = 0x08
ABS_X = 0x00
ABS_Y = 0x01

BTN_LEFT = 0x110
BTN_RIGHT = 0x111
BTN_MIDDLE = 0x112

BUS_USB = 0x03

# ioctl numbers on x86_64/arm64 Linux (linux/uinput.h)
UI_SET_EVBIT = 0x40045564
UI_SET_KEYBIT = 0x40045565
UI_SET_RELBIT = 0x40045566
UI_SET_ABSBIT = 0x40045567
UI_DEV_SETUP = 0x405C5503
UI_ABS_SETUP = 0x401C5504
UI_DEV_CREATE = 0x5501
UI_DEV_DESTROY = 0x5502

ABS_MAX = 65535

# Standard Linux keycodes (linux/input-event-codes.h)
KEY_CODES: dict[str, int] = {
    "esc": 1, "escape": 1,
    "1": 2, "2": 3, "3": 4, "4": 5, "5": 6, "6": 7, "7": 8, "8": 9, "9": 10, "0": 11,
    "-": 12, "minus": 12, "=": 13, "equal": 13, "backspace": 14, "bs": 14,
    "tab": 15,
    "q": 16, "w": 17, "e": 18, "r": 19, "t": 20, "y": 21, "u": 22, "i": 23, "o": 24, "p": 25,
    "[": 26, "leftbrace": 26, "]": 27, "rightbrace": 27,
    "enter": 28, "return": 28,
    "ctrl": 29, "control": 29, "lctrl": 29, "leftctrl": 29,
    "a": 30, "s": 31, "d": 32, "f": 33, "g": 34, "h": 35, "j": 36, "k": 37, "l": 38,
    ";": 39, "semicolon": 39, "'": 40, "apostrophe": 40, "`": 41, "grave": 41,
    "shift": 42, "lshift": 42, "leftshift": 42,
    "\\": 43, "backslash": 43,
    "z": 44, "x": 45, "c": 46, "v": 47, "b": 48, "n": 49, "m": 50,
    ",": 51, "comma": 51, ".": 52, "dot": 52, "period": 52, "/": 53, "slash": 53,
    "rshift": 54, "rightshift": 54,
    "alt": 56, "lalt": 56, "leftalt": 56,
    "space": 57, " ": 57,
    "capslock": 58,
    "f1": 59, "f2": 60, "f3": 61, "f4": 62, "f5": 63, "f6": 64,
    "f7": 65, "f8": 66, "f9": 67, "f10": 68, "f11": 87, "f12": 88,
    "102nd": 86,
    "rctrl": 97, "rightctrl": 97,
    "ralt": 100, "rightalt": 100, "altgr": 100,
    "home": 102, "up": 103, "pageup": 104, "pgup": 104,
    "left": 105, "right": 106, "end": 107, "down": 108,
    "pagedown": 109, "pgdn": 109, "insert": 110, "delete": 111, "del": 111,
    "print": 99, "printscreen": 99, "pause": 119,
    "super": 125, "meta": 125, "win": 125, "cmd": 125, "lmeta": 125, "leftmeta": 125,
    "rmeta": 126, "rightmeta": 126, "menu": 139,
    "volumedown": 114, "volumeup": 115, "mute": 113,
    "playpause": 164, "nextsong": 163, "previoussong": 165,
}

# Shifted US-QWERTY character -> base key name
SHIFTED_CHARS_US: dict[str, str] = {
    "!": "1", "@": "2", "#": "3", "$": "4", "%": "5", "^": "6", "&": "7", "*": "8", "(": "9", ")": "0",
    "_": "-", "+": "=", "{": "[", "}": "]", "|": "\\", ":": ";", '"': "'", "~": "`", "<": ",", ">": ".", "?": "/",
}
SHIFTED_CHARS = SHIFTED_CHARS_US

# UK (gb) ISO-105 QWERTY overrides vs US
UK_OVERRIDES: dict[str, tuple[bool, int]] = {
    '"': (True, KEY_CODES["2"]),
    "\u00a3": (True, KEY_CODES["3"]),  # £
    "\u00ac": (True, KEY_CODES["`"]),  # ¬
    "@": (True, KEY_CODES["'"]),
    "#": (False, KEY_CODES["\\"]),
    "~": (True, KEY_CODES["\\"]),
    "\\": (False, KEY_CODES["102nd"]),
    "|": (True, KEY_CODES["102nd"]),
}

# Complete Linux name -> X11 keysym mapping for xdotool fallbacks
X11_KEYSYMS: dict[str, str] = {
    "ctrl": "ctrl", "control": "ctrl", "lctrl": "Control_L", "leftctrl": "Control_L", "rctrl": "Control_R", "rightctrl": "Control_R",
    "shift": "shift", "lshift": "Shift_L", "leftshift": "Shift_L", "rshift": "Shift_R", "rightshift": "Shift_R",
    "alt": "alt", "lalt": "Alt_L", "leftalt": "Alt_L", "ralt": "ISO_Level3_Shift", "rightalt": "ISO_Level3_Shift", "altgr": "ISO_Level3_Shift",
    "super": "super", "meta": "super", "win": "super", "cmd": "super", "lmeta": "Super_L", "leftmeta": "Super_L", "rmeta": "Super_R", "rightmeta": "Super_R",
    "enter": "Return", "return": "Return", "esc": "Escape", "escape": "Escape",
    "tab": "Tab", "backspace": "BackSpace", "bs": "BackSpace", "delete": "Delete", "del": "Delete",
    "insert": "Insert", "space": "space", " ": "space", "capslock": "Caps_Lock", "menu": "Menu", "pause": "Pause",
    "print": "Print", "printscreen": "Print",
    "up": "Up", "down": "Down", "left": "Left", "right": "Right",
    "home": "Home", "end": "End", "pageup": "Prior", "pgup": "Prior", "pagedown": "Next", "pgdn": "Next",
    "f1": "F1", "f2": "F2", "f3": "F3", "f4": "F4", "f5": "F5", "f6": "F6",
    "f7": "F7", "f8": "F8", "f9": "F9", "f10": "F10", "f11": "F11", "f12": "F12",
    "-": "minus", "minus": "minus", "=": "equal", "equal": "equal", "+": "plus",
    "[": "bracketleft", "leftbrace": "bracketleft", "]": "bracketright", "rightbrace": "bracketright",
    ";": "semicolon", "semicolon": "semicolon", "'": "apostrophe", "apostrophe": "apostrophe",
    "`": "grave", "grave": "grave", "\\": "backslash", "backslash": "backslash",
    ",": "comma", "comma": "comma", ".": "period", "dot": "period", "period": "period",
    "/": "slash", "slash": "slash",
}

BUTTON_CODES: dict[str, int] = {
    "left": BTN_LEFT,
    "right": BTN_RIGHT,
    "middle": BTN_MIDDLE,
}

XDOTOOL_BUTTONS: dict[str, str] = {"left": "1", "middle": "2", "right": "3"}

TERMINAL_CLASSES = (
    "konsole", "kitty", "alacritty", "wezterm", "foot", "ghostty",
    "gnome-terminal", "tilix", "terminator", "xfce4-terminal", "xterm", "urxvt", "st",
    "windowsterminal", "cmd", "powershell", "pwsh", "mintty",
)


def detect_keyboard_layout() -> str:
    """Detect active XKB layout ('us', 'gb', or other/deadkey code) on Linux."""
    env_override = os.environ.get("COMPUTER_USE_KEYMAP", "").strip().lower()
    if env_override:
        return env_override
    # 1. KDE Plasma kxkbrc (respect Use= and VariantList= for dead-key detection)
    kxkb = os.path.expanduser("~/.config/kxkbrc")
    if os.path.isfile(kxkb):
        try:
            use_layout = True
            layout = ""
            variant = ""
            with open(kxkb, encoding="utf-8", errors="ignore") as f:
                for line in f:
                    s = line.strip()
                    if s.startswith("Use="):
                        use_layout = s.split("=", 1)[1].strip().lower() != "false"
                    elif s.startswith("LayoutList="):
                        layout = s.split("=", 1)[1].strip().split(",")[0].strip().lower()
                    elif s.startswith("VariantList="):
                        variant = s.split("=", 1)[1].strip().split(",")[0].strip().lower()
            if use_layout and layout:
                if any(k in variant for k in ("intl", "dead", "extd", "nodeadkeys")):
                    return f"{layout}-{variant}"
                return layout
        except Exception:
            pass
    # 2. GNOME input-sources via gsettings
    if os.environ.get("XDG_CURRENT_DESKTOP", "").lower().find("gnome") >= 0 and shutil.which("gsettings"):
        try:
            r = subprocess.run(
                ["gsettings", "get", "org.gnome.desktop.input-sources", "mru-sources"],
                capture_output=True, text=True, timeout=1.5,
            )
            m = re.search(r"'xkb',\s*'([^']+)'", r.stdout)
            if m:
                return m.group(1).strip().lower()
        except Exception:
            pass
    # 3. localectl status
    if shutil.which("localectl"):
        try:
            r = subprocess.run(["localectl", "status"], capture_output=True, text=True, timeout=1.5)
            m_lay = re.search(r"X11 Layout:\s*([a-zA-Z0-9_-]+)", r.stdout)
            m_var = re.search(r"X11 Variant:\s*([a-zA-Z0-9_-]+)", r.stdout)
            if m_lay:
                lay = m_lay.group(1).strip().lower()
                var = m_var.group(1).strip().lower() if m_var else ""
                if var and any(k in var for k in ("intl", "dead", "extd")):
                    return f"{lay}-{var}"
                return lay
        except Exception:
            pass
    # 4. setxkbmap
    if os.environ.get("DISPLAY") and shutil.which("setxkbmap"):
        try:
            r = subprocess.run(["setxkbmap", "-query"], capture_output=True, text=True, timeout=1.5)
            m = re.search(r"layout:\s*([a-zA-Z0-9_-]+)", r.stdout)
            if m:
                return m.group(1).strip().lower()
        except Exception:
            pass
    return "us"


def _split_combo_tokens(keys: str | list[str]) -> list[str]:
    """Split 'ctrl+shift++' -> ['ctrl', 'shift', '+'] without dropping trailing '+'."""
    if not isinstance(keys, str):
        return [str(p).strip() for p in keys if str(p).strip()]
    s = keys.strip()
    if not s:
        return []
    if s == "+":
        return ["+"]
    tokens: list[str] = []
    cur: list[str] = []
    i = 0
    while i < len(s):
        ch = s[i]
        if ch == "+":
            if cur:
                tok = "".join(cur).strip()
                if tok:
                    tokens.append(tok)
                cur = []
                # Handle trailing '++' (e.g. 'ctrl++' -> ['ctrl', '+'])
                if i + 1 < len(s) and s[i + 1] == "+":
                    tokens.append("+")
                    i += 2
                    continue
            else:
                tokens.append("+")
        else:
            cur.append(ch)
        i += 1
    if cur:
        tok = "".join(cur).strip()
        if tok:
            tokens.append(tok)
    return tokens


def char_to_uinput(ch: str, layout: str = "us") -> tuple[bool, int] | None:
    """Map a character to (needs_shift, linux_keycode) for the active keyboard layout, or None."""
    if ch == "\n" or ch == "\r":
        return False, KEY_CODES["enter"]
    if ch == "\t":
        return False, KEY_CODES["tab"]
    if ch == " ":
        return False, KEY_CODES["space"]

    lay = layout.lower()
    if lay in ("gb", "uk"):
        if ch in UK_OVERRIDES:
            return UK_OVERRIDES[ch]
        lay = "us"

    if lay != "us":
        return None

    if "a" <= ch <= "z":
        return False, KEY_CODES[ch]
    if "A" <= ch <= "Z":
        return True, KEY_CODES[ch.lower()]
    if ch in KEY_CODES and len(ch) == 1:
        return False, KEY_CODES[ch]
    if ch in SHIFTED_CHARS_US:
        return True, KEY_CODES[SHIFTED_CHARS_US[ch]]
    return None


def parse_key_combo(keys: str | list[str], layout: str = "us") -> list[tuple[str, int]]:
    """Parse 'ctrl+shift+t', 'ctrl++', or ['ctrl', 'l'] into [(name, linux_keycode), ...]."""
    raw = _split_combo_tokens(keys)
    if not raw:
        raise ValueError("Empty key specification")
    out: list[tuple[str, int]] = []
    seen_codes: set[int] = set()
    lay = layout.lower()
    for tok in raw:
        low = tok.lower()
        # Layout-aware punctuation overrides (e.g. UK '#', '~', '\', '|', '@', '"')
        if len(tok) == 1 and lay in ("gb", "uk") and tok in UK_OVERRIDES:
            need_shift, code = UK_OVERRIDES[tok]
            if need_shift and KEY_CODES["shift"] not in seen_codes:
                out.append(("shift", KEY_CODES["shift"]))
                seen_codes.add(KEY_CODES["shift"])
            out.append((tok, code))
            seen_codes.add(code)
        elif low in KEY_CODES:
            code = KEY_CODES[low]
            if code not in seen_codes:
                out.append((low, code))
                seen_codes.add(code)
        elif tok in SHIFTED_CHARS_US:
            if KEY_CODES["shift"] not in seen_codes:
                out.append(("shift", KEY_CODES["shift"]))
                seen_codes.add(KEY_CODES["shift"])
            base = SHIFTED_CHARS_US[tok]
            out.append((base, KEY_CODES[base]))
            seen_codes.add(KEY_CODES[base])
        else:
            raise ValueError(f"Unknown key name: {tok!r}")
    return out


def to_x11_combo(keys: str | list[str]) -> str:
    """Convert a key combo string/list into an xdotool-compatible X11 keysym combination."""
    raw = _split_combo_tokens(keys)
    if not raw:
        raise ValueError("Empty key specification")
    return "+".join(X11_KEYSYMS.get(tok.lower(), tok) for tok in raw)


def _pack_events(events: list[tuple[int, int, int]]) -> bytes:
    # Kernel uinput_inject_events overwrites timestamps on write; zero unsigned longs avoid Y2038 32-bit overflow
    return b"".join(struct.pack("LLHHi", 0, 0, etype, code, val) for etype, code, val in events)


def _write_all(fd: int, data: bytes) -> None:
    mv = memoryview(data)
    written = 0
    total = len(data)
    while written < total:
        try:
            n = os.write(fd, mv[written:])
            if n <= 0:
                raise OSError("Short zero write to /dev/uinput")
            written += n
        except OSError as exc:
            if exc.errno in (errno.EAGAIN, errno.EINTR):
                time.sleep(0.001)
                continue
            raise


class InputDriver:
    """Persistent ultra-low-latency Linux input driver (/dev/uinput with layout detection & CLI fallbacks)."""

    def __init__(
        self,
        screen_size_fn: Callable[[], tuple[int, int]],
        virtual_bounds_fn: Callable[[], tuple[int, int, int, int]] | None = None,
    ):
        self._screen_size_fn = screen_size_fn
        self._virtual_bounds_fn = virtual_bounds_fn
        self._ptr_fd: int | None = None
        self._kbd_fd: int | None = None
        self._backend: str | None = None
        self._uinput_error: str | None = None
        self._last_pos: tuple[int, int] | None = None
        self._last_abs: tuple[int, int] = (0, 0)
        self._layout: str = detect_keyboard_layout()
        self._uinput_lock = threading.Lock()
        atexit.register(self.close)

    def prewarm(self) -> None:
        """Open /dev/uinput virtual devices asynchronously at server startup to skip the 200 ms first-call settle."""
        if fcntl is None or not os.access("/dev/uinput", os.W_OK):
            return
        threading.Thread(target=self._ensure_uinput, daemon=True, name="agcu-uinput-prewarm").start()

    @property
    def layout(self) -> str:
        return self._layout

    @property
    def backend(self) -> str:
        if self._backend is not None:
            return f"{self._backend} ({self._layout})"
        if os.access("/dev/uinput", os.W_OK):
            return f"uinput-direct (ready, layout={self._layout})"
        if shutil.which("ydotool"):
            return "ydotool"
        if os.environ.get("DISPLAY") and shutil.which("xdotool"):
            return "xdotool"
        if shutil.which("wtype"):
            return "wtype (keyboard-only)"
        return "unavailable"

    def _bounds(self) -> tuple[int, int, int, int]:
        if self._virtual_bounds_fn is not None:
            return self._virtual_bounds_fn()
        sw, sh = self._screen_size_fn()
        return 0, 0, sw, sh

    def _ensure_uinput(self) -> bool:
        if fcntl is None:
            self._uinput_error = "uinput is Linux-only"
            return False
        if self._ptr_fd is not None and self._kbd_fd is not None:
            return True
        with self._uinput_lock:
            if self._ptr_fd is not None and self._kbd_fd is not None:
                return True
            if self._uinput_error is not None:
                return False
            return self._open_uinput_locked()

    def _open_uinput_locked(self) -> bool:
        ptr_fd = -1
        kbd_fd = -1
        try:
            ptr_fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
            for b in (EV_KEY, EV_ABS, EV_REL):
                fcntl.ioctl(ptr_fd, UI_SET_EVBIT, b)
            for btn in (BTN_LEFT, BTN_RIGHT, BTN_MIDDLE):
                fcntl.ioctl(ptr_fd, UI_SET_KEYBIT, btn)
            for rel in (REL_WHEEL, REL_HWHEEL):
                fcntl.ioctl(ptr_fd, UI_SET_RELBIT, rel)
            for axis in (ABS_X, ABS_Y):
                fcntl.ioctl(ptr_fd, UI_SET_ABSBIT, axis)
                fcntl.ioctl(ptr_fd, UI_ABS_SETUP, struct.pack("HHiiiiii", axis, 0, 0, 0, ABS_MAX, 0, 0, 0))
            fcntl.ioctl(
                ptr_fd,
                UI_DEV_SETUP,
                struct.pack("HHHH80sI", BUS_USB, 0x0627, 0x0001, 1, b"antigravity-cu-pointer".ljust(80, b"\x00"), 0),
            )
            fcntl.ioctl(ptr_fd, UI_DEV_CREATE)

            kbd_fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
            fcntl.ioctl(kbd_fd, UI_SET_EVBIT, EV_KEY)
            for code in range(1, 249):
                fcntl.ioctl(kbd_fd, UI_SET_KEYBIT, code)
            fcntl.ioctl(
                kbd_fd,
                UI_DEV_SETUP,
                struct.pack("HHHH80sI", BUS_USB, 0x0627, 0x0002, 1, b"antigravity-cu-keyboard".ljust(80, b"\x00"), 0),
            )
            fcntl.ioctl(kbd_fd, UI_DEV_CREATE)

            time.sleep(0.20)
            self._ptr_fd = ptr_fd
            self._kbd_fd = kbd_fd
            self._last_abs = (0, 0)
            self._backend = "uinput-direct"
            return True
        except Exception as exc:
            self._uinput_error = str(exc)
            for fd in (ptr_fd, kbd_fd):
                if fd >= 0:
                    try:
                        fcntl.ioctl(fd, UI_DEV_DESTROY)
                    except Exception:
                        pass
                    try:
                        os.close(fd)
                    except Exception:
                        pass
            return False

    def close(self) -> None:
        for attr in ("_ptr_fd", "_kbd_fd"):
            fd = getattr(self, attr, None)
            if fd is not None:
                try:
                    fcntl.ioctl(fd, UI_DEV_DESTROY)
                except Exception:
                    pass
                try:
                    os.close(fd)
                except Exception:
                    pass
                setattr(self, attr, None)

    def _screen_to_abs(self, x: int, y: int) -> tuple[int, int, int, int]:
        vx, vy, vw, vh = self._bounds()
        cx = max(vx, min(vx + vw - 1, int(round(x))))
        cy = max(vy, min(vy + vh - 1, int(round(y))))
        ax = int(round((cx - vx) * ABS_MAX / max(1, vw - 1)))
        ay = int(round((cy - vy) * ABS_MAX / max(1, vh - 1)))
        return cx, cy, ax, ay

    def _emit_ptr(self, events: list[tuple[int, int, int]]) -> None:
        assert self._ptr_fd is not None
        _write_all(self._ptr_fd, _pack_events(events + [(EV_SYN, SYN_REPORT, 0)]))

    def _emit_kbd(self, events: list[tuple[int, int, int]]) -> None:
        assert self._kbd_fd is not None
        _write_all(self._kbd_fd, _pack_events(events + [(EV_SYN, SYN_REPORT, 0)]))

    def move(self, x: int, y: int) -> tuple[int, int]:
        cx, cy, ax, ay = self._screen_to_abs(x, y)
        if self._ensure_uinput():
            # Guard against Linux kernel EV_ABS deduplication when physical mouse moved separately
            if (ax, ay) == self._last_abs:
                self._emit_ptr([(EV_ABS, ABS_X, ax ^ 1), (EV_ABS, ABS_Y, ay)])
            self._emit_ptr([(EV_ABS, ABS_X, ax), (EV_ABS, ABS_Y, ay)])
            self._last_abs = (ax, ay)
            self._last_pos = (cx, cy)
            return cx, cy
        if shutil.which("ydotool"):
            subprocess.run(["ydotool", "mousemove", "--absolute", f"-x={cx}", f"-y={cy}"], check=True, capture_output=True, timeout=2)
            self._last_pos = (cx, cy)
            return cx, cy
        if os.environ.get("DISPLAY") and shutil.which("xdotool"):
            subprocess.run(["xdotool", "mousemove", "--sync", str(cx), str(cy)], check=True, capture_output=True, timeout=2)
            self._last_pos = (cx, cy)
            return cx, cy
        raise RuntimeError(f"No pointer input backend available (uinput: {self._uinput_error})")

    def click(
        self,
        x: int | None = None,
        y: int | None = None,
        *,
        button: str = "left",
        count: int = 1,
        modifiers: list[str] | None = None,
    ) -> tuple[int, int] | None:
        btn_name = button.lower()
        if btn_name not in BUTTON_CODES:
            raise ValueError(f"Unsupported button: {button!r}")
        btn_code = BUTTON_CODES[btn_name]
        pos = None
        if x is not None and y is not None:
            pos = self.move(x, y)
            time.sleep(0.006)

        mod_codes = [KEY_CODES[m.lower()] for m in (modifiers or []) if m.lower() in KEY_CODES]
        if self._ensure_uinput():
            pressed_mods: list[int] = []
            try:
                if mod_codes:
                    self._emit_kbd([(EV_KEY, c, 1) for c in mod_codes])
                    pressed_mods = list(mod_codes)
                    time.sleep(0.005)
                for i in range(max(1, int(count))):
                    self._emit_ptr([(EV_KEY, btn_code, 1)])
                    time.sleep(0.004)
                    self._emit_ptr([(EV_KEY, btn_code, 0)])
                    if i + 1 < count:
                        time.sleep(0.030)
            finally:
                if pressed_mods:
                    time.sleep(0.005)
                    self._emit_kbd([(EV_KEY, c, 0) for c in reversed(pressed_mods)])
            return pos

        if shutil.which("ydotool"):
            code_map = {"left": "0xC0", "right": "0xC1", "middle": "0xC2"}
            for _ in range(max(1, int(count))):
                subprocess.run(["ydotool", "click", code_map[btn_name]], check=True, capture_output=True, timeout=2)
            return pos
        if os.environ.get("DISPLAY") and shutil.which("xdotool"):
            xmods = [X11_KEYSYMS.get(m.lower(), m) for m in (modifiers or [])]
            try:
                for xm in xmods:
                    subprocess.run(["xdotool", "keydown", xm], check=True, capture_output=True, timeout=2)
                subprocess.run(
                    ["xdotool", "click", "--repeat", str(max(1, int(count))), XDOTOOL_BUTTONS[btn_name]],
                    check=True, capture_output=True, timeout=2,
                )
            finally:
                for xm in reversed(xmods):
                    subprocess.run(["xdotool", "keyup", xm], capture_output=True, timeout=2)
            return pos
        raise RuntimeError(f"No click backend available (uinput: {self._uinput_error})")

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
        btn_name = button.lower()
        btn_code = BUTTON_CODES.get(btn_name, BTN_LEFT)
        start = self.move(x1, y1)
        if on_step:
            on_step(start[0], start[1])
        time.sleep(0.012)
        steps = max(2, min(60, int(steps)))
        dt = max(0.004, (duration_ms / 1000.0) / steps)
        if self._ensure_uinput():
            self._emit_ptr([(EV_KEY, btn_code, 1)])
            time.sleep(0.014)
            end = start
            try:
                for i in range(1, steps + 1):
                    t = 0.5 - 0.5 * math.cos(math.pi * i / steps)
                    ix = int(round(x1 + (x2 - x1) * t))
                    iy = int(round(y1 + (y2 - y1) * t))
                    cx, cy, ax, ay = self._screen_to_abs(ix, iy)
                    self._emit_ptr([(EV_ABS, ABS_X, ax), (EV_ABS, ABS_Y, ay)])
                    self._last_abs = (ax, ay)
                    end = (cx, cy)
                    if on_step:
                        on_step(cx, cy)
                    time.sleep(dt)
            finally:
                time.sleep(0.012)
                self._emit_ptr([(EV_KEY, btn_code, 0)])
            self._last_pos = end
            return start, end
        if os.environ.get("DISPLAY") and shutil.which("xdotool"):
            xb = XDOTOOL_BUTTONS.get(btn_name, "1")
            end_x, end_y, _, _ = self._screen_to_abs(x2, y2)
            subprocess.run(["xdotool", "mousedown", xb], check=True, capture_output=True, timeout=2)
            try:
                subprocess.run(["xdotool", "mousemove", "--sync", str(end_x), str(end_y)], check=True, capture_output=True, timeout=2)
            finally:
                subprocess.run(["xdotool", "mouseup", xb], capture_output=True, timeout=2)
            self._last_pos = (end_x, end_y)
            return start, (end_x, end_y)
        raise RuntimeError(f"Drag requires /dev/uinput or xdotool (uinput error: {self._uinput_error})")

    def scroll(
        self,
        direction: str = "down",
        clicks: int = 5,
        *,
        x: int | None = None,
        y: int | None = None,
    ) -> None:
        if x is not None and y is not None:
            self.move(x, y)
            time.sleep(0.008)
        d = direction.lower()
        n = max(1, min(100, int(clicks)))
        if self._ensure_uinput():
            if d in ("up", "down"):
                val = 1 if d == "up" else -1
                axis = REL_WHEEL
            elif d in ("left", "right"):
                val = -1 if d == "left" else 1
                axis = REL_HWHEEL
            else:
                raise ValueError(f"Invalid scroll direction: {direction!r}")
            for _ in range(n):
                self._emit_ptr([(EV_REL, axis, val)])
                time.sleep(0.005)
            return
        if os.environ.get("DISPLAY") and shutil.which("xdotool"):
            bmap = {"up": "4", "down": "5", "left": "6", "right": "7"}
            if d not in bmap:
                raise ValueError(f"Invalid scroll direction: {direction!r}")
            subprocess.run(["xdotool", "click", "--repeat", str(n), bmap[d]], check=True, capture_output=True, timeout=2)
            return
        raise RuntimeError(f"No scroll backend available (uinput: {self._uinput_error})")

    def hotkey(self, keys: str | list[str], *, count: int = 1) -> None:
        parsed = parse_key_combo(keys, layout=self._layout)
        codes = [c for _, c in parsed]
        if self._ensure_uinput():
            for i in range(max(1, int(count))):
                pressed: list[int] = []
                try:
                    for c in codes[:-1]:
                        self._emit_kbd([(EV_KEY, c, 1)])
                        pressed.append(c)
                        time.sleep(0.002)
                    self._emit_kbd([(EV_KEY, codes[-1], 1)])
                    pressed.append(codes[-1])
                    time.sleep(0.004)
                finally:
                    for c in reversed(pressed):
                        self._emit_kbd([(EV_KEY, c, 0)])
                        time.sleep(0.002)
                if i + 1 < count:
                    time.sleep(0.015)
            return
        if shutil.which("xdotool") and os.environ.get("DISPLAY"):
            combo = to_x11_combo(keys)
            for _ in range(max(1, int(count))):
                subprocess.run(["xdotool", "key", "--clearmodifiers", combo], check=True, capture_output=True, timeout=2)
            return
        raise RuntimeError(f"No keyboard backend available (uinput: {self._uinput_error})")

    def type_keys(self, text: str, *, key_delay_ms: int = 0) -> None:
        """Type text directly via kernel /dev/uinput using the detected keyboard layout."""
        if not text:
            return
        if self._ensure_uinput():
            delay = max(0.0008, key_delay_ms / 1000.0) if key_delay_ms > 0 else 0.0009
            shift = KEY_CODES["shift"]
            for ch in text:
                mapped = char_to_uinput(ch, layout=self._layout)
                if mapped is None:
                    raise ValueError(f"Character {ch!r} requires clipboard paste mode on layout {self._layout!r}")
                need_shift, code = mapped
                shift_down = False
                code_down = False
                try:
                    if need_shift:
                        self._emit_kbd([(EV_KEY, shift, 1)])
                        shift_down = True
                        time.sleep(0.0006)
                    self._emit_kbd([(EV_KEY, code, 1)])
                    code_down = True
                    time.sleep(delay)
                finally:
                    if code_down:
                        self._emit_kbd([(EV_KEY, code, 0)])
                    if shift_down:
                        time.sleep(0.0006)
                        self._emit_kbd([(EV_KEY, shift, 0)])
            return
        if shutil.which("wtype"):
            subprocess.run(["wtype", "--", text], check=True, capture_output=True, timeout=5)
            return
        if shutil.which("ydotool"):
            subprocess.run(["ydotool", "type", "--key-delay", str(max(1, key_delay_ms)), "--", text], check=True, capture_output=True, timeout=5)
            return
        if os.environ.get("DISPLAY") and shutil.which("xdotool"):
            subprocess.run(["xdotool", "type", "--delay", str(max(1, key_delay_ms)), "--", text], check=True, capture_output=True, timeout=5)
            return
        raise RuntimeError(f"No typing backend available (uinput: {self._uinput_error})")

    def is_pure_ascii_typable(self, text: str) -> bool:
        return all(char_to_uinput(ch, layout=self._layout) is not None for ch in text)
