---
name: computer-use
description: Ultra-fast native Linux (Wayland/X11) and Windows 10/11 desktop automation over MCP (~5 ms KWin+TurboJPEG and Win32 GDI+ capture, smooth custom AI cursor with click ripples, independent user mouse, background mode via Xvfb or Win32 PrintWindow/PostMessage, AT-SPI2/UIA semantic [ref] diffs, Set-of-Marks and 0..1000 grid). Use when controlling native desktop apps, OS dialogs, multi-app GUI workflows, background GUI automation, or taking annotated desktop screenshots.
---

# Computer Use Skill (Linux & Windows Fast Mode)

Operate Linux and Windows desktops via MCP server `computer-use_computer-use` using `call_mcp_tool`.

## Core Workflow

1. **Observe once (`desktop_observe`)**:
   - Call `desktop_observe()` with default `mode: "hybrid"`.
   - Returns open windows, active window, cursor position, and AT-SPI2 (Linux) or Windows UI Automation (Windows) `[ref]` IDs with incremental `AX_DIFF` on repeat calls.
   - When an app exposes fewer than 5 accessibility elements, `hybrid` mode automatically attaches a ~5 ms JPEG screenshot overlaid with a `0..1000` coordinate grid.

2. **Batch actions (`desktop_act`)**:
   - Chain `focus`, `click`, `type`, `key`, `scroll`, `drag`, `ax_action`, and `wait` in a single `desktop_act({steps: [...]})` call.
   - Targeting priority:
     1. `ax_action` with `ref` (mouse-free invoke/toggle, glides the custom AI cursor visually with a ripple)
     2. `click` with `ref` or `text`
     3. Keyboard shortcuts (`key: "ctrl+s"`, `"alt+f4"`, `"super"`, `"tab"`)
     4. `nx, ny` (`0..1000`) read from the coordinate grid (`coord_scope: "screen"` or `"last_image"`)
     5. Exact pixel `x, y`
   - Moving the user's mouse does not abort by default (`independent_cursor: true`): the custom cyan AI cursor glides smoothly to the target, clicks with an expanding ripple, and restores the user's physical cursor immediately.

3. **Background mode (`desktop_background` + `background: true`)**:
   - **Linux**: call `desktop_background({action: "start", command: "..."})` to spin up an isolated invisible `Xvfb` virtual desktop with its own mouse, keyboard, and screen buffer, then pass `background: true` to `desktop_observe`, `desktop_act`, and `desktop_screenshot`.
   - **Windows**: call `desktop_background({action: "attach", app: "..."})` (or `action: "launch"`) to target a background window via `PrintWindow` + `PostMessageW` without moving the user's cursor or stealing focus.

4. **Layout-aware typing and terminal safety**:
   - On Linux, active keyboard layout (`us`, `gb`, or international) is auto-detected; layout-sensitive symbols and long/Unicode strings use atomic save/paste/restore clipboard injection (`ctrl+shift+v` in terminals).
   - On Windows, `KEYEVENTF_UNICODE` injects UTF-16-LE directly independent of keyboard layout.

5. **Safety and context hygiene**:
   - Confirm with the user before destructive GUI actions (deleting files, submitting payments, changing system security settings, sending external messages).
   - Delegate heavy multi-step GUI sessions to the `computer-use` subagent so screenshots stay out of the main context window.
