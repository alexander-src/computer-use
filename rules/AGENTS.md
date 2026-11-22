# Computer Use Rules (Linux, Windows & macOS Fast Mode)

Use MCP server `computer-use_computer-use` via `call_mcp_tool` whenever the user asks to inspect, click, type into, or control desktop applications on Linux, Windows, or macOS. Do not spend a turn reading tool `.json` schema files; the full contract is below.

## Direct Tool Contract (`ServerName: "computer-use_computer-use"`)

- `desktop_act` (batched local execution, 0.05 ms with `observe: "none"`, non-blocking 120Hz cyan AI cursor + ripples, preserves user physical mouse):
  - `{steps: [{op, ...}, ...], background?: false, fast?: false, smooth?: true, independent_cursor?: true, settle?: "none"|"smart"|ms, observe?: "auto"|"ax"|"ocr"|"vision"|"crop_changed"|"none", verify_change?: false, detect_takeover?: false, screenshot?, annotate?, grid?, max_width?: 896, quality?: 62}` (or single-step shorthand `{op: "click", text: "Save"}`).
  - Moving the user's mouse never aborts by default. The AI glides its own cyan overlay cursor asynchronously at 120Hz (zero sleep on the action path) and restores the user's physical cursor immediately after clicking.
  - Text targets (`text`, `text_target`, `target`) resolve locally via AX/UIA/AXUIElement first, then pre-warmed 8-thread `tessdata_fast` OCR with local dark/light tile normalization (reads both dark apps and white modals on the same screen) and ~22 ms popup-diff cropping inside `wait_and_click`. Pass `offset_x` / `offset_y` to click an input box below a label (e.g. `offset_y: 28`) or an icon beside a header (e.g. `offset_x: 110`).
  - Compound ops (collapse multi-dialog flows into 1 tool call):
    - `launch`: `{command, wait_app?, timeout_ms?}` (focuses an already-open matching window in ~1.5 ms, or launches and waits for the matching window automatically)
    - `open_url`: `{url, app?, new_tab?: false, submit?: true}` (focuses/launches browser, sends `ctrl+l`/`cmd+l` or `ctrl+t`/`cmd+t`, pastes URL, presses Enter)
    - `wait_and_click`: `{text, timeout_ms?: 2500, poll_ms?: 25, area?, offset_x?, offset_y?, button?, count?, double?, clear?, type_text?, submit?}` (diffs against the pre-click frame, OCRs only the changed modal in ~22 ms, and clicks as soon as text appears, then optionally clears/types/submits)
    - `click_and_type`: `{ref|target|text_target|x,y|nx,ny, offset_x?, offset_y?, text|value, clear?: false, submit?: false, mode?: "auto"|"paste"|"keys"}`
    - `keys`: `{sequence: ["ctrl+t", "tab", "enter"], delay_ms?: 10}` (rapid keyboard macro burst)
    - `repeat`: `{count: 5, steps: [...], delay_ms?: 8}` (local loop, zero extra LLM turns)
  - Core ops:
    - `click`: `{ref|text|x,y|nx,ny, offset_x?, offset_y?, coord_scope?, button?: "left"|"right"|"middle", count?: 1|2|3, double?, modifiers?: ["ctrl","shift","alt","super","cmd"]}`
    - `move` / `hover`: `{ref|text|x,y|nx,ny, coord_scope?}`
    - `drag`: `{from_ref|from_x,from_y|from_nx,from_ny, to_ref|to_x,to_y|to_nx,to_ny, coord_scope?, button?, duration_ms?, steps?}`
    - `scroll`: `{direction?: "down"|"up"|"left"|"right", clicks?: 5, ref|text|x,y|nx,ny, coord_scope?}`
    - `type`: `{text, ref|text_target|x,y|nx,ny?, clear?: false, submit?: false, mode?: "auto"|"paste"|"keys", key_delay_ms?}` (layout-aware Linux `/dev/uinput` + async clipboard restore, UTF-16 `KEYEVENTF_UNICODE` on Windows, `CGEventKeyboardSetUnicodeString` on macOS)
    - `key` / `hotkey`: `{keys: "ctrl+s"|"cmd+s"|["ctrl","shift","t"], count?: 1}`
    - `ax_action`: `{ref, action_name?}` (mouse-free AT-SPI2 / Windows UIA / macOS `AXPress`)
    - `focus`: `{window_id?|app?|title?}`
    - `window`: `{window_action: "focus"|"minimize"|"maximize"|"unmaximize"|"close"|"move_resize", window_id?|app?|title?, x?, y?, w?, h?}`
    - `wait`: `{ms?|change?: true, area?, threshold_pct?, text?|app?, timeout_ms?}` (polls AX + local OCR when `text` is set; exits immediately on pixel change when `change: true`)
    - `screenshot`: `{scope?, area?, ref?, annotate?, grid?, max_width?, quality?}`
- `desktop_observe` (1-call state: windows + AX `[ref]` tree + automatic inline `OCR_TEXT` `[x,y]` labels on sparse-AX apps with zero image offload; falls back to compact `896px` JPEG only if OCR finds <5 words or `mode: "vision"` / `screenshot: true` is passed):
  - `{mode?: "hybrid"|"ax"|"ocr"|"vision"|"windows", background?: false, ocr?: false, find?, diff?: "auto"|true|false, screenshot?: "auto"|true|false, scope?: "screen"|"active", annotate?, grid?, max_elements?, max_width?: 896, quality?: 62}`
- `desktop_background` (zero-touch off-screen automation on Linux `Xvfb`, Windows `PrintWindow`+`PostMessageW`, macOS `CGWindowList`+`AXPress`/`CGEventPostToPid`):
  - `{action?: "status"|"start"|"attach"|"launch"|"stop", width?, height?, command?, window_id?, app?, title?, wait_app?, wait_ms?, timeout_ms?}`
  - Pass `background: true` on `desktop_observe`, `desktop_act`, or `desktop_screenshot` to run off-screen without touching the user's mouse, focus, or clipboard.
- `desktop_screenshot`: `{background?: false, scope?: "screen"|"active"|"window"|"area"|"ref", area?: [x,y,w,h], window_id?, app?, title?, ref?, pad?, annotate?, grid?, cursor?, diff_from_last?, max_width?, quality?, format?: "jpeg"|"png"}`
- `desktop_windows`: `{action?: "list"|"focus"|"minimize"|"maximize"|"unmaximize"|"close"|"move_resize"|"launch", background?: false, window_id?, app?, title?, x?, y?, w?, h?, command?, wait_app?, timeout_ms?}`
- `desktop_clipboard`: `{action?: "get"|"set"|"clear", text?}`
- `desktop_status`: `{}`

## Turn-Minimizing Execution Rules

- Do NOT spawn a subagent for 1 to 4 step desktop tasks; subagent startup adds 4 to 5 seconds of overhead. Execute directly via `call_mcp_tool` in 1 to 2 batched `desktop_act` calls. Only delegate long 6+ turn loops, using `invoke_subagent({TypeName: "self", Model: "flash", ...})`.
- Act first without a wasted initial screenshot whenever you already know the app, shortcut, URL, or button text. Chain the full flow in ONE `desktop_act` call (`launch` + `wait_and_click` + `click_and_type` + `keys` + `repeat`).
- Never insert blind 400 to 1000 ms `wait` sleeps between clicks. Use `wait_and_click(text=...)` for labeled modals or `wait(change=true, timeout_ms=800)` for icon-driven transitions.
- Avoid unnecessary `read_file` turns: `desktop_observe()` and `desktop_act(observe="auto")` return inline `[x,y]` `OCR_TEXT` labels on sparse-AX apps automatically AND save a ready-to-read `SCREENSHOT_PATH` grid JPEG in 1 ms. Use `OCR_TEXT` directly whenever labels exist; if you genuinely need a textless icon (such as a `+` symbol), call `read_file` directly on the reported `SCREENSHOT_PATH` instead of spending an extra `observe: "vision"` call.
- Pass `observe: "none"` on intermediate bursts where no visual/OCR feedback is needed.
- For purely in-browser web tasks in Brave, prefer `brave-browser_brave-browser` (CDP DOM/AX access). Use `computer-use` for native desktop apps, OS dialogs, window management, file pickers, IDEs, media tools, Windows/macOS apps, and background GUI workflows.
