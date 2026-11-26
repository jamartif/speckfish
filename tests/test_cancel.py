"""Ctrl+C while an agent runs: the whole tree stops, the person is told, and a failure is never silent."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from speckfish import proctree
from speckfish import runner as R
from speckfish.config import Config


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:                                   # a zombie is dead for our purposes
        return Path(f"/proc/{pid}/stat").read_text().split()[2] != "Z"
    except OSError:
        return True


# ---------------- for real, on POSIX: SIGINT while an agent and its child are running ----------------

@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
def test_ctrl_c_stops_the_agent_and_the_process_it_started(tmp_path, capsys):
    pidfile = tmp_path / "child.pid"
    agent = (f"import subprocess,sys,time\n"
             f"c=subprocess.Popen(['sleep','600'])\n"
             f"open({str(pidfile)!r},'w').write(str(c.pid))\n"
             f"print('started', flush=True)\n"
             f"time.sleep(600)\n")
    cfg = Config(root=tmp_path.resolve())
    run_dir = R.run_dir_for(cfg, "rid")
    inv = NS(argv=[sys.executable, "-c", agent], env=None, stdin=None)
    timer = threading.Timer(1.5, os.kill, (os.getpid(), signal.SIGINT))
    timer.start()
    started = time.time()
    try:
        with pytest.raises(KeyboardInterrupt):
            R.execute(NS(echo_stdout=False), inv, "implement", tmp_path, run_dir, 120, progress=False)
    finally:
        timer.cancel()
    assert time.time() - started < 15
    child = int(pidfile.read_text())
    time.sleep(0.3)
    assert not _alive(child), "the agent's child process kept running after Ctrl+C"
    err = capsys.readouterr().err
    assert "Stopping the agent" in err and "WARNING" not in err


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
def test_a_step_interrupted_in_a_real_run_leaves_a_log_and_no_state_for_the_unfinished_step(tmp_path):
    cfg = Config(root=tmp_path.resolve())
    run_dir = R.run_dir_for(cfg, "rid")
    inv = NS(argv=[sys.executable, "-c", "import time;print('x',flush=True);time.sleep(600)"], env=None, stdin=None)
    timer = threading.Timer(1.0, os.kill, (os.getpid(), signal.SIGINT))
    timer.start()
    with pytest.raises(KeyboardInterrupt):
        R.execute(NS(echo_stdout=False), inv, "implement", tmp_path, run_dir, 120, progress=False)
    timer.cancel()
    assert (run_dir / "logs" / "implement.log").exists()


# ---------------- the verified kill chain (simulated Windows) ----------------

class FakeProc:
    """A process that dies only when a given method is used (or never)."""

    def __init__(self, dies_on=None, pid=4242):
        self.pid, self.dies_on, self.returncode = pid, dies_on, None
        self.calls: list[str] = []

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is None:
            raise subprocess.TimeoutExpired("x", timeout)
        return self.returncode

    def kill(self):
        self.calls.append("kill")
        if self.dies_on == "kill":
            self.returncode = -9


class FakeJob:
    def __init__(self, proc, works=True):
        self.proc, self.works, self.terminated = proc, works, 0

    def terminate(self):
        self.terminated += 1
        if self.works:
            self.proc.returncode = 1
        return self.works


def test_the_job_object_ends_the_tree_without_needing_taskkill(monkeypatch):
    ran = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: ran.append(a))
    proc = FakeProc()
    ok, how = proctree.kill_tree(proc, FakeJob(proc), windows=True)
    assert ok and "job object" in how and ran == []


def test_when_the_job_fails_taskkill_is_the_next_try(monkeypatch):
    proc = FakeProc()

    def taskkill(args, **k):
        assert args[:3] == ["taskkill", "/F", "/T"]
        proc.returncode = 1

    monkeypatch.setattr(subprocess, "run", taskkill)
    ok, how = proctree.kill_tree(proc, FakeJob(proc, works=False), windows=True)
    assert ok and "taskkill" in how


def test_when_taskkill_is_blocked_it_falls_back_to_killing_the_process(monkeypatch):
    def blocked(*a, **k):
        raise PermissionError("blocked by App Control")

    monkeypatch.setattr(subprocess, "run", blocked)
    proc = FakeProc(dies_on="kill")
    ok, how = proctree.kill_tree(proc, None, wait=0.2, windows=True)
    assert ok and proc.calls == ["kill"]
    assert "children may remain" in how                      # honest: only the launched process was stopped


def test_a_process_that_survives_everything_is_reported_as_not_stopped(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: None)
    proc = FakeProc(dies_on=None)
    ok, how = proctree.kill_tree(proc, FakeJob(proc, works=False), wait=0.1, windows=True)
    assert ok is False and "still running after" in how and "job object" in how and "taskkill" in how


def test_a_failure_to_stop_is_printed_with_the_command_to_do_it_by_hand(monkeypatch, capsys):
    monkeypatch.setattr(R, "_kill_tree", lambda proc, job=None: (False, "still running after: kill"))
    monkeypatch.setattr(R, "is_windows", lambda: True)
    assert R._stop_and_report(FakeProc(pid=777), None) is False
    err = capsys.readouterr().err
    assert "WARNING" in err and "pid 777" in err and "taskkill /F /T /PID 777" in err


def test_a_success_prints_nothing_and_a_second_ctrl_c_forces_the_kill(monkeypatch, capsys):
    monkeypatch.setattr(R, "_kill_tree", lambda proc, job=None: (True, "stopped"))
    assert R._stop_and_report(FakeProc(), None) is True
    assert capsys.readouterr().err == ""
    proc = FakeProc(dies_on="kill")

    def impatient(proc, job=None):
        raise KeyboardInterrupt

    monkeypatch.setattr(R, "_kill_tree", impatient)
    assert R._stop_and_report(proc, None) is True               # no exception escapes; it used kill()
    assert proc.calls == ["kill"]


def test_the_hint_matches_the_platform():
    assert proctree.manual_hint(5, windows=True) == "taskkill /F /T /PID 5"
    assert proctree.manual_hint(5, windows=False) == "kill -9 -5"


# ---------------- job object calls (a fake kernel32) ----------------

class FakeKernel32:
    def __init__(self, fail=None):
        self.calls, self.fail = [], fail
        self.mode = 0x0007

    def CreateJobObjectW(self, a, b):
        self.calls.append("create")
        return 0 if self.fail == "create" else 111

    def SetInformationJobObject(self, h, cls, ref, size):
        self.calls.append(("set", cls, size > 0))
        return 0 if self.fail == "set" else 1

    def AssignProcessToJobObject(self, h, proc_handle):
        self.calls.append(("assign", proc_handle))
        return 0 if self.fail == "assign" else 1

    def TerminateJobObject(self, h, code):
        self.calls.append(("terminate", code))
        return 1

    def CloseHandle(self, h):
        self.calls.append("close")
        return 1

    # console
    def GetStdHandle(self, n):
        return n

    def GetConsoleMode(self, handle, ref):
        if self.fail == "notconsole":
            return 0
        ref._obj.value = self.mode
        return 1

    def SetConsoleMode(self, handle, value):
        self.calls.append(("setmode", value))
        self.mode = value
        return 1


def test_the_job_is_created_limited_to_kill_on_close_and_given_the_process():
    k = FakeKernel32()
    proc = NS(_handle=99)
    job = proctree.start(proc, kernel32=k)
    assert job is not None
    assert k.calls[0] == "create" and k.calls[1] == ("set", proctree.JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, True)
    assert k.calls[2] == ("assign", 99)
    assert job.terminate() is True and ("terminate", 1) in k.calls
    job.close()
    assert k.calls[-1] == "close" and job.handle is None


@pytest.mark.parametrize("fail", ["create", "set", "assign"])
def test_any_failure_creating_the_job_means_no_job_not_a_crash(fail):
    k = FakeKernel32(fail=fail)
    assert proctree.start(NS(_handle=1), kernel32=k) is None
    if fail != "create":
        assert "close" in k.calls                      # a half-made job does not leak its handle


def test_no_job_outside_windows_unless_a_kernel_is_injected():
    assert proctree.start(NS(_handle=1)) is None if os.name != "nt" else True


# ---------------- the console must keep delivering Ctrl+C ----------------

def test_the_guard_puts_ctrl_c_back_when_an_agent_switches_it_off_and_restores_the_console():
    k = FakeKernel32()
    k.mode = 0x0007                                     # processed input on, as we found it
    with proctree.ConsoleGuard(kernel32=k, enabled=True) as guard:
        assert guard.tick() is False                    # nothing to repair
        k.mode = 0x0004                                 # the agent turned processed input off
        assert guard.tick() is True and k.mode & proctree.ENABLE_PROCESSED_INPUT
        assert guard.repairs == 1
    assert k.mode == 0x0007                             # left as it was found


def test_the_guard_does_nothing_when_stdin_is_not_a_console():
    k = FakeKernel32(fail="notconsole")
    with proctree.ConsoleGuard(kernel32=k, enabled=True) as guard:
        assert guard.tick() is False
    assert not any(isinstance(c, tuple) and c[0] == "setmode" for c in k.calls)


def test_the_guard_is_inert_where_it_is_not_enabled():
    with proctree.ConsoleGuard(enabled=False) as guard:
        assert guard.tick() is False


def test_the_guard_swallows_any_ctypes_error():
    class Broken(FakeKernel32):
        def GetConsoleMode(self, *a):
            raise OSError("no console")

    with proctree.ConsoleGuard(kernel32=Broken(), enabled=True) as guard:
        assert guard.tick() is False


def test_execute_checks_the_console_on_every_wait_cycle(tmp_path, monkeypatch):
    ticks = []

    class Counting(proctree.ConsoleGuard):
        def tick(self):
            ticks.append(1)
            return False

    monkeypatch.setattr(proctree, "ConsoleGuard", Counting)
    cfg = Config(root=tmp_path.resolve())
    run_dir = R.run_dir_for(cfg, "rid")
    inv = NS(argv=[sys.executable, "-c", "import time;time.sleep(1.0)"], env=None, stdin=None)
    R.execute(NS(echo_stdout=False), inv, "x", tmp_path, run_dir, 30, progress=False)
    assert len(ticks) >= 3                              # poll is 0.1 s in the tests
