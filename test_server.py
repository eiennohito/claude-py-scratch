# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Smoke tests for py-scratch."""

import asyncio
import json
import os
import re
import subprocess
import sys
import time
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


# --- run_python_script (public API; returns the plain-text result) ---

def _run_of(text):
    """The registry entry for the run a result text is about."""
    return server_mod._runs[int(re.search(r"run (\d+)", text.splitlines()[0]).group(1))]


def _body(text):
    return text.splitlines()[1:]


async def test_fast_script_gives_ok_line():
    r = await run_python_script(intent="test", code="print('hi')")
    first = r.splitlines()[0]
    assert re.match(r"ok exit=0 \d+\.\d\ds · run \d+ · .*/\{code\.py,stdout,stderr\}$", first)
    assert _body(r) == ["hi"]
    run = _run_of(r)
    assert (run.dir / "stdout").read_text() == "hi\n"
    assert (run.dir / "code.py").exists()
    assert not r.lstrip().startswith("{")  # plain text, not JSON


async def test_run_python_script_default_tail_5():
    code = "\n".join(f"print('line {i}')" for i in range(20))
    lines = _body(await run_python_script(intent="test", code=code))
    assert len(lines) == 5
    assert "line 15" in lines[0]
    assert "line 19" in lines[-1]


async def test_run_python_script_head_only():
    code = "\n".join(f"print('line {i}')" for i in range(20))
    lines = _body(await run_python_script(intent="test", code=code, head=3, tail=0))
    assert len(lines) == 3
    assert "line 0" in lines[0]
    assert "line 2" in lines[-1]


async def test_run_python_script_head_and_tail():
    code = "\n".join(f"print('line {i}')" for i in range(20))
    lines = _body(await run_python_script(intent="test", code=code, head=2, tail=2))
    assert len(lines) == 4
    assert "line 0" in lines[0]
    assert "line 1" in lines[1]
    assert "line 18" in lines[2]
    assert "line 19" in lines[3]


async def test_failed_script_shows_stderr_and_exit():
    r = await run_python_script(intent="t", code="print('out')\nraise ValueError('boom')")
    assert r.startswith("failed exit=1 ")
    assert "stderr:" in r and "ValueError: boom" in r
    # the bootstrap frame is stripped from the traceback
    assert "bootstrap.py" not in r
    assert 'code.py", line 2' in r


async def test_ok_with_stderr_only_mentions_it():
    r = await run_python_script(intent="t", code="import sys; print('x', file=sys.stderr)")
    assert r.startswith("ok ")
    assert "(stderr: 1 lines" in r
    assert "stderr:\n" not in r


# --- early return, wait, kill, hard limit ---

_SLOW = (
    "import time\n"
    "print('started', flush=True)\n"
    "t0 = time.time()\n"
    "while time.time() - t0 < 60:\n"
    "    x = sum(range(1000))\n"        # line 5
)


async def test_exceeding_estimate_returns_running_with_trace():
    t0 = time.monotonic()
    r = await run_python_script(intent="slow", code=_SLOW, time_estimate=0.5)
    try:
        assert time.monotonic() - t0 < 10
        first = r.splitlines()[0]
        assert first.startswith("running ")
        assert "(estimate 0.5s, hard limit 5s)" in first
        assert first.endswith("{code.py,stdout,stderr,trace}")
        run = _run_of(r)
        assert "at line 5: x = sum(range(1000))" in r
        assert "started" in r
        assert f'run_id={run.id}, action="wait"|"kill"' in r
        trace = (run.dir / "trace").read_text()
        assert 'code.py", line 5' in trace
        assert run.state == "running" and run.pid
    finally:
        server_mod._kill_process(run.proc)
        await run.done.wait()


async def test_estimate_clock_starts_with_the_script_not_with_uv():
    # Uncached dependency resolution happens before the script starts; simulate a
    # slow uv by delaying the interpreter's start. The run must not have counted it.
    code = "import time; time.sleep(0.2); print('done')"
    orig = server_mod._build_command

    def slow_start(*argv, extra_deps=None):
        return ["sh", "-c", 'sleep 1.5; exec "$@"', "sh", *orig(*argv, extra_deps=extra_deps)]

    server_mod._build_command = slow_start
    try:
        r = await run_python_script(intent="t", code=code, time_estimate=1)
    finally:
        server_mod._build_command = orig
    assert r.startswith("ok "), r
    assert _run_of(r).elapsed < 1


async def test_wait_completes():
    r = await run_python_script(
        intent="t", code="import time; time.sleep(1.2); print('finished')", time_estimate=0.5
    )
    assert r.startswith("running ")
    run = _run_of(r)
    r2 = await run_python_script(run_id=run.id, action="wait")
    r3 = r2 if r2.startswith("ok ") else await run_python_script(run_id=run.id, action="wait")
    assert r3.startswith("ok exit=0 ")
    assert "finished" in r3
    # waiting on a finished run just reports it again
    assert (await run_python_script(run_id=run.id)).startswith("ok ")


async def test_wait_still_running_samples_again():
    r = await run_python_script(intent="t", code=_SLOW, time_estimate=0.3)
    run = _run_of(r)
    try:
        (run.dir / "trace").write_text("")
        r2 = await run_python_script(run_id=run.id, action="wait")
        assert r2.startswith("running ")
        assert "at line 5" in r2
        assert 'code.py", line 5' in (run.dir / "trace").read_text()
    finally:
        server_mod._kill_process(run.proc)
        await run.done.wait()


async def test_wait_with_new_estimate_extends_the_hard_limit():
    r = await run_python_script(intent="t", code=_SLOW, time_estimate=0.3)
    run = _run_of(r)
    try:
        r2 = await run_python_script(run_id=run.id, action="wait", time_estimate=0.3 * 7)
        assert "estimate 2.1s" in r2
        assert run.hard_limit > 3
    finally:
        server_mod._kill_process(run.proc)
        await run.done.wait()


async def test_kill_action():
    r = await run_python_script(intent="t", code=_SLOW, time_estimate=0.3)
    run = _run_of(r)
    pid = run.pid
    k = await run_python_script(run_id=run.id, action="kill")
    assert k.startswith("killed ")
    assert run.state == "killed"
    await asyncio.sleep(0.2)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return
    raise AssertionError("interpreter survived the kill")


async def test_hard_limit_kills_and_keeps_last_trace():
    r = await run_python_script(intent="t", code=_SLOW, time_estimate=0.1)
    run = _run_of(r)
    await asyncio.wait_for(run.done.wait(), 10)
    assert run.state == "timeout"
    assert run.elapsed < 3
    final = await run_python_script(run_id=run.id)
    assert final.startswith("timeout (hard limit 1s) ")
    assert "last seen at line 5" in final
    assert 'code.py", line 5' in (run.dir / "trace").read_text()


async def test_hard_limit_kills_the_grandchild_interpreter():
    # The direct child is `uv run`; the interpreter running the script is its child.
    # Killing only uv would leave that interpreter alive forever.
    r = await run_python_script(
        intent="orphan check",
        code="import os, sys, time; print(os.getpid(), flush=True); time.sleep(120)",
        time_estimate=0.2,
    )
    run = _run_of(r)
    await asyncio.wait_for(run.done.wait(), 10)
    assert run.state == "timeout"
    child_pid = int((run.dir / "stdout").read_text().split()[0])
    await asyncio.sleep(0.5)
    try:
        os.kill(child_pid, 0)
    except ProcessLookupError:
        return
    raise AssertionError(f"interpreter {child_pid} survived the timeout kill")


async def test_unknown_run_id_is_an_error():
    sent = _collect_responses()
    await server_mod._handle_tool_call(10, {
        "name": "run_python_script", "arguments": {"run_id": 987654, "action": "wait"},
    })
    assert len(sent) == 1
    assert sent[0]["result"]["isError"] is True
    assert "Unknown run_id 987654" in sent[0]["result"]["content"][0]["text"]


async def test_bad_action_is_an_error():
    sent = _collect_responses()
    r = await run_python_script(intent="t", code="print(1)")
    await server_mod._handle_tool_call(11, {
        "name": "run_python_script",
        "arguments": {"run_id": _run_of(r).id, "action": "explode"},
    })
    assert sent[0]["result"]["isError"] is True
    await server_mod._handle_tool_call(12, {
        "name": "run_python_script", "arguments": {"action": "kill", "code": "1"},
    })
    assert sent[1]["result"]["isError"] is True


async def test_timeout_is_an_alias_of_time_estimate():
    sent = _collect_responses()
    await server_mod._handle_tool_call(13, {
        "name": "run_python_script",
        "arguments": {"code": "import time; time.sleep(30)", "timeout": 0.3},
    })
    text = sent[0]["result"]["content"][0]["text"]
    assert not sent[0]["result"].get("isError")
    assert text.startswith("running ")
    assert "estimate 0.3s" in text
    assert "note: 'timeout' is now 'time_estimate'" in text
    run = _run_of(text)
    server_mod._kill_process(run.proc)
    await run.done.wait()


async def test_cancelled_request_kills_its_blocking_run():
    sent = _collect_responses()
    task = asyncio.create_task(server_mod._handle_tool_call(14, {
        "name": "run_python_script",
        "arguments": {"code": "import time; time.sleep(30)", "time_estimate": 20},
    }))
    for _ in range(100):
        await asyncio.sleep(0.05)
        if 14 in server_mod._blocking and server_mod._blocking[14].pid:
            break
    server_mod._cancel_request(14)
    await asyncio.wait_for(task, 10)
    assert len(sent) == 1  # still exactly one response
    assert sent[0]["result"]["content"][0]["text"].startswith("killed ")


# --- resources ---

async def test_resource_read_status():
    r = await run_python_script(intent="watch me", code=_SLOW, time_estimate=0.3)
    run = _run_of(r)
    try:
        uri = f"pyscratch://runs/{run.id}/status"
        listed = server_mod._sync_handle("resources/list", {})["resources"]
        assert uri in [x["uri"] for x in listed]
        got = server_mod._sync_handle("resources/read", {"uri": uri})["contents"][0]
        assert got["uri"] == uri and got["mimeType"] == "application/json"
        st = json.loads(got["text"])
        assert st["state"] == "running"
        assert st["estimate"] == 0.3 and st["hard_limit"] == 3
        assert st["exit_code"] is None
        assert st["pgid"] == run.proc.pid
        assert st["last_frame"]["line"] == 5
        assert st["dir"] == str(run.dir)
        assert st["elapsed"] > 0
    finally:
        server_mod._kill_process(run.proc)
        await run.done.wait()
    done = json.loads(server_mod._sync_handle("resources/read", {"uri": uri})["contents"][0]["text"])
    assert done["state"] in ("killed", "crashed")


def test_resource_read_unknown_is_an_rpc_error():
    try:
        server_mod._sync_handle("resources/read", {"uri": "pyscratch://runs/987654/status"})
    except server_mod._RpcError as exc:
        assert exc.code == -32002
    else:
        raise AssertionError("expected _RpcError")


def test_resource_templates():
    r = server_mod._sync_handle("resources/templates/list", {})
    assert r["resourceTemplates"][0]["uriTemplate"] == "pyscratch://runs/{id}/status"


# --- end to end over stdio ---

def _spawn_server(tmp_path):
    env = dict(os.environ, PY_SCRATCH_DIR=str(tmp_path / "art"), PY_SCRATCH_USE_SCRATCHPAD="0",
               PYTHONPATH=str(Path(__file__).parent / "src"))
    return subprocess.Popen(
        [sys.executable, "-m", "py_scratch"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True, env=env, cwd=str(tmp_path),
    )


def _rpc(proc, msg_id, method, params=None):
    proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": msg_id, "method": method,
                                 "params": params or {}}) + "\n")
    proc.stdin.flush()
    return json.loads(proc.stdout.readline())


def test_eof_kills_live_runs(tmp_path):
    proc = _spawn_server(tmp_path)
    try:
        init = _rpc(proc, 1, "initialize")
        assert init["result"]["capabilities"]["resources"] == {}
        assert init["result"]["serverInfo"]["version"] == "0.8.1"
        resp = _rpc(proc, 2, "tools/call", {"name": "run_python_script", "arguments": {
            "code": "import os, time; print(os.getpid(), flush=True); time.sleep(120)",
            "time_estimate": 0.5,
        }})
        text = resp["result"]["content"][0]["text"]
        assert text.startswith("running ")
        pid = int(next(ln for ln in _body(text) if ln.isdigit()))
        os.kill(pid, 0)  # alive while the server runs
        proc.stdin.close()
        proc.wait(timeout=15)
        for _ in range(50):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.1)
        raise AssertionError("background run survived stdin EOF")
    finally:
        proc.kill()
        proc.wait()


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
    assert "resources" in r["capabilities"]
    assert r["serverInfo"]["version"] == "0.8.1"
    assert r["serverInfo"]["name"] == "py-scratch"


def test_handle_initialized_is_notification():
    assert _sync_handle("notifications/initialized", {}) is None


def test_handle_tools_list():
    r = _sync_handle("tools/list", {})
    assert len(r["tools"]) == 1
    assert r["tools"][0]["name"] == "run_python_script"
    schema = r["tools"][0]["inputSchema"]
    assert "code" in schema["properties"]
    assert "time_estimate" in schema["properties"]
    assert "timeout" not in schema["properties"]
    assert "run_id" in schema["properties"] and "action" in schema["properties"]
    assert "required" not in schema  # code/intent are not needed with run_id
    assert "estimate" in r["tools"][0]["description"]


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
    text = sent[0]["result"]["content"][0]["text"]
    assert text.startswith("ok exit=0 ")
    assert "aliased" in text
    assert "note: " in text and "'script' as 'code'" in text


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
    r = await run_python_script(intent="hang", code="import time; time.sleep(30)", time_estimate=0.1)
    run = _run_of(r)
    await asyncio.wait_for(run.done.wait(), 10)
    assert run.state == "timeout"
    assert "hard limit" in run.explanation


async def test_signal_death_is_reported_as_crash():
    r = await run_python_script(
        intent="crash", code="import os, signal; os.kill(os.getpid(), signal.SIGKILL)"
    )
    assert r.startswith("crashed ")
    assert "SIGKILL" in r


async def test_crash_with_output_is_not_retried():
    r = await run_python_script(
        intent="crash after output",
        code="import os, signal, sys; print('side effect'); sys.stdout.flush();"
             " os.kill(os.getpid(), signal.SIGKILL)",
    )
    assert r.startswith("crashed ")
    assert "retry of" not in r


def test_normalise_args_coerces_loose_types():
    intent, code, deps, estimate, head, tail = server_mod._normalise_args(
        None, "print(1)", "httpx", "4.5", None, "3"
    )
    assert intent == ""
    assert deps == ["httpx"]
    assert estimate == 4.5
    assert head == 0
    assert tail == 3


def test_normalise_args_clamps_nonsense():
    _, _, _, estimate, _, _ = server_mod._normalise_args("i", "c", [], -5, 0, 5)
    assert estimate == 0.1
    _, _, _, estimate, _, _ = server_mod._normalise_args("i", "c", [], "soon", 0, 5)
    assert estimate == 10


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


async def test_crash_with_no_output_is_retried_once():
    r = await run_python_script(
        intent="silent crash",
        code="import os, signal; os.kill(os.getpid(), signal.SIGKILL)",
    )
    assert r.startswith("crashed ")
    assert re.search(r"retry of run \d+", r.splitlines()[0])
    assert "reproduced on retry" in r


async def test_import_error_auto_installs_dependency():
    # a package that is certainly not importable; the inferred dep is attempted
    # (and fails to resolve), which is enough to show the retry was made
    r = await run_python_script(intent="t", code="import definitely_not_a_module_xyz")
    assert "auto-installed definitely_not_a_module_xyz" in r.splitlines()[0]


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
