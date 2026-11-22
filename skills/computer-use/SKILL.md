---
name: computer-use
description: Fast native Linux (Wayland/X11), Windows 10/11, and macOS desktop automation over MCP (~5 ms KWin+TurboJPEG, Win32 GDI+, and macOS CoreGraphics/ImageIO capture, snappy smooth custom AI cursor with click ripples, independent user mouse, background mode on Linux/Windows/macOS, AT-SPI2/UIA/AXUIElement semantic [ref] diffs, Set-of-Marks and 0..1000 grid). Use when controlling native desktop apps, OS dialogs, multi-app GUI workflows, background GUI automation, or taking annotated desktop screenshots.
---

# Computer Use Skill (Linux, Windows & macOS Human-Speed Mode)

Operate Linux, Windows, and macOS desktops at human speed via MCP server `computer-use_computer-use` using `call_mcp_tool`.

## Core Workflow

1. **Observe once (`desktop_observe`)**:
   - Call `desktop_observe()` with default `mode: "hybrid"`.
   - Returns open windows, active window, cursor position, and semantic `[ref]` IDs (AT-SPI2 on Linux, UIA on Windows, AXUIElement on macOS) with incremental `AX_DIFF` on repeat calls.
   - When an app exposes fewer than 5 accessibility elements, `hybrid` mode caches the sparse status for 4s and attaches a ~5 ms 1024px JPEG screenshot overlaid with a `0..1000` coordinate grid.

2. **Chain full human workflows in one `desktop_act` call**:
   - Never burn separate LLM turns for click, then type, then press Enter. Combine `focus`, `hotkey`, `click`, `type(clear=true, submit=true)`, `scroll`, `drag`, `ax_action`, and `wait(change=true)` inside ONE `desktop_act({steps: [...]})` call.
   - Pass `fast: true` or `observe: "none"` for instant multi-step bursts (0 ms cursor snap, zero settle wait), and `observe: "crop_changed"` when verifying small UI changes.
   - Targeting priority:
     1. Keyboard shortcuts (`ctrl+l`/`cmd+l`, `ctrl+t`/`cmd+t`, `ctrl+s`/`cmd+s`, `tab`, `enter`, `esc`)
     2. `ax_action` with `ref` (mouse-free invoke/press with visual cursor glide + ripple)
     3. `click` with `ref` or `text`
     4. `nx, ny` (`0..1000`) read from the coordinate grid (`coord_scope: "screen"` or `"last_image"`)
     5. Exact pixel `x, y`
   - Moving the user's mouse does not abort by default (`independent_cursor: true`): the custom cyan AI cursor glides smoothly (20-55 ms), clicks with an expanding ripple, and restores the user's physical cursor immediately.

3. **Background mode (`desktop_background` + `background: true`)**:
   - Linux: call `desktop_background({action: "start", command: "..."})` to spin up a private `Xvfb` virtual desktop with its own mouse, keyboard, and screen buffer.
   - Windows: call `desktop_background({action: "attach", app: "..."})` (or `action: "launch"`) to automate a background window via `PrintWindow` + `PostMessageW`.
   - macOS: call `desktop_background({action: "attach", app: "..."})` (or `action: "launch"`, which runs `open -g -a`) to capture occluded windows via `CGWindowList` and send input via `AXPress` / `CGEventPostToPid`.
   - Then pass `background: true` to `desktop_observe`, `desktop_act`, and `desktop_screenshot` to operate off-screen without touching the user's mouse, focus, or clipboard.

4. **Layout-aware typing and OS shortcuts**:
   - Linux auto-detects `us`, `gb`, or international keymaps and uses async save/paste/restore clipboard injection (`ctrl+shift+v` in terminals) for long/Unicode text.
   - Windows (`KEYEVENTF_UNICODE`) and macOS (`CGEventKeyboardSetUnicodeString`) inject UTF-16 directly independent of keyboard layout, and automatically use `cmd+a`/`cmd+v` on macOS and `ctrl+v`/`ctrl+shift+v` on Windows.

5. **Safety and speed delegation**:
   - Confirm with the user before destructive GUI actions (deleting files, submitting payments, changing security settings, sending external messages).
   - Delegate multi-step GUI loops (3+ visual turns) to the `computer-use` (`flash`) subagent for faster turn latency and clean context isolation.
