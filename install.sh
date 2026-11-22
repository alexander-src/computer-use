#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_NAME="computer-use"
TARGET_DIR="${ANTIGRAVITY_PLUGIN_DIR:-$HOME/.gemini/config/plugins/$PLUGIN_NAME}"
PYTHON_BIN="${PYTHON_BIN:-$(command -v python3)}"

if [[ -z "${PYTHON_BIN}" ]]; then
  echo "Error: python3 not found in PATH." >&2
  exit 1
fi

echo "Installing Antigravity plugin '$PLUGIN_NAME' -> $TARGET_DIR"
mkdir -p "$TARGET_DIR"/{assets,agents,rules,scripts,skills/computer-use}

cp "$REPO_DIR/plugin.json" "$TARGET_DIR/plugin.json"
cp "$REPO_DIR/README.md" "$TARGET_DIR/README.md"
cp "$REPO_DIR/assets/logo.svg" "$TARGET_DIR/assets/logo.svg"
cp "$REPO_DIR/agents/computer-use.md" "$TARGET_DIR/agents/computer-use.md"
cp "$REPO_DIR/rules/AGENTS.md" "$TARGET_DIR/rules/AGENTS.md"
cp "$REPO_DIR/skills/computer-use/SKILL.md" "$TARGET_DIR/skills/computer-use/SKILL.md"
cp "$REPO_DIR"/scripts/*.py "$TARGET_DIR/scripts/"
chmod +x "$TARGET_DIR/scripts/computer_use_mcp.py"

cat > "$TARGET_DIR/mcp_config.json" <<EOF
{
  "mcpServers": {
    "computer-use": {
      "command": "$PYTHON_BIN",
      "args": [
        "$TARGET_DIR/scripts/computer_use_mcp.py"
      ]
    }
  }
}
EOF

echo
echo "Checking /dev/uinput access..."
if [[ -w /dev/uinput ]]; then
  echo "  [OK] /dev/uinput is writable by $(whoami) (direct kernel input enabled)"
else
  echo "  [WARN] /dev/uinput is not writable by $(whoami)."
  echo "         For sub-millisecond direct input on Wayland/X11, add yourself to the 'input' group"
  echo "         and install a udev rule:"
  echo "           sudo usermod -aG input $USER"
  echo "           echo 'KERNEL==\"uinput\", GROUP=\"input\", MODE=\"0660\"' | sudo tee /etc/udev/rules.d/99-uinput.rules"
  echo "           sudo udevadm control --reload-rules && sudo udevadm trigger /dev/uinput"
fi

echo
echo "Running non-destructive selftest..."
"$PYTHON_BIN" "$TARGET_DIR/scripts/computer_use_mcp.py" --selftest

echo
echo "Done. Restart Antigravity to load the newly installed '$PLUGIN_NAME' plugin."
