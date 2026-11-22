# computer-use

Fast native desktop automation MCP server and Antigravity plugin for Linux (Wayland and X11), Windows 10/11, and macOS (Apple Silicon and Intel): 4.5 ms full-screen capture + JPEG, non-blocking 120Hz cyan AI cursor with click ripples, independent user mouse, 8-thread parallel local OCR with inline `[x,y]` labels, closed-loop compound ops, background mode, and semantic AT-SPI2 / Windows UIA / macOS `AXUIElement` `[ref]` trees.

Works with Antigravity, Claude Code, Codex CLI, Cursor, Gemini CLI, and any stdio MCP client.

## Paste This Into Your Agent

Copy and paste this into Antigravity, Claude Code, Codex, or Cursor to install and verify automatically:

```text
Install https://github.com/alexander-src/computer-use by following https://raw.githubusercontent.com/alexander-src/computer-use/main/INSTALL.md and verify with --selftest.
```

## Manual Quick Start

Linux (Arch, CachyOS, Ubuntu, Fedora) and macOS (Apple Silicon / Intel):

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

Both installers set up the Antigravity plugin bundle (`~/.gemini/config/plugins/computer-use/`) and run `--selftest`. Restart Antigravity once after initial install so it launches the new MCP server (from v2.3.0 onward, the server also hot-reloads `scripts/*.py` in-process whenever updated on disk). See [INSTALL.md](INSTALL.md) for macOS permission setup and Claude Code, Codex CLI, Cursor, and Gemini CLI one-liners.

## Reducing Both Local Overhead and LLM Round-Trips

End-to-end speed in a desktop agent depends mostly on how many cloud LLM turns a task takes, not just raw millisecond timings. `computer-use` v2.5 cuts both:

- Inline pre-warmed `tessdata_fast` OCR (`OMP_THREAD_LIMIT=1` set before `numpy`/`libgomp` load to prevent OpenMP thread thrashing) replaces double-turn image offloads: on apps with empty accessibility trees (Discord, Brave, Chrome, Electron, VS Code, Spotify), `desktop_observe()` and `desktop_act(observe="auto")` run 8-thread in-process `libtesseract` with smooth local dark/white/colored-button polarity normalization and return clickable `[x,y] "Label"` coordinates inline plus a 1 ms `SCREENSHOT_PATH` grid JPEG for textless icons.
- Self-healing closed-loop compound ops collapse multi-dialog flows into one call: `launch` (focuses an already-open window in ~1.5 ms or waits for the matching window), `open_url`, `wait_and_click` (diffs against the pre-click frame, OCRs only the newly opened modal in ~21 to 32 ms, auto-retries a swallowed preceding click once after 420 ms if nothing changed, supports `offset_x`/`offset_y`), `click_and_type`, `keys`, `repeat`, `wait(gone="...")` (exits the millisecond a submitted modal closes), and `wait(change=true)`.
- Pre-warmed `/dev/uinput` + non-blocking 120Hz cursor glide + smart post-submit settle: eliminates the 200 ms first-input uinput setup delay and 400 ms first-OCR init delay at server boot, animates the cyan AI cursor on a background thread (`0.008 ms` on the action path), and waits for dialog transitions after `wait_and_click`/`submit` so post-action OCR sees the updated screen instead of a stale closing modal.

Measured live on Arch Linux with KDE Plasma 6 (Wayland, `1920x1080`) via `python3 scripts/computer_use_mcp.py --bench` and tool profiling:

| Operation | Backend | Measured | Notes |
| --- | --- | ---: | --- |
| `desktop_act(observe="none")` overhead | Direct `/dev/uinput` | 0.05 ms | Zero capture, zero settle |
| Smooth cyan AI cursor glide (`55 ms` curve) | 120Hz async overlay thread (`glide_to`) | 0.008 ms | Animates off the action path |
| `desktop_act(observe="auto")` (visual) | `KWin.ScreenShot2` + `896px` `libturbojpeg` | 7.37 ms | 43 KB base64 payload |
| `desktop_observe(mode="hybrid")` (visual) | KWin DBus + sparse AX cache + `896px` grid | 8.45 ms | 47 KB base64 payload |
| `400x300` region capture | `org.kde.KWin.ScreenShot2` FD pipe | 0.25 ms | Sub-millisecond crop |
| Full `1920x1080` capture + `896px` JPEG (`q=62`) | `KWin.ScreenShot2` + Cairo + AVX2 `libturbojpeg` | 4.54 ms | 41 KB (50 KB with grid) |
| Modal popup `450x550` local OCR (`wait_and_click`) | Pre-warmed 8-thread `tessdata_fast` + diff crop | 21 to 32 ms | Mixed dark/light tile normalization |
| Dense `1493x888` app window local OCR (326 words) | Pre-warmed 8-thread `tessdata_fast` | 122.7 ms | Down from 790 ms on distro `tessdata_best` |
| Window list + active window + cursor | `org.kde.KWin /Scripting` DBus | 1.23 ms | 1.15 ms min |
| Clipboard read | `org.kde.klipper` DBus | 0.03 ms | Async restore saves ~110 ms/paste |
| Private background capture (`800x600`) | Authenticated `Xvfb` + direct `libX11` | 1.91 ms | Zero interference with live session |

## Independent AI Cursor and Background Mode

### Non-Blocking 120Hz Custom AI Cursor (Keep Your Own Mouse)

While the agent clicks a target, a click-through cyan AI pointer glides along an ease-in-out curve at 120Hz on the overlay thread (`0.008 ms` call time, no blocking sleep on the action path) and plays expanding click ripples using `zwlr_layer_shell_v1` on Linux Wayland, `WS_EX_LAYERED` + `WDA_EXCLUDEFROMCAPTURE` on Windows, or a dedicated main-thread AppKit `NSWindow` (`NSWindowSharingNone`) on macOS. Moving your physical mouse never aborts active batches by default; your physical cursor is restored immediately after each click. Preview it non-destructively with `python3 scripts/computer_use_mcp.py --demo-cursor`.

### Background Computer Use (Linux, Windows, and macOS)

Pass `background: true` (after `desktop_background`) to automate apps without moving your mouse, stealing focus, or touching your clipboard:

- Linux: `desktop_background(action="start")` launches a private `MIT-MAGIC-COOKIE-1` authenticated `Xvfb` virtual display (`XGetImage` + `XTest`) with its own screen buffer and cursor.
- Windows: `desktop_background(action="attach", app="...")` captures the target window with `PrintWindow(PW_RENDERFULLCONTENT)` and sends child-HWND `PostMessageW` mouse and keyboard events directly to it while it stays behind your active windows.
- macOS: `desktop_background(action="attach", app="...")` (or `action="launch"` via `open -g -a`) captures the target window even while occluded (`CGWindowListCreateImage` / `screencapture -l`), activates native controls via `AXPress`, and delivers pointer/scroll/Unicode keyboard events directly via `CGEventPostToPid`.

## Cross-Platform Architecture

| Subsystem | Linux (Wayland / X11) | Windows 10 / 11 | macOS 13+ (arm64 / x86_64) |
| --- | --- | --- | --- |
| Screen capture | `KWin.ScreenShot2` FD pipe, `grim`, `maim`, `scrot`, `spectacle` | Per-Monitor DPI v2 `BitBlt`, `PrintWindow` | CoreGraphics `CGWindowListCreateImage` + `/usr/sbin/screencapture` (ScreenCaptureKit) fallback, Retina HiDPI |
| Image encode & overlays | Cairo + AVX2 `libturbojpeg`, `GdkPixbuf` (`glycin` compatible) | Built-in GDI+ exact-length `IStream`, `turbojpeg.dll`, 5x7 bitmap badges | `CGBitmapContext` bilinear scale + Apple `ImageIO` or Homebrew `libturbojpeg`, 5x7 bitmap badges |
| Pointer & keyboard | Persistent `/dev/uinput` (US/UK layout aware + async atomic paste), `ydotool`, `xdotool`, `wtype` | `SendInput` (`MOUSEEVENTF_VIRTUALDESK`, `KEYEVENTF_EXTENDEDKEY`, `KEYEVENTF_UNICODE` UTF-16) | `CGEvent` (`kCGMouseEventClickState`, `CGEventCreateScrollWheelEvent2`, `CGEventKeyboardSetUnicodeString` UTF-16) |
| Window management | KWin Scripting DBus, Hyprland, Sway, X11 (`wmctrl`/`xdotool`) | `EnumWindows` (`DWMWA_CLOAKED` filtered), `DWMWA_EXTENDED_FRAME_BOUNDS`, `AttachThreadInput` focus | `CGWindowListCopyWindowInfo`, `CGGetActiveDisplayList`, `NSWorkspace`/`NSRunningApplication`, `AXUIElement` |
| Accessibility & local OCR | AT-SPI2 (Wayland calibrated, 4s sparse cache) + 8-thread `libtesseract` OCR | Native MTA `IUIAutomation` COM (`[ref]` diffs, `Invoke`) + optional `libtesseract` OCR | Native `AXUIElement` (`0.35s` timeout, `[ref]` diffs, `AXPress`) + optional Homebrew `libtesseract` OCR |
| Clipboard | Klipper DBus, `wl-clipboard`, `xclip`, `xsel` | Win32 `CF_UNICODETEXT` with lock retry | UTF-8 `pbcopy` / `pbpaste` |
| Custom AI cursor | 120Hz Wayland `zwlr_layer_shell_v1` + `wl_shm` + Cairo | 120Hz Win32 `WS_EX_LAYERED` + `WS_EX_TRANSPARENT` + `WDA_EXCLUDEFROMCAPTURE` | 120Hz main-thread AppKit `NSWindow` helper (`NSWindowSharingNone`) |
| Background mode | Authenticated private `Xvfb` + `libX11`/`libXtst` | `PrintWindow` + child-HWND `PostMessageW` | Occluded `CGWindowList`/`screencapture -l` + `AXPress` + `CGEventPostToPid` |

## MCP Tools (7)

| Tool | Purpose |
| --- | --- |
| `desktop_observe` | 1-call window list + active app AT-SPI2/UIA/`AXUIElement` `[ref]` tree (`AX_DIFF` on repeat calls) + inline `[x,y]` `OCR_TEXT` on sparse-AX apps (no image offload) + `896px` JPEG fallback when OCR has <5 words or `mode: "vision"` is requested. |
| `desktop_act` | Execute batched steps (`launch`, `open_url`, `wait_and_click`, `click_and_type`, `keys`, `repeat`, `click`, `move`/`hover`, `drag`, `scroll`, `type`, `key`/`hotkey`, `ax_action`, `focus`, `window`, `wait`, `screenshot`) with non-blocking 120Hz cyan cursor glide, local AX+OCR text targeting, and `observe: "none"` fast bursts. Supports `background: true`. |
| `desktop_background` | Start, attach, launch into, inspect, or stop a background session (`Xvfb` on Linux, `PrintWindow` + `PostMessageW` on Windows, `CGWindowList` + `AXPress`/`CGEventPostToPid` on macOS). |
| `desktop_screenshot` | Capture `screen`, `active`, `window`, `area`, `ref`, or `background` with optional Set-of-Marks `[ref]` badges, `0..1000` coordinate grid, and `diff_from_last`. |
| `desktop_windows` | List, focus, minimize, maximize, unmaximize, move/resize, close, or launch desktop windows. |
| `desktop_clipboard` | Read, write, or clear the system clipboard. |
| `desktop_status` | Report OS, active backends (including OCR), macOS TCC permission status, overlay status, background status, and multi-monitor geometry. |

## Testing and Diagnostics

```bash
# Non-destructive MCP protocol and live desktop backend check
python3 scripts/computer_use_mcp.py --selftest

# Live subsystem latency benchmark
python3 scripts/computer_use_mcp.py --bench

# Visual non-destructive preview of the smooth cyan AI cursor + click ripples
python3 scripts/computer_use_mcp.py --demo-cursor

# Full Linux, Windows, and macOS unit + integration test suite
python3 -m unittest discover -s tests -v
```

Set `COMPUTER_USE_AUDIT_LOG=/path/to/audit.jsonl` to log every tool call and latency to JSONL. Set `COMPUTER_USE_OVERLAY=0` or `COMPUTER_USE_SMOOTH_CURSOR=0` to disable the custom cursor overlay or smooth glide. Set `COMPUTER_USE_HOT_RELOAD=0` to disable automatic in-process script reload on disk updates. Set `COMPUTER_USE_ENABLE_SCREENREADER=1` on Linux only if you want `org.a11y.Status.ScreenReaderEnabled` toggled on for Chromium/Electron AT-SPI2 trees (restored on exit).

## Limitations

- Total wall-clock time on multi-step tasks still depends on cloud LLM latency (typically 1 to 3 seconds per model turn). Inline local OCR and compound ops reduce the number of turns, but a task that needs 3 model turns will still spend most of its wall-clock time waiting on the model rather than local input/capture.
- On macOS, clicking, typing, and `AXUIElement` trees require Accessibility permission, and capturing other apps' windows requires Screen Recording permission (System Settings -> Privacy & Security).
- Local OCR text targeting uses system `libtesseract` when installed (`tesseract` + `tesseract-data-eng` on Linux, `brew install tesseract` on macOS, or Tesseract-OCR on Windows). Without it, accessibility trees and `0..1000` grid coordinates still work normally.
- Windows (`PrintWindow` + `PostMessageW`) and macOS (`CGWindowList` + `CGEventPostToPid`) background modes require the target window to be unminimized (it can sit behind other windows); some GPU-composited or raw-input apps ignore background posted events unless focused.
- On Linux Wayland, the click-through AI cursor overlay requires compositor support for `zwlr_layer_shell_v1` (supported on KDE Plasma 6, Hyprland, Sway, River, and COSMIC; GNOME Mutter does not expose `wlr-layer-shell`, so actions still work without the floating cyan cursor).
- On Windows, clicking inside elevated (Administrator / UAC) windows requires running the MCP server with matching elevation due to Windows UIPI isolation.
