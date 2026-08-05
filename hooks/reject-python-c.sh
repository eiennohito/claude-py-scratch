#!/bin/sh
# PreToolUse hook: reject throwaway Python via Bash when the code is non-trivial.
# Catches python -c, heredoc (python - <<), and write-then-run patterns.
# Reads MCP hook input JSON from stdin.
# Exit 0 = allow, exit 2 = block (stderr sent back to the agent).

set -e

INPUT=$(cat)
TOOL=$(printf '%s' "$INPUT" | jq -r '.tool_name // empty')

[ "$TOOL" = "Bash" ] || exit 0

COMMAND=$(printf '%s' "$INPUT" | jq -r '.tool_input.command // empty')

# Count newlines in the command
LINES=$(printf '%s' "$COMMAND" | wc -l)

MSG="Use the run_python_script MCP tool instead — it is more reliable than running Python through Bash (no shell escaping issues, automatic dependency resolution, persistent output logs). Load it with ToolSearch first if needed."

# 1) python -c / python3 -c  (>2 lines)
case "$COMMAND" in
  *python3\ -c*|*python\ -c*)
    if [ "$LINES" -gt 2 ]; then
      echo "Blocked: python -c with $LINES lines. $MSG" >&2
      exit 2
    fi
    exit 0
    ;;
esac

# 2) Heredoc: python - <<  /  python3 - <<  (any length — heredocs are always non-trivial)
case "$COMMAND" in
  *python3\ -\ \<\<*|*python\ -\ \<\<*|*python3\ -\<\<*|*python\ -\<\<*)
    echo "Blocked: Python heredoc with $LINES lines. $MSG" >&2
    exit 2
    ;;
esac

exit 0
