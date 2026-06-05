# Macro Compression — Session Variable System

## What it does

Use `@{VAR}` syntax in any bash command to substitute named values. The hook
expands them before execution, saving tokens on repeated paths and commands.

## Setting variables

```
macro-set REPO /home/user/projects/myapp
macro-set TEST_CMD "pytest tests/ -x --tb=short"
macro-set BRANCH feature/auth-fix
```

List all current session vars:
```
macro-list
```

Remove a var:
```
macro-unset VAR_NAME
```

## Using variables

Once set, write `@{VAR}` anywhere in a bash command:

```bash
cd @{REPO} && git status
@{TEST_CMD} --lf
grep -r "TODO" @{REPO}/src
```

## Built-in variables (no setup needed)

| Variable | Contents |
|----------|----------|
| `@{PWD}` | Current working directory (updated after every command) |
| `@{TaskRoot}` | Working directory at session start (never changes) |
| `@{LastOutput}` | stdout+stderr of the most recent command (truncated at 2000 chars) |
| `@{LastCommand[n]}` | The nth most recent command string (1 = most recent, max 20) |

Built-ins are free — no `macro-set` needed. Use them liberally.

## Undefined variable behavior

If you reference an undefined `@{VAR}`, the command is **rejected** — not
executed. You'll see:

```
MacroError: @{FOO} is not defined. Use `macro-set FOO <value>` to define it.
```

No silent failures. Fix the var name or run `macro-set` first.

## When to define a user var

User vars have a small setup cost (`macro-set` + cognitive overhead).
Only define one if you expect to use it **3 or more times**.
For one-off or two-use paths, just type them inline.

Built-ins (`@{PWD}`, `@{LastOutput}`, etc.) are always free — use them
without hesitation.

## Examples

```bash
# Navigate to repo consistently across many commands
macro-set REPO /workspace/django-bug-12345
cd @{REPO}
grep -rn "def authenticate" @{REPO}/auth/
git -C @{REPO} log --oneline -10

# Reference last output without re-running
cat /var/log/app.log
# ... later ...
echo "@{LastOutput}" | grep ERROR

# Parameterize test runs
macro-set TEST "python -m pytest tests/auth/ -x -q"
@{TEST}
@{TEST} --tb=long
@{TEST} -k test_login

# Return to where you started
cd @{TaskRoot}
```
