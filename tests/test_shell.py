"""Interactive console: wizard, dispatch to the CLI, free-text editor, confirmations."""
from __future__ import annotations

import io
from pathlib import Path

import pytest

from speckfish import cli as C
from speckfish import shell as SH
from speckfish.config import Config


class Console:
    """Scripted keyboard + captured screen + a recorder in place of `cli.main`."""

    def __init__(self, tmp_path: Path, lines: list[str], initialised: bool = True):
        self.calls: list[list[str]] = []
        self.out = io.StringIO()
        self.lines = iter(lines)
        self.code = 0
        if initialised:
            (tmp_path / ".specify").mkdir(exist_ok=True)
            Config(root=tmp_path.resolve()).save()
        self.shell = SH.Shell(tmp_path, input_fn=self._input, out=self.out, main=self._main)

    def _input(self, prompt: str = "") -> str:
        try:
            line = next(self.lines)
        except StopIteration:
            raise EOFError from None
        if isinstance(line, BaseException):
            raise line
        return line

    def _main(self, argv: list[str]) -> int:
        self.calls.append(argv)
        return self.code

    @property
    def screen(self) -> str:
        return self.out.getvalue()

    def argvs(self) -> list[list[str]]:
        """What reached the CLI, without the global --root/--harness/--model prefix."""
        out = []
        for argv in self.calls:
            i = 0
            while i < len(argv) and argv[i] in ("--root", "--harness", "--model"):
                i += 2
            out.append(argv[i:])
        return out


def test_a_slash_command_runs_the_matching_subcommand(tmp_path):
    c = Console(tmp_path, ["/status", "/exit"])
    assert c.shell.run() == 0
    assert c.argvs() == [["status"]]


def test_arguments_and_flags_pass_through(tmp_path):
    c = Console(tmp_path, ["/review --stage uninterrupted", "/exit"])
    c.shell.run()
    assert c.argvs() == [["review", "--stage", "uninterrupted"]]


def test_constitution_opens_an_editor_and_protects_a_leading_dash(tmp_path):
    c = Console(tmp_path, ["/constitution", "- Python 3.12", "- tests in every PR", "", "/exit"])
    c.shell.run()
    assert c.argvs() == [["constitution", "--", "- Python 3.12\n- tests in every PR"]]


def test_a_description_with_options_keeps_the_words_together(tmp_path):
    c = Console(tmp_path, ["/go --stack stdlib a task cli", "/exit"])
    c.shell.run()
    assert c.argvs() == [["go", "--stack", "stdlib", "a task cli"]]


def test_the_constitution_text_survives_the_real_parser(tmp_path):
    args = C.build_parser().parse_args(C.normalize_argv(["constitution", "--", "- a\n- b"]))
    assert args.args == "- a\n- b"


def test_an_empty_constitution_lets_the_agent_propose_one(tmp_path):
    c = Console(tmp_path, ["/constitution", "", "/exit"])
    c.shell.run()
    assert c.argvs() == [["constitution"]]


def test_specify_insists_on_some_text_and_can_be_cancelled(tmp_path):
    c = Console(tmp_path, ["/specify", "", "/cancel", "/exit"])
    c.shell.run()
    assert c.argvs() == []
    assert "needs some text" in c.screen and "cancelled" in c.screen


def test_text_on_the_line_skips_the_editor(tmp_path):
    c = Console(tmp_path, ["/specify a task cli", "/exit"])
    c.shell.run()
    assert c.argvs() == [["specify", "a task cli"]]


def test_an_option_is_not_mistaken_for_the_text(tmp_path):
    assert SH.Shell.has_text("go", ["--stack", "stdlib"]) is False
    assert SH.Shell.has_text("go", ["--stack", "stdlib", "an idea"]) is True
    assert SH.Shell.split_text(["--", "- a"]) == ([], "- a")


def test_implement_needs_confirmation_and_then_adds_yes(tmp_path):
    c = Console(tmp_path, ["/implement", "n", "/implement", "s", "/exit"])
    c.shell.run()
    assert c.argvs() == [["implement", "--yes"]]


def test_go_with_implement_is_confirmed_too(tmp_path):
    c = Console(tmp_path, ["/go an idea --implement", "n", "/exit"])
    c.shell.run()
    assert c.argvs() == []


def test_free_text_is_offered_as_go_and_not_run_without_consent(tmp_path):
    c = Console(tmp_path, ["a task cli", "n", "another idea", "s", "/exit"])
    c.shell.run()
    assert c.argvs() == [["go", "another idea"]]


def test_session_harness_and_model_reach_every_call_without_saving(tmp_path):
    c = Console(tmp_path, ["/harness claude", "/model opus", "/status", "/exit"])
    c.shell.run()
    assert c.calls[0][:6] == ["--root", str(tmp_path.resolve()), "--harness", "claude", "--model", "opus"]
    assert Config.load(tmp_path).harness == "auto"


def test_codex_asks_before_it_is_selected(tmp_path):
    c = Console(tmp_path, ["/harness codex", "n", "/status", "/exit"])
    c.shell.run()
    assert "--harness" not in c.calls[0]
    assert "without sandbox" in c.screen


def test_unknown_commands_and_bad_harness_are_reported(tmp_path):
    c = Console(tmp_path, ["/nope", "/harness gemini", "/exit"])
    c.shell.run()
    assert c.calls == []
    assert "unknown command /nope" in c.screen and "unknown harness" in c.screen


def test_ctrl_c_during_a_step_returns_to_the_prompt(tmp_path):
    c = Console(tmp_path, ["/review", "/status", "/exit"])
    boom = [True]
    real = c._main

    def main(argv):
        if boom[0]:
            boom[0] = False
            raise KeyboardInterrupt
        return real(argv)

    c.shell._main = main
    assert c.shell.run() == 0
    assert c.argvs() == [["status"]]
    assert "Back at the prompt" in c.screen


def test_an_argparse_exit_does_not_close_the_console(tmp_path):
    c = Console(tmp_path, ["/review --bogus", "/exit"])

    def main(argv):
        raise SystemExit(2)

    c.shell._main = main
    assert c.shell.run() == 0


def test_the_real_cli_handles_the_commands_the_console_offers(tmp_path):
    parser = C.build_parser()
    for name in ("constitution", "specify", "plan", "tasks", "review", "drift", "implement", "go",
                 "next", "revise", "status", "features", "runs", "report", "show", "doctor",
                 "config", "init", "shell"):
        assert name in C.HANDLERS and name in parser._subparsers._group_actions[0].choices


def test_ctrl_d_leaves_and_header_shows_the_next_step(tmp_path):
    c = Console(tmp_path, [])
    assert c.shell.run() == 0
    assert "next: /constitution" in c.screen


def test_enter_runs_the_suggested_step_after_confirmation(tmp_path):
    # Enter -> "Run /constitution?" (Enter = yes) -> editor (empty line = let the agent propose)
    c = Console(tmp_path, ["", "", "", "/exit"])
    c.shell.run()
    assert c.argvs() == [["constitution"]]


def test_declining_the_suggestion_runs_nothing(tmp_path):
    c = Console(tmp_path, ["", "n", "/exit"])
    c.shell.run()
    assert c.argvs() == []


def test_enter_with_nothing_to_suggest_says_why(tmp_path):
    (tmp_path / ".specify" / "memory").mkdir(parents=True)
    (tmp_path / ".specify" / "memory" / "constitution.md").write_text("x")
    c = Console(tmp_path, ["", "/exit"], initialised=True)
    c.shell.run()
    assert c.argvs() == []
    assert "/specify <idea>" in c.screen


# ---- wizard ----

def test_declining_the_wizard_leaves_the_project_alone(tmp_path):
    c = Console(tmp_path, ["n"], initialised=False)
    assert c.shell.run() == 1
    assert not (tmp_path / ".specify").exists()


def test_wizard_chooses_the_agent_and_runs_init(tmp_path, monkeypatch):
    seen = {}

    def fake_init(cfg, integration, **kw):
        seen["integration"], seen["harness"], seen["language"] = integration, cfg.harness, cfg.language
        (cfg.root / ".specify").mkdir(exist_ok=True)
        cfg.save()
        return {"actions": [{"git_init": True}, {"integration": integration, "specify_init": True},
                            {"egm": "cloned"}]}

    monkeypatch.setattr(SH.S, "do_init", fake_init)
    monkeypatch.setattr(SH.Shell, "installed_agents", lambda self, cfg: ["claude", "copilot"])
    c = Console(tmp_path, ["s", "2", "es", "/exit"], initialised=False)
    assert c.shell.run() == 0
    assert seen == {"integration": "copilot", "harness": "copilot", "language": "es"}
    assert Config.load(tmp_path).harness == "copilot"


def test_wizard_stops_when_no_agent_cli_is_installed(tmp_path, monkeypatch):
    monkeypatch.setattr(SH.Shell, "installed_agents", lambda self, cfg: [])
    c = Console(tmp_path, ["s"], initialised=False)
    assert c.shell.run() == 1
    assert "No agent CLI" in c.screen


# ---- entry point ----

def test_no_arguments_without_a_terminal_keeps_the_usage_error(monkeypatch):
    monkeypatch.setattr(SH, "is_interactive", lambda: False)
    with pytest.raises(SystemExit) as e:
        C.main([])
    assert e.value.code == 2


def test_no_arguments_in_a_terminal_opens_the_console(monkeypatch):
    opened = []
    monkeypatch.setattr(SH, "is_interactive", lambda: True)
    monkeypatch.setattr(SH, "run_shell", lambda root=".": opened.append(root) or 0)
    assert C.main([]) == 0
    assert opened == ["."]


def test_yes_goes_before_the_free_text(tmp_path):
    c = Console(tmp_path, ["/go --implement - an idea", "s", "/exit"])
    c.shell.run()
    assert c.argvs() == [["go", "--yes", "--implement", "--", "- an idea"]]
