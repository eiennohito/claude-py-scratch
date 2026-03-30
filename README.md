# py-scratch

MCP server that gives Claude Code a `run_python_script` tool, replacing `Bash(python -c "...")` for anything non-trivial.

Code goes in as a structured string field -- no shell escaping, no quote hell, no 200-line one-liners.

If launched from a directory with a single `pyproject.toml` project, scripts automatically run in that project's virtualenv -- `from myproj import ...` just works, no dependency declarations needed.

Zero dependencies. The MCP protocol (JSON-RPC over stdio) is implemented directly.

> This project (including this README) is written with LLM assistance.

## Install

Requires [uv](https://docs.astral.sh/uv/).

### As a Claude Code plugin

```bash
claude plugin add /path/to/py-scratch
```

This registers the MCP server, hook, and skill in one step:

- **MCP server** -- `run_python_script` tool, registered via `.mcp.json`
- **Hook** -- rejects `Bash(python -c ...)` when code exceeds ~2 lines, tells the agent to use `run_python_script`
- **Skill** -- teaches the agent when to use the tool vs plain `python -c`

### Manual (MCP server only)

Add to `.mcp.json` (project) or `~/.claude.json` (global):

```json
{
  "mcpServers": {
    "py-scratch": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/py-scratch", "py-scratch"]
    }
  }
}
```

This gives you the tool but not the hook or skill.

## Tool

### `run_python_script`

```
run_python_script(
  code: str,               # raw Python source
  dependencies: str[],     # optional PyPI package names
  timeout: int = 30,       # seconds
  head: int = 0,           # lines from start of output in preview
  tail: int = 5            # lines from end of output in preview
)
```

Returns:

```json
{
  "execution_id": "c46be57f66de",
  "exit_code": 0,
  "duration_ms": 457,
  "stdout_preview": "...",
  "stderr_preview": "",
  "stdout_path": "/tmp/py-scratch-executions/c46be57f66de/stdout.log",
  "stderr_path": "/tmp/py-scratch-executions/c46be57f66de/stderr.log"
}
```

By default, the last 5 lines of each stream are returned as preview. Use `head`/`tail` to control what's included. Full output at the file paths -- the agent uses its own Read/Grep tools when it needs more.

**Dependency auto-resolution:** if the script fails with `ModuleNotFoundError` and no deps were declared, the server infers the package name (handling mismatches like `cv2` -> `opencv-python`, `PIL` -> `Pillow`) and retries automatically.

**How it runs:**
1. Writes code to `{output_dir}/{execution_id}/script.py` with PEP 723 inline metadata for any declared deps
2. Executes via `uv run`, stdout/stderr streamed directly to files
3. Timeout enforced via subprocess

## Project context

When launched from a directory containing a Python project, py-scratch automatically detects it and runs every script in that project's virtualenv. The script gets access to all of the project's dependencies without declaring them.

### Auto-detection

On startup, py-scratch scans the working directory and its subdirectories (up to 3 levels deep) for `pyproject.toml` files with a `[project]` table.

- **Exactly 1 project found:** scripts run with `uv run --project <path>`, inheriting the project's full dependency tree and lockfile.
- **0 or 2+ projects found:** no project context is set. The tool description warns the agent to tell the user to create a config file.

Directories like `.git`, `.venv`, `node_modules`, `__pycache__`, `build`, `dist` are skipped during scanning.

### Manual configuration: `.py-scratch.json`

For workspaces with multiple projects, or to point at a project outside the scan depth, create `.py-scratch.json` in the working directory:

```json
{
  "project": "./path/to/main-project",
  "packages": ["./path/to/extra-lib"]
}
```

- `project` -- sets `--project` (the virtualenv context, at most one)
- `packages` -- adds `--with` flags (extra packages on top)
- When present, `.py-scratch.json` replaces auto-detection entirely
- `{"packages": []}` suppresses all injection

### What this means for scripts

With a project context set, the agent can write `from myproj import ...` in any py-scratch script with zero boilerplate -- no PEP 723 metadata, no dependency declarations.

## Output persistence

Each execution produces:

```
/tmp/py-scratch-executions/{execution_id}/
  script.py    # the code that ran
  stdout.log   # full stdout
  stderr.log   # full stderr
  meta.json    # exit code, duration, deps, timestamp
```

Override the base directory with `PY_SCRATCH_OUTPUT_DIR`.

## Development

```bash
uv sync
uv run python test_server.py
```
