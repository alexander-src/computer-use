---
name: computer-use
description: Superhuman-speed native Linux (Wayland/X11), Windows 10/11, and macOS desktop automation over MCP (4.5 ms capture+JPEG, 0.008 ms non-blocking 120Hz cyan AI cursor with click ripples, independent user mouse, 8-thread parallel local OCR, closed-loop compound ops wait_and_click/click_and_type/open_url/keys/repeat, background mode on Linux/Windows/macOS, and AT-SPI2/UIA/AXUIElement semantic [ref] diffs). Use when controlling native desktop apps, OS dialogs, multi-app GUI workflows, background GUI automation, or taking annotated desktop screenshots.
---

# Computer Use Skill (Linux, Windows & macOS Superhuman-Speed Mode)

Control Linux, Windows, and macOS desktops faster than a human operator via MCP server `computer-use_computer-use` using `call_mcp_tool`.

## Core Workflow

1. Act directly when you know the app, URL, shortcut, or button text (skip wasted initial screenshots):
   - Chain the entire sequence in ONE `desktop_act({observe: "none", steps: [...]})` call.
   - Use compound ops so polling, typing, and clicking execute locally in milliseconds instead of waiting for cloud LLM turns:
     - `open_url`: `{op: "open_url", app: "brave", url: "https://...", new_tab: true}`
     - `wait_and_click`: `{op: "wait_and_click", text: "Save Changes", timeout_ms: 2500, type_text?: "...", submit?: true}`
     - `click_and_type`: `{op: "click_and_type", target: "Search", text: "query", submit: true}`
     - `keys`: `{op: "keys", sequence: ["ctrl+a", "ctrl+c", "alt+tab", "ctrl+v"], delay_ms: 10}`
     - `repeat`: `{op: "repeat", count: 5, steps: [{op: "key", keys: "down"}]}`
   - Any `text`, `target`, or `text_target` automatically resolves via AX/UIA/AXUIElement first, then 8-thread in-process parallel OCR (`libtesseract`, ~40-82 ms) on Chromium, Electron, VS Code, Discord, Spotify, and custom-drawn UIs with zero cloud round-trips.

2. Observe cleanly when screen layout is unknown (`desktop_observe`):
   - Call `desktop_observe({mode: "ocr"})` when you only need clickable text coordinates (`[x,y] "Button"` with zero image tokens).
   - Call `desktop_observe()` (`mode: "hybrid"`) for open windows, `[ref]` IDs (AT-SPI2 on Linux, UIA on Windows, AXUIElement on macOS) with incremental `AX_DIFF`, and automatic 47 KB `896px` JPEG + `0..1000` grid fallback when AX has fewer than 5 elements.

3. Independent 120Hz AI cursor and zero-waste execution:
   - Moving the user's mouse does not abort by default (`independent_cursor: true`). The cyan AI pointer glides asynchronously at 120Hz on the overlay thread (0.008 ms call time, zero blocking sleep on the action path), fires click ripples, and restores the user's physical cursor immediately.
   - Pass `observe: "none"` on intermediate bursts (`0.05 ms` overhead) and `observe: "auto"` only on the final verification step (`7.4 ms`, `43 KB` JPEG). Pass `verify_change: true` or `observe: "crop_changed"` only when pixel-diff confirmation is needed.

4. Background mode (`desktop_background` + `background: true`):
   - Linux: `desktop_background({action: "start", command: "..."})` starts an authenticated private `Xvfb` virtual desktop.
   - Windows: `desktop_background({action: "attach", app: "..."})` (or `action: "launch"`) captures via `PrintWindow` and sends child-HWND `PostMessageW` events.
   - macOS: `desktop_background({action: "attach", app: "..."})` (or `action: "launch"` via `open -g -a`) captures occluded windows via `CGWindowList` and delivers input via `AXPress` / `CGEventPostToPid`.
   - Pass `background: true` on `desktop_observe`, `desktop_act`, and `desktop_screenshot` (including OCR-backed `click(text=...)` and `wait_and_click`!).

5. Safety and Flash delegation:
   - Confirm with the user before destructive GUI actions (deleting files, submitting payments, changing security settings, sending external messages).
   - Delegate visual GUI loops (2+ turns) immediately to the `computer-use` (`flash`) subagent via `invoke_subagent` for minimal cloud turn latency.
