# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Smoke tests for py-scratch."""

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from py_scratch.server import (
    _write_script, _infer_dep, _preview, _sync_handle,
    _find_projects, _extract_project_name, _build_command, _build_tool_def,
    run_python_script, STDOUT_FILE, STDERR_FILE,
)
import py_scratch.server as server_mod


# --- script writing ---

def test_write_script_no_deps(tmp_path):
    p = tmp_path / "test.py"
    _write_script(p, "print(1)", [])
    assert p.read_text() == "print(1)"


def test_write_script_with_deps(tmp_path):
    p = tmp_path / "test.py"
    _write_script(p, "import httpx", ["httpx"])
    text = p.read_text()
    assert "# /// script" in text
    assert '"httpx"' in text
    assert "import httpx" in text


# --- dep inference ---

def test_infer_dep_known(tmp_path):
    p = tmp_path / "err.log"
    p.write_text("ModuleNotFoundError: No module named 'cv2'")
    assert _infer_dep(p) == "opencv-python"
    p.write_text("ModuleNotFoundError: No module named 'PIL'")
    assert _infer_dep(p) == "Pillow"
    p.write_text("ModuleNotFoundError: No module named 'yaml'")
    assert _infer_dep(p) == "pyyaml"


def test_infer_dep_unknown(tmp_path):
    p = tmp_path / "err.log"
    p.write_text("ModuleNotFoundError: No module named 'foobar'")
    assert _infer_dep(p) == "foobar"


def test_infer_dep_no_match(tmp_path):
    p = tmp_path / "err.log"
    p.write_text("SyntaxError: invalid syntax")
    assert _infer_dep(p) is None


def test_infer_dep_missing_file(tmp_path):
    assert _infer_dep(tmp_path / "nope.log") is None


# --- execution (via public API) ---


# --- run_python_script (public API) ---

async def test_run_python_script_returns_paths():
    r = await run_python_script(intent="test", code="print('hi')")
    assert r["exit_code"] == 0
    assert r["stdout_path"].endswith("stdout.log")
    assert r["stderr_path"].endswith("stderr.log")
    assert Path(r["stdout_path"]).exists()
    assert "hi" in r["stdout_preview"]


async def test_run_python_script_default_tail_5():
    code = "\n".join(f"print('line {i}')" for i in range(20))
    r = await run_python_script(intent="test", code=code)
    preview_lines = r["stdout_preview"].strip().split("\n")
    assert len(preview_lines) == 5
    assert "line 15" in preview_lines[0]
    assert "line 19" in preview_lines[-1]


async def test_run_python_script_head_only():
    code = "\n".join(f"print('line {i}')" for i in range(20))
    r = await run_python_script(intent="test", code=code, head=3, tail=0)
    preview_lines = r["stdout_preview"].strip().split("\n")
    assert len(preview_lines) == 3
    assert "line 0" in preview_lines[0]
    assert "line 2" in preview_lines[-1]


async def test_run_python_script_head_and_tail():
    code = "\n".join(f"print('line {i}')" for i in range(20))
    r = await run_python_script(intent="test", code=code, head=2, tail=2)
    preview_lines = r["stdout_preview"].strip().split("\n")
    assert len(preview_lines) == 4
    assert "line 0" in preview_lines[0]
    assert "line 1" in preview_lines[1]
    assert "line 18" in preview_lines[2]
    assert "line 19" in preview_lines[3]


# --- _preview ---

def test_preview_short_output_no_dupes(tmp_path):
    p = tmp_path / "out.log"
    p.write_text("a\nb\nc")
    assert _preview(p, head=5, tail=5) == "a\nb\nc"


def test_preview_zero_zero(tmp_path):
    p = tmp_path / "out.log"
    p.write_text("a\nb\nc")
    assert _preview(p, head=0, tail=0) == ""


def test_preview_missing_file(tmp_path):
    assert _preview(tmp_path / "nope.log", head=0, tail=5) == ""


# --- MCP protocol ---

def test_handle_initialize():
    r = _sync_handle("initialize", {})
    assert r["protocolVersion"] == "2025-06-18"
    assert "tools" in r["capabilities"]
    assert r["serverInfo"]["name"] == "py-scratch"


def test_handle_initialized_is_notification():
    assert _sync_handle("notifications/initialized", {}) is None


def test_handle_tools_list():
    r = _sync_handle("tools/list", {})
    assert len(r["tools"]) == 1
    assert r["tools"][0]["name"] == "run_python_script"
    schema = r["tools"][0]["inputSchema"]
    assert "code" in schema["properties"]
    assert "intent" in schema["required"]


def test_handle_ping():
    r = _sync_handle("ping", {})
    assert r == {}


def test_handle_unknown_method():
    assert _sync_handle("foo/bar", {}) is None


# --- local package discovery ---

def _make_project(d: Path, name: str = "testpkg") -> Path:
    """Create a minimal installable pyproject.toml in d."""
    d.mkdir(parents=True, exist_ok=True)
    (d / "pyproject.toml").write_text(
        f'[project]\nname = "{name}"\nversion = "0.1.0"\n'
    )
    return d


def test_find_projects_single(tmp_path):
    _make_project(tmp_path / "mylib")
    found = _find_projects(tmp_path)
    assert len(found) == 1
    assert found[0] == tmp_path / "mylib"


def test_find_projects_root_itself(tmp_path):
    _make_project(tmp_path, "rootpkg")
    found = _find_projects(tmp_path)
    assert len(found) == 1
    assert found[0] == tmp_path


def test_find_projects_multiple(tmp_path):
    _make_project(tmp_path / "a", "pkg-a")
    _make_project(tmp_path / "b", "pkg-b")
    found = _find_projects(tmp_path)
    assert len(found) == 2


def test_find_projects_ignores_non_project_toml(tmp_path):
    d = tmp_path / "toolconfig"
    d.mkdir()
    (d / "pyproject.toml").write_text('[tool.ruff]\nline-length = 88\n')
    found = _find_projects(tmp_path)
    assert len(found) == 0


def test_find_projects_depth_limit(tmp_path):
    deep = tmp_path / "a" / "b" / "c" / "d"
    _make_project(deep, "deep")
    # depth=3 means root(0) -> a(1) -> b(2) -> c(3), d would be 4
    found = _find_projects(tmp_path, max_depth=3)
    assert len(found) == 0
    found = _find_projects(tmp_path, max_depth=4)
    assert len(found) == 1


def test_find_projects_skips_venv(tmp_path):
    _make_project(tmp_path / ".venv" / "pkg")
    found = _find_projects(tmp_path)
    assert len(found) == 0


def test_extract_project_name(tmp_path):
    _make_project(tmp_path, "buslib")
    assert _extract_project_name(tmp_path) == "buslib"


def test_extract_project_name_missing(tmp_path):
    assert _extract_project_name(tmp_path) is None


def test_build_command_with_project(tmp_path):
    orig_project = server_mod._PROJECT
    orig_extras = server_mod._EXTRA_PACKAGES
    orig_pyver = server_mod._REQUIRES_PYTHON
    try:
        server_mod._PROJECT = tmp_path / "mylib"
        server_mod._EXTRA_PACKAGES = []
        server_mod._REQUIRES_PYTHON = None
        cmd = _build_command(str(tmp_path / "script.py"))
        assert cmd == [
            "uv", "run", "--quiet",
            "--project", str(tmp_path / "mylib"),
            str(tmp_path / "script.py"),
        ]
    finally:
        server_mod._PROJECT = orig_project
        server_mod._EXTRA_PACKAGES = orig_extras
        server_mod._REQUIRES_PYTHON = orig_pyver


def test_build_command_with_project_and_python(tmp_path):
    orig_project = server_mod._PROJECT
    orig_extras = server_mod._EXTRA_PACKAGES
    orig_pyver = server_mod._REQUIRES_PYTHON
    try:
        server_mod._PROJECT = tmp_path / "mylib"
        server_mod._EXTRA_PACKAGES = []
        server_mod._REQUIRES_PYTHON = "3.14"
        cmd = _build_command(str(tmp_path / "script.py"))
        assert cmd == [
            "uv", "run", "--quiet",
            "--project", str(tmp_path / "mylib"),
            "--python", ">=3.14",
            str(tmp_path / "script.py"),
        ]
    finally:
        server_mod._PROJECT = orig_project
        server_mod._EXTRA_PACKAGES = orig_extras
        server_mod._REQUIRES_PYTHON = orig_pyver


def test_build_command_with_project_and_extras(tmp_path):
    orig_project = server_mod._PROJECT
    orig_extras = server_mod._EXTRA_PACKAGES
    orig_pyver = server_mod._REQUIRES_PYTHON
    try:
        server_mod._PROJECT = tmp_path / "main"
        server_mod._EXTRA_PACKAGES = [tmp_path / "extra1", tmp_path / "extra2"]
        server_mod._REQUIRES_PYTHON = None
        cmd = _build_command(str(tmp_path / "script.py"))
        assert cmd == [
            "uv", "run", "--quiet",
            "--project", str(tmp_path / "main"),
            "--with", str(tmp_path / "extra1"),
            "--with", str(tmp_path / "extra2"),
            str(tmp_path / "script.py"),
        ]
    finally:
        server_mod._PROJECT = orig_project
        server_mod._EXTRA_PACKAGES = orig_extras
        server_mod._REQUIRES_PYTHON = orig_pyver


def test_build_command_bare(tmp_path):
    orig_project = server_mod._PROJECT
    orig_extras = server_mod._EXTRA_PACKAGES
    orig_pyver = server_mod._REQUIRES_PYTHON
    try:
        server_mod._PROJECT = None
        server_mod._EXTRA_PACKAGES = []
        server_mod._REQUIRES_PYTHON = None
        cmd = _build_command(str(tmp_path / "script.py"))
        assert cmd == ["uv", "run", "--quiet", str(tmp_path / "script.py")]
    finally:
        server_mod._PROJECT = orig_project
        server_mod._EXTRA_PACKAGES = orig_extras
        server_mod._REQUIRES_PYTHON = orig_pyver


def test_tool_def_with_project(tmp_path):
    orig_project = server_mod._PROJECT
    orig_extras = server_mod._EXTRA_PACKAGES
    orig_discovered = server_mod._DISCOVERED_PROJECTS
    _make_project(tmp_path, "buslib")
    try:
        server_mod._PROJECT = tmp_path
        server_mod._EXTRA_PACKAGES = []
        server_mod._DISCOVERED_PROJECTS = [tmp_path]
        td = _build_tool_def()
        assert "buslib" in td["description"]
        assert "pre-configured" in td["description"]
    finally:
        server_mod._PROJECT = orig_project
        server_mod._EXTRA_PACKAGES = orig_extras
        server_mod._DISCOVERED_PROJECTS = orig_discovered


def test_tool_def_multi_project_warning(tmp_path):
    orig_project = server_mod._PROJECT
    orig_extras = server_mod._EXTRA_PACKAGES
    orig_discovered = server_mod._DISCOVERED_PROJECTS
    _make_project(tmp_path / "a", "pkg-a")
    _make_project(tmp_path / "b", "pkg-b")
    try:
        server_mod._PROJECT = None
        server_mod._EXTRA_PACKAGES = []
        server_mod._DISCOVERED_PROJECTS = [tmp_path / "a", tmp_path / "b"]
        td = _build_tool_def()
        assert "Multiple" in td["description"]
        assert "pkg-a" in td["description"]
        assert "pkg-b" in td["description"]
        assert "dependencies" in td["description"]
    finally:
        server_mod._PROJECT = orig_project
        server_mod._EXTRA_PACKAGES = orig_extras
        server_mod._DISCOVERED_PROJECTS = orig_discovered


def test_py_scratch_json_override(tmp_path):
    _make_project(tmp_path / "main", "mainpkg")
    _make_project(tmp_path / "extra", "extrapkg")
    config = {"project": "./main", "packages": ["./extra"]}
    (tmp_path / ".py-scratch.json").write_text(json.dumps(config))
    import os
    orig_cwd = os.getcwd()
    try:
        os.chdir(tmp_path)
        from py_scratch.server import _load_local_packages
        project, extras = _load_local_packages()
        assert project == (tmp_path / "main").resolve()
        assert extras == [(tmp_path / "extra").resolve()]
    finally:
        os.chdir(orig_cwd)


# --- runner ---
# --- protocol robustness: a request must always get exactly one response ---

def _collect_responses():
    """Patch the writer so handler output can be inspected without stdout."""
    sent = []

    async def _capture(message):
        sent.append(message)

    server_mod._write_message = _capture
    return sent


async def test_tool_call_with_wrong_arg_name_still_responds():
    # Regression: fable sent {"script": ...} instead of {"code": ...}. That raised
    # TypeError inside the handler task, no response was ever written, and the
    # client hung until its 30-minute idle abort.
    sent = _collect_responses()
    await server_mod._handle_tool_call(1, {
        "name": "run_python_script",
        "arguments": {"script": "print('aliased')"},
    })
    assert len(sent) == 1
    assert sent[0]["id"] == 1
    body = json.loads(sent[0]["result"]["content"][0]["text"])
    assert body["exit_code"] == 0
    assert "aliased" in body["stdout_preview"]
    assert any("script" in n and "code" in n for n in body["argument_notes"])


async def test_tool_call_missing_code_responds_with_error():
    sent = _collect_responses()
    await server_mod._handle_tool_call(2, {"name": "run_python_script", "arguments": {"intent": "x"}})
    assert len(sent) == 1
    assert sent[0]["result"]["isError"] is True
    assert "code" in sent[0]["result"]["content"][0]["text"]


async def test_tool_call_unknown_tool_responds():
    sent = _collect_responses()
    await server_mod._handle_tool_call(3, {"name": "nope", "arguments": {}})
    assert len(sent) == 1
    assert sent[0]["result"]["isError"] is True


async def test_tool_call_responds_even_when_execution_raises():
    sent = _collect_responses()
    original = server_mod.run_python_script

    async def _boom(**kwargs):
        raise RuntimeError("simulated internal failure")

    server_mod.run_python_script = _boom
    try:
        await server_mod._handle_tool_call(4, {
            "name": "run_python_script",
            "arguments": {"intent": "x", "code": "print(1)"},
        })
    finally:
        server_mod.run_python_script = original
    assert len(sent) == 1
    assert sent[0]["result"]["isError"] is True
    assert "simulated internal failure" in sent[0]["result"]["content"][0]["text"]


async def test_tool_call_bad_arguments_type_responds():
    sent = _collect_responses()
    await server_mod._handle_tool_call(5, {"name": "run_python_script", "arguments": "oops"})
    assert len(sent) == 1
    assert sent[0]["result"]["isError"] is True


def test_coerce_args_aliases_and_unknowns():
    clean, notes = server_mod._coerce_args({"script": "print(1)", "bogus": 1})
    assert clean["code"] == "print(1)"
    assert clean["intent"]
    assert any("bogus" in n for n in notes)


def test_coerce_args_keeps_explicit_over_alias():
    clean, notes = server_mod._coerce_args({"code": "a", "script": "b", "intent": "i"})
    assert clean["code"] == "a"
    assert any("script" in n for n in notes)


# --- child process robustness ---

def test_describe_exit_classifications():
    assert server_mod._describe_exit(0, False, 30)[0] == "ok"
    assert server_mod._describe_exit(1, False, 30)[0] == "failed"
    assert server_mod._describe_exit(-9, False, 30)[0] == "crashed"
    assert "SIGKILL" in server_mod._describe_exit(-9, False, 30)[1]
    assert server_mod._describe_exit(-9, True, 30)[0] == "timeout"
    assert server_mod._describe_exit(None, False, 30)[0] == "not_started"


async def test_timeout_is_reported_as_timeout_not_crash():
    r = await run_python_script(intent="hang", code="import time; time.sleep(30)", timeout=2)
    assert r["status"] == "timeout"
    assert "timeout" in r["explanation"]


async def test_signal_death_is_reported_as_crash():
    r = await run_python_script(
        intent="crash", code="import os, signal; os.kill(os.getpid(), signal.SIGKILL)"
    )
    assert r["status"] == "crashed"
    assert "SIGKILL" in r["explanation"]


async def test_crash_with_output_is_not_retried():
    r = await run_python_script(
        intent="crash after output",
        code="import os, signal, sys; print('side effect'); sys.stdout.flush();"
             " os.kill(os.getpid(), signal.SIGKILL)",
    )
    assert r["status"] == "crashed"
    assert "crash_retry_of" not in r


def test_normalise_args_coerces_loose_types():
    intent, code, deps, timeout, head, tail = server_mod._normalise_args(
        None, "print(1)", "httpx", "45", None, "3"
    )
    assert intent == ""
    assert deps == ["httpx"]
    assert timeout == 45
    assert head == 0
    assert tail == 3


def test_normalise_args_clamps_nonsense():
    _, _, _, timeout, _, _ = server_mod._normalise_args("i", "c", [], -5, 0, 5)
    assert timeout == 1


def test_read_log_survives_bad_encoding(tmp_path):
    p = tmp_path / "out.log"
    p.write_bytes(b"ok \xff\xfe broken")
    assert "ok" in server_mod._read_log(p)


def test_find_projects_survives_unreadable_dir(tmp_path):
    # An unreadable directory in the scan path used to raise PermissionError out of
    # import time, so the server never started at all.
    _make_project(tmp_path / "lib")
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    (blocked / "sub").mkdir()
    blocked.chmod(0o000)
    try:
        found = _find_projects(tmp_path)
    finally:
        blocked.chmod(0o755)
    assert (tmp_path / "lib") in found


# --- artifact location ---

_ORIG_READ_HANDOFF = server_mod._read_handoff
_ORIG_FIND_CLAUDE = server_mod._find_claude_process


def _restore_server_probes():
    server_mod._read_handoff = _ORIG_READ_HANDOFF
    server_mod._find_claude_process = _ORIG_FIND_CLAUDE


def _no_handoff():
    """Force the env-based fallback path (no hook installed)."""
    server_mod._read_handoff = lambda: None


def test_scratchpad_used_when_present(tmp_path):
    _no_handoff()
    cwd = tmp_path / "proj"
    cwd.mkdir()
    tmp = tmp_path / "tmproot"
    session = "sess-123"
    pad = tmp / f"claude-{os.getuid()}" / server_mod._slugify_cwd(str(cwd)) / session / "scratchpad"
    pad.mkdir(parents=True)

    old_cwd = os.getcwd()
    os.chdir(cwd)
    os.environ["CLAUDE_CODE_TMPDIR"] = str(tmp)
    os.environ["CLAUDE_CODE_SESSION_ID"] = session
    try:
        assert server_mod._claude_scratchpad() == pad
        assert str(server_mod._artifact_root()).startswith(str(pad))
    finally:
        _restore_server_probes()
        os.chdir(old_cwd)
        os.environ.pop("CLAUDE_CODE_TMPDIR", None)
        os.environ.pop("CLAUDE_CODE_SESSION_ID", None)


def test_sits_beside_scratchpad_when_claude_has_not_created_it(tmp_path):
    # Claude Code creates <session>/scratchpad lazily with a non-recursive mkdir, so
    # creating it ourselves could make its own mkdir fail with EEXIST.
    _no_handoff()
    cwd = tmp_path / "proj"
    cwd.mkdir()
    tmp = tmp_path / "tmproot"
    session = "sess-nopad"
    session_dir = tmp / f"claude-{os.getuid()}" / server_mod._slugify_cwd(str(cwd)) / session
    session_dir.mkdir(parents=True)

    old_cwd = os.getcwd()
    os.chdir(cwd)
    os.environ["CLAUDE_CODE_TMPDIR"] = str(tmp)
    os.environ["CLAUDE_CODE_SESSION_ID"] = session
    try:
        assert server_mod._claude_scratchpad() == session_dir
        assert not (session_dir / "scratchpad").exists()
    finally:
        _restore_server_probes()
        os.chdir(old_cwd)
        os.environ.pop("CLAUDE_CODE_TMPDIR", None)
        os.environ.pop("CLAUDE_CODE_SESSION_ID", None)


def test_falls_back_to_newest_when_session_id_is_stale(tmp_path):
    _no_handoff()
    cwd = tmp_path / "proj"
    cwd.mkdir()
    tmp = tmp_path / "tmproot"
    root = tmp / f"claude-{os.getuid()}" / server_mod._slugify_cwd(str(cwd))
    old_pad = root / "dead-session" / "scratchpad"
    new_pad = root / "live-session" / "scratchpad"
    old_pad.mkdir(parents=True)
    new_pad.mkdir(parents=True)
    os.utime(old_pad.parent, (1, 1))

    old_cwd = os.getcwd()
    os.chdir(cwd)
    os.environ["CLAUDE_CODE_TMPDIR"] = str(tmp)
    os.environ["CLAUDE_CODE_SESSION_ID"] = "gone-after-clear"
    try:
        assert server_mod._claude_scratchpad() == new_pad
    finally:
        _restore_server_probes()
        os.chdir(old_cwd)
        os.environ.pop("CLAUDE_CODE_TMPDIR", None)
        os.environ.pop("CLAUDE_CODE_SESSION_ID", None)


# --- hook handoff: exact session identity instead of a guess ---

def _load_hook():
    import importlib.util
    hook_path = Path(__file__).parent / "hooks" / "scratchpad_handoff.py"
    spec = importlib.util.spec_from_file_location("scratchpad_handoff", hook_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_hook_finds_this_process_tree():
    hook = _load_hook()
    # The test itself runs under some ancestor; whatever the walk returns must be a
    # live process whose starttime matches, or None when not run under Claude Code.
    found = hook.find_claude_process()
    if found is None:
        return
    pid, starttime = found
    assert hook._comm(pid) == "claude"
    assert hook._starttime(pid) == starttime


def test_hook_walk_stops_cleanly_on_bogus_pid():
    hook = _load_hook()
    assert hook.find_claude_process(start=999999999) is None


def test_hook_writes_handoff_and_server_reads_it(tmp_path):
    hook = _load_hook()
    handoff_dir = tmp_path / "handoff"
    cwd = tmp_path / "proj"
    cwd.mkdir()
    tmp = tmp_path / "tmproot"
    session = "hook-session"
    pad = tmp / f"claude-{os.getuid()}" / str(cwd).replace(os.sep, "-") / session / "scratchpad"
    pad.mkdir(parents=True)

    os.environ["PY_SCRATCH_HANDOFF_DIR"] = str(handoff_dir)
    os.environ["CLAUDE_CODE_TMPDIR"] = str(tmp)
    try:
        # Pretend this very process is the Claude Code process, so the test does not
        # depend on actually running under one.
        fake = (os.getpid(), hook._starttime(os.getpid()))
        hook.find_claude_process = lambda start=None: fake
        hook.write_handoff(
            {"session_id": session, "cwd": str(cwd), **hook.session_paths(session, str(cwd))},
            *fake,
        )

        server_mod._find_claude_process = lambda: fake
        data = server_mod._read_handoff()
        assert data["session_id"] == session
        assert server_mod._claude_scratchpad() == pad
    finally:
        _restore_server_probes()
        os.environ.pop("PY_SCRATCH_HANDOFF_DIR", None)
        os.environ.pop("CLAUDE_CODE_TMPDIR", None)


def test_handoff_beats_a_stale_env_session_id(tmp_path):
    # The whole point: env says one session, the hook knows the real one.
    hook = _load_hook()
    handoff_dir = tmp_path / "handoff"
    cwd = tmp_path / "proj"
    cwd.mkdir()
    tmp = tmp_path / "tmproot"
    root = tmp / f"claude-{os.getuid()}" / str(cwd).replace(os.sep, "-")
    (root / "stale-session" / "scratchpad").mkdir(parents=True)
    real_pad = root / "real-session" / "scratchpad"
    real_pad.mkdir(parents=True)
    # Make the stale one look newest, so any mtime heuristic would pick it.
    os.utime(root / "stale-session", None)

    os.environ["PY_SCRATCH_HANDOFF_DIR"] = str(handoff_dir)
    os.environ["CLAUDE_CODE_TMPDIR"] = str(tmp)
    os.environ["CLAUDE_CODE_SESSION_ID"] = "stale-session"
    try:
        fake = (os.getpid(), hook._starttime(os.getpid()))
        hook.write_handoff(
            {"session_id": "real-session", "cwd": str(cwd),
             **hook.session_paths("real-session", str(cwd))},
            *fake,
        )
        server_mod._find_claude_process = lambda: fake
        assert server_mod._claude_scratchpad() == real_pad
    finally:
        _restore_server_probes()
        os.environ.pop("PY_SCRATCH_HANDOFF_DIR", None)
        os.environ.pop("CLAUDE_CODE_TMPDIR", None)
        os.environ.pop("CLAUDE_CODE_SESSION_ID", None)


def test_hook_prunes_handoffs_for_dead_processes(tmp_path):
    hook = _load_hook()
    handoff_dir = tmp_path / "handoff"
    handoff_dir.mkdir()
    (handoff_dir / "999999999-12345.json").write_text("{}")
    os.environ["PY_SCRATCH_HANDOFF_DIR"] = str(handoff_dir)
    try:
        hook.prune_stale()
        assert not (handoff_dir / "999999999-12345.json").exists()
    finally:
        os.environ.pop("PY_SCRATCH_HANDOFF_DIR", None)


def test_hook_script_runs_end_to_end(tmp_path):
    import subprocess
    hook_path = Path(__file__).parent / "hooks" / "scratchpad-handoff.sh"
    env = dict(os.environ)
    env["PY_SCRATCH_HANDOFF_DIR"] = str(tmp_path / "handoff")
    env["CLAUDE_CODE_TMPDIR"] = str(tmp_path / "tmproot")
    event = json.dumps({
        "hook_event_name": "SessionStart", "source": "clear",
        "session_id": "e2e-session", "cwd": str(tmp_path),
    })
    r = subprocess.run([str(hook_path)], input=event, capture_output=True, text=True, env=env)
    assert r.returncode == 0
    # Must stay silent: UserPromptSubmit-style stdout would be injected as context.
    assert r.stdout == ""
    written = list((tmp_path / "handoff").glob("*.json")) if (tmp_path / "handoff").exists() else []
    if written:  # only when running under a real claude process
        data = json.loads(written[0].read_text())
        assert data["session_id"] == "e2e-session"


def test_hook_survives_garbage_stdin():
    import subprocess
    hook_path = Path(__file__).parent / "hooks" / "scratchpad-handoff.sh"
    r = subprocess.run([str(hook_path)], input="not json at all",
                       capture_output=True, text=True)
    assert r.returncode == 0
    assert r.stdout == ""


def test_scratchpad_disabled_by_env(tmp_path):
    os.environ["PY_SCRATCH_USE_SCRATCHPAD"] = "0"
    try:
        assert server_mod._claude_scratchpad() is None
    finally:
        os.environ.pop("PY_SCRATCH_USE_SCRATCHPAD", None)


def test_py_scratch_dir_env_overrides(tmp_path):
    os.environ["PY_SCRATCH_DIR"] = str(tmp_path / "custom")
    try:
        assert str(server_mod._artifact_root()).startswith(str(tmp_path / "custom"))
    finally:
        os.environ.pop("PY_SCRATCH_DIR", None)


async def test_timeout_kills_the_grandchild_interpreter():
    # The direct child is `uv run`; the interpreter running the script is its child.
    # Killing only uv would leave that interpreter alive forever.
    r = await run_python_script(
        intent="orphan check",
        code="import os, sys, time; print(os.getpid()); sys.stdout.flush(); time.sleep(120)",
        timeout=3,
    )
    assert r["status"] == "timeout"
    child_pid = int(r["stdout_preview"].strip().split("\n")[0])
    await asyncio.sleep(0.5)
    try:
        os.kill(child_pid, 0)
    except ProcessLookupError:
        return
    raise AssertionError(f"interpreter {child_pid} survived the timeout kill")


async def test_crash_with_no_output_is_retried_once():
    r = await run_python_script(
        intent="silent crash",
        code="import os, signal; os.kill(os.getpid(), signal.SIGKILL)",
    )
    assert r["status"] == "crashed"
    assert "crash_retry_of" in r
    assert "reproduced on retry" in r["explanation"]


def test_clear_is_followed_exactly(tmp_path):
    """The incident case: /clear starts a new session inside the same process, so
    the MCP server's CLAUDE_CODE_SESSION_ID goes stale while the server keeps
    running. The SessionStart hook fires again with the new id, keyed by the same
    Claude Code process, so the server follows it."""
    import subprocess

    handoff = tmp_path / "handoff"
    tmproot = tmp_path / "tmproot"
    cwd = tmp_path / "proj"
    cwd.mkdir()
    root = tmproot / f"claude-{os.getuid()}" / str(cwd).replace(os.sep, "-")

    os.environ["PY_SCRATCH_HANDOFF_DIR"] = str(handoff)
    os.environ["CLAUDE_CODE_TMPDIR"] = str(tmproot)
    os.environ["CLAUDE_CODE_SESSION_ID"] = "session-a"   # stale after the /clear
    hook_sh = Path(__file__).parent / "hooks" / "scratchpad-handoff.sh"

    def fire(session_id, source):
        event = json.dumps({
            "hook_event_name": "SessionStart", "source": source,
            "session_id": session_id, "cwd": str(cwd),
        })
        r = subprocess.run([str(hook_sh)], input=event, capture_output=True,
                           text=True, env=dict(os.environ))
        assert r.returncode == 0 and r.stdout == ""

    old_cwd = os.getcwd()
    os.chdir(cwd)
    try:
        pad_a = root / "session-a" / "scratchpad"
        pad_b = root / "session-b" / "scratchpad"
        pad_a.mkdir(parents=True)
        fire("session-a", "startup")
        if not list(handoff.glob("*.json")):
            return  # not running under a real claude process; nothing to assert
        assert server_mod._claude_scratchpad() == pad_a

        pad_b.mkdir(parents=True)
        os.utime(root / "session-a", None)   # make the dead session look newest
        fire("session-b", "clear")
        assert server_mod._claude_scratchpad() == pad_b

        # without the hook, the mtime heuristic would pick the dead session
        server_mod._read_handoff = lambda: None
        assert server_mod._claude_scratchpad() == pad_a
    finally:
        _restore_server_probes()
        os.chdir(old_cwd)
        for var in ("PY_SCRATCH_HANDOFF_DIR", "CLAUDE_CODE_TMPDIR", "CLAUDE_CODE_SESSION_ID"):
            os.environ.pop(var, None)


# --- multi-user safety: per-user roots, verified dirs, trusted handoffs ---

def test_scratch_root_is_per_user_and_survives_logout():
    if os.name != "posix":
        return
    root = server_mod._scratch_root()
    # never XDG_RUNTIME_DIR: artifacts must outlive the login session
    assert root.name == f"pyscratch-{os.getuid()}"
    assert "XDG_RUNTIME_DIR" not in str(root) or not os.environ.get("XDG_RUNTIME_DIR")


def test_handoff_root_prefers_xdg_runtime_dir(tmp_path):
    if os.name != "posix":
        return
    old = os.environ.get("XDG_RUNTIME_DIR")
    try:
        os.environ["XDG_RUNTIME_DIR"] = str(tmp_path)
        assert server_mod._handoff_root() == tmp_path / "pyscratch"
        # scratch root must NOT follow it
        assert server_mod._scratch_root().name == f"pyscratch-{os.getuid()}"
        os.environ.pop("XDG_RUNTIME_DIR")
        assert server_mod._handoff_root() == server_mod._scratch_root()
    finally:
        if old is not None:
            os.environ["XDG_RUNTIME_DIR"] = old
        else:
            os.environ.pop("XDG_RUNTIME_DIR", None)


def test_hook_and_server_agree_on_roots():
    hook = _load_hook()
    assert hook._scratch_root() == server_mod._scratch_root()
    assert hook._handoff_root() == server_mod._handoff_root()


def test_secure_mkdir_creates_private_dir(tmp_path):
    d = server_mod._secure_mkdir(tmp_path / "root")
    assert d == tmp_path / "root"
    if os.name == "posix":
        assert (os.lstat(d).st_mode & 0o777) == 0o700


def test_secure_mkdir_rejects_symlink(tmp_path):
    if os.name != "posix":
        return
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    assert server_mod._secure_mkdir(link) is None


def test_secure_mkdir_strips_group_other_bits(tmp_path):
    if os.name != "posix":
        return
    d = tmp_path / "loose"
    d.mkdir()
    os.chmod(d, 0o777)
    assert server_mod._secure_mkdir(d) == d
    assert (os.lstat(d).st_mode & 0o777) == 0o700


def test_read_trusted_json_accepts_own_private_file(tmp_path):
    p = tmp_path / "h.json"
    p.write_text('{"a": 1}')
    os.chmod(p, 0o600)
    assert server_mod._read_trusted_json(p) == {"a": 1}


def test_read_trusted_json_rejects_group_writable(tmp_path):
    if os.name != "posix":
        return
    p = tmp_path / "h.json"
    p.write_text('{"a": 1}')
    os.chmod(p, 0o666)
    assert server_mod._read_trusted_json(p) is None


def test_read_trusted_json_rejects_symlink(tmp_path):
    if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
        return
    p = tmp_path / "h.json"
    p.write_text('{"a": 1}')
    os.chmod(p, 0o600)
    link = tmp_path / "link.json"
    link.symlink_to(p)
    assert server_mod._read_trusted_json(link) is None


def test_read_trusted_json_missing_file(tmp_path):
    assert server_mod._read_trusted_json(tmp_path / "nope.json") is None


def test_hook_writes_private_handoff(tmp_path):
    hook = _load_hook()
    os.environ["PY_SCRATCH_HANDOFF_DIR"] = str(tmp_path / "handoff")
    try:
        hook.write_handoff({"session_id": "s"}, 1234, 5678)
        target = tmp_path / "handoff" / "1234-5678.json"
        assert json.loads(target.read_text()) == {"session_id": "s"}
        if os.name == "posix":
            assert (os.lstat(target).st_mode & 0o077) == 0
        # overwrite (same instance re-fires on /clear) must also work
        hook.write_handoff({"session_id": "s2"}, 1234, 5678)
        assert json.loads(target.read_text()) == {"session_id": "s2"}
    finally:
        os.environ.pop("PY_SCRATCH_HANDOFF_DIR", None)


if __name__ == "__main__":
    import asyncio
    import inspect
    import tempfile
    import traceback

    passed = failed = 0
    tests = [(k, v) for k, v in list(globals().items()) if k.startswith("test_")]
    for name, fn in tests:
        try:
            sig = inspect.signature(fn)
            kwargs = {}
            if "tmp_path" in sig.parameters:
                kwargs["tmp_path"] = Path(tempfile.mkdtemp())
            result = fn(**kwargs)
            if asyncio.iscoroutine(result):
                asyncio.run(result)
            print(f"  PASS {name}")
            passed += 1
        except Exception as e:
            print(f"  FAIL {name}: {e}")
            traceback.print_exc()
            failed += 1

    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
