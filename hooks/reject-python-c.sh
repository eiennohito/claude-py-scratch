#!/bin/sh
# PreToolUse hook: reject throwaway Python via Bash when the code is non-trivial.
# Catches python -c, heredoc (python - <<) at *command position* only —
# not when "python -c" appears as text inside arguments to other commands.
#
# Handles prefixes (nice, sudo, env, …), pipes, &&, ||, ;, and uv run.
# Exit 0 = allow, exit 2 = block (stderr sent back to the agent).

set -e

INPUT=$(cat)
TOOL=$(printf '%s' "$INPUT" | jq -r '.tool_name // empty')

[ "$TOOL" = "Bash" ] || exit 0

COMMAND=$(printf '%s' "$INPUT" | jq -r '.tool_input.command // empty')

# wc -l counts newline characters, not lines — add a trailing newline
# so the last line is counted even without one.
LINES=$(printf '%s\n' "$COMMAND" | wc -l)

MSG="Use the run_python_script MCP tool instead — it is more reliable than running Python through Bash (no shell escaping issues, automatic dependency resolution, persistent output logs). Load it with ToolSearch first if needed."

# Split command into segments at shell operators (| && || ;),
# strip known prefix wrappers (nice, sudo, env, uv run …),
# then check if python/python3 is the actual command being invoked.
# This avoids false positives when "python -c" appears as text
# inside arguments to other commands (e.g. git commit messages, echo).

PREFIXES='((nice|nohup|env|sudo|time|strace|command|exec|setsid|ionice|chrt|taskset|numactl)[[:space:]]+)*'
UV_RUN='(uv[[:space:]]+run[[:space:]]+([^[:space:]]+[[:space:]]+)*)?'
PYTHON="${PREFIXES}${UV_RUN}python3?[[:space:]]+"

# Split on shell operators (&&, || first to avoid partial | match),
# trim leading whitespace, then grep for python at segment start.
segments() {
  printf '%s\n' "$COMMAND" \
    | sed 's/[[:space:]]*&&[[:space:]]*/\n/g; s/[[:space:]]*||[[:space:]]*/\n/g' \
    | sed 's/[[:space:]]*|[[:space:]]*/\n/g; s/[[:space:]]*;[[:space:]]*/\n/g' \
    | sed 's/^[[:space:]]*//'
}

MODE=""
if segments | grep -qE "^${PYTHON}-c([[:space:]]|\$)"; then
  MODE="c"
elif segments | grep -qE "^${PYTHON}-[[:space:]]*<<"; then
  MODE="heredoc"
fi

case "$MODE" in
  c)
    if [ "$LINES" -gt 2 ]; then
      echo "Blocked: python -c with $LINES lines. $MSG" >&2
      exit 2
    fi
    ;;
  heredoc)
    echo "Blocked: Python heredoc with $LINES lines. $MSG" >&2
    exit 2
    ;;
esac

exit 0
