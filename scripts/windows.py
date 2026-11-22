"""Ultra-fast Linux window manager bridge (KDE KWin 6 Scripting DBus ~4 ms, Hyprland, Sway, X11)."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
from typing import Any


class WindowManager:
    """List, focus, move/resize, minimize, maximize, close, and launch desktop windows."""

    def __init__(self, session_bus=None):
        self._bus = session_bus
        self._seq = 0
        self._backend = self._detect()
        self._cached_screen: tuple[int, int] | None = None
        self._cached_screen_at = 0.0

    def _detect(self) -> str:
        try:
            import dbus
            bus = self._bus or dbus.SessionBus()
            bus.get_object("org.kde.KWin", "/Scripting")
            return "kwin-dbus"
        except Exception:
            pass
        if os.environ.get("HYPRLAND_INSTANCE_SIGNATURE") and shutil.which("hyprctl"):
            return "hyprland"
        if os.environ.get("SWAYSOCK") and shutil.which("swaymsg"):
            return "sway"
        if os.environ.get("DISPLAY") and (shutil.which("wmctrl") or shutil.which("xdotool")):
            return "x11"
        return "none"

    @property
    def backend(self) -> str:
        return self._backend

    def screen_size(self) -> tuple[int, int]:
        now = time.monotonic()
        if self._cached_screen and (now - self._cached_screen_at) < 5.0:
            return self._cached_screen
        size = self._query_screen_size()
        self._cached_screen = size
        self._cached_screen_at = now
        return size

    def _query_screen_size(self) -> tuple[int, int]:
        if self._backend == "kwin-dbus":
            try:
                data = self._run_kwin_js(
                    "var g = workspace.virtualScreenSize;"
                    "return {w: Math.round(g.width), h: Math.round(g.height)};"
                )
                if data and data.get("w", 0) > 0 and data.get("h", 0) > 0:
                    return int(data["w"]), int(data["h"])
            except Exception:
                pass
        if shutil.which("kscreen-doctor"):
            r = subprocess.run(["kscreen-doctor", "-o"], capture_output=True, text=True, timeout=2)
            if r.returncode == 0:
                max_x, max_y = 0, 0
                for m in re.finditer(r"Geometry:\s*(\d+),(\d+)\s+(\d+)x(\d+)", r.stdout):
                    x, y, w, h = map(int, m.groups())
                    max_x = max(max_x, x + w)
                    max_y = max(max_y, y + h)
                if max_x > 0 and max_y > 0:
                    return max_x, max_y
        if shutil.which("xrandr") and os.environ.get("DISPLAY"):
            r = subprocess.run(["xrandr", "--current"], capture_output=True, text=True, timeout=2)
            m = re.search(r"current\s+(\d+)\s+x\s+(\d+)", r.stdout)
            if m:
                return int(m.group(1)), int(m.group(2))
        return 1920, 1080

    def _run_kwin_js(self, body_js: str, timeout_ms: int = 1200) -> Any:
        """Execute a short KWin 6 script and receive JSON back over DBus in ~4 ms."""
        import dbus
        import dbus.service
        from gi.repository import GLib

        bus = self._bus or dbus.SessionBus()
        bus_name = bus.get_unique_name()
        self._seq += 1
        tag = f"cu_{os.getpid()}_{self._seq}"
        obj_path = f"/AntigravityCU/{tag}"
        iface_name = "org.antigravity.ComputerUse"
        result_box: list[str] = []
        loop = GLib.MainLoop()

        class _Receiver(dbus.service.Object):
            @dbus.service.method(iface_name, in_signature="s", out_signature="")
            def Report(self, payload: str) -> None:
                result_box.append(str(payload))
                loop.quit()

        receiver = _Receiver(bus, obj_path)
        wrapped = (
            "(function() {\n"
            f"  var __res = (function() {{\n{body_js}\n  }})();\n"
            f'  callDBus("{bus_name}", "{obj_path}", "{iface_name}", "Report", JSON.stringify(__res === undefined ? null : __res));\n'
            "})();\n"
        )
        script_path = ""
        scripting = dbus.Interface(bus.get_object("org.kde.KWin", "/Scripting"), "org.kde.kwin.Scripting")
        try:
            with tempfile.NamedTemporaryFile("w", suffix=".js", prefix="agcu_", delete=False, encoding="utf-8") as f:
                f.write(wrapped)
                script_path = f.name
            sid = int(scripting.loadScript(script_path, tag, signature="ss"))
            script_obj = dbus.Interface(bus.get_object("org.kde.KWin", f"/Scripting/Script{sid}"), "org.kde.kwin.Script")
            script_obj.run()
            timer_id = GLib.timeout_add(timeout_ms, loop.quit)
            loop.run()
            GLib.source_remove(timer_id)
            try:
                script_obj.stop()
            except Exception:
                pass
            try:
                scripting.unloadScript(tag)
            except Exception:
                pass
        finally:
            receiver.remove_from_connection()
            if script_path and os.path.exists(script_path):
                try:
                    os.unlink(script_path)
                except Exception:
                    pass
        if not result_box:
            raise TimeoutError("KWin scripting callback timed out")
        return json.loads(result_box[0])

    def state(self) -> dict[str, Any]:
        """Return {'screen': {w, h}, 'cursor': {x, y}, 'windows': [...], 'active': {...}|None}."""
        if self._backend == "kwin-dbus":
            data = self._run_kwin_js(
                """
                var wins = [];
                var list = workspace.windowList();
                var active = workspace.activeWindow;
                for (var i = 0; i < list.length; i++) {
                    var w = list[i];
                    if (w.skipTaskbar && !w.normalWindow) continue;
                    var g = w.frameGeometry;
                    if (g.width <= 1 || g.height <= 1) continue;
                    wins.push({
                        id: String(w.internalId),
                        title: String(w.caption || ""),
                        app: String(w.resourceClass || ""),
                        pid: w.pid || 0,
                        x: Math.round(g.x), y: Math.round(g.y),
                        w: Math.round(g.width), h: Math.round(g.height),
                        active: !!(active && w.internalId === active.internalId),
                        minimized: !!w.minimized,
                        fullscreen: !!w.fullScreen
                    });
                }
                var c = workspace.cursorPos;
                var vs = workspace.virtualScreenSize;
                return {
                    screen: {w: Math.round(vs.width), h: Math.round(vs.height)},
                    cursor: {x: Math.round(c.x), y: Math.round(c.y)},
                    windows: wins
                };
                """
            )
            self._cached_screen = (int(data["screen"]["w"]), int(data["screen"]["h"]))
            self._cached_screen_at = time.monotonic()
            active = next((w for w in data["windows"] if w.get("active")), None)
            data["active"] = active
            return data

        if self._backend == "hyprland":
            clients = json.loads(subprocess.check_output(["hyprctl", "clients", "-j"], text=True, timeout=2))
            active_w = json.loads(subprocess.check_output(["hyprctl", "activewindow", "-j"], text=True, timeout=2))
            cur_raw = subprocess.check_output(["hyprctl", "cursorpos"], text=True, timeout=2).strip()
            cx, cy = (int(p.strip()) for p in cur_raw.split(",")) if "," in cur_raw else (0, 0)
            active_addr = active_w.get("address")
            wins = [
                {
                    "id": c.get("address", ""),
                    "title": c.get("title", ""),
                    "app": c.get("class", ""),
                    "pid": c.get("pid", 0),
                    "x": c["at"][0], "y": c["at"][1],
                    "w": c["size"][0], "h": c["size"][1],
                    "active": c.get("address") == active_addr,
                    "minimized": bool(c.get("hidden")),
                    "fullscreen": bool(c.get("fullscreen")),
                }
                for c in clients if c.get("mapped", True)
            ]
            sw, sh = self.screen_size()
            return {"screen": {"w": sw, "h": sh}, "cursor": {"x": cx, "y": cy}, "windows": wins,
                    "active": next((w for w in wins if w["active"]), None)}

        if self._backend == "x11":
            return self._x11_state()

        sw, sh = self.screen_size()
        return {"screen": {"w": sw, "h": sh}, "cursor": {"x": 0, "y": 0}, "windows": [], "active": None}

    def _x11_state(self) -> dict[str, Any]:
        sw, sh = self.screen_size()
        cx, cy = 0, 0
        active_id = ""
        if shutil.which("xdotool"):
            r = subprocess.run(["xdotool", "getmouselocation", "--shell"], capture_output=True, text=True, timeout=2)
            for line in r.stdout.splitlines():
                if line.startswith("X="):
                    cx = int(line[2:])
                elif line.startswith("Y="):
                    cy = int(line[2:])
            r2 = subprocess.run(["xdotool", "getactivewindow"], capture_output=True, text=True, timeout=2)
            if r2.returncode == 0 and r2.stdout.strip().isdigit():
                active_id = hex(int(r2.stdout.strip()))
        wins: list[dict[str, Any]] = []
        if shutil.which("wmctrl"):
            r = subprocess.run(["wmctrl", "-lpGx"], capture_output=True, text=True, timeout=2)
            for line in r.stdout.splitlines():
                parts = line.split(None, 8)
                if len(parts) < 9:
                    continue
                wid = hex(int(parts[0], 16))
                wins.append({
                    "id": wid,
                    "title": parts[8],
                    "app": parts[7],
                    "pid": int(parts[2]),
                    "x": int(parts[3]), "y": int(parts[4]),
                    "w": int(parts[5]), "h": int(parts[6]),
                    "active": wid == active_id,
                    "minimized": False,
                    "fullscreen": False,
                })
        return {"screen": {"w": sw, "h": sh}, "cursor": {"x": cx, "y": cy}, "windows": wins,
                "active": next((w for w in wins if w["active"]), None)}

    def find_window(
        self,
        *,
        window_id: str | None = None,
        app: str | None = None,
        title: str | None = None,
    ) -> dict[str, Any] | None:
        st = self.state()
        wins = st["windows"]
        if window_id:
            for w in wins:
                if w["id"] == window_id or window_id in w["id"]:
                    return w
        if app:
            q = app.lower()
            for w in wins:
                if q in w["app"].lower():
                    return w
        if title:
            q = title.lower()
            for w in wins:
                if q in w["title"].lower():
                    return w
        return None

    def control(
        self,
        action: str,
        *,
        window_id: str | None = None,
        app: str | None = None,
        title: str | None = None,
        x: int | None = None,
        y: int | None = None,
        w: int | None = None,
        h: int | None = None,
    ) -> dict[str, Any]:
        target = self.find_window(window_id=window_id, app=app, title=title)
        if target is None:
            raise ValueError(f"Window not found (window_id={window_id!r}, app={app!r}, title={title!r})")
        wid = target["id"]
        act = action.lower()

        if self._backend == "kwin-dbus":
            wid_js = json.dumps(wid)
            if act == "focus":
                js = f"""
                var list = workspace.windowList();
                for (var i = 0; i < list.length; i++) {{
                    var w = list[i];
                    if (String(w.internalId) === {wid_js}) {{
                        w.minimized = false;
                        workspace.activeWindow = w;
                        return {{ok: true, id: String(w.internalId), title: String(w.caption)}};
                    }}
                }}
                return {{ok: false}};
                """
            elif act == "minimize":
                js = f"""
                var list = workspace.windowList();
                for (var i = 0; i < list.length; i++) {{
                    var w = list[i];
                    if (String(w.internalId) === {wid_js}) {{
                        w.minimized = true;
                        return {{ok: true, id: String(w.internalId)}};
                    }}
                }}
                return {{ok: false}};
                """
            elif act in ("maximize", "unmaximize"):
                flag = "true" if act == "maximize" else "false"
                js = f"""
                var list = workspace.windowList();
                for (var i = 0; i < list.length; i++) {{
                    var w = list[i];
                    if (String(w.internalId) === {wid_js}) {{
                        w.minimized = false;
                        w.setMaximize({flag}, {flag});
                        workspace.activeWindow = w;
                        return {{ok: true, id: String(w.internalId)}};
                    }}
                }}
                return {{ok: false}};
                """
            elif act == "close":
                js = f"""
                var list = workspace.windowList();
                for (var i = 0; i < list.length; i++) {{
                    var w = list[i];
                    if (String(w.internalId) === {wid_js}) {{
                        w.closeWindow();
                        return {{ok: true, id: String(w.internalId)}};
                    }}
                }}
                return {{ok: false}};
                """
            elif act == "move_resize":
                nx = target["x"] if x is None else int(x)
                ny = target["y"] if y is None else int(y)
                nw = target["w"] if w is None else int(w)
                nh = target["h"] if h is None else int(h)
                js = f"""
                var list = workspace.windowList();
                for (var i = 0; i < list.length; i++) {{
                    var w = list[i];
                    if (String(w.internalId) === {wid_js}) {{
                        w.minimized = false;
                        w.setMaximize(false, false);
                        w.frameGeometry = {{x: {nx}, y: {ny}, width: {nw}, height: {nh}}};
                        workspace.activeWindow = w;
                        return {{ok: true, id: String(w.internalId), x: {nx}, y: {ny}, w: {nw}, h: {nh}}};
                    }}
                }}
                return {{ok: false}};
                """
            else:
                raise ValueError(f"Unsupported window action: {action!r}")
            res = self._run_kwin_js(js)
            if not res or not res.get("ok"):
                raise RuntimeError(f"KWin action {action!r} failed on window {wid}")
            return {**target, "action": act}

        if self._backend == "hyprland":
            if act == "focus":
                subprocess.run(["hyprctl", "dispatch", "focuswindow", f"address:{wid}"], check=True, capture_output=True)
            elif act == "close":
                subprocess.run(["hyprctl", "dispatch", "closewindow", f"address:{wid}"], check=True, capture_output=True)
            else:
                raise ValueError(f"Action {act!r} not implemented for Hyprland yet")
            return {**target, "action": act}

        if self._backend == "x11" and shutil.which("wmctrl"):
            if act == "focus":
                subprocess.run(["wmctrl", "-i", "-a", wid], check=True, capture_output=True)
            elif act == "close":
                subprocess.run(["wmctrl", "-i", "-c", wid], check=True, capture_output=True)
            elif act == "maximize":
                subprocess.run(["wmctrl", "-i", "-r", wid, "-b", "add,maximized_vert,maximized_horz"], check=True, capture_output=True)
            elif act == "unmaximize":
                subprocess.run(["wmctrl", "-i", "-r", wid, "-b", "remove,maximized_vert,maximized_horz"], check=True, capture_output=True)
            elif act == "move_resize":
                nx = target["x"] if x is None else int(x)
                ny = target["y"] if y is None else int(y)
                nw = target["w"] if w is None else int(w)
                nh = target["h"] if h is None else int(h)
                subprocess.run(["wmctrl", "-i", "-r", wid, "-e", f"0,{nx},{ny},{nw},{nh}"], check=True, capture_output=True)
            return {**target, "action": act}

        raise RuntimeError(f"Window control unavailable on backend {self._backend!r}")

    def launch(
        self,
        command: str | list[str],
        *,
        wait_app: str | None = None,
        timeout_ms: int = 5000,
    ) -> dict[str, Any]:
        argv = shlex.split(command) if isinstance(command, str) else [str(a) for a in command]
        if not argv:
            raise ValueError("Empty launch command")
        before_ids = {w["id"] for w in self.state()["windows"]}
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        appeared = None
        if wait_app is not None or timeout_ms > 0:
            deadline = time.monotonic() + max(0.2, timeout_ms / 1000.0)
            q = (wait_app or os.path.basename(argv[0])).lower()
            while time.monotonic() < deadline:
                time.sleep(0.08)
                for w in self.state()["windows"]:
                    if w["id"] not in before_ids or (wait_app and q in (w["app"] + " " + w["title"]).lower()):
                        appeared = w
                        break
                if appeared:
                    break
        return {"pid": proc.pid, "command": argv, "window": appeared}
