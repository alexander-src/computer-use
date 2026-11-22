# computer-use

Fast native Linux desktop automation plugin and MCP server for Antigravity, built around direct KWin ScreenShot2 pipe capture, kernel `/dev/uinput` absolute pointer and keyboard input, AT-SPI2 semantic `[ref]` diffs, and Cairo Set-of-Marks and coordinate-grid overlays.

## Measured Latency

Measured on Arch Linux with KDE Plasma 6 (Wayland, `1920x1080`, scale `1.0`) via `python3 scripts/computer_use_mcp.py --bench` (median of 5 runs):

| Operation | Backend | Median | Min |
| --- | --- | ---: | ---: |
| Window list + active window + cursor | `org.kde.KWin /Scripting` DBus | 1.68 ms | 1.63 ms |
| Clipboard read | `org.kde.klipper` DBus | 0.05 ms | 0.03 ms |
| Full `1920x1080` raw screen capture | `org.kde.KWin.ScreenShot2` Unix FD pipe | 3.80 ms | 3.40 ms |
| `400x300` region capture | `org.kde.KWin.ScreenShot2` Unix FD pipe | 0.57 ms | 0.22 ms |
| `1280x720` JPEG encode (`q=75`, ~62 KB) | `GdkPixbuf` | 16.00 ms | 15.59 ms |
| `1280x720` JPEG + `0..1000` grid + cursor | `GdkPixbuf` + `cairo` | 23.06 ms | 22.93 ms |
| `1920x1080` frame pixel diff | `numpy` | 1.57 ms | 1.56 ms |
| Active window AT-SPI2 tree walk | `gi.repository.Atspi` | 4.41 ms | 4.22 ms |
| Absolute pointer move (`EV_ABS` 0..65535) | Persistent `/dev/uinput` | 0.01 ms | 0.01 ms |

End-to-end full-screen JPEG capture takes ~20 ms (~27 ms with coordinate grid and cursor overlay), compared to ~150 ms when spawning `spectacle` per screenshot.

## Architecture

Each subsystem uses a direct in-process Linux interface first and falls back automatically on other Wayland compositors or X11:

- `scripts/capture.py`: Direct `org.kde.KWin.ScreenShot2` Unix FD pipe capture (fallbacks: `grim`, `spectacle`, `maim`, `scrot`), bilinear downscale and JPEG/PNG encoding via `GdkPixbuf`, Cairo Set-of-Marks `[ref]` badges and `0..1000` coordinate grids, and NumPy frame diffing (`changed_pct`, `changed_bbox`, `wait_until_settled`, `wait_for_change`).
- `scripts/uinput_driver.py`: Persistent kernel `/dev/uinput` QEMU usb-tablet style absolute pointer (`0..65535`, keeping the visible desktop cursor intact with pixel-exact placement on Wayland and X11) and virtual keyboard, plus cosine-eased drag and CLI fallbacks (`ydotool`, `xdotool`, `wtype`).
- `scripts/windows.py`: Direct `org.kde.KWin /Scripting` DBus JSON bridge (fallbacks: `hyprctl`, `swaymsg`, `wmctrl`/`xdotool`) for window listing, focus, minimize, maximize, close, move/resize, and app launching.
- `scripts/a11y.py`: Bounded AT-SPI2 tree walker (320 ms / 1400-node cap) with stable per-window `[ref]` IDs, `find()`, incremental `AX_DIFF` output, and mouse-free `do_action()`.
- `scripts/clipboard.py`: Direct `org.kde.klipper` DBus read/write (fallbacks: `wl-clipboard`, `xclip`, `xsel`).
- `scripts/computer_use_mcp.py`: Serialized JSON-RPC 2.0 stdio MCP server exposing 6 tools, batched step execution, atomic save/paste/restore clipboard typing (auto-switching to `ctrl+shift+v` in terminals), human mouse-takeover detection (`>180 px` jump aborts remaining batch steps), and optional `COMPUTER_USE_AUDIT_LOG` JSONL logging.

## MCP Tools

| Tool | Purpose |
| --- | --- |
| `desktop_observe` | 1-call window list + active app AT-SPI2 `[ref]` tree (`AX_DIFF` on repeat calls) + automatic JPEG screenshot with grid when AX has fewer than 5 elements. |
| `desktop_act` | Execute batched steps (`click`, `move`/`hover`, `drag`, `scroll`, `type`, `key`/`hotkey`, `ax_action`, `focus`, `window`, `launch`, `wait`, `screenshot`) with smart settle, pre/post pixel diff, and takeover safety. |
| `desktop_screenshot` | Capture `screen`, `active`, `window`, `area`, or `ref` with optional Set-of-Marks `[ref]` badges, `0..1000` coordinate grid, and `diff_from_last`. |
| `desktop_windows` | List, focus, minimize, maximize, unmaximize, move/resize, close, or launch desktop applications. |
| `desktop_clipboard` | Read, write, or clear the system clipboard. |
| `desktop_status` | Report active display server, resolution, detected backends, and `/dev/uinput` write access. |

## Requirements

- Linux (Wayland or X11; fastest on KDE Plasma 6 Wayland)
- Python 3.10+ with system bindings:
  - Arch Linux: `sudo pacman -S python-gobject python-cairo python-numpy python-dbus gdk-pixbuf2 at-spi2-core`
  - Debian / Ubuntu: `sudo apt install python3-gi python3-cairo python3-numpy python3-dbus gir1.2-gdkpixbuf-2.0 gir1.2-atspi-2.0`
- Writable `/dev/uinput` for direct kernel pointer and keyboard injection:

```bash
sudo usermod -aG input "$USER"
echo 'KERNEL=="uinput", GROUP="input", MODE="0660"' | sudo tee /etc/udev/rules.d/99-uinput.rules
sudo udevadm control --reload-rules && sudo udevadm trigger /dev/uinput
```

Log out and back in if you were just added to the `input` group.

## Install for Antigravity

```bash
git clone https://github.com/alexander-src/computer-use.git
cd computer-use
./install.sh
```

`install.sh` copies the plugin bundle (`plugin.json`, `mcp_config.json`, `rules/AGENTS.md`, `skills/computer-use/SKILL.md`, `agents/computer-use.md`, `assets/logo.svg`, and `scripts/`) into `~/.gemini/config/plugins/computer-use/`, writes absolute paths to `mcp_config.json`, and runs `--selftest`. Restart Antigravity afterward so it discovers the new plugin directory.

## Standalone CLI Checks and Tests

```bash
# Non-destructive protocol and live backend check
python3 scripts/computer_use_mcp.py --selftest

# Live subsystem latency benchmark
python3 scripts/computer_use_mcp.py --bench

# Unit, mocked engine, Cairo overlay, and stdio JSON-RPC tests
python3 -m unittest discover -s tests -v
```

Set `COMPUTER_USE_AUDIT_LOG=/path/to/audit.jsonl` in `mcp_config.json` (or your shell environment) to record tool calls and timings in JSONL format.

## Limitations

- Linux only (no macOS or Windows backends).
- Direct `/dev/uinput` keyboard typing assumes a US-QWERTY keymap for short ASCII strings (`<= 24` chars). Longer text, Unicode characters, and non-US symbols automatically use atomic clipboard paste instead; pass `mode: "paste"` on `type` if you use a non-US layout for short ASCII strings as well.
- Chromium and Electron apps (such as Chrome, Brave, VS Code, or Discord) do not expose full AT-SPI2 trees unless launched with `--force-renderer-accessibility`. `desktop_observe` detects sparse AX trees (`< 5` elements) and automatically falls back to a grid-overlaid JPEG screenshot.
- Multi-monitor setups with fractional scaling or negative monitor offsets may require verifying cursor mapping against `desktop_windows` and `desktop_screenshot`; single-monitor and side-by-side integer-scale setups map 1:1.
- Running arbitrary GUI actions on a live session can click real buttons and submit real forms. Keep `detect_takeover: true` enabled so moving your mouse aborts active batches.
