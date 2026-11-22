"""Ultra-fast Linux window manager bridge (KDE KWin 6 Scripting DBus ~1.2 ms, Hyprland, Sway, X11)."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
from typing import Any


class WindowManager:
    """List, focus, move/resize, minimize, maximize, close, and launch desktop windows."""

    def __init__(self, session_bus=None):
        self._bus = session_bus
        self._seq = 0
        self._kwin_lock = threading.Lock()
        self._backend = self._detect()
        self._cached_bounds: tuple[int, int, int, int] | None = None
        self._cached_bounds_at = 0.0
        self._tmp_dir = "/dev/shm" if os.path.isdir("/dev/shm") and os.access("/dev/shm", os.W_OK) else None
        self._spawned: list[subprocess.Popen] = []

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

    def virtual_bounds(self) -> tuple[int, int, int, int]:
        """Return (vx, vy, vw, vh) bounding all monitors, supporting negative monitor offsets."""
        now = time.monotonic()
        if self._cached_bounds and (now - self._cached_bounds_at) < 5.0:
            return self._cached_bounds
        bounds = self._query_virtual_bounds()
        self._cached_bounds = bounds
        self._cached_bounds_at = now
        return bounds

    def screen_size(self) -> tuple[int, int]:
        _, _, vw, vh = self.virtual_bounds()
        return vw, vh

    def _query_virtual_bounds(self) -> tuple[int, int, int, int]:
        if self._backend == "kwin-dbus":
            try:
                data = self._run_kwin_js(
                    """
                    var sc = workspace.screens || [];
                    if (sc.length > 0) {
                        var minX = 1e9, minY = 1e9, maxX = -1e9, maxY = -1e9;
                        for (var i = 0; i < sc.length; i++) {
                            var g = sc[i].geometry;
                            if (g.x < minX) minX = g.x;
                            if (g.y < minY) minY = g.y;
                            if (g.x + g.width > maxX) maxX = g.x + g.width;
                            if (g.y + g.height > maxY) maxY = g.y + g.height;
                        }
                        if (maxX > minX && maxY > minY) {
                            return {x: Math.round(minX), y: Math.round(minY), w: Math.round(maxX - minX), h: Math.round(maxY - minY)};
                        }
                    }
                    var vs = workspace.virtualScreenSize;
                    return {x: 0, y: 0, w: Math.round(vs.width), h: Math.round(vs.height)};
                    """
                )
                if data and data.get("w", 0) > 0 and data.get("h", 0) > 0:
                    return int(data["x"]), int(data["y"]), int(data["w"]), int(data["h"])
            except Exception:
                pass
        if self._backend == "hyprland":
            try:
                mons = json.loads(subprocess.check_output(["hyprctl", "monitors", "-j"], text=True, timeout=2))
                if mons:
                    xs, ys, x2s, y2s = [], [], [], []
                    for m in mons:
                        sc = max(0.5, float(m.get("scale", 1.0)))
                        mw, mh = m["width"], m["height"]
                        if int(m.get("transform", 0)) % 2 == 1:
                            mw, mh = mh, mw
                        mx, my = int(m["x"]), int(m["y"])
                        xs.append(mx)
                        ys.append(my)
                        x2s.append(mx + int(round(mw / sc)))
                        y2s.append(my + int(round(mh / sc)))
                    if xs and max(x2s) > min(xs) and max(y2s) > min(ys):
                        return min(xs), min(ys), max(x2s) - min(xs), max(y2s) - min(ys)
            except Exception:
                pass
        if self._backend == "sway":
            try:
                outs = json.loads(subprocess.check_output(["swaymsg", "-t", "get_outputs"], text=True, timeout=2))
                active_outs = [o for o in outs if o.get("active") and o.get("rect")]
                if active_outs:
                    xs = [int(o["rect"]["x"]) for o in active_outs]
                    ys = [int(o["rect"]["y"]) for o in active_outs]
                    x2s = [int(o["rect"]["x"]) + int(o["rect"]["width"]) for o in active_outs]
                    y2s = [int(o["rect"]["y"]) + int(o["rect"]["height"]) for o in active_outs]
                    return min(xs), min(ys), max(x2s) - min(xs), max(y2s) - min(ys)
            except Exception:
                pass
        if shutil.which("kscreen-doctor"):
            r = subprocess.run(["kscreen-doctor", "-o"], capture_output=True, text=True, timeout=2)
            if r.returncode == 0:
                xs, ys, x2s, y2s = [], [], [], []
                for m in re.finditer(r"Geometry:\s*(-?\d+),(-?\d+)\s+(\d+)x(\d+)", r.stdout):
                    x, y, w, h = map(int, m.groups())
                    xs.append(x)
                    ys.append(y)
                    x2s.append(x + w)
                    y2s.append(y + h)
                if xs:
                    return min(xs), min(ys), max(x2s) - min(xs), max(y2s) - min(ys)
        if shutil.which("xrandr") and os.environ.get("DISPLAY"):
            r = subprocess.run(["xrandr", "--current"], capture_output=True, text=True, timeout=2)
            m = re.search(r"current\s+(\d+)\s+x\s+(\d+)", r.stdout)
            if m:
                return 0, 0, int(m.group(1)), int(m.group(2))
        return 0, 0, 1920, 1080

    def _run_kwin_js(self, body_js: str, timeout_ms: int = 1200) -> Any:
        """Execute a short KWin 6 script via /dev/shm and receive JSON back over DBus in ~1.2 ms."""
        import dbus
        import dbus.service
        from gi.repository import GLib

        with self._kwin_lock:
            bus = self._bus or dbus.SessionBus()
            bus_name = bus.get_unique_name()
            self._seq += 1
            tag = f"cu_{os.getpid()}_{self._seq}"
            obj_path = f"/AntigravityCU/{tag}"
            iface_name = "org.antigravity.ComputerUse"
            result_box: list[str] = []
            timed_out = [False]
            loop = GLib.MainLoop()

            class _Receiver(dbus.service.Object):
                @dbus.service.method(iface_name, in_signature="s", out_signature="")
                def Report(self, payload: str) -> None:
                    result_box.append(str(payload))
                    if loop.is_running():
                        loop.quit()

            def _on_timeout() -> bool:
                timed_out[0] = True
                if loop.is_running():
                    loop.quit()
                return False

            receiver = _Receiver(bus, obj_path)
            wrapped = (
                "(function() {\n"
                f"  var __res = (function() {{\n{body_js}\n  }})();\n"
                f'  callDBus("{bus_name}", "{obj_path}", "{iface_name}", "Report", JSON.stringify(__res === undefined ? null : __res));\n'
                "})();\n"
            )
            script_path = ""
            scripting = dbus.Interface(bus.get_object("org.kde.KWin", "/Scripting"), "org.kde.kwin.Scripting")
            sid = -1
            script_obj = None
            try:
                with tempfile.NamedTemporaryFile(
                    "w", suffix=".js", prefix="agcu_", dir=self._tmp_dir, delete=False, encoding="utf-8"
                ) as f:
                    f.write(wrapped)
                    script_path = f.name
                sid = int(scripting.loadScript(script_path, tag, signature="ss"))
                if sid < 0:
                    raise RuntimeError("KWin loadScript failed")
                script_obj = dbus.Interface(bus.get_object("org.kde.KWin", f"/Scripting/Script{sid}"), "org.kde.kwin.Script")
                script_obj.run()
                if not result_box:
                    timer_id = GLib.timeout_add(timeout_ms, _on_timeout)
                    loop.run()
                    if not timed_out[0]:
                        GLib.source_remove(timer_id)
            finally:
                if script_obj is not None:
                    try:
                        script_obj.stop()
                    except Exception:
                        pass
                if sid >= 0:
                    try:
                        scripting.unloadScript(tag)
                    except Exception:
                        pass
                receiver.remove_from_connection()
                if script_path and os.path.exists(script_path):
                    try:
                        os.unlink(script_path)
                    except Exception:
                        pass
            if not result_box:
                raise TimeoutError("KWin scripting callback timed out")
            return json.loads(result_box[0])

    def cursor_pos(self) -> tuple[int, int]:
        """Fast single-call query for the current OS cursor position."""
        if self._backend == "kwin-dbus":
            try:
                d = self._run_kwin_js("var c = workspace.cursorPos; return {x: Math.round(c.x), y: Math.round(c.y)};")
                return int(d["x"]), int(d["y"])
            except Exception:
                pass
        elif self._backend == "hyprland":
            try:
                raw = subprocess.check_output(["hyprctl", "cursorpos"], text=True, timeout=1.5).strip()
                if "," in raw:
                    x, y = raw.split(",", 1)
                    return int(x.strip()), int(y.strip())
            except Exception:
                pass
        elif self._backend == "x11" and shutil.which("xdotool"):
            try:
                r = subprocess.run(["xdotool", "getmouselocation", "--shell"], capture_output=True, text=True, timeout=1.5)
                cx = cy = 0
                for line in r.stdout.splitlines():
                    if line.startswith("X="):
                        cx = int(line[2:])
                    elif line.startswith("Y="):
                        cy = int(line[2:])
                return cx, cy
            except Exception:
                pass
        st = self.state()
        return int(st["cursor"]["x"]), int(st["cursor"]["y"])

    def _sway_windows(self) -> list[dict[str, Any]]:
        tree = json.loads(subprocess.check_output(["swaymsg", "-t", "get_tree"], text=True, timeout=2))
        wins: list[dict[str, Any]] = []

        def walk(node: dict[str, Any]) -> None:
            if node.get("type") in ("con", "floating_con") and node.get("pid"):
                r = node.get("rect") or {}
                if r.get("width", 0) > 1 and r.get("height", 0) > 1:
                    app = node.get("app_id") or (node.get("window_properties") or {}).get("class") or "window"
                    wins.append({
                        "id": str(node["id"]),
                        "title": str(node.get("name") or ""),
                        "app": str(app),
                        "pid": int(node.get("pid") or 0),
                        "x": int(r["x"]), "y": int(r["y"]),
                        "w": int(r["width"]), "h": int(r["height"]),
                        "active": bool(node.get("focused")),
                        "minimized": False,
                        "fullscreen": bool(node.get("fullscreen_mode")),
                    })
            for ch in (node.get("nodes") or []) + (node.get("floating_nodes") or []):
                walk(ch)

        walk(tree)
        return wins

    def state(self) -> dict[str, Any]:
        """Return {'screen': {x, y, w, h}, 'cursor': {x, y}, 'windows': [...], 'active': {...}|None}."""
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
                var sc = workspace.screens || [];
                var minX = 0, minY = 0, sw = 0, sh = 0;
                if (sc.length > 0) {
                    minX = 1e9; minY = 1e9; var maxX = -1e9, maxY = -1e9;
                    for (var j = 0; j < sc.length; j++) {
                        var sg = sc[j].geometry;
                        if (sg.x < minX) minX = sg.x;
                        if (sg.y < minY) minY = sg.y;
                        if (sg.x + sg.width > maxX) maxX = sg.x + sg.width;
                        if (sg.y + sg.height > maxY) maxY = sg.y + sg.height;
                    }
                    sw = Math.round(maxX - minX);
                    sh = Math.round(maxY - minY);
                    minX = Math.round(minX);
                    minY = Math.round(minY);
                } else {
                    var vs = workspace.virtualScreenSize;
                    sw = Math.round(vs.width);
                    sh = Math.round(vs.height);
                }
                return {
                    screen: {x: minX, y: minY, w: sw, h: sh},
                    cursor: {x: Math.round(c.x), y: Math.round(c.y)},
                    windows: wins
                };
                """
            )
            sc = data["screen"]
            self._cached_bounds = (int(sc.get("x", 0)), int(sc.get("y", 0)), int(sc["w"]), int(sc["h"]))
            self._cached_bounds_at = time.monotonic()
            data["active"] = next((w for w in data["windows"] if w.get("active")), None)
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
            vx, vy, vw, vh = self.virtual_bounds()
            return {"screen": {"x": vx, "y": vy, "w": vw, "h": vh}, "cursor": {"x": cx, "y": cy}, "windows": wins,
                    "active": next((w for w in wins if w["active"]), None)}

        if self._backend == "sway":
            wins = self._sway_windows()
            vx, vy, vw, vh = self.virtual_bounds()
            return {"screen": {"x": vx, "y": vy, "w": vw, "h": vh}, "cursor": {"x": 0, "y": 0}, "windows": wins,
                    "active": next((w for w in wins if w["active"]), None)}

        if self._backend == "x11":
            return self._x11_state()

        vx, vy, vw, vh = self.virtual_bounds()
        return {"screen": {"x": vx, "y": vy, "w": vw, "h": vh}, "cursor": {"x": 0, "y": 0}, "windows": [], "active": None}

    def _x11_state(self) -> dict[str, Any]:
        vx, vy, vw, vh = self.virtual_bounds()
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
            # wmctrl -lpGx has 10 columns: WID DESKTOP PID X Y W H WM_CLASS CLIENT_MACHINE TITLE...
            r = subprocess.run(["wmctrl", "-lpGx"], capture_output=True, text=True, timeout=2)
            for line in r.stdout.splitlines():
                parts = line.split(None, 9)
                if len(parts) < 9:
                    continue
                wid = hex(int(parts[0], 16))
                wins.append({
                    "id": wid,
                    "title": parts[9] if len(parts) > 9 else "",
                    "app": parts[7],
                    "pid": int(parts[2]),
                    "x": int(parts[3]), "y": int(parts[4]),
                    "w": int(parts[5]), "h": int(parts[6]),
                    "active": wid == active_id,
                    "minimized": False,
                    "fullscreen": False,
                })
        return {"screen": {"x": vx, "y": vy, "w": vw, "h": vh}, "cursor": {"x": cx, "y": cy}, "windows": wins,
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
                subprocess.run(["hyprctl", "dispatch", "focuswindow", f"address:{wid}"], check=True, capture_output=True, timeout=2)
            elif act == "close":
                subprocess.run(["hyprctl", "dispatch", "closewindow", f"address:{wid}"], check=True, capture_output=True, timeout=2)
            else:
                raise ValueError(f"Action {act!r} not supported on Hyprland")
            return {**target, "action": act}

        if self._backend == "sway":
            if act == "focus":
                subprocess.run(["swaymsg", f"[con_id={wid}] focus"], check=True, capture_output=True, timeout=2)
            elif act == "close":
                subprocess.run(["swaymsg", f"[con_id={wid}] kill"], check=True, capture_output=True, timeout=2)
            elif act == "maximize":
                subprocess.run(["swaymsg", f"[con_id={wid}] fullscreen enable"], check=True, capture_output=True, timeout=2)
            elif act == "unmaximize":
                subprocess.run(["swaymsg", f"[con_id={wid}] fullscreen disable"], check=True, capture_output=True, timeout=2)
            elif act == "minimize":
                subprocess.run(["swaymsg", f"[con_id={wid}] move scratchpad"], check=True, capture_output=True, timeout=2)
            else:
                raise ValueError(f"Action {act!r} not supported on Sway")
            return {**target, "action": act}

        if self._backend == "x11":
            if act == "minimize" and shutil.which("xdotool"):
                subprocess.run(["xdotool", "windowminimize", str(int(wid, 16))], check=True, capture_output=True, timeout=2)
                return {**target, "action": act}
            if shutil.which("wmctrl"):
                if act == "focus":
                    subprocess.run(["wmctrl", "-i", "-a", wid], check=True, capture_output=True, timeout=2)
                elif act == "close":
                    subprocess.run(["wmctrl", "-i", "-c", wid], check=True, capture_output=True, timeout=2)
                elif act == "maximize":
                    subprocess.run(["wmctrl", "-i", "-r", wid, "-b", "add,maximized_vert,maximized_horz"], check=True, capture_output=True, timeout=2)
                elif act == "unmaximize":
                    subprocess.run(["wmctrl", "-i", "-r", wid, "-b", "remove,maximized_vert,maximized_horz"], check=True, capture_output=True, timeout=2)
                elif act == "move_resize":
                    nx = target["x"] if x is None else int(x)
                    ny = target["y"] if y is None else int(y)
                    nw = target["w"] if w is None else int(w)
                    nh = target["h"] if h is None else int(h)
                    subprocess.run(["wmctrl", "-i", "-r", wid, "-e", f"0,{nx},{ny},{nw},{nh}"], check=True, capture_output=True, timeout=2)
                else:
                    raise ValueError(f"Unsupported X11 window action: {action!r}")
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
        # Reap any previously finished child processes so we never leak zombies
        self._spawned = [p for p in self._spawned if p.poll() is None]
        before_ids = {w["id"] for w in self.state()["windows"]}
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self._spawned.append(proc)
        appeared = None
        if wait_app is not None or timeout_ms > 0:
            deadline = time.monotonic() + max(0.2, timeout_ms / 1000.0)
            q = (wait_app or os.path.basename(argv[0])).lower()
            while time.monotonic() < deadline:
                time.sleep(0.06)
                for w in self.state()["windows"]:
                    if w["id"] not in before_ids:
                        if not wait_app or q in (w["app"] + " " + w["title"]).lower():
                            appeared = w
                            break
                if appeared:
                    break
        return {"pid": proc.pid, "command": argv, "window": appeared}
