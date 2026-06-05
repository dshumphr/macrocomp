"""
synthesize.py — Convert baseline SWE-bench trajectories into LoRA training examples.

Takes JSON transcripts saved by swe_runner.py (logs/transcripts/*.json) and
produces JSONL training examples in Qwen2.5-Instruct chat format, where every
eligible path occurrence is replaced with @{REPO} and every repeated pytest
invocation is replaced with @{TEST_CMD}.

Usage:
    python3 synthesize.py \\
        --transcripts logs/transcripts/ \\
        --output data/output/train.jsonl \\
        --min-substitutions 3 \\
        --min-calls 4

Output format (one JSON object per line):
    {
        "source": "psf__requests-2148",
        "messages": [
            {"role": "system", "content": "..."},
            {"role": "user", "content": "..."},
            {"role": "assistant", "content": "<tool_call>...</tool_call>"},
            {"role": "tool", "content": "..."},
            ...
        ],
        "substitutions": 8,
        "vars_used": ["@{REPO}", "@{TEST_CMD}"]
    }
"""

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Optional

# ------------------------------------------------------------------ #
# Qwen2.5 chat format constants                                        #
# ------------------------------------------------------------------ #

SYSTEM_PROMPT = """\
You are an expert software engineering assistant. You fix bugs in real GitHub repositories.

When working in a repository session, use session variables to avoid repeating long paths:
- Use @{REPO} instead of the full repository directory path in every bash command.
- Use @{TEST_CMD} for repeated test suite invocations.
- Use @{TESTS} when referencing specific test IDs more than once.
- Use @{LastOutput} to reference the output of the previous command without re-running it.

These variables are pre-seeded at session start. Use them from your very first command.\
"""

# Qwen2.5 tool-call wrapper format (matches the model's native format)
TOOL_CALL_TEMPLATE = '<tool_call>\n{{"name": "Bash", "arguments": {{"command": {cmd_json}}}}}\n</tool_call>'
TOOL_RESULT_TEMPLATE = '<tool_response>\n{output}\n</tool_response>'

# ------------------------------------------------------------------ #
# Path and command detection                                           #
# ------------------------------------------------------------------ #

# Matches the swe_runner tmpdir path ending in the repo name
REPO_PATH_RE = re.compile(r'/\S+/swe_[^/\s]+/\w+')

# Matches a pytest invocation (the runnable part)
PYTEST_RE = re.compile(
    r'(?:\.venv/bin/pytest|(?:/[^\s]+)?(?:python\S*)\s+-m\s+pytest)\s+[^\n]+'
)


def extract_repo_path(commands: list[str]) -> Optional[str]:
    """Find the canonical repo tmpdir path from a list of bash commands."""
    for cmd in commands:
        m = REPO_PATH_RE.search(cmd)
        if m:
            return m.group(0)
    return None


def make_macro_prompt(problem: str, repo: str, ftp: list[str],
                      repo_placeholder: str, test_cmd_placeholder: str) -> str:
    """Build the user prompt for the macro-enabled training example."""
    tests_str = " ".join(ftp)
    return f"""\
You are fixing a bug in the {repo} repository.

Repository location: {repo_placeholder}

Session variables pre-seeded for this task:
  @{{REPO}}     = {repo_placeholder}
  @{{TEST_CMD}} = {test_cmd_placeholder}
  @{{TESTS}}    = {tests_str}

Problem:
{problem}

The following tests currently FAIL and must pass after your fix:
{chr(10).join(ftp)}

Instructions:
- Use @{{REPO}} instead of the full path in every bash command — from your very first command.
- Use @{{TEST_CMD}} to run the test suite. Use @{{TESTS}} for individual test IDs.
- Read relevant source files to understand the codebase.
- Make the minimal change needed to fix the problem.
- The test files already exist (pre-applied) — do not create them.\
"""


# ------------------------------------------------------------------ #
# Command rewriting                                                    #
# ------------------------------------------------------------------ #

def rewrite_command(cmd: str, repo_path: str, test_cmd_value: str) -> tuple[str, int]:
    """
    Replace repo_path with @{REPO} and test invocations with @{TEST_CMD}.
    Returns (rewritten_cmd, n_substitutions).
    """
    n = 0
    result = cmd

    # Replace repo path occurrences
    occurrences = result.count(repo_path)
    if occurrences:
        result = result.replace(repo_path, "@{REPO}")
        n += occurrences

    # Replace pytest invocations with @{TEST_CMD}
    # Only replace if the invocation matches the seeded test_cmd closely
    # (avoid replacing pytest calls that use different flags)
    if test_cmd_value and PYTEST_RE.search(result):
        # Extract just the pytest part from the rewritten command
        def replace_pytest(m: re.Match) -> str:
            nonlocal n
            full_match = m.group(0)
            # Only substitute if the test IDs overlap with the seeded tests
            # (avoid replacing pytest calls on different tests)
            if "@{REPO}" in result or repo_path in full_match or "@{TESTS}" in full_match:
                n += 1
                return "@{TEST_CMD}"
            return full_match
        result = PYTEST_RE.sub(replace_pytest, result)

    return result, n


def build_test_cmd(repo_path: str, ftp: list[str], commands: list[str]) -> str:
    """
    Infer what @{TEST_CMD} should expand to from the baseline commands.
    Uses the first pytest invocation that includes any of the FTP test IDs.
    Falls back to a generic pattern.
    """
    ftp_short = [t.split("::")[-1] for t in ftp]
    for cmd in commands:
        if ".venv/bin/pytest" in cmd or "-m pytest" in cmd:
            if any(t in cmd for t in ftp + ftp_short):
                # Replace the repo path so the value is portable
                return cmd.replace(repo_path, "@{REPO}")
    # Fallback: construct from ftp list
    return f"{repo_path}/.venv/bin/pytest {' '.join(ftp)} -x -q".replace(repo_path, "@{REPO}")


# ------------------------------------------------------------------ #
# Training example construction                                        #
# ------------------------------------------------------------------ #

def build_training_example(
    transcript_data: dict,
    min_substitutions: int = 3,
    min_calls: int = 4,
) -> Optional[dict]:
    """
    Convert a baseline transcript into a LoRA training example.
    Returns None if the transcript doesn't meet quality filters.
    """
    task_id = transcript_data["task_id"]
    repo = transcript_data["repo"]
    problem = transcript_data.get("problem_statement", "")
    ftp_raw = transcript_data["fail_to_pass"]
    ftp = json.loads(ftp_raw) if isinstance(ftp_raw, str) else ftp_raw
    commands = transcript_data.get("bash_commands", [])
    transcript = transcript_data.get("transcript", [])

    # Must have passed and have enough calls
    if not transcript_data.get("success"):
        return None
    bash_calls = [t for t in transcript if t["role"] == "assistant"
                  and any(tc["name"] == "Bash" for tc in t.get("tool_calls", []))]
    if len(bash_calls) < min_calls:
        return None

    # Extract repo path
    repo_path = extract_repo_path(commands)
    if not repo_path:
        return None

    # Infer test_cmd value
    test_cmd_value = build_test_cmd(repo_path, ftp, commands)

    # Build the rewritten command sequence and count total substitutions
    total_subs = 0
    rewritten_commands = []
    for cmd in commands:
        rewritten, n = rewrite_command(cmd, repo_path, test_cmd_value)
        rewritten_commands.append(rewritten)
        total_subs += n

    if total_subs < min_substitutions:
        return None

    # Build the Qwen2.5 chat messages
    # We use a placeholder for the repo path in the prompt (it will vary at inference time)
    # The actual @{REPO} value is what the hook will substitute
    placeholder_path = f"/workspace/{repo.split('/')[-1]}"
    user_prompt = make_macro_prompt(problem, repo, ftp, placeholder_path, test_cmd_value)

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]

    # Replay the transcript with rewritten commands
    cmd_idx = 0  # index into rewritten_commands
    vars_used: set[str] = set()

    for turn in transcript:
        role = turn["role"]

        if role == "assistant":
            parts = []
            # Reasoning text (if any)
            text = turn.get("text", "").strip()
            if text:
                parts.append(text)

            # Tool calls
            for tc in turn.get("tool_calls", []):
                if tc["name"] == "Bash":
                    if cmd_idx < len(rewritten_commands):
                        rewritten_cmd = rewritten_commands[cmd_idx]
                        cmd_idx += 1
                    else:
                        rewritten_cmd = tc["input"].get("command", "")

                    # Track which vars were actually used
                    vars_used.update(re.findall(r"@\{[^}]+\}", rewritten_cmd))

                    cmd_json = json.dumps(rewritten_cmd)
                    parts.append(
                        f'<tool_call>\n{{"name": "Bash", "arguments": {{"command": {cmd_json}}}}}\n</tool_call>'
                    )
                elif tc["name"] in ("Read", "Edit", "Write", "MultiEdit"):
                    # Preserve non-Bash tool calls but rewrite any paths in them
                    inp = tc["input"]
                    # Rewrite path fields
                    for field in ("path", "file_path"):
                        if field in inp and repo_path in str(inp[field]):
                            inp = dict(inp)
                            inp[field] = inp[field].replace(repo_path, "@{REPO}")
                    cmd_json = json.dumps(inp)
                    parts.append(
                        f'<tool_call>\n{{"name": "{tc["name"]}", "arguments": {cmd_json}}}\n</tool_call>'
                    )

            if parts:
                messages.append({
                    "role": "assistant",
                    "content": "\n\n".join(parts),
                })

        elif role == "tool":
            output = turn.get("output", "").strip()
            messages.append({
                "role": "tool",
                "content": output,
                "tool_call_id": turn.get("tool_use_id", ""),
            })

    # Final validation: must end with a passing state (assistant turn after last tool)
    if not messages or messages[-1]["role"] == "tool":
        return None

    return {
        "source": task_id,
        "repo": repo,
        "messages": messages,
        "substitutions": total_subs,
        "vars_used": sorted(vars_used),
        "n_calls": len(commands),
        "rewritten_commands": rewritten_commands,
    }


# ------------------------------------------------------------------ #
# CLI                                                                  #
# ------------------------------------------------------------------ #

def main():
    parser = argparse.ArgumentParser(description="Synthesize LoRA training data from baseline transcripts")
    parser.add_argument("--transcripts", default="logs/transcripts",
                        help="Directory of transcript JSON files from swe_runner.py")
    parser.add_argument("--output", default="data/output/train.jsonl",
                        help="Output JSONL file for training examples")
    parser.add_argument("--min-substitutions", type=int, default=3,
                        help="Minimum @{VAR} substitutions required to include example")
    parser.add_argument("--min-calls", type=int, default=4,
                        help="Minimum bash calls required to include example")
    parser.add_argument("--stats", action="store_true",
                        help="Print detailed stats per example")
    args = parser.parse_args()

    transcript_dir = Path(args.transcripts)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    transcript_files = sorted(transcript_dir.glob("*.json"))
    if not transcript_files:
        print(f"No transcript files found in {transcript_dir}", file=sys.stderr)
        sys.exit(1)

    print(f"Found {len(transcript_files)} transcript files")

    total = 0
    included = 0
    skipped_success = 0
    skipped_min_calls = 0
    skipped_min_subs = 0
    skipped_no_path = 0

    with output_path.open("w") as out:
        for fpath in transcript_files:
            data = json.loads(fpath.read_text())
            total += 1

            if not data.get("success"):
                skipped_success += 1
                continue

            commands = data.get("bash_commands", [])
            if len([c for c in commands]) < args.min_calls:
                skipped_min_calls += 1
                continue

            if not extract_repo_path(commands):
                skipped_no_path += 1
                continue

            example = build_training_example(
                data,
                min_substitutions=args.min_substitutions,
                min_calls=args.min_calls,
            )

            if example is None:
                skipped_min_subs += 1
                continue

            out.write(json.dumps(example) + "\n")
            included += 1

            if args.stats:
                print(f"  {example['source']}: {example['substitutions']} subs, "
                      f"{example['n_calls']} calls, vars={example['vars_used']}")

    print()
    print(f"Results:")
    print(f"  Total transcripts:     {total}")
    print(f"  Included:              {included}")
    print(f"  Skipped (failed task): {skipped_success}")
    print(f"  Skipped (too few calls): {skipped_min_calls}")
    print(f"  Skipped (no repo path):  {skipped_no_path}")
    print(f"  Skipped (too few subs):  {skipped_min_subs}")
    print()
    print(f"Output: {output_path.resolve()}")
    if included > 0:
        print(f"  {included} training examples ready for LoRA fine-tuning")


if __name__ == "__main__":
    main()
