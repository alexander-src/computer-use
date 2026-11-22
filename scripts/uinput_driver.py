"""Direct Linux kernel /dev/uinput absolute pointer & keyboard driver with CLI fallbacks.

On Linux Wayland (KDE Plasma, GNOME, Hyprland, Sway) and X11, creating a persistent
QEMU usb-tablet style uinput pointer (EV_ABS 0..65535 without BTN_TOUCH/INPUT_PROP_POINTER)
gives 1:1 pixel-exact absolute cursor positioning in < 0.05 ms with zero daemon or
subprocess overhead, while keeping the mouse cursor visible.
"""

from __future__ import annotations

import atexit
import fcntl
import math
import os
import shutil
import struct
import subprocess
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
SHIFTED_CHARS: dict[str, str] = {
    "!": "1", "@": "2", "#": "3", "$": "4", "%": "5", "^": "6", "&": "7", "*": "8", "(": "9", ")": "0",
    "_": "-", "+": "=", "{": "[", "}": "]", "|": "\\", ":": ";", '"': "'", "~": "`", "<": ",", ">": ".", "?": "/",
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
)


def parse_key_combo(keys: str | list[str]) -> list[tuple[str, int]]:
    """Parse 'ctrl+shift+t' or ['ctrl', 'l'] into [(name, linux_keycode), ...]."""
    if isinstance(keys, str):
        raw = [p.strip() for p in keys.split("+") if p.strip()]
        if keys == "+":
            raw = ["+"]
    else:
        raw = [str(p).strip() for p in keys if str(p).strip()]
    if not raw:
        raise ValueError("Empty key specification")
    out: list[tuple[str, int]] = []
    for tok in raw:
        low = tok.lower()
        if low in KEY_CODES:
            out.append((low, KEY_CODES[low]))
        elif tok in SHIFTED_CHARS:
            out.append(("shift", KEY_CODES["shift"]))
            base = SHIFTED_CHARS[tok]
            out.append((base, KEY_CODES[base]))
        else:
            raise ValueError(f"Unknown key name: {tok!r}")
    return out


def char_to_uinput(ch: str) -> tuple[bool, int] | None:
    """Map an ASCII character to (needs_shift, keycode), or None if non-ASCII/unmapped."""
    if ch == "\n" or ch == "\r":
        return False, KEY_CODES["enter"]
    if ch == "\t":
        return False, KEY_CODES["tab"]
    if ch == " ":
        return False, KEY_CODES["space"]
    if "a" <= ch <= "z":
        return False, KEY_CODES[ch]
    if "A" <= ch <= "Z":
        return True, KEY_CODES[ch.lower()]
    if ch in KEY_CODES and len(ch) == 1:
        return False, KEY_CODES[ch]
    if ch in SHIFTED_CHARS:
        return True, KEY_CODES[SHIFTED_CHARS[ch]]
    return None


def _pack_events(events: list[tuple[int, int, int]]) -> bytes:
    now = time.time()
    sec = int(now)
    usec = int((now - sec) * 1_000_000)
    return b"".join(struct.pack("llHHi", sec, usec, etype, code, val) for etype, code, val in events)


class InputDriver:
    """Persistent ultra-low-latency Linux input driver (/dev/uinput with CLI fallbacks)."""

    def __init__(self, screen_size_fn: Callable[[], tuple[int, int]]):
        self._screen_size_fn = screen_size_fn
        self._ptr_fd: int | None = None
        self._kbd_fd: int | None = None
        self._backend: str | None = None
        self._uinput_error: str | None = None
        self._last_pos: tuple[int, int] | None = None
        atexit.register(self.close)

    @property
    def backend(self) -> str:
        if self._backend is not None:
            return self._backend
        if os.access("/dev/uinput", os.W_OK):
            return "uinput-direct (ready)"
        if shutil.which("ydotool"):
            return "ydotool"
        if os.environ.get("DISPLAY") and shutil.which("xdotool"):
            return "xdotool"
        if shutil.which("wtype"):
            return "wtype (keyboard-only)"
        return "unavailable"

    def _ensure_uinput(self) -> bool:
        if self._ptr_fd is not None and self._kbd_fd is not None:
            return True
        if self._uinput_error is not None:
            return False
        ptr_fd = -1
        kbd_fd = -1
        try:
            # 1. Persistent absolute USB-tablet style pointer (exact 1:1 on Wayland + X11)
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

            # 2. Persistent virtual keyboard
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

            # Give udev/libinput one-time attach settle (only happens ONCE per server lifetime)
            time.sleep(0.22)
            self._ptr_fd = ptr_fd
            self._kbd_fd = kbd_fd
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
        sw, sh = self._screen_size_fn()
        cx = max(0, min(sw - 1, int(round(x))))
        cy = max(0, min(sh - 1, int(round(y))))
        ax = int(round(cx * ABS_MAX / max(1, sw - 1)))
        ay = int(round(cy * ABS_MAX / max(1, sh - 1)))
        return cx, cy, ax, ay

    def _emit_ptr(self, events: list[tuple[int, int, int]]) -> None:
        assert self._ptr_fd is not None
        os.write(self._ptr_fd, _pack_events(events + [(EV_SYN, SYN_REPORT, 0)]))

    def _emit_kbd(self, events: list[tuple[int, int, int]]) -> None:
        assert self._kbd_fd is not None
        os.write(self._kbd_fd, _pack_events(events + [(EV_SYN, SYN_REPORT, 0)]))

    def move(self, x: int, y: int) -> tuple[int, int]:
        cx, cy, ax, ay = self._screen_to_abs(x, y)
        if self._ensure_uinput():
            self._emit_ptr([(EV_ABS, ABS_X, ax), (EV_ABS, ABS_Y, ay)])
            self._last_pos = (cx, cy)
            return cx, cy
        if shutil.which("ydotool"):
            subprocess.run(["ydotool", "mousemove", "--absolute", "-x", str(cx), "-y", str(cy)], check=True, capture_output=True)
            self._last_pos = (cx, cy)
            return cx, cy
        if os.environ.get("DISPLAY") and shutil.which("xdotool"):
            subprocess.run(["xdotool", "mousemove", "--sync", str(cx), str(cy)], check=True, capture_output=True)
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
            time.sleep(0.012)

        mod_codes = [KEY_CODES[m.lower()] for m in (modifiers or []) if m.lower() in KEY_CODES]
        if self._ensure_uinput():
            if mod_codes:
                self._emit_kbd([(EV_KEY, c, 1) for c in mod_codes])
                time.sleep(0.008)
            try:
                for i in range(max(1, int(count))):
                    self._emit_ptr([(EV_KEY, btn_code, 1)])
                    time.sleep(0.012)
                    self._emit_ptr([(EV_KEY, btn_code, 0)])
                    if i + 1 < count:
                        time.sleep(0.045)
            finally:
                if mod_codes:
                    time.sleep(0.008)
                    self._emit_kbd([(EV_KEY, c, 0) for c in reversed(mod_codes)])
            return pos

        if shutil.which("ydotool"):
            code_map = {"left": "0xC0", "right": "0xC1", "middle": "0xC2"}
            for _ in range(max(1, int(count))):
                subprocess.run(["ydotool", "click", code_map[btn_name]], check=True, capture_output=True)
            return pos
        if os.environ.get("DISPLAY") and shutil.which("xdotool"):
            subprocess.run(
                ["xdotool", "click", "--repeat", str(max(1, int(count))), XDOTOOL_BUTTONS[btn_name]],
                check=True,
                capture_output=True,
            )
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
    ) -> tuple[tuple[int, int], tuple[int, int]]:
        btn_name = button.lower()
        btn_code = BUTTON_CODES.get(btn_name, BTN_LEFT)
        start = self.move(x1, y1)
        time.sleep(0.015)
        steps = max(2, min(60, int(steps)))
        dt = max(0.004, (duration_ms / 1000.0) / steps)
        if self._ensure_uinput():
            self._emit_ptr([(EV_KEY, btn_code, 1)])
            time.sleep(0.018)
            try:
                for i in range(1, steps + 1):
                    # Cosine ease-in-out for smooth compositor & window drag recognition
                    t = 0.5 - 0.5 * math.cos(math.pi * i / steps)
                    ix = int(round(x1 + (x2 - x1) * t))
                    iy = int(round(y1 + (y2 - y1) * t))
                    _, _, ax, ay = self._screen_to_abs(ix, iy)
                    self._emit_ptr([(EV_ABS, ABS_X, ax), (EV_ABS, ABS_Y, ay)])
                    time.sleep(dt)
            finally:
                time.sleep(0.015)
                self._emit_ptr([(EV_KEY, btn_code, 0)])
            end = (int(round(x2)), int(round(y2)))
            self._last_pos = end
            return start, end
        raise RuntimeError(f"Drag requires /dev/uinput (error: {self._uinput_error})")

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
            time.sleep(0.01)
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
                time.sleep(0.006)
            return
        if os.environ.get("DISPLAY") and shutil.which("xdotool"):
            bmap = {"up": "4", "down": "5", "left": "6", "right": "7"}
            subprocess.run(["xdotool", "click", "--repeat", str(n), bmap[d]], check=True, capture_output=True)
            return
        raise RuntimeError(f"No scroll backend available (uinput: {self._uinput_error})")

    def hotkey(self, keys: str | list[str], *, count: int = 1) -> None:
        parsed = parse_key_combo(keys)
        codes = [c for _, c in parsed]
        if self._ensure_uinput():
            for i in range(max(1, int(count))):
                # Press modifiers in order, then final key, then release in reverse
                for c in codes[:-1]:
                    self._emit_kbd([(EV_KEY, c, 1)])
                    time.sleep(0.004)
                self._emit_kbd([(EV_KEY, codes[-1], 1)])
                time.sleep(0.010)
                self._emit_kbd([(EV_KEY, codes[-1], 0)])
                for c in reversed(codes[:-1]):
                    time.sleep(0.004)
                    self._emit_kbd([(EV_KEY, c, 0)])
                if i + 1 < count:
                    time.sleep(0.03)
            return
        if shutil.which("xdotool") and os.environ.get("DISPLAY"):
            combo = "+".join(name for name, _ in parsed)
            for _ in range(max(1, int(count))):
                subprocess.run(["xdotool", "key", combo], check=True, capture_output=True)
            return
        raise RuntimeError(f"No keyboard backend available (uinput: {self._uinput_error})")

    def type_keys(self, text: str, *, key_delay_ms: int = 0) -> None:
        """Type ASCII text directly via kernel /dev/uinput at sub-millisecond cadence."""
        if not text:
            return
        if self._ensure_uinput():
            delay = max(0.002, key_delay_ms / 1000.0) if key_delay_ms > 0 else 0.0025
            shift = KEY_CODES["shift"]
            for ch in text:
                mapped = char_to_uinput(ch)
                if mapped is None:
                    raise ValueError(f"Character {ch!r} requires clipboard paste mode")
                need_shift, code = mapped
                if need_shift:
                    self._emit_kbd([(EV_KEY, shift, 1)])
                    time.sleep(0.0015)
                self._emit_kbd([(EV_KEY, code, 1)])
                time.sleep(delay)
                self._emit_kbd([(EV_KEY, code, 0)])
                if need_shift:
                    time.sleep(0.0015)
                    self._emit_kbd([(EV_KEY, shift, 0)])
            return
        if shutil.which("wtype"):
            subprocess.run(["wtype", "--", text], check=True, capture_output=True)
            return
        if shutil.which("ydotool"):
            subprocess.run(["ydotool", "type", "--key-delay", str(max(1, key_delay_ms)), "--", text], check=True, capture_output=True)
            return
        if os.environ.get("DISPLAY") and shutil.which("xdotool"):
            subprocess.run(["xdotool", "type", "--delay", str(max(1, key_delay_ms)), "--", text], check=True, capture_output=True)
            return
        raise RuntimeError(f"No typing backend available (uinput: {self._uinput_error})")

    def is_pure_ascii_typable(self, text: str) -> bool:
        return all(char_to_uinput(ch) is not None for ch in text)
