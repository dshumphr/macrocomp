# macrocomp — Session Macro Compression for LLM Agents

A session-level variable and macro substitution system for LLM tool-use agents. Reduces token waste from repeated boilerplate in bash tool invocations — common in coding agents, SWE-bench-style tasks, and any agentic workflow with a fixed environment.

Targets Claude Code via its native hook system. Portable to other harnesses with minimal changes.

---

## What it does

Instead of this (repeated across dozens of tool calls):

```bash
cd /home/user/projects/django-bugfix-12345 && pytest tests/auth/ -x --tb=short
grep -rn "authenticate" /home/user/projects/django-bugfix-12345/auth/
git -C /home/user/projects/django-bugfix-12345 log --oneline -10
```

The agent writes this:

```bash
cd @{REPO} && @{TEST_CMD}
grep -rn "authenticate" @{REPO}/auth/
git -C @{REPO} log --oneline -10
```

The hook expands `@{VAR}` references before execution. The agent never sees raw paths in tool call arguments again.

---

## Syntax

Use `@{VAR_NAME}` for all substitutions — both simple values and command fragments.

- Does not conflict with `$VAR` or `${VAR}` bash syntax
- Unambiguous inside shell strings, backticks, and heredocs
- Easy for a model to learn in one read

---

## Variable types

### User-defined session variables

Set explicitly by the agent:

```bash
macro-set REPO /home/user/projects/myapp
macro-set TEST_CMD "pytest tests/ -x --tb=short"
macro-set BRANCH feature/auth-fix
```

### Built-in auto-populated variables

Maintained automatically by the hook — no setup needed:

| Variable | Contents |
|----------|----------|
| `@{PWD}` | Current working directory (updated after every command) |
| `@{TaskRoot}` | Working directory at session start (immutable) |
| `@{LastOutput}` | stdout+stderr of the most recent command (truncated at 2000 chars) |
| `@{LastCommand[n]}` | The nth most recent command string (1 = most recent, max 20) |

Built-ins cannot be overwritten. Attempting to do so raises a clear error.

---

## Behavior

**Undefined variable → command rejected.** If the agent writes `@{UNDEFINED_VAR}`, the hook blocks execution entirely and returns:

```
MacroError: @{UNDEFINED_VAR} is not defined. Use `macro-set UNDEFINED_VAR <value>` to define it, or `macro-list` to see current session vars.
```

No silent failures where a literal `@{FOO}` string runs as a command argument.

**Recursive expansion up to depth 2.** A variable whose value contains `@{OTHER}` is expanded one more time. Depth 3+ raises `RecursiveExpansionError`.

**Expansion is silent.** The agent writes `@{VAR}` forms; the hook expands and executes without echoing back expanded values.

---

## File structure

```
macro-compression/
  session_state.py    # in-memory store + file backing + expansion engine
  bash_hook.py        # Claude Code PreToolUse/PostToolUse hook
  macro_cli.py        # macro-set / macro-list / macro-unset CLI
  SKILL.md            # model-facing instructions (<400 tokens)
  install.sh          # idempotent install into ~/.claude/
  test_harness.py     # SWE-bench sample runner + metrics logger
  tests.py            # 24 unit tests for core logic
  logs/               # per-task JSON output from test_harness
```

---

## Install

```bash
cd macro-compression
bash install.sh
```

This:
1. Copies source files to `~/.claude/macro-compression/`
2. Creates `macro-set`, `macro-list`, `macro-unset` symlinks in `~/.local/bin/`
3. Registers `PreToolUse` and `PostToolUse` hooks in `~/.claude/settings.json` (merges safely — does not clobber existing hooks)
4. Initializes `/tmp/agent_session.json`

Idempotent — safe to run twice.

For project-local hooks instead of global:

```bash
bash install.sh --project
```

---

## CLI usage

```bash
# Define a session variable
macro-set REPO /home/user/projects/myapp
macro-set TEST_CMD "pytest tests/ -x --tb=short"

# List all current session variables (+ built-in reference)
macro-list

# Remove a variable
macro-unset BRANCH
```

---

## Claude Code hook protocol

The hook uses Claude Code's native PreToolUse/PostToolUse system:

**PreToolUse** (Bash matcher):
- Reads `tool_input.command` from stdin JSON
- Expands `@{VAR}` references
- On success: returns `{"tool_input": {"command": "<expanded>"}}` — Claude Code executes the expanded command
- On undefined var: returns `{"decision": "block", "reason": "MacroError: ..."}` with exit 1 — command is not executed

**PostToolUse** (Bash matcher):
- Appends original command to history (`@{LastCommand[n]}`)
- Stores truncated output as `@{LastOutput}`
- Updates `@{PWD}`

---

## Session state persistence

State is written to `/tmp/agent_session.json` on every mutation, making it inspectable at any point:

```json
{
  "user_vars": {"REPO": "/home/user/myapp", "TEST_CMD": "pytest tests/"},
  "pwd": "/home/user/myapp/src",
  "task_root": "/home/user/myapp",
  "last_output": "...",
  "command_history": ["pytest tests/", "git status", "ls"]
}
```

---

## Running tests

```bash
cd macro-compression
python3 tests.py
```

24 unit tests covering:
- Set/get/unset/list for user vars
- Built-in write protection
- Expansion: simple, multiple vars, recursive depth 2, depth 3 rejection
- Built-in resolution: PWD, TaskRoot, LastOutput, LastCommand[n]
- History capping at 20, LastOutput truncation at 2000 chars
- Disk persistence across SessionState instances
- Hook pre-tool-use: expansion, block on undefined, macro-cli bypass
- Hook post-tool-use: state update

---

## Evaluation (test_harness.py)

Run mock tasks under baseline and macro conditions and compare token usage:

```bash
cd macro-compression
python3 test_harness.py --tasks 3
```

Outputs per-task JSON logs to `logs/` and a `logs/summary.json`:

```json
{
  "task_id": "mock_001",
  "condition": "macro",
  "tool_call_tokens": 42,
  "total_calls": 8,
  "success": true,
  "macro_expansions": ["@{REPO}", "@{TEST_CMD}"]
}
```

For full SWE-bench Lite evaluation, provide a tasks JSON file:

```bash
python3 test_harness.py --tasks-file swebench_lite_tasks.json --repo-base /path/to/repos
```

Expected outcome on repetitive repo navigation tasks: 20–50% reduction in tool-call input tokens.

---

## When to define a user var

User vars have a setup cost (`macro-set` call). Only define one if you expect to use it **3 or more times**. For one-off paths, type them inline.

Built-ins (`@{PWD}`, `@{LastOutput}`, etc.) are always free — use them without hesitation.

---

## License

MIT
