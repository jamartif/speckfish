"""Interactive console: `speckfish` with no arguments in a terminal.

It adds no behaviour of its own. Every `/command` is turned into the argv of the matching subcommand and
run through `cli.main`, so the console and the command line cannot drift apart. What it adds is the
first-run wizard (init + choosing the agent CLI), a status header with the next suggested step, and
multi-line input for the steps that take free text (constitution, specify, ...).

Input is injectable (`input_fn`, `out`) so the console is testable without a TTY. Stdlib only.
"""
from __future__ import annotations

import contextlib
import shlex
import sys
from collections.abc import Callable
from pathlib import Path

from . import __version__
from . import steps as S
from .config import Config, norm
from .harnesses import REGISTRY, detect_order

# steps whose argument is free text: the console offers a multi-line editor when it is left out.
# value = (what to ask, may be left empty)
TEXT_STEPS: dict[str, tuple[str, bool]] = {
    "constitution": ("Principles of the project", True),     # empty = the agent proposes one
    "specify": ("Describe the feature", False),
    "plan": ("Stack and constraints", True),
    "brainstorm": ("The idea to explore", False),
    "go": ("The idea", False),
}

CONFIRM = {"implement"}          # writes code: always confirm in the console
SESSION_ONLY = {"harness", "model"}

HELP = """\
Commands (type / and the name; a bare name works too):
  /constitution   write or update the project principles
  /specify        describe a new feature          /go     idea -> spec -> plan -> tasks -> review
  /plan /tasks /analyze /review /revise /drift /implement
  /next           run the step the current feature needs      /status   where everything stands
  /features /runs /report /show /doctor /config /init
  /harness NAME   change the agent CLI for this session       /model NAME   same, for the model
  /help           this text                                   /exit     leave (Ctrl+D also works)
Anything else you type is treated as an idea (I will ask before spending anything).
Empty line: run the suggested next step.  Ctrl+C stops the running step and returns here.
Free-text steps open a small editor: finish with an empty line, or give @file.md to import a file."""


def is_interactive() -> bool:
    return bool(sys.stdin and sys.stdin.isatty() and sys.stdout and sys.stdout.isatty())


class Shell:
    def __init__(self, root: Path, *, input_fn: Callable[[str], str] = input,
                 out=None, main: Callable[[list[str]], int] | None = None):
        self.root = Path(root).resolve()
        self.input = input_fn
        self.out = out or sys.stdout
        if main is None:
            from .cli import main as cli_main
            main = cli_main
        self._main = main
        self.harness: str | None = None      # session overrides; the saved config is not touched
        self.model: str | None = None
        self.last_code = 0

    # ---------- io ----------
    def say(self, text: str = "") -> None:
        print(text, file=self.out)

    def ask(self, prompt: str) -> str:
        return self.input(prompt)

    def confirm(self, question: str, default: bool = True) -> bool:
        hint = "[S/n]" if default else "[s/N]"
        answer = self.ask(f"{question} {hint} ").strip().lower()
        if not answer:
            return default
        return answer in ("s", "si", "sí", "y", "yes")

    def read_text(self, title: str, may_be_empty: bool) -> str | None:
        """Multi-line editor: lines until an empty one. None = cancelled."""
        self.say(f"{title} (empty line to send, `@file.md` to import"
                 f"{', empty = let the agent propose' if may_be_empty else ''}; /cancel to abort):")
        lines: list[str] = []
        while True:
            try:
                line = self.ask("│ ")
            except EOFError:
                break
            if line.strip() == "/cancel":
                return None
            if not line.strip():
                if lines or may_be_empty:
                    break
                self.say("  (this step needs some text; /cancel to abort)")
                continue
            lines.append(line.rstrip())
        return "\n".join(lines)

    # ---------- project state ----------
    def cfg(self) -> Config:
        return Config.load(self.root)

    def initialised(self) -> bool:
        return (self.root / ".specify").is_dir() and Config.path(self.root).exists()

    def installed_agents(self, cfg: Config) -> list[str]:
        return [name for name in REGISTRY if REGISTRY[name](cfg).binary()]

    def suggestion(self) -> tuple[list[str] | None, str, Path | None]:
        """(argv of the next step | None, why, feature it applies to)."""
        from . import flow
        cfg = self.cfg()
        feat = S.current_feature(cfg)
        if feat is None:
            if not (self.root / ".specify" / "memory" / "constitution.md").exists():
                return ["constitution"], "no constitution yet", None
            return None, "no feature yet: /specify <idea> or /go <idea>", None
        kind, why = flow.next_step(cfg, feat, implement=False)
        if kind is None:
            return None, why, feat
        if kind == "specify":
            return None, why + ": /specify <idea>", feat
        return [kind], why, feat

    def header(self) -> None:
        cfg = self.cfg()
        agent = self.harness or cfg.harness
        if agent == "auto":
            order = detect_order()
            agent = f"auto ({order[0]})" if order else "auto (none installed)"
        argv, why, feat = self.suggestion()
        nxt = f"/{argv[0]}" if argv else "-"
        self.say("")
        self.say(f"┌ {self.root.name} · agent: {agent} · feature: {feat.name if feat else '-'} "
                 f"· next: {nxt} ({why})")

    # ---------- wizard ----------
    def wizard(self) -> bool:
        """First run: init the project and choose the agent. False when the user declines."""
        cfg = self.cfg()
        self.say(f"{self.root} is not initialised (git + Spec Kit + EGM).")
        if not self.confirm("Set it up now?"):
            return False
        agents = self.installed_agents(cfg)
        if not agents:
            self.say("No agent CLI found on PATH (claude, copilot, codex). Install one and run speckfish again.")
            return False
        self.say("Which agent CLI do you want to use?")
        for i, name in enumerate(agents, start=1):
            info = REGISTRY[name](cfg).doctor()
            tag = "verified" if info.get("verified") else "not verified"
            self.say(f"  {i}) {name:<8} {info.get('version') or '?':<24} {tag}")
        choice = agents[0]
        if len(agents) > 1:
            answer = self.ask(f"Number or name [1 = {agents[0]}]: ").strip().lower()
            if answer.isdigit() and 1 <= int(answer) <= len(agents):
                choice = agents[int(answer) - 1]
            elif answer in agents:
                choice = answer
            elif answer:
                self.say(f"  unknown choice {answer!r}, using {agents[0]}")
        language = self.ask(f"Language of the documents [{cfg.language}]: ").strip().lower() or cfg.language
        cfg.harness, cfg.language = choice, language
        cfg.save()
        self.say(f"Initialising with {choice}…")
        try:
            out = S.do_init(cfg, choice)
        except SystemExit as e:
            self.say(f"ERROR: {e}")
            return False
        for action in out["actions"]:
            if "git_init" in action:
                self.say(f"  {'✔' if action['git_init'] else '✘'} git init")
            elif "integration" in action:
                self.say(f"  {'✔' if action.get('specify_init') else '✘'} Spec Kit ({action['integration']})")
            elif "egm" in action:
                self.say(f"  {'✔' if action['egm'] != 'failed' else '✘'} EGM ({action['egm']})")
        ok = all(a.get("specify_init", True) and a.get("egm") != "failed" for a in out["actions"])
        if not ok:
            self.say("Some parts failed: run /doctor to see what is missing.")
        return True

    # ---------- dispatch ----------
    def globals(self) -> list[str]:
        argv = ["--root", str(self.root)]
        if self.harness:
            argv += ["--harness", self.harness]
        if self.model:
            argv += ["--model", self.model]
        return argv

    def run_cli(self, argv: list[str]) -> int:
        """Run a subcommand exactly as the command line would. Never lets an exit or Ctrl+C escape."""
        try:
            code = self._main(self.globals() + argv)
        except KeyboardInterrupt:
            self.say("\nStopped. Back at the prompt.")
            code = 130
        except SystemExit as e:
            # argparse errors (code 2, already printed) and handlers that bail out
            if e.code not in (None, 0, 2):
                self.say(f"ERROR: {e.code}")
            code = e.code if isinstance(e.code, int) else 2
        self.last_code = code
        return code

    def handle(self, line: str) -> bool:
        """One line of input. False = leave the console."""
        text = line.strip()
        if not text:
            return self.run_suggestion()
        if not text.startswith("/"):
            return self.idea(text)
        try:
            parts = shlex.split(text[1:], posix=(sys.platform != "win32"))
        except ValueError as e:
            self.say(f"cannot parse that line: {e}")
            return True
        if not parts:
            self.say(HELP)
            return True
        name, rest = parts[0].lower(), parts[1:]
        if name in ("exit", "quit", "q"):
            return False
        if name in ("help", "h", "?"):
            self.say(HELP)
            return True
        if name in SESSION_ONLY:
            return self.set_session(name, rest)
        if name == "next":
            return self.run_suggestion()
        return self.command(name, rest)

    def set_session(self, name: str, rest: list[str]) -> bool:
        if not rest:
            self.say(f"{name}: {getattr(self, name) or '(the saved config)'}")
            return True
        value = rest[0]
        if name == "harness" and value not in REGISTRY and value != "auto":
            self.say(f"unknown harness {value!r} (claude | copilot | codex | auto)")
            return True
        if name == "harness" and value == "codex":
            self.say("codex runs without sandbox or approvals: it can touch the whole filesystem.")
            if not self.confirm("Use it anyway?", default=False):
                return True
        setattr(self, name, None if value == "auto" else value)
        self.say(f"{name} = {value} (this session only; `speckfish config {name}=…` saves it)")
        return True

    def command(self, name: str, rest: list[str]) -> bool:
        from .cli import HANDLERS
        if name not in HANDLERS or name == "shell":
            self.say(f"unknown command /{name}. Type /help.")
            return True
        if name in TEXT_STEPS:
            options, body = self.split_text(rest)
            if not body:
                title, may_be_empty = TEXT_STEPS[name]
                body = self.read_text(title, may_be_empty)
                if body is None:
                    self.say("cancelled.")
                    return True
            # `--`: a list of principles starts with "- " and must not be read as an option
            rest = options + (["--", body] if body.startswith("-") else [body] if body else [])
        writes_code = name in CONFIRM or (name == "go" and "--implement" in rest)
        if writes_code and "--yes" not in rest and "--dry-run" not in rest:
            self.say(f"/{name} lets the agent write code and run commands in this project "
                     f"without asking (permissions of the {self.harness or 'configured'} agent).")
            if not self.confirm("Continue?", default=False):
                self.say("cancelled.")
                return True
            rest = ["--yes"] + rest          # before any `--`: after it everything is free text
        self.run_cli([name] + rest)
        return True

    VALUE_FLAGS = frozenset({"--stack", "--max-features", "--budget", "--chunk", "--max-chunks",
                             "--run-id", "--feature", "--max-rounds", "--only", "--from-feature"})

    @classmethod
    def split_text(cls, rest: list[str]) -> tuple[list[str], str]:
        """(options, free text): the words after the options are ONE argument, as the CLI expects.

        `/specify a task cli` is shlex-split into three tokens, but `specify` takes a single description.
        """
        options: list[str] = []
        words: list[str] = []
        i = 0
        while i < len(rest):
            token = rest[i]
            if token == "--":
                words += rest[i + 1:]
                break
            if token.startswith("--"):
                take = 2 if token in cls.VALUE_FLAGS else 1
                options += rest[i:i + take]
                i += take
                continue
            words.append(token)
            i += 1
        return options, " ".join(words)

    @classmethod
    def has_text(cls, name: str, rest: list[str]) -> bool:
        """Does the line already carry the free text (anything that is not an option)?"""
        return bool(cls.split_text(rest)[1])

    def idea(self, text: str) -> bool:
        self.say(f"That is not a command. Treat it as an idea and run /go on it?  {text[:70]!r}")
        if self.confirm("Run /go?"):
            self.run_cli(["go", text])
        return True

    def run_suggestion(self) -> bool:
        argv, why, feat = self.suggestion()
        if argv is None:
            self.say(why)
            return True
        self.say(f"Next: /{argv[0]} — {why}")
        if not self.confirm(f"Run /{argv[0]}?"):
            return True
        if feat is not None:
            with contextlib.suppress(OSError):
                S.set_active_feature(self.cfg(), feat)       # steps follow the active feature
        return self.command(argv[0], argv[1:])

    # ---------- main loop ----------
    def run(self) -> int:
        self.say(f"🐟 speckfish {__version__} · {norm(self.root)}")
        if not self.initialised() and not self.wizard():
            return 0 if self.initialised() else 1
        self.say("Type /help for the commands, or an empty line to run the suggested step.")
        while True:
            self.header()
            try:
                line = self.ask("speckfish › ")
            except EOFError:
                self.say("")
                return 0
            except KeyboardInterrupt:
                self.say("\n(Ctrl+D or /exit to leave)")
                continue
            try:
                if not self.handle(line):
                    return 0
            except EOFError:
                return 0
            except KeyboardInterrupt:
                self.say("\ncancelled.")


def run_shell(root: str | Path = ".") -> int:
    return Shell(Path(root)).run()
