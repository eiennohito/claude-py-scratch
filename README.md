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
- **Hooks**
  - rejects `Bash(python -c ...)` when code exceeds ~2 lines, tells the agent to use `run_python_script`
  - injects a short usage instruction at session start
  - hands the real session identity to the MCP server, so execution artifacts land in the right session directory (see [How the session is identified](#how-the-session-is-identified))
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
  intent: str,             # what the script is for -- required
  code: str,               # raw Python source -- required
  dependencies: str[],     # optional PyPI package names
  timeout: int = 30,       # seconds
  head: int = 0,           # lines from start of output in preview
  tail: int = 5            # lines from end of output in preview
)
```

Returns:

```json
{
  "execution_id": "0007",
  "exit_code": 0,
  "status": "ok",
  "duration_ms": 457,
  "stdout_preview": "...",
  "stderr_preview": "",
  "stdout_path": "/tmp/pyscratch/myproj-a1b2c3/2026-08-26-14-27-13-3513357/0007/stdout.log",
  "stderr_path": "/tmp/pyscratch/myproj-a1b2c3/2026-08-26-14-27-13-3513357/0007/stderr.log"
}
```

`status` is one of `ok`, `failed`, `timeout`, `crashed`, `not_started`; anything
other than `ok` also carries an `explanation`. See [Reliability](#reliability).

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
  "packages": ["./path/to/extra-lib"],
  "scratch_dir": "/path/for/execution/artifacts"
}
```

- `project` -- sets `--project` (the virtualenv context, at most one)
- `packages` -- adds `--with` flags (extra packages on top)
- `scratch_dir` -- optional, overrides where execution artifacts are written
- When present, `.py-scratch.json` replaces auto-detection entirely
- `{"packages": []}` suppresses all injection

### What this means for scripts

With a project context set, the agent can write `from myproj import ...` in any py-scratch script with zero boilerplate -- no PEP 723 metadata, no dependency declarations.

## Output persistence

Each execution produces:

```
{artifact_root}/{execution_id}/
  script.py    # the code that ran
  stdout.log   # full stdout
  stderr.log   # full stderr
  meta.json    # exit code, status, duration, deps, timestamp
```

### Where artifacts go

Resolved per call, first match wins:

1. `PY_SCRATCH_DIR` environment variable
2. `scratch_dir` in `.py-scratch.json`
3. **The Claude Code session directory**, when running under Claude Code:
   `<session scratchpad>/py-scratch/{run_id}/` — so script output lands beside the
   session's other working files and is cleaned up with them
4. `{tmpdir}/pyscratch/{project}-{hash}/{run_id}/`

Set `PY_SCRATCH_USE_SCRATCHPAD=0` to always use the temp directory.

#### How the session is identified

Claude Code never passes the scratchpad path to MCP servers. The directory layout is
derivable (`<claude tmp>/claude-<uid>/<cwd slug>/<session id>/scratchpad`), but the
session id is not: `CLAUDE_CODE_SESSION_ID` in the server's environment is the id it
was *spawned* with, and **`/clear` starts a new session inside the same process**, so
the value goes stale while the server keeps running. Guessing from directory mtimes
works most of the time, which is the worst kind of works.

So the plugin ships a `SessionStart` hook (`hooks/scratchpad_handoff.py`) that does
get the real session id. It records it in

```
{tmpdir}/pyscratch/handoff/{claude_pid}-{claude_starttime}.json
```

keyed by the owning Claude Code process, located by **walking the process tree**
rather than trusting an environment variable. The MCP server walks up from itself to
the same process and reads the same key, so both sides agree exactly — including
across `/clear`, `resume` and `compact`. `starttime` (field 22 of `/proc/<pid>/stat`)
disambiguates a recycled pid; `SessionEnd` removes the file, and stale entries are
pruned on the next `SessionStart`.

The hook prints nothing and always exits 0 — if anything goes wrong it simply falls
back to the environment/mtime heuristic. Installing the MCP server without the plugin
means no hook, and therefore the heuristic; everything still works, just less exactly.

py-scratch never creates the `scratchpad` directory itself: Claude Code creates it
lazily with a non-recursive `mkdir`, which would fail with `EEXIST` if we got there
first. When it does not exist yet, artifacts go into the session directory beside it,
which has the same lifetime.

Server-level diagnostics stay in the temp directory for the life of the process,
since they outlive any one session:

```
{tmpdir}/pyscratch/{project}-{hash}/{run_id}/
  server.log          # server events, warnings, handler tracebacks
  server-stderr.log   # everything the process wrote to stderr
```

`server-stderr.log` exists because Claude Code pipes an MCP server's stderr but
discards it unless started with `--mcp-debug`; without the tee, a crash in the
server would leave no trace anywhere on disk. Every `meta.json` records the
`server_log` path. Set `PY_SCRATCH_LOG_LEVEL` (default `INFO`) to adjust verbosity.

## Reliability

The tool call is answered exactly once, no matter what goes wrong — a request that
gets no response is invisible to the client, which simply waits (Claude Code
backgrounds it after 120s and gives up only after a 30-minute idle timeout).

- Any exception in the handler is returned as an `isError` result with the
  traceback and the server log path, never dropped.
- Near-miss argument names are mapped onto the schema (`script`/`source` → `code`,
  `deps`/`packages` → `dependencies`) and reported back in `argument_notes`;
  genuinely unusable arguments produce an error result explaining the schema.
- Unknown methods and internal failures get proper JSON-RPC error responses.
- Oversized or malformed input lines are logged and skipped instead of taking down
  the read loop.

Child processes are handled defensively too:

- `status` distinguishes `ok`, `failed`, `timeout`, `crashed` and `not_started`.
  A crash reports the signal and a likely cause (`SIGKILL` → OOM killer,
  `SIGSEGV` → native extension, ...). Because the child is `uv run`, signal deaths
  arrive as exit code `128+N` rather than a negative status; both are decoded.
- Scripts run in their own process group, so a timeout kills the interpreter too
  rather than orphaning it behind `uv`.
- A child that dies on a fatal signal *without producing any output* is retried
  once — it never got far enough to have side effects, and the cause is usually
  transient. A crash that reproduces is reported as such.

## Development

```bash
uv sync
uv run python test_server.py
```
