#!/usr/bin/env python3
"""Tell the py-scratch MCP server which Claude Code session it is serving.

The server writes each execution's artifacts into the session's temp directory, but
Claude Code never passes that path to MCP servers. The server can derive the layout
(<claude tmp>/claude-<uid>/<cwd slug>/<session id>/...), but not the session id: the
CLAUDE_CODE_SESSION_ID in its environment is the id it was *spawned* with, and /clear
starts a new session inside the same process, so the value goes stale. Guessing from
directory mtimes works most of the time, which is the worst kind of works.

Hooks do get the real session id, so this one records it in a file keyed by the
owning Claude Code process, found by walking the process tree rather than trusting
an environment variable. The MCP server walks up to the same process and reads the
same key, so both sides agree exactly.

Registered on SessionStart (including `clear`, which is the case that matters) and
SessionEnd. Prints nothing and always exits 0: a failure here must never disturb the
session, it just falls the server back to its own heuristics.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import time
from pathlib import Path

HANDOFF_VERSION = 1


def _scratch_root() -> Path:
    """Artifact root, keyed by uid because /tmp is shared across users;
    must mirror server.py exactly."""
    if os.name == "posix":
        return Path(tempfile.gettempdir()) / f"pyscratch-{os.getuid()}"
    return Path(tempfile.gettempdir()) / "pyscratch"


def _handoff_root() -> Path:
    """Handoff root; must mirror server.py exactly. $XDG_RUNTIME_DIR fits the
    handoff's session lifetime and is kernel-guaranteed private."""
    if os.name == "posix":
        xdg = os.environ.get("XDG_RUNTIME_DIR")
        if xdg and Path(xdg).is_dir():
            return Path(xdg) / "pyscratch"
    return _scratch_root()


def _secure_mkdir(path: Path) -> Path | None:
    """Create `path` 0700 and verify it is a real directory owned by us:
    reject symlinks and squatted dirs, strip group/other bits."""
    try:
        path.mkdir(mode=0o700, exist_ok=True)
    except OSError:
        return None
    if os.name != "posix":
        return path
    try:
        st = os.lstat(path)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
            return None
        if st.st_mode & 0o077:
            os.chmod(path, 0o700)
    except OSError:
        return None
    return path


def _stat_fields(pid: int) -> list[str]:
    """Fields of /proc/<pid>/stat from `state` onwards.

    comm (field 2) is parenthesised and may itself contain spaces and parentheses,
    so split on the last ')' rather than on whitespace.
    """
    with open(f"/proc/{pid}/stat", "rb") as fh:
        data = fh.read().decode("utf-8", "replace")
    _, _, rhs = data.partition("(")
    _, _, rest = rhs.rpartition(")")
    return rest.split()


def _comm(pid: int) -> str:
    with open(f"/proc/{pid}/comm", "rb") as fh:
        return fh.read().decode("utf-8", "replace").strip()


def _ppid(pid: int) -> int:
    return int(_stat_fields(pid)[1])


def _starttime(pid: int) -> int:
    # Field 22 (starttime); fields[] starts at field 3, so index 19.
    return int(_stat_fields(pid)[19])


def find_claude_process(start: int | None = None) -> tuple[int, int] | None:
    """Walk up the process tree to the nearest Claude Code process.

    Returns (pid, starttime). starttime disambiguates a recycled pid.
    """
    pid = start if start is not None else os.getpid()
    seen: set[int] = set()
    while pid > 1 and pid not in seen:
        seen.add(pid)
        try:
            if _comm(pid) == "claude":
                return pid, _starttime(pid)
            pid = _ppid(pid)
        except (OSError, ValueError, IndexError):
            return None
    return None


def handoff_dir() -> Path:
    override = os.environ.get("PY_SCRATCH_HANDOFF_DIR")
    if override:
        return Path(override)
    return _handoff_root() / "handoff"


def _ensure_handoff_dir() -> Path | None:
    """Create the handoff dir, refusing locations another user could tamper with."""
    override = os.environ.get("PY_SCRATCH_HANDOFF_DIR")
    if override:
        # An explicit override is the operator's choice; just create it.
        p = Path(override)
        try:
            p.mkdir(parents=True, exist_ok=True)
        except OSError:
            return None
        return p
    root = _secure_mkdir(_handoff_root())
    if root is None:
        return None
    return _secure_mkdir(root / "handoff")


def handoff_path(claude_pid: int, starttime: int) -> Path:
    return handoff_dir() / f"{claude_pid}-{starttime}.json"


def claude_temp_root() -> Path | None:
    """Root Claude Code uses for per-directory temp state."""
    getuid = getattr(os, "getuid", None)
    if getuid is None:
        return None
    tmp = os.environ.get("CLAUDE_CODE_TMPDIR") or os.environ.get("CLAUDE_TMPDIR")
    return Path(tmp or tempfile.gettempdir()) / f"claude-{getuid()}"


def session_paths(session_id: str, cwd: str) -> dict:
    root = claude_temp_root()
    if root is None:
        return {}
    session_dir = root / cwd.replace(os.sep, "-") / session_id
    return {
        "session_dir": str(session_dir),
        "scratchpad": str(session_dir / "scratchpad"),
    }


def prune_stale() -> None:
    """Drop handoffs whose Claude Code process is gone."""
    try:
        entries = list(handoff_dir().iterdir())
    except OSError:
        return
    for entry in entries:
        try:
            pid_s, _, start_s = entry.stem.partition("-")
            pid, start = int(pid_s), int(start_s)
        except ValueError:
            continue
        try:
            alive = _comm(pid) == "claude" and _starttime(pid) == start
        except (OSError, ValueError, IndexError):
            alive = False
        if not alive:
            try:
                entry.unlink()
            except OSError:
                pass


def write_handoff(payload: dict, claude_pid: int, starttime: int) -> None:
    directory = _ensure_handoff_dir()
    if directory is None:
        return
    target = directory / f"{claude_pid}-{starttime}.json"
    # Atomic replace: the server may read this at any moment. 0600 + O_NOFOLLOW
    # so the server's trust checks accept it; pid in the tmp name so concurrent
    # instances never collide.
    tmp_path = target.with_suffix(f".{os.getpid()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(str(tmp_path), flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, indent=2))
    os.replace(tmp_path, target)


def main() -> None:
    try:
        event = json.loads(sys.stdin.read() or "{}")
    except (json.JSONDecodeError, OSError):
        event = {}

    found = find_claude_process()
    if found is None:
        return
    claude_pid, starttime = found

    if event.get("hook_event_name") == "SessionEnd":
        try:
            handoff_path(claude_pid, starttime).unlink()
        except OSError:
            pass
        return

    session_id = event.get("session_id")
    cwd = event.get("cwd") or os.getcwd()
    if not session_id:
        return

    payload = {
        "version": HANDOFF_VERSION,
        "session_id": session_id,
        "cwd": cwd,
        "source": event.get("source"),
        "transcript_path": event.get("transcript_path"),
        "claude_pid": claude_pid,
        "updated_at": time.time(),
        **session_paths(session_id, cwd),
    }
    write_handoff(payload, claude_pid, starttime)
    prune_stale()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Never surface a hook failure to the user; the server has fallbacks.
        pass
    sys.exit(0)
