#!/usr/bin/env bash
# install.sh — Idempotent install for macro-compression system
# Copies files to ~/.claude/macro-compression/, symlinks CLI tools to
# ~/.local/bin/, and registers hooks in .claude/settings.json.
#
# Usage: bash install.sh [--project]
#   --project   also register hooks in ./.claude/settings.json (project-local)
#               Default: registers in ~/.claude/settings.json (global user)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="$HOME/.claude/macro-compression"
BIN_DIR="$HOME/.local/bin"
GLOBAL_SETTINGS="$HOME/.claude/settings.json"
SESSION_FILE="/tmp/agent_session.json"

PROJECT_MODE=0
for arg in "$@"; do
  [[ "$arg" == "--project" ]] && PROJECT_MODE=1
done

echo "=== Macro Compression Installer ==="
echo ""

# ------------------------------------------------------------------ #
# 1. Copy source files                                                #
# ------------------------------------------------------------------ #

echo "[1/5] Installing source files to $INSTALL_DIR ..."
mkdir -p "$INSTALL_DIR"

for f in session_state.py bash_hook.py macro_cli.py SKILL.md; do
  src="$SCRIPT_DIR/$f"
  dst="$INSTALL_DIR/$f"
  if [[ ! -f "$src" ]]; then
    echo "  ERROR: Source file not found: $src"
    exit 1
  fi
  cp "$src" "$dst"
  echo "  copied $f"
done

# ------------------------------------------------------------------ #
# 2. Make macro_cli.py executable and create symlinks                #
# ------------------------------------------------------------------ #

echo ""
echo "[2/5] Setting up macro-set / macro-list / macro-unset in $BIN_DIR ..."
chmod +x "$INSTALL_DIR/macro_cli.py"
mkdir -p "$BIN_DIR"

for verb in macro-set macro-list macro-unset; do
  link="$BIN_DIR/$verb"
  if [[ -L "$link" ]]; then
    echo "  symlink already exists: $link (skipping)"
  elif [[ -e "$link" ]]; then
    echo "  WARNING: $link exists and is not a symlink — skipping"
  else
    ln -s "$INSTALL_DIR/macro_cli.py" "$link"
    echo "  created symlink: $link -> $INSTALL_DIR/macro_cli.py"
  fi
done

# Check PATH
if ! echo "$PATH" | grep -q "$BIN_DIR"; then
  echo "  NOTE: $BIN_DIR is not in PATH. Add to your shell rc:"
  echo "        export PATH=\"\$HOME/.local/bin:\$PATH\""
fi

# ------------------------------------------------------------------ #
# 3. Register hooks in settings.json                                 #
# ------------------------------------------------------------------ #

HOOK_CMD="python3 $INSTALL_DIR/bash_hook.py"

if [[ "$PROJECT_MODE" -eq 1 ]]; then
  SETTINGS_FILE=".claude/settings.json"
  SETTINGS_LABEL="project (.claude/settings.json)"
  mkdir -p ".claude"
else
  SETTINGS_FILE="$GLOBAL_SETTINGS"
  SETTINGS_LABEL="global (~/.claude/settings.json)"
fi

echo ""
echo "[3/5] Registering hooks in $SETTINGS_LABEL ..."

# Use Python to safely merge the hook config — avoids clobbering existing hooks
python3 - "$SETTINGS_FILE" "$HOOK_CMD" <<'PYEOF'
import json
import sys
import os

settings_path = sys.argv[1]
hook_cmd = sys.argv[2]

# Load existing settings or start fresh
if os.path.exists(settings_path):
    try:
        with open(settings_path) as f:
            settings = json.load(f)
    except (json.JSONDecodeError, OSError):
        print(f"  WARNING: Could not parse {settings_path} — starting fresh")
        settings = {}
else:
    settings = {}

hooks = settings.setdefault("hooks", {})

# Helper: add hook to a hook-event list only if not already present
def add_hook(event: str, matcher: str, cmd: str):
    entries = hooks.setdefault(event, [])
    for entry in entries:
        for h in entry.get("hooks", []):
            if h.get("command") == cmd:
                print(f"  hook already registered: {event}/{matcher} (skipping)")
                return
    entries.append({"matcher": matcher, "hooks": [{"type": "command", "command": cmd}]})
    print(f"  registered hook: {event} matcher={matcher!r}")

add_hook("PreToolUse", "Bash", hook_cmd)
add_hook("PostToolUse", "Bash", hook_cmd)

os.makedirs(os.path.dirname(os.path.abspath(settings_path)), exist_ok=True)
with open(settings_path, "w") as f:
    json.dump(settings, f, indent=2)
    f.write("\n")

print(f"  saved {settings_path}")
PYEOF

# ------------------------------------------------------------------ #
# 4. Initialize session file                                          #
# ------------------------------------------------------------------ #

echo ""
echo "[4/5] Initializing session state at $SESSION_FILE ..."
if [[ -f "$SESSION_FILE" ]]; then
  echo "  $SESSION_FILE already exists (left unchanged)"
else
  echo '{"user_vars":{},"pwd":"","task_root":"","last_output":"","command_history":[]}' \
    > "$SESSION_FILE"
  echo "  created $SESSION_FILE"
fi

# ------------------------------------------------------------------ #
# 5. Summary                                                          #
# ------------------------------------------------------------------ #

echo ""
echo "[5/5] Installation complete."
echo ""
echo "  Source files:  $INSTALL_DIR"
echo "  CLI tools:     $BIN_DIR/macro-set, macro-list, macro-unset"
echo "  Settings:      $SETTINGS_FILE"
echo "  Session file:  $SESSION_FILE"
echo "  Skill doc:     $INSTALL_DIR/SKILL.md"
echo ""
echo "To verify hooks are active, open Claude Code and run:"
echo "  /hooks"
echo ""
echo "To load the skill into a Claude Code session:"
echo "  cat $INSTALL_DIR/SKILL.md"
echo ""
echo "Quick test:"
echo "  macro-set REPO /tmp/test-repo"
echo "  macro-list"
echo ""
