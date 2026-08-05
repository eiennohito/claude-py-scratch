#!/bin/sh
# SessionStart hook: inject a short instruction to prefer run_python_script.

cat <<'JSON'
{
  "hookSpecificOutput": {
    "hookEventName": "SessionStart",
    "additionalContext": "## Python execution\n\nUse the `run_python_script` MCP tool (deferred — call ToolSearch to load it) for ANY throwaway Python beyond a trivial one-liner. It is strictly better than `python -c`, heredocs, or temp files: no shell escaping, automatic dependency resolution, persistent output logs."
  }
}
JSON
