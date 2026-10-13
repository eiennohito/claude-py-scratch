---
name: throwaway-python
description: Use mcp__plugin_py-scratch_py-scratch__run_python_script for throwaway Python execution. More errorproof than using `python -c`. Use this skill for advanced configuration (not needed by default).
user-invocable: false
---

# Python Execution — use `run_python_script`

The `run_python_script` MCP tool is the **preferred and most reliable way** to run throwaway Python. It is strictly better than every alternative:

| Approach | Problems |
|---|---|
| `python -c "..."` | Shell escaping breaks on quotes, f-strings, backslashes. Fragile. |
| `python - <<'EOF'` (heredoc) | Still shell-interpreted; indentation issues; no dependency management. |
| Write `.py` file + `python file.py` | Extra steps, leaves temp files, no automatic dependency resolution. |
| Bash pipelines (`jq`, `awk`, `sed`, `grep`) | Hard to debug, poor error handling, breaks on edge cases in data. |
| **`run_python_script`** | **None. Raw string input, auto-deps, persistent logs, no escaping.** |

**Always use `run_python_script` for any Python beyond a trivial stdlib one-liner.**

**Prefer a Python script over Bash pipelines** for data processing, JSON/CSV manipulation, and multi-step transformations. Python is easier to read, debug, and extend.

**Performance matters.** Modern hardware processes gigabytes per second — a 100 MB file should take seconds, not minutes. Use efficient libraries (polars/pyarrow for tabular data, orjson for JSON) and columnar reads over row-by-row loops. If Python is genuinely the wrong tool for the job (e.g. massive line-oriented filtering where `rg`/`awk` saturates I/O), use the right tool instead.

The tool is a deferred MCP tool. If its schema is not yet loaded, call `ToolSearch` with `select:mcp__plugin_py-scratch_py-scratch__run_python_script` first.

## Usage

```
run_python_script(
  intent: str,             # what you're trying to do
  code: str,               # raw Python — no shell escaping needed
  time_estimate: number,   # seconds you expect it to take (default 10) — always set it
  dependencies: str[],     # optional PyPI packages — skips auto-retry
  head: int = 0,           # lines from start of stdout preview
  tail: int = 5            # lines from end of stdout preview
)
run_python_script(run_id: int, action: "wait" | "kill", time_estimate?: number)
```

- **Always estimate `time_estimate` honestly** (a 1 MB file: 1s; a 1 GB scan: 60s). Don't pad it "to be safe". If the script is still running when the estimate is up, the call returns at once with `running`, the line it is executing (`at line 14: ...`) and a `run_id`; the script keeps going in the background and is hard-killed at 10x the estimate. A script you expected to take 1s that is still running after 10s is almost always an infinite loop or quadratic code: `kill` it and fix it instead of waiting.
- **Follow up with `run_id`**: `action="wait"` blocks for another estimate period (pass a new `time_estimate` to change it) and reports the current line again; `action="kill"` stops it. `code`/`intent` are not needed. `timeout` is accepted as an alias of `time_estimate`, but it is an estimate, not a limit.
- **Use `intent` to explain what the script does.** Don't put explanatory `#` comments at the top of `code` — that's what `intent` is for.
- **Don't shell-escape the code.** It's a structured string field, not a shell argument.
- **Declare dependencies when you know them.** Auto-resolution works but adds a retry round-trip.
- **Don't pipe Python output into jq/awk/sed.** Do the processing in Python.
- **Prefer Python over Bash pipelines** for anything beyond trivial `grep`/`wc`. A Python script that reads a file and processes it is clearer and more robust than chaining `cat | jq | awk | sort | uniq`.

## Reading output

The result is plain text. The first line is the status: `ok exit=0 0.21s · run 42 · <dir>/{code.py,stdout,stderr}` (also `failed exit=1`, `crashed`, `killed`, `timeout (hard limit Ns)`, `running ...`). Below it come the last `tail` lines of stdout, and stderr if the run failed. The files in `<dir>` hold the full output: read them with the Read tool (or Grep); `trace` holds the stack sample of a run that was still running.

## Multi-project workspaces

If the tool description mentions multiple projects were detected, the tool still works — scripts just run without pre-loaded project dependencies. Declare what you need in the `dependencies` array. Do NOT create `.py-scratch.json` unless the user explicitly asks for it.
