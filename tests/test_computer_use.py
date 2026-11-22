"""Unit, mocked engine, MCP JSON-RPC stdio, and live desktop tests for computer-use."""

from __future__ import annotations

import base64
import json
import os
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
import uinput_driver


class FakeWindowManager:
    def __init__(self, screen=(1920, 1080), cursor=(500, 400), active_app="kate"):
        self.backend = "mock"
        self._screen = screen
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

    def screen_size(self) -> tuple[int, int]:
        return self._screen

    def state(self) -> dict[str, Any]:
        return {
            "screen": {"w": self._screen[0], "h": self._screen[1]},
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
    def __init__(self):
        self.backend = "mock"
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

    def drag(self, x1, y1, x2, y2, *, button="left", duration_ms=140, steps=14):
        self._last_pos = (x2, y2)
        self.events.append(("drag", (x1, y1, x2, y2, button)))
        return (x1, y1), (x2, y2)

    def scroll(self, direction="down", clicks=5, *, x=None, y=None):
        self.events.append(("scroll", (direction, clicks, x, y)))

    def hotkey(self, keys, *, count=1):
        self.events.append(("hotkey", (keys, count)))

    def type_keys(self, text, *, key_delay_ms=0):
        self.events.append(("type_keys", text))

    def is_pure_ascii_typable(self, text: str) -> bool:
        return all(uinput_driver.char_to_uinput(ch) is not None for ch in text)


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


class TestInputMapping(unittest.TestCase):
    def test_parse_key_combo(self):
        combo = uinput_driver.parse_key_combo("ctrl+shift+t")
        self.assertEqual([c for _, c in combo], [29, 42, 20])
        plus = uinput_driver.parse_key_combo("+")
        self.assertEqual([c for _, c in plus], [42, 13])
        with self.assertRaises(ValueError):
            uinput_driver.parse_key_combo("ctrl+notakey")

    def test_char_to_uinput(self):
        self.assertEqual(uinput_driver.char_to_uinput("a"), (False, 30))
        self.assertEqual(uinput_driver.char_to_uinput("A"), (True, 30))
        self.assertEqual(uinput_driver.char_to_uinput("!"), (True, 2))
        self.assertEqual(uinput_driver.char_to_uinput("\n"), (False, 28))
        self.assertIsNone(uinput_driver.char_to_uinput("\u00e9"))

    def test_screen_to_abs_clamping(self):
        drv = uinput_driver.InputDriver(screen_size_fn=lambda: (1920, 1080))
        self.assertEqual(drv._screen_to_abs(0, 0), (0, 0, 0, 0))
        self.assertEqual(drv._screen_to_abs(1919, 1079), (1919, 1079, 65535, 65535))
        self.assertEqual(drv._screen_to_abs(-50, 5000), (0, 1079, 0, 65535))

    def test_normalized_to_screen(self):
        self.assertEqual(mcp.normalized_to_screen(0, 0, (0, 0, 1920, 1080)), (0, 0))
        self.assertEqual(mcp.normalized_to_screen(1000, 1000, (0, 0, 1920, 1080)), (1919, 1079))
        self.assertEqual(mcp.normalized_to_screen(500, 500, (100, 200, 401, 201)), (300, 300))
        self.assertEqual(mcp.normalized_to_screen(1500, -100, (0, 0, 1000, 1000)), (999, 0))


class TestCaptureAndCairo(unittest.TestCase):
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
        self.assertAlmostEqual(bh, 60, delta=6)

    def test_encode_jpeg_png_with_cairo_overlays(self):
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


class TestEngineAndProtocol(unittest.TestCase):
    def _make_engine(self, *, active_app="kate", elements=None, frames=None):
        wm = FakeWindowManager(active_app=active_app)
        clip = FakeClipboard("saved-user-clip")
        inp = FakeInputDriver()
        ax = FakeA11y(elements=elements)
        cap = SyntheticCapture(frames=frames)
        eng = mcp.DesktopEngine(bus=False, windows=wm, capture=cap, a11y=ax, clipboard=clip, input_driver=inp)
        return eng, wm, clip, inp, ax

    def test_resolve_points_and_type_modes(self):
        elems = [{"ref": 3, "role": "btn", "name": "Save", "value": "", "states": [], "bbox": (200, 100, 80, 30), "center": [240, 115]}]
        eng, wm, clip, inp, ax = self._make_engine(elements=elems)
        eng.last_image_rect = (100, 200, 401, 201)

        # ref, text, exact, normalized screen, normalized last_image
        self.assertEqual(eng.resolve_point({"ref": 3}), (240, 115))
        self.assertEqual(eng.resolve_point({"op": "click", "text": "Save"}), (240, 115))
        self.assertEqual(eng.resolve_point({"x": 42, "y": 84}), (42, 84))
        self.assertEqual(eng.resolve_point({"nx": 500, "ny": 500, "coord_scope": "last_image"}), (300, 300))

        # Short ASCII -> uinput keys (clipboard untouched)
        eng.tool_act({"steps": [{"op": "type", "text": "hello"}], "settle": "none", "verify_change": False, "observe": "none"})
        self.assertIn(("type_keys", "hello"), inp.events)
        self.assertEqual(clip.history, [])

        # Long or Unicode text -> atomic paste + restore
        long_txt = "This string exceeds twenty-four characters \u2713"
        eng.tool_act({"steps": [{"op": "type", "text": long_txt}], "settle": "none", "verify_change": False, "observe": "none"})
        self.assertEqual(clip.value, "saved-user-clip")
        self.assertEqual(clip.history, [long_txt, "saved-user-clip"])
        self.assertIn(("hotkey", ("ctrl+v", 1)), inp.events)

    def test_terminal_uses_ctrl_shift_v(self):
        eng, wm, clip, inp, ax = self._make_engine(active_app="konsole")
        eng.tool_act({
            "steps": [{"op": "type", "text": "echo 'hello world from terminal paste test'"}],
            "settle": "none",
            "verify_change": False,
            "observe": "none",
        })
        self.assertIn(("hotkey", ("ctrl+shift+v", 1)), inp.events)
        self.assertEqual(clip.value, "saved-user-clip")

    def test_takeover_detection_aborts_batch(self):
        eng, wm, clip, inp, ax = self._make_engine()
        wm._cursor = (1400, 900)  # Far from step 0 click at (100, 100)
        blocks = eng.tool_act({
            "steps": [
                {"op": "click", "x": 100, "y": 100},
                {"op": "click", "x": 200, "y": 200},
            ],
            "settle": "none",
            "verify_change": False,
            "observe": "none",
        })
        txt = blocks[0]["text"]
        self.assertIn("takeover_detected", txt)
        # Second click must NOT have executed
        clicks = [ev for ev in inp.events if ev[0] == "click"]
        self.assertEqual(len(clicks), 1)

    def test_hybrid_observe_auto_screenshot_threshold(self):
        # Sparse AX (<5 refs) -> auto attaches screenshot
        eng_sparse, *_ = self._make_engine(elements=[])
        b1 = eng_sparse.tool_observe({"mode": "hybrid"})
        self.assertEqual(len(b1), 2)
        self.assertEqual(b1[1]["type"], "image")

        # Rich AX (>=5 refs) -> text-only without redundant screenshot
        rich = [
            {"ref": i, "role": "btn", "name": f"B{i}", "value": "", "states": [], "bbox": (i * 20, 10, 15, 15), "center": [i * 20 + 7, 17]}
            for i in range(1, 7)
        ]
        eng_rich, *_ = self._make_engine(elements=rich)
        b2 = eng_rich.tool_observe({"mode": "hybrid"})
        self.assertEqual(len(b2), 1)
        self.assertEqual(b2[0]["type"], "text")

    def test_mcp_protocol_framing_and_audit_log(self):
        with tempfile.TemporaryDirectory() as td:
            audit_file = os.path.join(td, "audit.jsonl")
            os.environ["COMPUTER_USE_AUDIT_LOG"] = audit_file
            try:
                eng, *_ = self._make_engine()
                srv = mcp.MCPServer(engine=eng)
                r1 = srv.handle_request({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
                self.assertEqual(r1["result"]["serverInfo"]["name"], "computer-use")
                r2 = srv.handle_request({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
                self.assertEqual(len(r2["result"]["tools"]), 6)
                r3 = srv.handle_request({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "desktop_status", "arguments": {}}})
                self.assertFalse(r3["result"]["isError"])
                r4 = srv.handle_request({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "no_such_tool", "arguments": {}}})
                self.assertIn("error", r4)
                self.assertIsNone(srv.handle_request({"jsonrpc": "2.0", "method": "notifications/initialized"}))
                with open(audit_file, encoding="utf-8") as f:
                    rec = json.loads(f.readline())
                self.assertEqual(rec["tool"], "desktop_status")
                self.assertTrue(rec["ok"])
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
        ) as proc:
            stdout, _ = proc.communicate(payload, timeout=5)
            lines = [json.loads(line) for line in stdout.splitlines() if line.strip()]
            self.assertEqual(len(lines), 3)
            self.assertEqual(lines[0]["id"], 1)
            self.assertEqual(len(lines[1]["result"]["tools"]), 6)
            status = json.loads(lines[2]["result"]["content"][0]["text"])
            self.assertIn("backends", status)


@unittest.skipUnless(os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY"), "Requires live desktop session")
class TestLiveDesktopNonDestructive(unittest.TestCase):
    def test_live_selftest_passes(self):
        r = subprocess.run(
            [sys.executable, str(SCRIPTS / "computer_use_mcp.py"), "--selftest"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(r.returncode, 0, msg=r.stdout + "\n" + r.stderr)
        self.assertIn("ALL PASSED", r.stdout)


if __name__ == "__main__":
    unittest.main()
