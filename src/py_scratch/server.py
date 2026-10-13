from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import logging
import os
import re
import signal
import stat
import sys
import tempfile
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore[no-redef]

from .package_map import IMPORT_TO_PYPI

_log = logging.getLogger("py-scratch")


def _setup_logging() -> None:
    level = os.environ.get("PY_SCRATCH_LOG_LEVEL", "INFO").upper()
    _log.setLevel(getattr(logging, level, logging.INFO))


_setup_logging()

_SCAN_SKIP = {".git", ".venv", "__pycache__", "node_modules", "build", "dist", ".tox", ".nox"}


def _find_projects(root: Path, max_depth: int = 3) -> list[Path]:
    """Find directories containing installable pyproject.toml (has [project] table).

    Scans root itself and subdirectories up to max_depth levels deep.
    """
    result: list[Path] = []

    def _scan(directory: Path, depth: int) -> None:
        # Every filesystem probe here can fail on a directory we merely happen to
        # be sitting in (an unreadable /tmp/.cache owned by another user is enough).
        # This runs at import, so an escaping error means the server never starts.
        pp = directory / "pyproject.toml"
        try:
            data = tomllib.loads(pp.read_text(encoding="utf-8"))
            if "project" in data:
                result.append(directory)
        except Exception:
            pass
        if depth >= max_depth:
            return
        try:
            children = sorted(directory.iterdir())
        except OSError:
            return
        for child in children:
            try:
                is_dir = child.is_dir()
            except OSError:
                continue
            if is_dir and child.name not in _SCAN_SKIP:
                _scan(child, depth + 1)

    _scan(root, 0)
    return result


def _read_config() -> dict | None:
    """Load .py-scratch.json from the cwd, or None if absent/unparseable."""
    config_path = Path.cwd() / ".py-scratch.json"
    if not config_path.exists():
        return None
    try:
        return json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        print("py-scratch: failed to parse .py-scratch.json, ignoring", file=sys.stderr)
        return None


def _load_local_packages() -> tuple[Path | None, list[Path]]:
    """Discover or load the project context and extra packages.

    Returns (project, extra_packages):
      - project: single project root for --project, or None
      - extra_packages: list of paths for --with
    """
    cwd = Path.cwd()
    config_path = cwd / ".py-scratch.json"

    if config_path.exists():
        data = _read_config()
        if data is None:
            return None, []
        project = None
        if "project" in data:
            project = (cwd / data["project"]).resolve()
        packages = [(cwd / p).resolve() for p in data.get("packages", [])]
        return project, packages

    # Auto-discovery
    found = _find_projects(cwd)
    if len(found) == 1:
        return found[0], []
    # 0 or 2+ projects: store for diagnostic, no project context
    return None, []


def _extract_project_name(project_dir: Path) -> str | None:
    pp = project_dir / "pyproject.toml"
    if not pp.exists():
        return None
    try:
        data = tomllib.loads(pp.read_text(encoding="utf-8"))
        return data.get("project", {}).get("name")
    except Exception:
        return None


_REQUIRES_PYTHON_RE = re.compile(r">=\s*(\d+\.\d+(?:\.\d+)?)")


def _extract_requires_python(project_dir: Path) -> str | None:
    """Extract the minimum Python version from a project's requires-python."""
    pp = project_dir / "pyproject.toml"
    if not pp.exists():
        return None
    try:
        data = tomllib.loads(pp.read_text(encoding="utf-8"))
        spec = data.get("project", {}).get("requires-python", "")
        m = _REQUIRES_PYTHON_RE.search(spec)
        if m:
            return m.group(1)
    except Exception:
        pass
    return None


# --- Discovery results (computed once at import time) ---

_DISCOVERED_PROJECTS = _find_projects(Path.cwd())
_PROJECT, _EXTRA_PACKAGES = _load_local_packages()
_REQUIRES_PYTHON = _extract_requires_python(_PROJECT) if _PROJECT else None

_RUN_ID = f"{time.strftime('%Y-%m-%d-%H-%M-%S')}-{os.getpid()}"


def _scratch_root() -> Path:
    """Artifact/log root, keyed by uid because /tmp is shared across users.
    Not $XDG_RUNTIME_DIR — artifacts should survive logout for debugging.
    Windows's temp dir is per-user already."""
    if os.name == "posix":
        return Path(tempfile.gettempdir()) / f"pyscratch-{os.getuid()}"
    return Path(tempfile.gettempdir()) / "pyscratch"


def _handoff_root() -> Path:
    """Handoff root; must mirror the hook exactly. $XDG_RUNTIME_DIR fits the
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


def _init_user_root() -> Path:
    root = _scratch_root()
    if _secure_mkdir(root) is None:
        # Only log privacy is at stake (the handoff is verified separately);
        # a findable path beats a random one, so warn and keep it.
        _log.warning("scratch root %s failed the ownership check; using it anyway", root)
    return root


_USER_ROOT = _init_user_root()


def _session_dir() -> Path:
    """Process-scoped fallback location for artifacts, used when no Claude Code
    scratchpad is available. Stable for the lifetime of the server."""
    cwd = os.getcwd()
    path_hash = hashlib.sha256(cwd.encode()).hexdigest()[:6]
    if _PROJECT:
        dir_name = _extract_project_name(_PROJECT) or _PROJECT.name
    else:
        dir_name = Path(cwd).name or "root"
    return _USER_ROOT / f"{dir_name}-{path_hash}" / _RUN_ID


def _slugify_cwd(cwd: str) -> str:
    """Mirror Claude Code's per-directory temp slug: /foo/bar -> -foo-bar."""
    return cwd.replace(os.sep, "-")


def _proc_stat_fields(pid: int) -> list[str]:
    """Fields of /proc/<pid>/stat from `state` onwards. comm may contain spaces and
    parentheses, so split on the last ')'."""
    with open(f"/proc/{pid}/stat", "rb") as fh:
        data = fh.read().decode("utf-8", "replace")
    _, _, rhs = data.partition("(")
    _, _, rest = rhs.rpartition(")")
    return rest.split()


def _find_claude_process() -> tuple[int, int] | None:
    """Walk up the process tree to the Claude Code process that owns this server.

    Returns (pid, starttime); starttime disambiguates a recycled pid. This is the
    key the SessionStart hook writes its handoff under, so both sides agree without
    either having to guess.
    """
    pid = os.getpid()
    seen: set[int] = set()
    while pid > 1 and pid not in seen:
        seen.add(pid)
        try:
            with open(f"/proc/{pid}/comm", "rb") as fh:
                if fh.read().decode("utf-8", "replace").strip() == "claude":
                    return pid, int(_proc_stat_fields(pid)[19])
            pid = int(_proc_stat_fields(pid)[1])
        except (OSError, ValueError, IndexError):
            return None
    return None


def _handoff_dir() -> Path:
    override = os.environ.get("PY_SCRATCH_HANDOFF_DIR")
    if override:
        return Path(override)
    return _handoff_root() / "handoff"


def _read_trusted_json(path: Path) -> dict | None:
    """Read JSON only from a file no other user could have planted: no
    symlinks, owned by us, not group/other-writable. A spoofed handoff would
    mean executing a script.py someone else can swap."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(str(path), flags)
    except OSError:
        return None
    try:
        fh = os.fdopen(fd, "r", encoding="utf-8")
    except OSError:
        os.close(fd)
        return None
    with fh:
        try:
            if os.name == "posix":
                st = os.fstat(fh.fileno())
                if st.st_uid != os.getuid() or st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                    _log.warning("ignoring handoff %s: not exclusively ours", path)
                    return None
            data = json.loads(fh.read())
        except (OSError, json.JSONDecodeError):
            return None
    return data if isinstance(data, dict) else None


def _read_handoff() -> dict | None:
    """Read the session handoff written by the SessionStart hook, if installed."""
    found = _find_claude_process()
    if found is None:
        return None
    claude_pid, starttime = found
    return _read_trusted_json(_handoff_dir() / f"{claude_pid}-{starttime}.json")


def _guess_session_dir() -> Path | None:
    """Fallback for when the hook is not installed (manual MCP config).

    CLAUDE_CODE_SESSION_ID is in our environment, but it is the id we were *spawned*
    with, and /clear starts a new session inside the same process, so it goes stale.
    Prefer it when its directory exists, else take the most recently touched session
    directory for this cwd.
    """
    getuid = getattr(os, "getuid", None)
    if getuid is None:
        return None
    tmp = os.environ.get("CLAUDE_CODE_TMPDIR") or os.environ.get("CLAUDE_TMPDIR")
    root = Path(tmp or tempfile.gettempdir()) / f"claude-{getuid()}" / _slugify_cwd(os.getcwd())

    session = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if session and (root / session).is_dir():
        return root / session
    try:
        return max(
            (d for d in root.iterdir() if d.is_dir()),
            key=lambda d: d.stat().st_mtime,
            default=None,
        )
    except OSError:
        return None


def _claude_scratchpad() -> Path | None:
    """Directory to put this session's execution artifacts in, or None.

    Claude Code owns `<session dir>/scratchpad` and creates it lazily with a
    non-recursive mkdir, so creating it ourselves could make its own mkdir fail with
    EEXIST. Use it when it is already there — that is the directory the user asked
    for and reads from it are frictionless — and otherwise sit beside it in the same
    session directory, which is cleaned up on the same schedule.
    """
    if os.environ.get("PY_SCRATCH_USE_SCRATCHPAD", "1") == "0":
        return None

    handoff = _read_handoff()
    if handoff:
        scratchpad = handoff.get("scratchpad")
        if scratchpad and Path(scratchpad).is_dir():
            return Path(scratchpad)
        session_dir = handoff.get("session_dir")
        if session_dir and Path(session_dir).is_dir():
            return Path(session_dir)
        return None

    guessed = _guess_session_dir()
    if guessed is None:
        return None
    pad = guessed / "scratchpad"
    return pad if pad.is_dir() else guessed


def _artifact_root() -> Path:
    """Where this call's execution artifacts go.

    Resolved per call rather than at import, so a server that outlives a /clear
    follows the live session's scratchpad instead of writing into a dead one.
    """
    explicit = os.environ.get("PY_SCRATCH_DIR") or _CONFIG_SCRATCH_DIR
    if explicit:
        return Path(explicit).expanduser() / _RUN_ID
    pad = _claude_scratchpad()
    if pad is not None:
        return pad / "py-scratch" / _RUN_ID
    return _session_dir()


_config = _read_config() or {}
_CONFIG_SCRATCH_DIR = _config.get("scratch_dir")

SESSION_DIR = _session_dir()
# Import-time: degrade to no file logging rather than not starting.
try:
    SESSION_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
except OSError:
    pass
CODE_FILE = "code.py"
STDOUT_FILE = "stdout"
STDERR_FILE = "stderr"
TRACE_FILE = "trace"
PID_FILE = "pid"
SERVER_LOG = SESSION_DIR / "server.log"
SERVER_STDERR_LOG = SESSION_DIR / "server-stderr.log"

try:
    _log_handler = logging.FileHandler(SERVER_LOG)
    _log_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    _log.addHandler(_log_handler)
except OSError:
    pass


class _StderrTee:
    """Duplicate everything written to sys.stderr into the artifact directory.

    Claude Code pipes an MCP server's stderr but discards it unless started with
    --mcp-debug, so an unhandled traceback in this process would otherwise leave no
    trace anywhere on disk. Keep the passthrough (it is still useful under
    --mcp-debug) and mirror it to a file we control.
    """

    def __init__(self, stream, path: Path) -> None:
        self._stream = stream
        try:
            self._file = open(path, "a", encoding="utf-8", errors="replace")
        except OSError:
            self._file = None

    def write(self, data: str) -> int:
        if self._file is not None:
            try:
                self._file.write(data)
                self._file.flush()
            except OSError:
                pass
        try:
            return self._stream.write(data)
        except Exception:
            return len(data)

    def flush(self) -> None:
        for target in (self._file, self._stream):
            if target is None:
                continue
            try:
                target.flush()
            except Exception:
                pass

    def fileno(self) -> int:
        return self._stream.fileno()

    def isatty(self) -> bool:
        return False

    def __getattr__(self, name):
        return getattr(self._stream, name)


def _install_crash_logging() -> None:
    """Make sure nothing dies quietly: tee stderr, log unhandled exceptions, and
    dump a native traceback if the interpreter itself crashes."""
    sys.stderr = _StderrTee(sys.stderr, SERVER_STDERR_LOG)

    def _excepthook(exc_type, exc, tb) -> None:
        _log.critical("unhandled exception", exc_info=(exc_type, exc, tb))
        sys.__excepthook__(exc_type, exc, tb)

    sys.excepthook = _excepthook

    try:
        import faulthandler

        faulthandler.enable(file=open(SERVER_STDERR_LOG, "a", encoding="utf-8"))
    except Exception:
        pass


_install_crash_logging()

_log.info("cwd: %s", Path.cwd())
_log.info("run id: %s", _RUN_ID)
_log.info("artifact root: %s", _artifact_root())
_log.info("discovered projects: %s", _DISCOVERED_PROJECTS)
_log.info("selected project: %s", _PROJECT)
_log.info("extra packages: %s", _EXTRA_PACKAGES)
_log.info("requires-python: %s", _REQUIRES_PYTHON)
_exec_counter = 0
_runs: dict[int, _Run] = {}
_supervisors: set[asyncio.Task] = set()


def _next_execution_id() -> int:
    global _exec_counter
    _exec_counter += 1
    return _exec_counter


def _exec_dir() -> tuple[int, Path]:
    eid = _next_execution_id()
    for base in (_artifact_root(), SESSION_DIR):
        d = base / str(eid)
        try:
            d.mkdir(mode=0o700, parents=True, exist_ok=True)
            return eid, d
        except OSError as exc:
            _log.warning("cannot create exec dir %s (%s), falling back", d, exc)
    raise OSError(
        f"no writable artifact directory (tried {_artifact_root()} and {SESSION_DIR})"
    )


def _write_script(path: Path, code: str, deps: list[str]) -> None:
    lines: list[str] = []
    if deps:
        lines.append("# /// script")
        lines.append('# requires-python = ">=3.10"')
        lines.append("# dependencies = [")
        for d in deps:
            lines.append(f'#   "{d}",')
        lines.append("# ]")
        lines.append("# ///")
        lines.append("")
    lines.append(code)
    path.write_text("\n".join(lines), encoding="utf-8")


def _build_base_cmd() -> list[str]:
    cmd = ["uv", "run", "--quiet"]
    if _PROJECT:
        cmd.extend(["--project", str(_PROJECT)])
        if _REQUIRES_PYTHON:
            cmd.extend(["--python", f">={_REQUIRES_PYTHON}"])
    for pkg in _EXTRA_PACKAGES:
        cmd.extend(["--with", str(pkg)])
    return cmd


def _build_command(*argv: str, extra_deps: list[str] | None = None) -> list[str]:
    cmd = _build_base_cmd()
    for dep in extra_deps or []:
        cmd.extend(["--with", dep])
    cmd.extend(argv)
    return cmd


_ENV_PRIME_TIMEOUT = 300

_primed_deps: set[str] = set()

# PEP 508 URL specifiers (e.g. "foo @ git+https://evil.com") let callers
# install from arbitrary URLs. Block the two markers that distinguish a URL
# specifier from a plain name+version constraint.
_UNSAFE_DEP = re.compile(r"@|://")


def _validate_deps(deps: list[str]) -> None:
    for dep in deps:
        if _UNSAFE_DEP.search(dep):
            raise ValueError(f"Unsafe dependency specifier: {dep}")


async def _ensure_env(deps: list[str]) -> None:
    """Run a no-op script with --with flags to force uv to download and cache
    the environment before the real run. Without this, a first-time large dep
    download (e.g. pyarrow, 46 MB) would run under the hard limit and get killed,
    leaving the cache empty — every subsequent attempt fails the same way. (The
    estimate clock itself starts only once the script does, see _supervise.)"""
    new_deps = [d for d in deps if d not in _primed_deps]
    if not new_deps:
        return
    cmd = _build_command("python3", "-c", "exit(0)", extra_deps=new_deps)
    _log.info("priming env: %s", cmd)
    t0 = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
            cwd=os.getcwd(), start_new_session=True,
        )
    except OSError as exc:
        # Priming is best-effort: if uv cannot even be spawned, let the real run
        # report the failure properly instead of aborting here.
        _log.warning("env priming could not start (%s)", exc)
        return
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=_ENV_PRIME_TIMEOUT)
    except asyncio.TimeoutError:
        _kill_process(proc)
        await proc.wait()
        _log.warning("env priming timed out after %ds for deps %s", _ENV_PRIME_TIMEOUT, new_deps)
        return
    duration_ms = int((time.monotonic() - t0) * 1000)
    if proc.returncode == 0:
        _primed_deps.update(new_deps)
        _log.info("env primed in %dms for deps %s", duration_ms, new_deps)
    else:
        _log.warning("env priming failed (exit %d) for deps %s: %s",
                      proc.returncode, new_deps, stderr.decode(errors="replace")[-500:])


_MAX_PREVIEW_READ = 8 * 1024 * 1024


def _read_log(path: Path) -> str:
    """Read a child's log file without ever raising: output may be huge, may be
    truncated mid-codepoint, or may have vanished."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            data = fh.read(_MAX_PREVIEW_READ)
            if fh.read(1):
                data += "\n[truncated: output exceeds 8 MiB, see the full file on disk]"
            return data
    except OSError:
        return ""


def _preview(path: Path, head: int, tail: int) -> str:
    if not path.exists():
        return ""
    lines = _read_log(path).splitlines()
    if not lines:
        return ""
    parts = []
    if head > 0:
        parts.extend(lines[:head])
    if tail > 0:
        tail_lines = lines[-tail:]
        if head > 0 and len(lines) <= head + tail:
            tail_lines = lines[head:]
        parts.extend(tail_lines)
    return "\n".join(parts)


_IMPORT_ERROR_RE = re.compile(
    r"(?:ModuleNotFoundError|ImportError): No module named ['\"](\w+)"
)


def _infer_dep(stderr_path: Path) -> str | None:
    if not stderr_path.exists():
        return None
    m = _IMPORT_ERROR_RE.search(_read_log(stderr_path))
    if not m:
        return None
    return IMPORT_TO_PYPI.get(m.group(1), m.group(1))


_FATAL_SIGNALS = (signal.SIGKILL, signal.SIGSEGV, signal.SIGBUS, signal.SIGABRT, signal.SIGILL)

_SIGNAL_HINTS = {
    "SIGKILL": "killed by SIGKILL — usually the OOM killer or an external kill",
    "SIGSEGV": "segfaulted — likely a crash inside a native extension module",
    "SIGBUS": "bus error — often a truncated mmap'd file or bad memory",
    "SIGABRT": "aborted — a native library called abort()",
    "SIGILL": "illegal instruction — a binary wheel built for a different CPU",
}


def _signal_from_exit(exit_code: int) -> int | None:
    """Recover the signal that killed the process, if any.

    A directly-killed child reports a negative returncode, but the child we spawn is
    `uv run`, which reports the shell convention 128+N instead. 128+N is ambiguous
    with a script that genuinely exited with that status; callers that act on this
    (the crash retry) additionally require the run to have produced no output.
    """
    if exit_code < 0:
        return -exit_code
    if 128 < exit_code < 128 + signal.NSIG:
        return exit_code - 128
    return None


def _describe_exit(
    exit_code: int | None, timed_out: bool, hard_limit: float
) -> tuple[str, str | None]:
    """Classify how the child ended. Returns (status, human explanation)."""
    if timed_out:
        return "timeout", f"killed after exceeding the {hard_limit:g}s hard limit"
    if exit_code is None:
        return "not_started", "the interpreter could not be started"
    if exit_code == 0:
        return "ok", None
    signum = _signal_from_exit(exit_code)
    if signum is None or signum not in _FATAL_SIGNALS:
        return "failed", None
    try:
        signame = signal.Signals(signum).name
    except ValueError:
        signame = f"signal {signum}"
    hint = _SIGNAL_HINTS.get(signame, f"killed by {signame}")
    if exit_code > 0:
        hint += f" (reported as exit code {exit_code})"
    return "crashed", hint


def _is_crash_signal(exit_code: int | None) -> bool:
    """Did the process die from something unrelated to the script's own logic?

    A single retry is worth it only when the child also produced no output at all,
    i.e. it died before doing anything observable, so a retry cannot double any
    side effects.
    """
    if exit_code is None:
        return False
    signum = _signal_from_exit(exit_code)
    return signum in _FATAL_SIGNALS


# --- runs: one subprocess each, tracked in a registry so a call can return early ---

_HARD_LIMIT_FACTOR = 10
# How long uv may spend resolving/installing before the script starts. The estimate
# clock does not run during this time, so it needs its own bound.
_START_TIMEOUT = 600
_RETAIN_RUNS = 200

_SAMPLE_SIGNAL = getattr(signal, "SIGUSR1", None)


class _UsageError(Exception):
    """A bad call (unknown run_id, bad action...), reported as isError."""


@dataclass
class _Run:
    id: int
    intent: str
    dir: Path
    deps: list[str]
    estimate: float
    hard_limit: float
    proc: asyncio.subprocess.Process | None = None
    pid: int | None = None          # the interpreter, not the `uv run` wrapper
    main_ident: int | None = None   # its main thread, to pick it out of a dump
    t_spawn: float = field(default_factory=time.monotonic)
    t_start: float | None = None    # when the user's script began executing
    t_end: float | None = None
    state: str = "running"
    exit_code: int | None = None
    explanation: str | None = None
    kill_reason: str | None = None  # killed | cancelled | timeout | start_timeout
    last_frame: dict | None = None
    auto_installed: str | None = None
    retry_of: int | None = None
    started: asyncio.Event = field(default_factory=asyncio.Event)
    done: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def pgid(self) -> int | None:
        # children are session leaders, so the group id is the wrapper's pid
        return self.proc.pid if self.proc is not None else None

    @property
    def elapsed(self) -> float:
        start = self.t_start if self.t_start is not None else self.t_spawn
        return (self.t_end if self.t_end is not None else time.monotonic()) - start


# Runs the user's file through a thin wrapper so the server can ask it where it is:
# SIGUSR1 makes faulthandler dump every thread's stack into the trace file, which
# works even while the interpreter is stuck in a C extension. The pid file doubles
# as the "script has started" marker. The code is exec'd from code.py so tracebacks
# and the dump both name that file and its real line numbers.
_BOOTSTRAP = '''\
import faulthandler, os, signal, sys, threading, traceback


def _main():
    code, trace, pidfile = sys.argv[1:4]
    fh = open(trace, "ab", buffering=0)
    sys._pyscratch_trace = fh
    if hasattr(signal, "SIGUSR1"):
        faulthandler.register(signal.SIGUSR1, file=fh, all_threads=True)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    with open(pidfile + ".tmp", "w") as f:
        f.write(f"{os.getpid()} {threading.get_ident()}")
    os.replace(pidfile + ".tmp", pidfile)
    import __main__
    sys.argv = [code]
    sys.path[0] = os.path.dirname(code)
    __main__.__file__ = code
    with open(code, encoding="utf-8") as f:
        src = f.read()
    try:
        exec(compile(src, code, "exec", dont_inherit=True), __main__.__dict__)
    except SystemExit:
        raise
    except BaseException as exc:
        traceback.print_exception(type(exc), exc, exc.__traceback__.tb_next)
        sys.exit(1)


_main()
'''


def _read_pidfile(run_dir: Path) -> tuple[int, int, float] | None:
    """(pid, main thread ident, time the script started) once the wrapper wrote it."""
    p = run_dir / PID_FILE
    try:
        pid, ident = p.read_text().split()
        return int(pid), int(ident), p.stat().st_mtime
    except (OSError, ValueError):
        return None


def _prune_runs() -> None:
    if len(_runs) <= _RETAIN_RUNS:
        return
    for rid in [r for r, run in _runs.items() if run.done.is_set()][: len(_runs) - _RETAIN_RUNS]:
        del _runs[rid]


async def _launch(intent: str, code: str, deps: list[str], estimate: float) -> _Run:
    """Start a run and return at once; _supervise tracks it from here on."""
    try:
        _validate_deps(deps)
    except ValueError as exc:
        raise _UsageError(str(exc)) from None
    run_id, run_dir = _exec_dir()
    run = _Run(run_id, intent, run_dir, deps, estimate, estimate * _HARD_LIMIT_FACTOR)
    _runs[run_id] = run
    _prune_runs()
    code_path = run_dir / CODE_FILE
    boot_path = run_dir / "bootstrap.py"
    if _PROJECT:
        _write_script(code_path, code, [])
        _write_script(boot_path, _BOOTSTRAP, [])
        extra_deps = deps
    else:
        # PEP 723 metadata is read from the file uv runs, i.e. the bootstrap
        _write_script(code_path, code, deps)
        _write_script(boot_path, _BOOTSTRAP, deps)
        extra_deps = []
    # Only prime when deps are passed as --with flags (_PROJECT path).
    # In the non-project path, deps are PEP 723 inline metadata in the script
    # file itself, so uv resolves them differently and a --with prime would
    # warm a different cache key.
    if extra_deps:
        await _ensure_env(extra_deps)
    cmd = _build_command(
        str(boot_path), str(code_path), str(run_dir / TRACE_FILE), str(run_dir / PID_FILE),
        extra_deps=extra_deps,
    )
    _log.info("run %s: %s", run_id, cmd)
    try:
        with open(run_dir / STDOUT_FILE, "wb") as out_f, open(run_dir / STDERR_FILE, "wb") as err_f:
            run.proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=out_f, stderr=err_f, cwd=os.getcwd(), start_new_session=True,
            )
    except OSError as exc:
        # uv missing from PATH, temp filesystem full, fork failure under memory
        # pressure. Report it as a failed execution rather than letting it escape.
        _log.error("run %s could not start: %s", run_id, exc)
        run.kill_reason = "start_error"
        run.explanation = f"{type(exc).__name__}: {exc}"
        _finalize(run, None)
        return run
    task = asyncio.create_task(_supervise(run))
    _supervisors.add(task)
    task.add_done_callback(_supervisors.discard)
    return run


async def _supervise(run: _Run) -> None:
    """Own a run until it ends: note when the script actually starts, enforce the
    hard limit, collect the exit status. Runs independently of any tool call, so
    a call can return while the script carries on."""
    exit_code = None
    try:
        waiter = asyncio.ensure_future(run.proc.wait())
        deadline = time.monotonic() + _START_TIMEOUT
        while not waiter.done():
            info = _read_pidfile(run.dir)
            if info is not None:
                break
            if time.monotonic() > deadline:
                run.kill_reason = "start_timeout"
                _kill_process(run.proc)
                break
            await asyncio.wait({waiter}, timeout=0.01)
        info = _read_pidfile(run.dir)
        if info is not None:
            run.pid, run.main_ident, mtime = info
            # the pid file was written when the script started; convert its wall
            # clock age to the monotonic clock
            run.t_start = time.monotonic() - max(0.0, time.time() - mtime)
        run.started.set()
        while not waiter.done() and run.kill_reason is None:
            start = run.t_start if run.t_start is not None else run.t_spawn
            remaining = start + run.hard_limit - time.monotonic()
            if remaining <= 0:
                run.kill_reason = "timeout"
                await _sample(run)  # leave the last known position behind
                _kill_process(run.proc)
                break
            await asyncio.wait({waiter}, timeout=remaining)
        exit_code = await waiter
    except Exception:
        _log.exception("supervising run %s failed", run.id)
        _kill_process(run.proc)
    finally:
        _finalize(run, exit_code)


def _finalize(run: _Run, exit_code: int | None) -> None:
    run.t_end = time.monotonic()
    run.exit_code = exit_code
    if run.kill_reason in ("killed", "cancelled"):
        run.state = "killed"
        run.explanation = (
            "cancelled by the client" if run.kill_reason == "cancelled" else "killed on request"
        )
    elif run.kill_reason == "start_timeout":
        run.state = "not_started"
        run.explanation = f"the script did not start within {_START_TIMEOUT}s"
    elif run.kill_reason == "start_error":
        run.state = "not_started"
    else:
        run.state, run.explanation = _describe_exit(
            exit_code, run.kill_reason == "timeout", run.hard_limit
        )
    if run.state in ("crashed", "not_started"):
        _log.warning("run %s %s: %s", run.id, run.state, run.explanation)
    meta = {
        "run_id": run.id,
        "intent": run.intent,
        "exit_code": exit_code,
        "status": run.state,
        "duration_s": round(run.elapsed, 3),
        "estimate_s": run.estimate,
        "hard_limit_s": run.hard_limit,
        "dependencies": run.deps,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if run.explanation:
        meta["explanation"] = run.explanation
    if _PROJECT:
        meta["project"] = str(_PROJECT)
    if _EXTRA_PACKAGES:
        meta["extra_packages"] = [str(p) for p in _EXTRA_PACKAGES]
    meta["server_log"] = str(SERVER_LOG)
    try:
        (run.dir / "meta.json").write_text(
            json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except OSError as exc:
        _log.warning("could not write meta.json for run %s: %s", run.id, exc)
    run.started.set()
    run.done.set()


# --- stack sampling ---

_TRACE_HEAD = re.compile(r"^(?:Current thread|Thread) (0x[0-9a-fA-F]+)")
_TRACE_FRAME = re.compile(r'^\s+File "(.*)", line (\d+) in (.*)$')


def _parse_trace(text: str, main_ident: int | None) -> list[tuple[str, int, str]]:
    """Frames (innermost first) of the main thread from a faulthandler dump."""
    threads: list[tuple[int, list[tuple[str, int, str]]]] = []
    for ln in text.splitlines():
        m = _TRACE_HEAD.match(ln)
        if m:
            threads.append((int(m.group(1), 16), []))
            continue
        m = _TRACE_FRAME.match(ln)
        if m and threads:
            threads[-1][1].append((m.group(1), int(m.group(2)), m.group(3)))
    if not threads:
        return []
    # fall back to the last-listed thread, which is the oldest, i.e. the main one
    return next((fr for ident, fr in threads if ident == main_ident), threads[-1][1])


def _source_line(path: str, lineno: int) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace").splitlines()[lineno - 1].strip()
    except (OSError, IndexError):
        return ""


def _pick_frame(frames: list[tuple[str, int, str]], code_path: Path) -> dict | None:
    """The innermost frame in the user's own file, which is what they can act on;
    when the script is deeper in a library, say so too."""
    if not frames:
        return None
    own = next((f for f in frames if f[0] == str(code_path)), None)
    file, line, func = own or frames[0]
    frame = {"file": file, "line": line, "func": func, "source": _source_line(file, line)}
    if own is not None and own is not frames[0]:
        frame["inner"] = f"{Path(frames[0][0]).name}:{frames[0][1]} {frames[0][2]}"
    return frame


async def _sample(run: _Run) -> None:
    """Dump the interpreter's stacks into the trace file and remember where the
    main thread is. Best effort: a failed sample leaves the previous one in place."""
    if _SAMPLE_SIGNAL is None or run.pid is None or run.done.is_set():
        return
    trace = run.dir / TRACE_FILE
    try:
        os.truncate(trace, 0)
        os.kill(run.pid, _SAMPLE_SIGNAL)
    except OSError:
        return
    for _ in range(50):
        await asyncio.sleep(0.01)
        try:
            if trace.stat().st_size > 0:
                break
        except OSError:
            return
    await asyncio.sleep(0.03)  # let a multi-thread dump finish
    frame = _pick_frame(
        _parse_trace(_read_log(trace), run.main_ident), run.dir / CODE_FILE
    )
    if frame is not None:
        run.last_frame = frame


# --- waiting, killing, reporting ---

# the msg id of the tool call being handled, so a cancellation can find its run
_current_msg: contextvars.ContextVar = contextvars.ContextVar("py_scratch_msg", default=None)
_blocking: dict = {}


async def _settle(run: _Run, period: float, from_start: bool) -> None:
    """Block until the run ends or `period` seconds pass (counted from the script's
    start for the first call, from now for a wait); if it is still going, sample it."""
    msg_id = _current_msg.get()
    if msg_id is not None:
        _blocking[msg_id] = run
    try:
        await run.started.wait()
        if not run.done.is_set():
            if from_start:
                period = max(0.0, period - run.elapsed)
            try:
                await asyncio.wait_for(run.done.wait(), period)
            except asyncio.TimeoutError:
                pass
        if not run.done.is_set():
            await _sample(run)
    finally:
        if msg_id is not None:
            _blocking.pop(msg_id, None)


async def _kill_run(run: _Run, reason: str) -> None:
    if run.proc is not None and run.proc.returncode is None and not run.done.is_set():
        run.kill_reason = reason
        _kill_process(run.proc)
    try:
        await asyncio.wait_for(run.done.wait(), 10)
    except asyncio.TimeoutError:
        pass


def _kill_all_runs() -> None:
    for run in _runs.values():
        if not run.done.is_set() and run.proc is not None:
            run.kill_reason = run.kill_reason or "killed"
            _kill_process(run.proc)


def _cancel_request(request_id) -> None:
    """notifications/cancelled: the client gave up on a call that is blocking on a
    run, so nobody is left to read its result; stop the run too."""
    run = _blocking.get(request_id)
    if run is not None and not run.done.is_set():
        _log.info("request %s cancelled, killing run %s", request_id, run.id)
        run.kill_reason = "cancelled"
        if run.proc is not None:
            _kill_process(run.proc)


def _kill_process(proc) -> None:
    """Kill a child and everything it spawned; never raise.

    The direct child is `uv run`, which execs the real interpreter as a grandchild.
    Killing only `uv` leaves that interpreter running with nobody waiting on it, so
    a timed-out script would keep burning CPU and holding files open. Children are
    started in their own session, so the whole group can be signalled at once.
    """
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        return
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        proc.kill()
    except (ProcessLookupError, OSError):
        pass


def _has_output(run_dir: Path) -> bool:
    for name in (STDOUT_FILE, STDERR_FILE):
        try:
            if (run_dir / name).stat().st_size > 0:
                return True
        except OSError:
            continue
    return False


def _fmt_secs(x: float) -> str:
    return f"{x:.2f}s" if x < 10 else f"{x:.1f}s"


def _status_line(run: _Run) -> str:
    names = [CODE_FILE, STDOUT_FILE, STDERR_FILE]
    try:
        if (run.dir / TRACE_FILE).stat().st_size > 0:
            names.append(TRACE_FILE)
    except OSError:
        pass
    where = f"run {run.id} · {run.dir}/{{{','.join(names)}}}"
    el = _fmt_secs(run.elapsed)
    if run.state == "running":
        head = f"running {el} (estimate {run.estimate:g}s, hard limit {run.hard_limit:g}s)"
    elif run.state in ("ok", "failed", "crashed"):
        head = f"{run.state} exit={run.exit_code} {el}"
    elif run.state == "timeout":
        head = f"timeout (hard limit {run.hard_limit:g}s) {el}"
    elif run.state == "killed":
        head = f"killed {el}"
    else:
        head = run.state
    line = f"{head} · {where}"
    if run.auto_installed:
        line += f" · auto-installed {run.auto_installed}"
    if run.retry_of is not None:
        line += f" · retry of run {run.retry_of}"
    return line


def _render(run: _Run, head: int, tail: int, notes: list[str] | None = None) -> str:
    """The whole tool result: status line first, then only what is relevant."""
    out = [_status_line(run)]
    if run.state in ("crashed", "not_started") and run.explanation:
        out.append(run.explanation)
    frame = run.last_frame
    if frame:
        label = "at line" if run.state == "running" else "last seen at line"
        text = f"{label} {frame['line']}: {frame['source']}"
        if frame.get("inner"):
            text += f"  [in {frame['inner']}]"
        out.append(text)
    stdout = _preview(run.dir / STDOUT_FILE, head, tail)
    if stdout:
        out.append(stdout)
    if run.state == "running":
        out.append(f'→ run_python_script(run_id={run.id}, action="wait"|"kill")')
    stderr_path = run.dir / STDERR_FILE
    if run.state in ("ok", "running"):
        n = len(_read_log(stderr_path).splitlines())
        if n:
            out.append(f"(stderr: {n} lines, see file)")
    else:
        stderr = _preview(stderr_path, head, tail)
        if stderr:
            out.append("stderr:\n" + stderr)
    if notes:
        out.append("note: " + "; ".join(notes))
    return "\n".join(out)


def _run_status(run: _Run) -> dict:
    return {
        "id": run.id,
        "state": run.state,
        "elapsed": round(run.elapsed, 3),
        "estimate": run.estimate,
        "hard_limit": run.hard_limit,
        "exit_code": run.exit_code,
        "pgid": run.pgid,
        "last_frame": run.last_frame,
        "dir": str(run.dir),
        "intent": run.intent,
    }


def _as_int(value, default: int, low: int, high: int) -> int:
    """Coerce a numeric argument that may arrive as a string or float."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, n))


def _as_float(value, default, low: float, high: float):
    try:
        n = float(value)
    except (TypeError, ValueError):
        return default
    if n != n:  # NaN
        return default
    return max(low, min(high, n))


def _normalise_args(intent, code, deps, time_estimate, head, tail):
    """Accept the loose shapes models actually send: numbers as strings, a bare
    string where a list is expected, None for optionals."""
    if isinstance(deps, str):
        deps = [deps]
    deps = [str(d) for d in (deps or [])]
    return (
        str(intent) if intent is not None else "",
        code if isinstance(code, str) else str(code),
        deps,
        _as_float(time_estimate, 10.0, 0.1, 86400.0),
        _as_int(head, 0, 0, 100000),
        _as_int(tail, 5, 0, 100000),
    )


def _lookup_run(run_id) -> _Run:
    try:
        run = _runs.get(int(run_id))
    except (TypeError, ValueError):
        run = None
    if run is None:
        known = ", ".join(str(r) for r in list(_runs)[-10:]) or "none"
        raise _UsageError(f"Unknown run_id {run_id!r}. Known runs: {known}.")
    return run


async def run_python_script(
    intent: str = "",
    code: str | None = None,
    dependencies: list[str] | None = None,
    time_estimate: float | None = None,
    head: int = 0,
    tail: int = 5,
    run_id: int | None = None,
    action: str | None = None,
    argument_notes: list[str] | None = None,
) -> str:
    notes = argument_notes or []
    if run_id is not None:
        return await _follow_up(run_id, action, time_estimate, head, tail, notes)
    if action is not None:
        raise _UsageError("'action' needs a 'run_id'.")
    deps = list(dependencies or [])
    intent, code, deps, estimate, head, tail = _normalise_args(
        intent, code, deps, time_estimate, head, tail
    )
    run = await _launch(intent, code, deps, estimate)
    await _settle(run, estimate, from_start=True)

    # The retries below only make sense for a run that finished within its
    # estimate; one that is still running is handed back to the caller as-is.

    # auto-retry on ImportError if no deps were declared
    if run.state == "failed" and not deps:
        inferred = _infer_dep(run.dir / STDERR_FILE)
        if inferred:
            deps = [inferred]
            run = await _launch(intent, code, deps, estimate)
            run.auto_installed = inferred
            await _settle(run, estimate, from_start=True)

    # A child that died on a fatal signal without producing a single byte never got
    # far enough to have side effects, and the cause (OOM sweep, a flaky native
    # import, a bad page) is often transient. Retry exactly once.
    if (
        run.state == "crashed"
        and _is_crash_signal(run.exit_code)
        and not _has_output(run.dir)
    ):
        _log.warning("run %s crashed with no output, retrying once", run.id)
        first = run
        run = await _launch(intent, code, deps, estimate)
        run.retry_of = first.id
        run.auto_installed = first.auto_installed
        await _settle(run, estimate, from_start=True)
        if run.state == "crashed":
            run.explanation = (
                f"{run.explanation}; reproduced on retry, so this is not a transient failure"
            )

    return _render(run, head, tail, notes)


async def _follow_up(run_id, action, time_estimate, head, tail, notes) -> str:
    run = _lookup_run(run_id)
    head = _as_int(head, 0, 0, 100000)
    tail = _as_int(tail, 5, 0, 100000)
    action = action or "wait"
    if action == "kill":
        await _kill_run(run, "killed")
    elif action == "wait":
        period = _as_float(time_estimate, None, 0.1, 86400.0)
        if period is not None and not run.done.is_set():
            # a fresh estimate renews the grace period as well, so a script that
            # turns out to need minutes is not shot at the original limit
            run.estimate = period
            run.hard_limit = max(run.hard_limit, run.elapsed + period * _HARD_LIMIT_FACTOR)
        await _settle(run, period if period is not None else run.estimate, from_start=False)
    else:
        raise _UsageError(f"Unknown action {action!r}: use \"wait\" or \"kill\".")
    return _render(run, head, tail, notes)


# --- MCP protocol (JSON-RPC over stdio) ---

_BASE_DESCRIPTION = (
    "Execute a Python script with optional dependencies. "
    "Use this for any Python longer than a couple lines or requiring third-party packages. "
    "Code is passed as a raw string — no shell escaping needed. "
    "Dependencies are installed automatically via uv.\n\n"
    "Always set time_estimate to your honest estimate of how many seconds the script should "
    "take. If it is still running when that time is up, the call returns immediately with "
    "state \"running\", the line it is currently executing and a run_id, while the script "
    "keeps running in the background (hard-killed at 10x the estimate). Then call again with "
    "run_id and action=\"wait\" or \"kill\". A script you expected to take a second that "
    "is still running after ten is almost always an infinite loop or accidentally quadratic "
    "code: kill it and fix it rather than waiting.\n\n"
    "The result is plain text: a status line (state, exit code, time, run id, the run "
    "directory holding code.py, stdout, stderr, and trace), then a stdout preview. "
    "Read the files in the run directory with the Read tool for the full output."
)

_TOOL_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {
            "type": "string",
            "description": "What are you trying to do with this script?",
        },
        "code": {
            "type": "string",
            "description": "Raw Python source code. Required unless run_id is given.",
        },
        "dependencies": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Optional PyPI package names",
            "default": [],
        },
        "time_estimate": {
            "type": "number",
            "description": (
                "IMPORTANT: your best estimate, in seconds, of how long this script should "
                "take (default 10). Think about the data size and the work involved. If the "
                "script is still running after this many seconds the call returns with "
                "state \"running\" and the current line, and the script continues in the "
                "background; it is killed at 10x the estimate. Do not pad the estimate "
                "\"to be safe\": an honest short estimate surfaces infinite loops and "
                "quadratic code within seconds instead of minutes. With action=\"wait\", "
                "the time to wait this round (default: the original estimate)."
            ),
            "default": 10,
        },
        "head": {
            "type": "integer",
            "description": "Number of lines to show from the start of stdout/stderr preview",
            "default": 0,
        },
        "tail": {
            "type": "integer",
            "description": "Number of lines to show from the end of stdout/stderr preview",
            "default": 5,
        },
        "run_id": {
            "type": "integer",
            "description": (
                "Follow up on an earlier run that returned state \"running\"; "
                "code and intent are then not needed."
            ),
        },
        "action": {
            "type": "string",
            "enum": ["wait", "kill"],
            "description": (
                "With run_id. \"wait\" (default): block until the run finishes or another "
                "time_estimate passes, then report again. \"kill\": stop the run."
            ),
        },
    },
}


def _build_tool_def() -> dict:
    desc = _BASE_DESCRIPTION

    if _PROJECT:
        name = _extract_project_name(_PROJECT) or _PROJECT.name
        desc += (
            f"\n\nThe following project is pre-configured as the execution context: {name} "
            f"(at {_PROJECT}). All its dependencies are available in every script — "
            "no need to declare them."
        )
        if _EXTRA_PACKAGES:
            extras = ", ".join(
                _extract_project_name(p) or p.name for p in _EXTRA_PACKAGES
            )
            desc += f" Additional packages also available: {extras}."
    elif len(_DISCOVERED_PROJECTS) >= 2:
        names = ", ".join(
            _extract_project_name(p) or p.name for p in _DISCOVERED_PROJECTS
        )
        desc += (
            f"\n\nMultiple Python projects detected ({names}), so no single project is "
            "pre-loaded as the execution context. This is fine — declare any packages you "
            "need in the dependencies array and they will be installed automatically."
        )

    return {
        "name": "run_python_script",
        "title": "Run Python Script",
        "description": desc,
        "inputSchema": _TOOL_INPUT_SCHEMA,
        "annotations": {
            "title": "Run Python Script",
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    }


TOOL_DEF = _build_tool_def()


_write_lock = asyncio.Lock()


def _sync_handle(method: str, params: dict | None) -> dict | None:
    """Handle methods that don't need async."""
    if method == "initialize":
        return {
            "protocolVersion": "2025-06-18",
            "capabilities": {"tools": {}, "resources": {}},
            "serverInfo": {"name": "py-scratch", "version": "0.8.1"},
        }
    if method == "notifications/initialized":
        return None
    if method == "tools/list":
        return {"tools": [TOOL_DEF]}
    if method == "ping":
        return {}
    if method == "resources/list":
        return {"resources": _list_resources()}
    if method == "resources/templates/list":
        return {"resourceTemplates": [{
            "uriTemplate": "pyscratch://runs/{id}/status",
            "name": "run-status",
            "description": "State of a run_python_script run (JSON)",
            "mimeType": "application/json",
        }]}
    if method == "resources/read":
        return _read_resource((params or {}).get("uri"))
    return None


class _RpcError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


_STATUS_URI = re.compile(r"^pyscratch://runs/(\d+)/status$")
_LISTED_FINISHED = 20


def _list_resources() -> list[dict]:
    """Active runs plus the most recent finished ones."""
    runs = sorted(_runs.values(), key=lambda r: r.id, reverse=True)
    finished = 0
    out = []
    for run in runs:
        if run.done.is_set():
            finished += 1
            if finished > _LISTED_FINISHED:
                continue
        out.append({
            "uri": f"pyscratch://runs/{run.id}/status",
            "name": f"run {run.id}",
            "description": f"{run.state}: {run.intent}",
            "mimeType": "application/json",
        })
    return out


def _read_resource(uri) -> dict:
    m = _STATUS_URI.match(uri) if isinstance(uri, str) else None
    run = _runs.get(int(m.group(1))) if m else None
    if run is None:
        raise _RpcError(-32002, f"Resource not found: {uri}")
    return {"contents": [{
        "uri": uri,
        "mimeType": "application/json",
        "text": json.dumps(_run_status(run), ensure_ascii=False),
    }]}


async def _write_message(message: dict) -> None:
    async with _write_lock:
        sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
        sys.stdout.flush()


async def _write_response(msg_id: int | str, result: dict) -> None:
    await _write_message({"jsonrpc": "2.0", "id": msg_id, "result": result})


async def _write_rpc_error(msg_id: int | str, code: int, message: str) -> None:
    await _write_message(
        {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}
    )


def _error_result(text: str) -> dict:
    return {"isError": True, "content": [{"type": "text", "text": text}]}


# Argument names models reach for that are not the ones in the schema. Silently
# failing on these used to strand the request forever, so map the obvious ones and
# tell the caller what we did.
_TOOL_PARAMS = {
    "intent", "code", "dependencies", "time_estimate", "head", "tail", "run_id", "action",
}
_ARG_ALIASES = {
    "script": "code",
    "source": "code",
    "python": "code",
    "program": "code",
    "deps": "dependencies",
    "packages": "dependencies",
    "requirements": "dependencies",
    "description": "intent",
    "timeout": "time_estimate",
}

_ARG_HELP = (
    "run_python_script takes: 'intent' (str, what you are trying to do) and "
    "'code' (str, raw Python source); optional 'dependencies' (list[str]), "
    "'time_estimate' (number, seconds you expect it to take), 'head' (int), 'tail' (int). "
    "To follow up on a run that is still running: 'run_id' (int) and "
    "'action' (\"wait\" or \"kill\")."
)


def _coerce_args(args: dict) -> tuple[dict, list[str]]:
    """Map near-miss argument names onto the real schema."""
    clean: dict = {}
    notes: list[str] = []
    for key, value in args.items():
        target = key if key in _TOOL_PARAMS else _ARG_ALIASES.get(key)
        if target is None:
            notes.append(f"ignored unknown argument {key!r}")
            continue
        if target in clean:
            notes.append(f"ignored {key!r}: {target!r} already provided")
            continue
        if key == "timeout" and target == "time_estimate":
            notes.append(
                "'timeout' is now 'time_estimate': your estimate of the run time in "
                "seconds, not a limit (the hard kill is at 10x)"
            )
        elif target != key:
            notes.append(f"interpreted {key!r} as {target!r}")
        clean[target] = value
    if "code" in clean and not clean.get("intent"):
        clean["intent"] = "(no intent provided)"
        notes.append("no 'intent' was given")
    return clean, notes


async def _handle_tool_call(msg_id: int | str, params: dict | None) -> None:
    """Run one tool call. This must always produce exactly one response.

    Anything that escapes here leaves the client waiting forever: Claude Code has
    no per-request timeout of its own beyond a 30-minute idle abort, so a dropped
    response looks to the model like a tool that silently never returned.
    """
    token = _current_msg.set(msg_id)
    try:
        params = params or {}
        name = params.get("name")
        args = params.get("arguments")
        if args is None:
            args = {}
        if name != "run_python_script":
            result = _error_result(f"Unknown tool: {name}")
        elif not isinstance(args, dict):
            result = _error_result(f"'arguments' must be an object, got {type(args).__name__}")
        else:
            clean, notes = _coerce_args(args)
            if clean.get("run_id") is None and (
                not isinstance(clean.get("code"), str) or not clean["code"].strip()
            ):
                result = _error_result(
                    "Missing required argument 'code' (or 'run_id' to follow up on a run). "
                    f"Received arguments: {sorted(args)}. {_ARG_HELP}"
                )
            else:
                text = await run_python_script(**clean, argument_notes=notes)
                result = {"content": [{"type": "text", "text": text}]}
    except _UsageError as exc:
        result = _error_result(str(exc))
    except Exception as exc:
        _log.exception("tool call %s failed", msg_id)
        result = _error_result(
            f"py-scratch failed to run this call: {type(exc).__name__}: {exc}\n"
            f"Server log: {SERVER_LOG}\n{traceback.format_exc()}"
        )
    finally:
        _current_msg.reset(token)

    try:
        await _write_response(msg_id, result)
    except Exception:
        # Last resort: if even the response cannot be serialised, say so rather
        # than dropping the request.
        _log.exception("could not write response for %s", msg_id)
        try:
            await _write_rpc_error(msg_id, -32603, "py-scratch could not serialise the result")
        except Exception:
            _log.exception("could not write error response for %s either", msg_id)


def _log_task_result(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        _log.critical("handler task died without responding", exc_info=exc)


# asyncio's default StreamReader limit is 64 KiB; a larger script would raise out
# of readline() and take the whole read loop down with it.
_READ_LIMIT = 32 * 1024 * 1024


async def serve() -> None:
    """Read JSON-RPC messages from stdin, dispatch tool calls concurrently."""
    loop = asyncio.get_event_loop()
    reader = asyncio.StreamReader(limit=_READ_LIMIT)
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    try:
        # a terminated server must not leave background runs behind: treat it as EOF
        loop.add_signal_handler(signal.SIGTERM, reader.feed_eof)
    except (NotImplementedError, RuntimeError, ValueError):
        pass

    pending: set[asyncio.Task] = set()
    while True:
        try:
            raw = await reader.readline()
        except ValueError as exc:
            # Line longer than the read limit. The buffer is unusable, but staying
            # alive for subsequent requests beats exiting the loop silently.
            _log.error("dropping oversized request line: %s", exc)
            continue
        if not raw:
            break
        line = raw.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as exc:
            _log.error("received malformed JSON (%s): %.200s", exc, line)
            continue
        if not isinstance(msg, dict):
            _log.error("received non-object JSON-RPC message: %.200s", line)
            continue

        method = msg.get("method", "")
        params = msg.get("params")

        if method == "tools/call" and "id" in msg:
            task = asyncio.create_task(_handle_tool_call(msg["id"], params))
            pending.add(task)
            task.add_done_callback(pending.discard)
            task.add_done_callback(_log_task_result)
            continue

        if method == "notifications/cancelled":
            _cancel_request((params or {}).get("requestId"))
            continue

        try:
            result = _sync_handle(method, params)
        except _RpcError as exc:
            if "id" in msg:
                await _write_rpc_error(msg["id"], exc.code, str(exc))
            continue
        except Exception as exc:
            _log.exception("handler for %s failed", method)
            if "id" in msg:
                await _write_rpc_error(msg["id"], -32603, f"{type(exc).__name__}: {exc}")
            continue

        if "id" not in msg:
            continue
        if result is None:
            await _write_rpc_error(msg["id"], -32601, f"Method not found: {method}")
        else:
            await _write_response(msg["id"], result)

    # Runs outlive the calls that started them, so nothing else would stop them now.
    _kill_all_runs()
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    if _supervisors:
        await asyncio.wait(set(_supervisors), timeout=5)
