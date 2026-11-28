"""Things a code review of 0.3.0 / 0.3.1 found, each checked against the code before it was fixed."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from types import SimpleNamespace as NS

import pytest

from speckfish import cli as C
from speckfish import config as K
from speckfish import harnesses as H
from speckfish import proctree, progress, runner, screen, store
from speckfish.config import Config

# ---------------- consent to run with no sandbox lives in the user's folder, not the repo ----------------

def _write_repo_config(root, **data):
    (root / ".speckfish").mkdir(exist_ok=True)
    (root / ".speckfish" / "config.json").write_text(json.dumps(data), encoding="utf-8")


def test_a_repo_cannot_grant_itself_unsafe(tmp_path):
    _write_repo_config(tmp_path, harness="codex", unsafe=True)
    cfg = Config.load(tmp_path)
    assert cfg.unsafe is False and cfg.unsafe_in_repo is True


def test_the_refusal_says_the_repo_setting_was_ignored(tmp_path, monkeypatch):
    _write_repo_config(tmp_path, harness="codex", unsafe=True)
    monkeypatch.setattr(H.shutil, "which", lambda name: "/bin/" + name)
    with pytest.raises(SystemExit) as e:
        H.get_harness(Config.load(tmp_path), "codex")
    assert "a repository cannot grant that" in str(e.value) and "~/.speckfish" in str(e.value)


def test_consent_is_saved_per_project_in_the_users_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("SPECKFISH_HOME", str(home))
    proj, other = tmp_path / "a", tmp_path / "b"
    proj.mkdir(), other.mkdir()
    assert C.main(["--root", str(proj), "config", "unsafe=true"]) == C.EXIT_OK
    assert Config.load(proj).unsafe is True and Config.load(other).unsafe is False
    assert str(proj.resolve()) in json.loads((home / "unsafe.json").read_text())["projects"]
    assert "unsafe" not in json.loads((proj / ".speckfish" / "config.json").read_text()) \
        if (proj / ".speckfish" / "config.json").exists() else True
    Config.load(proj).save()                              # saving something else never writes it back
    assert "unsafe" not in (proj / ".speckfish" / "config.json").read_text()
    assert C.main(["--root", str(proj), "config", "unsafe=false"]) == C.EXIT_OK
    assert Config.load(proj).unsafe is False


# ---------------- a model name is a name ----------------

@pytest.mark.parametrize("name,ok", [("gpt-5.5", True), ("claude-opus-4-8", True), ("o3:high", True),
                                     ("anthropic/claude-sonnet-5-5", True), ("--dangerously-skip-permissions", False),
                                     ("-x", False), ("a b", False), ("", False), ("m;rm -rf", False), ("a" * 101, False)])
def test_valid_model(name, ok):
    assert K.valid_model(name) is ok


def test_a_repo_cannot_slip_an_option_in_through_the_model(tmp_path):
    _write_repo_config(tmp_path, model="--dangerously-skip-permissions")
    assert Config.load(tmp_path).model == ""
    assert C.main(["--root", str(tmp_path), "config", "model=--oops"]) == C.EXIT_ENV
    assert C.main(["--root", str(tmp_path), "--model=--oops", "status"]) == C.EXIT_ENV
    assert C.main(["--root", str(tmp_path), "config", "model=gpt-5.5"]) == C.EXIT_OK


# ---------------- stopping the agent ----------------

@pytest.mark.skipif(os.name == "nt", reason="process groups are POSIX")
def test_the_group_is_signalled_even_when_the_launched_process_already_exited(tmp_path):
    """The shell exits at once; its background child lives on in the same group. It must still be killed."""
    marker = tmp_path / "pid"
    proc = subprocess.Popen(["sh", "-c", f"sleep 60 & echo $! > {marker}"], start_new_session=True)
    proc.wait(timeout=10)
    deadline = time.time() + 5
    while not marker.exists() and time.time() < deadline:
        time.sleep(0.05)
    child = int(marker.read_text())
    os.kill(child, 0)                                      # alive
    ok, how = proctree.kill_tree(proc, windows=False)
    assert ok and how == "already finished"
    for _ in range(100):
        try:
            os.kill(child, 0)
            time.sleep(0.05)
        except ProcessLookupError:
            break
    else:
        os.kill(child, signal.SIGKILL)
        pytest.fail("the child of the exited shell survived")


class FakeK32:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        fn = lambda *a: self.calls.append(name) or 1          # noqa: E731
        object.__setattr__(self, name, fn)
        return fn


def test_the_job_api_is_declared_so_handles_are_not_truncated():
    from ctypes import wintypes
    k = NS()
    for name in ("CreateJobObjectW", "SetInformationJobObject", "AssignProcessToJobObject",
                 "TerminateJobObject", "CloseHandle"):
        setattr(k, name, lambda *a: 1)
    job = proctree.Job(k)
    job.create()
    assert k.CreateJobObjectW.restype is wintypes.HANDLE
    assert k.AssignProcessToJobObject.argtypes[0] is wintypes.HANDLE
    assert k.TerminateJobObject.restype is wintypes.BOOL


# ---------------- locks and state ----------------

def test_a_read_only_folder_does_not_wait_out_the_lock_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(store.os, "access", lambda *_a: False)
    t = time.time()
    with store.file_lock(tmp_path / "state.json", timeout=5.0) as held:
        assert held is False
    assert time.time() - t < 1.0


def test_a_state_file_that_is_not_json_is_kept_before_it_is_rewritten(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    (run / "state.json").write_text("{this is not json", encoding="utf-8")
    cfg = Config(root=tmp_path)
    res = runner.StepResult(step="plan", kind="plan", ok=True, exit_code=0, duration_s=1.0)
    runner.record_state(cfg, run, "rid", res, "plan", NS(name="claude"), "now")
    saved = list(run.glob("state.json.corrupt-*"))
    assert len(saved) == 1 and saved[0].read_text(encoding="utf-8") == "{this is not json"
    assert json.loads((run / "state.json").read_text(encoding="utf-8"))["steps"]["plan"]["status"] == "ok"


# ---------------- what the agent can make the terminal do ----------------

def test_escape_sequences_in_an_agents_command_are_not_printed():
    nasty = "cat \x1b]0;pwned\x07 file\x1b[2J.txt\x00\x9b"
    out = progress._short(nasty)
    assert "\x1b" not in out and "\x07" not in out and "\x00" not in out and "\x9b" not in out
    assert "pwned" not in out and out.startswith("cat") and "file" in out
    assert progress._short("plain command --flag") == "plain command --flag"


def test_the_screen_registers_its_exit_hook_once(monkeypatch):
    calls = []
    monkeypatch.setattr(screen.atexit, "register", lambda fn: calls.append(fn))
    s = screen.AltScreen(stream=NS(write=lambda *_: None, flush=lambda: None, isatty=lambda: True))
    monkeypatch.setattr(type(s), "possible", property(lambda self: True))
    monkeypatch.setattr(s, "_catch_signals", lambda: None)
    for _ in range(3):
        s.enter()
        s.leave()
    assert len(calls) == 1


# ---------------- the clock set back during a step ----------------

def _exec(tmp_path, sleep, timeout, **kw):
    cfg = Config(root=tmp_path.resolve())
    run_dir = runner.run_dir_for(cfg, "rid")
    inv = NS(argv=[sys.executable, "-c", f"import time; time.sleep({sleep})"], env=None, stdin=None)
    return runner.execute(NS(echo_stdout=False), inv, "implement", tmp_path, run_dir, timeout,
                          progress=False, **kw)


def test_a_clock_set_back_does_not_extend_the_deadline(tmp_path, monkeypatch):
    """The wall clock goes back 100s in the middle of a step with a 2s timeout: it must still time out."""
    import threading
    real = time.time
    offset = [0.0]
    monkeypatch.setattr(time, "time", lambda: real() + offset[0])
    monkeypatch.setenv("SPECKFISH_POLL_SECONDS", "0.05")
    monkeypatch.setenv("SPECKFISH_IDLE_SECONDS", "0")
    timer = threading.Timer(0.4, lambda: offset.__setitem__(0, -100.0))
    timer.daemon = True
    timer.start()
    t = real()
    result = _exec(tmp_path, 60, 2)
    assert result[4] is True and real() - t < 15            # timed_out


def test_ctrl_c_still_reports_the_suspended_time(tmp_path, monkeypatch):
    stats: dict = {}
    monkeypatch.setattr(subprocess.Popen, "wait",
                        lambda self, timeout=None: (_ for _ in ()).throw(KeyboardInterrupt))
    with pytest.raises(KeyboardInterrupt):
        _exec(tmp_path, 5, 30, stats=stats)
    assert stats == {"suspended_s": 0.0}
