#!/bin/sh
# SessionStart hook: tell the model how to run throwaway Python and edit files.

cat <<'JSON'
{
  "hookSpecificOutput": {
    "hookEventName": "SessionStart",
    "additionalContext": "## Python and file edits\n\nThrowaway Python (anything beyond a one-liner): use the deferred MCP tool `run_python_script`. Load it first with ToolSearch query `select:mcp__plugin_py-scratch_py-scratch__run_python_script`, then call it with `intent`, `code`, `time_estimate` (seconds; optional `dependencies`, `head`, `tail`). Not `python -c`/heredocs via Bash.\n\nFile edits go through the Edit tool (or Write for new files), never Python/sed/perl patch scripts. Writing a script to a file and running it is fine for project code."
  }
}
JSON
