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

- **MCP server** -- `run_python_script` tool, registered via `mcpServers` in `.claude-plugin/plugin.json`
  (not a root `.mcp.json`, which Claude Code would also load as a project server when working in this repo)
- **Hooks**
  - blocks throwaway Python via Bash (multi-line `python -c`, `python -` heredocs, any interpreter flags):
    snippets that write files are pointed at the Edit tool, everything else at `run_python_script`
    with the exact `ToolSearch` line to load it. `python3 script.py` stays allowed.
  - dry-runs `sed -i` / `perl -pi`; if the edit would change more than max(20 lines, 10% of the file),
    blocks once with the diff stats — re-running the identical command confirms it
  - after every Bash command in a git repo, reports to the agent if that command changed a lot
    of tracked lines (>200 total, or >50% of a file), with the `git checkout` to undo it
  - injects a short usage instruction at session start (also after `/clear`)
  - hands the real session identity to the MCP server, so execution artifacts land in the right session directory (see [How the session is identified](#how-the-session-is-identified))
- **Skill** -- teaches the agent when to use the tool vs plain `python -c`

### Manual (MCP server only)

Add to a project `.mcp.json` or `~/.claude.json` (global):

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
  intent: str,             # what the script is for
  code: str,               # raw Python source -- required unless run_id is given
  time_estimate: float = 10,   # seconds you expect the script to take
  dependencies: str[],     # optional PyPI package names
  head: int = 0,           # lines from start of stdout preview
  tail: int = 5,           # lines from end of stdout preview
  run_id: int,             # follow up on a run that returned "running"
  action: "wait" | "kill"  # with run_id (default "wait")
)
```

`time_estimate` replaces the old `timeout` (still accepted as an alias, with a note).
It is an estimate, not a limit: the hard kill is at 10x the estimate, and the clock
starts when the script starts, not while uv installs dependencies.

If the script is still running when the estimate is up, the call **returns instead of
blocking**: the server samples the interpreter's stack (a small bootstrap registers
`faulthandler` on `SIGUSR1`, so this works even inside a C extension), writes it to
`trace`, and reports the state `running` and the line being executed. The script keeps
running in the background. `run_id` with `action="wait"` blocks for another estimate
period (or a new `time_estimate`) and samples again; `action="kill"` kills the
process group. A script expected to take a second that is still running after ten is
nearly always an infinite loop, and this surfaces it at once instead of after a
ten-minute timeout. Unknown `run_id` is an error.

The result is plain text, not JSON. The first line is the status:

```
ok exit=0 0.21s · run 42 · /tmp/pyscratch-1000/myproj-a1b2c3/2026-08-26-14-27-13-3513357/42/{code.py,stdout,stderr}
failed exit=1 ...      crashed exit=137 ...      killed ...      timeout (hard limit 100s) ...
running 3.1s (estimate 1s, hard limit 10s) · run 42 · <dir>/{code.py,stdout,stderr,trace}
at line 14: if key in seen_list:
<last stdout lines>
→ run_python_script(run_id=42, action="wait"|"kill")
```

Below the status come the stdout preview, and the stderr preview when the run did not
succeed (an `ok` run with stderr output just notes the line count). Extras appear only
when present: `note:` (argument corrections), `auto-installed <pkg>`, `retry of run N`,
and an explanation for `crashed`/`not_started`. `status` is one of `ok`, `failed`,
`timeout`, `killed`, `crashed`, `not_started`. See [Reliability](#reliability).

By default, the last 5 lines of stdout are shown. Use `head`/`tail` to control what's
included. Full output is in the run directory -- the agent uses its own Read/Grep tools
when it needs more.

**Dependency auto-resolution:** if the script fails with `ModuleNotFoundError` and no deps were declared, the server infers the package name (handling mismatches like `cv2` -> `opencv-python`, `PIL` -> `Pillow`) and retries automatically. Like the crash retry, this applies only to runs that finished within their estimate.

**How it runs:**
1. Writes code to `{output_dir}/{run_id}/code.py` with PEP 723 inline metadata for any declared deps
2. Executes it through a thin bootstrap via `uv run`, stdout/stderr streamed directly to files (stdout is line-buffered so progress shows up in `running` previews)
3. A background supervisor per run enforces the hard limit; runs are tracked in a registry

### Run status resource

The server declares the MCP `resources` capability. `resources/list` returns active
and recent runs, and `resources/read` of `pyscratch://runs/<id>/status` (template
`pyscratch://runs/{id}/status`) returns JSON: `state`, `elapsed`, `estimate`,
`hard_limit`, `exit_code`, `pgid`, `last_frame` (the line the last stack sample found),
and `dir`. Code, stdout and stderr are files only.

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
{artifact_root}/{run_id}/
  code.py      # the code that ran
  stdout       # full stdout
  stderr       # full stderr
  trace        # last stack sample (runs that outlived their estimate)
  meta.json    # exit code, status, duration, deps, timestamp
```

### Where artifacts go

Resolved per call, first match wins:

1. `PY_SCRATCH_DIR` environment variable
2. `scratch_dir` in `.py-scratch.json`
3. **The Claude Code session directory**, when running under Claude Code:
   `<session scratchpad>/py-scratch/{run_id}/` — so script output lands beside the
   session's other working files and is cleaned up with them
4. `{user root}/{project}-{hash}/{run_id}/`

Set `PY_SCRATCH_USE_SCRATCHPAD=0` to always use the temp directory.

`{user root}` is per-user, because `/tmp` is shared on multi-user machines and a
fixed `/tmp/pyscratch` would belong to whoever created it first — breaking the plugin
for everyone else and letting them tamper with scripts before they run. It is
`{tmpdir}/pyscratch-{uid}` on POSIX and `{tmpdir}/pyscratch` on Windows (whose temp
dir is per-user). Deliberately not `$XDG_RUNTIME_DIR`: that is wiped on the user's
last logout, and artifacts are what you come back to when debugging — `/tmp`'s
until-reboot lifetime fits better. The directory is created `0700` and verified to be
a real directory owned by the current user. If that check fails the path is still
used (with a logged warning): the handoff — the input that decides what gets
executed — is verified separately, so what remains at stake here is log privacy, and
a deterministic path you can find after the session is worth more than that.

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
{handoff root}/handoff/{claude_pid}-{claude_starttime}.json
```

`{handoff root}` is `$XDG_RUNTIME_DIR/pyscratch` when available, falling back to
`{user root}`. Unlike artifacts, a handoff is only meaningful while its Claude Code
process is alive, so the runtime dir's logout lifetime is the right one — and it is
kernel-guaranteed per-user and `0700`.

keyed by the owning Claude Code process, located by **walking the process tree**
rather than trusting an environment variable. The MCP server walks up from itself to
the same process and reads the same key, so both sides agree exactly — including
across `/clear`, `resume` and `compact`. `starttime` (field 22 of `/proc/<pid>/stat`)
disambiguates a recycled pid; `SessionEnd` removes the file, and stale entries are
pruned on the next `SessionStart`. Per-pid keys also mean any number of concurrent
Claude Code instances of the same user coexist without stepping on each other.

The handoff decides where the server writes — and then executes — `script.py`, so it
is only trusted when it could not have been planted: the hook writes it `0600` inside
the private per-user root, and the server refuses a handoff file that is a symlink,
owned by someone else, or writable by group/other.

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
{user root}/{project}-{hash}/{run_id}/
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
  `deps`/`packages` → `dependencies`, `timeout` → `time_estimate`) and reported back
  in a `note:` line;
  genuinely unusable arguments produce an error result explaining the schema.
- Unknown methods and internal failures get proper JSON-RPC error responses.
- Oversized or malformed input lines are logged and skipped instead of taking down
  the read loop.

Child processes are handled defensively too:

- `status` distinguishes `ok`, `failed`, `timeout`, `crashed` and `not_started`.
  A crash reports the signal and a likely cause (`SIGKILL` → OOM killer,
  `SIGSEGV` → native extension, ...). Because the child is `uv run`, signal deaths
  arrive as exit code `128+N` rather than a negative status; both are decoded.
- Scripts run in their own process group, so a kill (hard limit, `action="kill"`,
  stdin EOF or SIGTERM on the server, or a `notifications/cancelled` for the call that
  is blocking on the run) takes the interpreter too rather than orphaning it behind `uv`.
- A child that dies on a fatal signal *without producing any output* is retried
  once — it never got far enough to have side effects, and the cause is usually
  transient. A crash that reproduces is reported as such.

## Development

```bash
uv sync
uv run python test_server.py
```
