#!/usr/bin/env python3
"""Fast cross-platform Computer Use MCP server for Linux (Wayland/X11), Windows 10/11, and macOS.

Highlights:
- Linux capture: Direct org.kde.KWin.ScreenShot2 Unix FD pipe (~3.5 ms) + Cairo bilinear scale
  + AVX2 libturbojpeg zero-copy encode (~1.5 ms -> ~5.2 ms full-screen JPEG). Fallbacks:
  GdkPixbuf (glycin-compatible), grim, spectacle, maim, scrot.
- Windows capture: Per-Monitor DPI v2 GDI BitBlt + PrintWindow (for occluded background windows)
  + TurboJPEG or built-in Windows GDI+ zero-dep JPEG/PNG encoder with crisp 5x7 bitmap labels.
- macOS capture: CoreGraphics CGWindowListCreateImage (with /usr/sbin/screencapture ScreenCaptureKit
  fallback for macOS 15+ Sequoia / 26+), CGBitmapContext bilinear scale, Homebrew libturbojpeg or
  built-in Apple ImageIO JPEG/PNG encoder, Retina HiDPI scale tracking, and 5x7 bitmap labels.
- Independent AI Cursor + Smooth Glide: Custom click-through cyan AI pointer & animated click
  ripples (Wayland zwlr_layer_shell_v1 / Win32 WS_EX_LAYERED+WDA_EXCLUDEFROMCAPTURE / macOS AppKit
  NSWindow+NSWindowSharingNone) that smoothly glides while preserving your physical mouse position.
- Background Computer Use: Isolated Xvfb virtual desktop (Linux), child-HWND PrintWindow +
  PostMessageW (Windows), and occluded CGWindowList + AXPress / CGEventPostToPid (macOS).
- Semantic Accessibility: AT-SPI2 (Linux), native MTA IUIAutomation COM (Windows), and native
  ApplicationServices AXUIElement (macOS) with stable [ref] IDs, AX_DIFF, and mouse-free actions.
"""

from __future__ import annotations

import argparse
import base64
import importlib
import json
import math
import os
# Must be set BEFORE importing numpy/libgomp/libtesseract to prevent 16-thread OpenMP thrashing across 8 OCR workers (~5x faster OCR)
os.environ.setdefault("OMP_THREAD_LIMIT", "1")
import platform
import statistics
import sys
import tempfile
import threading
import time
from typing import Any

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

from capture import Frame
from cursor_overlay import CursorOverlay
from fast_ocr import FastOCR, format_ocr_compact

SERVER_NAME = "computer-use"
SERVER_VERSION = "2.5.0"
PROTOCOL_VERSION = "2024-11-05"

_LINUX_SHIFT_V_TERMINALS = (
    "konsole", "kitty", "alacritty", "wezterm", "foot", "ghostty",
    "gnome-terminal", "tilix", "terminator", "xfce4-terminal", "xterm", "urxvt", "st", "mintty",
)
_WIN_SHIFT_V_TERMINALS = ("windowsterminal", "wezterm", "alacritty", "kitty", "mintty")

SERVER_INSTRUCTIONS = (
    "Fast native Linux, Windows, and macOS Computer Use MCP server. Minimize LLM turns: chain full "
    "workflows inside ONE desktop_act call using launch, focus, open_url, wait_and_click, click_and_type, "
    "click(text=...), keys, repeat, and wait(change=true, timeout_ms=800) with observe='none' on intermediate "
    "bursts. Text targets resolve locally via AX/UIA/AXUIElement + 8-thread parallel OCR, and sparse-AX apps "
    "(Discord, Brave, Electron, VS Code) return inline [x,y] OCR_TEXT without offloading screenshots to disk "
    "(pass mode='vision' or screenshot=true only for textless icons). Never insert blind 400-1000 ms sleeps "
    "between UI steps; use wait_and_click or wait(change=true). Moving your mouse does not abort by default."
)


def _init_session_bus():
    if sys.platform in ("win32", "darwin"):
        return None
    try:
        from dbus.mainloop.glib import DBusGMainLoop
        import dbus

        DBusGMainLoop(set_as_default=True)
        return dbus.SessionBus()
    except Exception:
        return None


def _clamp(val: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, val))


def normalized_to_screen(
    nx: float,
    ny: float,
    rect: tuple[int, int, int, int],
) -> tuple[int, int]:
    """Map normalized 0..1000 coordinates onto a screen rectangle (ox, oy, w, h)."""
    ox, oy, w, h = rect
    fx = max(0.0, min(1000.0, float(nx))) / 1000.0
    fy = max(0.0, min(1000.0, float(ny))) / 1000.0
    px = ox + int(round(fx * max(0, w - 1)))
    py = oy + int(round(fy * max(0, h - 1)))
    return px, py


def format_windows_compact(windows: list[dict[str, Any]], max_windows: int = 20) -> str:
    if not windows:
        return "(none)"
    lines: list[str] = []
    for w in windows[:max_windows]:
        flags: list[str] = []
        if w.get("active"):
            flags.append("ACTIVE")
        if w.get("minimized"):
            flags.append("min")
        if w.get("fullscreen"):
            flags.append("fs")
        fstr = (" [" + ",".join(flags) + "]") if flags else ""
        title = (w.get("title") or "")[:72]
        app = w.get("app") or "?"
        lines.append(
            f"  - {app} | {title!r} | rect=({w['x']},{w['y']},{w['w']}x{w['h']}) | id={w['id']}{fstr}"
        )
    if len(windows) > max_windows:
        lines.append(f"  ... (+{len(windows) - max_windows} more)")
    return "\n".join(lines)


class DesktopEngine:
    """Cross-platform orchestrator uniting capture, input, windows, a11y, clipboard, cursor overlay, and background mode."""

    def __init__(
        self,
        *,
        bus=None,
        windows=None,
        capture=None,
        a11y=None,
        clipboard=None,
        input_driver=None,
        overlay: CursorOverlay | None = None,
        background=None,
        ocr: FastOCR | None = None,
    ):
        self._is_windows = sys.platform == "win32"
        self._is_macos = sys.platform == "darwin"
        self._bus = bus if bus is not None else _init_session_bus()

        if self._is_windows:
            from win32_backend import (
                WinA11yTree,
                WinBackgroundSession,
                WinClipboard,
                WinInputDriver,
                WinScreenCapture,
                WinWindowManager,
            )
            self.windows = windows or WinWindowManager()
            self.capture = capture or WinScreenCapture(virtual_bounds_fn=self.windows.virtual_bounds)
            self.a11y = a11y or WinA11yTree()
            self.clipboard = clipboard or WinClipboard()
            self.input = input_driver or WinInputDriver(virtual_bounds_fn=self.windows.virtual_bounds)
            self.background = background or WinBackgroundSession(self.windows, self.capture)
        elif self._is_macos:
            from macos_backend import (
                MacA11yTree,
                MacBackgroundSession,
                MacClipboard,
                MacInputDriver,
                MacScreenCapture,
                MacWindowManager,
            )
            self.windows = windows or MacWindowManager()
            self.capture = capture or MacScreenCapture(virtual_bounds_fn=self.windows.virtual_bounds)
            self.a11y = a11y or MacA11yTree()
            self.clipboard = clipboard or MacClipboard()
            self.input = input_driver or MacInputDriver(virtual_bounds_fn=self.windows.virtual_bounds)
            self.background = background or MacBackgroundSession(self.windows, self.capture, self.input)
        else:
            from a11y import A11yTree
            from capture import ScreenCapture
            from clipboard import Clipboard
            from linux_bg import LinuxBackgroundSession
            from uinput_driver import InputDriver
            from windows import WindowManager

            self.windows = windows or WindowManager(session_bus=self._bus)
            self.capture = capture or ScreenCapture(session_bus=self._bus, virtual_bounds_fn=self.windows.virtual_bounds)
            self.a11y = a11y or A11yTree(session_bus=self._bus)
            self.clipboard = clipboard or Clipboard(session_bus=self._bus)
            self.input = input_driver or InputDriver(
                screen_size_fn=self.windows.screen_size,
                virtual_bounds_fn=self.windows.virtual_bounds,
            )
            self.background = background or LinuxBackgroundSession()

        self.overlay = overlay if overlay is not None else CursorOverlay()
        self.ocr = ocr if ocr is not None else FastOCR()
        self.last_image_rect: tuple[int, int, int, int] | None = None
        self.last_bg_image_rect: tuple[int, int, int, int] | None = None
        self._pre_step_frame: Frame | None = None
        self._prev_click: tuple[int, int, str] | None = None
        self._ai_cursor_pos: tuple[float, float] | None = None
        self._clip_restore_gen = 0
        self._clip_restore_timer: threading.Timer | None = None
        self._clip_saved_orig: str | None = None
        self._clip_restore_lock = threading.Lock()
        self._smooth_default = os.environ.get("COMPUTER_USE_SMOOTH_CURSOR", "1").strip().lower() not in ("0", "false", "off")
        self._independent_cursor_default = os.environ.get("COMPUTER_USE_INDEPENDENT_CURSOR", "1").strip().lower() not in ("0", "false", "off")
        self._audit_path = os.environ.get("COMPUTER_USE_AUDIT_LOG", "").strip() or None

    def shutdown(self) -> None:
        for fn in (
            self.overlay.close,
            self.ocr.close,
            self.input.close,
            getattr(self.a11y, "close", None),
            (self.background.stop if (self.background and self.background.active) else None),
        ):
            if fn is not None:
                try:
                    fn()
                except Exception:
                    pass

    def _virtual_bounds(self) -> tuple[int, int, int, int]:
        if hasattr(self.windows, "virtual_bounds"):
            return self.windows.virtual_bounds()
        sw, sh = self.windows.screen_size()
        return 0, 0, sw, sh

    def _clamp_screen(self, x: int, y: int) -> tuple[int, int]:
        vx, vy, vw, vh = self._virtual_bounds()
        return _clamp(x, vx, vx + vw - 1), _clamp(y, vy, vy + vh - 1)

    def _overlay_ignore_rects(self) -> list[tuple[int, int, int, int]]:
        if hasattr(self.overlay, "active_dirty_rect"):
            r = self.overlay.active_dirty_rect()
            if r is not None:
                return [r]
        return []

    def _is_ffm_compositor(self) -> bool:
        return getattr(self.windows, "backend", "") in ("hyprland", "sway")

    def _glide_ai_cursor(
        self,
        tx: int,
        ty: int,
        *,
        label: str = "AI",
        smooth: bool | None = None,
        duration_ms: int | None = None,
        move_os_cursor: bool = False,
    ) -> None:
        """Glide the custom cyan AI cursor to (tx, ty); 0 ms non-blocking on overlay thread when independent."""
        use_smooth = self._smooth_default if smooth is None else bool(smooth)
        start = self._ai_cursor_pos
        if start is None:
            cur = self._safe_user_cursor()
            start = (float(cur[0]), float(cur[1])) if cur is not None else (float(tx), float(ty))

        sx, sy = start
        dx, dy = float(tx) - sx, float(ty) - sy
        dist = math.hypot(dx, dy)

        if not use_smooth or dist < 6.0:
            self._ai_cursor_pos = (float(tx), float(ty))
            self.overlay.show_move(tx, ty, label=label)
            if move_os_cursor:
                self.input.move(tx, ty)
            return

        dur_ms = int(duration_ms) if duration_ms is not None else int(max(24.0, min(65.0, 20.0 + dist * 0.035)))
        # Non-blocking 120Hz glide on the overlay thread: zero sleep on the action critical path!
        if not move_os_cursor and hasattr(self.overlay, "glide_to"):
            self.overlay.glide_to(tx, ty, label=label, duration_ms=dur_ms, from_pos=(sx, sy))
            self._ai_cursor_pos = (float(tx), float(ty))
            return

        steps = max(4, min(10, dur_ms // 6))
        dt = (dur_ms / 1000.0) / steps
        arc = min(10.0, dist * 0.028)
        nx_perp = -dy / dist
        ny_perp = dx / dist

        for i in range(1, steps + 1):
            t = i / steps
            e = t * t * (3.0 - 2.0 * t)
            bend = math.sin(math.pi * t) * arc
            ix = sx + dx * e + nx_perp * bend
            iy = sy + dy * e + ny_perp * bend
            if i == steps:
                ix, iy = float(tx), float(ty)
            self.overlay.show_move(ix, iy, label=label)
            if move_os_cursor:
                self.input.move(int(round(ix)), int(round(iy)))
            if i < steps:
                time.sleep(dt)
        self._ai_cursor_pos = (float(tx), float(ty))

    def _safe_user_cursor(self) -> tuple[int, int] | None:
        try:
            if hasattr(self.windows, "cursor_pos"):
                return self.windows.cursor_pos()
            st = self.windows.state()
            return int(st["cursor"]["x"]), int(st["cursor"]["y"])
        except Exception:
            return None

    def _a11y_snapshot(self, active: dict[str, Any] | None, *, find: str | None = None, max_elements: int = 100, diff: str | bool = "auto") -> dict[str, Any]:
        win_rect = (int(active["x"]), int(active["y"]), int(active["w"]), int(active["h"])) if active else None
        return self.a11y.snapshot(
            app_hint=active.get("app") if active else None,
            title_hint=active.get("title") if active else None,
            pid_hint=active.get("pid") if active else None,
            window_id=active.get("id", "") if active else "",
            window_rect=win_rect,
            find=find,
            max_elements=max_elements,
            diff=diff,
        )

    def audit(self, tool: str, args: dict[str, Any], ok: bool, elapsed_ms: float, error: str | None = None) -> None:
        if not self._audit_path:
            return
        try:
            safe_args = dict(args)
            if "text" in safe_args and isinstance(safe_args["text"], str) and len(safe_args["text"]) > 40:
                safe_args["text"] = f"<{len(safe_args['text'])} chars>"
            rec = {
                "ts": round(time.time(), 3),
                "tool": tool,
                "args": safe_args,
                "ok": ok,
                "elapsed_ms": round(elapsed_ms, 1),
            }
            if error:
                rec["error"] = error
            with open(self._audit_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, separators=(",", ":")) + "\n")
        except Exception:
            pass

    def _scope_rect(self, coord_scope: str, active_win: dict[str, Any] | None, *, background: bool = False) -> tuple[int, int, int, int]:
        scope = (coord_scope or "screen").lower()
        if background:
            if self.last_bg_image_rect is not None:
                return self.last_bg_image_rect
            if (self._is_windows or self._is_macos) and self.background.active and hasattr(self.background, "window_rect"):
                return self.background.window_rect()
            if not (self._is_windows or self._is_macos) and self.background.active:
                bw, bh = self.background.size
                return (0, 0, bw, bh)
            return self._virtual_bounds()
        if scope == "last_image" and self.last_image_rect is not None:
            return self.last_image_rect
        if scope == "active" and active_win is not None:
            return (int(active_win["x"]), int(active_win["y"]), int(active_win["w"]), int(active_win["h"]))
        return self._virtual_bounds()

    def _find_text_point(
        self,
        query: str,
        active_win: dict[str, Any] | None = None,
        *,
        area: tuple[int, int, int, int] | None = None,
        allow_ocr: bool = True,
        background: bool = False,
    ) -> tuple[int, int, str] | None:
        """Resolve visible text to (x, y, source) locally: AX cache -> fresh AX -> parallel local OCR."""
        if not background:
            el = self.a11y.find_by_text(query)
            if el is not None:
                cx, cy = el["center"]
                return int(cx), int(cy), "ax-cache"
            if active_win is not None and self.a11y.available:
                self._a11y_snapshot(active_win, diff=False)
                el = self.a11y.find_by_text(query)
                if el is not None:
                    cx, cy = el["center"]
                    return int(cx), int(cy), "ax"

        if allow_ocr and self.ocr.available:
            try:
                if background:
                    frame = self.background.capture(area=area)
                    hit = self.ocr.find_text(frame, query)
                    if hit is not None:
                        return int(hit["center"][0]), int(hit["center"][1]), f"ocr({hit['match_type']})"
                    return None
                if self.capture.backend == "unavailable":
                    return None
                if area is not None:
                    frame = self.capture.capture(scope="area", area=area, include_cursor=False)
                    hit = self.ocr.find_text(frame, query)
                    if hit is not None:
                        return int(hit["center"][0]), int(hit["center"][1]), f"ocr({hit['match_type']})"
                    return None
                # Search active window crop first (~3x faster than full screen), then fall back to full screen
                vx, vy, vw, vh = self._virtual_bounds()
                tried_full = False
                if (
                    active_win is not None
                    and int(active_win.get("w", 0)) >= 120
                    and int(active_win.get("h", 0)) >= 100
                ):
                    aw_rect = (
                        int(active_win["x"]), int(active_win["y"]),
                        int(active_win["w"]), int(active_win["h"]),
                    )
                    if aw_rect[2] < vw * 0.92 or aw_rect[3] < vh * 0.92:
                        frame = self.capture.capture(scope="area", area=aw_rect, include_cursor=False)
                        hit = self.ocr.find_text(frame, query)
                        if hit is not None:
                            return int(hit["center"][0]), int(hit["center"][1]), f"ocr-win({hit['match_type']})"
                    else:
                        tried_full = False
                if not tried_full:
                    frame = self.capture.capture(scope="screen", include_cursor=False)
                    hit = self.ocr.find_text(frame, query)
                    if hit is not None:
                        return int(hit["center"][0]), int(hit["center"][1]), f"ocr({hit['match_type']})"
            except Exception:
                return None
        return None

    @staticmethod
    def _steps_need_active_win(steps: list[Any]) -> bool:
        for st in steps:
            if not isinstance(st, dict):
                continue
            op = str(st.get("op") or st.get("action") or "").lower()
            if str(st.get("coord_scope") or "").lower() == "active":
                return True
            if op in ("type", "click_and_type", "wait_and_click", "screenshot"):
                return True
            if op == "wait" and st.get("text"):
                return True
            if st.get("text_target") or st.get("target") or (op != "type" and st.get("text")):
                return True
            if op == "repeat" and DesktopEngine._steps_need_active_win(st.get("steps") or []):
                return True
        return False

    def resolve_point(
        self,
        step: dict[str, Any],
        *,
        active_win: dict[str, Any] | None = None,
        prefix: str = "",
        required: bool = True,
        background: bool = False,
    ) -> tuple[int, int] | None:
        ref_k = f"{prefix}ref"
        text_k = f"{prefix}text_target" if prefix == "" and "text_target" in step else f"{prefix}text"
        x_k, y_k = f"{prefix}x", f"{prefix}y"
        nx_k, ny_k = f"{prefix}nx", f"{prefix}ny"

        if step.get(ref_k) is not None:
            rid = int(step[ref_k])
            el = self.a11y.get_ref(rid)
            if el is None:
                raise ValueError(f"Unknown or stale ref [{rid}]. Call desktop_observe first.")
            cx, cy = el["center"]
            ox = int(step.get("offset_x", 0)) if prefix == "" else 0
            oy = int(step.get("offset_y", 0)) if prefix == "" else 0
            return (int(cx) + ox, int(cy) + oy) if background else self._clamp_screen(int(cx) + ox, int(cy) + oy)

        if step.get(x_k) is not None and step.get(y_k) is not None:
            ox = int(step.get("offset_x", 0)) if prefix == "" else 0
            oy = int(step.get("offset_y", 0)) if prefix == "" else 0
            px, py = int(round(float(step[x_k]))) + ox, int(round(float(step[y_k]))) + oy
            return (px, py) if background else self._clamp_screen(px, py)

        if step.get(nx_k) is not None and step.get(ny_k) is not None:
            rect = self._scope_rect(str(step.get("coord_scope") or "screen"), active_win, background=background)
            ox = int(step.get("offset_x", 0)) if prefix == "" else 0
            oy = int(step.get("offset_y", 0)) if prefix == "" else 0
            px, py = normalized_to_screen(float(step[nx_k]), float(step[ny_k]), rect)
            return (px + ox, py + oy) if background else self._clamp_screen(px + ox, py + oy)

        raw_area = step.get("area")
        step_area = tuple(int(v) for v in raw_area) if (isinstance(raw_area, (list, tuple)) and len(raw_area) == 4) else None
        allow_ocr = bool(step.get("ocr", True))
        ox = int(step.get("offset_x", 0)) if prefix == "" else 0
        oy = int(step.get("offset_y", 0)) if prefix == "" else 0

        if step.get(text_k) and step.get("op") not in ("type", "click_and_type"):
            q = str(step[text_k])
            found = self._find_text_point(q, active_win, area=step_area, allow_ocr=allow_ocr, background=background)
            if found is None:
                raise ValueError(f"No AX or OCR element matched text {q!r}. Use desktop_observe or coordinates.")
            px, py = found[0] + ox, found[1] + oy
            return (px, py) if background else self._clamp_screen(px, py)
        if step.get("text_target"):
            q = str(step["text_target"])
            found = self._find_text_point(q, active_win, area=step_area, allow_ocr=allow_ocr, background=background)
            if found is None:
                raise ValueError(f"No AX or OCR element matched text_target {q!r}.")
            px, py = found[0] + ox, found[1] + oy
            return (px, py) if background else self._clamp_screen(px, py)

        if required:
            raise ValueError(f"Missing target: provide {ref_k}, {x_k}/{y_k}, {nx_k}/{ny_k}, or text")
        return None

    def _needs_ctrl_shift_v(self, active_win: dict[str, Any] | None) -> bool:
        if not active_win:
            return False
        app = (active_win.get("app") or "").lower()
        terms = _WIN_SHIFT_V_TERMINALS if self._is_windows else _LINUX_SHIFT_V_TERMINALS
        return any(tc in app for tc in terms)

    def _cancel_clip_restore(self) -> None:
        with self._clip_restore_lock:
            self._clip_restore_gen += 1
            if self._clip_restore_timer is not None:
                self._clip_restore_timer.cancel()
                self._clip_restore_timer = None
            self._clip_saved_orig = None

    def _restore_user_cursor(self, x: int, y: int) -> None:
        if hasattr(self.input, "restore_cursor"):
            self.input.restore_cursor(x, y)
        else:
            self.input.move(x, y)

    def _type_with_atomic_paste(self, text: str, active_win: dict[str, Any] | None) -> str:
        """Paste text via clipboard while preserving and restoring prior non-empty text clipboard."""
        with self._clip_restore_lock:
            saved_orig = self._clip_saved_orig
        if saved_orig is not None:
            prev_clip: str | None = saved_orig
        else:
            try:
                prev_clip = self.clipboard.get_text()
            except Exception:
                prev_clip = None

        self.clipboard.set_text(text)
        time.sleep(0.008)
        if self._is_macos:
            paste_combo = "cmd+v"
        else:
            paste_combo = "ctrl+shift+v" if self._needs_ctrl_shift_v(active_win) else "ctrl+v"
        self.input.hotkey(paste_combo)
        if prev_clip:
            if self.clipboard.backend == "mock":
                try:
                    self.clipboard.set_text(prev_clip)
                except Exception:
                    pass
            else:
                # Non-blocking async clipboard restore saves ~110 ms per paste while preserving original user clip across chained pastes
                with self._clip_restore_lock:
                    self._clip_restore_gen += 1
                    gen = self._clip_restore_gen
                    self._clip_saved_orig = prev_clip
                    if self._clip_restore_timer is not None:
                        self._clip_restore_timer.cancel()

                    def _restore_clip(expected_gen: int = gen, saved: str = prev_clip) -> None:
                        with self._clip_restore_lock:
                            if self._clip_restore_gen != expected_gen:
                                return
                            self._clip_saved_orig = None
                            self._clip_restore_timer = None
                        try:
                            self.clipboard.set_text(saved)
                        except Exception:
                            pass

                    timer = threading.Timer(0.11, _restore_clip)
                    timer.daemon = True
                    self._clip_restore_timer = timer
                    timer.start()
        return f"paste({paste_combo})"

    def _check_user_takeover(self) -> tuple[bool, tuple[int, int] | None]:
        expected = self.input._last_pos
        if expected is None:
            return False, None
        cur = self._safe_user_cursor()
        if cur is None:
            return False, None
        if math.hypot(cur[0] - expected[0], cur[1] - expected[1]) > 180.0:
            return True, cur
        return False, cur

    def _encode_frame_block(
        self,
        frame: Frame,
        *,
        max_width: int = 1280,
        quality: int = 75,
        fmt: str = "jpeg",
        annotations: list[dict[str, Any]] | None = None,
        grid: bool = False,
        cursor: tuple[int, int] | None = None,
        background: bool = False,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        enc = self.capture.encode(
            frame,
            max_width=max_width,
            fmt=fmt,
            quality=quality,
            annotations=annotations,
            grid=grid,
            cursor=cursor,
        )
        rect = (frame.origin_x, frame.origin_y, frame.logical_width, frame.logical_height)
        if background:
            self.last_bg_image_rect = rect
        else:
            self.last_image_rect = rect
        img_block = {
            "type": "image",
            "data": enc["data_b64"],
            "mimeType": enc["mime_type"],
        }
        meta = {k: v for k, v in enc.items() if k != "data_b64"}
        return img_block, meta

    @staticmethod
    def _crop_frame_logical(frame: Frame, bbox: tuple[int, int, int, int], pad: int = 32) -> Frame:
        bx, by, bw, bh = bbox
        sc = frame.scale if frame.scale > 0 else 1.0
        x0 = _clamp(int((bx - frame.origin_x - pad) * sc), 0, frame.width - 1)
        y0 = _clamp(int((by - frame.origin_y - pad) * sc), 0, frame.height - 1)
        x1 = _clamp(int((bx - frame.origin_x + bw + pad) * sc), x0 + 1, frame.width)
        y1 = _clamp(int((by - frame.origin_y + bh + pad) * sc), y0 + 1, frame.height)
        sub_rgba = frame.rgba[y0:y1, x0:x1, :].copy()
        return Frame(
            rgba=sub_rgba,
            origin_x=frame.origin_x + int(round(x0 / sc)),
            origin_y=frame.origin_y + int(round(y0 / sc)),
            width=x1 - x0,
            height=y1 - y0,
            scale=frame.scale,
            backend=frame.backend,
            capture_ms=frame.capture_ms,
        )

    def _save_quick_screenshot(
        self,
        frame: Frame,
        *,
        cursor_pos: tuple[int, int] | None,
        max_width: int = 896,
        quality: int = 62,
    ) -> tuple[str, str] | None:
        """Save a ~1 ms grid JPEG to a private temp file so icon lookups skip an extra observe='vision' turn."""
        try:
            enc = self.capture.encode(
                frame, max_width=max_width, fmt="jpeg", quality=quality, grid=True, cursor=cursor_pos,
            )
            raw = base64.b64decode(enc["data_b64"])
            uid_tag = str(os.getuid()) if hasattr(os, "getuid") else "user"
            dst = os.path.join(tempfile.gettempdir(), f"computer-use-last-{uid_tag}.jpg")
            tmp = f"{dst}.{os.getpid()}.tmp"
            flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
            fd = os.open(tmp, flags, 0o600)
            try:
                os.write(fd, raw)
            finally:
                os.close(fd)
            os.replace(tmp, dst)
            self.last_image_rect = (frame.origin_x, frame.origin_y, frame.logical_width, frame.logical_height)
            vx, vy, vw, vh = self._virtual_bounds()
            is_full = (frame.origin_x == vx and frame.origin_y == vy and frame.logical_width == vw and frame.logical_height == vh)
            return dst, ("screen" if is_full else "last_image")
        except Exception:
            return None

    # -----------------------------------------------------------------------
    # Tool implementations
    # -----------------------------------------------------------------------

    def tool_status(self, args: dict[str, Any]) -> list[dict[str, Any]]:
        vx, vy, vw, vh = self._virtual_bounds()
        mac_perms: dict[str, bool] | None = None
        if self._is_windows:
            session = "windows-win32"
            desktop = "Windows"
            uinput_writable = None
            a11y_label = "uia-com"
        elif self._is_macos:
            session = "macos-aqua"
            ver = platform.mac_ver()[0]
            desktop = f"macOS {ver}".strip()
            uinput_writable = None
            a11y_label = "axuielement"
            try:
                from macos_backend import _MacFrameworks
                fw = _MacFrameworks.get()
                mac_perms = {
                    "accessibility": fw.accessibility_granted(),
                    "screen_recording": fw.screen_recording_granted(),
                }
            except Exception:
                mac_perms = None
        else:
            session = "wayland" if os.environ.get("WAYLAND_DISPLAY") else ("x11" if os.environ.get("DISPLAY") else "headless")
            desktop = os.environ.get("XDG_CURRENT_DESKTOP", "unknown")
            uinput_writable = os.access("/dev/uinput", os.W_OK)
            a11y_label = "atspi2"

        info: dict[str, Any] = {
            "server": f"{SERVER_NAME} {SERVER_VERSION}",
            "platform": sys.platform,
            "session": session,
            "desktop": desktop,
            "screen": {"x": vx, "y": vy, "w": vw, "h": vh},
            "backends": {
                "capture": self.capture.backend,
                "input": self.input.backend,
                "windows": self.windows.backend,
                "clipboard": self.clipboard.backend,
                "a11y": a11y_label if self.a11y.available else "unavailable",
                "ocr": "libtesseract-parallel" if self.ocr.available else "unavailable",
                "cursor_overlay": self.overlay.backend,
                "background": self.background.status(),
            },
            "independent_cursor": self._independent_cursor_default,
            "smooth_cursor": self._smooth_default,
            "uinput_writable": uinput_writable,
            "cached_refs": len(self.a11y._ref_map),
            "last_image_rect": list(self.last_image_rect) if self.last_image_rect else None,
        }
        if mac_perms is not None:
            info["macos_permissions"] = mac_perms
        return [{"type": "text", "text": json.dumps(info, indent=2)}]

    def tool_background(self, args: dict[str, Any]) -> list[dict[str, Any]]:
        action = (args.get("action") or "status").lower()
        if action == "status":
            return [{"type": "text", "text": json.dumps(self.background.status(), indent=2)}]
        if action == "stop":
            self.last_bg_image_rect = None
            return [{"type": "text", "text": json.dumps(self.background.stop(), indent=2)}]
        if self._is_windows or self._is_macos:
            launch_kw: dict[str, Any] = {"wait_app": args.get("wait_app"), "timeout_ms": int(args.get("timeout_ms", 4000))}
            if self._is_macos:
                launch_kw["background"] = True
            if action in ("start", "attach"):
                if args.get("command"):
                    launched = self.windows.launch(args["command"], **launch_kw)
                    win = launched.get("window")
                    if win:
                        self.last_bg_image_rect = None
                    res = self.background.attach(window_id=win["id"]) if win else {"launched": launched, **self.background.status()}
                    return [{"type": "text", "text": json.dumps(res, indent=2)}]
                self.last_bg_image_rect = None
                res = self.background.attach(window_id=args.get("window_id"), app=args.get("app"), title=args.get("title"))
                return [{"type": "text", "text": json.dumps(res, indent=2)}]
            if action == "launch":
                cmd = args.get("command")
                if not cmd:
                    raise ValueError("Missing 'command' for desktop_background(action='launch')")
                launched = self.windows.launch(cmd, **launch_kw)
                win = launched.get("window")
                if win:
                    self.last_bg_image_rect = None
                    self.background.attach(window_id=win["id"])
                return [{"type": "text", "text": json.dumps({"launched": launched, "background": self.background.status()}, indent=2)}]
            os_name = "macOS" if self._is_macos else "Windows"
            raise ValueError(f"Unsupported {os_name} background action: {action!r}")
        else:
            if action == "start":
                res = self.background.start(width=int(args.get("width", 1920)), height=int(args.get("height", 1080)))
                if args.get("command"):
                    res["launched"] = self.background.launch(args["command"], wait_ms=int(args.get("wait_ms", 500)))
                return [{"type": "text", "text": json.dumps(res, indent=2)}]
            if action == "launch":
                cmd = args.get("command")
                if not cmd:
                    raise ValueError("Missing 'command' for desktop_background(action='launch')")
                res = self.background.launch(cmd, wait_ms=int(args.get("wait_ms", 500)))
                return [{"type": "text", "text": json.dumps(res, indent=2)}]
            raise ValueError(f"Unsupported Linux background action: {action!r} (use start, launch, status, stop)")

    def tool_windows(self, args: dict[str, Any]) -> list[dict[str, Any]]:
        action = (args.get("action") or "list").lower()
        t0 = time.perf_counter()
        if action == "list":
            st = self.windows.state()
            ms = (time.perf_counter() - t0) * 1000.0
            active = st.get("active")
            active_str = f"{active['app']} ({active['title'][:72]!r})" if active else "none"
            txt = (
                f"SCREEN: {st['screen']['w']}x{st['screen']['h']} | "
                f"CURSOR: ({st['cursor']['x']},{st['cursor']['y']}) | "
                f"ACTIVE: {active_str} | {ms:.1f} ms\n"
                f"WINDOWS ({len(st['windows'])}):\n"
                f"{format_windows_compact(st['windows'])}"
            )
            return [{"type": "text", "text": txt}]
        if action == "launch":
            cmd = args.get("command")
            if not cmd:
                raise ValueError("Missing 'command' for launch")
            if args.get("background") and not (self._is_windows or self._is_macos):
                return [{"type": "text", "text": json.dumps(self.background.launch(cmd, wait_ms=int(args.get("wait_ms", 500))), indent=2)}]
            launch_kw: dict[str, Any] = {"wait_app": args.get("wait_app"), "timeout_ms": int(args.get("timeout_ms", 4000))}
            if self._is_macos and args.get("background"):
                launch_kw["background"] = True
            res = self.windows.launch(cmd, **launch_kw)
            if args.get("background") and (self._is_windows or self._is_macos) and res.get("window"):
                self.last_bg_image_rect = None
                self.background.attach(window_id=res["window"]["id"])
            return [{"type": "text", "text": json.dumps(res, indent=2)}]
        res = self.windows.control(
            action,
            window_id=args.get("window_id"),
            app=args.get("app"),
            title=args.get("title"),
            x=args.get("x"), y=args.get("y"), w=args.get("w"), h=args.get("h"),
        )
        return [{"type": "text", "text": json.dumps(res, indent=2)}]

    def tool_clipboard(self, args: dict[str, Any]) -> list[dict[str, Any]]:
        action = (args.get("action") or "get").lower()
        if action == "get":
            return [{"type": "text", "text": self.clipboard.get_text()}]
        if action == "set":
            self._cancel_clip_restore()
            val = str(args.get("text", ""))
            self.clipboard.set_text(val)
            return [{"type": "text", "text": f"Clipboard set ({len(val)} chars) via {self.clipboard.backend}"}]
        if action == "clear":
            self._cancel_clip_restore()
            self.clipboard.set_text("")
            return [{"type": "text", "text": f"Clipboard cleared via {self.clipboard.backend}"}]
        raise ValueError(f"Unsupported clipboard action: {action!r}")

    def tool_screenshot(self, args: dict[str, Any]) -> list[dict[str, Any]]:
        bg = bool(args.get("background", False))
        scope = (args.get("scope") or "screen").lower()
        annotate = bool(args.get("annotate", False))
        grid = bool(args.get("grid", False))
        include_cursor = bool(args.get("cursor", True))
        do_diff = bool(args.get("diff_from_last", True))
        max_width = int(args.get("max_width", 1280))
        quality = int(args.get("quality", 75))
        fmt = str(args.get("format", "jpeg"))

        if bg:
            raw_area = args.get("area")
            area = (int(raw_area[0]), int(raw_area[1]), int(raw_area[2]), int(raw_area[3])) if (raw_area and len(raw_area) == 4) else None
            frame = self.background.capture(area=area)
            diff_info = self.capture.diff_from_last(frame) if do_diff else None
            img_block, meta = self._encode_frame_block(
                frame, max_width=max_width, quality=quality, fmt=fmt, grid=grid, background=True,
            )
            header = (
                f"BG_SCREENSHOT ({frame.backend}): {meta['image_w']}x{meta['image_h']} {meta['mime_type']} "
                f"({meta['bytes'] // 1024} KB) | RECT: origin=({frame.origin_x},{frame.origin_y}) size={frame.logical_width}x{frame.logical_height} | "
                f"TIMING: capture={meta['capture_ms']}ms encode={meta['encode_ms']}ms total={meta['total_ms']}ms"
            )
            if grid:
                header += " | GRID: 0..1000 (use nx/ny with background=true, coord_scope='last_image')"
            if diff_info is not None:
                header += f" | DIFF: changed={diff_info['changed_pct']}% bbox={diff_info['changed_bbox']}"
            return [{"type": "text", "text": header}, img_block]

        st = self.windows.state()
        active = st.get("active")
        cursor_pos = (int(st["cursor"]["x"]), int(st["cursor"]["y"])) if include_cursor else None
        area = None
        active_rect = None
        wid = None

        if scope == "area":
            raw_area = args.get("area")
            if not raw_area or len(raw_area) != 4:
                raise ValueError("scope='area' requires area=[x, y, w, h]")
            area = (int(raw_area[0]), int(raw_area[1]), int(raw_area[2]), int(raw_area[3]))
        elif scope == "ref":
            rid = args.get("ref")
            if rid is None:
                raise ValueError("scope='ref' requires ref=<int>")
            el = self.a11y.get_ref(int(rid))
            if el is None:
                raise ValueError(f"Unknown ref [{rid}]. Call desktop_observe first.")
            pad = int(args.get("pad", 40))
            vx, vy, vw, vh = self._virtual_bounds()
            bx, by, bw, bh = el["bbox"]
            x0 = _clamp(bx - pad, vx, vx + vw - 1)
            y0 = _clamp(by - pad, vy, vy + vh - 1)
            x1 = _clamp(bx + bw + pad, x0 + 1, vx + vw)
            y1 = _clamp(by + bh + pad, y0 + 1, vy + vh)
            area = (x0, y0, x1 - x0, y1 - y0)
            scope = "area"
        elif scope == "active":
            if active:
                active_rect = (int(active["x"]), int(active["y"]), int(active["w"]), int(active["h"]))
        elif scope == "window":
            w = self.windows.find_window(
                window_id=args.get("window_id"),
                app=args.get("app"),
                title=args.get("title"),
            )
            if w is None:
                raise ValueError("Target window not found for scope='window'")
            wid = w["id"]
            active_rect = (int(w["x"]), int(w["y"]), int(w["w"]), int(w["h"]))

        frame = self.capture.capture(
            scope=scope, area=area, window_id=wid, active_rect=active_rect, include_cursor=include_cursor,
        )
        diff_info = self.capture.diff_from_last(frame, ignore_rects=self._overlay_ignore_rects()) if do_diff else None
        anns = list(self.a11y._ref_map.values()) if annotate else None
        img_block, meta = self._encode_frame_block(
            frame, max_width=max_width, quality=quality, fmt=fmt, annotations=anns, grid=grid, cursor=cursor_pos,
        )

        vx, vy, vw, vh = self._virtual_bounds()
        header_parts = [
            f"SCREENSHOT ({frame.backend}): {meta['image_w']}x{meta['image_h']} {meta['mime_type']} ({meta['bytes'] // 1024} KB)",
            f"RECT: origin=({frame.origin_x},{frame.origin_y}) size={frame.logical_width}x{frame.logical_height}",
            f"TIMING: capture={meta['capture_ms']}ms encode={meta['encode_ms']}ms total={meta['total_ms']}ms",
        ]
        if cursor_pos:
            header_parts.append(f"CURSOR: ({cursor_pos[0]},{cursor_pos[1]})")
        if diff_info is not None:
            header_parts.append(f"DIFF: changed={diff_info['changed_pct']}% bbox={diff_info['changed_bbox']}")
        if grid:
            is_full = (frame.origin_x == vx and frame.origin_y == vy and frame.logical_width == vw and frame.logical_height == vh)
            scope_hint = "screen" if is_full else "last_image"
            header_parts.append(f"GRID: 0..1000 (use nx/ny with coord_scope={scope_hint!r})")

        return [{"type": "text", "text": " | ".join(header_parts)}, img_block]

    def tool_observe(self, args: dict[str, Any]) -> list[dict[str, Any]]:
        if args.get("background"):
            return self.tool_screenshot({
                "background": True,
                "grid": bool(args.get("grid", True)),
                "max_width": int(args.get("max_width", 896)),
                "quality": int(args.get("quality", 62)),
            })

        t0 = time.perf_counter()
        mode = (args.get("mode") or "hybrid").lower()
        find_q = args.get("find")
        diff_opt = args.get("diff", "auto")
        ss_opt = args.get("screenshot", "auto")
        scope = (args.get("scope") or "screen").lower()
        max_elements = int(args.get("max_elements", 100))
        max_width = int(args.get("max_width", 896))
        quality = int(args.get("quality", 62))
        want_ocr = bool(args.get("ocr", False)) or (mode == "ocr")

        st = self.windows.state()
        win_ms = (time.perf_counter() - t0) * 1000.0
        active = st.get("active")
        sw, sh = int(st["screen"]["w"]), int(st["screen"]["h"])
        vx, vy = int(st["screen"].get("x", 0)), int(st["screen"].get("y", 0))
        cursor_pos = (int(st["cursor"]["x"]), int(st["cursor"]["y"]))

        lines = [
            f"SCREEN: {sw}x{sh} | CURSOR: ({cursor_pos[0]},{cursor_pos[1]}) | "
            f"ACTIVE: {(active['app'] + ' ' + repr(active['title'][:60])) if active else 'none'} | "
            f"WIN_MS: {win_ms:.1f}"
        ]

        if mode == "windows":
            lines.append(f"WINDOWS ({len(st['windows'])}):")
            lines.append(format_windows_compact(st["windows"]))
            return [{"type": "text", "text": "\n".join(lines)}]

        if st["windows"]:
            summary = ", ".join(f"{w['app']}{'*' if w.get('active') else ''}" for w in st["windows"][:10])
            lines.append(f"OPEN_APPS ({len(st['windows'])}): {summary}")

        ax_res: dict[str, Any] | None = None
        if mode in ("hybrid", "ax"):
            ax_res = self._a11y_snapshot(active, find=find_q, max_elements=max_elements, diff=diff_opt)
            kind = "AX_DIFF (prior [ref] IDs still valid)" if ax_res["is_diff"] else f"AX_REFS ({len(ax_res['elements'])})"
            lines.append(f"{kind} | AX_MS: {ax_res['elapsed_ms']}")
            lines.append(ax_res["formatted"])

        n_ax = len(ax_res["elements"]) if ax_res else 0
        explicit_ss: bool | None = None
        if ss_opt is True or str(ss_opt).lower() == "true":
            explicit_ss = True
        elif ss_opt is False or str(ss_opt).lower() == "false":
            explicit_ss = False

        # In hybrid mode on sparse-AX apps (Discord, Brave, Electron, VS Code), prefer inline OCR_TEXT
        # so MCP hosts that offload images to disk don't force an extra read_file turn!
        auto_ocr = (mode == "hybrid") and (n_ax < 5) and (explicit_ss is not True) and self.ocr.available
        want_ocr = want_ocr or auto_ocr
        if explicit_ss is True:
            want_ss = True
        elif explicit_ss is False:
            want_ss = False
        else:
            want_ss = (mode == "vision") or (mode == "hybrid" and n_ax < 5 and not auto_ocr)

        content: list[dict[str, Any]] = []
        frame: Frame | None = None
        if want_ss or want_ocr:
            active_rect = (int(active["x"]), int(active["y"]), int(active["w"]), int(active["h"])) if (scope == "active" and active) else None
            if self.capture.backend != "unavailable":
                frame = self.capture.capture(scope=scope, active_rect=active_rect, include_cursor=want_ss)

        ocr_word_count = 0
        if want_ocr and frame is not None:
            t_ocr = time.perf_counter()
            words = self.ocr.scan_frame(frame)
            if find_q:
                fq = str(find_q).lower()
                words = [w for w in words if fq in w["text"].lower()]
            ocr_word_count = len(words)
            ocr_ms = (time.perf_counter() - t_ocr) * 1000.0
            lines.append(f"OCR_TEXT ({ocr_word_count} words, {ocr_ms:.1f}ms):")
            lines.append(format_ocr_compact(words))
            if auto_ocr and explicit_ss is None:
                if ocr_word_count >= 5:
                    saved = self._save_quick_screenshot(frame, cursor_pos=cursor_pos, max_width=max_width, quality=quality)
                    if saved is not None:
                        lines.append(
                            f"SCREENSHOT_PATH: {saved[0]} (grid 0..1000, coord_scope={saved[1]!r}; "
                            f"ONLY read_file if you need a textless icon not in OCR_TEXT)"
                        )
                    else:
                        lines.append("OCR_INLINE: exact [x,y] labels above (pass mode='vision' or screenshot=true only for textless icons).")
                else:
                    # Pure icon/canvas screen (<5 OCR words): fall back to grid screenshot automatically
                    want_ss = True

        if want_ss and frame is not None:
            diff_px = self.capture.diff_from_last(frame, ignore_rects=self._overlay_ignore_rects())
            annotate = bool(args["annotate"]) if "annotate" in args else (n_ax > 0)
            grid = bool(args["grid"]) if "grid" in args else (n_ax < 5)
            anns = (ax_res["elements"] if ax_res else []) if annotate else None
            img_block, meta = self._encode_frame_block(
                frame, max_width=max_width, quality=quality, annotations=anns, grid=grid, cursor=cursor_pos,
            )
            is_full = (frame.origin_x == vx and frame.origin_y == vy and frame.logical_width == sw and frame.logical_height == sh)
            scope_hint = "screen" if is_full else "last_image"
            lines.append(
                f"VISUAL: {meta['image_w']}x{meta['image_h']} ({meta['bytes'] // 1024} KB, "
                f"{meta['total_ms']}ms, annotate={annotate}, grid={grid}"
                + (f", grid_scope={scope_hint}" if grid else "")
                + (f", changed={diff_px['changed_pct']}%" if diff_px else "")
                + ")"
            )
            content.append({"type": "text", "text": "\n".join(lines)})
            content.append(img_block)
        else:
            content.append({"type": "text", "text": "\n".join(lines)})
        return content

    def tool_act(self, args: dict[str, Any]) -> list[dict[str, Any]]:
        t0 = time.perf_counter()
        raw_steps = args.get("steps")
        if raw_steps is None:
            if "op" in args or "action" in args:
                raw_steps = [args]
            else:
                raise ValueError("Provide 'steps': [{op, ...}, ...] or single-step shorthand {op: ...}")
        if not isinstance(raw_steps, list) or not raw_steps:
            raise ValueError("'steps' must be a non-empty list")

        bg = bool(args.get("background", False))
        fast = bool(args.get("fast", False))
        observe_opt = str(args.get("observe", "auto")).lower()
        if "verify_change" in args:
            verify_change = bool(args["verify_change"])
        else:
            verify_change = (observe_opt == "crop_changed")
        detect_takeover = bool(args.get("detect_takeover", False))
        # If caller explicitly opted into detect_takeover=True without setting independent_cursor, honor takeover mode
        if "independent_cursor" in args:
            independent_cursor = bool(args["independent_cursor"])
        else:
            independent_cursor = False if detect_takeover else self._independent_cursor_default
        smooth = False if fast else args.get("smooth", self._smooth_default)
        if "settle" in args:
            settle_opt: Any = args["settle"]
        elif fast or (observe_opt == "none" and not verify_change):
            settle_opt = "none"
        else:
            settle_opt = "fast_flip"
        max_width = int(args.get("max_width", 896))
        quality = int(args.get("quality", 62))

        need_pre_state = (not bg) and self._steps_need_active_win(raw_steps)
        st_before = self.windows.state() if need_pre_state else {"active": None, "cursor": {"x": 0, "y": 0}}
        active_win = st_before.get("active")
        before_frame: Frame | None = None
        before_ig: list[tuple[int, int, int, int]] = []
        if verify_change:
            try:
                if bg:
                    before_frame = self.background.capture()
                elif self.capture.backend != "unavailable":
                    before_ig = self._overlay_ignore_rects()
                    before_frame = self.capture.capture(scope="screen", include_cursor=False)
            except Exception:
                before_frame = None

        step_results: list[dict[str, Any]] = []
        extra_images: list[dict[str, Any]] = []
        aborted_takeover = False
        had_mutation = False
        self._pre_step_frame = None
        self._prev_click = None
        last_ok_op = ""
        last_ok_submitted = False

        for idx, raw_step in enumerate(raw_steps):
            if not isinstance(raw_step, dict):
                step_results.append({"step": idx, "ok": False, "error": "Step must be an object"})
                break
            if idx > 0 and detect_takeover and not bg and not independent_cursor:
                taken, cur_p = self._check_user_takeover()
                if taken:
                    aborted_takeover = True
                    step_results.append({
                        "step": idx,
                        "ok": False,
                        "takeover_detected": True,
                        "cursor": list(cur_p) if cur_p else None,
                        "error": "User moved the mouse >180px (detect_takeover=true); aborting remaining steps.",
                    })
                    break

            op = (raw_step.get("op") or raw_step.get("action") or "").lower()
            if op not in ("wait", "screenshot"):
                had_mutation = True
            # If next step is wait_and_click / wait(text=...), snapshot baseline before this click/key (~3 ms)
            # so wait_and_click can diff & OCR ONLY the newly opened modal/menu (~22 ms instead of full window!)
            if (
                not bg
                and op in ("click", "key", "hotkey", "press", "type", "click_and_type", "ax_action")
                and idx + 1 < len(raw_steps)
                and isinstance(raw_steps[idx + 1], dict)
                and self.capture.backend != "unavailable"
            ):
                nxt_op = str(raw_steps[idx + 1].get("op") or raw_steps[idx + 1].get("action") or "").lower()
                if nxt_op == "wait_and_click" or (nxt_op == "wait" and raw_steps[idx + 1].get("text")):
                    try:
                        if active_win and int(active_win.get("w", 0)) >= 120 and int(active_win.get("h", 0)) >= 100:
                            self._pre_step_frame = self.capture.capture(
                                scope="area",
                                area=(int(active_win["x"]), int(active_win["y"]), int(active_win["w"]), int(active_win["h"])),
                                include_cursor=False,
                            )
                        else:
                            self._pre_step_frame = self.capture.capture(scope="screen", include_cursor=False)
                    except Exception:
                        self._pre_step_frame = None
            try:
                if bg:
                    res, imgs = self._exec_bg_step(op, raw_step)
                else:
                    res, imgs, active_win = self._exec_step(
                        op, raw_step, active_win,
                        smooth=bool(raw_step.get("smooth", smooth)),
                        independent_cursor=bool(raw_step.get("independent_cursor", independent_cursor)),
                    )
                step_results.append({"step": idx, "op": op, "ok": True, **res})
                extra_images.extend(imgs)
                last_ok_op = op
                last_ok_submitted = bool(raw_step.get("submit"))
                if op == "click" and isinstance(res.get("pos"), list) and len(res["pos"]) == 2:
                    self._prev_click = (int(res["pos"][0]), int(res["pos"][1]), str(res.get("button", "left")))
                elif op != "wait_and_click":
                    self._prev_click = None
            except Exception as exc:
                step_results.append({"step": idx, "op": op, "ok": False, "error": str(exc)})
                if not raw_step.get("continue_on_error"):
                    break

        after_frame: Frame | None = None
        if not aborted_takeover:
            if bg:
                if isinstance(settle_opt, (int, float)) and settle_opt > 0:
                    time.sleep(min(5.0, float(settle_opt) / 1000.0))
                elif settle_opt == "smart":
                    time.sleep(0.030)
                elif settle_opt == "fast_flip" and had_mutation:
                    time.sleep(0.020)
                if observe_opt != "none" or before_frame is not None:
                    try:
                        after_frame = self.background.capture()
                    except Exception:
                        after_frame = None
            else:
                if settle_opt == "smart" and self.capture.backend != "unavailable":
                    try:
                        after_frame = self.capture.wait_until_settled(
                            scope="screen", max_wait_ms=55, poll_ms=16,
                            include_cursor=False, ignore_rects_fn=self._overlay_ignore_rects,
                        )
                    except Exception:
                        after_frame = None
                elif settle_opt == "fast_flip" and had_mutation and self.capture.backend != "unavailable":
                    # After wait_and_click or submit=True at end of batch, briefly wait (<=280 ms) for dialog/view swap
                    # so post-action OCR sees the NEW screen instead of the closing modal!
                    if (last_ok_op == "wait_and_click" or last_ok_submitted) and self._pre_step_frame is not None:
                        base_f = self._pre_step_frame
                        t_end = time.monotonic() + 0.28
                        while time.monotonic() < t_end:
                            time.sleep(0.022)
                            try:
                                cur_f = self.capture.capture(
                                    scope="area",
                                    area=(base_f.origin_x, base_f.origin_y, base_f.logical_width, base_f.logical_height),
                                    include_cursor=False,
                                )
                                if cur_f.width == base_f.width and cur_f.height == base_f.height:
                                    d = self.capture.diff_frames(base_f, cur_f, ignore_rects=self._overlay_ignore_rects())
                                    if d["changed_pct"] >= 1.2:
                                        after_frame = cur_f
                                        break
                            except Exception:
                                break
                    else:
                        # Single 60Hz compositor frame beat (~14 ms) instead of multi-frame settle polling
                        time.sleep(0.014)
                elif isinstance(settle_opt, (int, float)) and settle_opt > 0:
                    time.sleep(min(5.0, float(settle_opt) / 1000.0))

        diff_info: dict[str, Any] | None = None
        if before_frame is not None:
            if after_frame is None and not bg and self.capture.backend != "unavailable":
                try:
                    after_frame = self.capture.capture(scope="screen", include_cursor=False)
                except Exception:
                    after_frame = None
            if after_frame is not None:
                ig = [] if bg else (before_ig + self._overlay_ignore_rects())
                diff_info = self.capture.diff_frames(before_frame, after_frame, ignore_rects=ig)

        total_ms = (time.perf_counter() - t0) * 1000.0
        if bg:
            lines = [f"BG_ACT: {sum(1 for r in step_results if r.get('ok'))}/{len(raw_steps)} ok | {total_ms:.1f} ms"]
            if diff_info is not None:
                lines.append(f"PIXEL_DIFF: changed={diff_info['changed_pct']}% bbox={diff_info['changed_bbox']}")
            lines.append("STEPS: " + json.dumps(step_results, separators=(",", ":")))
            out_blocks: list[dict[str, Any]] = []
            if observe_opt != "none" and after_frame is not None:
                img_block, meta = self._encode_frame_block(
                    after_frame, max_width=max_width, quality=quality, grid=bool(args.get("grid", True)), background=True,
                )
                lines.append(f"VISUAL: {meta['image_w']}x{meta['image_h']} ({meta['bytes'] // 1024} KB, {meta['total_ms']}ms)")
                out_blocks.append({"type": "text", "text": "\n".join(lines)})
                out_blocks.extend(extra_images)
                out_blocks.append(img_block)
            else:
                out_blocks.append({"type": "text", "text": "\n".join(lines)})
                out_blocks.extend(extra_images)
            return out_blocks

        # Skip extra windows.state() roundtrip when observe="none" and no screenshot is requested
        need_post_state = (observe_opt != "none") or (args.get("screenshot") is True)
        st_after = self.windows.state() if need_post_state else st_before
        active_after = st_after.get("active")
        lines = [
            f"ACT: {sum(1 for r in step_results if r.get('ok'))}/{len(raw_steps)} ok | {total_ms:.1f} ms | "
            f"ACTIVE: {(active_after['app'] + ' ' + repr(active_after['title'][:55])) if active_after else 'none'}"
        ]
        if diff_info is not None:
            lines.append(f"PIXEL_DIFF: changed={diff_info['changed_pct']}% bbox={diff_info['changed_bbox']}")
        lines.append("STEPS: " + json.dumps(step_results, separators=(",", ":")))

        out_blocks = []
        ax_res: dict[str, Any] | None = None
        if observe_opt in ("auto", "ax") and not aborted_takeover:
            ax_res = self._a11y_snapshot(active_after, diff="auto")
            hdr = "AX_DIFF" if ax_res["is_diff"] else f"AX_REFS ({len(ax_res['elements'])})"
            lines.append(f"{hdr} ({ax_res['elapsed_ms']}ms):\n{ax_res['formatted']}")

        want_img = False
        want_inline_ocr = False
        crop_to_diff = False
        if observe_opt == "vision" or args.get("screenshot") is True:
            want_img = True
        elif observe_opt == "crop_changed":
            want_img = True
            crop_to_diff = True
        elif observe_opt == "ocr" and not aborted_takeover:
            want_inline_ocr = True
        elif observe_opt == "auto" and not aborted_takeover:
            n_ax = len(ax_res["elements"]) if ax_res else 0
            if n_ax < 5:
                if self.ocr.available:
                    want_inline_ocr = True
                else:
                    want_img = True

        obs_frame: Frame | None = after_frame
        if want_inline_ocr and self.capture.backend != "unavailable":
            if obs_frame is None:
                if active_after and int(active_after.get("w", 0)) >= 120 and int(active_after.get("h", 0)) >= 100:
                    aw_r = (
                        int(active_after["x"]), int(active_after["y"]),
                        int(active_after["w"]), int(active_after["h"]),
                    )
                    obs_frame = self.capture.capture(scope="area", area=aw_r, include_cursor=False)
                else:
                    obs_frame = self.capture.capture(scope="screen", include_cursor=False)
            t_ocr = time.perf_counter()
            words = self.ocr.scan_frame(obs_frame)
            ocr_ms = (time.perf_counter() - t_ocr) * 1000.0
            lines.append(f"OCR_TEXT ({len(words)} words, {ocr_ms:.1f}ms):")
            lines.append(format_ocr_compact(words))
            if observe_opt == "auto":
                if len(words) >= 5:
                    cursor_pos = (int(st_after["cursor"]["x"]), int(st_after["cursor"]["y"]))
                    saved = self._save_quick_screenshot(obs_frame, cursor_pos=cursor_pos, max_width=max_width, quality=quality)
                    if saved is not None:
                        lines.append(
                            f"SCREENSHOT_PATH: {saved[0]} (grid 0..1000, coord_scope={saved[1]!r}; "
                            f"ONLY read_file if you need a textless icon not in OCR_TEXT)"
                        )
                    else:
                        lines.append("OCR_INLINE: exact [x,y] labels above (pass observe='vision' or screenshot=true only for textless icons).")
                else:
                    want_img = True

        if want_img and self.capture.backend != "unavailable":
            frame = obs_frame or self.capture.capture(scope="screen", include_cursor=False)
            if crop_to_diff and diff_info and diff_info.get("changed_bbox"):
                bx, by, bw, bh = diff_info["changed_bbox"]
                sc = frame.scale if frame.scale > 0 else 1.0
                pad = 64
                x0 = _clamp(int((bx - frame.origin_x - pad) * sc), 0, frame.width - 1)
                y0 = _clamp(int((by - frame.origin_y - pad) * sc), 0, frame.height - 1)
                x1 = _clamp(int((bx - frame.origin_x + bw + pad) * sc), x0 + 1, frame.width)
                y1 = _clamp(int((by - frame.origin_y + bh + pad) * sc), y0 + 1, frame.height)
                sub_rgba = frame.rgba[y0:y1, x0:x1, :].copy()
                frame = Frame(
                    rgba=sub_rgba,
                    origin_x=frame.origin_x + int(round(x0 / sc)),
                    origin_y=frame.origin_y + int(round(y0 / sc)),
                    width=x1 - x0,
                    height=y1 - y0,
                    scale=frame.scale,
                    backend=frame.backend,
                    capture_ms=frame.capture_ms,
                )
            cursor_pos = (int(st_after["cursor"]["x"]), int(st_after["cursor"]["y"]))
            n_ax = len(ax_res["elements"]) if ax_res else 0
            anns = (ax_res["elements"] if ax_res else []) if (args.get("annotate", n_ax > 0)) else None
            grid = bool(args.get("grid", n_ax < 5))
            img_block, meta = self._encode_frame_block(
                frame, max_width=max_width, quality=quality, annotations=anns, grid=grid, cursor=cursor_pos
            )
            lines.append(f"VISUAL: {meta['image_w']}x{meta['image_h']} ({meta['bytes'] // 1024} KB, {meta['total_ms']}ms)")
            out_blocks.append({"type": "text", "text": "\n".join(lines)})
            out_blocks.extend(extra_images)
            out_blocks.append(img_block)
        else:
            out_blocks.append({"type": "text", "text": "\n".join(lines)})
            out_blocks.extend(extra_images)
        return out_blocks

    def _exec_bg_step(self, op: str, step: dict[str, Any], *, _depth: int = 0) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        extra_images: list[dict[str, Any]] = []
        if op == "click":
            pt = self.resolve_point(step, required=True, background=True)
            assert pt is not None
            button = str(step.get("button", "left"))
            count = 2 if step.get("double") else int(step.get("count", 1))
            pos = self.background.click(pt[0], pt[1], button=button, count=count)
            return {"pos": list(pos), "button": button, "count": count}, extra_images
        if op == "click_and_type":
            t_step = dict(step)
            t_step["op"] = "type"
            if "target" in t_step and "text_target" not in t_step:
                t_step["text_target"] = t_step.pop("target")
            if "value" in t_step and "text" not in t_step:
                t_step["text"] = t_step.pop("value")
            return self._exec_bg_step("type", t_step, _depth=_depth)
        if op == "wait_and_click":
            q = str(step.get("text") or step.get("target") or "").strip()
            if not q:
                raise ValueError("wait_and_click requires 'text'")
            timeout_ms = max(50, min(15000, int(step.get("timeout_ms", 2500))))
            poll_s = max(0.02, min(0.5, float(step.get("poll_ms", 50)) / 1000.0))
            raw_area = step.get("area")
            area = tuple(int(v) for v in raw_area) if (isinstance(raw_area, (list, tuple)) and len(raw_area) == 4) else None
            t_start = time.monotonic()
            deadline = t_start + timeout_ms / 1000.0
            found: tuple[int, int, str] | None = None
            while True:
                found = self._find_text_point(q, area=area, allow_ocr=True, background=True)
                if found is not None or time.monotonic() >= deadline:
                    break
                time.sleep(poll_s)
            if found is None:
                raise TimeoutError(f"wait_and_click timed out after {timeout_ms}ms waiting for {q!r}")
            px, py, src = found
            button = str(step.get("button", "left"))
            count = 2 if step.get("double") else int(step.get("count", 1))
            self.background.click(px, py, button=button, count=count)
            out: dict[str, Any] = {
                "pos": [px, py], "source": src, "waited_ms": round((time.monotonic() - t_start) * 1000.0, 1),
            }
            type_val = step.get("type_text") if "type_text" in step else step.get("value")
            if type_val is not None:
                time.sleep(0.02)
                if step.get("clear"):
                    self.background.hotkey("cmd+a" if self._is_macos else "ctrl+a")
                    self.background.hotkey("backspace")
                self.background.type_text(str(type_val))
                if step.get("submit"):
                    self.background.hotkey("enter")
                out["typed"] = len(str(type_val))
            return out, extra_images
        if op in ("move", "hover"):
            pt = self.resolve_point(step, required=True, background=True)
            assert pt is not None
            pos = self.background.move(pt[0], pt[1])
            return {"pos": list(pos)}, extra_images
        if op == "drag":
            p1 = self.resolve_point(step, prefix="from_", required=True, background=True)
            p2 = self.resolve_point(step, prefix="to_", required=True, background=True)
            assert p1 is not None and p2 is not None
            start, end = self.background.drag(
                p1[0], p1[1], p2[0], p2[1],
                button=str(step.get("button", "left")),
                duration_ms=int(step.get("duration_ms", 120)),
                steps=int(step.get("steps", 10)),
            )
            return {"from": list(start), "to": list(end)}, extra_images
        if op == "scroll":
            pt = self.resolve_point(step, required=False, background=True)
            direction = str(step.get("direction", "down"))
            clicks = int(step.get("clicks", step.get("amount", 5)))
            self.background.scroll(direction=direction, clicks=clicks, x=pt[0] if pt else None, y=pt[1] if pt else None)
            return {"direction": direction, "clicks": clicks}, extra_images
        if op == "type":
            pt = self.resolve_point(step, required=False, background=True)
            if pt is not None:
                self.background.click(pt[0], pt[1])
                time.sleep(0.03)
            if step.get("clear"):
                self.background.hotkey("cmd+a" if self._is_macos else "ctrl+a")
                time.sleep(0.008)
                self.background.hotkey("backspace")
                time.sleep(0.008)
            text = str(step.get("text", ""))
            self.background.type_text(text)
            if step.get("submit"):
                self.background.hotkey("enter")
            return {"chars": len(text), "submitted": bool(step.get("submit"))}, extra_images
        if op in ("key", "hotkey", "press"):
            keys = step.get("keys") if step.get("keys") is not None else step.get("key")
            if not keys:
                raise ValueError("Missing 'keys'")
            count = int(step.get("count", 1))
            self.background.hotkey(keys, count=count)
            return {"keys": keys, "count": count}, extra_images
        if op == "keys":
            seq = step.get("sequence") or step.get("keys")
            if not isinstance(seq, list) or not seq:
                raise ValueError("op='keys' requires a non-empty 'sequence' list")
            delay_s = max(0.0, min(0.5, float(step.get("delay_ms", 10)) / 1000.0))
            for i, combo in enumerate(seq):
                self.background.hotkey(combo)
                if delay_s > 0 and i < len(seq) - 1:
                    time.sleep(delay_s)
            return {"sequence": len(seq)}, extra_images
        if op == "repeat":
            if _depth >= 2:
                raise ValueError("repeat nesting limit exceeded")
            inner = step.get("steps")
            if not isinstance(inner, list) or not inner:
                raise ValueError("op='repeat' requires non-empty 'steps'")
            count = max(1, min(200, int(step.get("count", 1))))
            delay_s = max(0.0, min(1.0, float(step.get("delay_ms", 8)) / 1000.0))
            for r_i in range(count):
                for sub in inner:
                    sub_op = str(sub.get("op") or sub.get("action") or "").lower()
                    _, sub_imgs = self._exec_bg_step(sub_op, sub, _depth=_depth + 1)
                    extra_images.extend(sub_imgs)
                if delay_s > 0 and r_i < count - 1:
                    time.sleep(delay_s)
            return {"repeated": count, "steps_each": len(inner)}, extra_images
        if op == "launch":
            cmd = step.get("command")
            if not cmd:
                raise ValueError("Missing 'command' for launch")
            if self._is_windows or self._is_macos:
                launch_kw: dict[str, Any] = {"wait_app": step.get("wait_app"), "timeout_ms": int(step.get("timeout_ms", 4000))}
                if self._is_macos:
                    launch_kw["background"] = True
                launched = self.windows.launch(cmd, **launch_kw)
                if launched.get("window"):
                    self.last_bg_image_rect = None
                    self.background.attach(window_id=launched["window"]["id"])
                return launched, extra_images
            return self.background.launch(cmd, wait_ms=int(step.get("wait_ms", 500))), extra_images
        if op == "wait":
            ms = max(0, min(10000, int(step.get("ms", 150))))
            time.sleep(ms / 1000.0)
            return {"slept_ms": ms}, extra_images
        if op == "screenshot":
            blocks = self.tool_screenshot({**step, "background": True})
            extra_images.extend(b for b in blocks if b.get("type") == "image")
            return {"info": blocks[0]["text"] if blocks else ""}, extra_images
        raise ValueError(f"Unsupported background op: {op!r}")

    def _exec_step(
        self,
        op: str,
        step: dict[str, Any],
        active_win: dict[str, Any] | None,
        *,
        smooth: bool = True,
        independent_cursor: bool = True,
        _depth: int = 0,
    ) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any] | None]:
        extra_images: list[dict[str, Any]] = []
        ffm = self._is_ffm_compositor()

        if op == "click_and_type":
            t_step = dict(step)
            t_step["op"] = "type"
            if "target" in t_step and "text_target" not in t_step:
                t_step["text_target"] = t_step.pop("target")
            if "value" in t_step and "text" not in t_step:
                t_step["text"] = t_step.pop("value")
            return self._exec_step(
                "type", t_step, active_win, smooth=smooth, independent_cursor=independent_cursor, _depth=_depth,
            )

        if op == "wait_and_click":
            q = str(step.get("text") or step.get("target") or "").strip()
            if not q:
                raise ValueError("wait_and_click requires 'text'")
            timeout_ms = max(50, min(15000, int(step.get("timeout_ms", 2500))))
            poll_s = max(0.018, min(0.5, float(step.get("poll_ms", 25)) / 1000.0))
            raw_area = step.get("area")
            area = tuple(int(v) for v in raw_area) if (isinstance(raw_area, (list, tuple)) and len(raw_area) == 4) else None
            allow_ocr = bool(step.get("ocr", True))
            ox = int(step.get("offset_x", 0))
            oy = int(step.get("offset_y", 0))
            t_start = time.monotonic()
            deadline = t_start + timeout_ms / 1000.0
            found: tuple[int, int, str] | None = None
            pre_frame = self._pre_step_frame
            last_cur_frame: Frame | None = None
            retried_prev = False
            retry_prev_ok = bool(step.get("retry_prev", True))

            while True:
                # Fast path 1: AX cache/tree (instant on native apps)
                found = self._find_text_point(q, active_win, area=area, allow_ocr=False)
                if found is not None:
                    break
                # Fast path 2: diff against pre-click frame -> OCR ONLY the newly opened popup/modal (~22 ms!)
                if allow_ocr and self.ocr.available and self.capture.backend != "unavailable":
                    if area is not None:
                        cur_frame = self.capture.capture(scope="area", area=area, include_cursor=False)
                    elif pre_frame is not None:
                        cur_frame = self.capture.capture(
                            scope="area",
                            area=(pre_frame.origin_x, pre_frame.origin_y, pre_frame.logical_width, pre_frame.logical_height),
                            include_cursor=False,
                        )
                    elif active_win and int(active_win.get("w", 0)) >= 120 and int(active_win.get("h", 0)) >= 100:
                        cur_frame = self.capture.capture(
                            scope="area",
                            area=(int(active_win["x"]), int(active_win["y"]), int(active_win["w"]), int(active_win["h"])),
                            include_cursor=False,
                        )
                    else:
                        cur_frame = self.capture.capture(scope="screen", include_cursor=False)
                    last_cur_frame = cur_frame

                    used_crop = False
                    waiting_for_paint = False
                    if (
                        pre_frame is not None
                        and area is None
                        and cur_frame.width == pre_frame.width
                        and cur_frame.height == pre_frame.height
                    ):
                        d = self.capture.diff_frames(pre_frame, cur_frame, ignore_rects=self._overlay_ignore_rects())
                        bbox = d.get("changed_bbox")
                        if d["changed_pct"] >= 0.18 and bbox:
                            bw, bh = bbox[2], bbox[3]
                            if (bw * bh) < (cur_frame.logical_width * cur_frame.logical_height * 0.72):
                                crop = self._crop_frame_logical(cur_frame, bbox, pad=36)
                                hit = self.ocr.find_text(crop, q)
                                used_crop = True
                                if hit is not None:
                                    found = (int(hit["center"][0]), int(hit["center"][1]), f"ocr-popup({hit['match_type']})")
                                    break
                        elif (time.monotonic() - t_start) < 0.14:
                            # Screen hasn't painted the post-click transition yet; skip full-window OCR for 20 ms
                            waiting_for_paint = True
                        elif (
                            retry_prev_ok
                            and not retried_prev
                            and self._prev_click is not None
                            and (time.monotonic() - t_start) >= 0.42
                        ):
                            # Preceding click was likely swallowed by a closing modal animation; retry it ONCE!
                            rx, ry, rbtn = self._prev_click
                            retried_prev = True
                            self._exec_step(
                                "click", {"op": "click", "x": rx, "y": ry, "button": rbtn}, active_win,
                                smooth=False, independent_cursor=independent_cursor, _depth=_depth + 1,
                            )
                            waiting_for_paint = True
                    # Full active-window fallback if popup didn't match or text was already visible
                    if not waiting_for_paint and (not used_crop or (time.monotonic() - t_start) > 0.14):
                        hit = self.ocr.find_text(cur_frame, q)
                        if hit is not None:
                            found = (int(hit["center"][0]), int(hit["center"][1]), f"ocr({hit['match_type']})")
                            break
                if time.monotonic() >= deadline:
                    break
                time.sleep(poll_s)

            if found is None:
                # Final full-screen fallback in case a dialog opened outside active_win bounds
                if allow_ocr and self.ocr.available and self.capture.backend != "unavailable":
                    full_f = self.capture.capture(scope="screen", include_cursor=False)
                    hit = self.ocr.find_text(full_f, q)
                    if hit is not None:
                        found = (int(hit["center"][0]), int(hit["center"][1]), f"ocr-screen({hit['match_type']})")
            if found is None:
                raise TimeoutError(f"wait_and_click timed out after {timeout_ms}ms waiting for {q!r}")

            # Store frame right before clicking so the NEXT chained wait_and_click diffs against this modal!
            self._pre_step_frame = last_cur_frame
            px, py = self._clamp_screen(found[0] + ox, found[1] + oy)
            src = found[2]
            click_step = {
                k: v for k, v in step.items()
                if k not in ("text", "target", "text_target", "type_text", "value", "clear", "submit", "offset_x", "offset_y")
            }
            click_step.update({"op": "click", "x": px, "y": py})
            click_res, c_imgs, active_win = self._exec_step(
                "click", click_step, active_win, smooth=smooth, independent_cursor=independent_cursor, _depth=_depth,
            )
            extra_images.extend(c_imgs)
            out: dict[str, Any] = {
                **click_res,
                "source": src,
                "waited_ms": round((time.monotonic() - t_start) * 1000.0, 1),
            }
            type_val = step.get("type_text") if "type_text" in step else step.get("value")
            if type_val is not None:
                time.sleep(0.015)
                t_res, t_imgs, active_win = self._exec_step(
                    "type",
                    {
                        "op": "type",
                        "text": str(type_val),
                        "clear": bool(step.get("clear", False)),
                        "submit": bool(step.get("submit", False)),
                        "mode": step.get("mode", "auto"),
                    },
                    active_win,
                    smooth=smooth,
                    independent_cursor=independent_cursor,
                    _depth=_depth,
                )
                extra_images.extend(t_imgs)
                out["typed"] = t_res
            return out, extra_images, active_win

        if op == "open_url":
            url = str(step.get("url") or "").strip()
            if not url:
                raise ValueError("open_url requires 'url'")
            app_name = step.get("app")
            if app_name:
                try:
                    self.windows.control("focus", app=str(app_name))
                    time.sleep(0.03)
                except Exception:
                    self.windows.launch(str(app_name), wait_app=str(app_name), timeout_ms=int(step.get("timeout_ms", 4000)))
                    time.sleep(0.08)
                active_win = self.windows.state().get("active")
            mod = "cmd" if self._is_macos else "ctrl"
            if step.get("new_tab"):
                self.input.hotkey(f"{mod}+t")
                time.sleep(0.035)
            else:
                self.input.hotkey(f"{mod}+l")
                time.sleep(0.022)
            t_res, t_imgs, active_win = self._exec_step(
                "type",
                {
                    "op": "type",
                    "text": url,
                    "submit": bool(step.get("submit", True)),
                    "mode": step.get("mode", "auto"),
                },
                active_win,
                smooth=smooth,
                independent_cursor=independent_cursor,
                _depth=_depth,
            )
            extra_images.extend(t_imgs)
            return {"url": url, "app": app_name, "new_tab": bool(step.get("new_tab")), **t_res}, extra_images, active_win

        if op == "keys":
            self._cancel_clip_restore()
            seq = step.get("sequence") or step.get("keys")
            if not isinstance(seq, list) or not seq:
                raise ValueError("op='keys' requires a non-empty 'sequence' list")
            delay_s = max(0.0, min(0.5, float(step.get("delay_ms", 10)) / 1000.0))
            for i, combo in enumerate(seq):
                self.input.hotkey(combo)
                if delay_s > 0 and i < len(seq) - 1:
                    time.sleep(delay_s)
            return {"sequence": len(seq)}, extra_images, active_win

        if op == "repeat":
            if _depth >= 2:
                raise ValueError("repeat nesting limit exceeded")
            inner = step.get("steps")
            if not isinstance(inner, list) or not inner:
                raise ValueError("op='repeat' requires non-empty 'steps'")
            count = max(1, min(200, int(step.get("count", 1))))
            delay_s = max(0.0, min(1.0, float(step.get("delay_ms", 8)) / 1000.0))
            for r_i in range(count):
                for sub in inner:
                    sub_op = str(sub.get("op") or sub.get("action") or "").lower()
                    _, sub_imgs, active_win = self._exec_step(
                        sub_op, sub, active_win,
                        smooth=bool(sub.get("smooth", smooth)),
                        independent_cursor=bool(sub.get("independent_cursor", independent_cursor)),
                        _depth=_depth + 1,
                    )
                    extra_images.extend(sub_imgs)
                if delay_s > 0 and r_i < count - 1:
                    time.sleep(delay_s)
            return {"repeated": count, "steps_each": len(inner)}, extra_images, active_win

        if op == "click":
            pt = self.resolve_point(step, active_win=active_win, required=False)
            button = str(step.get("button", "left"))
            count = 2 if step.get("double") else int(step.get("count", 1))
            mods = step.get("modifiers")
            if isinstance(mods, str):
                mods = [m.strip() for m in mods.split("+") if m.strip()]

            saved_user_pos: tuple[int, int] | None = None
            already_moved = False
            if pt is not None:
                lbl = "Double-click" if count >= 2 else ("Right-click" if button == "right" else "Click")
                if independent_cursor:
                    self._glide_ai_cursor(pt[0], pt[1], label=lbl, smooth=smooth, duration_ms=step.get("duration_ms"), move_os_cursor=False)
                    if not ffm:
                        saved_user_pos = self._safe_user_cursor()
                else:
                    self._glide_ai_cursor(pt[0], pt[1], label=lbl, smooth=smooth, duration_ms=step.get("duration_ms"), move_os_cursor=True)
                    already_moved = True

            click_pt_x = pt[0] if (pt is not None and (not already_moved or self._is_macos)) else None
            click_pt_y = pt[1] if (pt is not None and (not already_moved or self._is_macos)) else None
            pos = self.input.click(
                click_pt_x,
                click_pt_y,
                button=button,
                count=count,
                modifiers=mods,
            )
            if already_moved and pt is not None:
                pos = pt
            if pt is not None:
                self.overlay.show_click(pt[0], pt[1], button=button, double=(count >= 2))
            if saved_user_pos is not None:
                # Brief compositor settle before restoring user's physical cursor
                time.sleep(0.006)
                self._restore_user_cursor(saved_user_pos[0], saved_user_pos[1])
            return {"pos": list(pos) if pos else None, "button": button, "count": count}, extra_images, active_win

        if op in ("move", "hover"):
            pt = self.resolve_point(step, active_win=active_win, required=True)
            assert pt is not None
            self._glide_ai_cursor(pt[0], pt[1], label="Hover", smooth=smooth, duration_ms=step.get("duration_ms"), move_os_cursor=True)
            return {"pos": [pt[0], pt[1]]}, extra_images, active_win

        if op == "drag":
            p1 = self.resolve_point(step, active_win=active_win, prefix="from_", required=True)
            p2 = self.resolve_point(step, active_win=active_win, prefix="to_", required=True)
            assert p1 is not None and p2 is not None
            self._glide_ai_cursor(p1[0], p1[1], label="Drag", smooth=smooth, move_os_cursor=False)
            saved_user_pos = self._safe_user_cursor() if (independent_cursor and not ffm) else None
            start, end = self.input.drag(
                p1[0], p1[1], p2[0], p2[1],
                button=str(step.get("button", "left")),
                duration_ms=int(step.get("duration_ms", 140)),
                steps=int(step.get("steps", 14)),
                on_step=lambda x, y: self.overlay.show_move(x, y, label="Drag"),
            )
            self._ai_cursor_pos = (float(end[0]), float(end[1]))
            if saved_user_pos is not None:
                time.sleep(0.010)
                self._restore_user_cursor(saved_user_pos[0], saved_user_pos[1])
            return {"from": list(start), "to": list(end)}, extra_images, active_win

        if op == "scroll":
            pt = self.resolve_point(step, active_win=active_win, required=False)
            direction = str(step.get("direction", "down"))
            clicks = int(step.get("clicks", step.get("amount", 5)))
            saved_user_pos = None
            if pt is not None:
                self._glide_ai_cursor(pt[0], pt[1], label="Scroll", smooth=smooth, move_os_cursor=False)
                if independent_cursor and not ffm:
                    saved_user_pos = self._safe_user_cursor()
            self.input.scroll(direction=direction, clicks=clicks, x=pt[0] if pt else None, y=pt[1] if pt else None)
            if saved_user_pos is not None:
                time.sleep(0.005)
                self._restore_user_cursor(saved_user_pos[0], saved_user_pos[1])
            return {"direction": direction, "clicks": clicks, "pos": list(pt) if pt else None}, extra_images, active_win

        if op == "type":
            text = str(step.get("text", ""))
            pt = self.resolve_point(step, active_win=active_win, required=False)
            saved_user_pos: tuple[int, int] | None = None
            if pt is not None:
                self._glide_ai_cursor(pt[0], pt[1], label="Type", smooth=smooth, move_os_cursor=False)
                if independent_cursor and not ffm:
                    saved_user_pos = self._safe_user_cursor()
                self.input.click(pt[0], pt[1])
                self.overlay.show_click(pt[0], pt[1], button="left")
                time.sleep(0.010)
                if active_win is None or not (
                    active_win["x"] <= pt[0] < active_win["x"] + active_win["w"]
                    and active_win["y"] <= pt[1] < active_win["y"] + active_win["h"]
                ):
                    active_win = self.windows.state().get("active")
            try:
                if step.get("clear"):
                    self.input.hotkey("cmd+a" if self._is_macos else "ctrl+a")
                    time.sleep(0.006)
                    self.input.hotkey("backspace")
                    time.sleep(0.006)

                mode = str(step.get("mode", "auto")).lower()
                # Windows (KEYEVENTF_UNICODE) handles up to 500 chars; macOS 1-char Unicode events handle up to 120 chars cleanly
                key_len_limit = 500 if self._is_windows else (120 if self._is_macos else 24)
                use_paste = (mode == "paste") or (
                    mode == "auto" and (len(text) > key_len_limit or not self.input.is_pure_ascii_typable(text))
                )
                if use_paste and self.clipboard.backend != "unavailable":
                    method = self._type_with_atomic_paste(text, active_win)
                else:
                    self.input.type_keys(text, key_delay_ms=int(step.get("key_delay_ms", 0)))
                    if self._is_windows:
                        method = "win32-unicode"
                    elif self._is_macos:
                        method = "macos-unicode"
                    else:
                        method = f"uinput({getattr(self.input, 'layout', 'us')})"

                if step.get("submit"):
                    time.sleep(0.035 if method.startswith("paste") else 0.008)
                    self.input.hotkey("enter")
            finally:
                if saved_user_pos is not None:
                    time.sleep(0.006)
                    self._restore_user_cursor(saved_user_pos[0], saved_user_pos[1])
            return {"chars": len(text), "method": method, "submitted": bool(step.get("submit"))}, extra_images, active_win

        if op in ("key", "hotkey", "press"):
            self._cancel_clip_restore()
            keys = step.get("keys") if step.get("keys") is not None else step.get("key")
            if not keys:
                raise ValueError("Missing 'keys' (e.g. 'ctrl+s' or 'enter')")
            count = int(step.get("count", 1))
            self.input.hotkey(keys, count=count)
            return {"keys": keys, "count": count}, extra_images, active_win

        if op == "ax_action":
            rid = step.get("ref")
            if rid is None:
                raise ValueError("ax_action requires 'ref'")
            meta = self.a11y.get_ref(int(rid))
            if meta is not None:
                cx, cy = meta["center"]
                self._glide_ai_cursor(cx, cy, label="Click", smooth=smooth, move_os_cursor=False)
                self.overlay.show_click(cx, cy, button="left")
            res = self.a11y.do_action(int(rid), action_name=step.get("action_name"))
            return res, extra_images, active_win

        if op == "focus":
            res = self.windows.control("focus", window_id=step.get("window_id"), app=step.get("app"), title=step.get("title"))
            time.sleep(0.04)
            active_win = self.windows.state().get("active")
            return {"focused": res.get("app"), "title": res.get("title")}, extra_images, active_win

        if op == "window":
            wact = str(step.get("window_action") or step.get("subaction") or "focus")
            res = self.windows.control(
                wact,
                window_id=step.get("window_id"), app=step.get("app"), title=step.get("title"),
                x=step.get("x"), y=step.get("y"), w=step.get("w"), h=step.get("h"),
            )
            time.sleep(0.04)
            active_win = self.windows.state().get("active")
            return res, extra_images, active_win

        if op == "launch":
            cmd = step.get("command")
            if not cmd:
                raise ValueError("Missing 'command' for launch")
            res = self.windows.launch(cmd, wait_app=step.get("wait_app"), timeout_ms=int(step.get("timeout_ms", 4000)))
            active_win = self.windows.state().get("active")
            return res, extra_images, active_win

        if op == "wait":
            timeout_ms = int(step.get("timeout_ms", 4000))
            if step.get("change"):
                raw_area = step.get("area")
                area = tuple(int(v) for v in raw_area) if raw_area and len(raw_area) == 4 else None
                res = self.capture.wait_for_change(
                    area=area,
                    timeout_ms=timeout_ms,
                    threshold_pct=float(step.get("threshold_pct", 0.15)),
                    ignore_rects_fn=self._overlay_ignore_rects,
                )
                return res, extra_images, active_win
            if step.get("app"):
                q = str(step["app"]).lower()
                deadline = time.monotonic() + timeout_ms / 1000.0
                while time.monotonic() < deadline:
                    w = self.windows.find_window(app=q) or self.windows.find_window(title=q)
                    if w is not None:
                        return {"found": True, "window": w}, extra_images, active_win
                    time.sleep(0.05)
                return {"found": False, "app": q}, extra_images, active_win
            if step.get("gone"):
                q = str(step["gone"])
                raw_area = step.get("area")
                area = tuple(int(v) for v in raw_area) if (isinstance(raw_area, (list, tuple)) and len(raw_area) == 4) else None
                t_start = time.monotonic()
                deadline = t_start + timeout_ms / 1000.0
                while time.monotonic() < deadline:
                    hit = self._find_text_point(q, active_win, area=area, allow_ocr=bool(step.get("ocr", True)))
                    if hit is None:
                        return {
                            "gone": True,
                            "text": q,
                            "waited_ms": round((time.monotonic() - t_start) * 1000.0, 1),
                        }, extra_images, active_win
                    time.sleep(0.03)
                return {"gone": False, "text": q}, extra_images, active_win
            if step.get("text"):
                q = str(step["text"])
                raw_area = step.get("area")
                area = tuple(int(v) for v in raw_area) if (isinstance(raw_area, (list, tuple)) and len(raw_area) == 4) else None
                t_start = time.monotonic()
                deadline = t_start + timeout_ms / 1000.0
                while time.monotonic() < deadline:
                    aw = self.windows.state().get("active")
                    hit = self._find_text_point(q, aw, area=area, allow_ocr=bool(step.get("ocr", True)))
                    if hit is not None:
                        return {
                            "found": True,
                            "pos": [hit[0], hit[1]],
                            "source": hit[2],
                            "waited_ms": round((time.monotonic() - t_start) * 1000.0, 1),
                        }, extra_images, aw
                    time.sleep(0.05)
                return {"found": False, "text": q}, extra_images, active_win
            ms = max(0, min(10000, int(step.get("ms", 150))))
            time.sleep(ms / 1000.0)
            return {"slept_ms": ms}, extra_images, active_win

        if op == "screenshot":
            blocks = self.tool_screenshot(step)
            meta_txt = blocks[0]["text"] if blocks else ""
            extra_images.extend(b for b in blocks if b.get("type") == "image")
            return {"info": meta_txt}, extra_images, active_win

        raise ValueError(f"Unknown desktop_act op: {op!r}")


# ---------------------------------------------------------------------------
# MCP Tool Schemas (7 focused tools)
# ---------------------------------------------------------------------------

TOOLS: list[dict[str, Any]] = [
    {
        "name": "desktop_observe",
        "description": (
            "Observe the Linux, Windows, or macOS desktop in 1 call: window list + active window + cursor (~1.2 ms) "
            "+ semantic accessibility [ref] tree (AT-SPI2 / UIA / AXUIElement with incremental AX_DIFF) "
            "+ inline 8-thread parallel local OCR [x,y] labels on sparse-AX apps (zero screenshot offload) "
            "+ compact 896px JPEG fallback when OCR has <5 words or mode='vision' / screenshot=true is requested."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["hybrid", "ax", "ocr", "vision", "windows"], "default": "hybrid"},
                "background": {"type": "boolean", "default": False},
                "ocr": {"type": "boolean", "default": False, "description": "Run local parallel OCR and return clickable [x,y] text lines."},
                "find": {"type": "string", "description": "Filter AX/OCR elements and text by substring."},
                "diff": {"oneOf": [{"type": "string", "enum": ["auto"]}, {"type": "boolean"}], "default": "auto"},
                "screenshot": {"oneOf": [{"type": "string", "enum": ["auto"]}, {"type": "boolean"}], "default": "auto"},
                "scope": {"type": "string", "enum": ["screen", "active"], "default": "screen"},
                "annotate": {"type": "boolean", "description": "Draw Set-of-Marks [ref] badges on screenshot."},
                "grid": {"type": "boolean", "description": "Draw 0..1000 normalized coordinate grid on screenshot."},
                "max_elements": {"type": "integer", "default": 100},
                "max_width": {"type": "integer", "default": 896},
                "quality": {"type": "integer", "default": 62},
            },
        },
    },
    {
        "name": "desktop_act",
        "description": (
            "Execute batched desktop actions with a non-blocking 120Hz cyan AI cursor + click ripples (preserving "
            "your physical mouse). Chain full workflows in ONE call using launch, focus, open_url, wait_and_click, "
            "click_and_type, click(text=...), keys, repeat, and wait(change=true, timeout_ms=800). Text targets "
            "resolve locally via AX + 8-thread parallel OCR; pass observe='none' on intermediate bursts."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "steps": {
                    "type": "array",
                    "description": (
                        "Ordered steps. Ops: click, click_and_type, wait_and_click, open_url, keys, repeat, "
                        "move/hover, drag, scroll, type, key/hotkey, ax_action, focus, window, launch, wait, screenshot."
                    ),
                    "items": {"type": "object"},
                },
                "op": {"type": "string", "description": "Single-step shorthand op when 'steps' is omitted."},
                "background": {"type": "boolean", "default": False},
                "fast": {"type": "boolean", "default": False, "description": "Instant 0ms cursor snap and skip settle/diff for maximum speed."},
                "smooth": {"type": "boolean", "default": True},
                "independent_cursor": {"type": "boolean", "default": True},
                "settle": {"oneOf": [{"type": "string", "enum": ["smart", "none"]}, {"type": "integer"}], "default": "none"},
                "observe": {"type": "string", "enum": ["auto", "ax", "ocr", "vision", "crop_changed", "none"], "default": "auto"},
                "verify_change": {"type": "boolean", "default": False},
                "detect_takeover": {"type": "boolean", "default": False},
                "screenshot": {"type": "boolean"},
                "annotate": {"type": "boolean"},
                "grid": {"type": "boolean"},
                "max_width": {"type": "integer", "default": 896},
                "quality": {"type": "integer", "default": 62},
            },
        },
    },
    {
        "name": "desktop_screenshot",
        "description": (
            "Capture a ~5 ms JPEG/PNG screenshot of the full screen, active window, named window, "
            "area [x,y,w,h], [ref] element, or background session, with optional Set-of-Marks badges, "
            "0..1000 coordinate grid, and pixel diff."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "background": {"type": "boolean", "default": False},
                "scope": {"type": "string", "enum": ["screen", "active", "window", "area", "ref"], "default": "screen"},
                "area": {"type": "array", "items": {"type": "integer"}, "minItems": 4, "maxItems": 4},
                "window_id": {"type": "string"},
                "app": {"type": "string"},
                "title": {"type": "string"},
                "ref": {"type": "integer"},
                "pad": {"type": "integer", "default": 40},
                "annotate": {"type": "boolean", "default": False},
                "grid": {"type": "boolean", "default": False},
                "cursor": {"type": "boolean", "default": True},
                "diff_from_last": {"type": "boolean", "default": True},
                "max_width": {"type": "integer", "default": 1280},
                "quality": {"type": "integer", "default": 75},
                "format": {"type": "string", "enum": ["jpeg", "png"], "default": "jpeg"},
            },
        },
    },
    {
        "name": "desktop_windows",
        "description": "List, focus, minimize, maximize, unmaximize, move/resize, close, or launch desktop windows (Linux, Windows, & macOS).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "focus", "minimize", "maximize", "unmaximize", "close", "move_resize", "launch"],
                    "default": "list",
                },
                "background": {"type": "boolean", "default": False},
                "window_id": {"type": "string"},
                "app": {"type": "string"},
                "title": {"type": "string"},
                "x": {"type": "integer"},
                "y": {"type": "integer"},
                "w": {"type": "integer"},
                "h": {"type": "integer"},
                "command": {"oneOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]},
                "wait_app": {"type": "string"},
                "timeout_ms": {"type": "integer", "default": 4000},
            },
        },
    },
    {
        "name": "desktop_background",
        "description": (
            "Manage zero-touch background computer use: starts an authenticated isolated Xvfb virtual desktop on Linux, "
            "attaches child-HWND PrintWindow + PostMessageW on Windows, or attaches occluded CGWindowList + AXPress / "
            "CGEventPostToPid on macOS without moving your mouse or stealing focus."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["status", "start", "attach", "launch", "stop"], "default": "status"},
                "width": {"type": "integer", "default": 1920},
                "height": {"type": "integer", "default": 1080},
                "command": {"oneOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]},
                "window_id": {"type": "string"},
                "app": {"type": "string"},
                "title": {"type": "string"},
                "wait_app": {"type": "string"},
                "wait_ms": {"type": "integer", "default": 500},
                "timeout_ms": {"type": "integer", "default": 4000},
            },
        },
    },
    {
        "name": "desktop_clipboard",
        "description": "Read, write, or clear the system clipboard (Klipper/wl-clipboard/xclip on Linux, Win32 CF_UNICODETEXT on Windows, pbcopy/pbpaste on macOS).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["get", "set", "clear"], "default": "get"},
                "text": {"type": "string"},
            },
        },
    },
    {
        "name": "desktop_status",
        "description": "Report OS, active backends (capture, input, windows, clipboard, a11y, cursor_overlay, background), and screen geometry.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


class MCPServer:
    """Newline-delimited JSON-RPC 2.0 MCP server (serialized on main thread for GLib/DBus/COM safety)."""

    def __init__(self, engine: DesktopEngine | None = None):
        self.engine = engine or DesktopEngine()
        self.handlers = {
            "desktop_observe": self.engine.tool_observe,
            "desktop_act": self.engine.tool_act,
            "desktop_screenshot": self.engine.tool_screenshot,
            "desktop_windows": self.engine.tool_windows,
            "desktop_background": self.engine.tool_background,
            "desktop_clipboard": self.engine.tool_clipboard,
            "desktop_status": self.engine.tool_status,
        }

    def handle_request(self, req: Any) -> dict[str, Any] | None:
        if not isinstance(req, dict):
            return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request: expected JSON object"}}
        method = req.get("method")
        req_id = req.get("id")
        params = req.get("params") or {}

        if method == "notifications/initialized":
            return None
        if method == "ping":
            return {"jsonrpc": "2.0", "id": req_id, "result": {}}
        if method == "initialize":
            client_ver = params.get("protocolVersion") or PROTOCOL_VERSION
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": client_ver,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                    "instructions": SERVER_INSTRUCTIONS,
                },
            }
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}}
        if method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments") or {}
            handler = self.handlers.get(name)
            if not handler:
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32601, "message": f"Unknown tool: {name}"},
                }
            t0 = time.perf_counter()
            try:
                content = handler(arguments)
                self.engine.audit(name, arguments, True, (time.perf_counter() - t0) * 1000.0)
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {"content": content, "isError": False},
                }
            except Exception as exc:
                self.engine.audit(name, arguments, False, (time.perf_counter() - t0) * 1000.0, str(exc))
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [{"type": "text", "text": f"Error: {exc}"}],
                        "isError": True,
                    },
                }

        if req_id is None:
            return None
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }

    @staticmethod
    def _script_mtimes() -> dict[str, int]:
        out: dict[str, int] = {}
        try:
            for entry in os.scandir(_SCRIPT_DIR):
                if entry.name.endswith(".py"):
                    out[entry.name] = entry.stat().st_mtime_ns
        except OSError:
            pass
        return out

    def _maybe_hot_reload(self, mtimes: dict[str, int]) -> tuple["MCPServer", dict[str, int]]:
        """Reload scripts in-process if updated on disk so plugin upgrades apply without restarting the host."""
        cur = self._script_mtimes()
        if not cur or cur == mtimes:
            return self, mtimes
        try:
            self.engine.shutdown()
            import importlib.util

            for mod_name in (
                "capture", "fast_ocr", "cursor_overlay", "windows", "a11y",
                "clipboard", "uinput_driver", "win32_backend", "macos_backend", "linux_bg",
            ):
                if mod_name in sys.modules:
                    importlib.reload(sys.modules[mod_name])
            spec = importlib.util.spec_from_file_location("computer_use_mcp_live", os.path.abspath(__file__))
            if spec and spec.loader:
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                return mod.MCPServer(), cur
        except Exception:
            pass
        return self, cur

    def serve_stdio(self) -> None:
        try:
            sys.stdin.reconfigure(encoding="utf-8")
            sys.stdout.reconfigure(encoding="utf-8", newline="\n")
        except Exception:
            pass
        hot_reload = os.environ.get("COMPUTER_USE_HOT_RELOAD", "1").strip().lower() not in ("0", "false", "off")
        mtimes = self._script_mtimes() if hot_reload else {}
        active_srv: MCPServer = self
        active_srv.engine.ocr.prewarm()
        if hasattr(active_srv.engine.input, "prewarm"):
            active_srv.engine.input.prewarm()
        try:
            for line in sys.stdin:
                line = line.strip()
                if not line:
                    continue
                try:
                    req = json.loads(line)
                except json.JSONDecodeError as exc:
                    sys.stdout.write(
                        json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(exc)}})
                        + "\n"
                    )
                    sys.stdout.flush()
                    continue
                if hot_reload and isinstance(req, dict) and req.get("method") in ("tools/call", "tools/list"):
                    prev_srv = active_srv
                    active_srv, mtimes = active_srv._maybe_hot_reload(mtimes)
                    if active_srv is not prev_srv:
                        active_srv.engine.ocr.prewarm()
                        if hasattr(active_srv.engine.input, "prewarm"):
                            active_srv.engine.input.prewarm()
                resp = active_srv.handle_request(req)
                if resp is not None:
                    sys.stdout.write(json.dumps(resp, separators=(",", ":")) + "\n")
                    sys.stdout.flush()
        finally:
            active_srv.engine.shutdown()


# ---------------------------------------------------------------------------
# Non-destructive --selftest, --bench, and --demo-cursor CLI helpers
# ---------------------------------------------------------------------------


def run_selftest(server: MCPServer) -> int:
    failures: list[str] = []

    def check(label: str, cond: bool, detail: str = "") -> None:
        tag = "PASS" if cond else "FAIL"
        print(f"  [{tag}] {label}" + (f": {detail}" if detail else ""))
        if not cond:
            failures.append(label)

    print(f"Running {SERVER_NAME} v{SERVER_VERSION} non-destructive selftest ({sys.platform})...")
    r_init = server.handle_request({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": PROTOCOL_VERSION}})
    check("MCP initialize", bool(r_init and r_init["result"]["serverInfo"]["name"] == SERVER_NAME))

    r_list = server.handle_request({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    tools = (r_list or {}).get("result", {}).get("tools", [])
    check("MCP tools/list", len(tools) == 7, f"{len(tools)} tools")

    r_stat = server.handle_request({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "desktop_status", "arguments": {}}})
    st_json = json.loads(r_stat["result"]["content"][0]["text"])
    check("desktop_status", not r_stat["result"]["isError"], json.dumps(st_json["backends"]))

    r_win = server.handle_request({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "desktop_windows", "arguments": {"action": "list"}}})
    check("desktop_windows(list)", not r_win["result"]["isError"], r_win["result"]["content"][0]["text"].splitlines()[0])

    if server.engine.clipboard.backend != "unavailable":
        r_clip = server.handle_request({"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "desktop_clipboard", "arguments": {"action": "get"}}})
        check("desktop_clipboard(get)", not r_clip["result"]["isError"], f"backend={server.engine.clipboard.backend}")

    if server.engine.capture.backend != "unavailable":
        run_live_ss = True
        if sys.platform == "darwin":
            try:
                from macos_backend import _MacFrameworks
                fw = _MacFrameworks.get()
                run_live_ss = bool(fw.screen_recording_granted() and int(fw.cg.CGMainDisplayID()) != 0)
            except Exception:
                run_live_ss = False
        if run_live_ss:
            vx, vy, vw, vh = server.engine._virtual_bounds()
            r_ss = server.handle_request({
                "jsonrpc": "2.0", "id": 6, "method": "tools/call",
                "params": {"name": "desktop_screenshot", "arguments": {"scope": "area", "area": [vx, vy, min(400, vw), min(300, vh)], "grid": True}},
            })
            ok_ss = (not r_ss["result"]["isError"]) and len(r_ss["result"]["content"]) == 2 and r_ss["result"]["content"][1]["type"] == "image"
            check("desktop_screenshot(area+grid)", ok_ss, r_ss["result"]["content"][0]["text"])
        else:
            import numpy as np
            print("  [SKIP] desktop_screenshot(live): macOS Screen Recording permission not granted or headless display")
            syn = Frame(rgba=np.full((120, 200, 4), 120, dtype=np.uint8), origin_x=0, origin_y=0, width=200, height=120, scale=1.0, backend="synthetic", capture_ms=0.1)
            enc = server.engine.capture.encode(syn, max_width=160, fmt="jpeg", quality=75, grid=True)
            check("desktop_screenshot(synthetic encode+grid)", enc["bytes"] > 200, f"{enc['image_w']}x{enc['image_h']} {enc['bytes']} B")

    r_obs = server.handle_request({
        "jsonrpc": "2.0", "id": 7, "method": "tools/call",
        "params": {"name": "desktop_observe", "arguments": {"mode": "hybrid", "screenshot": False}},
    })
    check("desktop_observe(hybrid)", not r_obs["result"]["isError"], r_obs["result"]["content"][0]["text"].splitlines()[0])

    print(f"Selftest result: {'ALL PASSED' if not failures else f'{len(failures)} FAILED: {failures}'}")
    return 0 if not failures else 1


def run_bench(server: MCPServer, runs: int = 5) -> int:
    eng = server.engine
    print(f"\nBenchmarking computer-use v{SERVER_VERSION} backends (median of {runs} runs)...")

    def bench(name: str, fn) -> None:
        times: list[float] = []
        res = fn()  # warmup
        for _ in range(runs):
            t0 = time.perf_counter()
            res = fn()
            times.append((time.perf_counter() - t0) * 1000.0)
        extra = f"  ({res})" if isinstance(res, str) else ""
        print(f"  {name:<42} median={statistics.median(times):6.2f} ms   min={min(times):6.2f} ms{extra}")

    bench(f"windows.state [{eng.windows.backend}]", lambda: f"{len(eng.windows.state()['windows'])} windows")
    if eng.clipboard.backend != "unavailable":
        bench(f"clipboard.get [{eng.clipboard.backend}]", lambda: f"{len(eng.clipboard.get_text())} chars")
    if eng.capture.backend != "unavailable":
        f_full: list[Frame] = []
        vx, vy, vw, vh = eng._virtual_bounds()

        def cap_full():
            f = eng.capture.capture(scope="screen", include_cursor=True)
            f_full[:] = [f]
            return f"{f.width}x{f.height}"

        bench(f"capture full screen [{eng.capture.backend}]", cap_full)
        bench("capture 400x300 area", lambda: (lambda f: f"{f.width}x{f.height}")(eng.capture.capture(scope='area', area=(vx, vy, min(400, vw), min(300, vh)))))
        bench("encode 896w JPEG q=62", lambda: f"{eng.capture.encode(f_full[0], max_width=896, quality=62)['bytes']//1024} KB")
        bench("encode 896w JPEG + grid + cursor", lambda: f"{eng.capture.encode(f_full[0], max_width=896, quality=62, grid=True, cursor=(vx + 400, vy + 300))['bytes']//1024} KB")
        f2 = eng.capture.capture(scope="screen", include_cursor=True)
        bench("numpy diff_frames", lambda: f"changed={eng.capture.diff_frames(f_full[0], f2)['changed_pct']}%")
        if eng.ocr.available:
            bench("fast_ocr scan_frame (cached)", lambda: f"{len(eng.ocr.scan_frame(f_full[0]))} words")
    if eng.a11y.available:
        st = eng.windows.state()
        aw = st.get("active")
        bench(
            "a11y.snapshot (active window)",
            lambda: f"{len(eng._a11y_snapshot(aw, diff=False)['elements'])} refs",
        )
    return 0


def run_demo_cursor(server: MCPServer) -> int:
    eng = server.engine
    vx, vy, vw, vh = eng._virtual_bounds()
    pts = [
        (vx + int(vw * 0.30), vy + int(vh * 0.35)),
        (vx + int(vw * 0.65), vy + int(vh * 0.45)),
        (vx + int(vw * 0.48), vy + int(vh * 0.62)),
    ]
    print(f"Demonstrating smooth custom AI cursor overlay across {len(pts)} points (non-destructive)...")
    eng._ai_cursor_pos = (float(pts[0][0] - 120), float(pts[0][1] - 80))
    for i, (px, py) in enumerate(pts):
        eng._glide_ai_cursor(px, py, label="AI", smooth=True, duration_ms=160, move_os_cursor=False)
        time.sleep(0.17)
        eng.overlay.show_click(px, py, button="right" if i == 1 else "left", double=(i == 2))
        time.sleep(0.32)
    time.sleep(0.35)
    print(f"Overlay backend: {eng.overlay.backend}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Antigravity Linux, Windows, & macOS Computer Use MCP server")
    parser.add_argument("--selftest", action="store_true", help="Run non-destructive protocol and backend checks")
    parser.add_argument("--bench", action="store_true", help="Run non-destructive latency benchmarks")
    parser.add_argument("--demo-cursor", action="store_true", help="Preview the smooth custom AI cursor and click ripples (no real clicks)")
    parser.add_argument("--runs", type=int, default=5, help="Iterations per benchmark")
    args = parser.parse_args()

    if args.selftest or args.bench or args.demo_cursor:
        # Prevent Windows cp1252 UnicodeEncodeError when printing non-ASCII window titles
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
        server = MCPServer()
        rc = 0
        try:
            if args.selftest:
                rc |= run_selftest(server)
            if args.bench:
                rc |= run_bench(server, runs=max(1, args.runs))
            if args.demo_cursor:
                rc |= run_demo_cursor(server)
        finally:
            server.engine.shutdown()
        sys.exit(rc)

    server = MCPServer()
    server.serve_stdio()


if __name__ == "__main__":
    main()
