#!/usr/bin/env python3
"""PreToolUse(Bash) hook: keep throwaway Python and bulk in-place edits out of Bash.

A. Python via `python -c` (more than 2 lines) or a heredoc/stdin, at command position
   (after prefixes, flags, `cd X &&`; heredoc bodies of other commands are skipped):
   - edit-like code (writes files)  -> deny, point at the Edit/Write tools
   - anything else                  -> deny, point at the run_python_script MCP tool
B. `sed -i` / `perl -pi`: dry-run on the real files; if the change is large
   (> max(20, 10% of a file's lines)) deny ONCE with stats and a diff excerpt. An
   identical re-run of the same command in the same session is then allowed.

Exit 2 + stderr blocks and goes to the model. Everything else, including any error in
this script, allows (exit 0).
"""

from __future__ import annotations

import difflib
import glob
import hashlib
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _hook_common import (  # noqa: E402
    cd_target, command_name, prune_state, read_event, run_main, split_command,
    state_dir, strip_prefix, words_of,
)

TOOL = "mcp__plugin_py-scratch_py-scratch__run_python_script"
MCP_MSG = (
    "Blocked: throwaway Python via Bash. Use the run_python_script MCP tool: first "
    f"ToolSearch query `select:{TOOL}`, then call it with intent, code, time_estimate. "
    "(Writing a script to a file and running it is fine for project code.)"
)
EDIT_MSG = (
    "Blocked: this Python snippet edits files. Use the Edit tool (exact old_string match, "
    "fails loudly) or Write for new files; Python string replacement can silently match "
    "nothing or rewrite far more than intended."
)
MIN_LINES = 2  # `python -c` is only blocked above this many command lines
CONFIRM_TTL = 600
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_DIFF_LINES = 200_000

# ----------------------------------------------------------------------- A: python

_PY = re.compile(r"^python(\d+(\.\d+)*)?$")
_NOARG_FLAGS = "bBdEiIOqsSuvVxRP"
_C_FLAG = re.compile(rf"^-[{_NOARG_FLAGS}]*c$")
_M_FLAG = re.compile(rf"^-[{_NOARG_FLAGS}]*m$")

_WRITE_MODE = re.compile(
    r"""(?:\bopen\s*\((?:[^()]|\([^()]*\))*?,\s*(?:mode\s*=\s*)?|\.open\(\s*)['"]([rwaxbt+U]+)['"]""", re.S)
_EDIT_PATTERNS = [
    re.compile(r"\.write_(?:text|bytes)\s*\("),
    re.compile(r"(?<!sys\.stdout)(?<!sys\.stderr)\.write\s*\("),
    re.compile(r"\bos\.(?:replace|rename|renames|remove|unlink)\s*\("),
    re.compile(r"\bshutil\.(?:copy\w*|move|rmtree)\s*\("),
    re.compile(r"\bfileinput\b.*\binplace\b|\binplace\s*=\s*True", re.S),
]


def is_edit_like(code: str) -> bool:
    for m in _WRITE_MODE.finditer(code):
        if set(m.group(1)) & set("wax+"):
            return True
    return any(p.search(code) for p in _EDIT_PATTERNS)


def python_invocation(words: list[str]):
    """('c', code) | ('stdin', None) | None for a command (prefixes already stripped)."""
    if not words or not _PY.match(command_name(words)):
        return None
    k = 1
    while k < len(words):
        a = words[k]
        if _C_FLAG.match(a):
            return ("c", words[k + 1]) if k + 1 < len(words) else None
        if a == "-" or a == "--":
            return ("stdin", None) if a == "-" else None
        if _M_FLAG.match(a):
            return None
        if a in ("-X", "-W", "--check-hash-based-pycs"):
            k += 2
        elif a.startswith("-"):
            k += 1
        else:
            return None  # script file
    return ("stdin", None)


def check_python(command: str, segs) -> str | None:
    nlines = command.count("\n") + 1
    prev = None
    for seg in segs:
        words = strip_prefix(words_of(seg.text))
        inv = python_invocation(words)
        if inv:
            kind, code = inv
            if kind == "stdin":
                bodies = seg.heredocs or (prev.heredocs if prev and seg.sep == "|" else [])
                if bodies:
                    return EDIT_MSG if is_edit_like("\n".join(bodies)) else MCP_MSG
            elif nlines > MIN_LINES:
                return EDIT_MSG if is_edit_like(code) else MCP_MSG
        prev = seg
    return None


# ----------------------------------------------------------------------- B: sed / perl

def _sed_unsafe(script: str) -> bool:
    return bool(re.search(r"/[0-9gIiMmp]*[we]\b", script)
                or re.search(r"(^|[;{}\n])\s*[0-9,$/]*\s*[wWeR]\s", script))


def parse_sed(words):
    """-> (args for a dry run without file operands, files) or None."""
    opts: list[str] = []
    positional: list[str] = []
    scripts: list[str] = []
    has_script_opt = False
    inplace = False
    i = 1
    n = len(words)
    while i < n:
        w = words[i]
        if w == "--":
            positional += words[i + 1 :]
            break
        if w.startswith("--"):
            name, eq, val = w.partition("=")
            if name == "--in-place":
                inplace = True
            elif name in ("--expression", "--file"):
                has_script_opt = True
                if name == "--file":
                    return None
                if eq:
                    opts.append(w)
                    scripts.append(val)
                elif i + 1 < n:
                    opts += [w, words[i + 1]]
                    scripts.append(words[i + 1])
                    i += 1
            elif name == "--line-length" and not eq and i + 1 < n:
                opts += [w, words[i + 1]]
                i += 1
            elif name in ("--sandbox", "--debug"):
                pass
            else:
                opts.append(w)
        elif w.startswith("-") and len(w) > 1:
            keep = ""
            cl = w[1:]
            j = 0
            while j < len(cl):
                c = cl[j]
                if c == "i":
                    inplace = True
                    break
                if c in "efl":
                    arg = cl[j + 1 :]
                    if not arg:
                        if i + 1 >= n:
                            return None
                        i += 1
                        arg = words[i]
                    if c == "f":
                        return None
                    if c == "e":
                        has_script_opt = True
                        scripts.append(arg)
                    if keep:
                        opts.append("-" + keep)
                        keep = ""
                    opts += ["-" + c, arg]
                    break
                keep += c
                j += 1
            if keep:
                opts.append("-" + keep)
        else:
            positional.append(w)
        i += 1
    if not inplace:
        return None
    if has_script_opt:
        files = positional
    else:
        if not positional:
            return None
        scripts.append(positional[0])
        opts.append(positional[0])
        files = positional[1:]
    if any(_sed_unsafe(s) for s in scripts):
        return None
    return opts, files


_PERL_UNSAFE = re.compile(r"\b(?:system|exec|unlink|open|rename|mkdir|rmdir|chdir|kill|fork)\b|`|\bqx\b")


def parse_perl(words):
    opts: list[str] = []
    positional: list[str] = []
    codes: list[str] = []
    inplace = False
    i = 1
    n = len(words)
    while i < n:
        w = words[i]
        if w == "--":
            positional += words[i + 1 :]
            break
        if w.startswith("-") and len(w) > 1 and not w.startswith("--"):
            cl = w[1:]
            keep = ""
            j = 0
            while j < len(cl):
                c = cl[j]
                if c in "eE":
                    arg = cl[j + 1 :]
                    if not arg:
                        if i + 1 >= n:
                            return None
                        i += 1
                        arg = words[i]
                    if keep:
                        opts.append("-" + keep)
                        keep = ""
                    opts += ["-" + c, arg]
                    codes.append(arg)
                    break
                if c == "i":
                    inplace = True
                    rest = cl[j + 1 :]
                    if rest == "e":  # -pie: treat as -p -i -e
                        if i + 1 >= n:
                            return None
                        i += 1
                        if keep:
                            opts.append("-" + keep)
                            keep = ""
                        opts += ["-e", words[i]]
                        codes.append(words[i])
                    break  # anything else after i is the backup suffix
                if c in "MmIF":
                    keep += cl[j:]
                    break
                keep += c
                j += 1
            if keep:
                opts.append("-" + keep)
        else:
            positional.append(w)
        i += 1
    if not inplace or not codes:
        return None
    if any(_PERL_UNSAFE.search(c) for c in codes):
        return None
    return opts, positional


def resolve_files(operands, cwd):
    out = []
    for f in operands:
        if "$" in f or "`" in f:
            return None
        p = os.path.expanduser(f)
        if not os.path.isabs(p):
            p = os.path.join(cwd, p)
        if not os.path.exists(p) and any(ch in p for ch in "*?["):
            hits = sorted(glob.glob(p))
            if not hits:
                return None
            out += hits
        else:
            out.append(p)
    out = [p for p in out if os.path.isfile(p)]
    return out or None


def _run(args, cwd, path):
    try:
        r = subprocess.run(args + [path], cwd=cwd, stdin=subprocess.DEVNULL,
                           capture_output=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def dry_run(prog, parsed, cwd):
    """-> list of (path, orig_lines, new_lines) or None."""
    opts, operands = parsed
    files = resolve_files(operands, cwd)
    if not files:
        return None
    results = []
    for path in files:
        try:
            if os.path.getsize(path) > MAX_FILE_BYTES:
                continue
            with open(path, "rb") as fh:
                orig = fh.read()
        except OSError:
            continue
        base = [prog] + opts
        out = _run(base, cwd, path)
        if out is None or len(out) > 2 * MAX_FILE_BYTES:
            continue
        results.append((path, orig.splitlines(keepends=True), out.splitlines(keepends=True)))
    return results


def changed_lines(a, b) -> int:
    if a == b:
        return 0
    if len(a) > MAX_DIFF_LINES or len(b) > MAX_DIFF_LINES:
        return -1
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    return sum(max(i2 - i1, j2 - j1) for tag, i1, i2, j1, j2 in sm.get_opcodes() if tag != "equal")


def check_inplace(event, command, segs) -> str | None:
    cwd = event.get("cwd") or os.getcwd()
    bad = []  # (path, changed, total, a, b)
    for seg in segs:
        words = words_of(seg.text)
        if command_name(words) == "cd":
            cwd = cd_target(words, cwd)
            continue
        if cwd is None:
            return None
        words = strip_prefix(words)
        name = command_name(words)
        if name in ("sed", "gsed"):
            parsed = parse_sed(words)
            prog = "sed" if name == "sed" else name
        elif name == "perl":
            parsed = parse_perl(words)
            prog = "perl"
        else:
            continue
        if not parsed:
            continue
        for path, a, b in dry_run(prog, parsed, cwd) or []:
            ch = changed_lines(a, b)
            if ch > max(20, 0.1 * len(a)):
                bad.append((path, ch, len(a), a, b))
    if not bad:
        return None
    if consume_confirmation(event.get("session_id", ""), command):
        return None
    return _deny_once(event, command, bad, cwd)


def _confirm_path(session_id, command):
    d = state_dir()
    if d is None:
        return None
    h = hashlib.sha256(f"{session_id}\0{command}".encode("utf-8", "replace")).hexdigest()
    return d, d / f"confirm-{h[:40]}"


def consume_confirmation(session_id, command) -> bool:
    """True if this exact command was denied recently (and consume that record)."""
    loc = _confirm_path(session_id, command)
    if loc is None:
        return True  # cannot remember -> never block
    _, p = loc
    try:
        fresh = time.time() - p.stat().st_mtime < CONFIRM_TTL
        p.unlink()
        return fresh
    except OSError:
        return False


def _deny_once(event, command, bad, cwd) -> str | None:
    loc = _confirm_path(event.get("session_id", ""), command)
    if loc is None:
        return None
    d, p = loc
    try:
        prune_state(d)
        p.touch()
    except OSError:
        return None
    lines = []
    for path, ch, total, _a, _b in bad:
        rel = os.path.relpath(path, cwd) if cwd else path
        lines.append(f"`sed/perl -i` would change {ch:,} of {total:,} lines in "
                     f"{rel if len(rel) < len(path) else path}")
    path, _ch, _t, a, b = bad[0]
    dl = [x for x in difflib.unified_diff(
        [x.decode("utf-8", "replace").rstrip("\r\n") for x in a[:MAX_DIFF_LINES]],
        [x.decode("utf-8", "replace").rstrip("\r\n") for x in b[:MAX_DIFF_LINES]],
        n=0, lineterm="") if not x.startswith(("---", "+++"))]
    minus = [x for x in dl if x.startswith("-")][:5]
    plus = [x for x in dl if x.startswith("+")][:5]
    excerpt = "\n".join(x[:160] for x in minus + plus)
    return ("Blocked (once): " + "; ".join(lines) + f".\n{excerpt}\n"
            "If this is intended, run the exact same command again; otherwise use Edit.")


# ----------------------------------------------------------------------- main

def main() -> None:
    event = read_event()
    if event.get("tool_name") != "Bash":
        return
    ti = event.get("tool_input") or {}
    command = ti.get("command") if isinstance(ti, dict) else None
    if not isinstance(command, str) or not command.strip():
        return
    segs = split_command(command)
    msg = check_python(command, segs)
    if msg is None and re.search(r"\b(?:sed|perl)\b", command):
        msg = check_inplace(event, command, segs)
    if msg:
        sys.stderr.write(msg + "\n")
        sys.stderr.flush()
        sys.exit(2)


if __name__ == "__main__":
    run_main(main)
