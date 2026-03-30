from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore[no-redef]

from .package_map import IMPORT_TO_PYPI

_SCAN_SKIP = {".git", ".venv", "__pycache__", "node_modules", "build", "dist", ".tox", ".nox"}


def _find_projects(root: Path, max_depth: int = 3) -> list[Path]:
    """Find directories containing installable pyproject.toml (has [project] table).

    Scans root itself and subdirectories up to max_depth levels deep.
    """
    result: list[Path] = []

    def _scan(directory: Path, depth: int) -> None:
        pp = directory / "pyproject.toml"
        if pp.exists():
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
        except PermissionError:
            return
        for child in children:
            if child.is_dir() and child.name not in _SCAN_SKIP:
                _scan(child, depth + 1)

    _scan(root, 0)
    return result


def _load_local_packages() -> tuple[Path | None, list[Path]]:
    """Discover or load the project context and extra packages.

    Returns (project, extra_packages):
      - project: single project root for --project, or None
      - extra_packages: list of paths for --with
    """
    cwd = Path.cwd()
    config_path = cwd / ".py-scratch.json"

    if config_path.exists():
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
        except Exception:
            print("py-scratch: failed to parse .py-scratch.json, ignoring", file=sys.stderr)
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


# --- Discovery results (computed once at import time) ---

_DISCOVERED_PROJECTS = _find_projects(Path.cwd())
_PROJECT, _EXTRA_PACKAGES = _load_local_packages()


def _session_dir() -> Path:
    cwd = os.getcwd()
    path_hash = hashlib.sha256(cwd.encode()).hexdigest()[:6]
    timestamp = time.strftime("%Y-%m-%d-%H-%M-%S")
    pid = os.getpid()
    return Path(f"/tmp/pyscratch/workspace-{path_hash}/{timestamp}-{pid}")


SESSION_DIR = _session_dir()
STDOUT_FILE = "stdout.log"
STDERR_FILE = "stderr.log"
_exec_counter = 0
_active_procs: set[asyncio.subprocess.Process] = set()


def _next_execution_id() -> str:
    global _exec_counter
    _exec_counter += 1
    return f"{_exec_counter:04d}"


def _exec_dir() -> tuple[str, Path]:
    eid = _next_execution_id()
    d = SESSION_DIR / eid
    d.mkdir(parents=True, exist_ok=True)
    return eid, d


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


def _build_command(script: Path) -> list[str]:
    cmd = ["uv", "run", "--quiet"]
    if _PROJECT:
        cmd.extend(["--project", str(_PROJECT)])
    for pkg in _EXTRA_PACKAGES:
        cmd.extend(["--with", str(pkg)])
    cmd.append(str(script))
    return cmd


def _preview(path: Path, head: int, tail: int) -> str:
    if not path.exists():
        return ""
    lines = path.read_text(encoding="utf-8").splitlines()
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
    m = _IMPORT_ERROR_RE.search(stderr_path.read_text(encoding="utf-8"))
    if not m:
        return None
    return IMPORT_TO_PYPI.get(m.group(1), m.group(1))


async def _run_script(
    intent: str, code: str, deps: list[str], timeout: int,
) -> dict:
    execution_id, exec_dir = _exec_dir()
    script = exec_dir / "script.py"
    stdout_path = exec_dir / STDOUT_FILE
    stderr_path = exec_dir / STDERR_FILE
    _write_script(script, code, deps)

    cmd = _build_command(script)
    t0 = time.monotonic()
    with open(stdout_path, "wb") as out_f, open(stderr_path, "wb") as err_f:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=out_f,
            stderr=err_f,
            cwd=os.getcwd(),
        )
        _active_procs.add(proc)
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout)
            exit_code = proc.returncode
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            exit_code = -1
            err_f.write(f"\n[killed: exceeded {timeout}s timeout]".encode())
        finally:
            _active_procs.discard(proc)
    duration_ms = int((time.monotonic() - t0) * 1000)

    meta: dict = {
        "execution_id": execution_id,
        "intent": intent,
        "exit_code": exit_code,
        "duration_ms": duration_ms,
        "dependencies": deps,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if _PROJECT:
        meta["project"] = str(_PROJECT)
    if _EXTRA_PACKAGES:
        meta["extra_packages"] = [str(p) for p in _EXTRA_PACKAGES]
    (exec_dir / "meta.json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    return {
        "execution_id": execution_id,
        "exit_code": exit_code,
        "duration_ms": duration_ms,
        "exec_dir": exec_dir,
    }


async def run_python_script(
    intent: str,
    code: str,
    dependencies: list[str] | None = None,
    timeout: int = 30,
    head: int = 0,
    tail: int = 5,
) -> dict:
    deps = list(dependencies or [])
    result = await _run_script(intent, code, deps, timeout)
    exec_dir = result.pop("exec_dir")

    # auto-retry on ImportError if no deps were declared
    if result["exit_code"] != 0 and not deps:
        inferred = _infer_dep(exec_dir / STDERR_FILE)
        if inferred:
            deps = [inferred]
            result = await _run_script(intent, code, deps, timeout)
            exec_dir = result.pop("exec_dir")
            result["auto_installed"] = inferred

    return {
        **result,
        "stdout_preview": _preview(exec_dir / STDOUT_FILE, head, tail),
        "stderr_preview": _preview(exec_dir / STDERR_FILE, head, tail),
        "stdout_path": str(exec_dir / STDOUT_FILE),
        "stderr_path": str(exec_dir / STDERR_FILE),
    }


# --- MCP protocol (JSON-RPC over stdio) ---

_BASE_DESCRIPTION = (
    "Execute a Python script with optional dependencies. "
    "Use this for any Python longer than a couple lines or requiring third-party packages. "
    "Code is passed as a raw string — no shell escaping needed. "
    "Dependencies are installed automatically via uv."
)

_TOOL_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {
            "type": "string",
            "description": "What are you trying to do with this script?",
        },
        "code": {"type": "string", "description": "Raw Python source code"},
        "dependencies": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Optional PyPI package names",
            "default": [],
        },
        "timeout": {
            "type": "integer",
            "description": "Seconds before the script is killed",
            "default": 30,
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
    },
    "required": ["intent", "code"],
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
            "\n\n⚠️ WARNING: THIS PLUGIN IS NOT CONFIGURED AND WILL NOT WORK CORRECTLY. "
            f"Multiple Python projects were detected ({names}) but none is selected as the "
            "execution context. The user MUST create a .py-scratch.json file in the working "
            "directory to select a project. Example:\n"
            '{"project": "./path-to-project", "packages": ["./optional-extra"]}\n'
            "TELL THE USER ABOUT THIS. Scripts will run without project dependencies until configured."
        )

    return {
        "name": "run_python_script",
        "description": desc,
        "inputSchema": _TOOL_INPUT_SCHEMA,
    }


TOOL_DEF = _build_tool_def()


_write_lock = asyncio.Lock()


def _sync_handle(method: str, params: dict | None) -> dict | None:
    """Handle methods that don't need async."""
    if method == "initialize":
        return {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "py-scratch", "version": "0.1.0"},
        }
    if method == "notifications/initialized":
        return None
    if method == "tools/list":
        return {"tools": [TOOL_DEF]}
    if method == "ping":
        return {}
    return None


async def _write_response(msg_id: int | str, result: dict) -> None:
    response = {"jsonrpc": "2.0", "id": msg_id, "result": result}
    async with _write_lock:
        sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
        sys.stdout.flush()


async def _handle_tool_call(msg_id: int | str, params: dict) -> None:
    name = params.get("name")
    args = params.get("arguments", {})
    if name != "run_python_script":
        result = {"isError": True, "content": [{"type": "text", "text": f"Unknown tool: {name}"}]}
    else:
        data = await run_python_script(**args)
        result = {"content": [{"type": "text", "text": json.dumps(data, indent=2, ensure_ascii=False)}]}
    await _write_response(msg_id, result)


async def serve() -> None:
    """Read JSON-RPC messages from stdin, dispatch tool calls concurrently."""
    loop = asyncio.get_event_loop()
    reader = asyncio.StreamReader()
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)

    pending: set[asyncio.Task] = set()
    while True:
        line = await reader.readline()
        if not line:
            break
        line = line.decode().strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue

        method = msg.get("method", "")
        params = msg.get("params")

        if method == "tools/call" and "id" in msg:
            task = asyncio.create_task(_handle_tool_call(msg["id"], params))
            pending.add(task)
            task.add_done_callback(pending.discard)
            continue

        result = _sync_handle(method, params)
        if "id" in msg and result is not None:
            await _write_response(msg["id"], result)

    for proc in _active_procs:
        proc.kill()
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
