# Computer Use Rules (Linux, Windows & macOS Human-Speed Mode)

Use MCP server `computer-use_computer-use` via `call_mcp_tool` whenever the user asks to inspect, click, type into, or control desktop applications on Linux, Windows, or macOS. Do NOT spend a turn reading tool `.json` schema files; the full parameter contract is below. Target 1 to 2 tool calls per desktop task by chaining steps like a human power user.

## Direct Tool Contract (`ServerName: "computer-use_computer-use"`)

- **`desktop_observe`** (1-call state: window list + active app AT-SPI2/UIA/AX `[ref]` tree + automatic ~5 ms visual fallback):
  - `{mode?: "hybrid"|"ax"|"vision"|"windows", background?: false, find?, diff?: "auto"|true|false, screenshot?: "auto"|true|false, scope?: "screen"|"active", annotate?, grid?, max_elements?, max_width?: 1024, quality?: 68}`
  - In `hybrid` mode (default), attaches a ~5 ms 1024px JPEG screenshot automatically whenever the active app exposes fewer than 5 accessibility elements (and caches sparse AX apps like Brave/Chrome/Electron for 4s to skip empty DBus walks).
- **`desktop_act`** (batch full workflows in 1 call + smooth 20-55 ms AI cursor + click ripples + independent user mouse):
  - `{steps: [{op, ...}, ...], background?: false, fast?: false, smooth?: true, independent_cursor?: true, settle?: "smart"|"none"|ms, observe?: "auto"|"ax"|"vision"|"crop_changed"|"none", verify_change?: true, detect_takeover?: false, screenshot?, annotate?, grid?, max_width?: 1024, quality?: 68}` (or single-step shorthand `{op: "click", ref: 12}`).
  - Moving the user's mouse does NOT abort by default; the AI glides its own cyan custom cursor overlay, clicks, and immediately restores the user's physical cursor. Pass `fast: true` for instant 0 ms cursor snap + skip settle/diff when pure speed matters.
  - Target any pointer step by `ref` (AT-SPI2/UIA/AX ID), `text` (accessibility label), exact screen `x, y`, or normalized `0..1000` `nx, ny` (`coord_scope?: "screen"|"active"|"last_image"`).
  - Ops:
    - `click`: `{ref|text|x,y|nx,ny, coord_scope?, button?: "left"|"right"|"middle", count?: 1|2|3, double?, modifiers?: ["ctrl","shift","alt","super","cmd"], smooth?, duration_ms?}`
    - `move` / `hover`: `{ref|text|x,y|nx,ny, coord_scope?, smooth?, duration_ms?}`
    - `drag`: `{from_ref|from_x,from_y|from_nx,from_ny, to_ref|to_x,to_y|to_nx,to_ny, coord_scope?, button?, duration_ms?, steps?}`
    - `scroll`: `{direction?: "down"|"up"|"left"|"right", clicks?: 5, ref|text|x,y|nx,ny, coord_scope?}`
    - `type`: `{text, ref|text_target|x,y|nx,ny?, clear?: false, submit?: false, mode?: "auto"|"paste"|"keys", key_delay_ms?}` (auto-detects US/GB/intl on Linux with async clipboard restore; layout-independent UTF-16 `KEYEVENTF_UNICODE` on Windows and `CGEventKeyboardSetUnicodeString` on macOS; auto `cmd+a`/`cmd+v` on macOS and `ctrl+shift+v` in Linux terminals)
    - `key` / `hotkey`: `{keys: "ctrl+s"|"cmd+s"|["ctrl","shift","t"], count?: 1}`
    - `ax_action`: `{ref, action_name?}` (mouse-free AT-SPI2 / Windows UIA Invoke / macOS `AXPress` with visual cursor glide + ripple)
    - `focus`: `{window_id?|app?|title?}`
    - `window`: `{window_action: "focus"|"minimize"|"maximize"|"unmaximize"|"close"|"move_resize", window_id?|app?|title?, x?, y?, w?, h?}`
    - `launch`: `{command, wait_app?, timeout_ms?}`
    - `wait`: `{ms?|change?: true, area?, threshold_pct?, text?|app?, timeout_ms?}`
    - `screenshot`: `{scope?, area?, ref?, annotate?, grid?, max_width?, quality?}`
- **`desktop_background`** (zero-touch background computer use on Linux, Windows, and macOS):
  - `{action?: "status"|"start"|"attach"|"launch"|"stop", width?, height?, command?, window_id?, app?, title?, wait_app?, wait_ms?, timeout_ms?}`
  - Linux runs a private `Xvfb` virtual desktop (`start` -> `launch`); Windows attaches `PrintWindow` + `PostMessageW`; macOS attaches occluded `CGWindowList` capture + `AXPress` / `CGEventPostToPid` (with `open -g` background launch). Pass `background: true` on `desktop_observe`, `desktop_act`, or `desktop_screenshot` to work off-screen without touching the user's mouse, focus, or clipboard.
- **`desktop_screenshot`**: `{background?: false, scope?: "screen"|"active"|"window"|"area"|"ref", area?: [x,y,w,h], window_id?, app?, title?, ref?, pad?, annotate?, grid?, cursor?, diff_from_last?, max_width?, quality?, format?: "jpeg"|"png"}`
- **`desktop_windows`**: `{action?: "list"|"focus"|"minimize"|"maximize"|"unmaximize"|"close"|"move_resize"|"launch", background?: false, window_id?, app?, title?, x?, y?, w?, h?, command?, wait_app?, timeout_ms?}`
- **`desktop_clipboard`**: `{action?: "get"|"set"|"clear", text?}`
- **`desktop_status`**: `{}` (reports OS, active backends, permissions, layout, overlay status, background status, and screen geometry)

## Human-Speed Execution Rules

- Operate like a fast human power user, never one click per LLM turn. Chain the whole logical sequence in ONE `desktop_act` call (for example: `focus` + `hotkey("ctrl+l")` + `type(url, submit=true)` + `wait(change=true, timeout_ms=2000)`).
- Prefer keyboard shortcuts (`ctrl+l`/`cmd+l`, `ctrl+t`/`cmd+t`, `ctrl+f`/`cmd+f`, `ctrl+s`/`cmd+s`, `tab`, `enter`, `esc`) and `ax_action` over hunting menus with the mouse.
- Pass `observe: "none"` (or `fast: true`) when chaining predictable actions that do not need a visual re-check, and `observe: "crop_changed"` when checking small UI updates.
- For purely in-browser web tasks in Brave, prefer `brave-browser_brave-browser` (CDP DOM/AX access). Use `computer-use` for native desktop apps, OS dialogs, window management, file pickers, IDEs, media tools, Windows/macOS apps, and background GUI workflows.
- Start with `desktop_observe()` once rather than blind screenshots, and reuse stable `[ref]` IDs across `AX_DIFF` turns.
- When clicking visual UIs without accessibility trees, read `nx, ny` (`0..1000`) from the auto-attached grid with the reported `coord_scope`.
- Prefer `desktop_background` + `background: true` whenever the user asks to automate an app without interrupting their active workspace.
- For multi-step visual desktop loops (3+ GUI turns), delegate to the `computer-use` (`flash`) subagent via `invoke_subagent` for ~0.5 s turn latency and clean context isolation.
