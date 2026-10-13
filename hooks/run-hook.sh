#!/bin/sh
# Runs a Python hook script (path relative to this directory) with whatever interpreter
# exists. stdin is inherited; stdout/stderr and the exit status (2 = block) pass through.
# With no interpreter at all the hook is skipped (fail open).

dir=$(dirname "$0")
script="$dir/$1"

if command -v python3 >/dev/null 2>&1; then
    exec python3 "$script"
elif command -v uv >/dev/null 2>&1; then
    exec uv run --quiet --python 3 "$script"
fi

exit 0
