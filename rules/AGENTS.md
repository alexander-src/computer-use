# Computer Use Rules (Linux, Windows & macOS Superhuman-Speed Mode)

Use MCP server `computer-use_computer-use` via `call_mcp_tool` whenever the user asks to inspect, click, type into, or control desktop applications on Linux, Windows, or macOS. Do not spend a turn reading tool `.json` schema files; the full contract is below. Aim for 1 tool call per task by executing compound action bursts locally instead of burning cloud vision round-trips.

## Direct Tool Contract (`ServerName: "computer-use_computer-use"`)

- `desktop_act` (superhuman batched execution, 0.05 ms with `observe: "none"`, 7.4 ms with `observe: "auto"`, 0.008 ms non-blocking 120Hz cyan AI cursor + ripples, preserves user physical mouse):
  - `{steps: [{op, ...}, ...], background?: false, fast?: false, smooth?: true, independent_cursor?: true, settle?: "none"|"smart"|ms, observe?: "auto"|"ax"|"vision"|"crop_changed"|"none", verify_change?: false, detect_takeover?: false, screenshot?, annotate?, grid?, max_width?: 896, quality?: 62}` (or single-step shorthand `{op: "click", text: "Save"}`).
  - Moving the user's mouse never aborts by default. The AI glides its own cyan overlay cursor asynchronously at 120Hz (zero sleep on the action path) and restores the user's physical cursor immediately after clicking.
  - Text targets (`text`, `text_target`, `target`) resolve locally in ~40-82 ms via AX/UIA/AXUIElement first, then 8-thread in-process parallel OCR (`libtesseract`) on Chromium/Electron/VS Code/Discord/games with zero cloud screenshots.
  - Compound super-ops (collapse 3 to 6 LLM turns into 1 sub-second call):
    - `open_url`: `{url, app?, new_tab?: false, submit?: true}` (focuses/launches browser, sends `ctrl+l`/`cmd+l` or `ctrl+t`/`cmd+t`, pastes URL, presses Enter)
    - `wait_and_click`: `{text, timeout_ms?: 2500, poll_ms?: 45, area?, button?, count?, double?, clear?, type_text?, submit?}` (polls AX + local OCR every 45 ms and clicks the millisecond text appears, then optionally clears/types/submits)
    - `click_and_type`: `{ref|target|text_target|x,y|nx,ny, text|value, clear?: false, submit?: false, mode?: "auto"|"paste"|"keys"}`
    - `keys`: `{sequence: ["ctrl+t", "tab", "enter"], delay_ms?: 10}` (rapid keyboard macro burst)
    - `repeat`: `{count: 5, steps: [...], delay_ms?: 8}` (local loop, zero extra LLM turns)
  - Core ops:
    - `click`: `{ref|text|x,y|nx,ny, coord_scope?, button?: "left"|"right"|"middle", count?: 1|2|3, double?, modifiers?: ["ctrl","shift","alt","super","cmd"]}`
    - `move` / `hover`: `{ref|text|x,y|nx,ny, coord_scope?}`
    - `drag`: `{from_ref|from_x,from_y|from_nx,from_ny, to_ref|to_x,to_y|to_nx,to_ny, coord_scope?, button?, duration_ms?, steps?}`
    - `scroll`: `{direction?: "down"|"up"|"left"|"right", clicks?: 5, ref|text|x,y|nx,ny, coord_scope?}`
    - `type`: `{text, ref|text_target|x,y|nx,ny?, clear?: false, submit?: false, mode?: "auto"|"paste"|"keys", key_delay_ms?}` (layout-aware Linux `/dev/uinput` + async clipboard restore, UTF-16 `KEYEVENTF_UNICODE` on Windows, `CGEventKeyboardSetUnicodeString` on macOS)
    - `key` / `hotkey`: `{keys: "ctrl+s"|"cmd+s"|["ctrl","shift","t"], count?: 1}`
    - `ax_action`: `{ref, action_name?}` (mouse-free AT-SPI2 / Windows UIA / macOS `AXPress`)
    - `focus`: `{window_id?|app?|title?}`
    - `window`: `{window_action: "focus"|"minimize"|"maximize"|"unmaximize"|"close"|"move_resize", window_id?|app?|title?, x?, y?, w?, h?}`
    - `launch`: `{command, wait_app?, timeout_ms?}`
    - `wait`: `{ms?|change?: true, area?, threshold_pct?, text?|app?, timeout_ms?}` (polls AX + local OCR when `text` is set)
    - `screenshot`: `{scope?, area?, ref?, annotate?, grid?, max_width?, quality?}`
- `desktop_observe` (1-call state: windows + AX `[ref]` tree + optional zero-image `mode: "ocr"` clickable `[x,y]` text + automatic 47 KB `896px` JPEG fallback when AX has <5 elements):
  - `{mode?: "hybrid"|"ax"|"ocr"|"vision"|"windows", background?: false, ocr?: false, find?, diff?: "auto"|true|false, screenshot?: "auto"|true|false, scope?: "screen"|"active", annotate?, grid?, max_elements?, max_width?: 896, quality?: 62}`
- `desktop_background` (zero-touch off-screen automation on Linux `Xvfb`, Windows `PrintWindow`+`PostMessageW`, macOS `CGWindowList`+`AXPress`/`CGEventPostToPid`):
  - `{action?: "status"|"start"|"attach"|"launch"|"stop", width?, height?, command?, window_id?, app?, title?, wait_app?, wait_ms?, timeout_ms?}`
  - Pass `background: true` on `desktop_observe`, `desktop_act`, or `desktop_screenshot` to run off-screen without touching the user's mouse, focus, or clipboard.
- `desktop_screenshot`: `{background?: false, scope?: "screen"|"active"|"window"|"area"|"ref", area?: [x,y,w,h], window_id?, app?, title?, ref?, pad?, annotate?, grid?, cursor?, diff_from_last?, max_width?, quality?, format?: "jpeg"|"png"}`
- `desktop_windows`: `{action?: "list"|"focus"|"minimize"|"maximize"|"unmaximize"|"close"|"move_resize"|"launch", background?: false, window_id?, app?, title?, x?, y?, w?, h?, command?, wait_app?, timeout_ms?}`
- `desktop_clipboard`: `{action?: "get"|"set"|"clear", text?}`
- `desktop_status`: `{}`

## Superhuman-Speed Protocol

- Act first without taking a wasted initial screenshot whenever you already know the app, shortcut, URL, or button label. Call `desktop_act` directly with `focus`/`launch` + `open_url` / `click(text=...)` / `wait_and_click` / `click_and_type` / `keys` / `repeat`.
- Always pass `observe: "none"` on intermediate bursts and predictable actions where you do not need an image back. Only use `observe: "auto"` on the final step when visual confirmation is needed, or `desktop_observe({mode: "ocr"})` when clickable `[x,y]` text is enough (zero image tokens).
- Keep all related steps in ONE `desktop_act` call. Never spend separate LLM turns to focus a window, press a shortcut, type text, and hit Enter.
- For purely in-browser web tasks in Brave, prefer `brave-browser_brave-browser` (CDP DOM/AX access). Use `computer-use` for native desktop apps, OS dialogs, window management, file pickers, IDEs, media tools, Windows/macOS apps, and background GUI workflows.
- When visual multi-step interaction is required (2+ GUI turns), delegate immediately to the `computer-use` (`flash`) subagent via `invoke_subagent` so each turn runs on Flash latency instead of a slow reasoning turn.
