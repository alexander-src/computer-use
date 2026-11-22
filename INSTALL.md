# Agent Installation Guide (`computer-use`)

Follow these exact steps to install `alexander-src/computer-use` on the user's machine and verify it works.

## 1. Clone the repository

On Linux or macOS:

```bash
git clone https://github.com/alexander-src/computer-use.git ~/.local/share/computer-use
cd ~/.local/share/computer-use
```

On Windows PowerShell:

```powershell
git clone https://github.com/alexander-src/computer-use.git "$env:LOCALAPPDATA\computer-use"
cd "$env:LOCALAPPDATA\computer-use"
```

## 2. Install dependencies and run the installer

### Linux (Arch / CachyOS / Manjaro)

```bash
sudo pacman -S --needed python-gobject python-cairo python-numpy python-dbus gdk-pixbuf2 at-spi2-core libjpeg-turbo xorg-server-xvfb xdotool
./install.sh
```

If `/dev/uinput` is not yet writable by the current user, tell the user to run:

```bash
sudo usermod -aG input "$USER"
echo 'KERNEL=="uinput", GROUP="input", MODE="0660"' | sudo tee /etc/udev/rules.d/99-uinput.rules
sudo udevadm control --reload-rules && sudo udevadm trigger /dev/uinput
```

### Linux (Debian / Ubuntu)

```bash
sudo apt-get update
sudo apt-get install -y python3-gi python3-cairo python3-numpy python3-dbus gir1.2-gdkpixbuf-2.0 gir1.2-atspi-2.0 libturbojpeg0 xvfb xdotool
./install.sh
```

### macOS (Apple Silicon and Intel, macOS 13+)

```bash
./install.sh
```

`install.sh` picks Python 3.10+, installs `numpy` if missing, and uses built-in Apple frameworks (CoreGraphics, ImageIO, ApplicationServices `AXUIElement`, AppKit) with no compiler or third-party GUI libraries required. Optional faster NEON/AVX2 JPEG encoding: `brew install jpeg-turbo`.

On first run, grant permissions to your terminal or Antigravity app in:

1. System Settings -> Privacy & Security -> Accessibility (required for clicking, typing, and `AXUIElement` trees)
2. System Settings -> Privacy & Security -> Screen & System Audio Recording (required for capturing other apps' windows)

### Windows 10 / 11 (PowerShell)

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

No native C compiler or third-party screen-capture libraries are required on Windows (`install.ps1` only installs `numpy` if missing; capture, GDI+ JPEG/PNG encoding, `SendInput`, clipboard, UI Automation COM, and the layered cursor overlay use built-in Win32 DLLs).

## 3. Configure other MCP clients (optional)

`install.sh` and `install.ps1` automatically install the full Antigravity plugin bundle into `~/.gemini/config/plugins/computer-use/` (restart Antigravity once to load a newly installed or updated MCP server).

For Claude Code:

```bash
claude mcp add computer-use -- python3 ~/.gemini/config/plugins/computer-use/scripts/computer_use_mcp.py
```

For Codex CLI:

```bash
codex mcp add computer-use -- python3 ~/.gemini/config/plugins/computer-use/scripts/computer_use_mcp.py
```

For Cursor, Gemini CLI, or any stdio MCP host, add to your MCP config JSON:

```json
{
  "mcpServers": {
    "computer-use": {
      "command": "python3",
      "args": ["/ABSOLUTE/PATH/TO/computer-use/scripts/computer_use_mcp.py"]
    }
  }
}
```

## 4. Verify installation

Run both checks and report the results to the user:

```bash
python3 scripts/computer_use_mcp.py --selftest --bench
```

Optional visual preview of the smooth custom AI cursor and click ripples (non-destructive, performs no real clicks):

```bash
python3 scripts/computer_use_mcp.py --demo-cursor
```
