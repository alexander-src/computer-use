---
name: computer-use
description: Fast native Linux desktop automation over MCP (direct KWin ScreenShot2 capture, kernel /dev/uinput absolute pointer and keyboard, AT-SPI2 semantic [ref] diffs, Set-of-Marks and 0..1000 grid overlays, window control, and atomic clipboard paste). Use when controlling native desktop apps, OS dialogs, system settings, multi-app GUI workflows, or taking annotated desktop screenshots.
---

# Computer Use Skill (Linux Fast Mode)

Operate the Linux desktop via MCP server `computer-use_computer-use` using `call_mcp_tool`.

## Core Workflow

1. **Observe once (`desktop_observe`)**:
   - Call `desktop_observe()` with default `mode: "hybrid"`.
   - On Qt/KDE/GTK apps, AT-SPI2 returns clickable `[ref]` IDs with exact screen centers and roles in ~5 ms, plus incremental `AX_DIFF` on follow-up turns.
   - On Electron, browsers, games, or canvas apps with fewer than 5 AT-SPI2 refs, `hybrid` mode automatically attaches a ~26 ms JPEG screenshot overlaid with a `0..1000` coordinate grid.

2. **Batch actions (`desktop_act`)**:
   - Chain `focus`, `click`, `type`, `key`, `scroll`, `drag`, `ax_action`, and `wait` in a single `desktop_act({steps: [...]})` call.
   - Targeting priority (most reliable first):
     1. `ax_action` with `ref` (mouse-free, instant)
     2. `click` with `ref` or `text` (AT-SPI2 exact center)
     3. Keyboard shortcuts (`key: "ctrl+s"`, `"alt+f4"`, `"super"`, `"tab"`)
     4. `nx, ny` (`0..1000`) read from the coordinate grid (`coord_scope: "screen"` for full screen, `"last_image"` for active-window or cropped screenshots)
     5. Exact pixel `x, y`
   - `desktop_act` automatically waits for frames to settle (`settle: "smart"`), computes `PIXEL_DIFF` (`changed_pct`, `changed_bbox`), and returns an `AX_DIFF` or post-action visual frame.

3. **Fast typing and terminal safety**:
   - `op: "type"` uses direct kernel `/dev/uinput` keystrokes for short ASCII strings (<= 24 chars) and atomic save/set/paste/restore clipboard injection for longer or Unicode strings.
   - Terminal emulators (`konsole`, `kitty`, `alacritty`, `wezterm`, `foot`, `ghostty`, `gnome-terminal`, etc.) are automatically detected and pasted via `ctrl+shift+v`.

4. **Precise inspection (`desktop_screenshot`)**:
   - Use `scope: "active"` or `scope: "area"` (`area: [x, y, w, h]`) or `scope: "ref"` to zoom into small text or dense toolbars at full resolution (`max_width: 1920` or `0`).
   - Use `observe: "crop_changed"` in `desktop_act` to return only the changed region after an action.

5. **Event-driven waiting**:
   - Never sleep blindly for long durations. Use `{op: "wait", change: true, area: [...], timeout_ms: 5000}` to wait for a UI transition or `{op: "wait", app: "dolphin"}` / `{op: "wait", text: "Finished"}`.

6. **Safety and context hygiene**:
   - Confirm with the user before destructive GUI actions (deleting files, submitting payments, changing system security settings, sending external messages).
   - Respect `takeover_detected: true` immediately if the user grabs the mouse mid-batch.
   - Delegate heavy multi-step GUI sessions to the `computer-use` subagent so screenshots stay out of the main context window.
