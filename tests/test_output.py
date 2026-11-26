"""Console output: commands in plain words, milestones only, one live line that never wraps, colour only on a tty."""
from __future__ import annotations

import io
import json
import sys

import pytest

from speckfish import cli as C
from speckfish import progress as P
from speckfish import runner as R
from speckfish import style
from speckfish.config import Config
from speckfish.harnesses import ClaudeHarness
from speckfish.progress import Progress, clean_command, describe_command

ROOT = "C:/Users/javie/Documents/GitHub/zenduty"


class TTY(io.StringIO):
    def isatty(self):
        return True


def _codex(cmd=None, path=None):
    if cmd is not None:
        return json.dumps({"type": "item.completed", "item": {"type": "command_execution", "command": cmd}})
    return json.dumps({"type": "item.completed",
                       "item": {"type": "file_change", "changes": [{"path": path}]}})


# ---------------- commands in plain words (samples from a real Codex run on Windows) ----------------

@pytest.mark.parametrize("command,expected", [
    (r"""powershell.exe -Command "[System.IO.File]::ReadAllText('C:\\Users\\javie\\Documents\\GitHub\\zenduty\\.agents\\skills\\speckit-constitution\\SKILL.md')" """,
     "reading .agents/skills/speckit-constitution/SKILL.md"),
    (r"""Get-Content -Raw -Encoding utf8 \"C:\\Users\\javie\\Documents\\GitHub\\zenduty\\.agents\\skills\\speckit-specify\\SKILL.md\"""",
     "reading .agents/skills/speckit-specify/SKILL.md"),
    (r"""Get-ChildItem -Force .specify; Write-Output '---'; Get-ChildItem .specify/scripts/powershell""",
     "listing .specify"),
    (r"""Get-ChildItem -Force \"C:\\Users\\javie\\Documents\\GitHub\\zenduty\" | Select-Object Name,Mode""",
     "listing ."),
    (r"""powershell -NoProfile -ExecutionPolicy Bypass -File .specify/scripts/powershell/resolve-template.ps1 -Name constitution""",
     "running resolve-template.ps1"),
    (r"""rg --files -g '*.csproj' -g '*.sln' -g 'global.json' -g 'Directory.Build.*' -g 'README*'""",
     "searching the project"),
    (r"""git status --short; git log --oneline -5; git branch -a""", "git status"),
    (r"""$p='.specify/memory/constitution.md'; $c=[System.IO.File]::ReadAllText($p,[System.Text.Encoding]::UTF8); [System.IO.File]::WriteAllText($p,$c)""",
     "writing .specify/memory/constitution.md"),
    (r"""[System.IO.File]::ReadAllText('.specify/memory/constitution.md',[System.Text.Encoding]::UTF8)""",
     "reading .specify/memory/constitution.md"),
    ("dotnet build --no-restore -warnaserror", "running dotnet build"),
    ("npm test -- --watch=false", "running npm test"),
])
def test_a_command_is_described_the_way_a_person_would_say_it(command, expected):
    assert describe_command(command, ROOT) == expected


def test_a_command_with_nothing_recognisable_is_shortened_not_dumped():
    text = describe_command("some-odd-tool " + "x" * 300, ROOT)
    assert text.startswith("running some-odd-tool") and len(text) < 80


@pytest.mark.parametrize("raw", [r"C:\Users\javie\Documents\GitHub\zenduty\.specify\memory\constitution.md",
                                 r"C:\\Users\\javie\\Documents\\GitHub\\zenduty\\.specify\\memory\\constitution.md",
                                 "C:/Users/javie/Documents/GitHub/zenduty/.specify/memory/constitution.md"])
def test_paths_are_shown_relative_to_the_project_whatever_the_slashes(raw):
    p = Progress("x", enabled=True, stream=io.StringIO(), root=r"C:\Users\javie\Documents\GitHub\zenduty")
    assert p.relpath(raw) == ".specify/memory/constitution.md"


def test_json_escaping_and_quote_leftovers_are_removed():
    assert "\\\\" not in clean_command(r"ReadAllText('C:\\Users\\javie\\x.md')")
    assert "'\"'" not in clean_command("""ReadAllText('"'C:\\\\x.md')""")
    assert '\\"' not in clean_command(r"""Get-Content \"a.md\"""")


# ---------------- what stays on screen ----------------

def _run(events, *, verbose=False, tty=True):
    buf = TTY() if tty else io.StringIO()
    p = Progress("constitution", enabled=True, stream=buf, root=ROOT, verbose=verbose, color=False,
                 plain_every=999)
    for e in events:
        p.feed(e)
    return p, buf


SESSION = [
    _codex(r"""[System.IO.File]::ReadAllText('C:\\Users\\javie\\Documents\\GitHub\\zenduty\\.agents\\skills\\x\\SKILL.md')"""),
    _codex("Get-ChildItem -Force .specify"),
    _codex("git status --short"),
    _codex(path="C:\\Users\\javie\\Documents\\GitHub\\zenduty\\.specify\\memory\\constitution.md"),
]


def test_on_a_terminal_only_milestones_stay_on_screen():
    p, buf = _run(SESSION)
    shown = buf.getvalue()
    assert "wrote .specify/memory/constitution.md" in shown
    assert "ReadAllText" not in shown and "Get-ChildItem" not in shown and "git status" not in shown
    assert shown.count("\n") == 1                    # one line for the whole session
    assert "wrote .specify/memory/constitution.md" in p.live_line()    # the live line follows the latest action
    assert (p.commands, p.files) == (3, [".specify/memory/constitution.md"])
    p.stop()


def test_the_live_line_says_what_it_is_doing_in_words():
    p, _ = _run(SESSION[:2])
    line = p.live_line()
    assert "constitution" in line and "listing .specify" in line and "2 actions" in line
    assert "powershell" not in line.lower() and "\\" not in line
    p.stop()


def test_verbose_keeps_every_action_in_plain_words():
    _p, buf = _run(SESSION, verbose=True)
    shown = buf.getvalue()
    for words in ("reading .agents/skills/x/SKILL.md", "listing .specify", "git status",
                  "wrote .specify/memory/constitution.md"):
        assert words in shown
    assert "powershell" not in shown.lower()


def test_without_a_terminal_every_action_is_one_plain_line_for_ci_logs():
    _p, buf = _run(SESSION, tty=False)
    lines = buf.getvalue().strip().splitlines()
    assert len(lines) == 4
    assert lines[1].startswith("[constitution] 00:") and "$ Get-ChildItem -Force .specify" in lines[1]
    assert "wrote .specify/memory/constitution.md" in lines[3]
    assert "\\\\" not in buf.getvalue() and "\r" not in buf.getvalue()


def test_claude_tools_follow_the_same_rules():
    buf = TTY()
    p = Progress("review", enabled=True, stream=buf, root="/p", color=False)
    for ev in (
        {"name": "Read", "input": {"file_path": "/p/specs/001/spec.md"}},
        {"name": "Bash", "input": {"command": "git diff --stat"}},
        {"name": "Write", "input": {"file_path": "/p/out/report.md"}},
        {"name": "Agent", "input": {"subagent_type": "egm:egm-zoom-out"}},
    ):
        p.feed(json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", **ev}]}}))
    shown = buf.getvalue()
    assert "wrote out/report.md" in shown and "subagent egm:egm-zoom-out" in shown
    assert "specs/001/spec.md" not in shown and "git diff" not in shown      # reads/commands: live line only
    assert p.commands == 1 and p.files == ["out/report.md"] and p.subagents == ["egm:egm-zoom-out"]
    p.stop()


# ---------------- the live line never wraps ----------------

@pytest.mark.parametrize("columns", [40, 60, 120])
def test_the_live_line_fits_the_terminal(monkeypatch, columns):
    monkeypatch.setattr(P.shutil, "get_terminal_size", lambda fallback=None: type("S", (), {"columns": columns})())
    p, _ = _run([_codex("Get-ChildItem -Force " + "very-long-folder-name/" * 30)])
    for _ in range(5):
        assert len(p.live_line()) <= columns - 1
    p.stop()


def test_redrawing_writes_no_newline_and_clears_what_was_longer(monkeypatch):
    monkeypatch.setattr(P.shutil, "get_terminal_size", lambda fallback=None: type("S", (), {"columns": 80})())
    buf = TTY()
    p = Progress("plan", enabled=True, stream=buf, color=False)
    p._draw(force=True)
    long_len = p._printed
    p.current = "x"
    p._draw()
    out = buf.getvalue()
    assert "\n" not in out and out.count("\r") == 2
    assert p._printed < long_len                        # the shorter line padded over the longer one
    p.stop()


# ---------------- colour ----------------

def test_colour_is_off_for_pipes_no_color_and_dumb_terminals(monkeypatch):
    assert style.supports_color(io.StringIO()) is False
    monkeypatch.setattr(style, "_windows_vt", lambda: True)
    assert style.supports_color(TTY()) is True
    monkeypatch.setenv("NO_COLOR", "1")
    assert style.supports_color(TTY()) is False
    monkeypatch.delenv("NO_COLOR")
    monkeypatch.setenv("TERM", "dumb")
    assert style.supports_color(TTY()) is False


def test_paint_is_a_no_op_when_off():
    assert style.paint("x", "red", on=False) == "x"
    assert style.paint("x", "red", "bold") == "\x1b[31;1mx\x1b[0m"
    assert style.paint("x") == "x"


def test_summary_colouring_keeps_every_word():
    text = "OK  review  ·  3s\n  BLOCKER  no auth\n  ERROR  boom\n  WARNING  denied\n    r.md   <- start here\n  plain"
    coloured = style.colorize_summary(text)
    assert "\x1b[" in coloured
    stripped = coloured
    for code in ("\x1b[32;1m", "\x1b[31m", "\x1b[33m", "\x1b[36m", "\x1b[0m"):
        stripped = stripped.replace(code, "")
    assert stripped == text
    assert style.colorize_summary("FAILED  x").startswith("\x1b[31;1m")


def test_emit_colours_only_for_a_terminal_and_never_json(monkeypatch, capsys):
    monkeypatch.setattr(style, "supports_color", lambda stream=None: True)
    C.emit({"a": 1}, False, "OK  x")
    assert "\x1b[" in capsys.readouterr().out
    C.emit({"a": 1}, True, "OK  x")
    assert "\x1b[" not in capsys.readouterr().out
    monkeypatch.setattr(style, "supports_color", lambda stream=None: False)
    C.emit({"a": 1}, False, "OK  x")
    assert capsys.readouterr().out == "OK  x\n"


# ---------------- what the agent did, in the result ----------------

def test_the_summary_says_what_the_agent_did():
    line = C._activity_line({"commands": 14, "files": ["a.md", "b.md", "c.md", "d.md"], "subagents": ["x"]})
    assert line == "  agent: 14 commands · wrote a.md, b.md, c.md (+1) · 1 subagent"
    assert C._activity_line({"commands": 1, "files": [], "subagents": []}) == "  agent: 1 command"
    assert C._activity_line({"commands": 0, "files": [], "subagents": []}) == ""
    assert C._activity_line(None) == ""
    res = {"ok": True, "step": "constitution", "duration_s": 96, "activity": {"commands": 3, "files": ["c.md"],
                                                                              "subagents": []}}
    assert "  agent: 3 commands · wrote c.md" in C.summarize(res)
    assert "agent:" not in C.summarize({"ok": True, "step": "x", "duration_s": 1})


def test_a_real_step_records_its_activity(tmp_path):
    cfg = Config(root=tmp_path.resolve())
    events = [
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": "git status"}},
            {"type": "tool_use", "name": "Write", "input": {"file_path": str(tmp_path / "a.md")}}]}},
        {"type": "result", "total_cost_usd": 0.1, "usage": {"input_tokens": 1, "output_tokens": 1}},
    ]

    class Fake(ClaudeHarness):
        def build(self, **kw):
            inv = super().build(**kw)
            code = "import sys\nfor l in sys.argv[1:]: print(l)"
            inv.argv = [sys.executable, "-c", code, *[json.dumps(e) for e in events]]
            return inv

    res = R.run_step(cfg, Fake(cfg), "plan", "p", tmp_path, "rid", None, stream=False, progress=True, kind="plan")
    assert res.activity == {"actions": 2, "commands": 1, "files": ["a.md"], "subagents": []}
    assert res.to_dict()["activity"]["commands"] == 1


def test_activity_does_not_leak_from_one_step_to_the_next(tmp_path):
    P._LAST = {"actions": 9, "commands": 9, "files": [], "subagents": []}
    cfg = Config(root=tmp_path.resolve())

    class Quiet(ClaudeHarness):
        def build(self, **kw):
            inv = super().build(**kw)
            inv.argv = [sys.executable, "-c", "print('hi')"]
            return inv

    res = R.run_step(cfg, Quiet(cfg), "plan", "p", tmp_path, "rid", None, stream=False, progress=False, kind="plan")
    assert not res.activity or res.activity["commands"] == 0


# ---------------- flags and the console ----------------

def test_verbose_flag_and_env_apply_to_that_run_only(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(C, "HANDLERS", {"status": lambda run: seen.append(P.VERBOSE) or 0})
    C.main(["--root", str(tmp_path), "--verbose", "status"])
    C.main(["--root", str(tmp_path), "status"])
    monkeypatch.setenv("SPECKFISH_VERBOSE", "1")
    C.main(["--root", str(tmp_path), "status"])
    assert seen == [True, False, True]


def test_the_console_toggles_verbose_and_leaves_air_after_a_step(tmp_path):
    from speckfish import shell as SH
    (tmp_path / ".specify").mkdir()
    Config(root=tmp_path.resolve()).save()
    calls, out = [], io.StringIO()
    lines = iter(["/verbose", "/status", "/verbose off", "/status", "/exit"])
    shell = SH.Shell(tmp_path, input_fn=lambda p="": next(lines), out=out,
                     main=lambda argv: calls.append(argv) or 0)
    shell.run()
    assert "--verbose" in calls[0] and "--verbose" not in calls[1]
    assert "verbose on" in out.getvalue() and "verbose off" in out.getvalue()
    assert "\n\n┌" in out.getvalue() or "\n\n" in out.getvalue()
