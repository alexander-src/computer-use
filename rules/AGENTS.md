# Computer Use Rules (Linux & Windows Fast Mode)

Use MCP server `computer-use_computer-use` via `call_mcp_tool` whenever the user asks to inspect, click, type into, or control desktop applications on Linux or Windows. Do NOT spend a turn reading tool `.json` schema files; the full parameter contract is below. Target 1 to 3 tool calls per desktop task.

## Direct Tool Contract (`ServerName: "computer-use_computer-use"`)

- **`desktop_observe`** (1-call state: window list + active app AT-SPI2/UIA `[ref]` tree + automatic ~5 ms visual fallback):
  - `{mode?: "hybrid"|"ax"|"vision"|"windows", background?: false, find?, diff?: "auto"|true|false, screenshot?: "auto"|true|false, scope?: "screen"|"active", annotate?, grid?, max_elements?, max_width?, quality?}`
  - In `hybrid` mode (default), attaches a ~5 ms JPEG screenshot automatically whenever the active app exposes fewer than 5 accessibility elements.
- **`desktop_act`** (batch multiple steps in 1 call + smooth custom AI cursor + click ripples + independent user mouse):
  - `{steps: [{op, ...}, ...], background?: false, smooth?: true, independent_cursor?: true, settle?: "smart"|"none"|ms, observe?: "auto"|"ax"|"vision"|"crop_changed"|"none", verify_change?: true, detect_takeover?: false, screenshot?, annotate?, grid?, max_width?, quality?}` (or single-step shorthand `{op: "click", ref: 12}`).
  - Moving the user's mouse does NOT abort by default; the AI smoothly glides its own cyan custom cursor overlay, clicks, and immediately restores the user's physical mouse position.
  - Target any pointer step by `ref` (AT-SPI2/UIA ID), `text` (accessibility label), exact screen `x, y`, or normalized `0..1000` `nx, ny` (`coord_scope?: "screen"|"active"|"last_image"`).
  - Ops:
    - `click`: `{ref|text|x,y|nx,ny, coord_scope?, button?: "left"|"right"|"middle", count?: 1|2|3, double?, modifiers?: ["ctrl","shift","alt","super"], smooth?, duration_ms?}`
    - `move` / `hover`: `{ref|text|x,y|nx,ny, coord_scope?, smooth?, duration_ms?}`
    - `drag`: `{from_ref|from_x,from_y|from_nx,from_ny, to_ref|to_x,to_y|to_nx,to_ny, coord_scope?, button?, duration_ms?, steps?}`
    - `scroll`: `{direction?: "down"|"up"|"left"|"right", clicks?: 5, ref|text|x,y|nx,ny, coord_scope?}`
    - `type`: `{text, ref|text_target|x,y|nx,ny?, clear?: false, submit?: false, mode?: "auto"|"paste"|"keys", key_delay_ms?}` (layout-aware: US/GB keymaps + atomic clipboard paste on Linux, layout-independent UTF-16 `KEYEVENTF_UNICODE` on Windows; auto `ctrl+shift+v` in terminals)
    - `key` / `hotkey`: `{keys: "ctrl+s"|["ctrl","shift","t"], count?: 1}`
    - `ax_action`: `{ref, action_name?}` (mouse-free AT-SPI2 Action / Windows UIA Invoke/Toggle with visual cursor glide + ripple)
    - `focus`: `{window_id?|app?|title?}`
    - `window`: `{window_action: "focus"|"minimize"|"maximize"|"unmaximize"|"close"|"move_resize", window_id?|app?|title?, x?, y?, w?, h?}`
    - `launch`: `{command, wait_app?, timeout_ms?}`
    - `wait`: `{ms?|change?: true, area?, threshold_pct?, text?|app?, timeout_ms?}`
    - `screenshot`: `{scope?, area?, ref?, annotate?, grid?, max_width?, quality?}`
- **`desktop_background`** (zero-touch background computer use on Linux and Windows):
  - `{action?: "status"|"start"|"attach"|"launch"|"stop", width?, height?, command?, window_id?, app?, title?, wait_app?, wait_ms?, timeout_ms?}`
  - Linux runs an isolated private `Xvfb` virtual desktop (`start` -> `launch`); Windows attaches `PrintWindow` + `PostMessageW` to a target window (`attach` or `launch`). Pass `background: true` on `desktop_observe`, `desktop_act`, or `desktop_screenshot` to operate completely off-screen without touching the user's mouse, focus, or clipboard.
- **`desktop_screenshot`**: `{background?: false, scope?: "screen"|"active"|"window"|"area"|"ref", area?: [x,y,w,h], window_id?, app?, title?, ref?, pad?, annotate?, grid?, cursor?, diff_from_last?, max_width?, quality?, format?: "jpeg"|"png"}`
- **`desktop_windows`**: `{action?: "list"|"focus"|"minimize"|"maximize"|"unmaximize"|"close"|"move_resize"|"launch", background?: false, window_id?, app?, title?, x?, y?, w?, h?, command?, wait_app?, timeout_ms?}`
- **`desktop_clipboard`**: `{action?: "get"|"set"|"clear", text?}`
- **`desktop_status`**: `{}` (reports OS, active backends, layout, overlay status, background status, and virtual screen geometry)

## Speed and Safety Rules

- For purely in-browser web tasks in Brave, prefer `brave-browser_brave-browser` (CDP DOM/AX access). Use `computer-use` for native desktop apps, OS dialogs, window management, file pickers, IDEs, media tools, Windows apps, and background GUI workflows.
- Start with `desktop_observe()` rather than taking blind screenshots. Reuse stable `[ref]` IDs across `AX_DIFF` turns.
- Batch related steps (`focus` + `click` + `type` + `key`) in a single `desktop_act` call.
- When clicking visual UIs without accessibility trees, pass `grid: true` on `desktop_screenshot` or `desktop_observe`, then click with `nx, ny` (0..1000) and the `coord_scope` reported in the screenshot header.
- Prefer `desktop_background` + `background: true` whenever the user asks to run GUI tasks without interrupting their active workspace.
- For long multi-step desktop sessions (5+ GUI steps), delegate to the `computer-use` subagent via `invoke_subagent` so intermediate screenshots stay out of the main conversation context.
