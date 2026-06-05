"""
bash_hook.py — Claude Code PreToolUse / PostToolUse hook for @{VAR} macro expansion.

Claude Code calls this script with JSON on stdin. We read the event, act, and
write JSON to stdout.

PreToolUse flow:
  1. Parse command from tool_input.command
  2. Check for macro-cli commands — pass through without expansion
  3. Expand @{VAR} references
  4. If expansion fails → block with MacroError message
  5. If success → rewrite tool_input.command with expanded form

PostToolUse flow:
  1. Append original command to history
  2. Update @{LastOutput} with tool_response.output (truncated)
  3. Update @{PWD} by parsing `pwd` equivalent from output or re-checking cwd

Usage (called by Claude Code hook runner):
  echo '<hook_json>' | python3 bash_hook.py
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

# Resolve session_state relative to this file so it works from any cwd
sys.path.insert(0, str(Path(__file__).parent))
from session_state import (
    get_state,
    UndefinedMacroError,
    RecursiveExpansionError,
    BuiltinWriteError,
)

# Regex that matches macro-cli commands — these must NOT go through expansion
MACRO_CLI_RE = re.compile(
    r"^\s*(macro-set|macro-list|macro-unset)\b"
)


def _is_macro_command(command: str) -> bool:
    return bool(MACRO_CLI_RE.match(command))


def _extract_pwd_from_output(output: str) -> str | None:
    """
    Look for the last line that looks like an absolute path in command output.
    Used as a fallback to detect directory changes.
    """
    lines = output.strip().splitlines()
    for line in reversed(lines):
        line = line.strip()
        if line.startswith("/") and "\n" not in line and " " not in line:
            return line
    return None


def handle_pre_tool_use(data: dict) -> dict:
    """
    Process PreToolUse hook input.
    Returns the JSON object to write to stdout.
    """
    tool_input = data.get("tool_input", {})
    command = tool_input.get("command", "")

    # Macro-CLI commands bypass expansion (they define/inspect vars)
    if _is_macro_command(command):
        return {"decision": "approve", "reason": "macro-cli command — expansion bypassed"}

    state = get_state()
    try:
        expanded = state.expand(command)
    except (UndefinedMacroError, RecursiveExpansionError, BuiltinWriteError) as e:
        # Block the command entirely — do NOT execute with literal @{...}
        return {
            "decision": "block",
            "reason": str(e),
        }
    except Exception as e:
        return {
            "decision": "block",
            "reason": f"MacroError: Unexpected expansion error: {e}",
        }

    # Rewrite the command with expanded form
    new_tool_input = dict(tool_input)
    new_tool_input["command"] = expanded
    return {"tool_input": new_tool_input}


def handle_post_tool_use(data: dict) -> dict:
    """
    Process PostToolUse hook input.
    Updates LastOutput, LastCommand history, and PWD.
    Returns an informational JSON object (decision field is ignored by Claude Code).
    """
    tool_input = data.get("tool_input", {})
    tool_response = data.get("tool_response", {})

    original_command = tool_input.get("command", "")
    output = tool_response.get("output", "")

    state = get_state()

    # 1. Record the command in history (original, pre-expansion form)
    state.push_command(original_command)

    # 2. Update LastOutput
    state.update_last_output(output)

    # 3. Update PWD
    # Strategy: run `pwd` in a subprocess to get the *actual* current working
    # directory after any `cd` calls in the command. We can't track this from
    # the hook alone because the bash subprocess may have changed dirs.
    # We run a tiny `pwd` subprocess using the same session working directory
    # that Claude Code sets (CLAUDE_PROJECT_DIR or cwd of this process).
    try:
        result = subprocess.run(
            ["bash", "-c", f"cd {repr(os.getcwd())} && {original_command} > /dev/null 2>&1; pwd"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        new_pwd = result.stdout.strip()
        if new_pwd and new_pwd.startswith("/"):
            state.update_pwd(new_pwd)
    except Exception:
        # If we can't determine new PWD, fall back to output heuristic
        guessed = _extract_pwd_from_output(output)
        if guessed:
            state.update_pwd(guessed)
        else:
            # Keep existing PWD unchanged
            pass

    return {"reason": "session state updated"}


def main():
    raw = sys.stdin.read()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        # Malformed input — let the tool proceed unblocked, log to stderr
        print(f"[bash_hook] Failed to parse stdin JSON: {e}", file=sys.stderr)
        sys.exit(0)

    event = data.get("hook_event_name", "")

    if event == "PreToolUse":
        result = handle_pre_tool_use(data)
        print(json.dumps(result))
        # Exit non-zero only when we're blocking
        if result.get("decision") == "block":
            sys.exit(1)
        sys.exit(0)

    elif event == "PostToolUse":
        result = handle_post_tool_use(data)
        print(json.dumps(result))
        sys.exit(0)

    else:
        # Unknown event — pass through silently
        sys.exit(0)


if __name__ == "__main__":
    main()
