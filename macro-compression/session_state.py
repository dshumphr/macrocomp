"""
session_state.py — In-memory session variable store with file backing.

Supports:
  - User-defined session variables (set/get/list/unset)
  - Built-in auto-populated variables (PWD, TaskRoot, LastOutput, LastCommand[n])
  - @{VAR} expansion with recursive depth limit of 2
  - Persistence to /tmp/agent_session.json on every mutation
"""

import json
import re
import os
from pathlib import Path
from typing import Any

STATE_FILE = Path("/tmp/agent_session.json")
MAX_HISTORY = 20
MAX_LAST_OUTPUT = 2000
MAX_EXPAND_DEPTH = 2

BUILTIN_NAMES = {"PWD", "TaskRoot", "LastOutput"}
BUILTIN_PATTERN = re.compile(r"LastCommand\[(\d+)\]")

VAR_RE = re.compile(r"@\{([^}]+)\}")


class UndefinedMacroError(Exception):
    pass


class RecursiveExpansionError(Exception):
    pass


class BuiltinWriteError(Exception):
    pass


class SessionState:
    def __init__(self):
        self._user_vars: dict[str, str] = {}
        self._pwd: str = os.getcwd()
        self._task_root: str = os.getcwd()
        self._last_output: str = ""
        self._command_history: list[str] = []  # index 0 = most recent
        self._load()

    # ------------------------------------------------------------------ #
    # Persistence                                                          #
    # ------------------------------------------------------------------ #

    def _load(self):
        if STATE_FILE.exists():
            try:
                data = json.loads(STATE_FILE.read_text())
                self._user_vars = data.get("user_vars", {})
                self._pwd = data.get("pwd", self._pwd)
                self._task_root = data.get("task_root", self._task_root)
                self._last_output = data.get("last_output", "")
                self._command_history = data.get("command_history", [])
            except (json.JSONDecodeError, OSError):
                pass  # start fresh on corruption

    def _save(self):
        data = {
            "user_vars": self._user_vars,
            "pwd": self._pwd,
            "task_root": self._task_root,
            "last_output": self._last_output,
            "command_history": self._command_history,
        }
        try:
            STATE_FILE.write_text(json.dumps(data, indent=2))
        except OSError as e:
            # Non-fatal — state works in-memory even if disk write fails
            import sys
            print(f"[MacroWarn] Could not persist state: {e}", file=sys.stderr)

    # ------------------------------------------------------------------ #
    # User-defined vars                                                    #
    # ------------------------------------------------------------------ #

    def _is_builtin(self, name: str) -> bool:
        if name in BUILTIN_NAMES:
            return True
        if BUILTIN_PATTERN.fullmatch(name):
            return True
        return False

    def set(self, name: str, value: str):
        if self._is_builtin(name):
            raise BuiltinWriteError(
                f"MacroError: @{{{name}}} is a built-in variable and cannot be overwritten. "
                "Built-ins are managed automatically by the hook."
            )
        self._user_vars[name] = value
        self._save()

    def get(self, name: str) -> str:
        """Retrieve a user-defined var. Raises UndefinedMacroError if absent."""
        if name not in self._user_vars:
            raise UndefinedMacroError(
                f"MacroError: @{{{name}}} is not defined. "
                f"Use `macro-set {name} <value>` to define it, or `macro-list` to see current session vars."
            )
        return self._user_vars[name]

    def unset(self, name: str):
        if self._is_builtin(name):
            raise BuiltinWriteError(
                f"MacroError: @{{{name}}} is a built-in variable and cannot be removed."
            )
        self._user_vars.pop(name, None)
        self._save()

    def list(self) -> dict[str, str]:
        return dict(self._user_vars)

    # ------------------------------------------------------------------ #
    # Built-in resolution                                                  #
    # ------------------------------------------------------------------ #

    def _resolve_builtin(self, name: str) -> str:
        """Resolve a built-in variable by name. Raises UndefinedMacroError if unknown."""
        if name == "PWD":
            return self._pwd
        if name == "TaskRoot":
            return self._task_root
        if name == "LastOutput":
            return self._last_output

        m = BUILTIN_PATTERN.fullmatch(name)
        if m:
            idx = int(m.group(1))  # 1-indexed, 1 = most recent
            if idx < 1 or idx > len(self._command_history):
                raise UndefinedMacroError(
                    f"MacroError: @{{{name}}} is out of range. "
                    f"History has {len(self._command_history)} entries."
                )
            return self._command_history[idx - 1]

        raise UndefinedMacroError(
            f"MacroError: @{{{name}}} is not defined. "
            f"Use `macro-set {name} <value>` to define it, or `macro-list` to see current session vars."
        )

    # ------------------------------------------------------------------ #
    # Expansion                                                            #
    # ------------------------------------------------------------------ #

    def _expand_once(self, text: str) -> tuple[str, bool]:
        """
        Perform a single pass of @{VAR} expansion.
        Returns (expanded_text, changed).
        Raises UndefinedMacroError for any unknown variable.
        """
        errors = []
        changed = False

        def replacer(m: re.Match) -> str:
            nonlocal changed
            name = m.group(1)
            if self._is_builtin(name):
                val = self._resolve_builtin(name)
            elif name in self._user_vars:
                val = self._user_vars[name]
            else:
                raise UndefinedMacroError(
                    f"MacroError: @{{{name}}} is not defined. "
                    f"Use `macro-set {name} <value>` to define it, or `macro-list` to see current session vars."
                )
            changed = True
            return val

        result = VAR_RE.sub(replacer, text)
        return result, changed

    def expand(self, text: str) -> str:
        """
        Expand all @{VAR} references in text, up to MAX_EXPAND_DEPTH recursive passes.
        Raises UndefinedMacroError if a var is not found.
        Raises RecursiveExpansionError if expansion exceeds depth limit.
        """
        current = text
        for depth in range(MAX_EXPAND_DEPTH + 1):
            expanded, changed = self._expand_once(current)
            if not changed:
                return expanded  # nothing left to expand
            if depth == MAX_EXPAND_DEPTH:
                raise RecursiveExpansionError(
                    f"MacroError: Expansion depth exceeded {MAX_EXPAND_DEPTH}. "
                    "Possible circular reference in variable definitions."
                )
            current = expanded
        return current  # unreachable, satisfies type checker

    # ------------------------------------------------------------------ #
    # Hook-managed built-in updates                                        #
    # ------------------------------------------------------------------ #

    def push_command(self, command: str):
        """Called by hook post-execution: prepend command to history."""
        self._command_history.insert(0, command)
        if len(self._command_history) > MAX_HISTORY:
            self._command_history = self._command_history[:MAX_HISTORY]
        self._save()

    def update_last_output(self, output: str):
        self._last_output = output[:MAX_LAST_OUTPUT]
        self._save()

    def update_pwd(self, pwd: str):
        self._pwd = pwd
        self._save()

    def init_task_root(self, path: str | None = None):
        """Set TaskRoot once (at session start). Ignored if already set."""
        if path is None:
            path = os.getcwd()
        self._task_root = path
        self._save()


# Module-level singleton — hooks import this object directly
_state = SessionState()


def get_state() -> SessionState:
    return _state
