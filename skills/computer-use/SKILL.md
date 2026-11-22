---
name: computer-use
description: Fast native Linux (Wayland/X11), Windows 10/11, and macOS desktop automation over MCP (4.5 ms capture+JPEG, non-blocking 120Hz cyan AI cursor with click ripples, independent user mouse, 8-thread parallel local OCR with inline [x,y] labels, compound ops launch/open_url/wait_and_click/click_and_type/keys/repeat, background mode on Linux/Windows/macOS, and AT-SPI2/UIA/AXUIElement [ref] diffs). Use when controlling native desktop apps, OS dialogs, multi-app GUI workflows, background GUI automation, or taking annotated desktop screenshots.
---

# Computer Use Skill (Linux, Windows & macOS Fast Mode)

Control Linux, Windows, and macOS desktops with minimal LLM round-trips via MCP server `computer-use_computer-use` using `call_mcp_tool`.

## Core Workflow

1. Act directly in ONE batched `desktop_act` call when the app, URL, shortcut, or button text is known:
   - Do not burn 4 to 5 seconds spawning a subagent for 1 to 4 step tasks; call `desktop_act` directly. Only delegate long 6+ turn loops via `invoke_subagent({TypeName: "self", Model: "flash", ...})`.
   - Use compound ops so polling, typing, and clicking execute locally instead of waiting for cloud LLM turns:
     - `launch`: `{op: "launch", command: "discord"}` (focuses an existing matching window in ~1.5 ms, or launches and waits for the matching window)
     - `open_url`: `{op: "open_url", app: "brave", url: "https://...", new_tab: true}`
     - `wait_and_click`: `{op: "wait_and_click", text: "Create My Own", timeout_ms: 2500, type_text?: "...", submit?: true}`
     - `click_and_type`: `{op: "click_and_type", target: "Server Name", text: "My Server", clear: true, submit: true}`
     - `keys`: `{op: "keys", sequence: ["ctrl+a", "ctrl+c", "alt+tab", "ctrl+v"], delay_ms: 10}`
     - `repeat`: `{op: "repeat", count: 5, steps: [{op: "key", keys: "down"}]}`
   - Never insert blind 400 to 1000 ms sleeps between steps. Use `wait_and_click(text=...)` for text modals or `wait(change=true, timeout_ms=800)` so execution resumes the millisecond pixels change.

2. Avoid wasted double-turn screenshot reads (`desktop_observe` and `observe: "auto"`):
   - On sparse-AX apps (Discord, Brave, Chrome, Electron, VS Code, Spotify, Telegram), `desktop_observe()` and `desktop_act(observe="auto")` automatically run 8-thread local OCR (~40-82 ms) and return inline `[x,y] "Label"` coordinates directly in the text response without offloading an image to disk.
   - Only pass `mode: "vision"` or `screenshot: true` when you need to find a textless icon (such as a `+` symbol) or inspect graphics.
   - Pass `observe: "none"` on predictable intermediate bursts (`0.05 ms` overhead).

3. Independent 120Hz AI cursor and background mode:
   - Moving the user's mouse does not abort by default (`independent_cursor: true`). The cyan AI pointer glides asynchronously at 120Hz on the overlay thread (`0.008 ms` call time), fires click ripples, and restores the user's physical cursor immediately.
   - For off-screen automation, call `desktop_background` (`start` on Linux `Xvfb`, `attach`/`launch` on Windows `PrintWindow`+`PostMessageW` or macOS `CGWindowList`+`AXPress`/`CGEventPostToPid`) and pass `background: true` on `desktop_observe`, `desktop_act`, and `desktop_screenshot`.

4. Safety:
   - Confirm with the user before destructive GUI actions (deleting files, submitting payments, changing security settings, sending external messages).
