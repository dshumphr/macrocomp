"""
tests.py — Unit tests for session_state.py and bash_hook.py core logic.

Run: python3 tests.py
"""

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

# Tests run from this directory
sys.path.insert(0, str(Path(__file__).parent))


def _fresh_state(tmp_file: str):
    """Return a SessionState backed by a temp file."""
    # Patch STATE_FILE before importing so each test gets a clean instance
    import session_state as ss
    ss.STATE_FILE = Path(tmp_file)
    state = ss.SessionState()
    return state


class Color:
    GREEN = "\033[92m"
    RED = "\033[91m"
    RESET = "\033[0m"
    BOLD = "\033[1m"


passed = 0
failed = 0


def test(name: str, fn):
    global passed, failed
    try:
        fn()
        print(f"  {Color.GREEN}PASS{Color.RESET}  {name}")
        passed += 1
    except AssertionError as e:
        print(f"  {Color.RED}FAIL{Color.RESET}  {name}")
        print(f"         AssertionError: {e}")
        failed += 1
    except Exception as e:
        print(f"  {Color.RED}ERR {Color.RESET}  {name}")
        print(f"         {type(e).__name__}: {e}")
        failed += 1


# ------------------------------------------------------------------ #
# SessionState tests                                                  #
# ------------------------------------------------------------------ #

def test_set_and_get():
    import session_state as ss
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        tmp = f.name
    state = _fresh_state(tmp)
    state.set("REPO", "/home/user/myapp")
    assert state.get("REPO") == "/home/user/myapp"
    os.unlink(tmp)


def test_overwrite():
    import session_state as ss
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        tmp = f.name
    state = _fresh_state(tmp)
    state.set("REPO", "/old/path")
    state.set("REPO", "/new/path")
    assert state.get("REPO") == "/new/path"
    os.unlink(tmp)


def test_get_undefined_raises():
    import session_state as ss
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        tmp = f.name
    state = _fresh_state(tmp)
    try:
        state.get("UNDEFINED")
        assert False, "Should have raised UndefinedMacroError"
    except ss.UndefinedMacroError as e:
        assert "UNDEFINED" in str(e)
    os.unlink(tmp)


def test_builtin_write_blocked():
    import session_state as ss
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        tmp = f.name
    state = _fresh_state(tmp)
    for builtin in ["PWD", "TaskRoot", "LastOutput"]:
        try:
            state.set(builtin, "value")
            assert False, f"Should have raised BuiltinWriteError for {builtin}"
        except ss.BuiltinWriteError:
            pass
    os.unlink(tmp)


def test_builtin_lastcommand_write_blocked():
    import session_state as ss
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        tmp = f.name
    state = _fresh_state(tmp)
    try:
        state.set("LastCommand[1]", "blah")
        assert False, "Should have raised BuiltinWriteError"
    except ss.BuiltinWriteError:
        pass
    os.unlink(tmp)


def test_unset():
    import session_state as ss
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        tmp = f.name
    state = _fresh_state(tmp)
    state.set("X", "123")
    state.unset("X")
    try:
        state.get("X")
        assert False, "Should raise after unset"
    except ss.UndefinedMacroError:
        pass
    os.unlink(tmp)


def test_list():
    import session_state as ss
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        tmp = f.name
    state = _fresh_state(tmp)
    state.set("A", "1")
    state.set("B", "2")
    result = state.list()
    assert result == {"A": "1", "B": "2"}
    os.unlink(tmp)


def test_expand_simple():
    import session_state as ss
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        tmp = f.name
    state = _fresh_state(tmp)
    state.set("REPO", "/home/user/myapp")
    result = state.expand("cd @{REPO} && ls")
    assert result == "cd /home/user/myapp && ls"
    os.unlink(tmp)


def test_expand_multiple():
    import session_state as ss
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        tmp = f.name
    state = _fresh_state(tmp)
    state.set("REPO", "/app")
    state.set("TEST", "pytest tests/")
    result = state.expand("cd @{REPO} && @{TEST}")
    assert result == "cd /app && pytest tests/"
    os.unlink(tmp)


def test_expand_undefined_raises():
    import session_state as ss
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        tmp = f.name
    state = _fresh_state(tmp)
    try:
        state.expand("@{MISSING_VAR}")
        assert False, "Should raise UndefinedMacroError"
    except ss.UndefinedMacroError as e:
        assert "MISSING_VAR" in str(e)
    os.unlink(tmp)


def test_expand_recursive_depth_2():
    import session_state as ss
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        tmp = f.name
    state = _fresh_state(tmp)
    state.set("A", "@{B}")
    state.set("B", "final_value")
    result = state.expand("@{A}")
    assert result == "final_value"
    os.unlink(tmp)


def test_expand_recursive_too_deep():
    import session_state as ss
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        tmp = f.name
    state = _fresh_state(tmp)
    # A -> B -> C -> D (depth 3, exceeds limit of 2)
    state.set("A", "@{B}")
    state.set("B", "@{C}")
    state.set("C", "@{D}")
    state.set("D", "deep_value")
    try:
        state.expand("@{A}")
        assert False, "Should raise RecursiveExpansionError"
    except ss.RecursiveExpansionError:
        pass
    os.unlink(tmp)


def test_expand_builtin_pwd():
    import session_state as ss
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        tmp = f.name
    state = _fresh_state(tmp)
    state.update_pwd("/some/dir")
    result = state.expand("cd @{PWD}")
    assert result == "cd /some/dir"
    os.unlink(tmp)


def test_expand_builtin_task_root():
    import session_state as ss
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        tmp = f.name
    state = _fresh_state(tmp)
    state.init_task_root("/project/root")
    result = state.expand("ls @{TaskRoot}")
    assert result == "ls /project/root"
    os.unlink(tmp)


def test_expand_builtin_last_output():
    import session_state as ss
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        tmp = f.name
    state = _fresh_state(tmp)
    state.update_last_output("hello world\n")
    result = state.expand("echo '@{LastOutput}'")
    assert "hello world" in result
    os.unlink(tmp)


def test_expand_builtin_last_command():
    import session_state as ss
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        tmp = f.name
    state = _fresh_state(tmp)
    state.push_command("pytest tests/")
    state.push_command("git status")
    # [1] = most recent = "git status", [2] = "pytest tests/"
    assert state.expand("@{LastCommand[1]}") == "git status"
    assert state.expand("@{LastCommand[2]}") == "pytest tests/"
    os.unlink(tmp)


def test_last_command_out_of_range():
    import session_state as ss
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        tmp = f.name
    state = _fresh_state(tmp)
    state.push_command("ls")
    try:
        state.expand("@{LastCommand[5]}")
        assert False, "Should raise UndefinedMacroError"
    except ss.UndefinedMacroError:
        pass
    os.unlink(tmp)


def test_last_output_truncation():
    import session_state as ss
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        tmp = f.name
    state = _fresh_state(tmp)
    long_output = "x" * 5000
    state.update_last_output(long_output)
    assert len(state._last_output) == 2000
    os.unlink(tmp)


def test_history_max_20():
    import session_state as ss
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        tmp = f.name
    state = _fresh_state(tmp)
    for i in range(25):
        state.push_command(f"cmd_{i}")
    assert len(state._command_history) == 20
    # Most recent first
    assert state._command_history[0] == "cmd_24"
    os.unlink(tmp)


def test_persistence():
    import session_state as ss
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        tmp = f.name
    state = _fresh_state(tmp)
    state.set("KEY", "value123")
    state.update_pwd("/saved/path")

    # Load a fresh instance from the same file
    state2 = _fresh_state(tmp)
    assert state2.get("KEY") == "value123"
    assert state2._pwd == "/saved/path"
    os.unlink(tmp)


# ------------------------------------------------------------------ #
# bash_hook.py tests                                                  #
# ------------------------------------------------------------------ #

def test_hook_pre_expansion():
    import session_state as ss
    import bash_hook

    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        tmp = f.name
    state = _fresh_state(tmp)
    state.set("REPO", "/my/project")

    # Monkey-patch get_state to return our fresh state
    original = bash_hook.get_state
    bash_hook.get_state = lambda: state

    data = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "cd @{REPO} && ls"},
    }
    result = bash_hook.handle_pre_tool_use(data)
    bash_hook.get_state = original

    assert "tool_input" in result
    assert result["tool_input"]["command"] == "cd /my/project && ls"
    os.unlink(tmp)


def test_hook_pre_block_on_undefined():
    import session_state as ss
    import bash_hook

    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        tmp = f.name
    state = _fresh_state(tmp)

    original = bash_hook.get_state
    bash_hook.get_state = lambda: state

    data = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "cd @{UNDEFINED_VAR}"},
    }
    result = bash_hook.handle_pre_tool_use(data)
    bash_hook.get_state = original

    assert result.get("decision") == "block"
    assert "UNDEFINED_VAR" in result.get("reason", "")
    os.unlink(tmp)


def test_hook_macro_cli_bypass():
    import bash_hook
    data = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "macro-set REPO /new/path"},
    }
    result = bash_hook.handle_pre_tool_use(data)
    # Should approve, not rewrite
    assert result.get("decision") == "approve"


def test_hook_post_updates_state():
    import session_state as ss
    import bash_hook

    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        tmp = f.name
    state = _fresh_state(tmp)

    original = bash_hook.get_state
    bash_hook.get_state = lambda: state

    data = {
        "hook_event_name": "PostToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "ls /tmp"},
        "tool_response": {"output": "file1.txt\nfile2.txt\n"},
    }
    bash_hook.handle_post_tool_use(data)
    bash_hook.get_state = original

    # Command should be in history
    assert state._command_history[0] == "ls /tmp"
    # LastOutput should be set
    assert "file1" in state._last_output
    os.unlink(tmp)


# ------------------------------------------------------------------ #
# Run all tests                                                        #
# ------------------------------------------------------------------ #

print(f"\n{Color.BOLD}=== session_state.py ==={Color.RESET}")
test("set and get", test_set_and_get)
test("overwrite", test_overwrite)
test("get undefined raises", test_get_undefined_raises)
test("builtin write blocked", test_builtin_write_blocked)
test("LastCommand[n] write blocked", test_builtin_lastcommand_write_blocked)
test("unset", test_unset)
test("list", test_list)
test("expand simple", test_expand_simple)
test("expand multiple vars", test_expand_multiple)
test("expand undefined raises", test_expand_undefined_raises)
test("expand recursive depth 2", test_expand_recursive_depth_2)
test("expand recursive too deep", test_expand_recursive_too_deep)
test("expand builtin PWD", test_expand_builtin_pwd)
test("expand builtin TaskRoot", test_expand_builtin_task_root)
test("expand builtin LastOutput", test_expand_builtin_last_output)
test("expand builtin LastCommand[n]", test_expand_builtin_last_command)
test("LastCommand out of range", test_last_command_out_of_range)
test("LastOutput truncation at 2000", test_last_output_truncation)
test("history capped at 20", test_history_max_20)
test("persistence to disk", test_persistence)

print(f"\n{Color.BOLD}=== bash_hook.py ==={Color.RESET}")
test("pre-tool-use expands command", test_hook_pre_expansion)
test("pre-tool-use blocks on undefined var", test_hook_pre_block_on_undefined)
test("macro-cli commands bypass expansion", test_hook_macro_cli_bypass)
test("post-tool-use updates state", test_hook_post_updates_state)

print(f"\n{'='*40}")
total = passed + failed
color = Color.GREEN if failed == 0 else Color.RED
print(f"{color}{Color.BOLD}{passed}/{total} tests passed{Color.RESET}")
if failed:
    sys.exit(1)
