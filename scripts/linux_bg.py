"""Isolated Linux background desktop session powered by authenticated Xvfb + direct libX11/libXtst ctypes.

Spins up a private invisible virtual X11 display using Xvfb -displayfd and a 0600 MIT-MAGIC-COOKIE-1
Xauthority file, with its own mouse cursor, keyboard, focus, and screen buffer so the agent can
launch, see, click, drag, and type into Linux GUI applications completely in the background without
ever touching your real monitor, mouse, keyboard, focus, or clipboard.
"""

from __future__ import annotations

import atexit
import ctypes
import ctypes.util
import math
import os
import secrets
import select
import shlex
import shutil
import signal
import struct
import subprocess
import tempfile
import time
from typing import Any

import numpy as np

from capture import Frame
from uinput_driver import to_x11_combo


class _XImage(ctypes.Structure):
    _fields_ = [
        ("width", ctypes.c_int),
        ("height", ctypes.c_int),
        ("xoffset", ctypes.c_int),
        ("format", ctypes.c_int),
        ("data", ctypes.c_void_p),
        ("byte_order", ctypes.c_int),
        ("bitmap_unit", ctypes.c_int),
        ("bitmap_bit_order", ctypes.c_int),
        ("bitmap_pad", ctypes.c_int),
        ("depth", ctypes.c_int),
        ("bytes_per_line", ctypes.c_int),
        ("bits_per_pixel", ctypes.c_int),
        ("red_mask", ctypes.c_ulong),
        ("green_mask", ctypes.c_ulong),
        ("blue_mask", ctypes.c_ulong),
    ]


_X_ERR_HANDLER_TYPE = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
_NONFATAL_X_ERR_HANDLER = _X_ERR_HANDLER_TYPE(lambda _dpy, _ev: 0)


def _write_xauth_file(path: str, display_num: str, cookie: bytes) -> None:
    """Write a pure-Python 0600 MIT-MAGIC-COOKIE-1 .Xauthority file (FamilyLocal=256 + FamilyWild=65535)."""
    def _rec(family: int, addr: bytes, num: bytes, name: bytes, data: bytes) -> bytes:
        return b"".join([
            struct.pack(">H", family),
            struct.pack(">H", len(addr)), addr,
            struct.pack(">H", len(num)), num,
            struct.pack(">H", len(name)), name,
            struct.pack(">H", len(data)), data,
        ])

    host = os.uname().nodename.encode("utf-8", "ignore")
    num_b = display_num.encode("ascii")
    proto = b"MIT-MAGIC-COOKIE-1"
    blob = _rec(256, host, num_b, proto, cookie) + _rec(65535, b"", num_b, proto, cookie)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, blob)
    finally:
        os.close(fd)


class LinuxBackgroundSession:
    """Private authenticated off-screen Xvfb desktop with zero-copy libX11 capture and XTest/xdotool input."""

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        self._display_str: str | None = None
        self._auth_path: str | None = None
        self._width = 1920
        self._height = 1080
        self._x11 = None
        self._xtst = None
        self._dpy = None
        self._root = 0
        self._apps: list[subprocess.Popen] = []
        self._cursor: tuple[int, int] = (0, 0)
        atexit.register(self.stop)

    @property
    def active(self) -> bool:
        return self._proc is not None and self._proc.poll() is None and self._display_str is not None and self._dpy is not None

    @property
    def display(self) -> str | None:
        return self._display_str if self.active else None

    @property
    def size(self) -> tuple[int, int]:
        return self._width, self._height

    def status(self) -> dict[str, Any]:
        return {
            "active": self.active,
            "backend": "xvfb+x11",
            "xvfb_available": bool(shutil.which("Xvfb")),
            "display": self.display,
            "size": [self._width, self._height],
            "cursor": list(self._cursor),
            "launched_pids": [p.pid for p in self._apps if p.poll() is None],
        }

    def _init_x11_libs(self, disp: str) -> None:
        x11_path = ctypes.util.find_library("X11") or "libX11.so.6"
        x11 = ctypes.CDLL(x11_path)
        x11.XSetErrorHandler.argtypes = [_X_ERR_HANDLER_TYPE]
        x11.XSetErrorHandler.restype = ctypes.c_void_p
        x11.XSetErrorHandler(_NONFATAL_X_ERR_HANDLER)

        x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        x11.XOpenDisplay.restype = ctypes.c_void_p
        x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
        x11.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        x11.XDefaultRootWindow.restype = ctypes.c_ulong
        x11.XGetImage.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_int,
            ctypes.c_uint, ctypes.c_uint, ctypes.c_ulong, ctypes.c_int,
        ]
        x11.XGetImage.restype = ctypes.POINTER(_XImage)
        x11.XDestroyImage.argtypes = [ctypes.POINTER(_XImage)]
        x11.XDestroyImage.restype = ctypes.c_int
        x11.XFlush.argtypes = [ctypes.c_void_p]
        x11.XQueryPointer.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_uint),
        ]
        x11.XQueryPointer.restype = ctypes.c_int
        x11.XSetInputFocus.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        x11.XSetInputFocus.restype = ctypes.c_int

        old_auth = os.environ.get("XAUTHORITY")
        if self._auth_path:
            os.environ["XAUTHORITY"] = self._auth_path
        try:
            dpy = None
            for _ in range(15):
                dpy = x11.XOpenDisplay(disp.encode("ascii"))
                if dpy:
                    break
                time.sleep(0.02)
            if not dpy:
                raise RuntimeError(f"XOpenDisplay({disp!r}) failed")
        finally:
            if old_auth is None:
                os.environ.pop("XAUTHORITY", None)
            else:
                os.environ["XAUTHORITY"] = old_auth

        self._x11 = x11
        self._dpy = dpy
        self._root = int(x11.XDefaultRootWindow(dpy))

        self._xtst = None
        for cand in (ctypes.util.find_library("Xtst"), "libXtst.so.6", "libXtst.so"):
            if not cand:
                continue
            try:
                xtst = ctypes.CDLL(cand)
                xtst.XTestFakeMotionEvent.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_ulong]
                xtst.XTestFakeButtonEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong]
                self._xtst = xtst
                break
            except Exception:
                continue

    def start(self, width: int = 1920, height: int = 1080) -> dict[str, Any]:
        if self.active:
            return self.status()
        xvfb = shutil.which("Xvfb")
        if not xvfb:
            raise RuntimeError("Xvfb is not installed (install xorg-server-xvfb / xvfb for Linux background mode)")
        self._width = max(320, int(width))
        self._height = max(240, int(height))

        # Generate a 0600 temp Xauthority file and let Xvfb pick a free display atomically via -displayfd
        fd_tmp, auth_path = tempfile.mkstemp(prefix="agcu_xauth_", suffix=".auth")
        os.close(fd_tmp)
        os.chmod(auth_path, 0o600)
        cookie = secrets.token_bytes(16)
        self._auth_path = auth_path

        r_fd, w_fd = os.pipe()
        try:
            os.set_inheritable(w_fd, True)
            # Pre-populate wildcard cookie so Xvfb starts with auth enabled, then rewrite with exact display num
            _write_xauth_file(auth_path, "0", cookie)
            self._proc = subprocess.Popen(
                [
                    xvfb, "-displayfd", str(w_fd),
                    "-screen", "0", f"{self._width}x{self._height}x24",
                    "-auth", auth_path, "-nolisten", "tcp",
                ],
                pass_fds=(w_fd,),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            os.close(w_fd)
            w_fd = -1

            ready, _, _ = select.select([r_fd], [], [], 3.0)
            if not ready:
                raise TimeoutError("Timed out waiting for Xvfb -displayfd")
            raw_num = os.read(r_fd, 64).decode("ascii", "ignore").strip()
            if not raw_num.isdigit():
                raise RuntimeError(f"Xvfb returned invalid display number: {raw_num!r}")
            disp = f":{raw_num}"
            # Rewrite Xauthority with the exact assigned display number (Xvfb re-reads or accepts FamilyWild)
            _write_xauth_file(auth_path, raw_num, cookie)
            self._display_str = disp
            self._init_x11_libs(disp)
            return self.status()
        except Exception:
            self.stop()
            raise
        finally:
            try:
                os.close(r_fd)
            except OSError:
                pass
            if w_fd >= 0:
                try:
                    os.close(w_fd)
                except OSError:
                    pass

    def stop(self) -> dict[str, Any]:
        for p in self._apps:
            if p.poll() is None:
                try:
                    os.killpg(os.getpgid(p.pid), signal.SIGTERM)
                except Exception:
                    try:
                        p.terminate()
                    except Exception:
                        pass
                try:
                    p.wait(timeout=0.5)
                except Exception:
                    try:
                        p.kill()
                        p.wait(timeout=0.5)
                    except Exception:
                        pass
        self._apps.clear()
        xvfb_alive = self._proc is not None and self._proc.poll() is None
        if self._dpy and self._x11 and xvfb_alive:
            try:
                self._x11.XCloseDisplay(self._dpy)
            except Exception:
                pass
        self._dpy = None
        if self._proc is not None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=2.0)
            except Exception:
                try:
                    self._proc.kill()
                    self._proc.wait(timeout=1.0)
                except Exception:
                    pass
            self._proc = None
        self._display_str = None
        if self._auth_path and os.path.exists(self._auth_path):
            try:
                os.unlink(self._auth_path)
            except OSError:
                pass
        self._auth_path = None
        return {"active": False}

    def _env(self) -> dict[str, str]:
        if not self.active:
            self.start()
        env = dict(os.environ)
        assert self._display_str is not None
        env["DISPLAY"] = self._display_str
        if self._auth_path:
            env["XAUTHORITY"] = self._auth_path
        env.pop("WAYLAND_DISPLAY", None)
        # Keep Qt, GTK, Electron, Firefox, and SDL apps inside the isolated Xvfb display
        env["XDG_SESSION_TYPE"] = "x11"
        env["QT_QPA_PLATFORM"] = "xcb"
        env["GDK_BACKEND"] = "x11"
        env["ELECTRON_OZONE_PLATFORM_HINT"] = "x11"
        env["MOZ_ENABLE_WAYLAND"] = "0"
        env["SDL_VIDEODRIVER"] = "x11"
        return env

    def _focus_window_under_cursor(self) -> None:
        """Without a window manager in Xvfb, explicitly set X11 input focus to the clicked window."""
        if not (self._x11 and self._dpy):
            return
        try:
            root_ret = ctypes.c_ulong(0)
            child = ctypes.c_ulong(0)
            rx, ry, wx, wy = ctypes.c_int(0), ctypes.c_int(0), ctypes.c_int(0), ctypes.c_int(0)
            mask = ctypes.c_uint(0)
            self._x11.XQueryPointer(
                self._dpy, self._root, ctypes.byref(root_ret), ctypes.byref(child),
                ctypes.byref(rx), ctypes.byref(ry), ctypes.byref(wx), ctypes.byref(wy), ctypes.byref(mask),
            )
            if child.value:
                RevertToParent = 2
                CurrentTime = 0
                self._x11.XSetInputFocus(self._dpy, child.value, RevertToParent, CurrentTime)
                self._x11.XFlush(self._dpy)
        except Exception:
            pass

    def launch(self, command: str | list[str], *, wait_ms: int = 400) -> dict[str, Any]:
        env = self._env()
        argv = shlex.split(command) if isinstance(command, str) else [str(a) for a in command]
        if not argv:
            raise ValueError("Empty command")
        proc = subprocess.Popen(
            argv, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self._apps.append(proc)
        if wait_ms > 0:
            time.sleep(min(3.0, wait_ms / 1000.0))
        return {"pid": proc.pid, "display": self._display_str, "command": argv}

    def capture(self, area: tuple[int, int, int, int] | None = None) -> Frame:
        if not self.active:
            self.start()
        assert self._x11 is not None and self._dpy is not None
        t0 = time.perf_counter()
        if area is not None:
            ox = max(0, min(self._width - 1, int(area[0])))
            oy = max(0, min(self._height - 1, int(area[1])))
            w = max(1, min(self._width - ox, int(area[2])))
            h = max(1, min(self._height - oy, int(area[3])))
        else:
            ox, oy, w, h = 0, 0, self._width, self._height
        ZPIXMAP = 2
        ALL_PLANES = ctypes.c_ulong(-1).value
        img_ptr = self._x11.XGetImage(self._dpy, self._root, ox, oy, w, h, ALL_PLANES, ZPIXMAP)
        if not img_ptr:
            raise RuntimeError("XGetImage failed on Xvfb display")
        try:
            img = img_ptr.contents
            stride = img.bytes_per_line
            if not img.data or stride < w * 4:
                raise RuntimeError(f"Unexpected XImage layout (stride={stride}, w={w})")
            # Direct zero-copy ctypes view onto XImage pixels (no extra string_at copy)
            buf = (ctypes.c_uint8 * (stride * h)).from_address(img.data)
            bgra = np.ctypeslib.as_array(buf).reshape((h, stride // 4, 4))[:, :w, :]
            rgba = np.empty((h, w, 4), dtype=np.uint8)
            rgba[:, :, 0] = bgra[:, :, 2]
            rgba[:, :, 1] = bgra[:, :, 1]
            rgba[:, :, 2] = bgra[:, :, 0]
            rgba[:, :, 3] = 255
        finally:
            self._x11.XDestroyImage(img_ptr)
        return Frame(
            rgba=rgba, origin_x=ox, origin_y=oy, width=w, height=h, scale=1.0,
            backend=f"xvfb({self._display_str})", capture_ms=(time.perf_counter() - t0) * 1000.0,
        )

    def move(self, x: int, y: int) -> tuple[int, int]:
        if not self.active:
            self.start()
        cx = max(0, min(self._width - 1, int(round(x))))
        cy = max(0, min(self._height - 1, int(round(y))))
        if self._xtst and self._dpy:
            self._xtst.XTestFakeMotionEvent(self._dpy, 0, cx, cy, 0)
            self._x11.XFlush(self._dpy)
        elif shutil.which("xdotool"):
            subprocess.run(["xdotool", "mousemove", str(cx), str(cy)], env=self._env(), check=True, capture_output=True, timeout=2)
        else:
            raise RuntimeError("Neither libXtst nor xdotool is available for Xvfb pointer input")
        self._cursor = (cx, cy)
        return cx, cy

    def click(self, x: int | None = None, y: int | None = None, *, button: str = "left", count: int = 1) -> tuple[int, int]:
        if not self.active:
            self.start()
        pos = self.move(x, y) if (x is not None and y is not None) else self._cursor
        self._focus_window_under_cursor()
        btn_map = {"left": 1, "middle": 2, "right": 3}
        b = btn_map.get(button.lower(), 1)
        if self._xtst and self._dpy:
            for i in range(max(1, int(count))):
                self._xtst.XTestFakeButtonEvent(self._dpy, b, 1, 0)
                self._x11.XFlush(self._dpy)
                time.sleep(0.008)
                self._xtst.XTestFakeButtonEvent(self._dpy, b, 0, 0)
                self._x11.XFlush(self._dpy)
                if i + 1 < count:
                    time.sleep(0.04)
        elif shutil.which("xdotool"):
            subprocess.run(["xdotool", "click", "--repeat", str(max(1, int(count))), str(b)], env=self._env(), check=True, capture_output=True, timeout=2)
        else:
            raise RuntimeError("Neither libXtst nor xdotool is available for Xvfb click input")
        return pos

    def drag(self, x1: int, y1: int, x2: int, y2: int, *, button: str = "left", duration_ms: int = 120, steps: int = 12) -> tuple[tuple[int, int], tuple[int, int]]:
        if not self.active:
            self.start()
        start = self.move(x1, y1)
        self._focus_window_under_cursor()
        btn_map = {"left": 1, "middle": 2, "right": 3}
        b = btn_map.get(button.lower(), 1)
        steps = max(2, min(40, int(steps)))
        dt = max(0.004, (duration_ms / 1000.0) / steps)
        if self._xtst and self._dpy:
            self._xtst.XTestFakeButtonEvent(self._dpy, b, 1, 0)
            self._x11.XFlush(self._dpy)
            end = start
            try:
                for i in range(1, steps + 1):
                    t = 0.5 - 0.5 * math.cos(math.pi * i / steps)
                    end = self.move(int(round(x1 + (x2 - x1) * t)), int(round(y1 + (y2 - y1) * t)))
                    time.sleep(dt)
            finally:
                self._xtst.XTestFakeButtonEvent(self._dpy, b, 0, 0)
                self._x11.XFlush(self._dpy)
            return start, end
        raise RuntimeError("Xvfb drag requires libXtst")

    def scroll(self, direction: str = "down", clicks: int = 5, *, x: int | None = None, y: int | None = None) -> None:
        if not self.active:
            self.start()
        if x is not None and y is not None:
            self.move(x, y)
        bmap = {"up": 4, "down": 5, "left": 6, "right": 7}
        b = bmap.get(direction.lower())
        if b is None:
            raise ValueError(f"Invalid scroll direction: {direction!r}")
        n = max(1, min(100, int(clicks)))
        if self._xtst and self._dpy:
            for _ in range(n):
                self._xtst.XTestFakeButtonEvent(self._dpy, b, 1, 0)
                self._xtst.XTestFakeButtonEvent(self._dpy, b, 0, 0)
                self._x11.XFlush(self._dpy)
        elif shutil.which("xdotool"):
            subprocess.run(["xdotool", "click", "--repeat", str(n), str(b)], env=self._env(), check=True, capture_output=True, timeout=2)
        else:
            raise RuntimeError("Neither libXtst nor xdotool is available for Xvfb scroll input")

    def type_text(self, text: str) -> None:
        if not text:
            return
        if not self.active:
            self.start()
        if shutil.which("xdotool"):
            subprocess.run(["xdotool", "type", "--clearmodifiers", "--delay", "2", "--", text], env=self._env(), check=True, capture_output=True, timeout=10)
            return
        raise RuntimeError("xdotool is required for typing inside Xvfb background sessions")

    def hotkey(self, keys: str | list[str], *, count: int = 1) -> None:
        if not self.active:
            self.start()
        if not shutil.which("xdotool"):
            raise RuntimeError("xdotool is required for hotkeys inside Xvfb background sessions")
        combo = to_x11_combo(keys)
        for _ in range(max(1, int(count))):
            subprocess.run(["xdotool", "key", "--clearmodifiers", combo], env=self._env(), check=True, capture_output=True, timeout=3)
