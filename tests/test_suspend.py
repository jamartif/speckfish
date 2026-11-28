"""A suspended computer is not a silent agent: the pause is not counted, not killed for, and reported.

Seen with Codex on Windows: Modern Standby froze everything for 4 h 08 min, and on waking speckfish read the
gap as "the agent wrote nothing for 600s", killed the step, and reported a duration of 248 minutes.
The suspension is simulated by making the wall clock jump forward while a real agent process keeps running.
"""
from __future__ import annotations

import json
import sys
import threading
import time
from types import SimpleNamespace as NS

import pytest

from speckfish import cli as C
from speckfish import progress as P
from speckfish import runner as R
from speckfish.config import Config
from speckfish.harnesses import ClaudeHarness

HELLO = "import time;print('hello',flush=True);time.sleep({sleep});print('bye',flush=True)"


@pytest.fixture
def jump(monkeypatch):
    """`jump(seconds, after)`: the wall clock leaps `seconds` ahead `after` real seconds from now."""
    real = time.time
    offset = [0.0]
    monkeypatch.setattr(time, "time", lambda: real() + offset[0])
    timers = []

    def schedule(seconds, after):
        timer = threading.Timer(after, lambda: offset.__setitem__(0, offset[0] + seconds))
        timer.daemon = True
        timer.start()
        timers.append(timer)

    yield schedule
    for t in timers:
        t.cancel()


def _run(tmp_path, sleep, *, progress=False, stats=None, **kw):
    cfg = Config(root=tmp_path.resolve())
    run_dir = R.run_dir_for(cfg, "rid")
    inv = NS(argv=[sys.executable, "-c", HELLO.format(sleep=sleep)], env=None, stdin=None)
    return R.execute(NS(echo_stdout=False), inv, "implement", tmp_path, run_dir, kw.pop("timeout", 3600),
                     progress=progress, stats=stats, **kw)


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("SPECKFISH_SUSPEND_SECONDS", "5")
    monkeypatch.setenv("SPECKFISH_IDLE_SECONDS", "10")


def test_a_suspension_in_the_middle_of_a_healthy_step_neither_kills_it_nor_counts(tmp_path, env, jump):
    jump(7200, after=1.0)                                  # two hours asleep, then it wakes
    stats: dict = {}
    code, out, _err, duration, timed_out, idle = _run(tmp_path, 2.5, stats=stats)
    assert (code, timed_out, idle) == (0, False, False)    # before: killed on waking as "silent for 10 s"
    assert "bye" in out
    assert 7100 < stats["suspended_s"] < 7300
    assert duration < 8, f"the pause leaked into the duration: {duration:.0f}s"


def test_a_step_that_finishes_while_the_computer_is_suspended_does_not_count_the_pause(tmp_path, env, jump):
    jump(7200, after=0.3)
    stats: dict = {}
    code, out, _err, duration, _timed_out, _idle = _run(tmp_path, 0.6, stats=stats)
    assert code == 0 and "bye" in out
    assert stats["suspended_s"] > 7000 and duration < 8


def test_an_agent_that_stays_silent_after_waking_is_still_stopped_with_a_true_reason(tmp_path, monkeypatch, jump):
    monkeypatch.setenv("SPECKFISH_SUSPEND_SECONDS", "5")
    monkeypatch.setenv("SPECKFISH_IDLE_SECONDS", "2")
    jump(7200, after=0.5)
    stats: dict = {}
    started = time.monotonic()
    _code, _out, _err, duration, timed_out, idle = _run(tmp_path, 600, stats=stats)
    real = time.monotonic() - started
    assert idle is True and timed_out is False
    assert real >= 1.5, "it was killed the moment the computer woke: the silence clock was not reset"
    assert duration < 15 and stats["suspended_s"] > 7000


def test_the_duration_is_taken_before_the_work_of_closing_the_step(tmp_path, monkeypatch):
    """Joining the readers and clearing the status line can block (a stuck console): not time the agent used."""
    monkeypatch.setattr(P.Progress, "stop", lambda self: time.sleep(1.5))
    started = time.monotonic()
    _code, _out, _err, duration, *_ = _run(tmp_path, 0.2, progress=True)
    assert time.monotonic() - started >= 1.5
    assert duration < 1.0, f"the closing work was counted: {duration:.1f}s"


def test_a_normal_run_reports_no_suspension(tmp_path, env):
    stats: dict = {}
    _code, out, *_ = _run(tmp_path, 0.3, stats=stats)
    assert "bye" in out and stats["suspended_s"] == 0.0


def test_a_short_pause_below_the_threshold_is_not_a_suspension(tmp_path, env, jump):
    jump(3, after=0.2)                                     # 3 s < the 5 s threshold
    stats: dict = {}
    _run(tmp_path, 0.8, stats=stats)
    assert stats["suspended_s"] == 0.0


def test_the_threshold_comes_from_the_environment(tmp_path, monkeypatch, jump):
    monkeypatch.setenv("SPECKFISH_SUSPEND_SECONDS", "2")
    monkeypatch.setenv("SPECKFISH_IDLE_SECONDS", "10")
    jump(3, after=0.2)                                     # 3 s > 2 s
    stats: dict = {}
    _run(tmp_path, 0.8, stats=stats)
    assert stats["suspended_s"] > 2


# ---------------- what the person sees ----------------

class Fake(ClaudeHarness):
    def build(self, **kw):
        inv = super().build(**kw)
        inv.argv = [sys.executable, "-c", HELLO.format(sleep=2.0)]
        return inv


def test_the_step_result_the_summary_the_meta_and_the_screen_all_say_so(tmp_path, env, jump, capsys):
    cfg = Config(root=tmp_path.resolve())
    jump(7200, after=0.8)
    res = R.run_step(cfg, Fake(cfg), "implement", "p", tmp_path, "rid", None, stream=False, progress=False,
                     kind="implement")
    assert 7100 < res.suspended_s < 7300 and res.duration_s < 10
    assert res.exit_code == 0 and "wrote nothing" not in res.error
    assert res.to_dict()["suspended_s"] == res.suspended_s
    text = C.summarize(res.to_dict())
    assert "note: the computer was suspended for 2h 00m during this step" in text
    assert "not counted in the time above" in text
    meta = json.loads((cfg.runs_dir / "rid" / "logs" / "implement.meta.json").read_text(encoding="utf-8"))
    assert meta["suspended_s"] == res.suspended_s
    assert "the computer was suspended for 2h 00m; that time is not counted" in capsys.readouterr().err


def test_the_live_line_gets_a_milestone_instead_of_a_raw_stderr_line():
    out = []

    class Meter:
        enabled = True

        def activity(self, text, **kw):
            out.append((text, kw))

    R._say_suspended(Meter(), "implement", 14900)
    assert out == [("warning: the computer was suspended for 4h 08m; that time is not counted",
                    {"milestone": True, "style": "yellow"})]


def test_a_step_without_a_suspension_has_no_note():
    assert "suspended" not in C.summarize({"ok": True, "step": "plan", "duration_s": 5, "suspended_s": 0.0})


@pytest.mark.parametrize("seconds,expected", [(40, "40s"), (95, "1m 35s"), (3600, "1h 00m"), (7200, "2h 00m"),
                                              (14926, "4h 08m"), (0, "0s"), (59.6, "1m 00s")])
def test_format_span(seconds, expected):
    assert R.format_span(seconds) == expected
