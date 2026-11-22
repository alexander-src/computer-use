"""Sub-millisecond Linux clipboard backend (KDE Klipper DBus, wl-clipboard, xclip/xsel)."""

from __future__ import annotations

import os
import shutil
import subprocess


class Clipboard:
    """Fast clipboard read/write using Klipper DBus (0.4 ms) with Wayland/X11 fallbacks."""

    def __init__(self, session_bus=None):
        self._bus = session_bus
        self._klipper = None
        self._backend = self._detect()

    def _detect(self) -> str:
        if self._try_klipper() is not None:
            return "klipper-dbus"
        if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-copy") and shutil.which("wl-paste"):
            return "wl-clipboard"
        if os.environ.get("DISPLAY") and shutil.which("xclip"):
            return "xclip"
        if os.environ.get("DISPLAY") and shutil.which("xsel"):
            return "xsel"
        return "unavailable"

    def _try_klipper(self):
        if self._klipper is not None:
            return self._klipper
        try:
            import dbus
            bus = self._bus or dbus.SessionBus()
            proxy = bus.get_object("org.kde.klipper", "/klipper")
            iface = dbus.Interface(proxy, "org.kde.klipper.klipper")
            iface.getClipboardContents()
            self._klipper = iface
            return iface
        except Exception:
            return None

    @property
    def backend(self) -> str:
        return self._backend

    def get_text(self) -> str:
        if self._backend == "klipper-dbus":
            k = self._try_klipper()
            if k is not None:
                return str(k.getClipboardContents())
        if shutil.which("wl-paste") and os.environ.get("WAYLAND_DISPLAY"):
            r = subprocess.run(["wl-paste", "--no-newline"], capture_output=True, text=True, timeout=2)
            return r.stdout if r.returncode == 0 else ""
        if shutil.which("xclip") and os.environ.get("DISPLAY"):
            r = subprocess.run(["xclip", "-selection", "clipboard", "-o"], capture_output=True, text=True, timeout=2)
            return r.stdout if r.returncode == 0 else ""
        if shutil.which("xsel") and os.environ.get("DISPLAY"):
            r = subprocess.run(["xsel", "--clipboard", "--output"], capture_output=True, text=True, timeout=2)
            return r.stdout if r.returncode == 0 else ""
        raise RuntimeError("No clipboard backend available")

    def set_text(self, text: str) -> None:
        if self._backend == "klipper-dbus":
            k = self._try_klipper()
            if k is not None:
                k.setClipboardContents(text)
                return
        if shutil.which("wl-copy") and os.environ.get("WAYLAND_DISPLAY"):
            subprocess.run(["wl-copy"], input=text, text=True, check=True, capture_output=True, timeout=2)
            return
        if shutil.which("xclip") and os.environ.get("DISPLAY"):
            subprocess.run(["xclip", "-selection", "clipboard", "-i"], input=text, text=True, check=True, capture_output=True, timeout=2)
            return
        if shutil.which("xsel") and os.environ.get("DISPLAY"):
            subprocess.run(["xsel", "--clipboard", "--input"], input=text, text=True, check=True, capture_output=True, timeout=2)
            return
        raise RuntimeError("No clipboard backend available")
