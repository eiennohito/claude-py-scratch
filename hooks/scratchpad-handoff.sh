#!/bin/sh
# Wrapper for scratchpad_handoff.py: picks an interpreter and never fails the session.
# stdin (the hook event JSON) is inherited by the interpreter.

dir=$(dirname "$0")

if command -v python3 >/dev/null 2>&1; then
    python3 "$dir/scratchpad_handoff.py" 2>/dev/null
elif command -v uv >/dev/null 2>&1; then
    uv run --quiet --python 3 "$dir/scratchpad_handoff.py" 2>/dev/null
fi

exit 0
