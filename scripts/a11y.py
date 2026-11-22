"""Fast Linux AT-SPI2 accessibility tree walker with Wayland coord calibration, stable [ref] IDs, and diffs."""

from __future__ import annotations

import atexit
import os
import time
from typing import Any

INTERACTIVE_ROLES = {
    "push button", "toggle button", "check box", "radio button", "menu item",
    "check menu item", "radio menu item", "menu", "combo box", "text", "entry",
    "password text", "spin button", "slider", "page tab", "link", "list item",
    "tree item", "table cell", "scroll bar", "icon", "split button", "document web",
}

TEXT_ROLES = {
    "label", "heading", "paragraph", "static", "status bar", "alert",
    "notification", "info bar", "caption", "section",
}

SHORT_ROLE = {
    "push button": "btn",
    "toggle button": "toggle",
    "check box": "checkbox",
    "radio button": "radio",
    "menu item": "menuitem",
    "check menu item": "menuitem",
    "radio menu item": "menuitem",
    "combo box": "combobox",
    "password text": "password",
    "spin button": "spin",
    "page tab": "tab",
    "list item": "item",
    "tree item": "treeitem",
    "table cell": "cell",
    "scroll bar": "scrollbar",
}


class A11yTree:
    """Walks AT-SPI2 with strict time/node caps, Wayland global-coord offset calibration, stable [ref] IDs, and diffs."""

    def __init__(self, session_bus=None):
        self._bus = session_bus
        self._atspi = None
        self._enabled_tried = False
        self._orig_is_enabled: bool | None = None
        self._orig_sr_enabled: bool | None = None
        self._interactive_role_ids: set[int] = set()
        self._text_role_ids: set[int] = set()
        self._next_ref = 1
        self._key_to_ref: dict[tuple[Any, ...], int] = {}
        self._ref_map: dict[int, dict[str, Any]] = {}
        self._ref_objs: dict[int, Any] = {}
        self._last_window_key: str = ""
        self._last_lines: dict[int, str] = {}
        self._sparse_cache: dict[str, float] = {}
        self._available = self._init_atspi()
        atexit.register(self.close)

    def _init_atspi(self) -> bool:
        try:
            import gi
            gi.require_version("Atspi", "2.0")
            from gi.repository import Atspi
            self._atspi = Atspi
            try:
                Atspi.set_timeout(120, 250)
            except Exception:
                pass
            R = Atspi.Role
            for nm in (
                "PUSH_BUTTON", "TOGGLE_BUTTON", "CHECK_BOX", "RADIO_BUTTON", "MENU_ITEM",
                "CHECK_MENU_ITEM", "RADIO_MENU_ITEM", "MENU", "COMBO_BOX", "TEXT", "ENTRY",
                "PASSWORD_TEXT", "SPIN_BUTTON", "SLIDER", "PAGE_TAB", "LINK", "LIST_ITEM",
                "TREE_ITEM", "TABLE_CELL", "SCROLL_BAR", "ICON", "SPLIT_BUTTON", "DOCUMENT_WEB",
            ):
                if hasattr(R, nm):
                    self._interactive_role_ids.add(int(getattr(R, nm)))
            for nm in (
                "LABEL", "HEADING", "PARAGRAPH", "STATIC", "STATUS_BAR", "ALERT",
                "NOTIFICATION", "INFO_BAR", "CAPTION", "SECTION",
            ):
                if hasattr(R, nm):
                    self._text_role_ids.add(int(getattr(R, nm)))
            return True
        except Exception:
            return False

    @property
    def available(self) -> bool:
        return self._available

    def ensure_enabled(self) -> bool:
        """Enable org.a11y.Status.IsEnabled on session DBus; ScreenReaderEnabled stays strictly opt-in."""
        if self._enabled_tried:
            return self._available
        self._enabled_tried = True
        opt_in_sr = os.environ.get("COMPUTER_USE_ENABLE_SCREENREADER", "0").strip().lower() in ("1", "true", "yes")
        try:
            import dbus
            bus = self._bus or dbus.SessionBus()
            proxy = bus.get_object("org.a11y.Bus", "/org/a11y/bus")
            props = dbus.Interface(proxy, "org.freedesktop.DBus.Properties")
            cur_en = bool(props.Get("org.a11y.Status", "IsEnabled"))
            if not cur_en:
                self._orig_is_enabled = False
                props.Set("org.a11y.Status", "IsEnabled", dbus.Boolean(True))
            if opt_in_sr:
                cur_sr = bool(props.Get("org.a11y.Status", "ScreenReaderEnabled"))
                if not cur_sr:
                    self._orig_sr_enabled = False
                    props.Set("org.a11y.Status", "ScreenReaderEnabled", dbus.Boolean(True))
        except Exception:
            pass
        return self._available

    def close(self) -> None:
        """Restore any modified org.a11y.Status flags on clean exit."""
        if self._orig_is_enabled is None and self._orig_sr_enabled is None:
            return
        try:
            import dbus
            bus = self._bus or dbus.SessionBus()
            proxy = bus.get_object("org.a11y.Bus", "/org/a11y/bus")
            props = dbus.Interface(proxy, "org.freedesktop.DBus.Properties")
            if self._orig_sr_enabled is not None:
                props.Set("org.a11y.Status", "ScreenReaderEnabled", dbus.Boolean(self._orig_sr_enabled))
                self._orig_sr_enabled = None
            if self._orig_is_enabled is not None:
                props.Set("org.a11y.Status", "IsEnabled", dbus.Boolean(self._orig_is_enabled))
                self._orig_is_enabled = None
        except Exception:
            pass

    def get_ref(self, ref_id: int) -> dict[str, Any] | None:
        return self._ref_map.get(int(ref_id))

    def find_by_text(self, query: str) -> dict[str, Any] | None:
        q = query.strip().lower()
        if not q:
            return None
        for el in self._ref_map.values():
            if el.get("name", "").lower() == q:
                return el
        for el in self._ref_map.values():
            hay = f"{el.get('name', '')} {el.get('value', '')}".lower()
            if q in hay:
                return el
        return None

    def do_action(self, ref_id: int, action_name: str | None = None) -> dict[str, Any]:
        """Invoke AT-SPI2 Action.do_action directly on a [ref] without moving the mouse."""
        obj = self._ref_objs.get(int(ref_id))
        meta = self._ref_map.get(int(ref_id))
        if obj is None or meta is None:
            raise ValueError(f"Unknown or stale ref [{ref_id}]")
        iface = obj.get_action_iface()
        if iface is None:
            raise RuntimeError(f"Element [{ref_id}] ({meta['role']}) does not expose AT-SPI Action")
        n = iface.get_n_actions()
        if n <= 0:
            raise RuntimeError(f"Element [{ref_id}] has no AT-SPI actions")
        chosen = 0
        chosen_name = iface.get_action_name(0)
        if action_name:
            want = action_name.lower()
            for idx in range(n):
                nm = (iface.get_action_name(idx) or "").lower()
                if want in nm:
                    chosen = idx
                    chosen_name = nm
                    break
        ok = bool(iface.do_action(chosen))
        return {"ok": ok, "ref": ref_id, "action": chosen_name, "name": meta.get("name", "")}

    @staticmethod
    def _wayland_coord_offset(
        root_ext: tuple[int, int, int, int] | None,
        window_rect: tuple[int, int, int, int] | None,
    ) -> tuple[int, int]:
        """Calibrate window-local Wayland AT-SPI2 extents to global compositor coordinates.

        On Wayland (KWin/Sway/Hyprland/Mutter), clients do not know global screen origins, so
        Atspi.Component.get_extents(CoordType.SCREEN) often returns (0, 0)-relative coordinates
        even when the window sits at (win_x, win_y). Account for CSD shadow padding as well.
        """
        if not os.environ.get("WAYLAND_DISPLAY") or root_ext is None or window_rect is None:
            return 0, 0
        rx, ry, rw, rh = root_ext
        wx, wy, ww, wh = window_rect
        if (wx == 0 and wy == 0) or (abs(rx - wx) <= 24 and abs(ry - wy) <= 24):
            return 0, 0
        # Root starts near window-local (0, 0) while compositor reports a non-zero global window origin
        if abs(rx) <= 48 and abs(ry) <= 48:
            pad_x = max(0, (rw - ww) // 2) if (rw > ww and (rw - ww) <= 80) else 0
            pad_y = max(0, (rh - wh) // 2) if (rh > wh and (rh - wh) <= 80) else 0
            return (wx - rx - pad_x), (wy - ry - pad_y)
        return 0, 0

    def snapshot(
        self,
        *,
        app_hint: str | None = None,
        title_hint: str | None = None,
        pid_hint: int | None = None,
        window_id: str = "",
        window_rect: tuple[int, int, int, int] | None = None,
        find: str | None = None,
        max_elements: int = 120,
        budget_ms: int = 110,
        diff: str | bool = "auto",
    ) -> dict[str, Any]:
        """Walk the target window's AT-SPI2 tree and return calibrated refs + text/diff."""
        t0 = time.perf_counter()
        win_key = window_id or f"{app_hint or ''}:{title_hint or ''}"
        if win_key != self._last_window_key:
            self._key_to_ref.clear()
            self._ref_map.clear()
            self._ref_objs.clear()
            self._last_lines.clear()
            self._next_ref = 1

        if not self._available:
            self._last_window_key = win_key
            return {
                "available": False, "window_key": win_key, "elements": [], "text_lines": [],
                "formatted": "(AT-SPI2 unavailable)", "is_diff": False, "elapsed_ms": 0.0,
            }
        # Skip repeated AT-SPI2 DBus walks on known sparse/empty windows (e.g. Brave/Chrome/Electron/games) for 4s
        if find is None and diff != False:
            sparse_ts = self._sparse_cache.get(win_key)
            if sparse_ts is not None and (t0 - sparse_ts) < 4.0:
                self._last_window_key = win_key
                return {
                    "available": True, "window_key": win_key, "elements": [], "text_lines": [],
                    "formatted": "(sparse AT-SPI2 window cached; using ~5ms visual + grid)",
                    "is_diff": False, "elapsed_ms": 0.0,
                }
        self.ensure_enabled()
        Atspi = self._atspi
        deadline = t0 + (budget_ms / 1000.0)

        desktop = Atspi.get_desktop(0)
        root = self._pick_root(desktop, app_hint=app_hint, title_hint=title_hint, pid_hint=pid_hint)
        if root is None:
            self._ref_map.clear()
            self._ref_objs.clear()
            self._last_lines.clear()
            self._last_window_key = win_key
            self._sparse_cache[win_key] = time.perf_counter()
            return {
                "available": True, "window_key": win_key, "elements": [], "text_lines": [],
                "formatted": "(no AT-SPI2 tree for active window; using visual screenshot + grid)",
                "is_diff": False, "elapsed_ms": round((time.perf_counter() - t0) * 1000.0, 1),
            }

        dx, dy = self._wayland_coord_offset(self._bbox(root), window_rect)

        elements: list[dict[str, Any]] = []
        new_ref_map: dict[int, dict[str, Any]] = {}
        new_ref_objs: dict[int, Any] = {}
        key_counts: dict[tuple[Any, ...], int] = {}
        text_snippets: list[str] = []
        visited = 0
        max_nodes = 1400 if find else 1200
        stack: list[tuple[Any, int]] = [(root, 0)]
        st_visible = Atspi.StateType.VISIBLE
        st_showing = Atspi.StateType.SHOWING

        while stack and visited < max_nodes and time.perf_counter() < deadline:
            node, depth = stack.pop()
            if node is None or depth > 18:
                continue
            visited += 1
            try:
                ss = node.get_state_set()
                if not ss.contains(st_visible):
                    continue
                if depth > 0 and not ss.contains(st_showing):
                    continue
                role_id = int(node.get_role())
            except Exception:
                continue

            if role_id in self._interactive_role_ids:
                raw_bbox = self._bbox(node)
                if raw_bbox is not None and raw_bbox[2] > 1 and raw_bbox[3] > 1:
                    bbox = (raw_bbox[0] + dx, raw_bbox[1] + dy, raw_bbox[2], raw_bbox[3])
                    try:
                        role = (node.get_role_name() or "").lower()
                        name = (node.get_name() or "").strip()
                    except Exception:
                        role, name = "control", ""
                    val = self._read_text_or_value(node, role)
                    states = self._read_states(ss)
                    base_key = (win_key, role, name[:48], bbox[0] // 8, bbox[1] // 8, bbox[2] // 8, bbox[3] // 8)
                    occ = key_counts.get(base_key, 0)
                    key_counts[base_key] = occ + 1
                    skey = (*base_key, occ)
                    ref = self._key_to_ref.get(skey)
                    if ref is None:
                        ref = self._next_ref
                        self._next_ref += 1
                        self._key_to_ref[skey] = ref
                    cx = bbox[0] + bbox[2] // 2
                    cy = bbox[1] + bbox[3] // 2
                    item = {
                        "ref": ref,
                        "role": SHORT_ROLE.get(role, role),
                        "raw_role": role,
                        "name": name[:100],
                        "value": val[:120] if val else "",
                        "states": states,
                        "bbox": bbox,
                        "center": [cx, cy],
                    }
                    elements.append(item)
                    new_ref_map[ref] = item
                    new_ref_objs[ref] = node
                    if not find and len(elements) >= max_elements * 2:
                        break
            elif role_id in self._text_role_ids and len(text_snippets) < 40:
                try:
                    name = (node.get_name() or "").strip()
                except Exception:
                    name = ""
                txt = name or self._read_text_or_value(node, "label")
                if txt and len(txt) <= 180 and txt not in text_snippets:
                    text_snippets.append(txt)

            try:
                n_children = node.get_child_count()
                if n_children > 0:
                    cap = min(n_children, 80)
                    for idx in range(cap - 1, -1, -1):
                        ch = node.get_child_at_index(idx)
                        if ch is not None:
                            stack.append((ch, depth + 1))
            except Exception:
                pass

        # Prune dead refs so stale widgets from closed tabs/dialogs never remain clickable
        self._ref_map = new_ref_map
        self._ref_objs = new_ref_objs
        if find is None:
            if len(elements) < 5:
                self._sparse_cache[win_key] = time.perf_counter()
            else:
                self._sparse_cache.pop(win_key, None)

        if find:
            q = find.strip().lower()
            elements = [
                e for e in elements
                if q in e["name"].lower() or q in e["value"].lower() or q in e["role"].lower()
            ]
            text_snippets = [t for t in text_snippets if q in t.lower()]

        elements = elements[:max_elements]
        cur_lines = {e["ref"]: self._format_elem(e) for e in elements}

        use_diff = (diff is True) or (diff == "auto" and win_key == self._last_window_key and bool(self._last_lines) and not find)
        is_diff = False
        if use_diff:
            added = [line for r, line in cur_lines.items() if r not in self._last_lines]
            changed = [line for r, line in cur_lines.items() if r in self._last_lines and self._last_lines[r] != line]
            removed = [r for r in self._last_lines if r not in cur_lines]
            parts: list[str] = []
            if added:
                parts.append("+ ADDED:\n" + "\n".join(added[:50]))
            if changed:
                parts.append("~ CHANGED:\n" + "\n".join(changed[:50]))
            if removed:
                parts.append(f"- REMOVED REFS: {removed[:40]}")
            formatted = "\n".join(parts) if parts else "(no AX tree changes; prior [ref] IDs still valid)"
            is_diff = True
        else:
            lines_out = list(cur_lines.values())
            if text_snippets:
                lines_out.append("--- TEXT ---")
                lines_out.extend(text_snippets[:25])
            formatted = "\n".join(lines_out) if lines_out else "(empty AX tree)"

        self._last_window_key = win_key
        if not find:
            self._last_lines = cur_lines

        return {
            "available": True,
            "window_key": win_key,
            "elements": elements,
            "text_lines": text_snippets,
            "formatted": formatted,
            "is_diff": is_diff,
            "visited": visited,
            "coord_offset": [dx, dy],
            "elapsed_ms": round((time.perf_counter() - t0) * 1000.0, 1),
        }

    def _pick_root(self, desktop, *, app_hint: str | None, title_hint: str | None, pid_hint: int | None):
        Atspi = self._atspi
        n_apps = desktop.get_child_count()
        app_l = (app_hint or "").lower()
        title_l = (title_hint or "").lower()
        best = None
        best_score = -1

        for i in range(n_apps):
            app = desktop.get_child_at_index(i)
            if app is None:
                continue
            try:
                aname = (app.get_name() or "").lower()
                apid = app.get_process_id()
                n_wins = app.get_child_count()
            except Exception:
                continue
            for j in range(n_wins):
                win = app.get_child_at_index(j)
                if win is None:
                    continue
                try:
                    wname = (win.get_name() or "").lower()
                    ss = win.get_state_set()
                    is_active = ss.contains(Atspi.StateType.ACTIVE)
                except Exception:
                    continue
                score = 0
                if pid_hint and apid == pid_hint:
                    score += 100
                if is_active:
                    score += 50
                if title_l and wname and title_l in wname:
                    score += 40
                elif wname and title_l and wname in title_l:
                    score += 25
                if app_l and aname and (app_l in aname or aname in app_l):
                    score += 30
                if score > best_score and score > 0:
                    best_score = score
                    best = win
        return best

    def _bbox(self, node) -> tuple[int, int, int, int] | None:
        try:
            comp = node.get_component_iface()
            if comp is None:
                return None
            ext = comp.get_extents(self._atspi.CoordType.SCREEN)
            return int(ext.x), int(ext.y), int(ext.width), int(ext.height)
        except Exception:
            return None

    def _read_text_or_value(self, node, role: str) -> str:
        try:
            ti = node.get_text_iface()
            if ti is not None:
                n = min(ti.get_character_count(), 160)
                if n > 0:
                    return (ti.get_text(0, n) or "").replace("\n", " ").strip()
        except Exception:
            pass
        if role in ("slider", "spin button", "scroll bar"):
            try:
                vi = node.get_value_iface()
                if vi is not None:
                    return f"{vi.get_current_value():.3g}"
            except Exception:
                pass
        return ""

    def _read_states(self, ss) -> list[str]:
        Atspi = self._atspi
        flags: list[str] = []
        for st, lbl in (
            (Atspi.StateType.FOCUSED, "focused"),
            (Atspi.StateType.CHECKED, "checked"),
            (Atspi.StateType.SELECTED, "selected"),
            (Atspi.StateType.EXPANDED, "expanded"),
            (Atspi.StateType.PRESSED, "pressed"),
        ):
            if ss.contains(st):
                flags.append(lbl)
        if not ss.contains(Atspi.StateType.SENSITIVE):
            flags.append("disabled")
        return flags

    @staticmethod
    def _format_elem(e: dict[str, Any]) -> str:
        parts = [f"[{e['ref']}] {e['role']}"]
        if e["name"]:
            parts.append(f'"{e["name"]}"')
        if e["value"] and e["value"] != e["name"]:
            parts.append(f'val="{e["value"]}"')
        if e["states"]:
            parts.append("[" + ",".join(e["states"]) + "]")
        cx, cy = e["center"]
        parts.append(f"@({cx},{cy})")
        return " ".join(parts)
