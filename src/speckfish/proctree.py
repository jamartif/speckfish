"""Stopping an agent for real: its whole process tree, checked, and said out loud when it did not work.

An agent CLI is rarely one process (`node` starts the native binary, which starts a shell for every command it
runs), so killing the one we launched can leave the part that is writing your code alive. Three defences, all
for Windows because POSIX has process groups and `killpg`:

- a **job object**: the agent is put in a job set to kill everything in it, so one call ends the whole tree and
  the OS does it too if speckfish itself dies. It needs no `taskkill`, which App Control can block;
- a **fallback chain** (`job -> taskkill /F /T -> kill`) that then *checks* the process is gone, so the caller
  can tell the person the truth instead of pretending;
- a **console guard**: some agents switch the console's input to raw mode, after which Ctrl+C is just another
  key and never reaches us. While a step runs the guard puts `ENABLE_PROCESSED_INPUT` back, and on exit it
  restores the mode it found.

Everything that touches `ctypes` is optional: any failure falls back to the plain behaviour.
"""
from __future__ import annotations

import contextlib
import ctypes
import os
import signal
import subprocess
from typing import Any

ENABLE_PROCESSED_INPUT = 0x0001
STD_INPUT_HANDLE = -10
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9


def _kernel32() -> Any:
    return ctypes.windll.kernel32           # type: ignore[attr-defined,unused-ignore]


# ---------------------------------------------------------------- job object

class Job:
    """A Windows job object that kills every process in it when terminated or closed."""

    def __init__(self, kernel32: Any = None):
        self.k = kernel32 if kernel32 is not None else _kernel32()
        self.handle = None

    def create(self) -> bool:
        from ctypes import wintypes

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class BASIC(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
                        ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER), ("LimitFlags", wintypes.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t), ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", wintypes.DWORD), ("Affinity", ctypes.c_size_t),
                        ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]

        class EXTENDED(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IO_COUNTERS),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        handle = self.k.CreateJobObjectW(None, None)
        if not handle:
            return False
        info = EXTENDED()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = self.k.SetInformationJobObject(handle, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                                            ctypes.byref(info), ctypes.sizeof(info))
        if not ok:
            self.k.CloseHandle(handle)
            return False
        self.handle = handle
        return True

    def add(self, proc: subprocess.Popen) -> bool:
        if not self.handle:
            return False
        return bool(self.k.AssignProcessToJobObject(self.handle, int(proc._handle)))   # type: ignore[attr-defined,unused-ignore]

    def terminate(self) -> bool:
        return bool(self.handle) and bool(self.k.TerminateJobObject(self.handle, 1))

    def close(self) -> None:
        if self.handle:
            with contextlib.suppress(Exception):
                self.k.CloseHandle(self.handle)
            self.handle = None


def start(proc: subprocess.Popen, kernel32: Any = None) -> Job | None:
    """Put a just-launched agent in a kill-on-close job (Windows). None when that is not possible."""
    if os.name != "nt" and kernel32 is None:
        return None
    job: Job | None = None
    with contextlib.suppress(Exception):         # any ctypes failure: carry on without a job
        job = Job(kernel32)
        if job.create() and job.add(proc):
            return job
    if job is not None:
        job.close()
    return None


# ---------------------------------------------------------------- killing, and checking

def _dead(proc: subprocess.Popen, wait: float) -> bool:
    try:
        proc.wait(timeout=wait)
        return True
    except subprocess.TimeoutExpired:
        return False


def kill_tree(proc: subprocess.Popen, job: Job | None = None, wait: float = 8.0,
              windows: bool | None = None) -> tuple[bool, str]:
    """Stop `proc` and everything it started. Returns (it is gone, what happened).

    The answer is verified: a kill that "succeeded" while the process lives is reported as a failure.
    """
    windows = (os.name == "nt") if windows is None else windows
    tried: list[str] = []
    if proc.poll() is not None and job is None:
        return True, "already finished"
    if windows:
        if job is not None:
            tried.append("job object")
            with contextlib.suppress(Exception):
                job.terminate()
            if _dead(proc, 2.0):
                return True, "stopped (job object)"
        tried.append("taskkill /T")
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, timeout=20)
        except Exception as exc:                         # App Control blocking it, a timeout...
            tried[-1] += f" (failed: {exc.__class__.__name__})"
        if _dead(proc, 3.0):
            return True, "stopped (taskkill)"
    else:
        tried.append("process group")                      # POSIX only: Windows takes the branch above
        with contextlib.suppress(Exception):
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)   # type: ignore[attr-defined,unused-ignore]
    tried.append("kill")
    with contextlib.suppress(Exception):
        proc.kill()
    if _dead(proc, wait):
        return True, "stopped" + (" (only the launched process: its children may remain)"
                                  if windows and tried[-1] == "kill" and len(tried) > 1 else "")
    return False, f"still running after: {', '.join(tried)}"


def manual_hint(pid: int, windows: bool | None = None) -> str:
    windows = (os.name == "nt") if windows is None else windows
    return f"taskkill /F /T /PID {pid}" if windows else f"kill -9 -{pid}"


# ---------------------------------------------------------------- the console must keep delivering Ctrl+C

class ConsoleGuard:
    """Keep Ctrl+C working while an agent runs (Windows console), and leave the console as we found it."""

    def __init__(self, kernel32: Any = None, enabled: bool | None = None):
        self.enabled = (os.name == "nt") if enabled is None else enabled
        self.k = kernel32
        self.handle: Any = None
        self.original: int | None = None
        self.repairs = 0

    def _mode(self) -> int | None:
        mode = ctypes.c_uint32()
        if not self.k.GetConsoleMode(self.handle, ctypes.byref(mode)):
            return None                                   # stdin is not a console (a pipe, a file)
        return int(mode.value)

    def __enter__(self) -> ConsoleGuard:
        if not self.enabled:
            return self
        try:
            self.k = self.k if self.k is not None else _kernel32()
            self.handle = self.k.GetStdHandle(STD_INPUT_HANDLE)
            self.original = self._mode()
        except Exception:
            self.original = None
        return self

    def tick(self) -> bool:
        """Call it now and then while the agent runs: re-enable Ctrl+C if something switched it off."""
        if self.original is None:
            return False
        with contextlib.suppress(Exception):
            mode = self._mode()
            if mode is not None and not mode & ENABLE_PROCESSED_INPUT:
                self.k.SetConsoleMode(self.handle, mode | ENABLE_PROCESSED_INPUT)
                self.repairs += 1
                return True
        return False

    def __exit__(self, *_exc: object) -> None:
        if self.original is not None:
            with contextlib.suppress(Exception):
                self.k.SetConsoleMode(self.handle, self.original)
