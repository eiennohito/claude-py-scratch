"""Shared helpers for the py-scratch Bash hooks (stdlib only).

Hooks must never break a session: callers wrap everything in `run_main` and only
exit 2 deliberately, to block.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import stat
import sys
import tempfile
import time
from pathlib import Path


# --------------------------------------------------------------------------- process

def read_event() -> dict:
    try:
        data = json.loads(sys.stdin.read())
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def install_deadline(seconds: int = 8) -> None:
    """Exit 0 (allow) if the hook runs too long."""
    try:
        import signal

        signal.signal(signal.SIGALRM, lambda *_: os._exit(0))
        signal.alarm(seconds)
    except Exception:
        pass


def run_main(main) -> None:
    """Run main(); any error means allow."""
    install_deadline()
    try:
        main()
    except SystemExit:
        raise
    except BaseException:
        pass
    sys.exit(0)


# --------------------------------------------------------------------------- state dir

def _secure_mkdir(path: Path) -> Path | None:
    try:
        path.mkdir(mode=0o700, exist_ok=True)
        if os.name == "posix":
            st = os.lstat(path)
            if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
                return None
            if st.st_mode & 0o077:
                os.chmod(path, 0o700)
    except OSError:
        return None
    return path


def state_dir() -> Path | None:
    """Private per-user dir for hook state: $XDG_RUNTIME_DIR/pyscratch/hooks, else
    <tmp>/pyscratch-<uid>/hooks."""
    override = os.environ.get("PY_SCRATCH_HOOK_STATE_DIR")
    if override:
        p = Path(override)
        try:
            p.mkdir(parents=True, exist_ok=True)
        except OSError:
            return None
        return p
    if os.name == "posix":
        xdg = os.environ.get("XDG_RUNTIME_DIR")
        if xdg and Path(xdg).is_dir():
            root = Path(xdg) / "pyscratch"
        else:
            root = Path(tempfile.gettempdir()) / f"pyscratch-{os.getuid()}"
    else:
        root = Path(tempfile.gettempdir()) / "pyscratch"
    root = _secure_mkdir(root)
    return _secure_mkdir(root / "hooks") if root else None


def prune_state(d: Path, max_age: float = 3600) -> None:
    now = time.time()
    try:
        for p in d.iterdir():
            try:
                if now - p.stat().st_mtime > max_age:
                    p.unlink()
            except OSError:
                pass
    except OSError:
        pass


# --------------------------------------------------------------------------- shell parsing

class Seg:
    """One simple command of a compound shell command."""

    def __init__(self, sep: str):
        self.sep = sep  # operator that precedes it: '', '&&', '||', ';', '|', '&', '\n'
        self.text = ""
        self.heredocs: list[str] = []  # bodies of heredocs this command declares


_WS = " \t"
_WORD_END = " \t\n;&|<>()"


def split_command(cmd: str) -> list[Seg]:
    """Split on && || ; | & and newlines (outside quotes), attaching heredoc bodies to
    the command that declared them instead of scanning them as commands."""
    segs: list[Seg] = []
    cur = Seg("")
    buf: list[str] = []
    pending: list[tuple[Seg, str, bool]] = []
    n = len(cmd)
    i = 0

    def flush(next_sep: str) -> None:
        nonlocal cur, buf
        cur.text = "".join(buf).strip()
        if cur.text:
            segs.append(cur)
        cur = Seg(next_sep)
        buf = []

    while i < n:
        c = cmd[i]
        if c == "\\":
            buf.append(cmd[i : i + 2])
            i += 2
        elif c == "'":
            j = cmd.find("'", i + 1)
            j = n if j < 0 else j + 1
            buf.append(cmd[i:j])
            i = j
        elif c == '"':
            j = i + 1
            while j < n and cmd[j] != '"':
                j += 2 if cmd[j] == "\\" else 1
            j = min(j + 1, n)
            buf.append(cmd[i:j])
            i = j
        elif c == "#" and (i == 0 or cmd[i - 1] in " \t\n;&|("):
            j = cmd.find("\n", i)
            i = n if j < 0 else j
        elif cmd.startswith("<<", i) and not cmd.startswith("<<<", i):
            j = i + 2
            strip = False
            if j < n and cmd[j] == "-":
                strip = True
                j += 1
            while j < n and cmd[j] in _WS:
                j += 1
            k = j
            delim = ""
            while k < n and cmd[k] not in _WORD_END:
                if cmd[k] in "'\"":
                    q = cmd.find(cmd[k], k + 1)
                    q = n - 1 if q < 0 else q
                    delim += cmd[k + 1 : q]
                    k = q + 1
                elif cmd[k] == "\\":
                    k += 1
                else:
                    delim += cmd[k]
                    k += 1
            buf.append(cmd[i:k])
            if delim:
                pending.append((cur, delim, strip))
            i = k
        elif cmd.startswith("&&", i) or cmd.startswith("||", i):
            flush(cmd[i : i + 2])
            i += 2
        elif c == ";" or c == "|":
            flush(c)
            i += 1
        elif c == "&" and (i + 1 >= n or cmd[i + 1] != ">") and (i == 0 or cmd[i - 1] not in "<>"):
            flush("&")
            i += 1
        elif c == "\n":
            flush("\n")
            i += 1
            for seg, delim, strip in pending:
                body: list[str] = []
                while i < n:
                    j = cmd.find("\n", i)
                    line = cmd[i:] if j < 0 else cmd[i:j]
                    i = n if j < 0 else j + 1
                    if (line.lstrip("\t") if strip else line) == delim:
                        break
                    body.append(line)
                seg.heredocs.append("\n".join(body))
            pending = []
        else:
            buf.append(c)
            i += 1
    flush("")
    return segs


_REDIR = re.compile(r"^(?:\d*|&)(?:>>?|<<?<?|>&|<&)(.*)$")


def words_of(text: str) -> list[str]:
    """shlex-split a command, dropping redirections (and their targets)."""
    try:
        toks = shlex.split(text)
    except ValueError:
        toks = text.split()
    out: list[str] = []
    skip = False
    for t in toks:
        if skip:
            skip = False
            continue
        m = _REDIR.match(t)
        if m:
            if not m.group(1):
                skip = True
            continue
        out.append(t)
    return out


_ENV = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_PLAIN = {"nohup", "time", "command", "exec", "setsid", "builtin", "!", "{", "(",
          "then", "do", "else", "if", "while", "until"}
_FLAG_ARGS = {
    "nice": {"-n", "--adjustment"},
    "ionice": {"-c", "-n", "-p", "-t", "--class", "--classdata"},
    "sudo": {"-u", "-g", "-h", "-p", "-C", "-D", "-r", "-t", "-U", "-R"},
    "env": {"-u", "-C", "-S", "--unset", "--chdir"},
    "timeout": {"-s", "-k", "--signal", "--kill-after"},
    "strace": {"-o", "-e", "-p", "-s", "-u", "-E"},
    "numactl": set(),
    "chrt": set(),
    "taskset": {"-c"},
    "stdbuf": {"-i", "-o", "-e"},
}
_POSITIONAL = {"timeout": 1, "chrt": 1, "taskset": 1}
_UV_FLAG_ARGS = {"--with", "--python", "-p", "--project", "--directory", "--with-requirements",
                 "--extra", "--group", "--env-file", "--package", "--index", "--with-editable",
                 "--from", "--no-extra", "--only-group", "-w"}


def strip_prefix(words: list[str]) -> list[str]:
    """Drop wrappers (nice, env VAR=x, sudo, timeout N, uv run ..., ...) in front of
    the real command."""
    w = list(words)
    while w:
        t = w[0]
        if len(t) > 1 and t[0] in "({" and t[1:].lstrip("({"):
            w[0] = t = t.lstrip("({")
        b = os.path.basename(t)
        if _ENV.match(t) or t in _PLAIN:
            w.pop(0)
        elif b in _FLAG_ARGS:
            w.pop(0)
            args = _FLAG_ARGS[b]
            while w and w[0].startswith("-") and w[0] != "-":
                f = w.pop(0)
                if f in args and w:
                    w.pop(0)
            for _ in range(_POSITIONAL.get(b, 0)):
                if w:
                    w.pop(0)
        elif b == "uv" and len(w) > 1 and w[1] == "run":
            del w[:2]
            while w and w[0].startswith("-") and w[0] != "-":
                f = w.pop(0)
                if f in _UV_FLAG_ARGS and w:
                    w.pop(0)
        else:
            break
    return w


def command_name(words: list[str]) -> str:
    return os.path.basename(words[0]) if words else ""


def cd_target(words: list[str], cur: str | None) -> str | None:
    """Working dir after a `cd DIR` segment (None when unknowable)."""
    args = [a for a in words[1:] if a != "--"]
    if not args or cur is None:
        return None
    t = args[0]
    if "$" in t or "`" in t or t == "-":
        return None
    return os.path.normpath(os.path.join(cur, os.path.expanduser(t)))
