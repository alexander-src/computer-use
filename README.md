# computer-use

Fast native desktop automation MCP server and Antigravity plugin for Linux (Wayland and X11) and Windows 10/11: ~5 ms full-screen JPEG capture, smooth custom AI cursor with click ripples, independent user mouse, background mode, and semantic AT-SPI2 / Windows UI Automation `[ref]` trees.

Works with Antigravity, Claude Code, Codex CLI, Cursor, Gemini CLI, and any stdio MCP client.

## Paste This Into Your Agent

Copy and paste this into Antigravity, Claude Code, Codex, or Cursor to install and verify automatically:

```text
Install https://github.com/alexander-src/computer-use by following https://raw.githubusercontent.com/alexander-src/computer-use/main/INSTALL.md and verify with --selftest.
```

## Manual Quick Start

Linux (Arch, CachyOS, Ubuntu, Fedora, Wayland or X11):

```bash
git clone https://github.com/alexander-src/computer-use.git
cd computer-use
./install.sh
```

Windows 10 / 11 (PowerShell, zero external binaries required):

```powershell
git clone https://github.com/alexander-src/computer-use.git
cd computer-use
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

Both installers set up the Antigravity plugin bundle (`~/.gemini/config/plugins/computer-use/`) and run `--selftest`. Restart Antigravity afterward so it loads the new plugin. See [INSTALL.md](INSTALL.md) for Claude Code, Codex CLI, Cursor, and Gemini CLI one-liners.

## Why It Is Fast (Measured Latency)

Measured on Arch Linux with KDE Plasma 6 (Wayland, `1920x1080`) via `python3 scripts/computer_use_mcp.py --bench` (median of 5 runs):

| Operation | Backend | Median | Min |
| --- | --- | ---: | ---: |
| Window list + active window + cursor | `org.kde.KWin /Scripting` DBus | 1.27 ms | 1.23 ms |
| Clipboard read | `org.kde.klipper` DBus | 0.21 ms | 0.19 ms |
| Full `1920x1080` raw screen capture | `org.kde.KWin.ScreenShot2` FD pipe | 3.70 ms | 3.28 ms |
| `400x300` region capture | `org.kde.KWin.ScreenShot2` FD pipe | 0.28 ms | 0.26 ms |
| `1280x720` JPEG encode (`q=75`) | Cairo bilinear + AVX2 `libturbojpeg` | 1.53 ms | 1.52 ms |
| `1280x720` JPEG + `0..1000` grid + cursor | Cairo zero-swap RGBX + `libturbojpeg` | 1.69 ms | 1.66 ms |
| `1920x1080` pixel diff | `numpy` | 1.48 ms | 1.46 ms |
| Private background capture (`800x600`) | Authenticated `Xvfb` + direct `libX11` | 1.91 ms | 1.85 ms |
| Absolute pointer move (`EV_ABS` 0..65535) | Persistent `/dev/uinput` | 0.01 ms | 0.01 ms |

Full `1920x1080 -> 1280x720` screenshot capture plus JPEG encoding runs in ~5.2 ms (~5.4 ms with coordinate grid and cursor ring), roughly 28x faster than spawning `spectacle` (~150 ms).

## Independent AI Cursor and Background Mode

### Smooth Custom AI Cursor (Keep Your Own Mouse)

While the agent moves to click a button, a click-through cyan AI pointer glides smoothly along an ease-in-out curve and plays an expanding click ripple (cyan for left click, amber for right click, double pulse for double click) using `zwlr_layer_shell_v1` on Wayland (KDE Plasma 6, Hyprland, Sway, River, COSMIC) or `WS_EX_LAYERED` + `WDA_EXCLUDEFROMCAPTURE` on Windows. Moving your physical mouse never aborts active batches by default; after each click, your physical cursor is restored immediately. Preview it non-destructively with `python3 scripts/computer_use_mcp.py --demo-cursor`.

### Background Computer Use (Linux and Windows)

Pass `background: true` (after `desktop_background`) to automate apps without moving your mouse, stealing focus, or touching your clipboard. On Linux, `desktop_background(action="start")` launches a private `MIT-MAGIC-COOKIE-1` authenticated `Xvfb` virtual display (`XGetImage` + `XTest`) with its own screen buffer and cursor. On Windows, `desktop_background(action="attach", app="...")` captures the target window with `PrintWindow(PW_RENDERFULLCONTENT)` and sends child-HWND `PostMessageW` mouse and keyboard events directly to it while it stays behind your active windows.

## Cross-Platform Architecture

| Subsystem | Linux (Wayland / X11) | Windows 10 / 11 |
| --- | --- | --- |
| Screen capture | `KWin.ScreenShot2` FD pipe, `grim`, `maim`, `scrot`, `spectacle` | Per-Monitor DPI v2 `BitBlt`, `PrintWindow` |
| Image encode & overlays | Cairo + AVX2 `libturbojpeg`, `GdkPixbuf` (`glycin` compatible) | Built-in GDI+ exact-length `IStream`, `turbojpeg.dll`, 5x7 bitmap badges |
| Pointer & keyboard | Persistent `/dev/uinput` (US/UK layout aware + atomic paste), `ydotool`, `xdotool`, `wtype` | `SendInput` (`MOUSEEVENTF_VIRTUALDESK`, `KEYEVENTF_EXTENDEDKEY`, `KEYEVENTF_UNICODE` UTF-16) |
| Window management | KWin Scripting DBus, Hyprland, Sway, X11 (`wmctrl`/`xdotool`) | `EnumWindows` (`DWMWA_CLOAKED` filtered), `DWMWA_EXTENDED_FRAME_BOUNDS`, `AttachThreadInput` focus |
| Accessibility tree | AT-SPI2 (Wayland global-coord calibrated, stable `[ref]` diffs, `Action`) | Native MTA `IUIAutomation` COM (`ControlViewWalker`, `[ref]` diffs, `Invoke`/`Toggle`) |
| Clipboard | Klipper DBus, `wl-clipboard`, `xclip`, `xsel` | Win32 `CF_UNICODETEXT` with lock retry |
| Custom AI cursor | Pure-Python Wayland `zwlr_layer_shell_v1` + `wl_shm` + Cairo | Win32 `WS_EX_LAYERED` + `WS_EX_TRANSPARENT` + `WDA_EXCLUDEFROMCAPTURE` |
| Background mode | Authenticated private `Xvfb` + `libX11`/`libXtst` | `PrintWindow` + child-HWND `PostMessageW` |

## MCP Tools (7)

| Tool | Purpose |
| --- | --- |
| `desktop_observe` | 1-call window list + active app AT-SPI2/UIA `[ref]` tree (`AX_DIFF` on repeat calls) + automatic ~5 ms JPEG screenshot with `0..1000` grid when AX has fewer than 5 elements. |
| `desktop_act` | Execute batched steps (`click`, `move`/`hover`, `drag`, `scroll`, `type`, `key`/`hotkey`, `ax_action`, `focus`, `window`, `launch`, `wait`, `screenshot`) with smooth AI cursor glide, click ripples, smart settle, and pixel diff. Supports `background: true`. |
| `desktop_background` | Start, attach, launch into, inspect, or stop an isolated background session (`Xvfb` on Linux, `PrintWindow` + `PostMessageW` on Windows). |
| `desktop_screenshot` | Capture `screen`, `active`, `window`, `area`, `ref`, or `background` with optional Set-of-Marks `[ref]` badges, `0..1000` coordinate grid, and `diff_from_last`. |
| `desktop_windows` | List, focus, minimize, maximize, unmaximize, move/resize, close, or launch desktop windows. |
| `desktop_clipboard` | Read, write, or clear the system clipboard. |
| `desktop_status` | Report OS, active backends, keyboard layout, overlay status, background status, and virtual multi-monitor geometry. |

## Testing and Diagnostics

```bash
# Non-destructive MCP protocol and live desktop backend check
python3 scripts/computer_use_mcp.py --selftest

# Live subsystem latency benchmark
python3 scripts/computer_use_mcp.py --bench

# Visual non-destructive preview of the smooth cyan AI cursor + click ripples
python3 scripts/computer_use_mcp.py --demo-cursor

# Full unit, TurboJPEG/Cairo, Xvfb background, Win32/COM, and stdio JSON-RPC suite
python3 -m unittest discover -s tests -v
```

Set `COMPUTER_USE_AUDIT_LOG=/path/to/audit.jsonl` to log every tool call and latency to JSONL. Set `COMPUTER_USE_OVERLAY=0` or `COMPUTER_USE_SMOOTH_CURSOR=0` to disable the custom cursor overlay or smooth glide. Set `COMPUTER_USE_ENABLE_SCREENREADER=1` on Linux only if you want `org.a11y.Status.ScreenReaderEnabled` toggled on for Chromium/Electron AT-SPI2 trees (restored on exit).

## Limitations

- Supports Linux (Wayland and X11) and Windows 10/11; macOS is not implemented yet.
- Windows background mode (`PrintWindow` + `PostMessageW`) requires the target window to be unminimized (it can sit behind other windows) because Windows DWM stops rendering minimized windows; some apps that poll raw hardware keyboard state ignore posted modifier hotkeys like `Ctrl+S` in background mode.
- On Linux Wayland, the click-through AI cursor overlay requires compositor support for `zwlr_layer_shell_v1` (supported on KDE Plasma 6, Hyprland, Sway, River, and COSMIC; GNOME Mutter does not expose `wlr-layer-shell`, so actions still work without the floating cyan cursor).
- On Windows, clicking inside elevated (Administrator / UAC) windows requires running the MCP server with matching elevation due to Windows UIPI isolation.
