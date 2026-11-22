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

    @unittest.skipIf(sys.platform == "win32", "Cairo/GdkPixbuf test runs on Linux")
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


class TestEngineAndProtocol(unittest.TestCase):
    def _make_engine(self, *, active_app="kate", elements=None, frames=None, layout="us"):
        wm = FakeWindowManager(active_app=active_app)
        clip = FakeClipboard("saved-user-clip")
        inp = FakeInputDriver(layout=layout)
        ax = FakeA11y(elements=elements)
        cap = SyntheticCapture(frames=frames)
        ov = FakeOverlay()
        bg = FakeBackground()
        eng = mcp.DesktopEngine(
            bus=False, windows=wm, capture=cap, a11y=ax, clipboard=clip,
            input_driver=inp, overlay=ov, background=bg,
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
        eng, wm, clip, inp, ax, ov, bg = self._make_engine(active_app="konsole")
        eng.tool_act({
            "steps": [{"op": "type", "text": "echo 'hello world from terminal paste test'"}],
            "settle": "none",
            "verify_change": False,
            "observe": "none",
        })
        self.assertIn(("hotkey", ("ctrl+shift+v", 1)), inp.events)
        self.assertEqual(clip.value, "saved-user-clip")

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
            stdout, _ = proc.communicate(payload, timeout=10)
            lines = [json.loads(line) for line in stdout.splitlines() if line.strip()]
            self.assertEqual(len(lines), 3)
            self.assertEqual(len(lines[1]["result"]["tools"]), 7)
            status = json.loads(lines[2]["result"]["content"][0]["text"])
            self.assertIn("backends", status)


@unittest.skipUnless(sys.platform != "win32" and shutil.which("Xvfb"), "Requires Linux with Xvfb")
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


@unittest.skipUnless(sys.platform == "win32" or os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY"), "Requires live desktop")
class TestLiveSelftest(unittest.TestCase):
    def test_live_selftest_passes(self):
        r = subprocess.run(
            [sys.executable, str(SCRIPTS / "computer_use_mcp.py"), "--selftest"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
        self.assertEqual(r.returncode, 0, msg=r.stdout + "\n" + r.stderr)
        self.assertIn("ALL PASSED", r.stdout)


if __name__ == "__main__":
    unittest.main()
