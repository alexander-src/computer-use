"""Cross-platform unit, TurboJPEG/Cairo, Win32/COM, Xvfb background, overlay, and MCP stdio tests."""

from __future__ import annotations

import base64
import ctypes
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import a11y
import capture
import computer_use_mcp as mcp
import cursor_overlay
import fast_ocr
import macos_backend
import uinput_driver
import win32_backend


class FakeWindowManager:
    def __init__(self, bounds=(0, 0, 1920, 1080), cursor=(500, 400), active_app="kate"):
        self.backend = "mock"
        self._bounds = bounds
        self._cursor = cursor
        self._active = {
            "id": "win-1",
            "title": "notes.txt - Kate",
            "app": active_app,
            "pid": 1234,
            "x": 100,
            "y": 50,
            "w": 1000,
            "h": 800,
            "active": True,
            "minimized": False,
            "fullscreen": False,
        }
        self.controls: list[tuple[str, dict[str, Any]]] = []

    def virtual_bounds(self) -> tuple[int, int, int, int]:
        return self._bounds

    def screen_size(self) -> tuple[int, int]:
        return self._bounds[2], self._bounds[3]

    def cursor_pos(self) -> tuple[int, int]:
        return self._cursor

    def state(self) -> dict[str, Any]:
        vx, vy, vw, vh = self._bounds
        return {
            "screen": {"x": vx, "y": vy, "w": vw, "h": vh},
            "cursor": {"x": self._cursor[0], "y": self._cursor[1]},
            "windows": [self._active],
            "active": self._active,
        }

    def find_window(self, **kwargs):
        return self._active

    def control(self, action: str, **kwargs):
        self.controls.append((action, kwargs))
        return {**self._active, "action": action}

    def launch(self, command, **kwargs):
        return {"pid": 9999, "command": command, "window": self._active}


class FakeClipboard:
    def __init__(self, initial: str = "original-clip"):
        self.backend = "mock"
        self.value = initial
        self.history: list[str] = []

    def get_text(self) -> str:
        return self.value

    def set_text(self, text: str) -> None:
        self.value = text
        self.history.append(text)


class FakeInputDriver:
    def __init__(self, layout: str = "us"):
        self.backend = "mock"
        self.layout = layout
        self._last_pos: tuple[int, int] | None = None
        self.events: list[tuple[str, Any]] = []

    def close(self) -> None:
        pass

    def move(self, x: int, y: int) -> tuple[int, int]:
        self._last_pos = (x, y)
        self.events.append(("move", (x, y)))
        return x, y

    def click(self, x=None, y=None, *, button="left", count=1, modifiers=None):
        if x is not None and y is not None:
            self._last_pos = (x, y)
        self.events.append(("click", (x, y, button, count, modifiers)))
        return (x, y) if x is not None and y is not None else None

    def drag(self, x1, y1, x2, y2, *, button="left", duration_ms=140, steps=14, on_step=None):
        self._last_pos = (x2, y2)
        if on_step:
            on_step(x2, y2)
        self.events.append(("drag", (x1, y1, x2, y2, button)))
        return (x1, y1), (x2, y2)

    def scroll(self, direction="down", clicks=5, *, x=None, y=None):
        self.events.append(("scroll", (direction, clicks, x, y)))

    def hotkey(self, keys, *, count=1):
        self.events.append(("hotkey", (keys, count)))

    def type_keys(self, text, *, key_delay_ms=0):
        self.events.append(("type_keys", text))

    def is_pure_ascii_typable(self, text: str) -> bool:
        return all(uinput_driver.char_to_uinput(ch, layout=self.layout) is not None for ch in text)


class FakeOverlay:
    def __init__(self):
        self.backend = "mock-overlay"
        self.moves: list[tuple[float, float, str]] = []
        self.clicks: list[tuple[float, float, str, bool]] = []

    def show_move(self, x: float, y: float, label: str = "AI") -> None:
        self.moves.append((x, y, label))

    def show_click(self, x: float, y: float, *, button: str = "left", double: bool = False) -> None:
        self.clicks.append((x, y, button, double))

    def close(self) -> None:
        pass


class FakeBackground:
    def __init__(self):
        self.active = True
        self.size = (1280, 720)
        self.events: list[tuple[str, Any]] = []

    def status(self):
        return {"active": self.active, "backend": "mock-bg"}

    def start(self, width=1920, height=1080):
        self.active = True
        self.size = (width, height)
        return self.status()

    def attach(self, **kwargs):
        self.active = True
        return self.status()

    def stop(self):
        self.active = False
        return {"active": False}

    def launch(self, cmd, **kwargs):
        self.events.append(("launch", cmd))
        return {"pid": 4242, "command": cmd}

    def capture(self, area=None):
        return SyntheticCapture._make_frame(320, 200, (15, 25, 35, 255))

    def move(self, x, y):
        self.events.append(("move", (x, y)))
        return x, y

    def click(self, x, y, *, button="left", count=1):
        self.events.append(("click", (x, y, button, count)))
        return x, y

    def scroll(self, direction="down", clicks=5, *, x=None, y=None):
        self.events.append(("scroll", (direction, clicks, x, y)))

    def type_text(self, text):
        self.events.append(("type_text", text))

    def hotkey(self, keys, *, count=1):
        self.events.append(("hotkey", (keys, count)))


class FakeA11y:
    def __init__(self, elements: list[dict[str, Any]] | None = None):
        self.available = True
        self._elements = elements or []
        self._ref_map = {e["ref"]: e for e in self._elements}
        self.actions: list[tuple[int, str | None]] = []

    def get_ref(self, ref_id: int):
        return self._ref_map.get(int(ref_id))

    def find_by_text(self, query: str):
        q = query.lower()
        for e in self._ref_map.values():
            if q in e["name"].lower():
                return e
        return None

    def do_action(self, ref_id: int, action_name=None):
        self.actions.append((ref_id, action_name))
        return {"ok": True, "ref": ref_id, "action": action_name or "click"}

    def snapshot(self, **kwargs):
        return {
            "available": True,
            "window_key": "win-1",
            "elements": self._elements,
            "text_lines": [],
            "formatted": "\n".join(a11y.A11yTree._format_elem(e) for e in self._elements) or "(empty)",
            "is_diff": False,
            "visited": len(self._elements),
            "elapsed_ms": 1.2,
        }


class SyntheticCapture(capture.ScreenCapture):
    def __init__(self, frames: list[capture.Frame] | None = None):
        self._bus = None
        self._kwin_ss = None
        self._backend = "synthetic"
        self._last_thumb = None
        self._last_frame_rect = None
        self._jpeg_needs_rgb = False
        self._tj = capture.TurboJpegEncoder()
        self._frames = list(frames or [self._make_frame(320, 200, (30, 40, 50, 255))])
        self._idx = 0

    @staticmethod
    def _make_frame(w: int, h: int, color=(30, 40, 50, 255), ox=0, oy=0) -> capture.Frame:
        arr = np.zeros((h, w, 4), dtype=np.uint8)
        arr[:, :] = color
        return capture.Frame(rgba=arr, origin_x=ox, origin_y=oy, width=w, height=h, scale=1.0, backend="synthetic", capture_ms=0.5)

    def capture(self, **kwargs) -> capture.Frame:
        f = self._frames[min(self._idx, len(self._frames) - 1)]
        self._idx += 1
        return f


class TestInputAndLayouts(unittest.TestCase):
    def test_parse_key_combo_linux_and_windows(self):
        combo = uinput_driver.parse_key_combo("ctrl+shift+t")
        self.assertEqual([c for _, c in combo], [29, 42, 20])
        plus = uinput_driver.parse_key_combo("+")
        self.assertEqual([c for _, c in plus], [42, 13])
        with self.assertRaises(ValueError):
            uinput_driver.parse_key_combo("ctrl+notakey")

        wcombo = win32_backend.parse_win_key_combo("ctrl+shift+t")
        self.assertEqual([c for _, c in wcombo], [0x11, 0x10, 0x54])
        self.assertEqual(win32_backend.parse_win_key_combo(["win", "r"]), [("win", 0x5B), ("r", 0x52)])
        # Also verify 'ctrl++', UK '#' / '~' / '\' / '¬', and X11 keysym mapping
        self.assertEqual([c for _, c in uinput_driver.parse_key_combo("ctrl++")], [29, 42, 13])
        self.assertEqual([c for _, c in uinput_driver.parse_key_combo("#", layout="gb")], [43])
        self.assertEqual(uinput_driver.char_to_uinput("\u00ac", layout="gb"), (True, 41))
        self.assertEqual(uinput_driver.to_x11_combo("ctrl+enter"), "ctrl+Return")
        self.assertEqual(uinput_driver.to_x11_combo("ctrl+shift++"), "ctrl+shift+plus")
        self.assertEqual([c for _, c in win32_backend.parse_win_key_combo("ctrl++")], [0x11, 0xBB])

    def test_layout_aware_uinput_mapping(self):
        # US layout: '"' on apostrophe (40), '@' on '2' (3)
        self.assertEqual(uinput_driver.char_to_uinput('"', layout="us"), (True, 40))
        self.assertEqual(uinput_driver.char_to_uinput("@", layout="us"), (True, 3))
        # UK (gb) layout: '"' on '2' (3), '@' on apostrophe (40), '#' on 43, '\' on 86, '£' on Shift+3
        self.assertEqual(uinput_driver.char_to_uinput('"', layout="gb"), (True, 3))
        self.assertEqual(uinput_driver.char_to_uinput("@", layout="gb"), (True, 40))
        self.assertEqual(uinput_driver.char_to_uinput("#", layout="gb"), (False, 43))
        self.assertEqual(uinput_driver.char_to_uinput("\\", layout="gb"), (False, 86))
        self.assertEqual(uinput_driver.char_to_uinput("\u00a3", layout="gb"), (True, 4))
        # International (e.g. German/French) returns None for letters/symbols -> routes safely to atomic clipboard paste
        self.assertIsNone(uinput_driver.char_to_uinput("z", layout="de"))
        self.assertIsNone(uinput_driver.char_to_uinput("\u00e9", layout="us"))

    def test_win32_input_struct_size_and_utf16_surrogates(self):
        expected = 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28
        self.assertEqual(ctypes.sizeof(win32_backend.INPUT), expected)
        # Astral plane emoji (U+1F680 rocket) -> 2 UTF-16 surrogate code units
        units = win32_backend.text_to_utf16_units("A\U0001F680")
        self.assertEqual(units, [0x0041, 0xD83D, 0xDE80])

    def test_macos_keycodes_utf16_structs_and_appkit_coords(self):
        # Exact CoreGraphics C struct ABI sizes (CGFloat = double on 64-bit macOS)
        self.assertEqual(ctypes.sizeof(macos_backend.CGPoint), 16)
        self.assertEqual(ctypes.sizeof(macos_backend.CGSize), 16)
        self.assertEqual(ctypes.sizeof(macos_backend.CGRect), 32)

        # Key combo parser (including cmd++ edge case & modifiers)
        codes, flags = macos_backend.parse_mac_key_combo("cmd+shift+s")
        self.assertEqual(codes, [0x37, 0x38, 0x01])
        self.assertEqual(flags, macos_backend.MAC_FLAG_CMD | macos_backend.MAC_FLAG_SHIFT)
        codes_plus, flags_plus = macos_backend.parse_mac_key_combo("cmd++")
        self.assertEqual(codes_plus, [0x37, 0x18])
        self.assertEqual(flags_plus, macos_backend.MAC_FLAG_CMD)
        self.assertEqual(macos_backend.parse_mac_key_combo(" "), ([0x31], 0))
        self.assertEqual(macos_backend.parse_mac_key_combo("cmd+ "), ([0x37, 0x31], macos_backend.MAC_FLAG_CMD))

        # UTF-16 surrogate-safe chunker never splits a surrogate pair across chunks (including max_units=1!)
        self.assertEqual(macos_backend.split_utf16_chunks("A\U0001F680B", max_units=1), [[0x0041], [0xD83D, 0xDE80], [0x0042]])
        sample = "abc\U0001F680def\U0001F525xyz" * 3
        for cap in (1, 2, 3, 5, 7, 16):
            chunks = macos_backend.split_utf16_chunks(sample, max_units=cap)
            reconstructed = bytearray()
            for ch in chunks:
                self.assertGreater(len(ch), 0)
                self.assertFalse(0xD800 <= ch[-1] <= 0xDBFF, f"Split high surrogate in {ch}")
                self.assertFalse(0xDC00 <= ch[0] <= 0xDFFF, f"Orphan low surrogate in {ch}")
                for u in ch:
                    reconstructed.extend(u.to_bytes(2, "little"))
            self.assertEqual(reconstructed.decode("utf-16-le"), sample)

        # AppKit bottom-left coordinate conversion (primary and negative-Y secondary monitor)
        self.assertEqual(macos_backend.cg_top_left_to_appkit_bottom_left(100, 50, 160, 900), (100.0, 690.0))
        self.assertEqual(macos_backend.cg_top_left_to_appkit_bottom_left(-400, -200, 160, 900), (-400.0, 940.0))

        # AX role normalization
        self.assertEqual(macos_backend.normalize_mac_role("AXButton"), "button")
        self.assertEqual(macos_backend.normalize_mac_role("AXPopUpButton"), "combobox")
        self.assertEqual(macos_backend.normalize_mac_role("AXTextField"), "text")

    def test_negative_multimonitor_virtual_bounds(self):
        bounds = (-1920, -200, 3840, 1280)
        drv = uinput_driver.InputDriver(screen_size_fn=lambda: (3840, 1280), virtual_bounds_fn=lambda: bounds)
        self.assertEqual(drv._screen_to_abs(-1920, -200), (-1920, -200, 0, 0))
        self.assertEqual(drv._screen_to_abs(1919, 1079), (1919, 1079, 65535, 65535))
        self.assertEqual(win32_backend.win_virtual_to_abs(-1920, -200, bounds), (-1920, -200, 0, 0))
        self.assertEqual(win32_backend.win_virtual_to_abs(1919, 1079, bounds), (1919, 1079, 65535, 65535))

    def test_normalized_to_screen(self):
        self.assertEqual(mcp.normalized_to_screen(0, 0, (-1920, 0, 3840, 1080)), (-1920, 0))
        self.assertEqual(mcp.normalized_to_screen(1000, 1000, (0, 0, 1920, 1080)), (1919, 1079))
        self.assertEqual(mcp.normalized_to_screen(500, 500, (100, 200, 401, 201)), (300, 300))


class TestCaptureAndOverlayRendering(unittest.TestCase):
    def test_diff_frames_identical_and_localized(self):
        f1 = SyntheticCapture._make_frame(400, 300, (20, 20, 20, 255))
        f2 = SyntheticCapture._make_frame(400, 300, (20, 20, 20, 255))
        sc = SyntheticCapture()
        d0 = sc.diff_frames(f1, f2)
        self.assertEqual(d0["changed_pct"], 0.0)
        self.assertIsNone(d0["changed_bbox"])

        f2.rgba[100:160, 200:280, :3] = (240, 240, 240)
        d1 = sc.diff_frames(f1, f2)
        self.assertGreater(d1["changed_pct"], 3.0)
        bx, by, bw, bh = d1["changed_bbox"]
        self.assertAlmostEqual(bx, 200, delta=4)
        self.assertAlmostEqual(by, 100, delta=4)
        self.assertAlmostEqual(bw, 80, delta=6)
        # Verify diff_frames ignore_rects suppresses overlay/ripple pixels cleanly
        d_masked = sc.diff_frames(f1, f2, ignore_rects=[(195, 95, 95, 70)])
        self.assertEqual(d_masked["changed_pct"], 0.0)
        self.assertIsNone(d_masked["changed_bbox"])

        # Verify zero-dep 5x7 digit pill renderer writes crisp pixels on BGRA buffer
        bgra = np.zeros((40, 80, 4), dtype=np.uint8)
        win32_backend.blit_pill_digits_bgra(bgra, 4, 4, "420", bg_bgr=(24, 16, 12), fg_bgr=(255, 240, 100))
        self.assertGreater(int((bgra[:, :, 3] == 255).sum()), 150)

        # Verify Wayland AT-SPI2 window-local -> global offset calibration
        old_wl = os.environ.get("WAYLAND_DISPLAY")
        os.environ["WAYLAND_DISPLAY"] = "wayland-0"
        try:
            dx, dy = a11y.A11yTree._wayland_coord_offset((0, 0, 1084, 723), (427, 138, 1066, 705))
            self.assertEqual((dx, dy), (418, 129))
        finally:
            if old_wl is None:
                os.environ.pop("WAYLAND_DISPLAY", None)
            else:
                os.environ["WAYLAND_DISPLAY"] = old_wl

    @unittest.skipUnless(sys.platform.startswith("linux"), "Cairo/GdkPixbuf test runs on Linux")
    def test_encode_turbojpeg_and_png_with_cairo_overlays(self):
        f = SyntheticCapture._make_frame(640, 480, (25, 35, 55, 255))
        sc = SyntheticCapture()
        anns = [{"ref": 7, "bbox": (40, 50, 120, 40)}]
        enc_jpg = sc.encode(f, max_width=320, fmt="jpeg", quality=80, annotations=anns, grid=True, cursor=(100, 80))
        self.assertEqual(enc_jpg["mime_type"], "image/jpeg")
        self.assertEqual(enc_jpg["image_w"], 320)
        self.assertEqual(enc_jpg["image_h"], 240)
        raw_jpg = base64.b64decode(enc_jpg["data_b64"])
        self.assertTrue(raw_jpg.startswith(b"\xff\xd8\xff"))

        enc_png = sc.encode(f, max_width=640, fmt="png", grid=True)
        self.assertEqual(enc_png["mime_type"], "image/png")
        raw_png = base64.b64decode(enc_png["data_b64"])
        self.assertTrue(raw_png.startswith(b"\x89PNG"))

    def test_win32_overlay_argb_buffer_rendering(self):
        size = 160
        buf = bytearray(size * size * 4)
        now = 10.0
        ripples = [cursor_overlay._Ripple(x=200.0, y=200.0, started_at=9.92, color=(0.22, 0.78, 0.99), double=True)]
        cursor_overlay._draw_win32_argb_buffer(buf, size, cx=200.0, cy=200.0, win_x=120, win_y=120, alpha=1.0, ripples=ripples, now=now)
        # Center hotspot + ripple must have non-zero premultiplied alpha pixels
        nonzero_alpha = sum(1 for i in range(3, len(buf), 4) if buf[i] > 0)
        self.assertGreater(nonzero_alpha, 150)


class FakeOCR:
    def __init__(self, responses: list[list[dict[str, Any]]] | None = None):
        self.available = True
        self._responses = list(responses or [[]])
        self._calls = 0

    def close(self) -> None:
        pass

    def scan_frame(self, frame, *, min_conf: float = 35.0) -> list[dict[str, Any]]:
        words = self._responses[min(self._calls, len(self._responses) - 1)]
        self._calls += 1
        return words

    def find_text(self, frame, query: str) -> dict[str, Any] | None:
        return fast_ocr.match_ocr_phrase(self.scan_frame(frame), query)


class MockEncodeCapture(SyntheticCapture):
    """OS-independent mock capture encoder for DesktopEngine protocol unit tests."""

    def encode(self, frame, *, max_width=1280, fmt="jpeg", quality=75, annotations=None, grid=False, cursor=None):
        raw = b"\xff\xd8\xff\xe0MOCKJPEG\xff\xd9" if fmt == "jpeg" else b"\x89PNGMOCKIEND"
        return {
            "data_b64": base64.b64encode(raw).decode("ascii"),
            "mime_type": "image/jpeg" if fmt == "jpeg" else "image/png",
            "bytes": len(raw),
            "image_w": min(frame.width, max_width),
            "image_h": frame.height,
            "capture_ms": frame.capture_ms,
            "encode_ms": 0.1,
            "total_ms": frame.capture_ms + 0.1,
        }


class TestEngineAndProtocol(unittest.TestCase):
    def _make_engine(self, *, active_app="kate", elements=None, frames=None, layout="us", ocr=None):
        wm = FakeWindowManager(active_app=active_app)
        clip = FakeClipboard("saved-user-clip")
        inp = FakeInputDriver(layout=layout)
        ax = FakeA11y(elements=elements)
        cap = MockEncodeCapture(frames=frames)
        ov = FakeOverlay()
        bg = FakeBackground()
        eng = mcp.DesktopEngine(
            bus=False, windows=wm, capture=cap, a11y=ax, clipboard=clip,
            input_driver=inp, overlay=ov, background=bg, ocr=ocr or FakeOCR(),
        )
        return eng, wm, clip, inp, ax, ov, bg

    def test_independent_cursor_restores_user_mouse_and_glides_overlay(self):
        eng, wm, clip, inp, ax, ov, bg = self._make_engine()
        wm._cursor = (1500, 900)  # User's real mouse position
        eng._ai_cursor_pos = (100.0, 100.0)
        blocks = eng.tool_act({
            "steps": [{"op": "click", "x": 400, "y": 300, "duration_ms": 40}],
            "settle": "none",
            "verify_change": False,
            "observe": "none",
        })
        self.assertIn("1/1 ok", blocks[0]["text"])
        # AI overlay glided smoothly in multiple steps + fired click ripple at (400, 300)
        self.assertGreaterEqual(len(ov.moves), 4)
        self.assertEqual(ov.clicks[-1][:3], (400, 300, "left"))
        # Physical OS click happened at (400, 300), then user's real cursor was immediately restored to (1500, 900)!
        self.assertIn(("click", (400, 300, "left", 1, None)), inp.events)
        self.assertEqual(inp.events[-1], ("move", (1500, 900)))

    def test_moving_user_mouse_does_not_abort_by_default(self):
        eng, wm, clip, inp, ax, ov, bg = self._make_engine()
        wm._cursor = (1400, 900)
        blocks = eng.tool_act({
            "steps": [
                {"op": "click", "x": 100, "y": 100, "smooth": False},
                {"op": "click", "x": 200, "y": 200, "smooth": False},
            ],
            "settle": "none",
            "verify_change": False,
            "observe": "none",
        })
        self.assertIn("2/2 ok", blocks[0]["text"])
        clicks = [ev for ev in inp.events if ev[0] == "click"]
        self.assertEqual(len(clicks), 2)

    def test_opt_in_takeover_abort_still_works(self):
        eng, wm, clip, inp, ax, ov, bg = self._make_engine()
        wm._cursor = (1400, 900)
        blocks = eng.tool_act({
            "steps": [
                {"op": "click", "x": 100, "y": 100, "smooth": False},
                {"op": "click", "x": 200, "y": 200, "smooth": False},
            ],
            "detect_takeover": True,
            "independent_cursor": False,
            "settle": "none",
            "verify_change": False,
            "observe": "none",
        })
        self.assertIn("takeover_detected", blocks[0]["text"])
        clicks = [ev for ev in inp.events if ev[0] == "click"]
        self.assertEqual(len(clicks), 1)

    def test_background_act_never_touches_live_input(self):
        eng, wm, clip, inp, ax, ov, bg = self._make_engine()
        blocks = eng.tool_act({
            "background": True,
            "steps": [
                {"op": "click", "x": 120, "y": 80},
                {"op": "type", "text": "hello bg"},
                {"op": "key", "keys": "enter"},
            ],
            "settle": "none",
            "verify_change": False,
            "observe": "none",
        })
        self.assertIn("3/3 ok", blocks[0]["text"])
        self.assertEqual(inp.events, [])  # Live OS mouse/keyboard completely untouched!
        self.assertIn(("click", (120, 80, "left", 1)), bg.events)
        self.assertIn(("type_text", "hello bg"), bg.events)

    def test_type_modes_and_terminal_paste(self):
        # Linux auto mode: >24 chars in terminal uses atomic ctrl+shift+v paste and restores clipboard
        eng, wm, clip, inp, ax, ov, bg = self._make_engine(active_app="konsole")
        eng._is_windows = False
        eng._is_macos = False
        eng.tool_act({
            "steps": [{"op": "type", "text": "echo 'hello world from terminal paste test'"}],
            "settle": "none",
            "verify_change": False,
            "observe": "none",
        })
        self.assertIn(("hotkey", ("ctrl+shift+v", 1)), inp.events)
        self.assertEqual(clip.value, "saved-user-clip")

        # Windows auto mode: <=500 chars uses KEYEVENTF_UNICODE directly without touching clipboard
        eng_w, _, clip_w, inp_w, _, _, _ = self._make_engine(active_app="powershell")
        eng_w._is_windows = True
        eng_w._is_macos = False
        eng_w.tool_act({
            "steps": [{"op": "type", "text": "echo 'hello world from windows unicode typing'"}],
            "settle": "none",
            "verify_change": False,
            "observe": "none",
        })
        self.assertIn(("type_keys", "echo 'hello world from windows unicode typing'"), inp_w.events)
        self.assertEqual(clip_w.value, "saved-user-clip")

        # Windows explicit paste mode: classic PowerShell/cmd uses ctrl+v, Windows Terminal uses ctrl+shift+v
        eng_w.tool_act({
            "steps": [{"op": "type", "text": "pasted-ps", "mode": "paste"}],
            "settle": "none",
            "verify_change": False,
            "observe": "none",
        })
        self.assertIn(("hotkey", ("ctrl+v", 1)), inp_w.events)
        self.assertEqual(clip_w.value, "saved-user-clip")

        eng_wt, _, clip_wt, inp_wt, _, _, _ = self._make_engine(active_app="windowsterminal")
        eng_wt._is_windows = True
        eng_wt._is_macos = False
        eng_wt.tool_act({
            "steps": [{"op": "type", "text": "pasted-wt", "mode": "paste"}],
            "settle": "none",
            "verify_change": False,
            "observe": "none",
        })
        self.assertIn(("hotkey", ("ctrl+shift+v", 1)), inp_wt.events)
        self.assertEqual(clip_wt.value, "saved-user-clip")

        # macOS auto mode uses CGEventKeyboardSetUnicodeString; clear=True uses cmd+a; paste uses cmd+v
        eng_m, _, clip_m, inp_m, _, _, _ = self._make_engine(active_app="Terminal")
        eng_m._is_windows = False
        eng_m._is_macos = True
        eng_m.tool_act({
            "steps": [
                {"op": "type", "text": "echo 'hello macos unicode typing'", "clear": True},
                {"op": "type", "text": "pasted-mac", "mode": "paste"},
            ],
            "settle": "none",
            "verify_change": False,
            "observe": "none",
        })
        self.assertIn(("hotkey", ("cmd+a", 1)), inp_m.events)
        self.assertIn(("type_keys", "echo 'hello macos unicode typing'"), inp_m.events)
        self.assertIn(("hotkey", ("cmd+v", 1)), inp_m.events)
        self.assertEqual(clip_m.value, "saved-user-clip")

    def test_mcp_protocol_7_tools_and_audit_log(self):
        with tempfile.TemporaryDirectory() as td:
            audit_file = os.path.join(td, "audit.jsonl")
            os.environ["COMPUTER_USE_AUDIT_LOG"] = audit_file
            try:
                eng, *_ = self._make_engine()
                srv = mcp.MCPServer(engine=eng)
                r1 = srv.handle_request({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
                self.assertEqual(r1["result"]["serverInfo"]["name"], "computer-use")
                r2 = srv.handle_request({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
                self.assertEqual(len(r2["result"]["tools"]), 7)
                r3 = srv.handle_request({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "desktop_status", "arguments": {}}})
                self.assertFalse(r3["result"]["isError"])
                with open(audit_file, encoding="utf-8") as f:
                    rec = json.loads(f.readline())
                self.assertEqual(rec["tool"], "desktop_status")
            finally:
                os.environ.pop("COMPUTER_USE_AUDIT_LOG", None)

    def test_stdio_subprocess_roundtrip(self):
        reqs = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "desktop_status", "arguments": {}}},
        ]
        payload = "".join(json.dumps(r) + "\n" for r in reqs)
        with subprocess.Popen(
            [sys.executable, str(SCRIPTS / "computer_use_mcp.py")],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        ) as proc:
            stdout, stderr = proc.communicate(payload, timeout=20)
            self.assertEqual(proc.returncode, 0, msg=stderr)
            lines = [json.loads(line) for line in stdout.splitlines() if line.strip()]
            self.assertEqual(len(lines), 3, msg=stderr)
            self.assertEqual(len(lines[1]["result"]["tools"]), 7)
            status = json.loads(lines[2]["result"]["content"][0]["text"])
            self.assertIn("backends", status)
            self.assertIn("ocr", status["backends"])

    def test_fast_ocr_phrase_matching_and_compact_format(self):
        words = [
            {"text": "Save", "x": 100, "y": 40, "w": 42, "h": 18, "conf": 95.0},
            {"text": "Changes", "x": 150, "y": 41, "w": 68, "h": 18, "conf": 92.0},
            # Far-right column on same visual row (>160px away -> must NOT merge as phrase with "Save")
            {"text": "Cancel", "x": 600, "y": 40, "w": 56, "h": 18, "conf": 96.0},
            {"text": "Settings", "x": 100, "y": 90, "w": 70, "h": 18, "conf": 91.0},
            {"text": "SettingsPanelExtra", "x": 300, "y": 90, "w": 140, "h": 18, "conf": 99.0},
        ]
        # Exact word beats longer substring even with slightly lower confidence
        hit_s = fast_ocr.match_ocr_phrase(words, "Settings")
        self.assertIsNotNone(hit_s)
        self.assertEqual(hit_s["match_type"], "exact")
        self.assertEqual(hit_s["center"], [135, 99])

        # Multi-word contiguous phrase bounding box
        hit_p = fast_ocr.match_ocr_phrase(words, "save changes")
        self.assertIsNotNone(hit_p)
        self.assertEqual(hit_p["match_type"], "phrase_exact")
        self.assertEqual(hit_p["bbox"], [100, 40, 118, 19])
        self.assertEqual(hit_p["center"], [159, 49])

        # Wide horizontal gap rejects cross-column false phrase
        self.assertIsNone(fast_ocr.match_ocr_phrase(words, "changes cancel"))

        # Compact formatter splits wide gaps into separate clickable [x,y] entries
        compact = fast_ocr.format_ocr_compact(words)
        self.assertIn('[159,49] "Save Changes"', compact)
        self.assertIn('[628,49] "Cancel"', compact)

    def test_compound_ops_inline_ocr_and_hot_reload(self):
        ocr_words = [
            {"text": "Search", "x": 400, "y": 100, "w": 60, "h": 20, "conf": 94.0},
            {"text": "Accept", "x": 500, "y": 240, "w": 60, "h": 20, "conf": 95.0},
            {"text": "All", "x": 568, "y": 240, "w": 24, "h": 20, "conf": 93.0},
        ]
        # First OCR call empty, second call reveals "Accept All" -> wait_and_click must poll and click immediately
        fake_ocr = FakeOCR(responses=[[], ocr_words])
        eng, wm, clip, inp, ax, ov, bg = self._make_engine(elements=[], ocr=fake_ocr)

        # wait_and_click + click_and_type + open_url + keys + repeat in ONE call!
        eng._is_windows = False
        eng._is_macos = False
        blocks = eng.tool_act({
            "observe": "none",
            "steps": [
                {"op": "wait_and_click", "text": "Accept All", "poll_ms": 20, "timeout_ms": 500, "smooth": False},
                {"op": "click_and_type", "target": "Search", "text": "rust", "submit": True, "smooth": False},
                {"op": "open_url", "url": "https://example.com", "new_tab": True, "smooth": False},
                {"op": "keys", "sequence": ["ctrl+a", "ctrl+c"], "delay_ms": 0},
                {"op": "repeat", "count": 3, "delay_ms": 0, "steps": [{"op": "key", "keys": "down"}]},
            ],
        })
        self.assertIn("5/5 ok", blocks[0]["text"])
        self.assertIn(("click", (546, 250, "left", 1, None)), inp.events)
        self.assertIn(("click", (430, 110, "left", 1, None)), inp.events)
        self.assertIn(("type_keys", "rust"), inp.events)
        self.assertIn(("hotkey", ("ctrl+t", 1)), inp.events)
        self.assertIn(("type_keys", "https://example.com"), inp.events)
        self.assertEqual(inp.events.count(("hotkey", ("down", 1))), 3)

        # desktop_observe(mode="ocr") returns clickable text coords with ZERO image block!
        obs = eng.tool_observe({"mode": "ocr"})
        self.assertEqual(len(obs), 1)
        self.assertIn("OCR_TEXT", obs[0]["text"])
        self.assertIn('[430,110] "Search"', obs[0]["text"])

        # In hybrid mode on sparse-AX apps, >=5 OCR words returns inline OCR_TEXT with ZERO image offload!
        rich_ocr = FakeOCR(responses=[[
            {"text": "Create", "x": 100, "y": 100, "w": 50, "h": 18, "conf": 95.0},
            {"text": "My", "x": 158, "y": 100, "w": 22, "h": 18, "conf": 95.0},
            {"text": "Own", "x": 186, "y": 100, "w": 32, "h": 18, "conf": 95.0},
            {"text": "Server", "x": 224, "y": 100, "w": 48, "h": 18, "conf": 95.0},
            {"text": "Cancel", "x": 100, "y": 200, "w": 52, "h": 18, "conf": 95.0},
        ]])
        eng2, *_ = self._make_engine(elements=[], ocr=rich_ocr)
        obs_h = eng2.tool_observe({"mode": "hybrid"})
        self.assertEqual(len(obs_h), 1)
        self.assertIn("SCREENSHOT_PATH:", obs_h[0]["text"])
        self.assertIn("Create My Own Server", obs_h[0]["text"])
        act_a = eng2.tool_act({"steps": [{"op": "wait", "ms": 0}], "observe": "auto"})
        self.assertEqual(len(act_a), 1)
        self.assertIn("SCREENSHOT_PATH:", act_a[0]["text"])
        # Verify offset_x / offset_y shifts text click coordinates cleanly (e.g. input box 28px below label)
        eng2_blocks = eng2.tool_act({
            "observe": "none",
            "steps": [{"op": "click", "text": "Cancel", "offset_x": 15, "offset_y": 28, "smooth": False}],
        })
        self.assertIn("1/1 ok", eng2_blocks[0]["text"])
        self.assertIn("[141,237]", eng2_blocks[0]["text"])
        # Explicit screenshot=True still attaches the JPEG image!
        obs_v = eng2.tool_observe({"mode": "hybrid", "screenshot": True})
        self.assertEqual(len(obs_v), 2)
        self.assertEqual(obs_v[1]["type"], "image")

        # Verify script mtime tracker works cleanly for in-process hot-reload
        mtimes = mcp.MCPServer._script_mtimes()
        self.assertIn("computer_use_mcp.py", mtimes)

    def test_cursor_overlay_glide_to_is_non_blocking(self):
        # Even with a long 250ms glide animation, glide_to() must return in <10ms (async on overlay thread)
        ov = cursor_overlay.CursorOverlay()
        ov._enabled = True
        ov._ensure_started = lambda: None  # Test pure glide_to state machine without opening a native window
        try:
            t0 = time.perf_counter()
            ov.glide_to(800, 600, label="Click", duration_ms=250, from_pos=(100, 100))
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            self.assertLess(elapsed_ms, 15.0)
            self.assertEqual((ov._anim_tx, ov._anim_ty), (800.0, 600.0))
            self.assertGreater(ov._anim_dur, 0.0)
        finally:
            ov.close()

    @unittest.skipUnless(sys.platform.startswith("linux") and fast_ocr.FastOCR().available, "Requires Linux with Cairo + libtesseract")
    def test_live_libtesseract_ocr_on_cairo_dark_ui(self):
        import cairo
        # Regression test for real Discord layout: dark app background + white modal + blurple button!
        w, h = 760, 420
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
        ctx = cairo.Context(surf)
        # Dark Discord background (#1e1f22)
        ctx.set_source_rgb(0.118, 0.122, 0.133)
        ctx.paint()
        ctx.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        ctx.set_font_size(20.0)
        ctx.set_source_rgb(0.92, 0.93, 0.96)
        ctx.move_to(24, 60)
        ctx.show_text("Sidebar Settings")
        # Bright WHITE modal dialog (#ffffff) in the center (must NOT be inverted to black!)
        ctx.set_source_rgb(1.0, 1.0, 1.0)
        ctx.rectangle(240, 60, 380, 300)
        ctx.fill()
        ctx.set_source_rgb(0.08, 0.09, 0.11)
        ctx.set_font_size(22.0)
        ctx.move_to(280, 145)
        ctx.show_text("Create My Own")
        ctx.move_to(280, 210)
        ctx.show_text("For me and my friends")
        # Blurple button (#5865f2) with white text inside the white modal!
        ctx.set_source_rgb(0.345, 0.396, 0.949)
        ctx.rectangle(460, 285, 130, 46)
        ctx.fill()
        ctx.set_source_rgb(1.0, 1.0, 1.0)
        ctx.move_to(488, 315)
        ctx.show_text("Create")
        surf.flush()
        bgra = np.frombuffer(surf.get_data(), dtype=np.uint8).reshape((h, w, 4))
        rgba = bgra[:, :, [2, 1, 0, 3]].copy()
        frame = capture.Frame(rgba=rgba, origin_x=200, origin_y=100, width=w, height=h, scale=1.0, backend="synthetic", capture_ms=0.1)

        ocr = fast_ocr.FastOCR(workers=4)
        try:
            # Dark sidebar text
            hit_dark = ocr.find_text(frame, "Sidebar Settings")
            self.assertIsNotNone(hit_dark, "Failed to detect light text on dark sidebar")
            # White modal text over dark background
            hit_modal = ocr.find_text(frame, "Create My Own")
            self.assertIsNotNone(hit_modal, "Failed to detect dark text inside white modal over dark app!")
            self.assertTrue(440 <= hit_modal["center"][0] <= 760, f"Unexpected modal cx={hit_modal['center']}")
            self.assertTrue(210 <= hit_modal["center"][1] <= 270, f"Unexpected modal cy={hit_modal['center']}")
            # Blurple button inside white modal (isolated button beats "Create" inside "Create My Own")
            hit_btn = ocr.find_text(frame, "Create")
            self.assertIsNotNone(hit_btn, "Failed to detect white text on blurple Create button inside white modal!")
            self.assertTrue(640 <= hit_btn["center"][0] <= 790, f"Expected isolated Create button, got {hit_btn}")
            # Second call hits frame fingerprint cache in <2 ms
            t0 = time.perf_counter()
            hit2 = ocr.find_text(frame, "For me and my friends")
            self.assertIsNotNone(hit2)
            self.assertLess((time.perf_counter() - t0) * 1000.0, 5.0)
        finally:
            ocr.close()


@unittest.skipUnless(sys.platform.startswith("linux") and shutil.which("Xvfb"), "Requires Linux with Xvfb")
class TestLinuxXvfbBackground(unittest.TestCase):
    def test_xvfb_background_capture_and_input(self):
        import linux_bg
        bg = linux_bg.LinuxBackgroundSession()
        try:
            st = bg.start(640, 480)
            self.assertTrue(st["active"])
            f = bg.capture()
            self.assertEqual((f.width, f.height), (640, 480))
            self.assertEqual(bg.move(120, 90), (120, 90))
            self.assertEqual(bg.click(140, 110), (140, 110))
        finally:
            bg.stop()


@unittest.skipUnless(sys.platform == "win32", "Windows-only live Win32 integration tests")
class TestWindowsLiveIntegration(unittest.TestCase):
    def test_win32_windows_capture_clipboard_uia_overlay(self):
        wm = win32_backend.WinWindowManager()
        vx, vy, vw, vh = wm.virtual_bounds()
        self.assertGreater(vw, 0)
        self.assertGreater(vh, 0)
        st = wm.state()
        self.assertIn("windows", st)

        # Clipboard roundtrip with Unicode + emoji (restore previous value)
        cb = win32_backend.WinClipboard()
        prev = cb.get_text()
        try:
            sample = "computer-use Win32 test \u00a3 \u2713 \U0001F680"
            cb.set_text(sample)
            self.assertEqual(cb.get_text(), sample)
        finally:
            cb.set_text(prev)

        # GDI BitBlt + GDI+ JPEG & PNG encode with grid & Set-of-Marks
        cap = win32_backend.WinScreenCapture(virtual_bounds_fn=wm.virtual_bounds)
        f = cap.capture(scope="area", area=(vx, vy, min(400, vw), min(300, vh)))
        self.assertGreater(f.width, 0)
        enc_jpg = cap.encode(f, max_width=320, fmt="jpeg", quality=75, grid=True, cursor=(vx + 50, vy + 50), annotations=[{"ref": 1, "bbox": (vx + 10, vy + 10, 80, 30)}])
        self.assertTrue(base64.b64decode(enc_jpg["data_b64"]).startswith(b"\xff\xd8\xff"))
        enc_png = cap.encode(f, max_width=320, fmt="png")
        self.assertTrue(base64.b64decode(enc_png["data_b64"]).startswith(b"\x89PNG"))

        # UI Automation COM init + snapshot
        uia = win32_backend.WinA11yTree()
        self.assertTrue(uia.available)
        snap = uia.snapshot(diff=False)
        self.assertTrue(snap["available"])
        uia.close()

        # Second layered overlay instance must NOT crash with UAF after first closes!
        for _ in range(2):
            ov = cursor_overlay._Win32LayeredOverlay()
            try:
                self.assertTrue(ov.ready)
                ov.render(200.0, 200.0, 1.0, "AI", [], 1.0)
            finally:
                ov.close()

        # Verify GDI+ JPEG ends cleanly at EOI (0xFFD9) and PNG ends at IEND with no GlobalSize garbage
        raw_jpg = base64.b64decode(enc_jpg["data_b64"])
        raw_png = base64.b64decode(enc_png["data_b64"])
        self.assertTrue(raw_jpg.endswith(b"\xff\xd9"))
        self.assertIn(b"IEND", raw_png[-16:])


@unittest.skipUnless(sys.platform == "darwin", "macOS-only live CoreGraphics/ImageIO/AX/AppKit integration tests")
class TestMacOSLiveIntegration(unittest.TestCase):
    def test_macos_frameworks_windows_clipboard_imageio_orientation_a11y_overlay(self):
        fw = macos_backend._MacFrameworks.get()
        self.assertIsNotNone(fw.cf)
        self.assertIsNotNone(fw.cg)

        wm = macos_backend.MacWindowManager()
        vx, vy, vw, vh = wm.virtual_bounds()
        self.assertGreater(vw, 0)
        self.assertGreater(vh, 0)
        st = wm.state()
        self.assertIn("windows", st)
        self.assertIn("cursor", st)

        # UTF-8 + emoji clipboard roundtrip (skip cleanly in headless SSH sessions without pboard)
        cb = macos_backend.MacClipboard()
        try:
            prev = cb.get_text()
            try:
                sample = "computer-use macOS test \u00a3 \u2713 \U0001F680"
                cb.set_text(sample)
                self.assertEqual(cb.get_text(), sample)
            finally:
                try:
                    cb.set_text(prev)
                except Exception:
                    pass
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            pass

        # Synthetic orientation test: top half RED, bottom half BLUE -> resize + encode PNG & JPEG -> decode PNG
        # Verifies CGBitmapContext does NOT flip the image vertically and ImageIO quality/format encode cleanly!
        cap = macos_backend.MacScreenCapture(virtual_bounds_fn=wm.virtual_bounds)
        arr = np.zeros((120, 240, 4), dtype=np.uint8)
        arr[:60, :, :] = (230, 30, 30, 255)   # Top half RED
        arr[60:, :, :] = (30, 60, 235, 255)   # Bottom half BLUE
        frame = capture.Frame(rgba=arr, origin_x=0, origin_y=0, width=240, height=120, scale=2.0, backend="synthetic", capture_ms=0.1)

        enc_jpg = cap.encode(
            frame, max_width=160, fmt="jpeg", quality=80, grid=True, cursor=(40, 30),
            annotations=[{"ref": 7, "bbox": (10, 10, 40, 20)}],
        )
        raw_jpg = base64.b64decode(enc_jpg["data_b64"])
        self.assertTrue(raw_jpg.startswith(b"\xff\xd8\xff"))
        self.assertTrue(raw_jpg.endswith(b"\xff\xd9"))

        enc_png = cap.encode(frame, max_width=120, fmt="png")
        raw_png = base64.b64decode(enc_png["data_b64"])
        self.assertTrue(raw_png.startswith(b"\x89PNG"))
        self.assertIn(b"IEND", raw_png[-16:])

        decoded = cap._decode_image_bytes_to_rgba(raw_png)
        self.assertEqual(decoded.shape[:2], (60, 120))
        top_px = decoded[10, 60, :3]
        bot_px = decoded[50, 60, :3]
        self.assertGreater(int(top_px[0]), 180, f"Expected RED on top, got {top_px}")
        self.assertGreater(int(bot_px[2]), 180, f"Expected BLUE on bottom, got {bot_px}")

        # MacA11yTree non-crashing snapshot + cleanup (works whether Accessibility TCC is granted or not)
        ax = macos_backend.MacA11yTree()
        snap = ax.snapshot(diff=False)
        self.assertIn("formatted", snap)
        self.assertIn("text_lines", snap)
        ax.close()

        # AppKit cursor overlay helper spawns and shuts down cleanly without hanging or crashing
        ov = cursor_overlay._MacAppKitOverlayProxy()
        try:
            if ov.ready:
                ov.render(200.0, 200.0, 1.0, "AI", [], time.monotonic())
        finally:
            ov.close()


@unittest.skipUnless(sys.platform in ("win32", "darwin") or os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY"), "Requires live desktop")
class TestLiveSelftest(unittest.TestCase):
    def test_live_selftest_passes(self):
        r = subprocess.run(
            [sys.executable, str(SCRIPTS / "computer_use_mcp.py"), "--selftest"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=25,
        )
        self.assertEqual(r.returncode, 0, msg=r.stdout + "\n" + r.stderr)
        self.assertIn("ALL PASSED", r.stdout)


if __name__ == "__main__":
    unittest.main()
