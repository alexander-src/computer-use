# Computer Use Rules (Linux Fast Mode)

Use MCP server `computer-use_computer-use` via `call_mcp_tool` whenever the user asks to inspect, click, type into, or control desktop applications on Linux. Do NOT spend a turn reading tool `.json` schema files; the full parameter contract is below. Target 1 to 3 tool calls per desktop task.

## Direct Tool Contract (`ServerName: "computer-use_computer-use"`)

- **`desktop_observe`** (1-call state: KWin window list + AT-SPI2 `[ref]` tree + automatic visual fallback):
  - `{mode?: "hybrid"|"ax"|"vision"|"windows", find?, diff?: "auto"|true|false, screenshot?: "auto"|true|false, scope?: "screen"|"active", annotate?, grid?, max_elements?, max_width?, quality?}`
  - In `hybrid` mode (default), attaches a 20 to 27 ms JPEG screenshot automatically whenever the active app exposes fewer than 5 AT-SPI2 elements (browsers, Electron, canvases, games).
- **`desktop_act`** (batch multiple desktop steps in 1 call + smart settle + pre/post pixel diff + takeover safety):
  - `{steps: [{op, ...}, ...], settle?: "smart"|"none"|ms, observe?: "auto"|"ax"|"vision"|"crop_changed"|"none", verify_change?: true, detect_takeover?: true, screenshot?, annotate?, grid?, max_width?, quality?}` (or single-step shorthand `{op: "click", ref: 12}`).
  - Target any pointer step by `ref` (AT-SPI2 ID), `text` (AT-SPI2 label), exact screen `x, y`, or normalized `0..1000` `nx, ny` (`coord_scope?: "screen"|"active"|"last_image"`).
  - Ops:
    - `click`: `{ref|text|x,y|nx,ny, coord_scope?, button?: "left"|"right"|"middle", count?: 1|2|3, double?, modifiers?: ["ctrl","shift","alt","super"]}`
    - `move` / `hover`: `{ref|text|x,y|nx,ny, coord_scope?}`
    - `drag`: `{from_ref|from_x,from_y|from_nx,from_ny, to_ref|to_x,to_y|to_nx,to_ny, coord_scope?, button?, duration_ms?, steps?}`
    - `scroll`: `{direction?: "down"|"up"|"left"|"right", clicks?: 5, ref|text|x,y|nx,ny, coord_scope?}`
    - `type`: `{text, ref|text_target|x,y|nx,ny?, clear?: false, submit?: false, mode?: "auto"|"paste"|"keys", key_delay_ms?}` (auto uses kernel uinput for short ASCII <= 24 chars and atomic save/paste/restore clipboard for longer or Unicode text, automatically switching to `ctrl+shift+v` in terminals)
    - `key` / `hotkey`: `{keys: "ctrl+s"|["ctrl","shift","t"], count?: 1}`
    - `ax_action`: `{ref, action_name?}` (mouse-free AT-SPI2 action)
    - `focus`: `{window_id?|app?|title?}`
    - `window`: `{window_action: "focus"|"minimize"|"maximize"|"unmaximize"|"close"|"move_resize", window_id?|app?|title?, x?, y?, w?, h?}`
    - `launch`: `{command, wait_app?, timeout_ms?}`
    - `wait`: `{ms?|change?: true, area?, threshold_pct?, text?|app?, timeout_ms?}`
    - `screenshot`: `{scope?, area?, ref?, annotate?, grid?, max_width?, quality?}`
- **`desktop_screenshot`**: `{scope?: "screen"|"active"|"window"|"area"|"ref", area?: [x,y,w,h], window_id?, app?, title?, ref?, pad?, annotate?, grid?, cursor?, diff_from_last?, max_width?, quality?, format?: "jpeg"|"png"}`
- **`desktop_windows`**: `{action?: "list"|"focus"|"minimize"|"maximize"|"unmaximize"|"close"|"move_resize"|"launch", window_id?, app?, title?, x?, y?, w?, h?, command?, wait_app?, timeout_ms?}`
- **`desktop_clipboard`**: `{action?: "get"|"set"|"clear", text?}`
- **`desktop_status`**: `{}` (reports active backends, screen geometry, and `/dev/uinput` status)

## Speed and Safety Rules

- For web browsing, prefer `brave-browser_brave-browser` (CDP DOM/AX access) over pixel clicking whenever the task is purely inside Brave. Use `computer-use` for native desktop apps, OS dialogs, window management, file pickers, IDEs, media tools, and cross-app workflows.
- Start with `desktop_observe()` rather than taking blind screenshots. Reuse stable `[ref]` IDs across `AX_DIFF` turns.
- Batch related steps (`focus` + `click` + `type` + `key`) in a single `desktop_act` call.
- When clicking visual UIs without AT-SPI2 trees, pass `grid: true` on `desktop_screenshot` or `desktop_observe`, then click with `nx, ny` (0..1000) and the `coord_scope` reported in the screenshot header (`"screen"` for full screen, `"last_image"` for active/area crops).
- Always prefer `desktop_act({op: "type", ...})` over sending individual letters, and prefer `ax_action` or keyboard shortcuts (`hotkey`) when reliable.
- If `desktop_act` reports `takeover_detected: true` (the user moved the mouse >180px mid-batch), stop immediately and ask the user before resuming.
- For long multi-step desktop sessions (5+ GUI steps), delegate to the `computer-use` subagent via `invoke_subagent` so intermediate screenshots do not bloat the main conversation context.
