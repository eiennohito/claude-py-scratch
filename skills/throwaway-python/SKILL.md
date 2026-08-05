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
| **`run_python_script`** | **None. Raw string input, auto-deps, persistent logs, no escaping.** |

**Always use `run_python_script` for any Python beyond a trivial stdlib one-liner.**

The tool is a deferred MCP tool. If its schema is not yet loaded, call `ToolSearch` with `select:mcp__plugin_py-scratch_py-scratch__run_python_script` first.

## Usage

```
run_python_script(
  intent: str,             # what you're trying to do — required
  code: str,               # raw Python — no shell escaping needed
  dependencies: str[],     # optional PyPI packages — skips auto-retry
  timeout: int = 30,       # seconds
  head: int = 0,           # lines from start of output in preview
  tail: int = 5            # lines from end of output in preview
)
```

- **Use `intent` to explain what the script does.** Don't put explanatory `#` comments at the top of `code` — that's what `intent` is for.
- **Don't shell-escape the code.** It's a structured string field, not a shell argument.
- **Declare dependencies when you know them.** Auto-resolution works but adds a retry round-trip.
- **Don't pipe Python output into jq/awk/sed.** Do the processing in Python.

## Reading output

The tool returns a preview of stdout/stderr (default: last 5 lines, adjustable via `head`/`tail`) plus file paths (`stdout_path`, `stderr_path`). Use Read or Grep on those paths when you need more.

## Multi-project workspaces

If the tool description mentions multiple projects were detected, the tool still works — scripts just run without pre-loaded project dependencies. Declare what you need in the `dependencies` array. Do NOT create `.py-scratch.json` unless the user explicitly asks for it.
