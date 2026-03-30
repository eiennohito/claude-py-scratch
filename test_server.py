# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Smoke tests for py-scratch."""

import json
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
    assert r["protocolVersion"] == "2024-11-05"
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
        cmd = _build_command(tmp_path / "script.py")
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
        cmd = _build_command(tmp_path / "script.py")
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
        cmd = _build_command(tmp_path / "script.py")
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
        cmd = _build_command(tmp_path / "script.py")
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
        assert "WARNING" in td["description"]
        assert "pkg-a" in td["description"]
        assert "pkg-b" in td["description"]
        assert ".py-scratch.json" in td["description"]
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
