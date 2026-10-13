# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Tests for the Bash hooks: each hook runs as a subprocess fed JSON on stdin."""

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

HOOKS = Path(__file__).parent / "hooks"


def run_hook(script, event, tmp_path, stdin=None):
    env = dict(os.environ, PY_SCRATCH_HOOK_STATE_DIR=str(tmp_path / "state"))
    data = stdin if stdin is not None else json.dumps(event)
    return subprocess.run([sys.executable, str(HOOKS / script)], input=data,
                          capture_output=True, text=True, env=env, timeout=30)


def bash_event(command, cwd, **kw):
    return {"hook_event_name": "PreToolUse", "tool_name": "Bash", "session_id": "s1",
            "tool_use_id": kw.pop("tool_use_id", "t1"), "cwd": str(cwd),
            "tool_input": {"command": command}, **kw}


def guard(command, tmp_path, **kw):
    return run_hook("bash_guard.py", bash_event(command, tmp_path, **kw), tmp_path)


def assert_mcp(r):
    assert r.returncode == 2, r.stderr
    assert "select:mcp__plugin_py-scratch_py-scratch__run_python_script" in r.stderr
    assert "intent, code, time_estimate" in r.stderr


def assert_edit(r):
    assert r.returncode == 2, r.stderr
    assert "Edit tool" in r.stderr and "run_python_script" not in r.stderr


# --- python detection ---

def test_python_c_oneliner_allowed(tmp_path):
    assert guard('python3 -c "print(1)"', tmp_path).returncode == 0


def test_python_c_multiline_blocked(tmp_path):
    assert_mcp(guard('python3 -c "\nimport os\nprint(os.getcwd())\n"', tmp_path))


def test_heredoc_edit_like(tmp_path):
    cmd = "python3 - <<'EOF'\np = 'a.py'\ns = open(p).read()\nopen(p, 'w').write(s.replace('a', 'b'))\nEOF"
    assert_edit(guard(cmd, tmp_path))


def test_heredoc_edit_like_variants(tmp_path):
    for body in ["Path('a').write_text('x')", "os.replace('a', 'b')", "shutil.move('a', 'b')",
                 "open('a', mode='a').close()", "for l in fileinput.input('a', inplace=True): pass"]:
        assert_edit(guard(f"python3 - <<EOF\n{body}\nEOF", tmp_path))


def test_heredoc_compute(tmp_path):
    assert_mcp(guard("python3 - <<'EOF'\nimport sys\nprint(sys.version)\nopen('a').read()\nEOF", tmp_path))


def test_stdout_write_is_not_edit(tmp_path):
    assert_mcp(guard("python3 - <<EOF\nimport sys\nsys.stdout.write('x')\nEOF", tmp_path))


def test_python_flags_blocked(tmp_path):
    assert_mcp(guard("python3 -I - <<EOF\nprint(1)\nEOF", tmp_path))
    assert_mcp(guard("python3 -u -B -X dev - <<EOF\nprint(1)\nEOF", tmp_path))
    assert_mcp(guard("python3.12 <<EOF\nprint(1)\nEOF", tmp_path))
    assert_mcp(guard("uv run --with x python - <<EOF\nprint(1)\nEOF", tmp_path))


def test_python_inside_other_heredoc_allowed(tmp_path):
    cmd = "cat >> notes.md <<'EOF'\nuse this:\npython3 - <<X\nprint(1)\nX\nEOF"
    assert guard(cmd, tmp_path).returncode == 0


def test_compound_cd_blocked(tmp_path):
    assert_mcp(guard("cd x && python3 - <<EOF\nprint(1)\nEOF", tmp_path))
    assert_mcp(guard("cd x && env FOO=1 timeout 5 python3 -I - <<EOF\nprint(1)\nEOF", tmp_path))


def test_script_file_allowed(tmp_path):
    assert guard("python3 script.py", tmp_path).returncode == 0
    assert guard("cd x && python3 -u script.py --arg", tmp_path).returncode == 0
    assert guard("python3 -m pytest -x", tmp_path).returncode == 0


def test_commit_message_mentions_python_c(tmp_path):
    assert guard('git commit -m "block python -c\n\nand python3 - <<EOF"', tmp_path).returncode == 0
    assert guard("echo 'python3 -c x' | cat", tmp_path).returncode == 0


# --- sed / perl in place ---

def make_file(tmp_path, n=100, name="foo.py"):
    p = tmp_path / name
    p.write_text("".join(f"line {i}\n" for i in range(n)))
    return p


def test_sed_small_change_allowed(tmp_path):
    make_file(tmp_path)
    assert guard("sed -i 's/line 5$/LINE 5/' foo.py", tmp_path).returncode == 0


def test_sed_huge_change_denied_then_allowed(tmp_path):
    make_file(tmp_path)
    cmd = "sed -i 's/line/LINE/' foo.py"
    r = guard(cmd, tmp_path)
    assert r.returncode == 2
    assert "would change 100 of 100 lines in foo.py" in r.stderr
    assert "run the exact same command again" in r.stderr
    assert "+LINE 0" in r.stderr
    assert guard(cmd, tmp_path).returncode == 0
    assert guard(cmd, tmp_path).returncode == 2  # confirmation was consumed
    # a different session does not inherit the confirmation
    assert guard(cmd, tmp_path, session_id="other").returncode == 2
    assert (tmp_path / "foo.py").read_text().startswith("line 0")  # hook never edits


def test_sed_variants_and_cd(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    make_file(sub)
    for cmd in ["cd sub && sed -i.bak -e 's/line/L/' foo.py", "cd sub && sed --in-place 's/line/L/' foo.py",
                "cd sub && sed -ni 's/line/L/p' foo.py", "cd sub && sed -i -E 's/l(i)/\\1/' fo*.py"]:
        assert guard(cmd, tmp_path).returncode == 2, cmd


def test_sed_unresolvable_allowed(tmp_path):
    make_file(tmp_path)
    assert guard("sed -i 's/line/L/' missing.py", tmp_path).returncode == 0
    assert guard("sed -i 's/line/L/' *.nomatch", tmp_path).returncode == 0
    assert guard("sed -i 's/line/L/' $FILES", tmp_path).returncode == 0
    assert guard("find . -name '*.py' -exec sed -i 's/line/L/' {} +", tmp_path).returncode == 0
    assert guard("sed 's/line/L/' foo.py", tmp_path).returncode == 0  # not in place
    assert guard("sed -i 's/line/L/w out.txt' foo.py", tmp_path).returncode == 0  # w command: not run


def test_perl_variants(tmp_path):
    make_file(tmp_path)
    for cmd in ["perl -pi -e 's/line/L/' foo.py", "perl -i -pe 's/line/L/' foo.py",
                "perl -pie 's/line/L/' foo.py", "perl -pi.bak -e 's/line/L/' foo.py"]:
        r = guard(cmd, tmp_path, session_id=uuid.uuid4().hex)
        assert r.returncode == 2, cmd
        assert "would change 100 of 100 lines" in r.stderr
    assert guard("perl -pi -e 's/line 5$/L/' foo.py", tmp_path).returncode == 0
    assert guard("perl -pe 's/line/L/' foo.py", tmp_path).returncode == 0


# --- git report ---

def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                   check=True, capture_output=True)


def test_post_report(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "big.txt").write_text("".join(f"row {i}\n" for i in range(300)))
    (repo / "other.txt").write_text("a\nb\n")
    git(repo, "init", "-q")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "init")
    ev = bash_event("echo hi > big.txt", repo, tool_use_id="toolu_1")
    assert run_hook("git_report.py", ev, tmp_path).stdout == ""
    ev["hook_event_name"] = "PreToolUse"
    r = run_hook("git_report.py", ev, tmp_path)
    assert r.returncode == 0 and r.stdout == ""
    (repo / "big.txt").write_text("".join(f"new {i}\n" for i in range(300)))
    post = dict(ev, hook_event_name="PostToolUse", tool_response={})
    r = run_hook("git_report.py", post, tmp_path)
    out = json.loads(r.stdout)["hookSpecificOutput"]
    assert out["hookEventName"] == "PostToolUse"
    assert "changed 300 lines across 1 files" in out["additionalContext"]
    assert "big.txt (+300/-300)" in out["additionalContext"]
    assert "git -C" in out["additionalContext"] and "checkout -- big.txt" in out["additionalContext"]
    # snapshot cleaned up: a second post is silent
    assert run_hook("git_report.py", post, tmp_path).stdout == ""


def test_post_small_change_silent_and_outside_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "f.txt").write_text("".join(f"row {i}\n" for i in range(300)))
    git(repo, "init", "-q")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "init")
    ev = bash_event("touch x; sed -i 's/row 1$/x/' f.txt", repo, tool_use_id="toolu_2")
    run_hook("git_report.py", ev, tmp_path)
    (repo / "f.txt").write_text((repo / "f.txt").read_text().replace("row 1\n", "x\n"))
    r = run_hook("git_report.py", dict(ev, hook_event_name="PostToolUse"), tmp_path)
    assert r.returncode == 0 and r.stdout == ""
    plain = tmp_path / "plain"
    plain.mkdir()
    ev = bash_event("rm -rf x", plain, tool_use_id="toolu_3")
    r = run_hook("git_report.py", ev, tmp_path)
    assert r.returncode == 0 and r.stdout == ""


# --- robustness ---

def test_fail_open_on_garbage(tmp_path):
    for script in ("bash_guard.py", "git_report.py"):
        for data in ["", "not json", "[1,2]", '{"tool_name": "Bash"}',
                     '{"tool_name": "Bash", "tool_input": {"command": 5}}',
                     '{"tool_name": "Bash", "tool_input": null, "cwd": 3}']:
            r = run_hook(script, None, tmp_path, stdin=data)
            assert r.returncode == 0, (script, data, r.stderr)


def test_non_bash_ignored(tmp_path):
    ev = bash_event("python3 - <<EOF\nprint(1)\nEOF", tmp_path)
    ev["tool_name"] = "Write"
    assert run_hook("bash_guard.py", ev, tmp_path).returncode == 0


def test_session_start_and_hooks_json():
    out = subprocess.run([str(HOOKS / "session-start.sh")], capture_output=True, text=True).stdout
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "select:mcp__plugin_py-scratch_py-scratch__run_python_script" in ctx
    assert "intent" in ctx and "time_estimate" in ctx and "Edit tool" in ctx
    cfg = json.loads((HOOKS / "hooks.json").read_text())["hooks"]
    assert "clear" in cfg["SessionStart"][0]["matcher"].split("|")
    assert cfg["PostToolUse"][0]["matcher"] == "Bash"


def test_hook_commands_are_executable(tmp_path):
    # hooks.json invokes the entry scripts directly; a missing exec bit makes Claude Code
    # treat the hook as a non-blocking error and silently skip it.
    cfg = json.loads((HOOKS / "hooks.json").read_text())["hooks"]
    for groups in cfg.values():
        for group in groups:
            for hook in group["hooks"]:
                entry = hook["command"].split('"')[1].replace("${CLAUDE_PLUGIN_ROOT}/hooks", str(HOOKS))
                assert os.access(entry, os.X_OK), entry
    # and the wrapper really blocks, end to end
    ev = bash_event("python3 -I - <<'EOF'\nprint(1)\nEOF", tmp_path)
    r = subprocess.run([str(HOOKS / "run-hook.sh"), "bash_guard.py"], input=json.dumps(ev),
                       capture_output=True, text=True)
    assert r.returncode == 2, r.stderr
