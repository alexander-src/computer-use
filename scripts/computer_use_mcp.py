#!/usr/bin/env python3
"""Ultra-fast Linux Computer Use MCP server for Antigravity.

Architecture:
- Screen capture: Direct org.kde.KWin.ScreenShot2 Unix FD pipe (~8.5 ms 1080p, ~1.3 ms crop)
  + zero-copy GdkPixbuf JPEG encode (~22 ms) + Cairo Set-of-Marks/grid + NumPy frame diff (<4 ms).
  Fallbacks: grim (wlroots), spectacle, maim/scrot (X11).
- Input injection: Persistent kernel /dev/uinput QEMU usb-tablet absolute pointer + virtual keyboard
  (<0.05 ms emit, 1:1 pixel-exact on Wayland & X11). Fallbacks: ydotool, xdotool, wtype.
- Window control: Direct org.kde.KWin /Scripting DBus (~4 ms). Fallbacks: hyprctl, swaymsg, wmctrl/xdotool.
- Accessibility tree: Native AT-SPI2 walker with stable [ref] IDs, incremental DIFFs, and mouse-free actions.
- Clipboard: Direct org.kde.klipper DBus (~0.4 ms). Fallbacks: wl-clipboard, xclip, xsel.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
import time
from typing import Any

# Ensure sibling driver modules import cleanly regardless of CWD
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

from a11y import A11yTree
from capture import Frame, ScreenCapture
from clipboard import Clipboard
from uinput_driver import InputDriver, TERMINAL_CLASSES
from windows import WindowManager

SERVER_NAME = "computer-use"
SERVER_VERSION = "1.0.0"
PROTOCOL_VERSION = "2024-11-05"

SERVER_INSTRUCTIONS = (
    "Ultra-fast Linux Computer Use MCP server. Prefer desktop_observe (AX-first with automatic "
    "visual fallback) to discover windows and [ref] IDs, then chain multi-step actions in a single "
    "desktop_act call. Use nx/ny (0..1000) with grid=true for canvas/Electron UIs without AX trees."
)


def _init_session_bus():
    """Initialize GLib DBus mainloop before creating SessionBus (required by dbus-python callbacks)."""
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
    """Stateful orchestrator uniting capture, input, windows, AT-SPI2, and clipboard."""

    def __init__(
        self,
        *,
        bus=None,
        windows: WindowManager | None = None,
        capture: ScreenCapture | None = None,
        a11y: A11yTree | None = None,
        clipboard: Clipboard | None = None,
        input_driver: InputDriver | None = None,
    ):
        self._bus = bus if bus is not None else _init_session_bus()
        self.windows = windows or WindowManager(session_bus=self._bus)
        self.capture = capture or ScreenCapture(session_bus=self._bus)
        self.a11y = a11y or A11yTree(session_bus=self._bus)
        self.clipboard = clipboard or Clipboard(session_bus=self._bus)
        self.input = input_driver or InputDriver(screen_size_fn=self.windows.screen_size)
        self.last_image_rect: tuple[int, int, int, int] | None = None
        self._audit_path = os.environ.get("COMPUTER_USE_AUDIT_LOG", "").strip() or None

    def shutdown(self) -> None:
        self.input.close()

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

    def _scope_rect(self, coord_scope: str, active_win: dict[str, Any] | None) -> tuple[int, int, int, int]:
        scope = (coord_scope or "screen").lower()
        if scope == "last_image" and self.last_image_rect is not None:
            return self.last_image_rect
        if scope == "active" and active_win is not None:
            return (int(active_win["x"]), int(active_win["y"]), int(active_win["w"]), int(active_win["h"]))
        sw, sh = self.windows.screen_size()
        return (0, 0, sw, sh)

    def resolve_point(
        self,
        step: dict[str, Any],
        *,
        active_win: dict[str, Any] | None = None,
        prefix: str = "",
        required: bool = True,
    ) -> tuple[int, int] | None:
        """Resolve a target point from ref, text, exact x/y, or normalized nx/ny (0..1000)."""
        ref_k = f"{prefix}ref"
        text_k = f"{prefix}text_target" if prefix == "" and "text_target" in step else f"{prefix}text"
        x_k, y_k = f"{prefix}x", f"{prefix}y"
        nx_k, ny_k = f"{prefix}nx", f"{prefix}ny"

        # 1. [ref] ID from AT-SPI2 snapshot
        if step.get(ref_k) is not None:
            rid = int(step[ref_k])
            el = self.a11y.get_ref(rid)
            if el is None:
                raise ValueError(f"Unknown or stale ref [{rid}]. Call desktop_observe first.")
            cx, cy = el["center"]
            return int(cx), int(cy)

        # 2. Exact pixel x, y
        if step.get(x_k) is not None and step.get(y_k) is not None:
            sw, sh = self.windows.screen_size()
            return _clamp(int(round(float(step[x_k]))), 0, sw - 1), _clamp(int(round(float(step[y_k]))), 0, sh - 1)

        # 3. Normalized 0..1000 nx, ny
        if step.get(nx_k) is not None and step.get(ny_k) is not None:
            rect = self._scope_rect(str(step.get("coord_scope") or "screen"), active_win)
            px, py = normalized_to_screen(float(step[nx_k]), float(step[ny_k]), rect)
            sw, sh = self.windows.screen_size()
            return _clamp(px, 0, sw - 1), _clamp(py, 0, sh - 1)

        # 4. Text label lookup in AT-SPI2 (only when caller explicitly passed text target for click/move/scroll)
        if step.get(text_k) and step.get("op") != "type":
            q = str(step[text_k])
            el = self.a11y.find_by_text(q)
            if el is None and active_win is not None:
                self.a11y.snapshot(
                    app_hint=active_win.get("app"),
                    title_hint=active_win.get("title"),
                    pid_hint=active_win.get("pid"),
                    window_id=active_win.get("id", ""),
                    diff=False,
                )
                el = self.a11y.find_by_text(q)
            if el is None:
                raise ValueError(f"No AT-SPI2 element matched text {q!r}. Use desktop_observe or coordinates.")
            cx, cy = el["center"]
            return int(cx), int(cy)
        if step.get("text_target"):
            q = str(step["text_target"])
            el = self.a11y.find_by_text(q)
            if el is None:
                raise ValueError(f"No AT-SPI2 element matched text_target {q!r}.")
            cx, cy = el["center"]
            return int(cx), int(cy)

        if required:
            raise ValueError(f"Missing target: provide {ref_k}, {x_k}/{y_k}, {nx_k}/{ny_k}, or text")
        return None

    def _is_terminal_active(self, active_win: dict[str, Any] | None) -> bool:
        if not active_win:
            return False
        app = (active_win.get("app") or "").lower()
        return any(tc in app for tc in TERMINAL_CLASSES)

    def _type_with_atomic_paste(self, text: str, active_win: dict[str, Any] | None) -> str:
        """Paste text via clipboard while atomically preserving and restoring prior clipboard contents."""
        prev_clip: str | None = None
        try:
            prev_clip = self.clipboard.get_text()
        except Exception:
            prev_clip = None

        self.clipboard.set_text(text)
        time.sleep(0.012)
        paste_combo = "ctrl+shift+v" if self._is_terminal_active(active_win) else "ctrl+v"
        self.input.hotkey(paste_combo)
        time.sleep(0.045)
        if prev_clip is not None:
            try:
                self.clipboard.set_text(prev_clip)
            except Exception:
                pass
        return f"paste({paste_combo})"

    def _check_user_takeover(self) -> tuple[bool, tuple[int, int] | None]:
        """Detect if the human physically moved the mouse >180px since our last scripted move."""
        expected = self.input._last_pos
        if expected is None:
            return False, None
        try:
            st = self.windows.state()
            cur = (int(st["cursor"]["x"]), int(st["cursor"]["y"]))
        except Exception:
            return False, None
        dist = math.hypot(cur[0] - expected[0], cur[1] - expected[1])
        if dist > 180.0:
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
        self.last_image_rect = (frame.origin_x, frame.origin_y, frame.width, frame.height)
        img_block = {
            "type": "image",
            "data": enc["data_b64"],
            "mimeType": enc["mime_type"],
        }
        meta = {k: v for k, v in enc.items() if k != "data_b64"}
        return img_block, meta

    # -----------------------------------------------------------------------
    # Tool implementations
    # -----------------------------------------------------------------------

    def tool_status(self, args: dict[str, Any]) -> list[dict[str, Any]]:
        sw, sh = self.windows.screen_size()
        uinput_writable = os.access("/dev/uinput", os.W_OK)
        session = "wayland" if os.environ.get("WAYLAND_DISPLAY") else ("x11" if os.environ.get("DISPLAY") else "headless")
        info = {
            "server": f"{SERVER_NAME} {SERVER_VERSION}",
            "session": session,
            "desktop": os.environ.get("XDG_CURRENT_DESKTOP", "unknown"),
            "screen": {"w": sw, "h": sh},
            "backends": {
                "capture": self.capture.backend,
                "input": self.input.backend,
                "windows": self.windows.backend,
                "clipboard": self.clipboard.backend,
                "a11y": "atspi2" if self.a11y.available else "unavailable",
            },
            "uinput_writable": uinput_writable,
            "cached_refs": len(self.a11y._ref_map),
            "last_image_rect": list(self.last_image_rect) if self.last_image_rect else None,
        }
        return [{"type": "text", "text": json.dumps(info, indent=2)}]

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
            res = self.windows.launch(
                cmd,
                wait_app=args.get("wait_app"),
                timeout_ms=int(args.get("timeout_ms", 4000)),
            )
            return [{"type": "text", "text": json.dumps(res, indent=2)}]
        res = self.windows.control(
            action,
            window_id=args.get("window_id"),
            app=args.get("app"),
            title=args.get("title"),
            x=args.get("x"),
            y=args.get("y"),
            w=args.get("w"),
            h=args.get("h"),
        )
        return [{"type": "text", "text": json.dumps(res, indent=2)}]

    def tool_clipboard(self, args: dict[str, Any]) -> list[dict[str, Any]]:
        action = (args.get("action") or "get").lower()
        if action == "get":
            txt = self.clipboard.get_text()
            return [{"type": "text", "text": txt}]
        if action == "set":
            val = str(args.get("text", ""))
            self.clipboard.set_text(val)
            return [{"type": "text", "text": f"Clipboard set ({len(val)} chars) via {self.clipboard.backend}"}]
        if action == "clear":
            self.clipboard.set_text("")
            return [{"type": "text", "text": f"Clipboard cleared via {self.clipboard.backend}"}]
        raise ValueError(f"Unsupported clipboard action: {action!r}")

    def tool_screenshot(self, args: dict[str, Any]) -> list[dict[str, Any]]:
        scope = (args.get("scope") or "screen").lower()
        annotate = bool(args.get("annotate", False))
        grid = bool(args.get("grid", False))
        include_cursor = bool(args.get("cursor", True))
        do_diff = bool(args.get("diff_from_last", True))
        max_width = int(args.get("max_width", 1280))
        quality = int(args.get("quality", 75))
        fmt = str(args.get("format", "jpeg"))

        st = self.windows.state()
        active = st.get("active")
        cursor_pos = (int(st["cursor"]["x"]), int(st["cursor"]["y"])) if include_cursor else None
        area: tuple[int, int, int, int] | None = None
        active_rect: tuple[int, int, int, int] | None = None
        wid: str | None = None

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
            sw, sh = int(st["screen"]["w"]), int(st["screen"]["h"])
            bx, by, bw, bh = el["bbox"]
            x0 = _clamp(bx - pad, 0, sw - 1)
            y0 = _clamp(by - pad, 0, sh - 1)
            x1 = _clamp(bx + bw + pad, x0 + 1, sw)
            y1 = _clamp(by + bh + pad, y0 + 1, sh)
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
            scope=scope,
            area=area,
            window_id=wid,
            active_rect=active_rect,
            include_cursor=include_cursor,
        )
        diff_info = self.capture.diff_from_last(frame) if do_diff else None
        anns = list(self.a11y._ref_map.values()) if annotate else None
        img_block, meta = self._encode_frame_block(
            frame,
            max_width=max_width,
            quality=quality,
            fmt=fmt,
            annotations=anns,
            grid=grid,
            cursor=cursor_pos,
        )

        header_parts = [
            f"SCREENSHOT ({frame.backend}): {meta['image_w']}x{meta['image_h']} {meta['mime_type']} ({meta['bytes'] // 1024} KB)",
            f"RECT: origin=({frame.origin_x},{frame.origin_y}) size={frame.width}x{frame.height}",
            f"TIMING: capture={meta['capture_ms']}ms encode={meta['encode_ms']}ms total={meta['total_ms']}ms",
        ]
        if cursor_pos:
            header_parts.append(f"CURSOR: ({cursor_pos[0]},{cursor_pos[1]})")
        if diff_info is not None:
            header_parts.append(f"DIFF: changed={diff_info['changed_pct']}% bbox={diff_info['changed_bbox']}")
        if grid:
            sw, sh = int(st["screen"]["w"]), int(st["screen"]["h"])
            is_full = (frame.origin_x == 0 and frame.origin_y == 0 and frame.width == sw and frame.height == sh)
            scope_hint = "screen" if is_full else "last_image"
            header_parts.append(f"GRID: 0..1000 (use nx/ny with coord_scope={scope_hint!r})")

        return [{"type": "text", "text": " | ".join(header_parts)}, img_block]

    def tool_observe(self, args: dict[str, Any]) -> list[dict[str, Any]]:
        t0 = time.perf_counter()
        mode = (args.get("mode") or "hybrid").lower()
        find_q = args.get("find")
        diff_opt = args.get("diff", "auto")
        ss_opt = args.get("screenshot", "auto")
        scope = (args.get("scope") or "screen").lower()
        max_elements = int(args.get("max_elements", 100))
        max_width = int(args.get("max_width", 1280))
        quality = int(args.get("quality", 75))

        st = self.windows.state()
        win_ms = (time.perf_counter() - t0) * 1000.0
        active = st.get("active")
        sw, sh = int(st["screen"]["w"]), int(st["screen"]["h"])
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

        # Compact open-windows summary line
        if st["windows"]:
            summary = ", ".join(
                f"{w['app']}{'*' if w.get('active') else ''}" for w in st["windows"][:10]
            )
            lines.append(f"OPEN_APPS ({len(st['windows'])}): {summary}")

        ax_res: dict[str, Any] | None = None
        if mode in ("hybrid", "ax"):
            ax_res = self.a11y.snapshot(
                app_hint=active.get("app") if active else None,
                title_hint=active.get("title") if active else None,
                pid_hint=active.get("pid") if active else None,
                window_id=active.get("id", "") if active else "",
                find=find_q,
                max_elements=max_elements,
                diff=diff_opt,
            )
            kind = "AX_DIFF (prior [ref] IDs still valid)" if ax_res["is_diff"] else f"AX_REFS ({len(ax_res['elements'])})"
            lines.append(f"{kind} | AX_MS: {ax_res['elapsed_ms']}")
            lines.append(ax_res["formatted"])

        # Decide whether to attach a visual screenshot
        n_ax = len(ax_res["elements"]) if ax_res else 0
        want_ss = False
        if ss_opt is True or str(ss_opt).lower() == "true":
            want_ss = True
        elif ss_opt is False or str(ss_opt).lower() == "false":
            want_ss = False
        else:
            # "auto": attach in vision mode, or in hybrid mode when AX has <5 elements
            want_ss = (mode == "vision") or (mode == "hybrid" and n_ax < 5)

        content: list[dict[str, Any]] = []
        if want_ss:
            active_rect = (int(active["x"]), int(active["y"]), int(active["w"]), int(active["h"])) if (scope == "active" and active) else None
            frame = self.capture.capture(scope=scope, active_rect=active_rect, include_cursor=True)
            diff_px = self.capture.diff_from_last(frame)
            annotate = bool(args["annotate"]) if "annotate" in args else (n_ax > 0)
            grid = bool(args["grid"]) if "grid" in args else (n_ax < 5)
            anns = (ax_res["elements"] if ax_res else []) if annotate else None
            img_block, meta = self._encode_frame_block(
                frame,
                max_width=max_width,
                quality=quality,
                annotations=anns,
                grid=grid,
                cursor=cursor_pos,
            )
            is_full = (frame.origin_x == 0 and frame.origin_y == 0 and frame.width == sw and frame.height == sh)
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

        verify_change = bool(args.get("verify_change", True))
        detect_takeover = bool(args.get("detect_takeover", True))
        settle_opt = args.get("settle", "smart")
        observe_opt = str(args.get("observe", "auto")).lower()
        max_width = int(args.get("max_width", 1280))
        quality = int(args.get("quality", 75))

        st_before = self.windows.state()
        active_win = st_before.get("active")
        before_frame: Frame | None = None
        if verify_change and self.capture.backend != "unavailable":
            try:
                before_frame = self.capture.capture(scope="screen", include_cursor=False)
            except Exception:
                before_frame = None

        step_results: list[dict[str, Any]] = []
        extra_images: list[dict[str, Any]] = []
        aborted_takeover = False

        for idx, raw_step in enumerate(raw_steps):
            if not isinstance(raw_step, dict):
                step_results.append({"step": idx, "ok": False, "error": "Step must be an object"})
                break
            if idx > 0 and detect_takeover:
                taken, cur_p = self._check_user_takeover()
                if taken:
                    aborted_takeover = True
                    step_results.append({
                        "step": idx,
                        "ok": False,
                        "takeover_detected": True,
                        "cursor": list(cur_p) if cur_p else None,
                        "error": "User moved the mouse >180px; aborting remaining steps to avoid fighting user.",
                    })
                    break

            op = (raw_step.get("op") or raw_step.get("action") or "").lower()
            try:
                res, imgs, active_win = self._exec_step(op, raw_step, active_win)
                step_results.append({"step": idx, "op": op, "ok": True, **res})
                extra_images.extend(imgs)
            except Exception as exc:
                step_results.append({"step": idx, "op": op, "ok": False, "error": str(exc)})
                if not raw_step.get("continue_on_error"):
                    break

        # Smart post-batch settle
        after_frame: Frame | None = None
        if not aborted_takeover:
            if settle_opt == "smart" and self.capture.backend != "unavailable":
                try:
                    after_frame = self.capture.wait_until_settled(scope="screen", max_wait_ms=260, poll_ms=45, include_cursor=True)
                except Exception:
                    after_frame = None
            elif isinstance(settle_opt, (int, float)) and settle_opt > 0:
                time.sleep(min(5.0, float(settle_opt) / 1000.0))

        diff_info: dict[str, Any] | None = None
        if before_frame is not None:
            if after_frame is None and self.capture.backend != "unavailable":
                try:
                    after_frame = self.capture.capture(scope="screen", include_cursor=True)
                except Exception:
                    after_frame = None
            if after_frame is not None:
                diff_info = self.capture.diff_frames(before_frame, after_frame)

        st_after = self.windows.state()
        active_after = st_after.get("active")
        total_ms = (time.perf_counter() - t0) * 1000.0

        lines = [
            f"ACT: {sum(1 for r in step_results if r.get('ok'))}/{len(raw_steps)} ok | {total_ms:.1f} ms | "
            f"ACTIVE: {(active_after['app'] + ' ' + repr(active_after['title'][:55])) if active_after else 'none'}"
        ]
        if diff_info is not None:
            lines.append(f"PIXEL_DIFF: changed={diff_info['changed_pct']}% bbox={diff_info['changed_bbox']}")
        lines.append("STEPS: " + json.dumps(step_results, separators=(",", ":")))

        out_blocks: list[dict[str, Any]] = []
        ax_res: dict[str, Any] | None = None
        if observe_opt in ("auto", "ax") and not aborted_takeover:
            ax_res = self.a11y.snapshot(
                app_hint=active_after.get("app") if active_after else None,
                title_hint=active_after.get("title") if active_after else None,
                pid_hint=active_after.get("pid") if active_after else None,
                window_id=active_after.get("id", "") if active_after else "",
                diff="auto",
            )
            hdr = "AX_DIFF" if ax_res["is_diff"] else f"AX_REFS ({len(ax_res['elements'])})"
            lines.append(f"{hdr} ({ax_res['elapsed_ms']}ms):\n{ax_res['formatted']}")

        want_img = False
        crop_to_diff = False
        if observe_opt == "vision" or args.get("screenshot") is True:
            want_img = True
        elif observe_opt == "crop_changed":
            want_img = True
            crop_to_diff = True
        elif observe_opt == "auto" and not aborted_takeover:
            # Attach a fast visual frame if AX tree is empty/sparse so model always has ground truth
            n_ax = len(ax_res["elements"]) if ax_res else 0
            if n_ax < 5:
                want_img = True

        if want_img and self.capture.backend != "unavailable":
            frame = after_frame or self.capture.capture(scope="screen", include_cursor=True)
            if crop_to_diff and diff_info and diff_info.get("changed_bbox"):
                bx, by, bw, bh = diff_info["changed_bbox"]
                pad = 64
                x0 = _clamp(bx - pad, 0, frame.width - 1)
                y0 = _clamp(by - pad, 0, frame.height - 1)
                x1 = _clamp(bx + bw + pad, x0 + 1, frame.width)
                y1 = _clamp(by + bh + pad, y0 + 1, frame.height)
                sub_rgba = frame.rgba[y0:y1, x0:x1, :].copy()
                frame = Frame(
                    rgba=sub_rgba,
                    origin_x=frame.origin_x + x0,
                    origin_y=frame.origin_y + y0,
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

    def _exec_step(
        self,
        op: str,
        step: dict[str, Any],
        active_win: dict[str, Any] | None,
    ) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any] | None]:
        extra_images: list[dict[str, Any]] = []

        if op == "click":
            pt = self.resolve_point(step, active_win=active_win, required=False)
            button = str(step.get("button", "left"))
            count = 2 if step.get("double") else int(step.get("count", 1))
            mods = step.get("modifiers")
            if isinstance(mods, str):
                mods = [m.strip() for m in mods.split("+") if m.strip()]
            pos = self.input.click(
                pt[0] if pt else None,
                pt[1] if pt else None,
                button=button,
                count=count,
                modifiers=mods,
            )
            return {"pos": list(pos) if pos else None, "button": button, "count": count}, extra_images, active_win

        if op in ("move", "hover"):
            pt = self.resolve_point(step, active_win=active_win, required=True)
            assert pt is not None
            pos = self.input.move(pt[0], pt[1])
            return {"pos": list(pos)}, extra_images, active_win

        if op == "drag":
            p1 = self.resolve_point(step, active_win=active_win, prefix="from_", required=True)
            p2 = self.resolve_point(step, active_win=active_win, prefix="to_", required=True)
            assert p1 is not None and p2 is not None
            start, end = self.input.drag(
                p1[0], p1[1], p2[0], p2[1],
                button=str(step.get("button", "left")),
                duration_ms=int(step.get("duration_ms", 140)),
                steps=int(step.get("steps", 14)),
            )
            return {"from": list(start), "to": list(end)}, extra_images, active_win

        if op == "scroll":
            pt = self.resolve_point(step, active_win=active_win, required=False)
            direction = str(step.get("direction", "down"))
            clicks = int(step.get("clicks", step.get("amount", 5)))
            self.input.scroll(direction=direction, clicks=clicks, x=pt[0] if pt else None, y=pt[1] if pt else None)
            return {"direction": direction, "clicks": clicks, "pos": list(pt) if pt else None}, extra_images, active_win

        if op == "type":
            text = str(step.get("text", ""))
            # Optional target click to focus the input field first
            pt = self.resolve_point(step, active_win=active_win, required=False)
            if pt is not None:
                self.input.click(pt[0], pt[1])
                time.sleep(0.03)
                active_win = self.windows.state().get("active")
            if step.get("clear"):
                self.input.hotkey("ctrl+a")
                time.sleep(0.015)
                self.input.hotkey("backspace")
                time.sleep(0.015)

            mode = str(step.get("mode", "auto")).lower()
            use_paste = (mode == "paste") or (
                mode == "auto" and (len(text) > 24 or not self.input.is_pure_ascii_typable(text))
            )
            if use_paste and self.clipboard.backend != "unavailable":
                method = self._type_with_atomic_paste(text, active_win)
            else:
                self.input.type_keys(text, key_delay_ms=int(step.get("key_delay_ms", 0)))
                method = "uinput-keys"

            if step.get("submit"):
                time.sleep(0.02)
                self.input.hotkey("enter")
            return {"chars": len(text), "method": method, "submitted": bool(step.get("submit"))}, extra_images, active_win

        if op in ("key", "hotkey", "press"):
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
            res = self.a11y.do_action(int(rid), action_name=step.get("action_name"))
            return res, extra_images, active_win

        if op == "focus":
            res = self.windows.control(
                "focus",
                window_id=step.get("window_id"),
                app=step.get("app"),
                title=step.get("title"),
            )
            time.sleep(0.04)
            active_win = self.windows.state().get("active")
            return {"focused": res.get("app"), "title": res.get("title")}, extra_images, active_win

        if op == "window":
            wact = str(step.get("window_action") or step.get("subaction") or "focus")
            res = self.windows.control(
                wact,
                window_id=step.get("window_id"),
                app=step.get("app"),
                title=step.get("title"),
                x=step.get("x"), y=step.get("y"), w=step.get("w"), h=step.get("h"),
            )
            time.sleep(0.04)
            active_win = self.windows.state().get("active")
            return res, extra_images, active_win

        if op == "launch":
            res = self.windows.launch(
                step["command"],
                wait_app=step.get("wait_app"),
                timeout_ms=int(step.get("timeout_ms", 4000)),
            )
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
                )
                return res, extra_images, active_win
            if step.get("app"):
                q = str(step["app"]).lower()
                deadline = time.monotonic() + timeout_ms / 1000.0
                while time.monotonic() < deadline:
                    w = self.windows.find_window(app=q) or self.windows.find_window(title=q)
                    if w is not None:
                        return {"found": True, "window": w}, extra_images, active_win
                    time.sleep(0.06)
                return {"found": False, "app": q}, extra_images, active_win
            if step.get("text"):
                q = str(step["text"])
                deadline = time.monotonic() + timeout_ms / 1000.0
                while time.monotonic() < deadline:
                    st = self.windows.state()
                    aw = st.get("active")
                    snap = self.a11y.snapshot(
                        app_hint=aw.get("app") if aw else None,
                        title_hint=aw.get("title") if aw else None,
                        pid_hint=aw.get("pid") if aw else None,
                        window_id=aw.get("id", "") if aw else "",
                        find=q,
                        diff=False,
                    )
                    if snap["elements"] or snap["text_lines"]:
                        return {"found": True, "matches": len(snap["elements"]) + len(snap["text_lines"])}, extra_images, aw
                    time.sleep(0.1)
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
# MCP Tool Schemas
# ---------------------------------------------------------------------------

TOOLS: list[dict[str, Any]] = [
    {
        "name": "desktop_observe",
        "description": (
            "Observe the Linux desktop in 1 call: KWin window list + active window + cursor (~4 ms) "
            "+ AT-SPI2 accessibility tree with stable [ref] IDs and incremental DIFFs + automatic "
            "fast JPEG screenshot (~31 ms) with Set-of-Marks/grid when the active app has sparse AX."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["hybrid", "ax", "vision", "windows"], "default": "hybrid"},
                "find": {"type": "string", "description": "Filter AX elements and text by substring."},
                "diff": {"oneOf": [{"type": "string", "enum": ["auto"]}, {"type": "boolean"}], "default": "auto"},
                "screenshot": {"oneOf": [{"type": "string", "enum": ["auto"]}, {"type": "boolean"}], "default": "auto"},
                "scope": {"type": "string", "enum": ["screen", "active"], "default": "screen"},
                "annotate": {"type": "boolean", "description": "Draw Set-of-Marks [ref] badges on screenshot."},
                "grid": {"type": "boolean", "description": "Draw 0..1000 normalized coordinate grid on screenshot."},
                "max_elements": {"type": "integer", "default": 100},
                "max_width": {"type": "integer", "default": 1280},
                "quality": {"type": "integer", "default": 75},
            },
        },
    },
    {
        "name": "desktop_act",
        "description": (
            "Execute one or more desktop actions in a single batched call via direct kernel /dev/uinput "
            "(<0.05 ms emit, pixel-exact on Wayland/X11) with automatic smart settle, pre/post pixel diff, "
            "human-takeover safety abort, and compact AX diff or visual verification."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "steps": {
                    "type": "array",
                    "description": (
                        "Ordered action steps. Ops: click, move/hover, drag, scroll, type, key/hotkey, "
                        "ax_action, focus, window, launch, wait, screenshot."
                    ),
                    "items": {"type": "object"},
                },
                "op": {"type": "string", "description": "Single-step shorthand op when 'steps' is omitted."},
                "settle": {"oneOf": [{"type": "string", "enum": ["smart", "none"]}, {"type": "integer"}], "default": "smart"},
                "observe": {"type": "string", "enum": ["auto", "ax", "vision", "crop_changed", "none"], "default": "auto"},
                "verify_change": {"type": "boolean", "default": True},
                "detect_takeover": {"type": "boolean", "default": True},
                "screenshot": {"type": "boolean"},
                "annotate": {"type": "boolean"},
                "grid": {"type": "boolean"},
                "max_width": {"type": "integer", "default": 1280},
                "quality": {"type": "integer", "default": 75},
            },
        },
    },
    {
        "name": "desktop_screenshot",
        "description": (
            "Capture a fast screenshot of the full screen (~31 ms), active window, named window, "
            "pixel area [x,y,w,h], or [ref] element, with optional Set-of-Marks [ref] badges, "
            "0..1000 coordinate grid, and frame-to-frame pixel diff."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
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
        "description": "List, focus, minimize, maximize, unmaximize, move/resize, close, or launch desktop windows (~4 ms on KWin).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "focus", "minimize", "maximize", "unmaximize", "close", "move_resize", "launch"],
                    "default": "list",
                },
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
        "name": "desktop_clipboard",
        "description": "Read, write, or clear the Linux clipboard (~0.4 ms via Klipper DBus, with wl-clipboard/xclip fallbacks).",
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
        "description": "Report active Linux desktop backends (capture, uinput, window manager, clipboard, AT-SPI2) and screen geometry.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


class MCPServer:
    """Newline-delimited JSON-RPC 2.0 MCP server (serialized on main thread for GLib/DBus safety)."""

    def __init__(self, engine: DesktopEngine | None = None):
        self.engine = engine or DesktopEngine()
        self.handlers = {
            "desktop_observe": self.engine.tool_observe,
            "desktop_act": self.engine.tool_act,
            "desktop_screenshot": self.engine.tool_screenshot,
            "desktop_windows": self.engine.tool_windows,
            "desktop_clipboard": self.engine.tool_clipboard,
            "desktop_status": self.engine.tool_status,
        }

    def handle_request(self, req: dict[str, Any]) -> dict[str, Any] | None:
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

    def serve_stdio(self) -> None:
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
                resp = self.handle_request(req)
                if resp is not None:
                    sys.stdout.write(json.dumps(resp, separators=(",", ":")) + "\n")
                    sys.stdout.flush()
        finally:
            self.engine.shutdown()


# ---------------------------------------------------------------------------
# Non-destructive --selftest and --bench CLI helpers
# ---------------------------------------------------------------------------


def run_selftest(server: MCPServer) -> int:
    failures: list[str] = []

    def check(label: str, cond: bool, detail: str = "") -> None:
        tag = "PASS" if cond else "FAIL"
        print(f"  [{tag}] {label}" + (f": {detail}" if detail else ""))
        if not cond:
            failures.append(label)

    print("Running computer-use MCP non-destructive selftest...")
    r_init = server.handle_request({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": PROTOCOL_VERSION}})
    check("MCP initialize", bool(r_init and r_init["result"]["serverInfo"]["name"] == SERVER_NAME))

    r_list = server.handle_request({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    tools = (r_list or {}).get("result", {}).get("tools", [])
    check("MCP tools/list", len(tools) == 6, f"{len(tools)} tools")

    r_stat = server.handle_request({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "desktop_status", "arguments": {}}})
    st_json = json.loads(r_stat["result"]["content"][0]["text"])
    check("desktop_status", not r_stat["result"]["isError"], json.dumps(st_json["backends"]))

    r_win = server.handle_request({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "desktop_windows", "arguments": {"action": "list"}}})
    check("desktop_windows(list)", not r_win["result"]["isError"], r_win["result"]["content"][0]["text"].splitlines()[0])

    if server.engine.clipboard.backend != "unavailable":
        r_clip = server.handle_request({"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "desktop_clipboard", "arguments": {"action": "get"}}})
        check("desktop_clipboard(get)", not r_clip["result"]["isError"], f"backend={server.engine.clipboard.backend}")

    if server.engine.capture.backend != "unavailable":
        r_ss = server.handle_request({
            "jsonrpc": "2.0", "id": 6, "method": "tools/call",
            "params": {"name": "desktop_screenshot", "arguments": {"scope": "area", "area": [0, 0, 400, 300], "grid": True}},
        })
        ok_ss = (not r_ss["result"]["isError"]) and len(r_ss["result"]["content"]) == 2 and r_ss["result"]["content"][1]["type"] == "image"
        check("desktop_screenshot(area+grid)", ok_ss, r_ss["result"]["content"][0]["text"])

    r_obs = server.handle_request({
        "jsonrpc": "2.0", "id": 7, "method": "tools/call",
        "params": {"name": "desktop_observe", "arguments": {"mode": "hybrid", "screenshot": False}},
    })
    check("desktop_observe(hybrid)", not r_obs["result"]["isError"], r_obs["result"]["content"][0]["text"].splitlines()[0])

    print(f"Selftest result: {'ALL PASSED' if not failures else f'{len(failures)} FAILED: {failures}'}")
    return 0 if not failures else 1


def run_bench(server: MCPServer, runs: int = 5) -> int:
    eng = server.engine
    print(f"\nBenchmarking computer-use backends (median of {runs} runs)...")

    def bench(name: str, fn) -> None:
        times: list[float] = []
        extra = ""
        # Warmup once
        res = fn()
        for _ in range(runs):
            t0 = time.perf_counter()
            res = fn()
            times.append((time.perf_counter() - t0) * 1000.0)
        if isinstance(res, str):
            extra = f"  ({res})"
        med = statistics.median(times)
        lo = min(times)
        print(f"  {name:<36} median={med:6.2f} ms   min={lo:6.2f} ms{extra}")

    bench(f"windows.state [{eng.windows.backend}]", lambda: f"{len(eng.windows.state()['windows'])} windows")
    if eng.clipboard.backend != "unavailable":
        bench(f"clipboard.get [{eng.clipboard.backend}]", lambda: f"{len(eng.clipboard.get_text())} chars")
    if eng.capture.backend != "unavailable":
        f_full: list[Frame] = []

        def cap_full():
            f = eng.capture.capture(scope="screen", include_cursor=True)
            f_full[:] = [f]
            return f"{f.width}x{f.height}"

        bench(f"capture full screen [{eng.capture.backend}]", cap_full)
        bench(f"capture 400x300 area [{eng.capture.backend}]", lambda: f"{eng.capture.capture(scope='area', area=(0,0,400,300)).width}x300")
        bench("encode 1280w JPEG q=75", lambda: f"{eng.capture.encode(f_full[0], max_width=1280, quality=75)['bytes']//1024} KB")
        bench("encode 1280w JPEG + grid + cursor", lambda: f"{eng.capture.encode(f_full[0], max_width=1280, quality=75, grid=True, cursor=(400,300))['bytes']//1024} KB")
        f2 = eng.capture.capture(scope="screen", include_cursor=True)
        bench("numpy diff_frames (1080p)", lambda: f"changed={eng.capture.diff_frames(f_full[0], f2)['changed_pct']}%")
    if eng.a11y.available:
        st = eng.windows.state()
        aw = st.get("active")
        bench(
            "a11y.snapshot (active window)",
            lambda: f"{len(eng.a11y.snapshot(app_hint=aw['app'] if aw else None, title_hint=aw['title'] if aw else None, pid_hint=aw['pid'] if aw else None, diff=False)['elements'])} refs",
        )
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Antigravity Linux Computer Use MCP server")
    parser.add_argument("--selftest", action="store_true", help="Run non-destructive protocol and backend checks")
    parser.add_argument("--bench", action="store_true", help="Run non-destructive latency benchmarks")
    parser.add_argument("--runs", type=int, default=5, help="Iterations per benchmark")
    args = parser.parse_args()

    server = MCPServer()
    if args.selftest or args.bench:
        rc = 0
        try:
            if args.selftest:
                rc |= run_selftest(server)
            if args.bench:
                rc |= run_bench(server, runs=max(1, args.runs))
        finally:
            server.engine.shutdown()
        sys.exit(rc)

    server.serve_stdio()


if __name__ == "__main__":
    main()
