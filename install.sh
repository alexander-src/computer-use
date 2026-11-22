#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OS_NAME="$(uname -s)"
PLUGIN_NAME="computer-use"
TARGET_DIR="${ANTIGRAVITY_PLUGIN_DIR:-$HOME/.gemini/config/plugins/$PLUGIN_NAME}"

# Pick the best Python >= 3.10 interpreter
if [[ -z "${PYTHON_BIN:-}" ]]; then
  if [[ "$OS_NAME" == "Darwin" ]]; then
    for cand in "$(command -v python3 || true)" /opt/homebrew/bin/python3 /usr/local/bin/python3 /Library/Frameworks/Python.framework/Versions/Current/bin/python3 /usr/bin/python3; do
      if [[ -n "$cand" && -x "$cand" ]]; then
        if "$cand" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" 2>/dev/null; then
          PYTHON_BIN="$cand"
          break
        fi
      fi
    done
  else
    # On Linux prefer /usr/bin/python3 when it has native GI/DBus/numpy bindings
    if [[ -x /usr/bin/python3 ]] && /usr/bin/python3 -c "import numpy" 2>/dev/null; then
      PYTHON_BIN="/usr/bin/python3"
    else
      PYTHON_BIN="$(command -v python3 || true)"
    fi
  fi
fi

if [[ -z "${PYTHON_BIN:-}" ]]; then
  echo "Error: Python 3.10+ not found in PATH." >&2
  if [[ "$OS_NAME" == "Darwin" ]]; then
    echo "Install with Homebrew: brew install python" >&2
  fi
  exit 1
fi

# Resolve pyenv/asdf/mise shims to real executable so GUI MCP hosts with minimal PATH can launch it
if [[ "$PYTHON_BIN" == *"/shims/"* ]]; then
  PYTHON_BIN="$("$PYTHON_BIN" -c 'import sys; print(sys.executable)')"
fi

if ! "$PYTHON_BIN" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" 2>/dev/null; then
  echo "Error: $PYTHON_BIN is older than Python 3.10." >&2
  exit 1
fi

echo "Using Python: $PYTHON_BIN ($OS_NAME)"
echo "Installing Antigravity plugin '$PLUGIN_NAME' -> $TARGET_DIR"
mkdir -p "$TARGET_DIR"/{assets,agents,rules,scripts,skills/computer-use}

# Portable realpath via Python (works on macOS 12+ and Linux)
REPO_REAL="$("$PYTHON_BIN" -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$REPO_DIR")"
TARGET_REAL="$("$PYTHON_BIN" -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$TARGET_DIR")"

if [[ "$REPO_REAL" != "$TARGET_REAL" ]]; then
  cp -f "$REPO_DIR/plugin.json" "$TARGET_DIR/plugin.json"
  cp -f "$REPO_DIR/README.md" "$TARGET_DIR/README.md"
  cp -f "$REPO_DIR/assets/logo.svg" "$TARGET_DIR/assets/logo.svg"
  cp -f "$REPO_DIR/agents/computer-use.md" "$TARGET_DIR/agents/computer-use.md"
  cp -f "$REPO_DIR/rules/AGENTS.md" "$TARGET_DIR/rules/AGENTS.md"
  cp -f "$REPO_DIR/skills/computer-use/SKILL.md" "$TARGET_DIR/skills/computer-use/SKILL.md"
  cp -f "$REPO_DIR"/scripts/*.py "$TARGET_DIR/scripts/"
fi
chmod +x "$TARGET_DIR/scripts/computer_use_mcp.py"

"$PYTHON_BIN" - "$PYTHON_BIN" "$TARGET_DIR" <<'PY'
import json, os, sys
py_bin, target = sys.argv[1], sys.argv[2]
cfg = {
    "mcpServers": {
        "computer-use": {
            "command": py_bin,
            "args": [os.path.join(target, "scripts", "computer_use_mcp.py")],
        }
    }
}
with open(os.path.join(target, "mcp_config.json"), "w", encoding="utf-8") as f:
    json.dump(cfg, f, indent=2)
    f.write("\n")
PY

echo
if [[ "$OS_NAME" == "Darwin" ]]; then
  echo "Checking macOS dependencies..."
  if ! "$PYTHON_BIN" -c "import numpy" 2>/dev/null; then
    echo "Installing numpy..."
    if "$PYTHON_BIN" -c "import sys; raise SystemExit(0 if sys.prefix != sys.base_prefix else 1)" 2>/dev/null; then
      "$PYTHON_BIN" -m pip install numpy || { echo "Error: install numpy into $PYTHON_BIN" >&2; exit 1; }
    else
      "$PYTHON_BIN" -m pip install --user numpy >/dev/null 2>&1 \
        || "$PYTHON_BIN" -m pip install --break-system-packages --user numpy >/dev/null 2>&1 \
        || "$PYTHON_BIN" -m pip install numpy \
        || { echo "Error: could not install numpy. Install numpy into $PYTHON_BIN or run: brew install numpy" >&2; exit 1; }
    fi
  fi
  "$PYTHON_BIN" - "$TARGET_DIR/scripts" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
from capture import TurboJpegEncoder
from macos_backend import _MacFrameworks
fw = _MacFrameworks.get()
print("  [OK] python module: numpy")
tj = TurboJpegEncoder()
print(f"  [{'OK' if tj.available else 'INFO'}] optional libturbojpeg (brew install jpeg-turbo): {'enabled' if tj.available else 'using built-in Apple ImageIO'}")
ax_ok = fw.accessibility_granted()
sr_ok = fw.screen_recording_granted()
print(f"  [{'OK' if ax_ok else 'WARN'}] macOS Accessibility permission: {'granted' if ax_ok else 'not yet granted'}")
print(f"  [{'OK' if sr_ok else 'WARN'}] macOS Screen Recording permission: {'granted' if sr_ok else 'not yet granted'}")
if not (ax_ok and sr_ok):
    print("         Grant access to your terminal / Antigravity app in:")
    print("           System Settings -> Privacy & Security -> Accessibility")
    print("           System Settings -> Privacy & Security -> Screen & System Audio Recording")
PY
else
  echo "Checking Linux dependencies..."
  "$PYTHON_BIN" -c '
import importlib.util, shutil
for mod in ("numpy", "cairo", "gi", "dbus"):
    tag = "OK" if importlib.util.find_spec(mod) is not None else "WARN"
    print(f"  [{tag}] python module: {mod}")
for bin_name in ("Xvfb", "xdotool"):
    tag = "OK" if shutil.which(bin_name) is not None else "INFO"
    print(f"  [{tag}] optional CLI (background mode): {bin_name}")
'

  echo
  echo "Checking /dev/uinput access..."
  ME="$(whoami)"
  if [[ -w /dev/uinput ]]; then
    echo "  [OK] /dev/uinput is writable by $ME (direct kernel input enabled)"
  else
    echo "  [WARN] /dev/uinput is not writable by $ME."
    echo "         For sub-millisecond direct input on Wayland/X11, run:"
    echo "           sudo usermod -aG input $ME"
    echo "           echo 'KERNEL==\"uinput\", GROUP=\"input\", MODE=\"0660\"' | sudo tee /etc/udev/rules.d/99-uinput.rules"
    echo "           sudo udevadm control --reload-rules && sudo udevadm trigger /dev/uinput"
  fi
fi

echo
echo "Running non-destructive selftest..."
"$PYTHON_BIN" "$TARGET_DIR/scripts/computer_use_mcp.py" --selftest

echo
echo "Done. Restart Antigravity to load the newly installed '$PLUGIN_NAME' plugin."
