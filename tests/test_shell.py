"""Interactive console: wizard, dispatch to the CLI, free-text editor, confirmations."""
from __future__ import annotations

import io
from pathlib import Path

import pytest

from speckfish import cli as C
from speckfish import shell as SH
from speckfish import steps as S
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
    c = Console(tmp_path, ["/constitution", "- Python 3.12", "- tests in every PR", "", "", "/exit"])
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
    c = Console(tmp_path, ["/constitution", "", "", "/exit"])
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
    assert "NO sandbox" in c.screen


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
    c = Console(tmp_path, ["", "", "", "", "/exit"])    # + the preview's "Send?" (Enter = send)
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
    c = Console(tmp_path, ["s", "2", "", "es", "/exit"], initialised=False)
    assert c.shell.run() == 0
    assert seen == {"integration": "copilot", "harness": "copilot", "language": "es"}
    assert Config.load(tmp_path).harness == "copilot"
    assert Config.load(tmp_path).fallback == ""


def test_wizard_saves_the_fallback_agent_and_the_cli_uses_it(tmp_path, monkeypatch):
    def fake_init(cfg, integration, **kw):
        (cfg.root / ".specify").mkdir(exist_ok=True)
        cfg.save()
        return {"actions": [{"integration": integration, "specify_init": True}]}

    monkeypatch.setattr(SH.S, "do_init", fake_init)
    monkeypatch.setattr(SH.Shell, "installed_agents", lambda self, cfg: ["claude", "copilot", "codex"])
    c = Console(tmp_path, ["s", "claude", "codex", "", "/exit"], initialised=False)
    c.shell.run()
    cfg = Config.load(tmp_path)
    assert (cfg.harness, cfg.fallback) == ("claude", "codex")
    seen = {}
    monkeypatch.setattr(C, "HANDLERS", {"status": lambda run: seen.setdefault("f", run.args.fallback) and 0})
    C.main(["--root", str(tmp_path), "status"])
    assert seen["f"] == "codex"


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


# ---- menu, help, completion: all derived from the CLI ----

def test_the_catalog_and_help_come_from_the_parser_and_include_every_visible_command():
    names = [n for n, _ in SH.command_catalog()]
    for must in ("constitution", "specify", "go", "plan", "review", "implement", "status", "feature",
                 "harness", "exit"):
        assert must in names
    assert not {"shell", "editors", "pipeline"} & set(names)
    assert names.index("constitution") < names.index("specify") < names.index("plan")
    help_ = SH.help_text()
    assert "/constitution" in help_ and "/feature" in help_


def test_a_new_subcommand_appears_in_the_menu_without_touching_the_console(monkeypatch):
    real = C.build_parser

    def with_extra():
        p = real()
        p._subparsers._group_actions[0].add_parser("zap", help="a brand new command")
        return p

    monkeypatch.setattr(C, "build_parser", with_extra)
    assert ("zap", "a brand new command") in SH.command_catalog()


def test_slash_completion_filters_by_prefix_and_stops_after_a_space():
    cat = [("constitution", "principles"), ("clarify", "questions"), ("plan", "plan it")]
    assert [n for n, _ in SH.complete_commands(cat, "/c")] == ["/constitution", "/clarify"]
    assert [n for n, _ in SH.complete_commands(cat, "/")] == ["/constitution", "/clarify", "/plan"]
    assert SH.complete_commands(cat, "/plan x") == []
    assert SH.complete_commands(cat, "plain text") == []


def test_path_completion_stays_inside_the_project_and_offers_text_files(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "brief.md").write_text("x")
    (tmp_path / "docs" / "image.png").write_text("x")
    (tmp_path / "notes.txt").write_text("x")
    (tmp_path / ".git").mkdir()
    assert SH.complete_paths(tmp_path, "") == ["docs/", "notes.txt"]
    assert SH.complete_paths(tmp_path, "docs/") == ["docs/brief.md"]
    assert SH.complete_paths(tmp_path, "docs/b") == ["docs/brief.md"]
    assert SH.complete_paths(tmp_path, "../") == []
    assert SH.complete_paths(tmp_path, "missing/") == []


# ---- preview before sending ----

def test_the_preview_shows_what_will_be_sent_and_edit_goes_back_to_the_editor(tmp_path):
    c = Console(tmp_path, ["/specify", "first draft", "", "e", "second draft", "", "", "/exit"])
    c.shell.run()
    assert c.argvs() == [["specify", "second draft"]]
    assert "first draft" in c.screen and "preview" in c.screen


def test_cancelling_at_the_preview_sends_nothing(tmp_path):
    c = Console(tmp_path, ["/specify", "something", "", "c", "/exit"])
    c.shell.run()
    assert c.argvs() == []
    assert "cancelled" in c.screen


def test_the_preview_of_an_imported_file_names_it_and_its_size(tmp_path):
    (tmp_path / "brief.md").write_text("hello")
    c = Console(tmp_path, ["/specify", "@brief.md", "", "", "/exit"])
    c.shell.run()
    assert "brief.md" in c.screen and "5 bytes" in c.screen
    assert c.argvs() == [["specify", "@brief.md"]]


# ---- /feature ----

def _features(root, *names):
    for n in names:
        d = root / "specs" / n
        d.mkdir(parents=True, exist_ok=True)
        (d / "spec.md").write_text("# s")


def test_feature_by_name_sets_the_active_feature(tmp_path):
    _features(tmp_path, "001-a", "002-b")
    c = Console(tmp_path, ["/feature 002", "/exit"])
    c.shell.run()
    assert "active feature: 002-b" in c.screen
    assert S.active_feature(Config.load(tmp_path)).name == "002-b"


def test_feature_picker_lists_them_and_marks_what_exists(tmp_path):
    _features(tmp_path, "001-a", "002-b")
    (tmp_path / "specs" / "002-b" / "plan.md").write_text("# p")
    c = Console(tmp_path, ["/feature", "2", "/exit"])
    c.shell.run()
    assert "001-a" in c.screen and "[sp-]" in c.screen
    assert S.active_feature(Config.load(tmp_path)).name == "002-b"


def test_feature_with_none_or_unknown_says_so(tmp_path):
    c = Console(tmp_path, ["/feature", "/feature zzz", "/exit"])
    c.shell.run()
    assert "no features yet" in c.screen and "ERROR: no specs/" in c.screen
    _features(tmp_path, "001-a")
    c = Console(tmp_path, ["/feature zzz", "/exit"])
    c.shell.run()
    assert "feature not found: zzz" in c.screen


# ---- the rich front end (prompt_toolkit, optional) ----

ptk = pytest.importorskip("prompt_toolkit")


@pytest.fixture
def pipe():
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    with create_pipe_input() as inp, create_app_session(input=inp, output=DummyOutput()):
        yield inp


def _ui(tmp_path, inp):
    from prompt_toolkit.output import DummyOutput

    from speckfish.tui import PtkUI
    return PtkUI(tmp_path, input=inp, output=DummyOutput())


def test_ptk_command_line_returns_what_was_typed(tmp_path, pipe):
    ui = _ui(tmp_path, pipe)
    pipe.send_text("/status\r")
    assert ui.command() == "/status"


def test_ptk_editor_sends_on_an_empty_line_and_keeps_the_lines(tmp_path, pipe):
    ui = _ui(tmp_path, pipe)
    pipe.send_text("- one\r- two\r\r")
    assert ui.editor("Principles", False) == "- one\n- two"


def test_ptk_editor_ignores_enter_while_text_is_required(tmp_path, pipe):
    ui = _ui(tmp_path, pipe)
    pipe.send_text("\r\rhello\r\r")
    assert ui.editor("Idea", False) == "hello"


def test_ptk_editor_may_send_nothing_when_empty_is_allowed(tmp_path, pipe):
    ui = _ui(tmp_path, pipe)
    pipe.send_text("\r")
    assert ui.editor("Principles", True) == ""


def test_ptk_editor_starts_from_the_text_being_edited_and_cancels(tmp_path, pipe):
    ui = _ui(tmp_path, pipe)
    pipe.send_text("\r\r")                       # Enter on the end of "draft" adds a line, then sends
    assert ui.editor("Idea", False, initial="draft") == "draft"
    pipe.send_text("/cancel\r\r")
    assert ui.editor("Idea", False) is None


def test_ptk_editor_sends_a_lone_file_import_in_one_enter(tmp_path, pipe):
    ui = _ui(tmp_path, pipe)
    pipe.send_text("@brief.md\r")
    assert ui.editor("Idea", False) == "@brief.md"


def test_ptk_picker_moves_with_the_arrows_and_backs_out_with_escape(tmp_path, pipe):
    ui = _ui(tmp_path, pipe)
    opts = [("a", "A"), ("b", "B"), ("c", "C")]
    pipe.send_text("\x1b[B\x1b[B\r")             # down, down, Enter
    assert ui.pick("Pick", opts) == "c"
    pipe.send_text("\r")
    assert ui.pick("Pick", opts, default=1) == "b"
    pipe.send_text("\x1b")
    assert ui.pick("Pick", opts) is None


def test_ptk_completer_offers_commands_and_project_files(tmp_path, pipe):
    from prompt_toolkit.completion import CompleteEvent
    from prompt_toolkit.document import Document
    (tmp_path / "brief.md").write_text("x")
    ui = _ui(tmp_path, pipe)
    ui.bind(SimpleShell([("constitution", "principles"), ("plan", "plan it")]))
    got = [c.text for c in ui._completer.get_completions(Document("/co"), CompleteEvent())]
    assert got == ["/constitution"]
    got = [c.text for c in ui._completer.get_completions(Document("/specify @br"), CompleteEvent())]
    assert got == ["@brief.md"]


class SimpleShell:
    def __init__(self, catalog):
        self.catalog = catalog


def test_the_shell_uses_the_rich_ui_for_the_prompt_the_editor_and_the_toolbar(tmp_path):
    class FakeUI:
        def __init__(self):
            self.lines = iter(["/constitution", "/exit"])
            self.toolbar = []

        def set_toolbar(self, text):
            self.toolbar.append(text)

        def command(self):
            return next(self.lines)

        def editor(self, title, may_be_empty, initial=""):
            return "- be kind"

        def pick(self, *a, **k):
            return None

    ui = FakeUI()
    c = Console(tmp_path, ["", ""], initialised=True)       # "" = accept the preview
    c.shell.ui = ui
    c.shell.run()
    assert c.argvs() == [["constitution", "--", "- be kind"]]
    assert ui.toolbar and "next: /constitution" in ui.toolbar[0]
    assert "┌" not in c.screen                                # the toolbar replaces the printed header


def test_confirmations_are_asked_in_english_but_still_accept_s_and_si(tmp_path):
    asked = []
    answers = iter(["y", "", "n", "s", "sí", "yes", ""])
    c = Console(tmp_path, [])
    c.shell.input = lambda prompt="": (asked.append(prompt), next(answers))[1]
    assert c.shell.confirm("Go?") is True
    assert c.shell.confirm("Go?") is True                       # Enter takes the default (yes)
    assert c.shell.confirm("Go?") is False
    assert c.shell.confirm("Go?", default=False) is True        # "s"
    assert c.shell.confirm("Go?", default=False) is True        # "sí"
    assert c.shell.confirm("Go?", default=False) is True        # "yes"
    assert c.shell.confirm("Go?", default=False) is False       # Enter takes the default (no)
    assert asked[0].endswith("[Y/n] ") and asked[-1].endswith("[y/N] ")
    assert not any("S/n" in a or "s/N" in a for a in asked)
