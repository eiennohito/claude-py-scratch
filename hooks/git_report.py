#!/usr/bin/env python3
"""Pre/PostToolUse(Bash) hook: tell the model when one command rewrote a lot of a repo.

Pre:  snapshot `git diff --numstat` (tracked changes vs index) keyed by tool_use_id.
Post: diff the numstat against the snapshot. If this single command changed more than
      200 lines in total, or rewrote more than 50% of a file with more than 40 lines,
      return additionalContext so the model can restore if it was unintended.

Silent and exit 0 on anything unexpected (no git, slow git, no snapshot, ...).
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _hook_common import prune_state, read_event, run_main, split_command, state_dir, strip_prefix, words_of  # noqa: E402

TOTAL_LIMIT = 200
FILE_MIN_LINES = 40
GIT_TIMEOUT = 2

# Commands that cannot modify tracked files (when also free of redirections).
_READONLY = {"ls", "cat", "grep", "rg", "head", "tail", "wc", "pwd", "echo", "which", "stat",
             "tree", "file", "du", "df", "cd", "true", "false", "test", "[", "printf", "less",
             "env", "date", "whoami", "id", "uname", "ps", "diff", "sort", "uniq", "cut"}
_READONLY_GIT = {"status", "log", "diff", "show", "branch", "rev-parse", "ls-files", "blame",
                 "remote", "describe", "grep", "shortlog", "config"}


def obviously_readonly(command: str) -> bool:
    if re.search(r"(?<![<>=\-])>|\btee\b|<<", command):
        return False
    for seg in split_command(command):
        words = strip_prefix(words_of(seg.text))
        if not words:
            continue
        name = os.path.basename(words[0])
        if name == "git":
            rest = [w for w in words[1:] if not w.startswith("-")]
            if not rest or rest[0] not in _READONLY_GIT:
                return False
        elif name == "find":
            if any(w in ("-exec", "-execdir", "-delete", "-ok", "-fprint") for w in words):
                return False
        elif name == "sed":
            if any(w.startswith("-i") or w.startswith("--in-place") for w in words):
                return False
        elif name not in _READONLY:
            return False
    return True


def numstat(cwd: str):
    try:
        r = subprocess.run(
            ["git", "diff", "--numstat", "--no-renames", "--no-ext-diff", "-z"],
            cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True, timeout=GIT_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    out = {}
    for rec in r.stdout.decode("utf-8", "replace").split("\0"):
        parts = rec.split("\t", 2)
        if len(parts) != 3:
            continue
        a, d, path = parts
        out[path.lstrip("\n")] = (int(a) if a.isdigit() else 0, int(d) if d.isdigit() else 0)
    return out


def toplevel(cwd: str) -> str | None:
    try:
        r = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=cwd,
                           stdin=subprocess.DEVNULL, capture_output=True, timeout=GIT_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.decode("utf-8", "replace").strip() or None if r.returncode == 0 else None


def snap_path(event):
    d = state_dir()
    tid = event.get("tool_use_id")
    if d is None or not isinstance(tid, str) or not re.fullmatch(r"[\w.-]{1,128}", tid):
        return None, None
    return d, d / f"gitsnap-{tid}.json"


def pre(event) -> None:
    cwd = event.get("cwd")
    command = (event.get("tool_input") or {}).get("command", "")
    if not cwd or not isinstance(command, str) or obviously_readonly(command):
        return
    d, p = snap_path(event)
    if p is None:
        return
    snap = numstat(cwd)
    if snap is None:
        return
    prune_state(d)
    p.write_text(json.dumps({"cwd": cwd, "stat": snap}))


def count_lines(path: str) -> int:
    try:
        if os.path.getsize(path) > 20 * 1024 * 1024:
            return 0
        with open(path, "rb") as fh:
            return fh.read().count(b"\n")
    except OSError:
        return 0


def post(event) -> None:
    d, p = snap_path(event)
    if p is None or not p.exists():
        return
    try:
        data = json.loads(p.read_text())
    finally:
        try:
            p.unlink()
        except OSError:
            pass
    cwd = data["cwd"]
    before = {k: tuple(v) for k, v in data["stat"].items()}
    after = numstat(cwd)
    if after is None:
        return
    top = toplevel(cwd) or cwd
    rows = []  # (path, added, deleted, changed)
    for path in set(before) | set(after):
        a1, d1 = before.get(path, (0, 0))
        a2, d2 = after.get(path, (0, 0))
        x, y = a2 - a1, d2 - d1
        if x or y:
            rows.append((path, x, y, max(abs(x), abs(y))))
    if not rows:
        return
    total = sum(r[3] for r in rows)
    big = False
    for path, _x, _y, ch in rows:
        n = count_lines(os.path.join(top, path))
        if n > FILE_MIN_LINES and ch > 0.5 * n:
            big = True
    if total <= TOTAL_LIMIT and not big:
        return
    rows.sort(key=lambda r: -r[3])
    shown = ", ".join(f"{p} (+{max(x, 0)}/-{max(y, 0)})"
                      for p, x, y, _ in rows[:8])
    if len(rows) > 8:
        shown += f", ... {len(rows) - 8} more"
    files = " ".join(shlex.quote(r[0]) for r in rows[:8])
    msg = (f"This command changed {total:,} lines across {len(rows)} files: {shown}. "
           f"If unintended, restore with `git -C {shlex.quote(top)} checkout -- {files}` (or git stash); "
           "check before continuing.")
    json.dump({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": msg}},
              sys.stdout)


def main() -> None:
    event = read_event()
    if event.get("tool_name") != "Bash":
        return
    name = event.get("hook_event_name")
    if name == "PreToolUse":
        pre(event)
    elif name == "PostToolUse":
        post(event)


if __name__ == "__main__":
    run_main(main)
