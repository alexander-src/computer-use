"""Ultra-fast Linux screen capture, Set-of-Marks annotation, coordinate grid, and pixel diff engine.

On KDE Plasma 6 Wayland, captures directly over org.kde.KWin.ScreenShot2 Unix FD pipe:
- Full 1920x1080 raw frame: ~3.5 ms
- Region crop (400x300): ~0.28 ms
- Cairo bilinear downscale + AVX2 libturbojpeg zero-copy encode: ~1.5 ms (~5.2 ms total)
Automatically falls back to GdkPixbuf (with glycin RGB8 compatibility), grim, spectacle,
or maim/scrot (X11).
"""

from __future__ import annotations

import atexit
import base64
import ctypes
import ctypes.util
import os
import select
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from typing import Any, Callable

import numpy as np


@dataclass
class Frame:
    rgba: np.ndarray  # shape (H, W, 4), uint8, writable RGBX/RGBA channel order
    origin_x: int     # logical screen-space origin X
    origin_y: int     # logical screen-space origin Y
    width: int        # pixel width of rgba
    height: int       # pixel height of rgba
    scale: float      # physical pixels per logical unit (1.0 on integer 100% scale)
    backend: str
    capture_ms: float

    @property
    def logical_width(self) -> int:
        s = self.scale if self.scale > 0 else 1.0
        return max(1, int(round(self.width / s)))

    @property
    def logical_height(self) -> int:
        s = self.scale if self.scale > 0 else 1.0
        return max(1, int(round(self.height / s)))


# Qt6 QImage::Format values
_QT_BGRA_FORMATS = {4, 5, 6}       # RGB32, ARGB32, ARGB32_Premultiplied
_QT_RGBA_FORMATS = {16, 17, 18}    # RGBX8888, RGBA8888, RGBA8888_Premultiplied
_QT_RGB24_FORMAT = 13              # RGB888 (3 bytes/px)


class TurboJpegEncoder:
    """Zero-copy ctypes binding to libturbojpeg (AVX2/NEON SIMD JPEG encode in ~1.5 ms)."""

    TJPF_RGB = 0
    TJPF_BGR = 1
    TJPF_RGBX = 7
    TJPF_BGRX = 8
    TJSAMP_420 = 2
    TJFLAG_FASTDCT = 2048

    def __init__(self) -> None:
        self._lib = None
        self._handle = None
        self._init()

    def _init(self) -> None:
        if sys.platform == "win32":
            candidates = [ctypes.util.find_library("turbojpeg"), "turbojpeg.dll"]
        elif sys.platform == "darwin":
            candidates = [
                ctypes.util.find_library("turbojpeg"),
                "/opt/homebrew/lib/libturbojpeg.dylib",
                "/opt/homebrew/opt/jpeg-turbo/lib/libturbojpeg.dylib",
                "/usr/local/lib/libturbojpeg.dylib",
                "/usr/local/opt/jpeg-turbo/lib/libturbojpeg.dylib",
                "libturbojpeg.0.dylib",
                "libturbojpeg.dylib",
            ]
        else:
            candidates = [ctypes.util.find_library("turbojpeg"), "libturbojpeg.so.0", "libturbojpeg.so"]
        for cand in candidates:
            if not cand:
                continue
            try:
                lib = ctypes.CDLL(cand)
                lib.tjInitCompress.restype = ctypes.c_void_p
                lib.tjCompress2.argtypes = [
                    ctypes.c_void_p,
                    ctypes.c_void_p,
                    ctypes.c_int,
                    ctypes.c_int,
                    ctypes.c_int,
                    ctypes.c_int,
                    ctypes.POINTER(ctypes.c_void_p),
                    ctypes.POINTER(ctypes.c_ulong),
                    ctypes.c_int,
                    ctypes.c_int,
                    ctypes.c_int,
                ]
                lib.tjCompress2.restype = ctypes.c_int
                lib.tjFree.argtypes = [ctypes.c_void_p]
                lib.tjDestroy.argtypes = [ctypes.c_void_p]
                handle = lib.tjInitCompress()
                if handle:
                    self._lib = lib
                    self._handle = handle
                    atexit.register(self.close)
                    return
            except Exception:
                continue

    @property
    def available(self) -> bool:
        return self._handle is not None

    def compress(self, arr: np.ndarray, *, pixel_format: int = TJPF_RGBX, quality: int = 75) -> bytes:
        assert self._lib is not None and self._handle is not None
        if not arr.flags["C_CONTIGUOUS"]:
            arr = np.ascontiguousarray(arr)
        h, w = arr.shape[:2]
        buf = ctypes.c_void_p()
        sz = ctypes.c_ulong(0)
        q = max(20, min(95, int(quality)))
        rc = self._lib.tjCompress2(
            self._handle,
            arr.ctypes.data,
            int(w),
            int(arr.strides[0]),
            int(h),
            int(pixel_format),
            ctypes.byref(buf),
            ctypes.byref(sz),
            self.TJSAMP_420,
            q,
            self.TJFLAG_FASTDCT,
        )
        if rc != 0 or not buf.value:
            if buf.value:
                self._lib.tjFree(buf)
            raise RuntimeError(f"tjCompress2 failed with code {rc}")
        try:
            return ctypes.string_at(buf.value, sz.value)
        finally:
            self._lib.tjFree(buf)

    def close(self) -> None:
        if self._lib is not None and self._handle is not None:
            try:
                self._lib.tjDestroy(self._handle)
            except Exception:
                pass
            self._handle = None


def _draw_cairo_overlays(
    ctx,
    *,
    w: int,
    h: int,
    origin_x: int,
    origin_y: int,
    scale_x: float,
    scale_y: float,
    annotations: list[dict[str, Any]],
    grid: bool,
    cursor: tuple[int, int] | None,
    swap_rb: bool,
) -> None:
    """Draw 0..1000 coordinate grid, Set-of-Marks [ref] badges, and cursor ring on a Cairo context."""
    import cairo

    ctx.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
    if swap_rb:
        # Grayscale AA avoids inverted R/B subpixel LCD color fringes when drawing onto RGBX buffers
        fo = cairo.FontOptions()
        fo.set_antialias(cairo.ANTIALIAS_GRAY)
        ctx.set_font_options(fo)

    def set_rgba(r: float, g: float, b: float, a: float) -> None:
        if swap_rb:
            ctx.set_source_rgba(b, g, r, a)
        else:
            ctx.set_source_rgba(r, g, b, a)

    if grid:
        ctx.set_line_width(1.0)
        ctx.set_font_size(11.0)
        for n in range(100, 1000, 100):
            gx = round(w * n / 1000.0) + 0.5
            gy = round(h * n / 1000.0) + 0.5
            set_rgba(0.0, 0.85, 1.0, 0.16)
            ctx.move_to(gx, 0)
            ctx.line_to(gx, h)
            ctx.move_to(0, gy)
            ctx.line_to(w, gy)
            ctx.stroke()
            lbl = str(n)
            set_rgba(0.05, 0.05, 0.08, 0.72)
            ctx.rectangle(gx - 12, 2, 26, 13)
            ctx.rectangle(2, gy - 7, 26, 13)
            ctx.fill()
            set_rgba(0.4, 0.95, 1.0, 0.92)
            ctx.move_to(gx - 10, 12)
            ctx.show_text(lbl)
            ctx.move_to(4, gy + 3)
            ctx.show_text(lbl)

    if annotations:
        palette = (
            (0.95, 0.26, 0.21),  # red
            (0.13, 0.59, 0.95),  # blue
            (0.18, 0.76, 0.36),  # green
            (0.98, 0.62, 0.04),  # amber
            (0.68, 0.30, 0.92),  # purple
        )
        ctx.set_font_size(11.5)
        for el in annotations:
            ref = el.get("ref")
            bx, by, bw, bh = el.get("bbox", (0, 0, 0, 0))
            rx = (bx - origin_x) * scale_x
            ry = (by - origin_y) * scale_y
            rw = bw * scale_x
            rh = bh * scale_y
            if rw < 4 or rh < 4 or rx + rw < 0 or ry + rh < 0 or rx > w or ry > h:
                continue
            r, g, b = palette[int(ref) % len(palette)]
            ctx.set_line_width(1.5)
            set_rgba(r, g, b, 0.88)
            ctx.rectangle(rx + 0.5, ry + 0.5, max(2.0, rw), max(2.0, rh))
            ctx.stroke()

            label = str(ref)
            ext = ctx.text_extents(label)
            pw = ext.width + 8
            ph = 15
            px = max(1.0, min(w - pw - 1.0, rx))
            py = max(1.0, ry - ph if ry >= ph + 1 else ry + 1)
            set_rgba(r, g, b, 0.94)
            ctx.rectangle(px, py, pw, ph)
            ctx.fill()
            set_rgba(1.0, 1.0, 1.0, 1.0)
            ctx.move_to(px + 4, py + 11.5)
            ctx.show_text(label)

    if cursor is not None:
        cx = (cursor[0] - origin_x) * scale_x
        cy = (cursor[1] - origin_y) * scale_y
        if 0 <= cx <= w and 0 <= cy <= h:
            ctx.set_line_width(2.0)
            set_rgba(0.14, 0.75, 0.98, 0.95)
            ctx.new_sub_path()
            ctx.arc(cx, cy, 8.0, 0, 6.283185)
            ctx.stroke()


def _apply_ignore_rects(
    diff_mask: np.ndarray,
    frame: Frame,
    step: int,
    ignore_rects: list[tuple[int, int, int, int]] | None,
) -> None:
    """Zero out subsampled diff pixels overlapping ignored screen rects (e.g. AI cursor overlay)."""
    if not ignore_rects:
        return
    h_sub, w_sub = diff_mask.shape
    sc = frame.scale if frame.scale > 0 else 1.0
    for rx, ry, rw, rh in ignore_rects:
        if rw <= 0 or rh <= 0:
            continue
        x0 = max(0, int(((rx - frame.origin_x) * sc) // step))
        y0 = max(0, int(((ry - frame.origin_y) * sc) // step))
        x1 = min(w_sub, int(((rx + rw - frame.origin_x) * sc + step - 1) // step) + 1)
        y1 = min(h_sub, int(((ry + rh - frame.origin_y) * sc + step - 1) // step) + 1)
        if x1 > x0 and y1 > y0:
            diff_mask[y0:y1, x0:x1] = False


class ScreenCapture:
    """Multi-backend Linux screen capture with TurboJPEG, Set-of-Marks, coordinate grid, and frame diffing."""

    def __init__(
        self,
        session_bus=None,
        virtual_bounds_fn: Callable[[], tuple[int, int, int, int]] | None = None,
    ):
        self._bus = session_bus
        self._virtual_bounds_fn = virtual_bounds_fn
        self._kwin_ss = None
        self._backend = self._detect()
        self._last_thumb: np.ndarray | None = None
        self._last_frame_rect: tuple[int, int, int, int] | None = None
        self._jpeg_needs_rgb: bool = False
        self._tj = TurboJpegEncoder()

    def _detect(self) -> str:
        if self._try_kwin() is not None:
            return "kwin-screenshot2"
        if os.environ.get("WAYLAND_DISPLAY"):
            if shutil.which("grim"):
                return "grim"
            if shutil.which("spectacle"):
                return "spectacle"
        if os.environ.get("DISPLAY"):
            # On X11, maim/scrot are ~10x faster than spawning KDE spectacle
            if shutil.which("maim"):
                return "maim"
            if shutil.which("scrot"):
                return "scrot"
        if shutil.which("spectacle"):
            return "spectacle"
        return "unavailable"

    def _try_kwin(self):
        if self._kwin_ss is not None:
            return self._kwin_ss
        try:
            import dbus
            bus = self._bus or dbus.SessionBus()
            proxy = bus.get_object("org.kde.KWin", "/org/kde/KWin/ScreenShot2")
            iface = dbus.Interface(proxy, "org.kde.KWin.ScreenShot2")
            self._kwin_ss = iface
            return iface
        except Exception:
            return None

    @property
    def backend(self) -> str:
        enc = "+turbojpeg" if self._tj.available else ""
        return f"{self._backend}{enc}" if self._backend != "unavailable" else "unavailable"

    def _workspace_origin(self) -> tuple[int, int]:
        if self._virtual_bounds_fn is not None:
            try:
                vx, vy, _, _ = self._virtual_bounds_fn()
                return int(vx), int(vy)
            except Exception:
                pass
        return 0, 0

    def _read_kwin_pipe(self, call_fn, timeout_s: float = 3.0) -> tuple[dict[str, Any], bytearray]:
        import dbus
        r_fd, w_fd = os.pipe()
        try:
            meta = call_fn(dbus.types.UnixFd(w_fd))
            os.close(w_fd)
            w_fd = -1
            expected = int(meta.get("stride", 0)) * int(meta.get("height", 0))
            deadline = time.monotonic() + timeout_s
            if expected > 0:
                buf = bytearray(expected)
                mv = memoryview(buf)
                pos = 0
                while pos < expected:
                    rem = deadline - time.monotonic()
                    if rem <= 0:
                        raise TimeoutError("Timed out reading KWin ScreenShot2 pipe")
                    ready, _, _ = select.select([r_fd], [], [], rem)
                    if not ready:
                        raise TimeoutError("Timed out reading KWin ScreenShot2 pipe")
                    n = os.readv(r_fd, [mv[pos:]])
                    if n <= 0:
                        break
                    pos += n
                return dict(meta), buf[:pos] if pos < expected else buf
            out = bytearray()
            while True:
                rem = deadline - time.monotonic()
                if rem <= 0:
                    raise TimeoutError("Timed out reading KWin ScreenShot2 pipe")
                ready, _, _ = select.select([r_fd], [], [], rem)
                if not ready:
                    raise TimeoutError("Timed out reading KWin ScreenShot2 pipe")
                chunk = os.read(r_fd, 1 << 20)
                if not chunk:
                    break
                out.extend(chunk)
            return dict(meta), out
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

    def _decode_kwin_raw(self, meta: dict[str, Any], raw: bytearray, ox: int, oy: int, capture_ms: float) -> Frame:
        w = int(meta.get("width", 0))
        h = int(meta.get("height", 0))
        stride = int(meta.get("stride", 0))
        fmt = int(meta.get("format", 16))
        scale = float(meta.get("scale", 1.0)) or 1.0
        if w <= 0 or h <= 0 or stride <= 0 or len(raw) < h * stride:
            raise RuntimeError(f"Invalid or truncated KWin screenshot frame (w={w}, h={h}, stride={stride}, bytes={len(raw)})")

        if fmt == _QT_RGB24_FORMAT and stride >= w * 3:
            src = np.frombuffer(raw, dtype=np.uint8)[: h * stride].reshape((h, stride))[:, : w * 3].reshape((h, w, 3))
            rgba = np.empty((h, w, 4), dtype=np.uint8)
            rgba[:, :, :3] = src
            rgba[:, :, 3] = 255
        elif stride % 4 == 0 and stride >= w * 4 and (fmt in _QT_BGRA_FORMATS or fmt in _QT_RGBA_FORMATS):
            arr = np.frombuffer(raw, dtype=np.uint8)[: h * stride].reshape((h, stride // 4, 4))[:, :w, :]
            if fmt in _QT_BGRA_FORMATS:
                rgba = np.empty((h, w, 4), dtype=np.uint8)
                rgba[:, :, 0] = arr[:, :, 2]
                rgba[:, :, 1] = arr[:, :, 1]
                rgba[:, :, 2] = arr[:, :, 0]
                rgba[:, :, 3] = 255
            else:
                rgba = arr if (arr.flags["C_CONTIGUOUS"] and arr.flags["WRITEABLE"]) else np.array(arr, dtype=np.uint8, copy=True)
                if fmt == 18:
                    # Premultiplied RGBA8888 (e.g. CaptureWindow) -> opaque alpha avoids dark PNG halos
                    rgba[:, :, 3] = 255
        else:
            raise RuntimeError(f"Unsupported KWin QImage format={fmt} (w={w}, h={h}, stride={stride})")

        return Frame(rgba=rgba, origin_x=ox, origin_y=oy, width=w, height=h, scale=scale,
                     backend="kwin-screenshot2", capture_ms=capture_ms)

    def capture(
        self,
        *,
        scope: str = "screen",
        area: tuple[int, int, int, int] | None = None,
        window_id: str | None = None,
        active_rect: tuple[int, int, int, int] | None = None,
        include_cursor: bool = True,
    ) -> Frame:
        """Capture full screen, active window, specific window, or [x, y, w, h] area."""
        t0 = time.perf_counter()
        if self._backend == "kwin-screenshot2":
            import dbus
            iface = self._try_kwin()
            assert iface is not None
            opts = {
                "native-resolution": dbus.Boolean(False),
                "include-cursor": dbus.Boolean(bool(include_cursor)),
                "include-decoration": dbus.Boolean(True),
            }
            if scope == "area" and area is not None:
                ax, ay, aw, ah = (int(area[0]), int(area[1]), max(1, int(area[2])), max(1, int(area[3])))
                meta, raw = self._read_kwin_pipe(
                    lambda fd: iface.CaptureArea(dbus.Int32(ax), dbus.Int32(ay), dbus.UInt32(aw), dbus.UInt32(ah), opts, fd)
                )
                return self._decode_kwin_raw(meta, raw, ax, ay, (time.perf_counter() - t0) * 1000.0)
            if scope == "active" and active_rect is not None:
                ax, ay, aw, ah = (int(active_rect[0]), int(active_rect[1]), max(1, int(active_rect[2])), max(1, int(active_rect[3])))
                meta, raw = self._read_kwin_pipe(
                    lambda fd: iface.CaptureArea(dbus.Int32(ax), dbus.Int32(ay), dbus.UInt32(aw), dbus.UInt32(ah), opts, fd)
                )
                return self._decode_kwin_raw(meta, raw, ax, ay, (time.perf_counter() - t0) * 1000.0)
            if scope == "window" and window_id:
                meta, raw = self._read_kwin_pipe(lambda fd: iface.CaptureWindow(str(window_id), opts, fd))
                ox = int(active_rect[0]) if active_rect else 0
                oy = int(active_rect[1]) if active_rect else 0
                return self._decode_kwin_raw(meta, raw, ox, oy, (time.perf_counter() - t0) * 1000.0)
            if scope == "active":
                meta, raw = self._read_kwin_pipe(lambda fd: iface.CaptureActiveWindow(opts, fd))
                ox = int(active_rect[0]) if active_rect else 0
                oy = int(active_rect[1]) if active_rect else 0
                return self._decode_kwin_raw(meta, raw, ox, oy, (time.perf_counter() - t0) * 1000.0)
            meta, raw = self._read_kwin_pipe(lambda fd: iface.CaptureWorkspace(opts, fd))
            vx, vy = self._workspace_origin()
            return self._decode_kwin_raw(meta, raw, vx, vy, (time.perf_counter() - t0) * 1000.0)

        return self._capture_cli(scope=scope, area=area, active_rect=active_rect, t0=t0)

    def _capture_cli(
        self,
        *,
        scope: str,
        area: tuple[int, int, int, int] | None,
        active_rect: tuple[int, int, int, int] | None,
        t0: float,
    ) -> Frame:
        crop_rect = area if scope == "area" else (active_rect if scope == "active" else None)
        vx, vy = self._workspace_origin()
        with tempfile.TemporaryDirectory(prefix="agcu_ss_") as td:
            out_path = os.path.join(td, "shot.png")
            if self._backend == "grim":
                cmd = ["grim"]
                if crop_rect:
                    cmd += ["-g", f"{crop_rect[0]},{crop_rect[1]} {crop_rect[2]}x{crop_rect[3]}"]
                cmd.append(out_path)
                subprocess.run(cmd, check=True, capture_output=True, timeout=5)
                ox, oy = (crop_rect[0], crop_rect[1]) if crop_rect else (vx, vy)
            elif self._backend == "spectacle":
                subprocess.run(["spectacle", "-b", "-n", "-f", "-o", out_path], check=True, capture_output=True, timeout=5)
                ox, oy = vx, vy
            elif self._backend == "maim":
                cmd = ["maim"]
                if crop_rect:
                    cmd += ["-g", f"{crop_rect[2]}x{crop_rect[3]}+{crop_rect[0]}+{crop_rect[1]}"]
                cmd.append(out_path)
                subprocess.run(cmd, check=True, capture_output=True, timeout=5)
                ox, oy = (crop_rect[0], crop_rect[1]) if crop_rect else (vx, vy)
            elif self._backend == "scrot":
                subprocess.run(["scrot", out_path], check=True, capture_output=True, timeout=5)
                ox, oy = vx, vy
            else:
                raise RuntimeError("No screen capture backend available")

            rgba = self._load_png_rgba(out_path)
            h, w = rgba.shape[:2]
            if crop_rect and self._backend in ("spectacle", "scrot"):
                x0 = max(0, min(w - 1, crop_rect[0] - vx))
                y0 = max(0, min(h - 1, crop_rect[1] - vy))
                x1 = max(x0 + 1, min(w, crop_rect[0] - vx + crop_rect[2]))
                y1 = max(y0 + 1, min(h, crop_rect[1] - vy + crop_rect[3]))
                rgba = np.ascontiguousarray(rgba[y0:y1, x0:x1, :])
                ox, oy = vx + x0, vy + y0
                h, w = rgba.shape[:2]
            return Frame(rgba=rgba, origin_x=ox, origin_y=oy, width=w, height=h, scale=1.0,
                         backend=self._backend, capture_ms=(time.perf_counter() - t0) * 1000.0)

    @staticmethod
    def _load_png_rgba(path: str) -> np.ndarray:
        try:
            import gi
            gi.require_version("GdkPixbuf", "2.0")
            from gi.repository import GdkPixbuf

            pb = GdkPixbuf.Pixbuf.new_from_file(path)
            if not pb.get_has_alpha():
                pb = pb.add_alpha(False, 0, 0, 0)
            w, h, rs = pb.get_width(), pb.get_height(), pb.get_rowstride()
            arr = np.frombuffer(pb.get_pixels(), dtype=np.uint8).reshape((h, rs // 4, 4))[:, :w, :]
            return np.array(arr, dtype=np.uint8, copy=True)
        except Exception:
            import cairo

            surf = cairo.ImageSurface.create_from_png(path)
            w, h, stride = surf.get_width(), surf.get_height(), surf.get_stride()
            bgra = np.frombuffer(surf.get_data(), dtype=np.uint8).reshape((h, stride // 4, 4))[:, :w, :]
            rgba = np.empty((h, w, 4), dtype=np.uint8)
            rgba[:, :, 0] = bgra[:, :, 2]
            rgba[:, :, 1] = bgra[:, :, 1]
            rgba[:, :, 2] = bgra[:, :, 0]
            rgba[:, :, 3] = 255
            return rgba

    def _thumbnail(self, frame: Frame) -> np.ndarray:
        step = max(1, min(frame.width, frame.height) // 160)
        return frame.rgba[::step, ::step, :3].astype(np.int16, copy=False)

    def diff_frames(
        self,
        before: Frame,
        after: Frame,
        *,
        ignore_rects: list[tuple[int, int, int, int]] | None = None,
    ) -> dict[str, Any]:
        """Compute changed pixel percentage and logical screen-space bounding box in ~1.5 ms."""
        if before.width != after.width or before.height != after.height:
            return {
                "changed_pct": 100.0,
                "changed_bbox": [after.origin_x, after.origin_y, after.logical_width, after.logical_height],
            }
        step = max(1, min(after.width, after.height) // 180)
        a = before.rgba[::step, ::step, :3].astype(np.int16)
        b = after.rgba[::step, ::step, :3].astype(np.int16)
        diff = np.abs(a - b).max(axis=2) > 8
        _apply_ignore_rects(diff, after, step, ignore_rects)
        total = diff.size
        changed = int(diff.sum())
        if changed == 0:
            return {"changed_pct": 0.0, "changed_bbox": None}
        ys, xs = np.nonzero(diff)
        sc = after.scale if after.scale > 0 else 1.0
        x0 = after.origin_x + int(round((int(xs.min()) * step) / sc))
        y0 = after.origin_y + int(round((int(ys.min()) * step) / sc))
        x1 = after.origin_x + int(round(min(after.width, int(xs.max() + 1) * step) / sc))
        y1 = after.origin_y + int(round(min(after.height, int(ys.max() + 1) * step) / sc))
        return {
            "changed_pct": round(100.0 * changed / max(1, total), 2),
            "changed_bbox": [x0, y0, max(1, x1 - x0), max(1, y1 - y0)],
        }

    def diff_from_last(
        self,
        frame: Frame,
        *,
        ignore_rects: list[tuple[int, int, int, int]] | None = None,
    ) -> dict[str, Any] | None:
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
                sc = frame.scale if frame.scale > 0 else 1.0
                ys, xs = np.nonzero(diff)
                x0 = frame.origin_x + int(round((int(xs.min()) * step) / sc))
                y0 = frame.origin_y + int(round((int(ys.min()) * step) / sc))
                x1 = frame.origin_x + int(round(min(frame.width, int(xs.max() + 1) * step) / sc))
                y1 = frame.origin_y + int(round(min(frame.height, int(ys.max() + 1) * step) / sc))
                bbox = [x0, y0, max(1, x1 - x0), max(1, y1 - y0)]
            res = {"changed_pct": pct, "changed_bbox": bbox}
        self._last_thumb = thumb
        self._last_frame_rect = rect
        return res

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
        """Poll rapidly until consecutive frames stabilize (ignoring our own AI cursor overlay)."""
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

    def wait_for_change(
        self,
        *,
        area: tuple[int, int, int, int] | None = None,
        timeout_ms: int = 5000,
        threshold_pct: float = 0.15,
        ignore_rects_fn: Callable[[], list[tuple[int, int, int, int]]] | None = None,
    ) -> dict[str, Any]:
        """Poll fast CaptureArea until screen pixels change by >= threshold_pct."""
        scope = "area" if area else "screen"
        t0 = time.monotonic()
        deadline = t0 + (timeout_ms / 1000.0)
        base_ig = ignore_rects_fn() if ignore_rects_fn else []
        base = self.capture(scope=scope, area=area, include_cursor=False)
        while time.monotonic() < deadline:
            time.sleep(0.022)
            cur_ig = ignore_rects_fn() if ignore_rects_fn else []
            cur = self.capture(scope=scope, area=area, include_cursor=False)
            d = self.diff_frames(base, cur, ignore_rects=base_ig + cur_ig)
            if d["changed_pct"] >= threshold_pct:
                return {"changed": True, "elapsed_ms": round((time.monotonic() - t0) * 1000.0, 1), **d}
        return {"changed": False, "elapsed_ms": round((time.monotonic() - t0) * 1000.0, 1), "changed_pct": 0.0}

    def _encode_turbojpeg_fast(
        self,
        frame: Frame,
        *,
        out_w: int,
        out_h: int,
        quality: int,
        annotations: list[dict[str, Any]] | None,
        grid: bool,
        cursor: tuple[int, int] | None,
    ) -> bytes | None:
        """Ultra-fast ~1.5 ms Cairo bilinear downscale + zero-swap RGBX overlay + AVX2 libturbojpeg."""
        if not self._tj.available or sys.byteorder != "little":
            return None
        src_w, src_h = frame.width, frame.height
        need_scale = (out_w != src_w or out_h != src_h)
        need_overlay = bool(annotations or grid or cursor is not None)

        if not need_scale and not need_overlay:
            return self._tj.compress(frame.rgba, pixel_format=TurboJpegEncoder.TJPF_RGBX, quality=quality)

        try:
            import cairo
        except Exception:
            return None

        src_arr = frame.rgba
        if not (src_arr.flags["C_CONTIGUOUS"] and src_arr.flags["WRITEABLE"]):
            src_arr = np.array(src_arr, dtype=np.uint8, copy=True)

        if need_scale:
            src_surf = cairo.ImageSurface.create_for_data(src_arr, cairo.FORMAT_RGB24, src_w, src_h, src_w * 4)
            dst_arr = np.empty((out_h, out_w, 4), dtype=np.uint8)
            dst_surf = cairo.ImageSurface.create_for_data(dst_arr, cairo.FORMAT_RGB24, out_w, out_h, out_w * 4)
            ctx = cairo.Context(dst_surf)
            ctx.scale(out_w / max(1, src_w), out_h / max(1, src_h))
            ctx.set_source_surface(src_surf, 0, 0)
            ctx.get_source().set_filter(cairo.FILTER_BILINEAR)
            ctx.paint()
            ctx.identity_matrix()
        else:
            dst_arr = src_arr.copy()
            dst_surf = cairo.ImageSurface.create_for_data(dst_arr, cairo.FORMAT_RGB24, out_w, out_h, out_w * 4)
            ctx = cairo.Context(dst_surf)

        if need_overlay:
            # Map logical coordinates (AT-SPI2/KWin) onto output image pixels accounting for frame.scale
            log_w, log_h = frame.logical_width, frame.logical_height
            _draw_cairo_overlays(
                ctx,
                w=out_w,
                h=out_h,
                origin_x=frame.origin_x,
                origin_y=frame.origin_y,
                scale_x=out_w / max(1, log_w),
                scale_y=out_h / max(1, log_h),
                annotations=annotations or [],
                grid=grid,
                cursor=cursor,
                swap_rb=True,
            )
        dst_surf.flush()
        return self._tj.compress(dst_arr, pixel_format=TurboJpegEncoder.TJPF_RGBX, quality=quality)

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
        """Scale, optionally overlay Set-of-Marks / grid / cursor, and encode JPEG/PNG."""
        t0 = time.perf_counter()
        src_w, src_h = frame.width, frame.height
        mw = int(max_width) if max_width is not None else 0
        if mw > 0 and src_w > mw:
            out_w = mw
            out_h = max(1, int(round(src_h * (out_w / src_w))))
        else:
            out_w, out_h = src_w, src_h
        out_fmt = "png" if fmt.lower() == "png" else "jpeg"
        log_w, log_h = frame.logical_width, frame.logical_height

        if out_fmt == "jpeg":
            tj_bytes = self._encode_turbojpeg_fast(
                frame, out_w=out_w, out_h=out_h, quality=quality,
                annotations=annotations, grid=grid, cursor=cursor,
            )
            if tj_bytes is not None:
                encode_ms = (time.perf_counter() - t0) * 1000.0
                return {
                    "data_b64": base64.b64encode(tj_bytes).decode("ascii"),
                    "mime_type": "image/jpeg",
                    "bytes": len(tj_bytes),
                    "image_w": out_w,
                    "image_h": out_h,
                    "screen_origin": [frame.origin_x, frame.origin_y],
                    "screen_size": [log_w, log_h],
                    "scale_to_screen": round(log_w / max(1, out_w), 4),
                    "capture_ms": round(frame.capture_ms, 1),
                    "encode_ms": round(encode_ms, 1),
                    "total_ms": round(frame.capture_ms + encode_ms, 1),
                }

        import gi
        gi.require_version("GdkPixbuf", "2.0")
        from gi.repository import GdkPixbuf, GLib

        scale_x = out_w / max(1, log_w)
        scale_y = out_h / max(1, log_h)
        raw_bytes = frame.rgba.tobytes()
        pb = GdkPixbuf.Pixbuf.new_from_bytes(
            GLib.Bytes.new(raw_bytes), GdkPixbuf.Colorspace.RGB, True, 8, src_w, src_h, src_w * 4
        )
        if out_w != src_w or out_h != src_h:
            pb = pb.scale_simple(out_w, out_h, GdkPixbuf.InterpType.BILINEAR)

        if annotations or grid or cursor is not None:
            pb = self._overlay_cairo(
                pb,
                origin_x=frame.origin_x,
                origin_y=frame.origin_y,
                scale_x=scale_x,
                scale_y=scale_y,
                annotations=annotations or [],
                grid=grid,
                cursor=cursor,
            )

        if out_fmt == "jpeg":
            q = str(max(20, min(95, int(quality))))
            if self._jpeg_needs_rgb and pb.get_has_alpha():
                pb = self._strip_alpha(pb)
            try:
                ok, encoded = pb.save_to_bufferv("jpeg", ["quality"], [q])
            except Exception:
                if not pb.get_has_alpha():
                    raise
                self._jpeg_needs_rgb = True
                pb = self._strip_alpha(pb)
                ok, encoded = pb.save_to_bufferv("jpeg", ["quality"], [q])
            mime = "image/jpeg"
        else:
            ok, encoded = pb.save_to_bufferv("png", ["compression"], ["3"])
            mime = "image/png"
        if not ok:
            raise RuntimeError(f"GdkPixbuf failed to encode {out_fmt}")

        encode_ms = (time.perf_counter() - t0) * 1000.0
        return {
            "data_b64": base64.b64encode(encoded).decode("ascii"),
            "mime_type": mime,
            "bytes": len(encoded),
            "image_w": out_w,
            "image_h": out_h,
            "screen_origin": [frame.origin_x, frame.origin_y],
            "screen_size": [log_w, log_h],
            "scale_to_screen": round(log_w / max(1, out_w), 4),
            "capture_ms": round(frame.capture_ms, 1),
            "encode_ms": round(encode_ms, 1),
            "total_ms": round(frame.capture_ms + encode_ms, 1),
        }

    @staticmethod
    def _strip_alpha(pb):
        from gi.repository import GdkPixbuf, GLib

        w, h, rs = pb.get_width(), pb.get_height(), pb.get_rowstride()
        arr = np.frombuffer(pb.get_pixels(), dtype=np.uint8).reshape((h, rs // 4, 4))[:, :w, :3]
        rgb = np.ascontiguousarray(arr)
        return GdkPixbuf.Pixbuf.new_from_bytes(
            GLib.Bytes.new(rgb.tobytes()), GdkPixbuf.Colorspace.RGB, False, 8, w, h, w * 3
        )

    def _overlay_cairo(
        self,
        pb,
        *,
        origin_x: int,
        origin_y: int,
        scale_x: float,
        scale_y: float,
        annotations: list[dict[str, Any]],
        grid: bool,
        cursor: tuple[int, int] | None,
    ):
        import cairo
        from gi.repository import GdkPixbuf, GLib

        w, h, rs = pb.get_width(), pb.get_height(), pb.get_rowstride()
        nch = pb.get_n_channels()
        raw = np.frombuffer(pb.get_pixels(), dtype=np.uint8).reshape((h, rs))[:, : w * nch].reshape((h, w, nch))
        bgra = np.empty((h, w, 4), dtype=np.uint8)
        bgra[:, :, 0] = raw[:, :, 2]
        bgra[:, :, 1] = raw[:, :, 1]
        bgra[:, :, 2] = raw[:, :, 0]
        bgra[:, :, 3] = 255
        surf = cairo.ImageSurface.create_for_data(bgra, cairo.FORMAT_ARGB32, w, h, w * 4)
        ctx = cairo.Context(surf)
        _draw_cairo_overlays(
            ctx,
            w=w,
            h=h,
            origin_x=origin_x,
            origin_y=origin_y,
            scale_x=scale_x,
            scale_y=scale_y,
            annotations=annotations,
            grid=grid,
            cursor=cursor,
            swap_rb=False,
        )
        surf.flush()
        rgb_out = np.ascontiguousarray(bgra[:, :, [2, 1, 0]])
        return GdkPixbuf.Pixbuf.new_from_bytes(
            GLib.Bytes.new(rgb_out.tobytes()), GdkPixbuf.Colorspace.RGB, False, 8, w, h, w * 3
        )
