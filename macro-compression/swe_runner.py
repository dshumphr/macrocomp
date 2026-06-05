"""
swe_runner.py — Real SWE-bench runner for macro-compression evaluation.

For each task × condition:
  1. Clone repo at base_commit into a fresh tmpdir
  2. Set up a uv venv and install the package
  3. Apply the test_patch so the tests exist
  4. Run Claude Code (--output-format stream-json) with or without macro hooks
  5. Run FAIL_TO_PASS tests to score success
  6. Emit JSON log per (task, condition)

Usage:
  python3 swe_runner.py --tasks /tmp/swe_selected.json [--conditions baseline,macro] [--max-turns 15]
"""

import argparse
import json
import os
import re
import shutil
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
SESSION_JSON = Path("/tmp/agent_session.json")

# Use the updated claude binary (v2.x with --output-format support)
CLAUDE_BIN = str(Path.home() / ".npm-global" / "bin" / "claude")
if not Path(CLAUDE_BIN).exists():
    CLAUDE_BIN = "claude"  # fallback to PATH

# ------------------------------------------------------------------ #
# Data                                                                 #
# ------------------------------------------------------------------ #

@dataclass
class TaskResult:
    task_id: str
    condition: str
    tool_call_chars: int = 0       # sum of bash command string lengths (proxy for tokens)
    tool_call_tokens_est: int = 0  # rough estimate: chars / 4
    total_bash_calls: int = 0
    total_input_tokens: int = 0    # from Claude's usage field (full context)
    total_output_tokens: int = 0
    success: bool = False
    macro_expansions: list = field(default_factory=list)
    error: Optional[str] = None
    duration_seconds: float = 0.0
    bash_commands: list = field(default_factory=list)  # all commands Claude issued


# ------------------------------------------------------------------ #
# Repo setup                                                           #
# ------------------------------------------------------------------ #

GITHUB_URLS = {
    "pallets/flask": "https://github.com/pallets/flask.git",
    "pytest-dev/pytest": "https://github.com/pytest-dev/pytest.git",
    "psf/requests": "https://github.com/psf/requests.git",
    "sympy/sympy": "https://github.com/sympy/sympy.git",
    "django/django": "https://github.com/django/django.git",
    "sphinx-doc/sphinx": "https://github.com/sphinx-doc/sphinx.git",
    "pylint-dev/pylint": "https://github.com/pylint-dev/pylint.git",
    "pydata/xarray": "https://github.com/pydata/xarray.git",
}

INSTALL_EXTRAS = {
    "flask": [".[dev]"],
    "pytest": [".[testing]"],
    "requests": ["-e", ".", "pytest", "pytest-mock", "pytest-timeout"],
    "sympy": ["."],
    "django": [".", "pytest", "pytest-django"],
    "sphinx": ["setuptools", ".[test]"],
    "pylint": ["-e", ".[dev,testutils]", "pytest"],
}

# Per-repo Python version overrides.
REPO_PYTHON = {
    "requests": "/Users/danielhumphries/.local/share/uv/python/cpython-3.9-macos-aarch64-none/bin/python3.9",
    "django": "/opt/homebrew/bin/python3.12",
    "sphinx": "/opt/homebrew/bin/python3.11",
    "pylint": "/Users/danielhumphries/.local/share/uv/python/cpython-3.11-macos-aarch64-none/bin/python3.11",
}

# Version-specific constraint pins needed to match the original test environment.
# SWE-bench tasks were recorded against specific dep versions; without these pins
# the package often fails to import due to incompatible transitive dependencies.
VERSION_CONSTRAINTS = {
    ("flask", "2.0"): ["werkzeug<2.1", "jinja2<3.1"],
    ("flask", "2.1"): ["werkzeug>=2.0,<2.2"],
    ("flask", "2.2"): ["werkzeug>=2.2,<3"],
    ("flask", "2.3"): ["werkzeug>=2.3"],
    ("pytest", "4.4"): ["pluggy>=0.7,<1.0", "attrs>=17.4.0", "more-itertools>=4.0.0"],
    ("pytest", "4.5"): ["pluggy>=0.7,<1.0"],
    ("pytest", "4.6"): ["pluggy>=0.7,<1.0", "py>=1.5.0", "wcwidth"],
    ("pytest", "5.4"): ["pluggy>=0.12,<1.0", "py>=1.5.0", "attrs>=17.4.0"],
    ("pytest", "6.3"): ["pluggy>=0.12,<2.0"],
    ("pytest", "7.0"): ["pluggy>=1.0,<2.0"],
    ("pytest", "7.4"): ["pluggy>=1.0,<2.0"],
    ("pytest", "8.0"): ["pluggy>=1.0,<2.0"],
    ("pytest", "8.1"): ["pluggy>=1.0,<2.0"],
}


def clone_repo(repo: str, commit: str, dest: Path) -> bool:
    url = GITHUB_URLS.get(repo)
    if not url:
        url = f"https://github.com/{repo}.git"

    # Try shallow clone at commit first; fall back to full clone
    r = subprocess.run(
        ["git", "clone", "--quiet", url, str(dest)],
        capture_output=True, text=True, timeout=300,
    )
    if r.returncode != 0:
        print(f"    [clone failed] {r.stderr[:200]}")
        return False

    r = subprocess.run(
        ["git", "-C", str(dest), "checkout", commit, "--quiet"],
        capture_output=True, text=True, timeout=60,
    )
    if r.returncode != 0:
        print(f"    [checkout failed] {r.stderr[:200]}")
        return False
    return True


def setup_venv(repo_name: str, repo_dir: Path, version: str = "", python_bin: str = "") -> Optional[Path]:
    """Create uv venv and install package. Returns python path or None."""
    venv_dir = repo_dir / ".venv"
    venv_cmd = ["uv", "venv", str(venv_dir), "--quiet", "--no-seed"]
    if python_bin:
        venv_cmd += ["--python", python_bin]
    r = subprocess.run(venv_cmd, capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        print(f"    [venv failed] {r.stderr[:200]}")
        return None

    python = venv_dir / "bin" / "python"
    # Use 'uv pip install --python <path>' — uv venvs don't include a pip binary
    # Note: --python goes AFTER the subcommand, not before it
    def uv_pip_install(*args):
        return ["uv", "pip", "install", "--python", str(python)] + list(args)

    # Apply version-specific constraints first (werkzeug, pluggy, etc.)
    constraints = VERSION_CONSTRAINTS.get((repo_name, version), [])
    if constraints:
        r = subprocess.run(
            uv_pip_install(*constraints, "--quiet"),
            capture_output=True, text=True, timeout=120, cwd=str(repo_dir),
        )
        if r.returncode != 0:
            print(f"    [constraint install warning] {r.stderr[:200]}")

    extras = INSTALL_EXTRAS.get(repo_name, ["."])

    # Install package.
    # If extras list starts with "-e", treat the whole list as flat packages to install at once.
    # Otherwise each entry is an editable extras spec like ".[dev]".
    if extras and extras[0] == "-e":
        # Flat install: e.g. ["-e", ".", "pytest", "pytest-mock"]
        r = subprocess.run(
            uv_pip_install(*extras, "--quiet"),
            capture_output=True, text=True, timeout=300, cwd=str(repo_dir),
        )
        if r.returncode != 0:
            print(f"    [install failed] {r.stderr[:300]}")
            return None
    else:
        # Editable extras: each entry is like ".[dev]" or ".[testing]"
        for extra in extras:
            r = subprocess.run(
                uv_pip_install("-e", extra, "--quiet"),
                capture_output=True, text=True, timeout=300, cwd=str(repo_dir),
            )
            if r.returncode != 0:
                # Fallback: plain editable without extras spec
                r2 = subprocess.run(
                    uv_pip_install("-e", ".", "--quiet"),
                    capture_output=True, text=True, timeout=300, cwd=str(repo_dir),
                )
                if r2.returncode != 0:
                    print(f"    [install failed] {r.stderr[:300]}")
                    return None
                break

    # Always ensure pytest is available
    subprocess.run(
        uv_pip_install("pytest", "--quiet"),
        capture_output=True, text=True, timeout=60, cwd=str(repo_dir),
    )

    return python


def apply_patch(patch_text: str, repo_dir: Path) -> bool:
    """Apply a unified diff patch to the repo."""
    r = subprocess.run(
        ["git", "apply", "--whitespace=fix", "-"],
        input=patch_text,
        capture_output=True, text=True, cwd=str(repo_dir),
    )
    if r.returncode != 0:
        # Try with --3way
        r2 = subprocess.run(
            ["git", "apply", "--3way", "--whitespace=fix", "-"],
            input=patch_text,
            capture_output=True, text=True, cwd=str(repo_dir),
        )
        return r2.returncode == 0
    return True


def run_tests(test_ids: list[str], repo_dir: Path, python: Path, extra_flags: list[str] | None = None) -> tuple[bool, str]:
    """Run the FAIL_TO_PASS tests. Returns (passed, output)."""
    pytest_bin = python.parent / "pytest"
    if not pytest_bin.exists():
        base_cmd = [str(python), "-m", "pytest"]
    else:
        base_cmd = [str(pytest_bin)]
    cmd = base_cmd + test_ids + ["-x", "--tb=short", "-q"] + (extra_flags or [])
    r = subprocess.run(
        cmd, capture_output=True, text=True, timeout=180, cwd=str(repo_dir),
    )
    return r.returncode == 0, (r.stdout + r.stderr)[-2000:]


def reset_repo(repo_dir: Path):
    """Undo all working-tree changes made by Claude."""
    subprocess.run(
        ["git", "checkout", "."],
        capture_output=True, cwd=str(repo_dir),
    )
    subprocess.run(
        ["git", "clean", "-fd", "--quiet"],
        capture_output=True, cwd=str(repo_dir),
    )


# ------------------------------------------------------------------ #
# Claude runner                                                        #
# ------------------------------------------------------------------ #

VAR_RE = re.compile(r"@\{[^}]+\}")


def build_prompt(condition: str, task: dict, repo_dir: Path, test_cmd: str) -> str:
    problem = task['problem_statement']
    ftp = task['fail_to_pass']
    if isinstance(ftp, str):
        ftp = json.loads(ftp)

    test_note = ("Note: add -p no:hypothesispytest to pytest invocations to avoid a "
                 "stale system plugin interfering with results.")

    if condition == "baseline":
        return f"""You are fixing a bug in the {task['repo']} repository.

Repository location: {repo_dir}
Problem: {problem}

The following tests currently FAIL and must pass after your fix:
{chr(10).join(ftp)}

Test command: {test_cmd}
{test_note}

Instructions:
- Read relevant source files to understand the codebase
- Make the minimal change needed to fix the problem
- Run the tests to verify your fix
- The test files already exist (pre-applied) — do not create them
- Work only in {repo_dir}
"""
    else:
        # Macro condition: same prompt as baseline but with @{VAR} session vars.
        # SKILL.md is injected via --append-system-prompt-file (not embedded here)
        # so it adds zero extra prompt overhead beyond the variable definitions.
        return f"""You are fixing a bug in the {task['repo']} repository.

Repository location: {repo_dir}
Problem: {problem}

Session variables pre-seeded for this task:
  @{{REPO}}     = {repo_dir}
  @{{TEST_CMD}} = {test_cmd}
  @{{TESTS}}    = {" ".join(ftp)}

The following tests currently FAIL and must pass after your fix:
{chr(10).join(ftp)}

{test_note}

Instructions:
- Use @{{REPO}} instead of the full path in every bash command
- Use @{{TEST_CMD}} to run the test suite; @{{TESTS}} for individual test IDs
- Read relevant source files to understand the codebase
- Make the minimal change needed to fix the problem
- The test files already exist (pre-applied) — do not create them
"""


def write_hook_settings(tmp_dir: Path) -> Path:
    """Write a settings.json that wires the bash hook."""
    hook_cmd = f"python3 {INSTALL_DIR / 'bash_hook.py'}"
    settings = {
        "hooks": {
            "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": hook_cmd}]}],
            "PostToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": hook_cmd}]}],
        }
    }
    path = tmp_dir / "hook_settings.json"
    path.write_text(json.dumps(settings))
    return path


def seed_session_state(repo_dir: Path, test_cmd: str, task: dict):
    """Pre-seed /tmp/agent_session.json for the macro condition."""
    ftp = task['fail_to_pass']
    if isinstance(ftp, str):
        ftp = json.loads(ftp)
    state = {
        "user_vars": {
            "REPO": str(repo_dir),
            "TEST_CMD": test_cmd,
            "TESTS": " ".join(ftp),
        },
        "pwd": str(repo_dir),
        "task_root": str(repo_dir),
        "last_output": "",
        "command_history": [],
    }
    SESSION_JSON.write_text(json.dumps(state, indent=2))


def run_claude(condition: str, task: dict, repo_dir: Path, test_cmd: str, max_turns: int) -> dict:
    """
    Run Claude Code with stream-json output. Returns parsed metrics dict.
    """
    prompt = build_prompt(condition, task, repo_dir, test_cmd)

    with tempfile.TemporaryDirectory() as tmpd:
        tmp = Path(tmpd)

        # Write prompt to file
        prompt_file = tmp / "prompt.txt"
        prompt_file.write_text(prompt)

        cmd = [
            CLAUDE_BIN,
            "-p", prompt,
            "--output-format", "stream-json",
            "--verbose",
            "--max-turns", str(max_turns),
            "--allowedTools", "Bash,Read,Write,Edit,MultiEdit",
            "--dangerously-skip-permissions",
        ]

        if condition == "macro":
            # Inject SKILL.md as a system prompt addition (amortized cost, not per-task prompt)
            if SKILL_PATH.exists():
                cmd += ["--append-system-prompt-file", str(SKILL_PATH)]
            # Wire the bash hook
            if INSTALL_DIR.exists():
                settings_path = write_hook_settings(tmp)
                cmd += ["--settings", str(settings_path)]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=600,
            cwd=str(repo_dir),
        )

    return parse_stream_json(result.stdout, result.stderr)


def parse_stream_json(stdout: str, stderr: str) -> dict:
    """
    Parse claude CLI stream-json events to extract:
    - bash commands from assistant tool_use content blocks
    - tool results from user tool_result content blocks
    - full turn-by-turn transcript for training data synthesis
    - total usage tokens from the result event

    Claude CLI stream-json format (v2.x):
      {"type":"assistant","message":{"content":[{"type":"tool_use","name":"Bash","input":{"command":"..."}}],...}}
      {"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":"...","content":"..."}]},...}
      {"type":"result","usage":{"input_tokens":N,...}}
    """
    bash_commands = []
    total_input_tokens = 0
    total_output_tokens = 0
    error = None

    # Full transcript: list of turn dicts for training data synthesis
    # Each turn: {"role": "assistant"|"tool", "content": [...], "tool_use_id": ...}
    transcript: list[dict] = []

    # Track pending tool_use IDs so we can correlate results
    pending_tool_uses: dict[str, str] = {}  # id -> command

    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue

        etype = evt.get("type", "")

        if etype == "assistant":
            msg = evt.get("message", {})
            usage = msg.get("usage", {})
            total_input_tokens += usage.get("input_tokens", 0)
            total_output_tokens += usage.get("output_tokens", 0)

            content_blocks = msg.get("content", [])
            turn_text = ""
            turn_tool_calls = []

            for block in content_blocks:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    turn_text += block.get("text", "")
                elif block.get("type") == "tool_use":
                    tool_id = block.get("id", "")
                    tool_name = block.get("name", "")
                    tool_input = block.get("input", {})
                    if tool_name == "Bash":
                        cmd = tool_input.get("command", "")
                        if cmd:
                            bash_commands.append(cmd)
                            pending_tool_uses[tool_id] = cmd
                    turn_tool_calls.append({
                        "tool_use_id": tool_id,
                        "name": tool_name,
                        "input": tool_input,
                    })

            if turn_text or turn_tool_calls:
                transcript.append({
                    "role": "assistant",
                    "text": turn_text,
                    "tool_calls": turn_tool_calls,
                })

        elif etype == "user":
            # Tool results come back as user messages
            msg = evt.get("message", {})
            for block in msg.get("content", []):
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_result":
                    tool_id = block.get("tool_use_id", "")
                    result_content = block.get("content", "")
                    if isinstance(result_content, list):
                        result_content = " ".join(
                            b.get("text", "") for b in result_content if isinstance(b, dict)
                        )
                    is_error = block.get("is_error", False)
                    # Claude Code stream-json puts tool_use_result at the event top level
                    tool_result_meta = evt.get("tool_use_result", {})
                    if isinstance(tool_result_meta, dict):
                        stdout_out = tool_result_meta.get("stdout", result_content)
                        stderr_out = tool_result_meta.get("stderr", "")
                    else:
                        stdout_out = result_content
                        stderr_out = ""
                    output = stdout_out
                    if stderr_out:
                        output = (output + "\n" + stderr_out).strip()

                    transcript.append({
                        "role": "tool",
                        "tool_use_id": tool_id,
                        "command": pending_tool_uses.get(tool_id, ""),
                        "output": output[:4000],  # cap for storage
                        "is_error": is_error,
                    })

        elif etype == "result":
            subtype = evt.get("subtype", "")
            if subtype and subtype != "success":
                error = subtype
            usage = evt.get("usage", {})
            if usage:
                total_input_tokens = usage.get("input_tokens", total_input_tokens)
                total_output_tokens = usage.get("output_tokens", total_output_tokens)

    if not stdout.strip() and stderr:
        error = stderr[:300]

    return {
        "bash_commands": bash_commands,
        "total_input_tokens": total_input_tokens,
        "total_output_tokens": total_output_tokens,
        "transcript": transcript,
        "error": error,
    }


# ------------------------------------------------------------------ #
# Main runner                                                          #
# ------------------------------------------------------------------ #

def run_task(task: dict, condition: str, max_turns: int) -> tuple["TaskResult", list[dict]]:
    """Returns (TaskResult, transcript). Transcript is the raw turn-by-turn list for synthesis."""
    result = TaskResult(task_id=task['task_id'], condition=condition)
    saved_transcript: list[dict] = []
    start = time.time()

    repo = task['repo']
    repo_name = repo.split('/')[1]
    ftp = task['fail_to_pass']
    if isinstance(ftp, str):
        ftp = json.loads(ftp)

    with tempfile.TemporaryDirectory(prefix=f"swe_{task['task_id'][:20]}_") as tmpd:
        repo_dir = Path(tmpd) / repo_name
        print(f"    cloning {repo} @ {task['base_commit'][:8]} ...", flush=True)

        if not clone_repo(repo, task['base_commit'], repo_dir):
            result.error = "clone failed"
            result.duration_seconds = round(time.time() - start, 1)
            return result, saved_transcript

        print(f"    setting up venv ...", flush=True)
        python_bin = REPO_PYTHON.get(repo_name, "")
        python = setup_venv(repo_name, repo_dir, task.get("version", ""), python_bin)
        if not python:
            result.error = "venv/install failed"
            result.duration_seconds = round(time.time() - start, 1)
            return result, saved_transcript

        print(f"    applying test patch ...", flush=True)
        if not apply_patch(task['test_patch'], repo_dir):
            result.error = "test_patch apply failed"
            result.duration_seconds = round(time.time() - start, 1)
            return result, saved_transcript

        # Verify tests fail before Claude runs
        # Build test command for prompts and seed
        pytest_bin = python.parent / "pytest"
        # Only pytest's own test suite needs -p no:hypothesispytest (stale system plugin)
        if repo_name == "pytest":
            repo_flags = "-p no:hypothesispytest"
        elif repo_name == "requests":
            repo_flags = "--timeout=10"
        else:
            repo_flags = ""
        if pytest_bin.exists():
            test_cmd = f"{pytest_bin} {' '.join(ftp)} -x -q {repo_flags}".strip()
        else:
            test_cmd = f"{python} -m pytest {' '.join(ftp)} -x -q {repo_flags}".strip()
        before_pass, before_out = run_tests(ftp, repo_dir, python)
        if before_pass:
            print(f"    WARNING: tests pass before fix — task may be trivial or already patched")

        # Repo-specific extra flags for the success check after Claude runs
        # flask 2.0 uses filterwarnings=error in setup.cfg; on Python 3.12+ pkgutil.get_loader
        # triggers a DeprecationWarning in collection. Override it for our scoring run.
        if repo_name == "pytest":
            extra_test_flags = ["-p", "no:hypothesispytest"]
        elif repo_name == "flask":
            extra_test_flags = ["-W", "ignore::DeprecationWarning"]
        elif repo_name == "requests":
            extra_test_flags = ["--timeout=10"]
        else:
            extra_test_flags = []

        # Seed session state for macro condition
        if condition == "macro":
            seed_session_state(repo_dir, test_cmd, task)

        print(f"    running Claude ({condition}) ...", flush=True)
        claude_out = run_claude(condition, task, repo_dir, test_cmd, max_turns)

        if claude_out.get("error"):
            result.error = claude_out["error"]

        cmds = claude_out.get("bash_commands", [])
        transcript = claude_out.get("transcript", [])
        saved_transcript = transcript
        result.bash_commands = cmds
        result.total_bash_calls = len(cmds)
        result.tool_call_chars = sum(len(c) for c in cmds)
        result.tool_call_tokens_est = result.tool_call_chars // 4
        result.total_input_tokens = claude_out.get("total_input_tokens", 0)
        result.total_output_tokens = claude_out.get("total_output_tokens", 0)

        # For macro condition, find @{VAR} references in commands
        if condition == "macro":
            found = set()
            for c in cmds:
                found.update(VAR_RE.findall(c))
            result.macro_expansions = sorted(found)

        # Score success
        passed, test_out = run_tests(ftp, repo_dir, python, extra_test_flags)
        result.success = passed
        if not passed:
            print(f"    tests FAILED:\n      {test_out[-400:]}")

    result.duration_seconds = round(time.time() - start, 1)
    return result, saved_transcript


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tasks", default="/tmp/swe_selected.json")
    parser.add_argument("--conditions", default="baseline,macro")
    parser.add_argument("--max-turns", type=int, default=15)
    parser.add_argument("--task-filter", default=None, help="Run only this task_id")
    args = parser.parse_args()

    conditions = [c.strip() for c in args.conditions.split(",")]
    tasks = json.loads(Path(args.tasks).read_text())
    if args.task_filter:
        tasks = [t for t in tasks if args.task_filter in t['task_id']]

    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    all_results: list[TaskResult] = []

    for task in tasks:
        print(f"\n{'='*60}")
        print(f"Task: {task['task_id']}")
        ftp = task['fail_to_pass']
        if isinstance(ftp, str):
            ftp = json.loads(ftp)
        print(f"Tests: {ftp}")

        for condition in conditions:
            print(f"\n  -- condition: {condition} --")
            result, transcript = run_task(task, condition, args.max_turns)

            status = "PASS" if result.success else ("ERR:" + (result.error or "?") if result.error else "FAIL")
            print(f"  result: {status} | bash calls: {result.total_bash_calls} | "
                  f"cmd chars: {result.tool_call_chars} (~{result.tool_call_tokens_est} tok) | "
                  f"total input tok: {result.total_input_tokens} | "
                  f"{result.duration_seconds}s")
            if result.macro_expansions:
                print(f"  macro expansions used: {result.macro_expansions}")

            log_path = LOGS_DIR / f"{task['task_id']}_{condition}.json"
            log_path.write_text(json.dumps(asdict(result), indent=2))

            # Save full transcript for training data synthesis (baseline condition)
            # Save regardless of success — synthesize.py filters on success field
            if condition == "baseline" and transcript:
                tpath = LOGS_DIR / "transcripts" / f"{task['task_id']}_baseline.json"
                tpath.parent.mkdir(parents=True, exist_ok=True)
                tpath.write_text(json.dumps({
                    "task_id": task['task_id'],
                    "repo": task['repo'],
                    "version": task.get('version', ''),
                    "problem_statement": task.get('problem_statement', ''),
                    "fail_to_pass": task['fail_to_pass'],
                    "prompt": build_prompt("baseline", task, Path("/PLACEHOLDER"), "pytest"),
                    "transcript": transcript,
                    "bash_commands": result.bash_commands,
                    "success": result.success,
                }, indent=2))
            all_results.append(result)

    # Summary
    print(f"\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")

    baseline = [r for r in all_results if r.condition == "baseline"]
    macro = [r for r in all_results if r.condition == "macro"]

    def avg(lst, key):
        vals = [getattr(r, key) for r in lst if not r.error]
        return round(sum(vals) / len(vals), 1) if vals else 0

    if baseline and macro:
        b_chars = avg(baseline, 'tool_call_chars')
        m_chars = avg(macro, 'tool_call_chars')
        b_calls = avg(baseline, 'total_bash_calls')
        m_calls = avg(macro, 'total_bash_calls')
        b_pass = sum(1 for r in baseline if r.success)
        m_pass = sum(1 for r in macro if r.success)
        b_intok = avg(baseline, 'total_input_tokens')
        m_intok = avg(macro, 'total_input_tokens')
        char_reduction = round((1 - m_chars / b_chars) * 100, 1) if b_chars > 0 else 0
        tok_reduction = round((1 - m_intok / b_intok) * 100, 1) if b_intok > 0 else 0

        print(f"\n{'Metric':<35} {'Baseline':>10} {'Macro':>10}")
        print(f"{'-'*55}")
        print(f"{'Avg bash cmd chars (tool-call proxy)':<35} {b_chars:>10} {m_chars:>10}")
        print(f"{'Avg bash calls':<35} {b_calls:>10} {m_calls:>10}")
        print(f"{'Avg total input tokens':<35} {b_intok:>10} {m_intok:>10}")
        print(f"{'Tasks passed':<35} {b_pass:>10} {m_pass:>10}")
        print(f"{'Cmd-char reduction %':<35} {'':>10} {char_reduction:>9}%")
        print(f"{'Total input token reduction %':<35} {'':>10} {tok_reduction:>9}%")

        summary = {
            "baseline": {"avg_cmd_chars": b_chars, "avg_bash_calls": b_calls,
                         "avg_input_tokens": b_intok, "tasks_passed": b_pass},
            "macro": {"avg_cmd_chars": m_chars, "avg_bash_calls": m_calls,
                      "avg_input_tokens": m_intok, "tasks_passed": m_pass,
                      "cmd_char_reduction_pct": char_reduction,
                      "input_token_reduction_pct": tok_reduction},
            "tasks": [asdict(r) for r in all_results],
        }
        (LOGS_DIR / "summary.json").write_text(json.dumps(summary, indent=2))
        print(f"\nLogs: {LOGS_DIR}/")


if __name__ == "__main__":
    main()
