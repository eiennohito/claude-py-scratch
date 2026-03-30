#!/bin/sh
# PreToolUse hook: reject Bash(python -c ...) when the Python code is >10 lines.
# Reads MCP hook input JSON from stdin.
# Exit 0 = allow, exit 2 = block (stderr sent back to the agent).

set -e

INPUT=$(cat)
TOOL=$(printf '%s' "$INPUT" | jq -r '.tool_name // empty')

[ "$TOOL" = "Bash" ] || exit 0

COMMAND=$(printf '%s' "$INPUT" | jq -r '.tool_input.command // empty')

# Check for python -c or python3 -c
case "$COMMAND" in
  *python3\ -c*|*python\ -c*)
    ;;
  *)
    exit 0
    ;;
esac

# Count newlines in the command itself
LINES=$(printf '%s' "$COMMAND" | wc -l)

if [ "$LINES" -gt 2 ]; then
  echo "Blocked: python -c with $LINES lines. Use the run-python skill (run_python_script MCP tool) instead." >&2
  exit 2
fi

exit 0
