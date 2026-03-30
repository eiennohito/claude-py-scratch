---
name: throwaway-python
description: Use run_python_script for throwaway Python execution beyond trivial one-liners. Triggers when the agent is about to write multi-line Python or needs third-party packages.
user-invocable: false
---

# Python Execution

Use the `run_python_script` MCP tool instead of `Bash(python -c "...")` whenever:

- The Python code is longer than 2 lines
- The code requires third-party packages
- The code contains nested quotes, f-strings, or backslashes that would need escaping

`python -c` is fine for stdlib one-liners like `python -c "import json; print(json.dumps(data))"`.

## Usage

```
run_python_script(
  intent: str,             # what you're trying to do — required
  code: str,               # raw Python — no shell escaping
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
