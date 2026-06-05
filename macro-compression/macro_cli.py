#!/usr/bin/env python3
"""
macro_cli.py — CLI for managing session macro variables.

Invoked as:
  macro-set VAR_NAME "value here"    # define or overwrite a session var
  macro-list                          # list all user-defined vars
  macro-unset VAR_NAME                # remove a var

This script is symlinked on PATH as macro-set, macro-list, macro-unset.
The verb is inferred from argv[0] (the symlink name) or from the first
positional argument when called directly as `python3 macro_cli.py <verb> ...`.
"""

import sys
import os
from pathlib import Path

# Resolve session_state relative to this file regardless of cwd
sys.path.insert(0, str(Path(__file__).parent))
from session_state import (
    get_state,
    UndefinedMacroError,
    BuiltinWriteError,
)


def cmd_set(args: list[str]):
    if len(args) < 2:
        print("Usage: macro-set VAR_NAME <value>", file=sys.stderr)
        sys.exit(1)
    name = args[0]
    # Everything after name joined with space (allows multi-word values)
    value = " ".join(args[1:])
    state = get_state()
    try:
        state.set(name, value)
        print(f"macro: @{{{name}}} = {value!r}")
    except BuiltinWriteError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)


def cmd_list(args: list[str]):
    state = get_state()
    user_vars = state.list()
    if not user_vars:
        print("No session variables defined. Use `macro-set NAME value` to add one.")
        return

    max_len = max(len(k) for k in user_vars)
    print(f"{'Variable':<{max_len}}  Value")
    print(f"{'-' * max_len}  -----")
    for name, value in sorted(user_vars.items()):
        display = value if len(value) <= 80 else value[:77] + "..."
        print(f"{name:<{max_len}}  {display}")

    # Also show built-ins for reference
    print()
    print("Built-in variables (auto-managed, read-only):")
    print("  @{PWD}             current working directory (updated after each command)")
    print("  @{TaskRoot}        working directory at session start (immutable)")
    print("  @{LastOutput}      stdout+stderr of most recent command (truncated at 2000 chars)")
    print("  @{LastCommand[n]}  nth most recent command string (1 = most recent, max 20)")


def cmd_unset(args: list[str]):
    if len(args) < 1:
        print("Usage: macro-unset VAR_NAME", file=sys.stderr)
        sys.exit(1)
    name = args[0]
    state = get_state()
    try:
        state.unset(name)
        print(f"macro: @{{{name}}} removed")
    except BuiltinWriteError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)


VERBS = {
    "macro-set": cmd_set,
    "macro-list": cmd_list,
    "macro-unset": cmd_unset,
    "set": cmd_set,
    "list": cmd_list,
    "unset": cmd_unset,
}


def main():
    argv0 = os.path.basename(sys.argv[0])  # symlink name or script name

    # Determine verb from argv[0] symlink name first
    if argv0 in VERBS:
        verb = argv0
        args = sys.argv[1:]
    elif len(sys.argv) >= 2 and sys.argv[1] in VERBS:
        # Called as: python3 macro_cli.py <verb> [args...]
        verb = sys.argv[1]
        args = sys.argv[2:]
    else:
        print(
            "Usage:\n"
            "  macro-set VAR_NAME value   # define a session variable\n"
            "  macro-list                  # show all session variables\n"
            "  macro-unset VAR_NAME        # remove a session variable",
            file=sys.stderr,
        )
        sys.exit(1)

    VERBS[verb](args)


if __name__ == "__main__":
    main()
