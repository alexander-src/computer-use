#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_NAME="computer-use"
TARGET_DIR="${ANTIGRAVITY_PLUGIN_DIR:-$HOME/.gemini/config/plugins/$PLUGIN_NAME}"

# Prefer system /usr/bin/python3 when it has native GI/DBus bindings (avoids ephemeral venvs)
if [[ -z "${PYTHON_BIN:-}" ]]; then
  if [[ -x /usr/bin/python3 ]] && /usr/bin/python3 -c "import numpy" 2>/dev/null; then
    PYTHON_BIN="/usr/bin/python3"
  else
    PYTHON_BIN="$(command -v python3 || true)"
  fi
fi

if [[ -z "${PYTHON_BIN}" ]]; then
  echo "Error: python3 not found in PATH." >&2
  exit 1
fi

echo "Using Python: $PYTHON_BIN"
echo "Installing Antigravity plugin '$PLUGIN_NAME' -> $TARGET_DIR"
mkdir -p "$TARGET_DIR"/{assets,agents,rules,scripts,skills/computer-use}

if [[ "$(realpath "$REPO_DIR")" != "$(realpath "$TARGET_DIR")" ]]; then
  cp "$REPO_DIR/plugin.json" "$TARGET_DIR/plugin.json"
  cp "$REPO_DIR/README.md" "$TARGET_DIR/README.md"
  cp "$REPO_DIR/assets/logo.svg" "$TARGET_DIR/assets/logo.svg"
  cp "$REPO_DIR/agents/computer-use.md" "$TARGET_DIR/agents/computer-use.md"
  cp "$REPO_DIR/rules/AGENTS.md" "$TARGET_DIR/rules/AGENTS.md"
  cp "$REPO_DIR/skills/computer-use/SKILL.md" "$TARGET_DIR/skills/computer-use/SKILL.md"
  cp "$REPO_DIR"/scripts/*.py "$TARGET_DIR/scripts/"
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

echo
echo "Running non-destructive selftest..."
"$PYTHON_BIN" "$TARGET_DIR/scripts/computer_use_mcp.py" --selftest

echo
echo "Done. Restart Antigravity to load the newly installed '$PLUGIN_NAME' plugin."
