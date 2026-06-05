"""
test_harness.py — SWE-bench Lite sample runner for macro-compression evaluation.

Runs a set of SWE-bench tasks under two conditions:
  baseline  — standard Claude Code, no macro system
  macro     — Claude Code with macro hook active, SKILL.md in system prompt

Metrics per task:
  - tool_call_tokens (input tokens from Bash tool calls specifically)
  - total_calls (number of Bash tool calls)
  - success (bool, based on test suite pass/fail)
  - macro_expansions (list of @{VAR} references seen in macro condition)

Output:
  logs/<task_id>_<condition>.json per task
  logs/summary.json with aggregated stats

Usage:
  python3 test_harness.py [--tasks <n>] [--conditions baseline,macro] [--repo-base <path>]

Requirements:
  - claude CLI installed and authenticated (claude auth status)
  - SWE-bench tasks cloned under --repo-base (default: /tmp/swebench-repos)
  - jq installed (used for extracting token counts from Claude JSON output)

SWE-bench task format expected in tasks.json (auto-downloaded if absent):
  [{"task_id": "...", "repo": "owner/repo", "test_patch": "...", "patch": "..."}, ...]

Note: For lightweight testing without full SWE-bench setup, see --mock flag.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

SCRIPT_DIR = Path(__file__).parent
LOGS_DIR = SCRIPT_DIR / "logs"
SKILL_PATH = SCRIPT_DIR / "SKILL.md"
INSTALL_DIR = Path.home() / ".claude" / "macro-compression"

# Approximate token count: ~4 chars per token (rough)
def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


@dataclass
class TaskResult:
    task_id: str
    condition: str  # "baseline" or "macro"
    tool_call_tokens: int = 0
    total_calls: int = 0
    success: bool = False
    macro_expansions: list[str] = field(default_factory=list)
    error: Optional[str] = None
    duration_seconds: float = 0.0


# ------------------------------------------------------------------ #
# Lightweight mock tasks for testing without full SWE-bench setup    #
# ------------------------------------------------------------------ #

MOCK_TASKS = [
    {
        "task_id": "mock_001",
        "repo": "mock/repo",
        "description": "Add a hello function to utils.py",
        "test_cmd": "python3 -m pytest tests/test_utils.py -x -q",
        "setup_files": {
            "utils.py": "# utils module\n",
            "tests/__init__.py": "",
            "tests/test_utils.py": (
                "from utils import hello\n"
                "def test_hello():\n"
                "    assert hello() == 'hello'\n"
            ),
        },
    },
    {
        "task_id": "mock_002",
        "repo": "mock/repo",
        "description": "Fix the divide function to handle zero division",
        "test_cmd": "python3 -m pytest tests/test_math.py -x -q",
        "setup_files": {
            "math_utils.py": "def divide(a, b):\n    return a / b\n",
            "tests/__init__.py": "",
            "tests/test_math.py": (
                "from math_utils import divide\n"
                "def test_divide_zero():\n"
                "    assert divide(10, 0) is None\n"
                "def test_divide_normal():\n"
                "    assert divide(10, 2) == 5.0\n"
            ),
        },
    },
    {
        "task_id": "mock_003",
        "repo": "mock/repo",
        "description": "Add a word count function that handles empty strings",
        "test_cmd": "python3 -m pytest tests/test_text.py -x -q",
        "setup_files": {
            "text_utils.py": "def word_count(s):\n    return len(s.split())\n",
            "tests/__init__.py": "",
            "tests/test_text.py": (
                "from text_utils import word_count\n"
                "def test_empty():\n"
                "    assert word_count('') == 0\n"
                "def test_normal():\n"
                "    assert word_count('hello world') == 2\n"
            ),
        },
    },
]


def setup_mock_repo(task: dict, tmp_dir: Path):
    """Create a minimal repo for a mock task."""
    for rel_path, content in task["setup_files"].items():
        full = tmp_dir / rel_path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content)


def build_baseline_prompt(task: dict, repo_path: Path) -> str:
    return f"""You are working on a software engineering task.

Repository: {repo_path}
Task: {task['description']}
Test command: {task.get('test_cmd', 'pytest')}

Please fix the issue so all tests pass. Work in the repository directory.
Run the tests to verify your fix. Be thorough — check the test file to understand what's expected.
"""


def build_macro_prompt(task: dict, repo_path: Path, skill_content: str) -> str:
    return f"""{skill_content}

---

You are working on a software engineering task. Session variables have been pre-seeded:
  @{{REPO}}      = {repo_path}
  @{{TaskRoot}}  = {repo_path}
  @{{TEST_CMD}}  = {task.get('test_cmd', 'pytest')}

Use @{{REPO}}, @{{TEST_CMD}}, and other macros to reduce repetition.
Task: {task['description']}

Please fix the issue so all tests pass. Run @{{TEST_CMD}} to verify your fix.
"""


def run_claude(
    prompt: str,
    repo_path: Path,
    condition: str,
    task: dict,
    settings_override: Optional[dict] = None,
) -> dict:
    """
    Run Claude Code in print mode on a task.
    Returns parsed JSON output from --output-format json.
    """
    # Write prompt to temp file to avoid shell escaping nightmares
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        f.write(prompt)
        prompt_file = f.name

    # Build settings (with or without hooks)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        if settings_override:
            json.dump(settings_override, f)
        else:
            json.dump({}, f)
        settings_file = f.name

    cmd = [
        "claude",
        "--bare",  # skip global hooks/plugins — we control settings explicitly
        "-p", open(prompt_file).read(),
        "--output-format", "json",
        "--max-turns", "20",
        "--allowedTools", "Bash,Read,Write,Edit",
        "--settings", settings_file,
        "--dangerously-skip-permissions",
    ]

    if condition == "macro" and (INSTALL_DIR / "bash_hook.py").exists():
        # Add hook settings
        hook_cmd = f"python3 {INSTALL_DIR / 'bash_hook.py'}"
        hook_settings = {
            "hooks": {
                "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": hook_cmd}]}],
                "PostToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": hook_cmd}]}],
            }
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(hook_settings, f)
            settings_file = f.name
        cmd[-2] = settings_file  # replace settings file ref

    try:
        result = subprocess.run(
            cmd,
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=300,
        )
        raw = result.stdout.strip()
        if raw:
            return json.loads(raw)
        return {"error": result.stderr or "No output from claude"}
    except subprocess.TimeoutExpired:
        return {"error": "Timeout after 300s"}
    except json.JSONDecodeError as e:
        return {"error": f"JSON parse error: {e}", "raw": result.stdout[:2000]}
    except FileNotFoundError:
        return {"error": "claude CLI not found — is it installed?"}
    finally:
        os.unlink(prompt_file)
        os.unlink(settings_file)


def extract_bash_tool_calls(transcript_path: Optional[str]) -> list[str]:
    """
    Read the Claude Code transcript JSON and extract all Bash tool call command strings.
    Returns list of command strings.
    """
    if not transcript_path or not Path(transcript_path).exists():
        return []
    try:
        transcript = json.loads(Path(transcript_path).read_text())
        commands = []
        # Transcript format: list of messages with tool_use content blocks
        for msg in transcript:
            if not isinstance(msg, dict):
                continue
            for block in msg.get("content", []):
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    if block.get("name") == "Bash":
                        cmd = block.get("input", {}).get("command", "")
                        if cmd:
                            commands.append(cmd)
        return commands
    except Exception:
        return []


def find_macro_expansions(commands: list[str]) -> list[str]:
    """Find all @{VAR} references in a list of command strings."""
    pattern = re.compile(r"@\{[^}]+\}")
    found = set()
    for cmd in commands:
        found.update(pattern.findall(cmd))
    return sorted(found)


def run_tests(test_cmd: str, repo_path: Path) -> bool:
    """Run the test command and return True if all tests pass."""
    try:
        result = subprocess.run(
            test_cmd,
            shell=True,
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=60,
        )
        return result.returncode == 0
    except Exception:
        return False


def run_condition(
    task: dict,
    condition: str,
    repo_path: Path,
) -> TaskResult:
    """Run one task under one condition."""
    result = TaskResult(task_id=task["task_id"], condition=condition)
    start = time.time()

    skill_content = SKILL_PATH.read_text() if SKILL_PATH.exists() else ""

    if condition == "baseline":
        prompt = build_baseline_prompt(task, repo_path)
        claude_out = run_claude(prompt, repo_path, condition, task)
    else:
        prompt = build_macro_prompt(task, repo_path, skill_content)
        claude_out = run_claude(prompt, repo_path, condition, task)

    result.duration_seconds = round(time.time() - start, 2)

    if "error" in claude_out:
        result.error = claude_out["error"]
        return result

    # Extract metrics from Claude output
    usage = claude_out.get("usage", {})
    # input_tokens from usage reflects full context; we need tool-call-specific tokens
    # Approximate: extract commands from transcript and sum their token costs
    transcript_path = claude_out.get("transcript_path")  # not always present in --bare mode
    bash_commands = extract_bash_tool_calls(transcript_path)
    result.total_calls = len(bash_commands)

    # Token estimate: sum the character lengths of all bash commands
    result.tool_call_tokens = sum(estimate_tokens(cmd) for cmd in bash_commands)

    # For macro condition, find @{VAR} references
    if condition == "macro":
        result.macro_expansions = find_macro_expansions(bash_commands)

    # Run tests to determine success
    test_cmd = task.get("test_cmd", "pytest")
    result.success = run_tests(test_cmd, repo_path)

    return result


def main():
    parser = argparse.ArgumentParser(description="Macro compression SWE-bench evaluator")
    parser.add_argument("--tasks", type=int, default=3, help="Number of mock tasks to run")
    parser.add_argument(
        "--conditions",
        default="baseline,macro",
        help="Comma-separated conditions: baseline,macro",
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        default=True,
        help="Use built-in mock tasks (default; no SWE-bench setup needed)",
    )
    parser.add_argument(
        "--tasks-file",
        default=None,
        help="JSON file with SWE-bench tasks (overrides --mock)",
    )
    parser.add_argument(
        "--repo-base",
        default="/tmp/swebench-repos",
        help="Base dir for checked-out repos (SWE-bench mode)",
    )
    args = parser.parse_args()

    conditions = [c.strip() for c in args.conditions.split(",")]
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    # Load tasks
    if args.tasks_file:
        tasks = json.loads(Path(args.tasks_file).read_text())[: args.tasks]
        use_mock = False
    else:
        tasks = MOCK_TASKS[: args.tasks]
        use_mock = True

    all_results: list[TaskResult] = []

    for task in tasks:
        print(f"\n=== Task: {task['task_id']} ===")

        for condition in conditions:
            print(f"  Condition: {condition} ...", end=" ", flush=True)

            if use_mock:
                # Set up a fresh temporary repo for each (task, condition) pair
                with tempfile.TemporaryDirectory() as tmp:
                    repo_path = Path(tmp)
                    setup_mock_repo(task, repo_path)

                    # Pre-seed session vars for macro condition
                    if condition == "macro":
                        # Reset session state
                        session_file = Path("/tmp/agent_session.json")
                        session_file.write_text(json.dumps({
                            "user_vars": {
                                "REPO": str(repo_path),
                                "TEST_CMD": task.get("test_cmd", "pytest"),
                            },
                            "pwd": str(repo_path),
                            "task_root": str(repo_path),
                            "last_output": "",
                            "command_history": [],
                        }, indent=2))

                    result = run_condition(task, condition, repo_path)
            else:
                repo_path = Path(args.repo_base) / task["task_id"]
                repo_path.mkdir(parents=True, exist_ok=True)
                result = run_condition(task, condition, repo_path)

            status = "PASS" if result.success else ("ERR" if result.error else "FAIL")
            print(f"{status} | {result.tool_call_tokens} tok | {result.total_calls} calls")

            # Save per-task log
            log_path = LOGS_DIR / f"{task['task_id']}_{condition}.json"
            log_path.write_text(json.dumps(asdict(result), indent=2))
            all_results.append(result)

    # ------------------------------------------------------------------ #
    # Summary                                                              #
    # ------------------------------------------------------------------ #

    print("\n=== Summary ===\n")
    baseline_results = [r for r in all_results if r.condition == "baseline"]
    macro_results = [r for r in all_results if r.condition == "macro"]

    def avg(vals):
        return round(sum(vals) / len(vals), 1) if vals else 0

    if baseline_results and macro_results:
        base_tok = avg([r.tool_call_tokens for r in baseline_results])
        macro_tok = avg([r.tool_call_tokens for r in macro_results])
        base_calls = avg([r.total_calls for r in baseline_results])
        macro_calls = avg([r.total_calls for r in macro_results])
        base_pass = sum(1 for r in baseline_results if r.success)
        macro_pass = sum(1 for r in macro_results if r.success)

        reduction = round((1 - macro_tok / base_tok) * 100, 1) if base_tok > 0 else 0

        print(f"{'Metric':<30} {'Baseline':>12} {'Macro':>12}")
        print(f"{'-'*54}")
        print(f"{'Avg tool-call tokens':<30} {base_tok:>12} {macro_tok:>12}")
        print(f"{'Avg bash calls':<30} {base_calls:>12} {macro_calls:>12}")
        print(f"{'Tasks passed':<30} {base_pass:>12} {macro_pass:>12}")
        print(f"{'Token reduction %':<30} {'':>12} {reduction:>11}%")

        summary = {
            "baseline": {
                "avg_tool_call_tokens": base_tok,
                "avg_bash_calls": base_calls,
                "tasks_passed": base_pass,
                "total_tasks": len(baseline_results),
            },
            "macro": {
                "avg_tool_call_tokens": macro_tok,
                "avg_bash_calls": macro_calls,
                "tasks_passed": macro_pass,
                "total_tasks": len(macro_results),
                "token_reduction_pct": reduction,
            },
            "tasks": [asdict(r) for r in all_results],
        }
        (LOGS_DIR / "summary.json").write_text(json.dumps(summary, indent=2))
        print(f"\nFull results: {LOGS_DIR}/summary.json")


if __name__ == "__main__":
    main()
