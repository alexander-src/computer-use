"""Ultra-fast Linux screen capture, Set-of-Marks annotation, coordinate grid, and pixel diff engine.

On KDE Plasma 6 Wayland, captures directly over org.kde.KWin.ScreenShot2 Unix FD pipe:
- Full 1920x1080 raw frame: ~8.5 ms
- Region crop (400x300): ~1.3 ms
- Zero-copy GdkPixbuf scale (1280px) + JPEG encode: ~22 ms (~31 ms total)
Automatically falls back to grim (wlroots), spectacle, or maim/scrot (X11).
"""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from typing import Any

import numpy as np


@dataclass
class Frame:
    rgba: np.ndarray  # shape (H, W, 4), uint8, RGBX/RGBA channel order
    origin_x: int
    origin_y: int
    width: int
    height: int
    scale: float
    backend: str
    capture_ms: float


# Qt6 QImage::Format values where channel order in little-endian memory is BGRA
_QT_BGRA_FORMATS = {4, 5, 6}  # RGB32, ARGB32, ARGB32_Premultiplied
# Qt6 QImage::Format values where channel order in memory is RGBA
_QT_RGBA_FORMATS = {16, 17, 18}  # RGBX8888, RGBA8888, RGBA8888_Premultiplied


class ScreenCapture:
    """Multi-backend Linux screen capture with Set-of-Marks, coordinate grid, and frame diffing."""

    def __init__(self, session_bus=None):
        self._bus = session_bus
        self._kwin_ss = None
        self._backend = self._detect()
        self._last_thumb: np.ndarray | None = None
        self._last_frame_rect: tuple[int, int, int, int] | None = None

    def _detect(self) -> str:
        if self._try_kwin() is not None:
            return "kwin-screenshot2"
        if os.environ.get("WAYLAND_DISPLAY") and shutil.which("grim"):
            return "grim"
        if shutil.which("spectacle"):
            return "spectacle"
        if os.environ.get("DISPLAY") and shutil.which("maim"):
            return "maim"
        if os.environ.get("DISPLAY") and shutil.which("scrot"):
            return "scrot"
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
        return self._backend

    def _read_kwin_pipe(self, call_fn) -> tuple[dict[str, Any], bytes]:
        import dbus
        r_fd, w_fd = os.pipe()
        try:
            meta = call_fn(dbus.types.UnixFd(w_fd))
            os.close(w_fd)
            w_fd = -1
            chunks: list[bytes] = []
            while True:
                buf = os.read(r_fd, 1 << 20)
                if not buf:
                    break
                chunks.append(buf)
            return dict(meta), b"".join(chunks)
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

    def _decode_kwin_raw(self, meta: dict[str, Any], raw: bytes, ox: int, oy: int, capture_ms: float) -> Frame:
        w = int(meta["width"])
        h = int(meta["height"])
        stride = int(meta["stride"])
        fmt = int(meta.get("format", 16))
        scale = float(meta.get("scale", 1.0))
        arr = np.frombuffer(raw, dtype=np.uint8).reshape((h, stride // 4, 4))[:, :w, :]
        if fmt in _QT_BGRA_FORMATS:
            rgba = np.ascontiguousarray(arr[:, :, [2, 1, 0, 3]])
        else:
            # Formats 16/17/18 are already RGBX/RGBA in memory (zero-copy view when stride == w*4)
            rgba = arr if arr.flags["C_CONTIGUOUS"] else np.ascontiguousarray(arr)
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
                return self._decode_kwin_raw(meta, raw, 0, 0, (time.perf_counter() - t0) * 1000.0)
            meta, raw = self._read_kwin_pipe(lambda fd: iface.CaptureWorkspace(opts, fd))
            return self._decode_kwin_raw(meta, raw, 0, 0, (time.perf_counter() - t0) * 1000.0)

        # CLI fallback path (grim / spectacle / maim / scrot)
        return self._capture_cli(scope=scope, area=area, active_rect=active_rect, t0=t0)

    def _capture_cli(
        self,
        *,
        scope: str,
        area: tuple[int, int, int, int] | None,
        active_rect: tuple[int, int, int, int] | None,
        t0: float,
    ) -> Frame:
        import gi
        gi.require_version("GdkPixbuf", "2.0")
        from gi.repository import GdkPixbuf

        crop_rect = area if scope == "area" else (active_rect if scope == "active" else None)
        with tempfile.TemporaryDirectory(prefix="agcu_ss_") as td:
            out_path = os.path.join(td, "shot.png")
            if self._backend == "grim":
                cmd = ["grim"]
                if crop_rect:
                    cmd += ["-g", f"{crop_rect[0]},{crop_rect[1]} {crop_rect[2]}x{crop_rect[3]}"]
                cmd.append(out_path)
                subprocess.run(cmd, check=True, capture_output=True, timeout=5)
                ox, oy = (crop_rect[0], crop_rect[1]) if crop_rect else (0, 0)
            elif self._backend == "spectacle":
                cmd = ["spectacle", "-b", "-n", "-f", "-o", out_path]
                subprocess.run(cmd, check=True, capture_output=True, timeout=5)
                ox, oy = 0, 0
            elif self._backend == "maim":
                cmd = ["maim"]
                if crop_rect:
                    cmd += ["-g", f"{crop_rect[2]}x{crop_rect[3]}+{crop_rect[0]}+{crop_rect[1]}"]
                cmd.append(out_path)
                subprocess.run(cmd, check=True, capture_output=True, timeout=5)
                ox, oy = (crop_rect[0], crop_rect[1]) if crop_rect else (0, 0)
            elif self._backend == "scrot":
                subprocess.run(["scrot", out_path], check=True, capture_output=True, timeout=5)
                ox, oy = 0, 0
            else:
                raise RuntimeError("No screen capture backend available")

            pb = GdkPixbuf.Pixbuf.new_from_file(out_path)
            if not pb.get_has_alpha():
                pb = pb.add_alpha(False, 0, 0, 0)
            w, h, rs = pb.get_width(), pb.get_height(), pb.get_rowstride()
            arr = np.frombuffer(pb.get_pixels(), dtype=np.uint8).reshape((h, rs // 4, 4))[:, :w, :]
            rgba = np.ascontiguousarray(arr)
            if crop_rect and ox == 0 and oy == 0 and self._backend in ("spectacle", "scrot"):
                x0 = max(0, min(w - 1, crop_rect[0]))
                y0 = max(0, min(h - 1, crop_rect[1]))
                x1 = max(x0 + 1, min(w, crop_rect[0] + crop_rect[2]))
                y1 = max(y0 + 1, min(h, crop_rect[1] + crop_rect[3]))
                rgba = np.ascontiguousarray(rgba[y0:y1, x0:x1, :])
                ox, oy = x0, y0
                h, w = rgba.shape[:2]
            return Frame(rgba=rgba, origin_x=ox, origin_y=oy, width=w, height=h, scale=1.0,
                         backend=self._backend, capture_ms=(time.perf_counter() - t0) * 1000.0)

    def _thumbnail(self, frame: Frame) -> np.ndarray:
        """Fast 1/8 stride subsample for < 2 ms frame-to-frame pixel diffing."""
        step = max(1, min(frame.width, frame.height) // 160)
        return frame.rgba[::step, ::step, :3].astype(np.int16, copy=False)

    def diff_frames(self, before: Frame, after: Frame) -> dict[str, Any]:
        """Compute changed pixel percentage and screen-space bounding box in < 4 ms."""
        if before.width != after.width or before.height != after.height:
            return {"changed_pct": 100.0, "changed_bbox": [after.origin_x, after.origin_y, after.width, after.height]}
        step = max(1, min(after.width, after.height) // 180)
        a = before.rgba[::step, ::step, :3].astype(np.int16)
        b = after.rgba[::step, ::step, :3].astype(np.int16)
        diff = np.abs(a - b).max(axis=2) > 8
        total = diff.size
        changed = int(diff.sum())
        if changed == 0:
            return {"changed_pct": 0.0, "changed_bbox": None}
        ys, xs = np.nonzero(diff)
        x0 = after.origin_x + int(xs.min()) * step
        y0 = after.origin_y + int(ys.min()) * step
        x1 = after.origin_x + min(after.width, int(xs.max() + 1) * step)
        y1 = after.origin_y + min(after.height, int(ys.max() + 1) * step)
        return {
            "changed_pct": round(100.0 * changed / max(1, total), 2),
            "changed_bbox": [x0, y0, max(1, x1 - x0), max(1, y1 - y0)],
        }

    def diff_from_last(self, frame: Frame) -> dict[str, Any] | None:
        rect = (frame.origin_x, frame.origin_y, frame.width, frame.height)
        thumb = self._thumbnail(frame)
        res = None
        if self._last_thumb is not None and self._last_frame_rect == rect and self._last_thumb.shape == thumb.shape:
            diff = np.abs(self._last_thumb - thumb).max(axis=2) > 8
            changed = int(diff.sum())
            pct = round(100.0 * changed / max(1, diff.size), 2)
            bbox = None
            if changed > 0:
                step = max(1, min(frame.width, frame.height) // 160)
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

    def wait_until_settled(
        self,
        *,
        scope: str = "screen",
        area: tuple[int, int, int, int] | None = None,
        active_rect: tuple[int, int, int, int] | None = None,
        max_wait_ms: int = 320,
        poll_ms: int = 45,
        include_cursor: bool = True,
    ) -> Frame:
        """Poll rapidly until consecutive frames stabilize (or max_wait_ms expires)."""
        deadline = time.monotonic() + (max_wait_ms / 1000.0)
        prev = self.capture(scope=scope, area=area, active_rect=active_rect, include_cursor=include_cursor)
        while time.monotonic() < deadline:
            time.sleep(poll_ms / 1000.0)
            cur = self.capture(scope=scope, area=area, active_rect=active_rect, include_cursor=include_cursor)
            d = self.diff_frames(prev, cur)
            prev = cur
            if d["changed_pct"] < 0.08:
                break
        return prev

    def wait_for_change(
        self,
        *,
        area: tuple[int, int, int, int] | None = None,
        timeout_ms: int = 5000,
        threshold_pct: float = 0.15,
    ) -> dict[str, Any]:
        """Poll fast CaptureArea until screen pixels change by >= threshold_pct."""
        scope = "area" if area else "screen"
        t0 = time.monotonic()
        deadline = t0 + (timeout_ms / 1000.0)
        base = self.capture(scope=scope, area=area, include_cursor=False)
        while time.monotonic() < deadline:
            time.sleep(0.03)
            cur = self.capture(scope=scope, area=area, include_cursor=False)
            d = self.diff_frames(base, cur)
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
        """Scale, optionally overlay Cairo Set-of-Marks / grid / cursor, and encode JPEG/PNG."""
        import gi
        gi.require_version("GdkPixbuf", "2.0")
        from gi.repository import GdkPixbuf, GLib

        t0 = time.perf_counter()
        src_w, src_h = frame.width, frame.height
        if max_width and src_w > max_width:
            out_w = int(max_width)
            out_h = max(1, int(round(src_h * (out_w / src_w))))
        else:
            out_w, out_h = src_w, src_h
        scale_x = out_w / max(1, src_w)
        scale_y = out_h / max(1, src_h)

        # Strip unused alpha channel up front: 25% less memory for scaling and required by glycin's JPEG encoder (Rgb8)
        rgb = np.ascontiguousarray(frame.rgba[:, :, :3])
        pb = GdkPixbuf.Pixbuf.new_from_bytes(
            GLib.Bytes.new(rgb.tobytes()), GdkPixbuf.Colorspace.RGB, False, 8, src_w, src_h, src_w * 3
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

        out_fmt = "png" if fmt.lower() == "png" else "jpeg"
        if out_fmt == "jpeg":
            q = str(max(20, min(95, int(quality))))
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
            "screen_size": [src_w, src_h],
            "scale_to_screen": round(src_w / max(1, out_w), 4),
            "capture_ms": round(frame.capture_ms, 1),
            "encode_ms": round(encode_ms, 1),
            "total_ms": round(frame.capture_ms + encode_ms, 1),
        }

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
        """Draw crisp Set-of-Marks [ref] badges and/or a 0..1000 normalized coordinate grid."""
        import cairo
        from gi.repository import GdkPixbuf, GLib

        w, h, rs = pb.get_width(), pb.get_height(), pb.get_rowstride()
        nch = pb.get_n_channels()
        # GdkPixbuf is RGB or RGBA; Cairo ARGB32 on little-endian is BGRA
        raw = np.frombuffer(pb.get_pixels(), dtype=np.uint8).reshape((h, rs))[:, : w * nch].reshape((h, w, nch))
        bgra = np.empty((h, w, 4), dtype=np.uint8)
        bgra[:, :, 0] = raw[:, :, 2]
        bgra[:, :, 1] = raw[:, :, 1]
        bgra[:, :, 2] = raw[:, :, 0]
        bgra[:, :, 3] = 255
        surf = cairo.ImageSurface.create_for_data(bgra, cairo.FORMAT_ARGB32, w, h, w * 4)
        ctx = cairo.Context(surf)
        ctx.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)

        if grid:
            ctx.set_line_width(1.0)
            ctx.set_font_size(11.0)
            for n in range(100, 1000, 100):
                gx = round(w * n / 1000.0) + 0.5
                gy = round(h * n / 1000.0) + 0.5
                ctx.set_source_rgba(0.0, 0.85, 1.0, 0.28)
                ctx.move_to(gx, 0)
                ctx.line_to(gx, h)
                ctx.move_to(0, gy)
                ctx.line_to(w, gy)
                ctx.stroke()
                # Subtle edge coordinate labels (0..1000 normalized)
                lbl = str(n)
                ctx.set_source_rgba(0.05, 0.05, 0.08, 0.72)
                ctx.rectangle(gx - 12, 2, 26, 13)
                ctx.rectangle(2, gy - 7, 26, 13)
                ctx.fill()
                ctx.set_source_rgba(0.4, 0.95, 1.0, 0.95)
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
                ctx.set_source_rgba(r, g, b, 0.88)
                ctx.rectangle(rx + 0.5, ry + 0.5, max(2.0, rw), max(2.0, rh))
                ctx.stroke()

                label = str(ref)
                ext = ctx.text_extents(label)
                pw = ext.width + 8
                ph = 15
                px = max(1.0, min(w - pw - 1.0, rx))
                py = max(1.0, ry - ph if ry >= ph + 1 else ry + 1)
                ctx.set_source_rgba(r, g, b, 0.94)
                ctx.rectangle(px, py, pw, ph)
                ctx.fill()
                ctx.set_source_rgba(1.0, 1.0, 1.0, 1.0)
                ctx.move_to(px + 4, py + 11.5)
                ctx.show_text(label)

        if cursor is not None:
            cx = (cursor[0] - origin_x) * scale_x
            cy = (cursor[1] - origin_y) * scale_y
            if 0 <= cx <= w and 0 <= cy <= h:
                ctx.set_line_width(2.0)
                ctx.set_source_rgba(1.0, 0.2, 0.2, 0.9)
                ctx.arc(cx, cy, 7.0, 0, 6.283185)
                ctx.stroke()

        surf.flush()
        rgb_out = np.ascontiguousarray(bgra[:, :, [2, 1, 0]])
        return GdkPixbuf.Pixbuf.new_from_bytes(
            GLib.Bytes.new(rgb_out.tobytes()), GdkPixbuf.Colorspace.RGB, False, 8, w, h, w * 3
        )
