"""Live click-through custom AI agent cursor and click ripple overlay (Linux Wayland + Windows + macOS).

Shows a ChatGPT-Operator-style glowing cyan pointer halo, compact status pill ("AI", "Click",
"Type", "Drag"), and expanding animated click ripples while the agent moves and clicks,
then automatically fades out when idle.

Backends:
- Linux Wayland (KDE Plasma 6, Hyprland, Sway, River, COSMIC): pure-Python wire client
  for zwlr_layer_shell_v1 (LAYER_OVERLAY, set_size(0,0) + anchor=15, keyboard_interactivity=NONE,
  empty wl_region for 100% click-through) + double-buffered wl_shm memfd + Cairo partial dirty-rect draw.
- Windows 10/11: pure-ctypes WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOPMOST |
  WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE + WDA_EXCLUDEFROMCAPTURE floating 160x160 persistent
  per-pixel alpha DIB updated via UpdateLayeredWindow (NumPy-vectorized, 64-bit safe).
- macOS 13/14/15/26 (Apple Silicon arm64 & Intel x86_64): dedicated main-thread AppKit
  NSWindow helper subprocess (NSApplicationActivationPolicyAccessory, NSScreenSaverWindowLevel,
  ignoresMouseEvents=True, NSWindowSharingNone, CanJoinAllSpaces | FullScreenAuxiliary) fed over
  stdin JSON with immutable CFData-backed premultiplied BGRA CGImage on CALayer.contents.
"""

from __future__ import annotations

import array
import atexit
import ctypes
import json
import math
import mmap
import os
import select
import socket
import struct
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any

import numpy as np


@dataclass
class _Ripple:
    x: float
    y: float
    started_at: float
    color: tuple[float, float, float]  # RGB 0..1
    double: bool = False


def _wl_str(s: str) -> bytes:
    b = s.encode("utf-8") + b"\x00"
    pad = (4 - (len(b) % 4)) % 4
    return struct.pack("<I", len(b)) + b + (b"\x00" * pad)


def _wl_msg(oid: int, opcode: int, payload: bytes = b"") -> bytes:
    size = 8 + len(payload)
    return struct.pack("<II", oid, (size << 16) | (opcode & 0xFFFF)) + payload


def _rect_union(
    a: tuple[int, int, int, int] | None,
    b: tuple[int, int, int, int] | None,
) -> tuple[int, int, int, int] | None:
    if a is None:
        return b
    if b is None:
        return a
    x0 = min(a[0], b[0])
    y0 = min(a[1], b[1])
    x1 = max(a[0] + a[2], b[0] + b[2])
    y1 = max(a[1] + a[3], b[1] + b[3])
    return (x0, y0, x1 - x0, y1 - y0)


def _draw_cairo_cursor_and_ripples(
    ctx,
    *,
    cx: float,
    cy: float,
    ox: float,
    oy: float,
    alpha: float,
    label: str,
    ripples: list[_Ripple],
    now: float,
) -> None:
    """Draw glowing custom AI cursor, badge pill, and expanding click ripples via Cairo."""
    import cairo

    for rp in ripples:
        age = now - rp.started_at
        if age < 0 or age > 0.34:
            continue
        t = age / 0.34
        ease = 1.0 - (1.0 - t) ** 3
        r, g, b = rp.color
        rx = rp.x - ox
        ry = rp.y - oy
        radius = 6.0 + 34.0 * ease
        fade = (1.0 - t) * 0.9
        ctx.set_line_width(3.0 * (1.0 - 0.55 * t))
        ctx.set_source_rgba(r, g, b, fade)
        ctx.arc(rx, ry, radius, 0, math.tau)
        ctx.stroke()

        if t < 0.45:
            ctx.set_source_rgba(r, g, b, (0.45 - t) * 0.65)
            ctx.arc(rx, ry, 5.0 + 12.0 * ease, 0, math.tau)
            ctx.fill()

        if rp.double and age > 0.07:
            t2 = min(1.0, (age - 0.07) / 0.27)
            e2 = 1.0 - (1.0 - t2) ** 3
            ctx.set_line_width(2.2 * (1.0 - 0.5 * t2))
            ctx.set_source_rgba(r, g, b, (1.0 - t2) * 0.85)
            ctx.arc(rx, ry, 4.0 + 26.0 * e2, 0, math.tau)
            ctx.stroke()

    if alpha <= 0.01:
        return

    px = cx - ox
    py = cy - oy

    ctx.set_source_rgba(0.14, 0.72, 0.98, 0.22 * alpha)
    ctx.arc(px, py, 14.0, 0, math.tau)
    ctx.fill()
    ctx.set_line_width(1.6)
    ctx.set_source_rgba(0.22, 0.78, 0.99, 0.85 * alpha)
    ctx.arc(px, py, 9.0, 0, math.tau)
    ctx.stroke()

    ctx.save()
    ctx.translate(px, py)
    ctx.move_to(0.0, 0.0)
    ctx.line_to(0.0, 21.0)
    ctx.line_to(5.4, 15.8)
    ctx.line_to(9.8, 24.0)
    ctx.line_to(13.0, 22.3)
    ctx.line_to(8.8, 14.2)
    ctx.line_to(15.6, 13.6)
    ctx.close_path()
    ctx.set_line_width(2.6)
    ctx.set_line_join(cairo.LINE_JOIN_ROUND)
    ctx.set_source_rgba(0.05, 0.09, 0.16, 0.92 * alpha)
    ctx.stroke_preserve()
    ctx.set_source_rgba(0.22, 0.78, 0.99, 0.98 * alpha)
    ctx.fill()
    ctx.restore()

    if label:
        ctx.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        ctx.set_font_size(11.0)
        ext = ctx.text_extents(label)
        pw = max(24.0, ext.width + 14.0)
        ph = 18.0
        bx = px + 16.0
        by = py + 15.0
        rad = 6.0
        ctx.new_sub_path()
        ctx.arc(bx + pw - rad, by + rad, rad, -math.pi / 2, 0)
        ctx.arc(bx + pw - rad, by + ph - rad, rad, 0, math.pi / 2)
        ctx.arc(bx + rad, by + ph - rad, rad, math.pi / 2, math.pi)
        ctx.arc(bx + rad, by + rad, rad, math.pi, 3 * math.pi / 2)
        ctx.close_path()
        ctx.set_source_rgba(0.06, 0.10, 0.18, 0.88 * alpha)
        ctx.fill_preserve()
        ctx.set_line_width(1.2)
        ctx.set_source_rgba(0.22, 0.78, 0.99, 0.90 * alpha)
        ctx.stroke()

        ctx.set_source_rgba(0.95, 0.98, 1.0, 0.98 * alpha)
        ctx.move_to(bx + 7.0, by + 13.0)
        ctx.show_text(label)


# ---------------------------------------------------------------------------
# Linux Wayland zwlr_layer_shell_v1 zero-dep click-through overlay
# ---------------------------------------------------------------------------


class _WaylandLayerOverlay:
    """Pure-Python Wayland zwlr_layer_shell_v1 overlay with double-buffered wl_shm + Cairo."""

    def __init__(self):
        self._w = 0
        self._h = 0
        self._buf_w = 0
        self._buf_h = 0
        self._sock: socket.socket | None = None
        self._rx_buf = b""
        self._next_id = 2
        self._globals: dict[str, tuple[int, int]] = {}
        self._shm_id = 0
        self._surface_id = 0
        self._layer_id = 0
        self._pool_id = 0
        self._buffers: list[int] = []
        self._buf_busy: list[bool] = [False, False]
        self._buf_dirty: list[tuple[int, int, int, int] | None] = [None, None]
        self._committed_dirty: tuple[int, int, int, int] | None = None
        self._memfd: int = -1
        self._mm: mmap.mmap | None = None
        self._views: list[memoryview] = []
        self._surfaces: list[Any] = []
        self._frame_size = 0
        self._configured = False
        self._needs_realloc = False
        self._closed = False
        self._ready = self._connect()

    @property
    def ready(self) -> bool:
        return self._ready and not self._closed

    def _alloc_id(self) -> int:
        oid = self._next_id
        self._next_id += 1
        return oid

    def _send(self, data: bytes, fds: list[int] | None = None) -> None:
        if self._sock is None or self._closed:
            return
        try:
            if fds:
                anc = [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", fds).tobytes())]
                sent = self._sock.sendmsg([data], anc)
                if sent < len(data):
                    self._sock.sendall(data[sent:])
            else:
                self._sock.sendall(data)
        except OSError:
            self._closed = True

    def _pump(self, timeout: float = 0.0) -> None:
        if self._sock is None or self._closed:
            return
        first = True
        while not self._closed:
            r, _, _ = select.select([self._sock], [], [], timeout if first else 0.0)
            first = False
            if not r:
                break
            try:
                chunk = self._sock.recv(16384)
            except OSError:
                self._closed = True
                return
            if not chunk:
                self._closed = True
                return
            self._rx_buf += chunk
            off = 0
            buf = self._rx_buf
            while off + 8 <= len(buf):
                oid, sz_op = struct.unpack_from("<II", buf, off)
                sz = sz_op >> 16
                op = sz_op & 0xFFFF
                if sz < 8:
                    self._closed = True
                    return
                if off + sz > len(buf):
                    break
                payload = buf[off + 8 : off + sz]
                off += sz
                self._handle_event(oid, op, payload)
            self._rx_buf = buf[off:]

    def _handle_event(self, oid: int, op: int, payload: bytes) -> None:
        if oid == 1 and op == 0:  # wl_display.error
            self._closed = True
            return
        if oid == self._reg_id and op == 0:  # wl_registry.global
            name, slen = struct.unpack_from("<II", payload, 0)
            iface = payload[8 : 8 + slen - 1].decode("utf-8", "replace")
            pad = (slen + 3) & ~3
            ver = struct.unpack_from("<I", payload, 8 + pad)[0]
            self._globals[iface] = (name, ver)
        elif oid == self._sync_id and op == 0:
            self._sync_done = True
        elif oid == self._layer_id and op == 0:  # zwlr_layer_surface_v1.configure
            serial, cw, ch = struct.unpack_from("<III", payload, 0)
            if cw > 0 and ch > 0 and (cw != self._w or ch != self._h):
                self._w, self._h = cw, ch
                if self._buf_w > 0:
                    self._needs_realloc = True
            self._send(_wl_msg(self._layer_id, 6, struct.pack("<I", serial)))  # ack_configure
            self._configured = True
        elif oid == self._layer_id and op == 1:  # closed
            self._closed = True
        elif oid in self._buffers and op == 0:  # wl_buffer.release
            self._buf_busy[self._buffers.index(oid)] = False

    def _destroy_shm(self) -> None:
        for s in self._surfaces:
            try:
                s.finish()
            except Exception:
                pass
        self._surfaces = []
        for v in self._views:
            try:
                v.release()
            except Exception:
                pass
        self._views = []
        if not self._closed and self._sock is not None:
            for bid in self._buffers:
                self._send(_wl_msg(bid, 0))  # wl_buffer.destroy
            if self._pool_id:
                self._send(_wl_msg(self._pool_id, 1))  # wl_shm_pool.destroy
        self._buffers = []
        self._pool_id = 0
        self._buf_busy = [False, False]
        self._buf_dirty = [None, None]
        self._committed_dirty = None
        if self._mm is not None:
            try:
                self._mm.close()
            except Exception:
                pass
            self._mm = None
        if self._memfd >= 0:
            try:
                os.close(self._memfd)
            except Exception:
                pass
            self._memfd = -1

    def _alloc_shm(self, w: int, h: int) -> bool:
        import cairo

        self._destroy_shm()
        stride = w * 4
        self._frame_size = stride * h
        total = self._frame_size * 2
        self._memfd = os.memfd_create("agcu_overlay", os.MFD_CLOEXEC)
        os.ftruncate(self._memfd, total)
        self._mm = mmap.mmap(self._memfd, total, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE)

        self._pool_id = self._alloc_id()
        self._send(_wl_msg(self._shm_id, 0, struct.pack("<Ii", self._pool_id, total)), fds=[self._memfd])
        if self._closed:
            return False

        for idx in range(2):
            bid = self._alloc_id()
            offset = idx * self._frame_size
            self._send(_wl_msg(self._pool_id, 0, struct.pack("<IiiiiI", bid, offset, w, h, stride, 0)))
            self._buffers.append(bid)
            view = memoryview(self._mm)[offset : offset + self._frame_size]
            self._views.append(view)
            self._surfaces.append(cairo.ImageSurface.create_for_data(view, cairo.FORMAT_ARGB32, w, h, stride))
            del view
        self._buf_w, self._buf_h = w, h
        self._needs_realloc = False
        return True

    def _connect(self) -> bool:
        try:
            import cairo  # noqa: F401
        except Exception:
            return False
        xdg = os.environ.get("XDG_RUNTIME_DIR", "")
        disp = os.environ.get("WAYLAND_DISPLAY", "")
        if not xdg or not disp:
            return False
        sock_path = disp if os.path.isabs(disp) else os.path.join(xdg, disp)
        if not os.path.exists(sock_path):
            return False
        try:
            self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._sock.settimeout(1.0)
            self._sock.connect(sock_path)
            self._reg_id = self._alloc_id()
            self._sync_id = self._alloc_id()
            self._sync_done = False
            self._send(
                _wl_msg(1, 1, struct.pack("<I", self._reg_id))
                + _wl_msg(1, 0, struct.pack("<I", self._sync_id))
            )
            deadline = time.monotonic() + 0.6
            while not self._sync_done and not self._closed and time.monotonic() < deadline:
                self._pump(0.05)

            for req in ("wl_compositor", "wl_shm", "zwlr_layer_shell_v1"):
                if req not in self._globals:
                    self.close()
                    return False

            def bind(iface: str, max_ver: int) -> int:
                name, ver = self._globals[iface]
                v = min(ver, max_ver)
                nid = self._alloc_id()
                self._send(_wl_msg(self._reg_id, 0, struct.pack("<I", name) + _wl_str(iface) + struct.pack("<II", v, nid)))
                return nid

            comp_id = bind("wl_compositor", 4)
            self._shm_id = bind("wl_shm", 1)
            shell_id = bind("zwlr_layer_shell_v1", 4)

            self._surface_id = self._alloc_id()
            empty_region = self._alloc_id()
            self._layer_id = self._alloc_id()

            # Per wlr-layer-shell spec: anchor to all 4 edges (15) with set_size(0, 0) so compositor picks exact output bounds
            msgs = (
                _wl_msg(comp_id, 0, struct.pack("<I", self._surface_id))
                + _wl_msg(comp_id, 1, struct.pack("<I", empty_region))
                + _wl_msg(self._surface_id, 5, struct.pack("<I", empty_region))  # set_input_region(empty)
                + _wl_msg(empty_region, 0)  # wl_region.destroy
                + _wl_msg(
                    shell_id, 0,
                    struct.pack("<IIII", self._layer_id, self._surface_id, 0, 3) + _wl_str("antigravity-cursor"),
                )
                + _wl_msg(self._layer_id, 0, struct.pack("<II", 0, 0))  # set_size(0, 0)
                + _wl_msg(self._layer_id, 1, struct.pack("<I", 15))     # anchor TOP|BOTTOM|LEFT|RIGHT
                + _wl_msg(self._layer_id, 2, struct.pack("<i", -1))     # exclusive_zone = -1
                + _wl_msg(self._layer_id, 4, struct.pack("<I", 0))      # keyboard_interactivity = NONE
                + _wl_msg(self._surface_id, 6)                          # commit
            )
            self._send(msgs)

            deadline = time.monotonic() + 0.8
            while not self._configured and not self._closed and time.monotonic() < deadline:
                self._pump(0.04)
            if not self._configured or self._w <= 0 or self._h <= 0:
                self.close()
                return False

            if not self._alloc_shm(self._w, self._h):
                self.close()
                return False
            return True
        except Exception:
            self.close()
            return False

    def render(self, cx: float, cy: float, alpha: float, label: str, ripples: list[_Ripple], now: float) -> tuple[int, int, int, int] | None:
        if not self.ready:
            return None
        import cairo

        self._pump(0.0)
        if self._closed:
            return None
        if self._needs_realloc and self._w > 0 and self._h > 0:
            if not self._alloc_shm(self._w, self._h):
                return None

        # Compute bounding box of everything about to be drawn
        new_dirty: tuple[int, int, int, int] | None = None
        xs: list[float] = []
        ys: list[float] = []
        if alpha > 0.01:
            xs.extend([cx - 22, cx + 150])
            ys.extend([cy - 22, cy + 48])
        for rp in ripples:
            if 0.0 <= (now - rp.started_at) <= 0.34:
                xs.extend([rp.x - 46, rp.x + 46])
                ys.extend([rp.y - 46, rp.y + 46])
        if xs and ys:
            x0 = max(0, int(math.floor(min(xs))))
            y0 = max(0, int(math.floor(min(ys))))
            x1 = min(self._buf_w, int(math.ceil(max(xs))))
            y1 = min(self._buf_h, int(math.ceil(max(ys))))
            if x1 > x0 and y1 > y0:
                new_dirty = (x0, y0, x1 - x0, y1 - y0)

        # Clean unmap when hidden/faded out (prevents frozen ghost cursor on fade-out!)
        if new_dirty is None:
            if self._committed_dirty is not None:
                self._send(
                    _wl_msg(self._surface_id, 1, struct.pack("<Iii", 0, 0, 0))  # attach(NULL)
                    + _wl_msg(self._surface_id, 6)                              # commit
                )
                self._committed_dirty = None
            return None

        # Pick a free buffer slot; if both momentarily busy, skip frame instead of tearing mid-scanout
        if not self._buf_busy[0]:
            slot = 0
        elif not self._buf_busy[1]:
            slot = 1
        else:
            return self._committed_dirty

        surf = self._surfaces[slot]
        ctx = cairo.Context(surf)
        slot_old = self._buf_dirty[slot]

        if slot_old is not None:
            ctx.save()
            ctx.set_operator(cairo.OPERATOR_CLEAR)
            ctx.rectangle(*slot_old)
            ctx.fill()
            ctx.restore()

        ctx.save()
        ctx.rectangle(*new_dirty)
        ctx.clip()
        _draw_cairo_cursor_and_ripples(ctx, cx=cx, cy=cy, ox=0.0, oy=0.0, alpha=alpha, label=label, ripples=ripples, now=now)
        ctx.restore()
        surf.flush()
        self._buf_dirty[slot] = new_dirty

        # Damage MUST cover union(previously committed screen dirty rect, new_dirty), NOT just this slot's old rect
        dmg = _rect_union(self._committed_dirty, new_dirty)
        assert dmg is not None
        self._committed_dirty = new_dirty
        self._buf_busy[slot] = True
        self._send(
            _wl_msg(self._surface_id, 1, struct.pack("<Iii", self._buffers[slot], 0, 0))  # attach
            + _wl_msg(self._surface_id, 2, struct.pack("<iiii", *dmg))                    # damage
            + _wl_msg(self._surface_id, 6)                                                # commit
        )
        return new_dirty

    def close(self) -> None:
        was_open = not self._closed
        self._ready = False
        self._destroy_shm()
        if was_open and self._sock is not None:
            try:
                if self._layer_id:
                    self._send(_wl_msg(self._layer_id, 7))  # zwlr_layer_surface_v1.destroy
                if self._surface_id:
                    self._send(_wl_msg(self._surface_id, 0))  # wl_surface.destroy
            except Exception:
                pass
        self._closed = True
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None


# ---------------------------------------------------------------------------
# Windows 10/11 pure-ctypes WS_EX_LAYERED | WS_EX_TRANSPARENT cursor overlay
# ---------------------------------------------------------------------------

# Precomputed pixel coordinate grid for fast NumPy-vectorized 160x160 Win32 overlay rendering
_WIN_OV_SIZE = 160
_WIN_OV_YY, _WIN_OV_XX = np.mgrid[0:_WIN_OV_SIZE, 0:_WIN_OV_SIZE].astype(np.float32)


def _draw_win32_argb_buffer(
    buf: bytearray | np.ndarray,
    size: int,
    *,
    cx: float,
    cy: float,
    win_x: int,
    win_y: int,
    alpha: float,
    ripples: list[_Ripple],
    now: float,
) -> None:
    """Fast NumPy-vectorized premultiplied BGRA cursor halo, arrow, and click ripples for Win32 UpdateLayeredWindow."""
    if isinstance(buf, np.ndarray):
        arr = buf
    else:
        arr = np.frombuffer(buf, dtype=np.uint8).reshape((size, size, 4))
    arr.fill(0)

    if size == _WIN_OV_SIZE:
        xx, yy = _WIN_OV_XX, _WIN_OV_YY
    else:
        yy, xx = np.mgrid[0:size, 0:size].astype(np.float32)

    out_b = np.zeros((size, size), dtype=np.float32)
    out_g = np.zeros((size, size), dtype=np.float32)
    out_r = np.zeros((size, size), dtype=np.float32)
    out_a = np.zeros((size, size), dtype=np.float32)

    def blend_mask(cov: np.ndarray, r: float, g: float, b: float) -> None:
        m = np.clip(cov, 0.0, 1.0)
        inv = 1.0 - m
        out_r[:] = r * m + out_r * inv
        out_g[:] = g * m + out_g * inv
        out_b[:] = b * m + out_b * inv
        out_a[:] = 255.0 * m + out_a * inv

    def draw_ring(rcx: float, rcy: float, radius: float, thickness: float, rgb: tuple[float, float, float], a: float) -> None:
        d = np.hypot(xx - rcx, yy - rcy)
        edge = (thickness * 0.5) - np.abs(d - radius)
        cov = np.clip((edge + 0.8) / 1.4, 0.0, 1.0) * a
        blend_mask(cov, rgb[0], rgb[1], rgb[2])

    for rp in ripples:
        age = now - rp.started_at
        if age < 0 or age > 0.34:
            continue
        t = age / 0.34
        ease = 1.0 - (1.0 - t) ** 3
        rgb = (rp.color[0] * 255.0, rp.color[1] * 255.0, rp.color[2] * 255.0)
        draw_ring(rp.x - win_x, rp.y - win_y, 6.0 + 34.0 * ease, 3.2 * (1.0 - 0.5 * t), rgb, (1.0 - t) * 0.92)
        if rp.double and age > 0.07:
            t2 = min(1.0, (age - 0.07) / 0.27)
            e2 = 1.0 - (1.0 - t2) ** 3
            draw_ring(rp.x - win_x, rp.y - win_y, 4.0 + 26.0 * e2, 2.2, rgb, (1.0 - t2) * 0.85)

    if alpha > 0.01:
        px = cx - win_x
        py = cy - win_y
        draw_ring(px, py, 9.5, 2.0, (56.0, 189.0, 248.0), 0.90 * alpha)
        draw_ring(px, py, 13.5, 3.0, (14.0, 165.0, 233.0), 0.28 * alpha)

        # Crisp pointer arrow (24x24 ROI around hotspot)
        ix0 = max(0, int(px) - 2)
        iy0 = max(0, int(py) - 2)
        ix1 = min(size, int(px) + 18)
        iy1 = min(size, int(py) + 24)
        if ix1 > ix0 and iy1 > iy0:
            rx = xx[iy0:iy1, ix0:ix1] - px
            ry = yy[iy0:iy1, ix0:ix1] - py
            # Pointer triangle: (0,0) -> (0,20) -> (5.5,15) -> (14.5,13.5)
            in_tri1 = (rx >= 0.0) & (ry >= 0.0) & (ry <= 20.0 - 0.9 * rx) & (ry >= 0.93 * rx)
            in_tri2 = (rx >= 0.0) & (ry >= 0.93 * rx) & (ry <= 13.5 + (15.0 - 13.5) * (14.5 - rx) / 9.0) & (rx <= 14.5)
            inside = in_tri1 | in_tri2
            # Dark 1px outline via simple dilation
            outline = np.zeros_like(inside)
            outline[1:, :] |= inside[:-1, :]
            outline[:-1, :] |= inside[1:, :]
            outline[:, 1:] |= inside[:, :-1]
            outline[:, :-1] |= inside[:, 1:]
            border = outline & (~inside)
            for mask, col in ((border, (15.0, 23.0, 42.0, 0.92 * alpha)), (inside, (56.0, 189.0, 248.0, 0.98 * alpha))):
                m = mask.astype(np.float32) * col[3]
                inv = 1.0 - m
                out_r[iy0:iy1, ix0:ix1] = col[0] * m + out_r[iy0:iy1, ix0:ix1] * inv
                out_g[iy0:iy1, ix0:ix1] = col[1] * m + out_g[iy0:iy1, ix0:ix1] * inv
                out_b[iy0:iy1, ix0:ix1] = col[2] * m + out_b[iy0:iy1, ix0:ix1] * inv
                out_a[iy0:iy1, ix0:ix1] = 255.0 * m + out_a[iy0:iy1, ix0:ix1] * inv

    arr[:, :, 0] = np.clip(out_b, 0, 255).astype(np.uint8)
    arr[:, :, 1] = np.clip(out_g, 0, 255).astype(np.uint8)
    arr[:, :, 2] = np.clip(out_r, 0, 255).astype(np.uint8)
    arr[:, :, 3] = np.clip(out_a, 0, 255).astype(np.uint8)


# Module-level hoisted Win32 structs & persistent WNDPROC (prevents UAF across multiple overlay instances)
_WIN32_CLS_REGISTERED: str | None = None
_WIN32_WNDPROC_REF: Any = None


class _WIN_POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_int32), ("y", ctypes.c_int32)]


class _WIN_SIZE(ctypes.Structure):
    _fields_ = [("cx", ctypes.c_int32), ("cy", ctypes.c_int32)]


class _WIN_BLENDFUNCTION(ctypes.Structure):
    _fields_ = [
        ("BlendOp", ctypes.c_ubyte),
        ("BlendFlags", ctypes.c_ubyte),
        ("SourceConstantAlpha", ctypes.c_ubyte),
        ("AlphaFormat", ctypes.c_ubyte),
    ]


class _WIN_BMIH(ctypes.Structure):
    _fields_ = [
        ("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32), ("biHeight", ctypes.c_int32),
        ("biPlanes", ctypes.c_uint16), ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
        ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_int32),
        ("biYPelsPerMeter", ctypes.c_int32), ("biClrUsed", ctypes.c_uint32), ("biClrImportant", ctypes.c_uint32),
    ]


class _Win32LayeredOverlay:
    """Floating 160x160 click-through Win32 layered window (64-bit safe, persistent DIB, WDA_EXCLUDEFROMCAPTURE)."""

    WIN_SIZE = _WIN_OV_SIZE

    def __init__(self):
        self._hwnd = None
        self._owner_tid = 0
        self._ready = False
        self._hdc_scr = None
        self._hdc_mem = None
        self._hbmp = None
        self._old_obj = None
        self._bits_ptr = ctypes.c_void_p()
        self._arr = np.zeros((self.WIN_SIZE, self.WIN_SIZE, 4), dtype=np.uint8)
        self._init_win32()

    @property
    def ready(self) -> bool:
        return self._ready and bool(self._hwnd)

    def _init_win32(self) -> None:
        global _WIN32_CLS_REGISTERED, _WIN32_WNDPROC_REF
        if sys.platform != "win32":
            return
        try:
            from ctypes import wintypes

            # Ensure DPI awareness even if overlay starts before WinWindowManager
            try:
                from win32_backend import ensure_dpi_awareness
                ensure_dpi_awareness()
            except Exception:
                pass

            user32 = ctypes.windll.user32
            gdi32 = ctypes.windll.gdi32
            kernel32 = ctypes.windll.kernel32
            self._user32 = user32
            self._gdi32 = gdi32
            self._kernel32 = kernel32

            # Explicit 64-bit safe argtypes & restype for EVERY Win32 API used
            kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
            kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE
            kernel32.GetCurrentThreadId.argtypes = []
            kernel32.GetCurrentThreadId.restype = wintypes.DWORD

            WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

            class WNDCLASSEXW(ctypes.Structure):
                _fields_ = [
                    ("cbSize", wintypes.UINT), ("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
                    ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE),
                    ("hIcon", wintypes.HICON), ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                    ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR), ("hIconSm", wintypes.HICON),
                ]

            user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
            user32.DefWindowProcW.restype = ctypes.c_ssize_t
            user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
            user32.RegisterClassExW.restype = wintypes.ATOM
            user32.CreateWindowExW.argtypes = [
                wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
            ]
            user32.CreateWindowExW.restype = wintypes.HWND
            user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
            user32.ShowWindow.restype = wintypes.BOOL
            user32.DestroyWindow.argtypes = [wintypes.HWND]
            user32.DestroyWindow.restype = wintypes.BOOL
            user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
            user32.PostMessageW.restype = wintypes.BOOL
            user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]
            user32.PeekMessageW.restype = wintypes.BOOL
            user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
            user32.TranslateMessage.restype = wintypes.BOOL
            user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
            user32.DispatchMessageW.restype = ctypes.c_ssize_t
            user32.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
            user32.SetWindowDisplayAffinity.restype = wintypes.BOOL
            user32.GetDC.argtypes = [wintypes.HWND]
            user32.GetDC.restype = wintypes.HDC
            user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
            user32.ReleaseDC.restype = ctypes.c_int
            user32.UpdateLayeredWindow.argtypes = [
                wintypes.HWND, wintypes.HDC, ctypes.POINTER(_WIN_POINT), ctypes.POINTER(_WIN_SIZE),
                wintypes.HDC, ctypes.POINTER(_WIN_POINT), wintypes.DWORD, ctypes.POINTER(_WIN_BLENDFUNCTION), wintypes.DWORD,
            ]
            user32.UpdateLayeredWindow.restype = wintypes.BOOL

            gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
            gdi32.CreateCompatibleDC.restype = wintypes.HDC
            gdi32.CreateDIBSection.argtypes = [
                wintypes.HDC, ctypes.c_void_p, wintypes.UINT,
                ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD,
            ]
            gdi32.CreateDIBSection.restype = wintypes.HBITMAP
            gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
            gdi32.SelectObject.restype = wintypes.HGDIOBJ
            gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
            gdi32.DeleteObject.restype = wintypes.BOOL
            gdi32.DeleteDC.argtypes = [wintypes.HDC]
            gdi32.DeleteDC.restype = wintypes.BOOL

            hinst = kernel32.GetModuleHandleW(None)
            if _WIN32_CLS_REGISTERED is None:
                WM_CLOSE = 0x0010

                def _proc(h, m, w, l):
                    if m == WM_CLOSE:
                        user32.DestroyWindow(h)
                        return 0
                    return user32.DefWindowProcW(h, m, w, l)

                _WIN32_WNDPROC_REF = WNDPROC(_proc)
                cls_name = f"AntigravityCUOverlay_{os.getpid()}"
                wc = WNDCLASSEXW()
                wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
                wc.lpfnWndProc = _WIN32_WNDPROC_REF
                wc.hInstance = hinst
                wc.lpszClassName = cls_name
                if user32.RegisterClassExW(ctypes.byref(wc)):
                    _WIN32_CLS_REGISTERED = cls_name
                else:
                    return

            WS_EX_LAYERED = 0x00080000
            WS_EX_TRANSPARENT = 0x00000020
            WS_EX_TOPMOST = 0x00000008
            WS_EX_TOOLWINDOW = 0x00000080
            WS_EX_NOACTIVATE = 0x08000000
            WS_POPUP = 0x80000000

            hwnd = user32.CreateWindowExW(
                WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE,
                _WIN32_CLS_REGISTERED, "", WS_POPUP,
                0, 0, self.WIN_SIZE, self.WIN_SIZE,
                None, None, hinst, None,
            )
            if not hwnd:
                return
            self._hwnd = hwnd
            self._owner_tid = int(kernel32.GetCurrentThreadId())

            # Exclude our overlay from BitBlt(CAPTUREBLT) screenshots so ripples never pollute diffs!
            try:
                user32.SetWindowDisplayAffinity(hwnd, 0x00000011)  # WDA_EXCLUDEFROMCAPTURE
            except Exception:
                pass

            # Allocate persistent 160x160 DIBSection once (zero 90Hz GDI churn)
            self._hdc_scr = user32.GetDC(None)
            self._hdc_mem = gdi32.CreateCompatibleDC(self._hdc_scr)
            bmi = _WIN_BMIH()
            bmi.biSize = ctypes.sizeof(_WIN_BMIH)
            bmi.biWidth = self.WIN_SIZE
            bmi.biHeight = -self.WIN_SIZE
            bmi.biPlanes = 1
            bmi.biBitCount = 32
            self._hbmp = gdi32.CreateDIBSection(self._hdc_mem, ctypes.byref(bmi), 0, ctypes.byref(self._bits_ptr), None, 0)
            if not self._hbmp or not self._bits_ptr.value:
                self.close()
                return
            self._old_obj = gdi32.SelectObject(self._hdc_mem, self._hbmp)
            user32.ShowWindow(hwnd, 4)  # SW_SHOWNOACTIVATE
            self._ready = True
        except Exception:
            self.close()

    def render(self, cx: float, cy: float, alpha: float, label: str, ripples: list[_Ripple], now: float) -> tuple[int, int, int, int] | None:
        if not self.ready:
            return None
        from ctypes import wintypes

        size = self.WIN_SIZE
        win_x = int(round(cx)) - size // 2
        win_y = int(round(cy)) - size // 2
        _draw_win32_argb_buffer(self._arr, size, cx=cx, cy=cy, win_x=win_x, win_y=win_y, alpha=alpha, ripples=ripples, now=now)

        msg = wintypes.MSG()
        while self._user32.PeekMessageW(ctypes.byref(msg), self._hwnd, 0, 0, 1):
            self._user32.TranslateMessage(ctypes.byref(msg))
            self._user32.DispatchMessageW(ctypes.byref(msg))

        ctypes.memmove(self._bits_ptr.value, self._arr.ctypes.data, self._arr.nbytes)
        pt_dst = _WIN_POINT(win_x, win_y)
        pt_src = _WIN_POINT(0, 0)
        sz = _WIN_SIZE(size, size)
        blend = _WIN_BLENDFUNCTION(0, 0, 255, 1)  # AC_SRC_OVER, AC_SRC_ALPHA
        self._user32.UpdateLayeredWindow(
            self._hwnd, self._hdc_scr, ctypes.byref(pt_dst), ctypes.byref(sz),
            self._hdc_mem, ctypes.byref(pt_src), 0, ctypes.byref(blend), 2,
        )
        return (win_x, win_y, size, size) if (alpha > 0.01 or ripples) else None

    def close(self) -> None:
        self._ready = False
        if self._old_obj and self._hdc_mem:
            try:
                self._gdi32.SelectObject(self._hdc_mem, self._old_obj)
            except Exception:
                pass
            self._old_obj = None
        if self._hbmp:
            try:
                self._gdi32.DeleteObject(self._hbmp)
            except Exception:
                pass
            self._hbmp = None
        if self._hdc_mem:
            try:
                self._gdi32.DeleteDC(self._hdc_mem)
            except Exception:
                pass
            self._hdc_mem = None
        if self._hdc_scr:
            try:
                self._user32.ReleaseDC(None, self._hdc_scr)
            except Exception:
                pass
            self._hdc_scr = None
        if self._hwnd:
            try:
                cur_tid = int(self._kernel32.GetCurrentThreadId())
                if cur_tid == self._owner_tid:
                    self._user32.DestroyWindow(self._hwnd)
                else:
                    self._user32.PostMessageW(self._hwnd, 0x0010, 0, 0)  # WM_CLOSE
            except Exception:
                pass
            self._hwnd = None


# ---------------------------------------------------------------------------
# macOS AppKit Floating Click-Through Overlay (main-thread subprocess helper)
# ---------------------------------------------------------------------------


class _MacAppKitOverlayProxy:
    """Spawn & control a dedicated main-thread AppKit NSWindow overlay subprocess on macOS.

    AppKit requires NSApplication/NSWindow to live on a process's main thread; isolating it in a
    tiny helper subprocess avoids main-thread SIGABRTs inside the stdio MCP server and degrades
    gracefully in headless SSH / CI sessions.
    """

    SIZE = _WIN_OV_SIZE

    def __init__(self) -> None:
        self.ready = False
        self._proc: subprocess.Popen | None = None
        self._init()

    def _init(self) -> None:
        if sys.platform != "darwin":
            return
        try:
            env = os.environ.copy()
            env["PYTHONUNBUFFERED"] = "1"
            self._proc = subprocess.Popen(
                [sys.executable, os.path.abspath(__file__), "--mac-overlay-helper"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=env,
            )
            if self._proc.stdout is None:
                self.close()
                return
            deadline = time.monotonic() + 4.0
            while time.monotonic() < deadline:
                if self._proc.poll() is not None:
                    break
                r, _, _ = select.select([self._proc.stdout], [], [], 0.05)
                if r:
                    raw_line = self._proc.stdout.readline()
                    if not raw_line:
                        break
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if line == "READY":
                        self.ready = True
                        return
                    if line == "HEADLESS":
                        break
            self.close()
        except Exception:
            self.close()

    def _send(self, payload: dict[str, Any]) -> bool:
        proc = self._proc
        if not self.ready or proc is None or proc.stdin is None or proc.poll() is not None:
            self.ready = False
            return False
        try:
            proc.stdin.write((json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8"))
            proc.stdin.flush()
            return True
        except Exception:
            self.ready = False
            return False

    def render(
        self,
        cx: float,
        cy: float,
        alpha: float,
        label: str,
        ripples: list[_Ripple],
        now: float,
    ) -> tuple[int, int, int, int] | None:
        if not self.ready:
            return None
        if alpha <= 0.01 and not ripples:
            self._send({"op": "hide"})
            return None
        win_x = int(round(cx)) - self.SIZE // 2
        win_y = int(round(cy)) - self.SIZE // 2
        rip_payload = [
            [round(rp.x, 1), round(rp.y, 1), round(max(0.0, now - rp.started_at), 4), rp.color[0], rp.color[1], rp.color[2], bool(rp.double)]
            for rp in ripples
        ]
        self._send({
            "op": "render",
            "cx": round(cx, 1),
            "cy": round(cy, 1),
            "win_x": win_x,
            "win_y": win_y,
            "alpha": round(alpha, 3),
            "ripples": rip_payload,
        })
        # Also return rect so diff_frames masks the overlay even on macOS 15+ ScreenCaptureKit
        return (win_x, win_y, self.SIZE, self.SIZE)

    def close(self) -> None:
        proc = self._proc
        self._proc = None
        self.ready = False
        if proc is not None:
            try:
                if proc.stdin:
                    try:
                        proc.stdin.write(b'{"op":"quit"}\n')
                        proc.stdin.flush()
                    except Exception:
                        pass
                    try:
                        proc.stdin.close()
                    except Exception:
                        pass
                try:
                    proc.wait(timeout=0.5)
                except Exception:
                    try:
                        proc.terminate()
                        proc.wait(timeout=0.5)
                    except Exception:
                        try:
                            proc.kill()
                            proc.wait(timeout=0.5)
                        except Exception:
                            pass
            finally:
                if proc.stdout:
                    try:
                        proc.stdout.close()
                    except Exception:
                        pass


def _run_mac_overlay_helper() -> int:
    """Main-thread macOS AppKit borderless click-through cursor overlay helper process."""
    if sys.platform != "darwin":
        return 1
    script_dir = os.path.dirname(os.path.abspath(__file__))
    if script_dir not in sys.path:
        sys.path.insert(0, script_dir)
    try:
        from macos_backend import (
            CGPoint,
            CGRect,
            CGSize,
            _OBJC_BOOL,
            _MacFrameworks,
            cg_top_left_to_appkit_bottom_left,
            kCGBitmapByteOrder32Little,
            kCGImageAlphaPremultipliedFirst,
        )
        fw = _MacFrameworks.get()
        if fw.appkit is None or int(fw.cg.CGMainDisplayID()) == 0:
            sys.stdout.write("HEADLESS\n")
            sys.stdout.flush()
            return 0

        # Ensure CGDataProviderCreateWithCFData signature
        fw.cg.CGDataProviderCreateWithCFData.argtypes = [ctypes.c_void_p]
        fw.cg.CGDataProviderCreateWithCFData.restype = ctypes.c_void_p

        ns_app_cls = fw.objc.objc_getClass(b"NSApplication")
        ns_win_cls = fw.objc.objc_getClass(b"NSWindow")
        ns_color_cls = fw.objc.objc_getClass(b"NSColor")
        ns_date_cls = fw.objc.objc_getClass(b"NSDate")
        ca_layer_cls = fw.objc.objc_getClass(b"CALayer")
        ca_tx_cls = fw.objc.objc_getClass(b"CATransaction")
        if not (ns_app_cls and ns_win_cls and ns_color_cls and ns_date_cls and ca_layer_cls):
            sys.stdout.write("HEADLESS\n")
            sys.stdout.flush()
            return 0

        setup_pool = fw.objc.objc_autoreleasePoolPush()
        try:
            app = fw.objc_call(ns_app_cls, b"sharedApplication", ctypes.c_void_p)
            if not app:
                sys.stdout.write("HEADLESS\n")
                sys.stdout.flush()
                return 0
            # NSApplicationActivationPolicyAccessory = 1 (zero Dock icon, zero menu hijack, zero focus steal)
            fw.objc_call(app, b"setActivationPolicy:", _OBJC_BOOL, (ctypes.c_long, 1))
            fw.objc_call(app, b"finishLaunching", None)

            size = _WIN_OV_SIZE
            win = fw.objc_call(ns_win_cls, b"alloc", ctypes.c_void_p)
            init_rect = CGRect(CGPoint(0.0, 0.0), CGSize(float(size), float(size)))
            win = fw.objc_call(
                win,
                b"initWithContentRect:styleMask:backing:defer:",
                ctypes.c_void_p,
                (CGRect, init_rect),
                (ctypes.c_uint64, 0),  # NSWindowStyleMaskBorderless
                (ctypes.c_uint64, 2),  # NSBackingStoreBuffered
                (_OBJC_BOOL, False),
            )
            if not win:
                sys.stdout.write("HEADLESS\n")
                sys.stdout.flush()
                return 0

            clear_col = fw.objc_call(ns_color_cls, b"clearColor", ctypes.c_void_p)
            fw.objc_call(win, b"setLevel:", None, (ctypes.c_long, 1000))  # NSScreenSaverWindowLevel
            fw.objc_call(win, b"setOpaque:", None, (_OBJC_BOOL, False))
            fw.objc_call(win, b"setBackgroundColor:", None, (ctypes.c_void_p, clear_col))
            fw.objc_call(win, b"setHasShadow:", None, (_OBJC_BOOL, False))
            fw.objc_call(win, b"setIgnoresMouseEvents:", None, (_OBJC_BOOL, True))
            fw.objc_call(win, b"setSharingType:", None, (ctypes.c_uint64, 0))  # NSWindowSharingNone
            # CanJoinAllSpaces(1) | Stationary(16) | IgnoresCycle(64) | FullScreenAuxiliary(256)
            fw.objc_call(win, b"setCollectionBehavior:", None, (ctypes.c_uint64, 1 | 16 | 64 | 256))

            # Layer-hosting view: setLayer: FIRST, then setWantsLayer:YES so AppKit never clears contents
            content_view = fw.objc_call(win, b"contentView", ctypes.c_void_p)
            layer = fw.objc_call(ca_layer_cls, b"layer", ctypes.c_void_p)
            if not (content_view and layer):
                sys.stdout.write("HEADLESS\n")
                sys.stdout.flush()
                return 0
            fw.objc_call(content_view, b"setLayer:", None, (ctypes.c_void_p, layer))
            fw.objc_call(content_view, b"setWantsLayer:", None, (_OBJC_BOOL, True))
        finally:
            fw.objc.objc_autoreleasePoolPop(setup_pool)

        cs = fw.cg.CGColorSpaceCreateDeviceRGB()
        if not cs:
            sys.stdout.write("HEADLESS\n")
            sys.stdout.flush()
            return 0
        default_mode = fw.cf_runloop_default_mode

        cmd_lock = threading.Lock()
        latest_cmd: list[dict[str, Any] | None] = [None]
        stop_flag = threading.Event()

        def reader() -> None:
            try:
                for raw in sys.stdin:
                    line = raw.strip()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)
                    except Exception:
                        continue
                    if msg.get("op") == "quit":
                        stop_flag.set()
                        return
                    with cmd_lock:
                        latest_cmd[0] = msg
            finally:
                stop_flag.set()

        t = threading.Thread(target=reader, daemon=True)
        t.start()

        sys.stdout.write("READY\n")
        sys.stdout.flush()

        arr = np.zeros((size, size, 4), dtype=np.uint8)
        visible = False

        try:
            while not stop_flag.is_set():
                pool = fw.objc.objc_autoreleasePoolPush()
                try:
                    with cmd_lock:
                        msg = latest_cmd[0]
                        latest_cmd[0] = None

                    if msg is not None:
                        op = msg.get("op")
                        if op == "hide":
                            if visible:
                                fw.objc_call(win, b"orderOut:", None, (ctypes.c_void_p, None))
                                visible = False
                        elif op == "render":
                            now_local = time.monotonic()
                            rips = [
                                _Ripple(
                                    x=float(r[0]),
                                    y=float(r[1]),
                                    started_at=now_local - float(r[2]),
                                    color=(float(r[3]), float(r[4]), float(r[5])),
                                    double=bool(r[6]),
                                )
                                for r in (msg.get("ripples") or [])
                            ]
                            win_x = int(msg["win_x"])
                            win_y = int(msg["win_y"])
                            _draw_win32_argb_buffer(
                                arr,
                                size,
                                cx=float(msg["cx"]),
                                cy=float(msg["cy"]),
                                win_x=win_x,
                                win_y=win_y,
                                alpha=float(msg["alpha"]),
                                ripples=rips,
                                now=now_local,
                            )
                            cf_data = fw.cf.CFDataCreate(None, arr.ctypes.data, arr.nbytes)
                            if cf_data:
                                try:
                                    prov = fw.cg.CGDataProviderCreateWithCFData(cf_data)
                                    if prov:
                                        try:
                                            cg_img = fw.cg.CGImageCreate(
                                                size, size, 8, 32, size * 4, cs,
                                                kCGBitmapByteOrder32Little | kCGImageAlphaPremultipliedFirst,
                                                prov, None, False, 0,
                                            )
                                            if cg_img:
                                                try:
                                                    if ca_tx_cls:
                                                        fw.objc_call(ca_tx_cls, b"begin", None)
                                                        fw.objc_call(ca_tx_cls, b"setDisableActions:", None, (_OBJC_BOOL, True))
                                                    fw.objc_call(layer, b"setContents:", None, (ctypes.c_void_p, cg_img))
                                                    if ca_tx_cls:
                                                        fw.objc_call(ca_tx_cls, b"commit", None)
                                                        fw.objc_call(ca_tx_cls, b"flush", None)
                                                finally:
                                                    fw.cg.CGImageRelease(cg_img)
                                        finally:
                                            fw.cg.CGDataProviderRelease(prov)
                                finally:
                                    fw.cf.CFRelease(cf_data)

                            prim_h = float(fw.cg.CGDisplayBounds(fw.cg.CGMainDisplayID()).size.height) or 900.0
                            ns_x, ns_y = cg_top_left_to_appkit_bottom_left(win_x, win_y, size, prim_h)
                            fw.objc_call(win, b"setFrameOrigin:", None, (CGPoint, CGPoint(ns_x, ns_y)))
                            if not visible:
                                fw.objc_call(win, b"orderFrontRegardless", None)
                                visible = True

                    # Real AppKit NSEvent pump (sleeps cleanly until dt without 100% CPU busy spin)
                    dt = 0.011 if visible else 0.030
                    until = fw.objc_call(ns_date_cls, b"dateWithTimeIntervalSinceNow:", ctypes.c_void_p, (ctypes.c_double, dt))
                    ev = fw.objc_call(
                        app,
                        b"nextEventMatchingMask:untilDate:inMode:dequeue:",
                        ctypes.c_void_p,
                        (ctypes.c_uint64, 0xFFFFFFFFFFFFFFFF),
                        (ctypes.c_void_p, until),
                        (ctypes.c_void_p, default_mode),
                        (_OBJC_BOOL, True),
                    )
                    while ev:
                        fw.objc_call(app, b"sendEvent:", None, (ctypes.c_void_p, ev))
                        ev = fw.objc_call(
                            app,
                            b"nextEventMatchingMask:untilDate:inMode:dequeue:",
                            ctypes.c_void_p,
                            (ctypes.c_uint64, 0xFFFFFFFFFFFFFFFF),
                            (ctypes.c_void_p, None),
                            (ctypes.c_void_p, default_mode),
                            (_OBJC_BOOL, True),
                        )
                finally:
                    fw.objc.objc_autoreleasePoolPop(pool)
        finally:
            teardown_pool = fw.objc.objc_autoreleasePoolPush()
            try:
                fw.objc_call(win, b"orderOut:", None, (ctypes.c_void_p, None))
                fw.objc_call(win, b"close", None)
            except Exception:
                pass
            finally:
                fw.objc.objc_autoreleasePoolPop(teardown_pool)
            fw.cg.CGColorSpaceRelease(cs)
        return 0
    except Exception:
        try:
            sys.stdout.write("HEADLESS\n")
            sys.stdout.flush()
        except Exception:
            pass
        return 0


# ---------------------------------------------------------------------------
# Unified non-blocking CursorOverlay controller
# ---------------------------------------------------------------------------


class CursorOverlay:
    """Thread-safe non-blocking custom AI cursor + click ripple overlay."""

    IDLE_HOLD_S = 1.8
    FADE_DURATION_S = 0.45

    def __init__(self, screen_size_fn=None):
        self._screen_size_fn = screen_size_fn
        self._enabled = os.environ.get("COMPUTER_USE_OVERLAY", "1").strip().lower() not in ("0", "false", "off", "no")
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._backend_name = "disabled" if not self._enabled else "lazy"
        self._cx = 0.0
        self._cy = 0.0
        self._anim_sx = 0.0
        self._anim_sy = 0.0
        self._anim_tx = 0.0
        self._anim_ty = 0.0
        self._anim_t0 = 0.0
        self._anim_dur = 0.0
        self._label = "AI"
        self._last_active = 0.0
        self._ripples: list[_Ripple] = []
        self._active_rect: tuple[int, int, int, int] | None = None
        atexit.register(self.close)

    @property
    def backend(self) -> str:
        return self._backend_name

    def active_dirty_rect(self) -> tuple[int, int, int, int] | None:
        """Return the current on-screen bounding box of the AI cursor & ripples (for diff masking)."""
        with self._lock:
            return self._active_rect

    def _ensure_started(self) -> None:
        if not self._enabled or self._thread is not None:
            return
        if sys.platform not in ("win32", "darwin") and not os.environ.get("WAYLAND_DISPLAY") and not os.environ.get("DISPLAY"):
            self._backend_name = "headless"
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run_loop, name="agcu-cursor-overlay", daemon=True)
        self._thread.start()

    def show_move(self, x: float, y: float, label: str = "AI") -> None:
        if not self._enabled:
            return
        self._ensure_started()
        with self._lock:
            self._cx = float(x)
            self._cy = float(y)
            self._anim_dur = 0.0
            self._label = label
            self._last_active = time.monotonic()
            self._active_rect = (int(x) - 28, int(y) - 28, 190, 90)
        self._wake.set()

    def glide_to(self, x: float, y: float, *, label: str = "AI", duration_ms: int = 50, from_pos: tuple[float, float] | None = None) -> None:
        """Non-blocking (~0.005 ms) smooth curved cursor glide interpolated at 120Hz in the overlay thread."""
        if not self._enabled:
            return
        self._ensure_started()
        now = time.monotonic()
        tx, ty = float(x), float(y)
        with self._lock:
            if from_pos is not None:
                sx, sy = float(from_pos[0]), float(from_pos[1])
            elif self._last_active > 0:
                sx, sy = self._cx, self._cy
            else:
                sx, sy = tx, ty
            dist = math.hypot(tx - sx, ty - sy)
            if duration_ms <= 0 or dist < 6.0:
                self._cx, self._cy = tx, ty
                self._anim_dur = 0.0
            else:
                self._anim_sx, self._anim_sy = sx, sy
                self._anim_tx, self._anim_ty = tx, ty
                self._anim_t0 = now
                self._anim_dur = max(0.018, min(0.18, float(duration_ms) / 1000.0))
            self._label = label
            self._last_active = now
            self._active_rect = (int(tx) - 36, int(ty) - 36, 200, 100)
        self._wake.set()

    def show_click(self, x: float, y: float, *, button: str = "left", double: bool = False) -> None:
        if not self._enabled:
            return
        self._ensure_started()
        color_map = {
            "left": (0.22, 0.78, 0.99),
            "right": (0.98, 0.62, 0.04),
            "middle": (0.68, 0.30, 0.92),
        }
        lbl = "Double-click" if double else ("Right-click" if button == "right" else "Click")
        with self._lock:
            now = time.monotonic()
            # Keep any in-flight smooth glide animating into (x, y) rather than cancelling it abruptly
            if self._anim_dur <= 0 or (now - self._anim_t0) >= self._anim_dur:
                self._cx = float(x)
                self._cy = float(y)
                self._anim_dur = 0.0
            else:
                self._anim_tx = float(x)
                self._anim_ty = float(y)
            self._label = lbl
            self._last_active = now
            self._ripples.append(_Ripple(x=float(x), y=float(y), started_at=now, color=color_map.get(button, color_map["left"]), double=double))
            self._ripples = self._ripples[-6:]
            self._active_rect = (int(x) - 52, int(y) - 52, 210, 120)
        self._wake.set()

    def hide(self) -> None:
        with self._lock:
            self._last_active = 0.0
            self._ripples.clear()
            self._active_rect = None
        self._wake.set()

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=0.5)
        self._thread = None
        with self._lock:
            self._active_rect = None

    def _create_backend(self):
        if sys.platform == "win32":
            b = _Win32LayeredOverlay()
            if b.ready:
                self._backend_name = "win32-layered"
                return b
            self._backend_name = "unavailable"
            return None
        if sys.platform == "darwin":
            b = _MacAppKitOverlayProxy()
            if b.ready:
                self._backend_name = "macos-appkit-overlay"
                return b
            self._backend_name = "unavailable"
            return None
        if os.environ.get("WAYLAND_DISPLAY"):
            b = _WaylandLayerOverlay()
            if b.ready:
                self._backend_name = "wayland-layer-shell"
                return b
        self._backend_name = "unavailable"
        return None

    def _run_loop(self) -> None:
        backend = self._create_backend()
        if backend is None:
            return
        try:
            was_visible = False
            while not self._stop.is_set():
                now = time.monotonic()
                gliding = False
                with self._lock:
                    if self._anim_dur > 0.0:
                        t = (now - self._anim_t0) / self._anim_dur
                        if t >= 1.0:
                            self._cx, self._cy = self._anim_tx, self._anim_ty
                            self._anim_dur = 0.0
                        else:
                            gliding = True
                            e = t * t * (3.0 - 2.0 * t)
                            dx = self._anim_tx - self._anim_sx
                            dy = self._anim_ty - self._anim_sy
                            dist = math.hypot(dx, dy)
                            if dist > 1.0:
                                arc = min(12.0, dist * 0.03) * math.sin(math.pi * t)
                                self._cx = self._anim_sx + dx * e + (-dy / dist) * arc
                                self._cy = self._anim_sy + dy * e + (dx / dist) * arc
                            else:
                                self._cx = self._anim_sx + dx * e
                                self._cy = self._anim_sy + dy * e
                    cx, cy = self._cx, self._cy
                    label = self._label
                    last_active = self._last_active
                    self._ripples = [r for r in self._ripples if (now - r.started_at) <= 0.36]
                    ripples = list(self._ripples)

                idle = now - last_active if last_active > 0 else 999.0
                if idle <= self.IDLE_HOLD_S:
                    alpha = 1.0
                elif idle <= self.IDLE_HOLD_S + self.FADE_DURATION_S:
                    alpha = max(0.0, 1.0 - (idle - self.IDLE_HOLD_S) / self.FADE_DURATION_S)
                else:
                    alpha = 0.0

                is_visible = alpha > 0.01 or bool(ripples) or gliding
                if is_visible or was_visible:
                    rect = backend.render(cx, cy, alpha, label, ripples, now)
                    with self._lock:
                        self._active_rect = rect
                    was_visible = is_visible

                if gliding or ripples or (0.0 < alpha < 1.0):
                    self._wake.wait(timeout=0.008)
                    self._wake.clear()
                elif is_visible:
                    self._wake.wait(timeout=0.04)
                    self._wake.clear()
                else:
                    self._wake.wait(timeout=1.0)
                    self._wake.clear()
        finally:
            backend.close()


if __name__ == "__main__":
    if "--mac-overlay-helper" in sys.argv:
        sys.exit(_run_mac_overlay_helper())
