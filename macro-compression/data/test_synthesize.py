"""
test_synthesize.py — Unit tests for the synthesize.py rewriting logic.
Run: python3 test_synthesize.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from synthesize import (
    extract_repo_path,
    rewrite_command,
    build_test_cmd,
    build_training_example,
    REPO_PATH_RE,
)

# ------------------------------------------------------------------ #
# Helpers                                                              #
# ------------------------------------------------------------------ #

FAKE_PATH = "/var/folders/l5/h6t9hd752xgdz4xqrpg44xp40000gn/T/swe_psf__requests-2148_qr17wysa/requests"
SHORT_FTP = ["test_requests.py::RequestsTestCase::test_iter_content_handles_socket_error"]


def ok(name): print(f"  \033[92mPASS\033[0m  {name}")
def fail(name, msg): print(f"  \033[91mFAIL\033[0m  {name}: {msg}")

passed = failed = 0

def test(name, condition, msg="assertion failed"):
    global passed, failed
    if condition:
        ok(name)
        passed += 1
    else:
        fail(name, msg)
        failed += 1


# ------------------------------------------------------------------ #
# extract_repo_path                                                    #
# ------------------------------------------------------------------ #

print("\n=== extract_repo_path ===")

cmds = [
    f"cd {FAKE_PATH} && grep -n socket requests/models.py",
    f"grep -rn ConnectionError {FAKE_PATH}/requests/",
]
test("finds path in cd command", extract_repo_path(cmds) == FAKE_PATH)
test("returns None for empty list", extract_repo_path([]) is None)
test("returns None when no swe_ path", extract_repo_path(["ls /tmp"]) is None)


# ------------------------------------------------------------------ #
# rewrite_command                                                      #
# ------------------------------------------------------------------ #

print("\n=== rewrite_command ===")

cmd = f"cd {FAKE_PATH} && grep -n socket_error test_requests.py"
rewritten, n = rewrite_command(cmd, FAKE_PATH, "")
test("replaces path with @{REPO}", "@{REPO}" in rewritten)
test("removes original path", FAKE_PATH not in rewritten)
test("counts 1 substitution", n == 1)

cmd2 = f"grep -rn ConnectionError {FAKE_PATH}/requests/ && cd {FAKE_PATH}"
rewritten2, n2 = rewrite_command(cmd2, FAKE_PATH, "")
test("replaces multiple occurrences", n2 == 2)
test("both occurrences replaced", rewritten2.count("@{REPO}") == 2)

cmd3 = "ls /tmp"
rewritten3, n3 = rewrite_command(cmd3, FAKE_PATH, "")
test("no-op when path absent", rewritten3 == cmd3 and n3 == 0)


# ------------------------------------------------------------------ #
# build_test_cmd                                                       #
# ------------------------------------------------------------------ #

print("\n=== build_test_cmd ===")

commands = [
    f"cd {FAKE_PATH} && grep -n socket test_requests.py",
    f"{FAKE_PATH}/.venv/bin/pytest test_requests.py::RequestsTestCase::test_iter_content_handles_socket_error -x -q",
]
tc = build_test_cmd(FAKE_PATH, SHORT_FTP, commands)
test("finds pytest invocation", "pytest" in tc)
test("replaces path in test_cmd", "@{REPO}" in tc or FAKE_PATH not in tc)
test("includes test ID", "test_iter_content" in tc)


# ------------------------------------------------------------------ #
# build_training_example — full pipeline                               #
# ------------------------------------------------------------------ #

print("\n=== build_training_example ===")

fake_transcript = {
    "task_id": "psf__requests-2148",
    "repo": "psf/requests",
    "version": "2.3",
    "problem_statement": "socket.error not caught in iter_content",
    "fail_to_pass": json.dumps(SHORT_FTP),
    "success": True,
    "bash_commands": [
        f"cd {FAKE_PATH} && grep -n socket_error test_requests.py",
        f"cd {FAKE_PATH} && grep -n iter_content requests/models.py",
        f"cd {FAKE_PATH} && grep -n generate requests/models.py | head -20",
        f"cd {FAKE_PATH} && sed -n '200,250p' requests/models.py",
        f"{FAKE_PATH}/.venv/bin/pytest {SHORT_FTP[0]} -x -q",
        f"{FAKE_PATH}/.venv/bin/pytest {SHORT_FTP[0]} -x -q",
    ],
    "transcript": [
        {"role": "assistant", "text": "Let me look at the test first.",
         "tool_calls": [{"tool_use_id": "t1", "name": "Bash",
                         "input": {"command": f"cd {FAKE_PATH} && grep -n socket_error test_requests.py"}}]},
        {"role": "tool", "tool_use_id": "t1", "command": f"cd {FAKE_PATH} && grep ...",
         "output": "729: with pytest.raises(ConnectionError):", "is_error": False},
        {"role": "assistant", "text": "Now the implementation.",
         "tool_calls": [{"tool_use_id": "t2", "name": "Bash",
                         "input": {"command": f"cd {FAKE_PATH} && grep -n iter_content requests/models.py"}}]},
        {"role": "tool", "tool_use_id": "t2", "command": "", "output": "300: def iter_content...", "is_error": False},
        {"role": "assistant", "text": "Checking generate method.",
         "tool_calls": [{"tool_use_id": "t3", "name": "Bash",
                         "input": {"command": f"cd {FAKE_PATH} && grep -n generate requests/models.py | head -20"}}]},
        {"role": "tool", "tool_use_id": "t3", "command": "", "output": "280: def generate():", "is_error": False},
        {"role": "assistant", "text": "Reading the generate function.",
         "tool_calls": [{"tool_use_id": "t4", "name": "Bash",
                         "input": {"command": f"cd {FAKE_PATH} && sed -n '200,250p' requests/models.py"}}]},
        {"role": "tool", "tool_use_id": "t4", "command": "", "output": "...", "is_error": False},
        {"role": "assistant", "text": "Running tests.",
         "tool_calls": [{"tool_use_id": "t5", "name": "Bash",
                         "input": {"command": f"{FAKE_PATH}/.venv/bin/pytest {SHORT_FTP[0]} -x -q"}}]},
        {"role": "tool", "tool_use_id": "t5", "command": "", "output": "1 passed", "is_error": False},
        {"role": "assistant", "text": "Fixed. The socket.error is now caught.", "tool_calls": []},
    ],
}

example = build_training_example(fake_transcript, min_substitutions=3, min_calls=4)
test("returns a dict", example is not None)
if example:
    test("has messages list", "messages" in example)
    test("has system message first", example["messages"][0]["role"] == "system")
    test("has user message second", example["messages"][1]["role"] == "user")
    test("substitutions >= 3", example["substitutions"] >= 3)
    test("@{REPO} in vars_used", "@{REPO}" in example["vars_used"])
    # Verify no raw paths remain in assistant turns
    raw_in_assistant = any(
        FAKE_PATH in m["content"]
        for m in example["messages"]
        if m["role"] == "assistant"
    )
    test("no raw paths in assistant turns", not raw_in_assistant)
    # Verify rewritten commands expand back to originals
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).parent.parent))
    import session_state as ss
    import tempfile, os
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        tmp = f.name
    state = ss.SessionState()
    state.set("REPO", FAKE_PATH)
    state.set("TEST_CMD", f"{FAKE_PATH}/.venv/bin/pytest {SHORT_FTP[0]} -x -q")
    expandable = True
    for rc in example["rewritten_commands"]:
        try:
            expanded = state.expand(rc)
        except Exception as e:
            expandable = False
            print(f"    expand failed: {e}")
            break
    test("all rewritten commands expand without error", expandable)
    os.unlink(tmp)

# Filtered out when too few subs
sparse_transcript = dict(fake_transcript)
sparse_transcript["bash_commands"] = ["ls /tmp", "pwd", "echo hi", "cat README.md"]
sparse_transcript["transcript"] = [
    {"role": "assistant", "text": "x",
     "tool_calls": [{"tool_use_id": "x1", "name": "Bash", "input": {"command": "ls /tmp"}}]},
    {"role": "tool", "tool_use_id": "x1", "command": "ls /tmp", "output": "...", "is_error": False},
    {"role": "assistant", "text": "done", "tool_calls": []},
]
result_sparse = build_training_example(sparse_transcript, min_substitutions=3, min_calls=4)
test("filters out sparse (no subs) transcript", result_sparse is None)

# Filtered out when task failed
failed_transcript = dict(fake_transcript)
failed_transcript["success"] = False
result_failed = build_training_example(failed_transcript, min_substitutions=3, min_calls=4)
test("filters out failed tasks", result_failed is None)


# ------------------------------------------------------------------ #
# Summary                                                              #
# ------------------------------------------------------------------ #

print(f"\n{'='*40}")
total = passed + failed
color = "\033[92m" if failed == 0 else "\033[91m"
print(f"{color}\033[1m{passed}/{total} tests passed\033[0m")
if failed:
    sys.exit(1)
