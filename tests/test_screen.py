"""Full-screen console: the alternate screen is entered, always given back, and long output is paged."""
from __future__ import annotations

import io
import os
import select
import signal
import sys
import time

import pytest

from speckfish import cli as C
from speckfish import screen as SC
from speckfish import shell as SH
from speckfish import style
from speckfish.config import Config
from speckfish.screen import CLEAR, ENTER, LEAVE, AltScreen, page


class TTY(io.StringIO):
    def isatty(self):
        return True


@pytest.fixture
def vt(monkeypatch):
    monkeypatch.setattr(style, "_windows_vt", lambda: True)


# ---------------- the alternate screen ----------------

def test_it_enters_clears_and_leaves_exactly_once(vt):
    buf = TTY()
    screen = AltScreen(buf)
    assert screen.enter() is True and screen.active
    assert buf.getvalue().startswith(ENTER) and "speckfish" in buf.getvalue()
    screen.enter()                                          # entering twice does nothing more
    assert buf.getvalue().count(ENTER) == 1
    screen.leave()
    screen.leave()
    assert buf.getvalue().count(LEAVE) == 1 and not screen.active


def test_it_does_nothing_on_a_pipe_or_a_dumb_terminal(vt, monkeypatch):
    buf = io.StringIO()
    screen = AltScreen(buf)
    assert screen.possible is False and screen.enter() is False and buf.getvalue() == ""
    monkeypatch.setenv("TERM", "dumb")
    assert AltScreen(TTY()).possible is False


def test_no_color_does_not_switch_full_screen_off(vt, monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    assert AltScreen(TTY()).possible is True and style.supports_color(TTY()) is False


def test_the_screen_is_given_back_even_when_the_body_raises(vt):
    buf = TTY()
    with pytest.raises(RuntimeError), AltScreen(buf):
        raise RuntimeError("boom")
    assert buf.getvalue().endswith(LEAVE)


def test_a_broken_stream_cannot_break_the_exit_path(vt):
    class Broken(TTY):
        def write(self, s):
            if self.closed_flag:
                raise ValueError("closed")
            return super().write(s)
        closed_flag = False

    stream = Broken()
    screen = AltScreen(stream)
    screen.enter()
    stream.closed_flag = True
    screen.leave()                                          # must not raise
    assert not screen.active


def test_clear_only_works_while_active(vt):
    buf = TTY()
    screen = AltScreen(buf)
    screen.clear()
    assert buf.getvalue() == ""
    screen.enter()
    screen.clear()
    assert buf.getvalue().endswith(CLEAR)
    screen.leave()


@pytest.mark.skipif(os.name == "nt", reason="POSIX signals")
def test_signal_handlers_are_installed_and_put_back(vt):
    before = signal.getsignal(signal.SIGTERM)
    screen = AltScreen(TTY())
    screen.enter()
    assert signal.getsignal(signal.SIGTERM) == screen._on_signal
    screen.leave()
    assert signal.getsignal(signal.SIGTERM) == before


def test_wanted_follows_the_environment_over_the_config(monkeypatch):
    monkeypatch.delenv("SPECKFISH_FULLSCREEN", raising=False)
    assert SC.wanted(True) is True and SC.wanted(False) is False
    for off in ("0", "false", "No", "off"):
        monkeypatch.setenv("SPECKFISH_FULLSCREEN", off)
        assert SC.wanted(True) is False
    monkeypatch.setenv("SPECKFISH_FULLSCREEN", "1")
    assert SC.wanted(False) is True


# ---------------- the pager ----------------

def _page(text, answers, height=14):
    out, asked = [], []
    feed = iter(answers)
    page(text, out.append, lambda p: (asked.append(p), next(feed))[1], height=height)
    return "".join(out), asked


def test_short_text_is_printed_whole_without_asking():
    out, asked = _page("a\nb\nc", [])
    assert out == "a\nb\nc\n" and asked == []


def test_long_text_is_paged_and_enter_goes_on():
    text = "\n".join(f"line {i}" for i in range(25))
    out, asked = _page(text, ["", ""], height=14)               # 10 lines a page
    assert out.splitlines() == [f"line {i}" for i in range(25)]
    assert len(asked) == 2 and "more" in asked[0] and "10/25" in asked[0]


def test_q_skips_the_rest_and_says_where_it_is_on_disk():
    text = "\n".join(f"line {i}" for i in range(40))
    out, asked = _page(text, ["q"], height=14)
    assert "line 9" in out and "line 10" not in out
    assert "30 more line(s) skipped" in out and ".speckfish/" in out and len(asked) == 1


def test_eof_or_ctrl_c_while_paging_just_stops():
    text = "\n".join(str(i) for i in range(40))
    for exc in (EOFError, KeyboardInterrupt):
        out = []

        def ask(_p, exc=exc):
            raise exc

        page(text, out.append, ask, height=14)
        assert "9" in "".join(out) and "39" not in "".join(out).split()


# ---------------- inside the console ----------------

class FakeScreen:
    def __init__(self):
        self.active = True
        self.cleared = 0

    def clear(self):
        self.cleared += 1


def _shell(tmp_path, lines, main=None, screen=None, tip=""):
    (tmp_path / ".specify").mkdir(exist_ok=True)
    Config(root=tmp_path.resolve()).save()
    out, it = io.StringIO(), iter(lines)
    asked = []
    shell = SH.Shell(tmp_path, input_fn=lambda p="": (asked.append(p), next(it))[1], out=out,
                     main=main or (lambda argv: 0), screen=screen, tip=tip)
    return shell, out, asked


def test_a_command_result_is_shown_after_it_ran_and_keeps_its_colour(tmp_path, capsys):
    seen = {}

    def main(argv):
        seen["tty"] = sys.stdout.isatty()
        C.emit({}, False, "OK  review  ·  3s")
        return 0

    shell, out, _ = _shell(tmp_path, ["/status", "/exit"], main=main, screen=FakeScreen())
    shell.run()
    assert seen["tty"] is True                                  # so emit() may colour it
    assert "OK  review  ·  3s" in out.getvalue()
    assert "OK  review" not in capsys.readouterr().out          # it went through the console, not around it


def test_a_long_result_is_paged_on_the_alternate_screen(tmp_path):
    def main(argv):
        print("\n".join(f"row {i}" for i in range(60)))
        return 0

    shell, out, asked = _shell(tmp_path, ["/status", "q", "/exit"], main=main, screen=FakeScreen())
    shell.run()
    assert any("Enter: more" in p for p in asked)
    assert "row 0" in out.getvalue() and "row 59" not in out.getvalue()


def test_without_the_alternate_screen_nothing_is_captured_or_paged(tmp_path, capsys):
    def main(argv):
        print("\n".join(f"row {i}" for i in range(60)))
        return 0

    shell, _out, asked = _shell(tmp_path, ["/status", "/exit"], main=main, screen=None)
    shell.run()
    assert "row 59" in capsys.readouterr().out and not any("Enter: more" in p for p in asked)


def test_help_is_paged_when_it_does_not_fit(tmp_path, monkeypatch):
    monkeypatch.setattr(SC.shutil, "get_terminal_size", lambda fallback=None: type("S", (), {"lines": 12, "columns": 80})())
    shell, _out, asked = _shell(tmp_path, ["/help", "q", "/exit"], screen=FakeScreen())
    shell.run()
    assert any("Enter: more" in p for p in asked)


def test_clear_clears_the_screen_and_draws_the_banner_again(tmp_path):
    fake = FakeScreen()
    shell, out, _ = _shell(tmp_path, ["/clear", "/exit"], screen=fake)
    shell.run()
    assert fake.cleared == 1 and out.getvalue().count("🐟 speckfish") == 2


def test_the_tip_about_the_tui_extra_is_part_of_the_banner(tmp_path):
    shell, out, _ = _shell(tmp_path, ["/exit"], tip="tip: pip install 'speckfish[tui]'")
    shell.run()
    assert "tip: pip install" in out.getvalue()


# ---------------- wiring ----------------

def test_run_shell_wraps_the_console_in_the_alternate_screen(tmp_path, vt, monkeypatch):
    buf = TTY()
    monkeypatch.setattr(sys, "stdout", buf)
    monkeypatch.setattr(SH, "is_interactive", lambda: True)
    monkeypatch.setattr(SC.sys, "stdout", buf)
    from speckfish import tui
    monkeypatch.setattr(tui, "available", lambda: False)
    ran = []
    monkeypatch.setattr(SH.Shell, "run", lambda self: ran.append(self.fullscreen) or 0)
    assert SH.run_shell(tmp_path) == 0
    assert ran == [True]
    assert buf.getvalue().startswith(ENTER) and buf.getvalue().endswith(LEAVE)


@pytest.mark.parametrize("how", ["env", "config"])
def test_it_can_be_turned_off(tmp_path, vt, monkeypatch, how):
    buf = TTY()
    monkeypatch.setattr(sys, "stdout", buf)
    monkeypatch.setattr(SH, "is_interactive", lambda: True)
    from speckfish import tui
    monkeypatch.setattr(tui, "available", lambda: False)
    if how == "env":
        monkeypatch.setenv("SPECKFISH_FULLSCREEN", "0")
    else:
        cfg = Config(root=tmp_path.resolve())
        cfg.fullscreen = False
        cfg.save()
    ran = []
    monkeypatch.setattr(SH.Shell, "run", lambda self: ran.append(self.fullscreen) or 0)
    SH.run_shell(tmp_path)
    assert ran == [False] and ENTER not in buf.getvalue()


def test_fullscreen_is_a_validated_config_key(tmp_path):
    assert C.main(["--root", str(tmp_path), "config", "fullscreen=sometimes"]) == C.EXIT_ENV
    assert C.main(["--root", str(tmp_path), "config", "fullscreen=false"]) == C.EXIT_OK
    assert Config.load(tmp_path).fullscreen is False
    assert Config.load(tmp_path / "other").fullscreen is True


# ---------------- end to end, in a real pseudo-terminal ----------------

pty = pytest.importorskip("pty") if os.name != "nt" else None


def _spawn(tmp_path):
    (tmp_path / ".specify").mkdir()
    Config(root=tmp_path.resolve()).save()
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path), "TERM": "xterm-256color",
           "SPECKFISH_HOME": str(tmp_path / "home"), "COLUMNS": "100", "LINES": "30"}
    pid, fd = pty.fork()
    if pid == 0:
        os.chdir(tmp_path)
        os.execvpe(sys.executable, [sys.executable, "-m", "speckfish"], env)  # noqa: S606  the test spawns our own CLI
    return pid, fd


def _read(fd, until, timeout=15):
    out = b""
    end = time.time() + timeout
    while time.time() < end and until not in out:
        ready, _, _ = select.select([fd], [], [], 0.2)
        if ready:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                break
            if not chunk:
                break
            out += chunk
    return out


@pytest.mark.skipif(os.name == "nt", reason="needs a POSIX pseudo-terminal")
def test_a_real_terminal_gets_the_alternate_screen_and_gets_it_back_on_exit(tmp_path):
    pid, fd = _spawn(tmp_path)
    started = _read(fd, b"speckfish \xe2\x80\xba")
    assert started.startswith(ENTER.encode()) or ENTER.encode() in started
    os.write(fd, b"/exit\r")
    rest = _read(fd, LEAVE.encode())
    os.waitpid(pid, 0)
    assert LEAVE.encode() in rest


@pytest.mark.skipif(os.name == "nt", reason="needs a POSIX pseudo-terminal")
def test_a_killed_console_still_gives_the_screen_back(tmp_path):
    pid, fd = _spawn(tmp_path)
    started = _read(fd, b"speckfish \xe2\x80\xba")
    assert ENTER.encode() in started
    os.kill(pid, signal.SIGTERM)
    rest = _read(fd, LEAVE.encode())
    os.waitpid(pid, 0)
    assert LEAVE.encode() in rest
