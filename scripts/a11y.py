"""Fast Linux AT-SPI2 accessibility tree walker with stable [ref] IDs, find(), and DIFF snapshots."""

from __future__ import annotations

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
    """Walks AT-SPI2 with strict time/node caps, stable [ref] IDs, and incremental diffs."""

    def __init__(self, session_bus=None):
        self._bus = session_bus
        self._atspi = None
        self._enabled_tried = False
        self._next_ref = 1
        self._key_to_ref: dict[tuple[Any, ...], int] = {}
        self._ref_map: dict[int, dict[str, Any]] = {}
        self._ref_objs: dict[int, Any] = {}
        self._last_window_key: str = ""
        self._last_lines: dict[int, str] = {}
        self._available = self._init_atspi()

    def _init_atspi(self) -> bool:
        try:
            import gi
            gi.require_version("Atspi", "2.0")
            from gi.repository import Atspi
            self._atspi = Atspi
            return True
        except Exception:
            return False

    @property
    def available(self) -> bool:
        return self._available

    def ensure_enabled(self) -> bool:
        """Enable org.a11y.Status.IsEnabled on the session bus (without ScreenReaderEnabled)."""
        if self._enabled_tried:
            return self._available
        self._enabled_tried = True
        try:
            import dbus
            bus = self._bus or dbus.SessionBus()
            proxy = bus.get_object("org.a11y.Bus", "/org/a11y/bus")
            props = dbus.Interface(proxy, "org.freedesktop.DBus.Properties")
            if not bool(props.Get("org.a11y.Status", "IsEnabled")):
                props.Set("org.a11y.Status", "IsEnabled", dbus.Boolean(True))
        except Exception:
            pass
        return self._available

    def get_ref(self, ref_id: int) -> dict[str, Any] | None:
        return self._ref_map.get(int(ref_id))

    def find_by_text(self, query: str) -> dict[str, Any] | None:
        q = query.strip().lower()
        if not q:
            return None
        # Exact name match first, then substring match
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

    def snapshot(
        self,
        *,
        app_hint: str | None = None,
        title_hint: str | None = None,
        pid_hint: int | None = None,
        window_id: str = "",
        find: str | None = None,
        max_elements: int = 120,
        budget_ms: int = 320,
        diff: str | bool = "auto",
    ) -> dict[str, Any]:
        """Walk the target window's AT-SPI2 tree and return compact refs + text/diff."""
        t0 = time.perf_counter()
        if not self._available:
            return {
                "available": False, "window_key": window_id, "elements": [], "text_lines": [],
                "formatted": "(AT-SPI2 unavailable)", "is_diff": False, "elapsed_ms": 0.0,
            }
        self.ensure_enabled()
        Atspi = self._atspi
        deadline = t0 + (budget_ms / 1000.0)

        desktop = Atspi.get_desktop(0)
        root = self._pick_root(desktop, app_hint=app_hint, title_hint=title_hint, pid_hint=pid_hint)
        if root is None:
            return {
                "available": True, "window_key": window_id, "elements": [], "text_lines": [],
                "formatted": "(no AT-SPI2 tree for active window; use visual screenshot / grid)",
                "is_diff": False, "elapsed_ms": round((time.perf_counter() - t0) * 1000.0, 1),
            }

        win_key = window_id or f"{app_hint or ''}:{title_hint or ''}"
        if win_key != self._last_window_key:
            self._key_to_ref.clear()
            self._ref_map.clear()
            self._ref_objs.clear()
            self._last_lines.clear()
            self._next_ref = 1

        elements: list[dict[str, Any]] = []
        text_snippets: list[str] = []
        visited = 0
        max_nodes = 1400
        stack: list[tuple[Any, int]] = [(root, 0)]

        while stack and visited < max_nodes and time.perf_counter() < deadline:
            node, depth = stack.pop()
            if node is None or depth > 18:
                continue
            visited += 1
            try:
                ss = node.get_state_set()
                if not ss.contains(Atspi.StateType.VISIBLE) or not ss.contains(Atspi.StateType.SHOWING):
                    continue
                role = (node.get_role_name() or "").lower()
                name = (node.get_name() or "").strip()
            except Exception:
                continue

            if role in INTERACTIVE_ROLES:
                bbox = self._bbox(node)
                if bbox is not None and bbox[2] > 1 and bbox[3] > 1:
                    val = self._read_text_or_value(node, role)
                    states = self._read_states(ss)
                    # Stable key across small pixel shifts (quantize bbox to 8px grid)
                    skey = (win_key, role, name[:48], bbox[0] // 8, bbox[1] // 8, bbox[2] // 8, bbox[3] // 8)
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
                    self._ref_map[ref] = item
                    self._ref_objs[ref] = node
                    if len(elements) >= max_elements * 2:
                        break
            elif role in TEXT_ROLES:
                txt = name or self._read_text_or_value(node, role)
                if txt and len(txt) <= 180 and txt not in text_snippets:
                    text_snippets.append(txt)
                    if len(text_snippets) > 40:
                        text_snippets = text_snippets[:40]

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

        # Optional find() filter
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
                if title_l and title_l in wname:
                    score += 40
                elif wname and title_l and wname in title_l:
                    score += 25
                if app_l and (app_l in aname or aname in app_l):
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
