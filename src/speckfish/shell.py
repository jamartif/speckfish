"""Interactive console: `speckfish` with no arguments in a terminal.

It adds no behaviour of its own. Every `/command` is turned into the argv of the matching subcommand and
run through `cli.main`, so the console and the command line cannot drift apart. What it adds is the
first-run wizard (init + choosing the agent CLI), a status header with the next suggested step, and
multi-line input for the steps that take free text (constitution, specify, ...).

The core is stdlib only. With the optional `speckfish[tui]` extra (prompt_toolkit) the same console gets a
`/` menu with autocompletion, `@file` path completion, history, a multi-line editor and a feature picker
(see `tui.py`); without it everything still works with plain `input()`. Input is injectable (`input_fn`,
`ui`, `out`) so the console is testable without a TTY.
"""
from __future__ import annotations

import contextlib
import io
import os
import shlex
import shutil
import sys
import textwrap
from collections.abc import Callable
from pathlib import Path

from . import __version__
from . import steps as S
from .config import Config, norm
from .harnesses import REGISTRY, detect_order
from .screen import AltScreen, page

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

# commands that exist only in the console, with the text the menu shows for them
SESSION_COMMANDS = {
    "next": "Run the step the current feature needs",
    "feature": "Choose the active feature (or /feature NAME)",
    "harness": "Choose the agent CLI: claude, copilot or codex (--save keeps it)",
    "model": "Choose the model the agent uses (--save keeps it)",
    "clear": "Clear the screen",
    "verbose": "Show every action the agent takes (on | off)",
    "help": "Show the commands",
    "exit": "Leave the console (Ctrl+D also works)",
}
# the order people use them in; whatever else the CLI has follows alphabetically
ORDER = ("constitution", "specify", "go", "plan", "tasks", "analyze", "clarify", "checklist", "review",
         "revise", "implement", "converge", "drift", "next", "feature", "status")
HIDDEN = {"shell", "editors", "pipeline"}
# what the menu says: the parser's own help describes files and skills, which reads like internals
FRIENDLY = {
    "constitution": "Write or update the project's principles",
    "specify": "Describe a feature: creates spec.md",
    "go": "Idea to spec, plan, tasks and a reviewed design in one go",
    "plan": "Plan the feature: stack, design, contracts",
    "tasks": "Break the plan into tasks",
    "analyze": "Check that spec, plan and tasks agree",
    "clarify": "List the open questions in the spec",
    "checklist": "Quality checklist for the requirements",
    "review": "Goldfish review of the plan (EGM)",
    "revise": "Fix the review's blockers, then review again",
    "implement": "Write the code, task by task (asks first)",
    "converge": "Add the work the code still lacks to tasks.md",
    "drift": "Does the code still match the design?",
    "status": "Where each feature stands, what is next, what it cost",
    "features": "List the features and the active one",
    "runs": "List past runs",
    "report": "Print the latest review or drift report",
    "show": "Details of a run",
    "doctor": "Check the agent CLI, Spec Kit and EGM",
    "config": "Show or change the project's settings",
    "trust": "Trust this project's EGM checkout",
    "init": "Set up git, Spec Kit and EGM in this folder",
    "brainstorm": "Explore a vague idea before specifying it",
    "draft": "Turn a concepts brief into a design draft",
}
TEXT_EXTENSIONS = (".md", ".markdown", ".txt", ".rst")


def command_catalog() -> list[tuple[str, str]]:
    """[(name, one-line help)] for the menu and /help, read from the CLI's own parser.

    A subcommand added to `cli.build_parser` shows up here with no second list to keep in sync.
    """
    from .cli import build_parser
    action = build_parser()._subparsers._group_actions[0]
    rows = {a.dest: FRIENDLY.get(a.dest) or (a.help or "").strip()
            for a in action._choices_actions if a.dest not in HIDDEN}
    rows.update(SESSION_COMMANDS)
    names = sorted(rows, key=lambda n: (ORDER.index(n) if n in ORDER else len(ORDER), n))
    return [(n, rows[n]) for n in names]


def help_text(catalog: list[tuple[str, str]] | None = None) -> str:
    rows = catalog or command_catalog()
    width = max(len(n) for n, _ in rows) + 1
    lines = ["Commands (type / to open the menu):"]
    lines += [f"  /{n:<{width}} {h}" for n, h in rows]
    lines += ["",
              "Anything that is not a /command is treated as an idea (I ask before spending anything).",
              "Empty line: run the suggested next step.  Ctrl+C stops the running step and returns here.",
              "Free-text steps open an editor: an empty line sends, `@file.md` imports a file,",
              "/cancel aborts.  You see a preview before anything is sent."]
    return "\n".join(lines)


def complete_commands(catalog: list[tuple[str, str]], text: str) -> list[tuple[str, str]]:
    """Menu entries for a line that is still just `/par…` (no space yet): [(/name, help)]."""
    if not text.startswith("/") or " " in text:
        return []
    word = text[1:].lower()
    return [(f"/{n}", h) for n, h in catalog if n.startswith(word)]


def complete_paths(root: Path, fragment: str, limit: int = 40) -> list[str]:
    """Files and folders for `@frag`, relative to the project and never outside it."""
    root = Path(root).resolve()
    dirpart, _, namepart = fragment.rpartition("/")
    base = (root / dirpart).resolve()
    try:
        base.relative_to(root)
    except ValueError:
        return []
    if not base.is_dir():
        return []
    found: list[str] = []
    for entry in sorted(base.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
        name = entry.name
        if name.startswith(".") and not namepart.startswith("."):
            continue
        if name in ("__pycache__", "node_modules") or not name.lower().startswith(namepart.lower()):
            continue
        if entry.is_dir():
            found.append(f"{dirpart + '/' if dirpart else ''}{name}/")
        elif entry.suffix.lower() in TEXT_EXTENSIONS:
            found.append(f"{dirpart + '/' if dirpart else ''}{name}")
        if len(found) >= limit:
            break
    return found


def is_interactive() -> bool:
    return bool(sys.stdin and sys.stdin.isatty() and sys.stdout and sys.stdout.isatty())


class _Capture(io.StringIO):
    """Collects what a command prints so it can be paged; still looks like a terminal (colour stays on)."""

    def isatty(self) -> bool:
        return True


class Shell:
    def __init__(self, root: Path, *, input_fn: Callable[[str], str] = input,
                 out=None, main: Callable[[list[str]], int] | None = None, ui=None,
                 screen: AltScreen | None = None, tip: str = ""):
        self.root = Path(root).resolve()
        self.input = input_fn
        self.ui = ui                          # optional rich front end (tui.PtkUI); None = plain input()
        self._catalog: list[tuple[str, str]] | None = None
        self.screen = screen                  # the alternate screen, when the console runs full screen
        self.setup_summary: list[str] = []    # what the wizard did, shown again on the clean screen
        self.tip = tip
        self.out = out or sys.stdout
        if main is None:
            from .cli import main as cli_main
            main = cli_main
        self._main = main
        self.harness: str | None = None      # session overrides; the saved config is not touched
        self.model: str | None = None
        self.unsafe = False                  # the person accepted an agent with no sandbox this session
        self.verbose = False
        self.last_code = 0

    # ---------- io ----------
    def say(self, text: str = "") -> None:
        print(text, file=self.out)

    def ask(self, prompt: str) -> str:
        return self.input(prompt)

    @property
    def fullscreen(self) -> bool:
        return bool(self.screen and self.screen.active)

    def show(self, text: str) -> None:
        """Print text; on the alternate screen (no scrollback) a long text goes through a pager."""
        if self.fullscreen:
            page(text, write=lambda t: print(t, end="", file=self.out), ask=self.ask)
        else:
            self.say(text)

    def width(self) -> int:
        return max(40, shutil.get_terminal_size((100, 30)).columns - 1)

    def say_wrapped(self, text: str, indent: str = "") -> None:
        """A long line, broken between words to fit the terminal (never in the middle of one)."""
        self.say(textwrap.fill(text, width=self.width(), subsequent_indent=indent))

    def banner(self) -> None:
        self.say(f"🐟 speckfish {__version__} · {norm(self.root)}")
        if self.tip:
            self.say_wrapped(self.tip)

    @property
    def catalog(self) -> list[tuple[str, str]]:
        if self._catalog is None:
            self._catalog = command_catalog()
        return self._catalog

    def pick(self, title: str, options: list[tuple[str, str]], default: int = 0) -> str | None:
        """Choose one of [(value, label)]: arrows with the rich UI, a number or a name otherwise.

        None means the user backed out. An empty answer takes the default.
        """
        if not options:
            return None
        if self.ui is not None:
            return self.ui.pick(title, options, default)
        self.say(title)
        for i, (_value, label) in enumerate(options, start=1):
            self.say(f"  {i}) {label}")
        answer = self.ask(f"Number or name [{default + 1}]: ").strip().lower()
        if not answer:
            return options[default][0]
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return options[int(answer) - 1][0]
        for value, _label in options:
            if value.lower() == answer:
                return value
        self.say(f"  unknown choice {answer!r}")
        return None

    def confirm(self, question: str, default: bool = True) -> bool:
        hint = "[Y/n]" if default else "[y/N]"
        answer = self.ask(f"{question} {hint} ").strip().lower()
        if not answer:
            return default
        return answer in ("s", "si", "sí", "y", "yes")

    def read_text(self, title: str, may_be_empty: bool, initial: str = "") -> str | None:
        """One pass of the multi-line editor. None = cancelled."""
        if self.ui is not None:
            return self.ui.editor(title, may_be_empty, initial)
        self.say(title)
        self.say_wrapped(f"  (empty line sends · `@file.md` imports · /cancel aborts"
                         f"{' · empty = let the agent propose' if may_be_empty else ''})", indent="  ")
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

    def preview(self, body: str) -> None:
        if not body:
            self.say("── preview: (empty) the agent will propose one ──")
            return
        if body.startswith("@") and "\n" not in body:
            target = Path(body[1:].strip().strip('"'))
            target = target if target.is_absolute() else self.root / target
            size = f"{target.stat().st_size} bytes" if target.is_file() else "NOT FOUND"
            self.say(f"── preview: imports {target} ({size}) ──")
            return
        shown = body.splitlines()
        self.say("── preview ──")
        for line in shown[:15]:
            self.say(f"  {line[:110]}")
        if len(shown) > 15:
            self.say(f"  … {len(shown) - 15} more line(s)")
        self.say(f"── {len(body)} characters ──")

    def compose(self, title: str, may_be_empty: bool) -> str | None:
        """Editor + preview: Send / edit again / cancel. None = nothing to send."""
        body = ""
        while True:
            edited = self.read_text(title, may_be_empty, body)
            if edited is None:
                return None
            body = edited
            self.preview(body)
            answer = self.ask("Send? [S]end / [e]dit / [c]ancel: ").strip().lower()
            if answer in ("", "s", "send", "y", "yes", "si", "sí"):
                return body
            if answer in ("c", "cancel", "n", "no"):
                return None
            # anything else (e): open the editor again with what was written

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

    def agent_name(self) -> str:
        """The agent CLI a step would use right now (the session choice, else the saved one, else auto)."""
        name = self.harness or self.cfg().harness
        if name == "auto":
            order = detect_order()
            return order[0] if order else "the agent"
        return name

    def welcome(self) -> None:
        self.say_wrapped("Type /help for the commands, or press Enter to run the suggested step.")
        self.say_wrapped("/harness changes the agent CLI, /model the model, /clear clears the screen.")

    def header_text(self) -> str:
        cfg = self.cfg()
        agent = self.harness or cfg.harness
        if agent == "auto":
            order = detect_order()
            agent = f"auto ({order[0]})" if order else "auto (none installed)"
        argv, why, feat = self.suggestion()
        nxt = f"/{argv[0]}" if argv else "-"
        model = self.model or cfg.model
        return (f"{self.root.name} · agent: {agent}{f' ({model})' if model else ''} · "
                f"feature: {feat.name if feat else '-'} · next: {nxt} ({why})")

    def header(self) -> None:
        text = self.header_text()
        room = self.width() - (0 if self.ui is not None else 2)         # the plain form adds "┌ "
        if len(text) > room:
            text = text[:room - 1] + "…"                 # the toolbar cuts at the edge: do it ourselves
        if self.ui is not None:
            self.ui.set_toolbar(text)         # computed once per prompt: a toolbar redraws on every key
        else:
            self.say("")
            self.say(f"┌ {text}")

    def read_command(self) -> str:
        if self.ui is not None:
            return self.ui.command()
        return self.ask("speckfish › ")

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
        options = []
        for name in agents:
            info = REGISTRY[name](cfg).doctor()
            tag = "verified" if info.get("verified") else "not verified"
            options.append((name, f"{name:<8} {info.get('version') or '?':<24} {tag}"))
        choice = self.pick("Which agent CLI do you want to use?", options) or agents[0]
        if getattr(REGISTRY[choice], "unsafe", False):
            self.say(f"{choice}: {REGISTRY[choice](cfg).permissions('implement')}.")
            if not self.confirm(f"Use {choice} anyway?", default=False):
                self.say("Run the wizard again and choose another agent.")
                return False
            cfg.unsafe = True
        fallback = ""
        if len(agents) > 1:
            others = [(n, label) for n, label in options if n != choice] + [("", "none")]
            fallback = self.pick("Fallback when it runs out of quota or credentials?",
                                 others, default=len(others) - 1) or ""
        language = self.ask(f"Language of the documents [{cfg.language}]: ").strip().lower() or cfg.language
        cfg.harness, cfg.language, cfg.fallback = choice, language, fallback
        cfg.save()
        self.say(f"Initialising with {choice}…")
        try:
            out = S.do_init(cfg, choice)
        except SystemExit as e:
            self.say(f"ERROR: {e}")
            return False
        for action in out["actions"]:
            line = ""
            if "git_init" in action:
                line = f"  {'✔' if action['git_init'] else '✘'} git init"
            elif "integration" in action:
                line = f"  {'✔' if action.get('specify_init') else '✘'} Spec Kit ({action['integration']})"
            elif "egm" in action:
                ref = f" {action['ref']}" if action.get("ref") else ""
                line = f"  {'✔' if action['egm'] != 'failed' else '✘'} EGM ({action['egm']}{ref})"
            if line:
                self.say(line)
                self.setup_summary.append(line)
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
        if self.unsafe:
            argv += ["--unsafe"]
        if self.verbose:
            argv += ["--verbose"]
        return argv

    def run_cli(self, argv: list[str]) -> int:
        """Run a subcommand exactly as the command line would. Never lets an exit or Ctrl+C escape."""
        capture = _Capture() if self.fullscreen else None
        try:
            with contextlib.redirect_stdout(capture) if capture is not None else contextlib.nullcontext():
                code = self._main(self.globals() + argv)
        except KeyboardInterrupt:
            self.say("\nStopped. Back at the prompt.")
            code = 130
        except SystemExit as e:
            # argparse errors (code 2, already printed) and handlers that bail out
            if e.code not in (None, 0, 2):
                self.say(f"ERROR: {e.code}")
            code = e.code if isinstance(e.code, int) else 2
        finally:
            if capture is not None and capture.getvalue():
                self.show(capture.getvalue())        # after the redirect is undone: it may need to ask
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
            self.show(help_text(self.catalog))
            return True
        name, rest = parts[0].lower(), parts[1:]
        if name in ("exit", "quit", "q"):
            return False
        if name in ("help", "h", "?"):
            self.show(help_text(self.catalog))
            return True
        if name == "feature":
            return self.choose_feature(rest)
        if name in SESSION_ONLY:
            return self.set_session(name, rest)
        if name == "clear":
            if self.screen and self.screen.active:
                self.screen.clear()
            self.banner()
            return True
        if name == "verbose":
            self.verbose = (rest[0].lower() not in ("off", "0", "no", "false")) if rest else not self.verbose
            self.say(f"verbose {'on: every action stays on screen' if self.verbose else 'off'} (this session)")
            return True
        if name == "next":
            return self.run_suggestion()
        return self.command(name, rest)

    def choose_feature(self, rest: list[str]) -> bool:
        """/feature NAME, or a list to pick from: the active feature is the one every step works on."""
        from . import flow
        cfg = self.cfg()
        if rest:
            try:
                feat = S.find_feature(cfg, rest[0])
            except SystemExit as e:
                self.say(f"ERROR: {e}")
                return True
        else:
            feats = S.features_with_spec(cfg)
            if not feats:
                self.say("no features yet: /specify <idea> or /go <idea>")
                return True
            active = S.active_feature(cfg)
            options, default = [], 0
            for i, f in enumerate(feats):
                st = flow.feature_state(cfg, f)
                arts = "".join(m if st[k] else "-" for k, m in (("spec", "s"), ("plan", "p"), ("tasks", "t")))
                review = (f"{st['blockers']} blocker(s)" if st["blockers"] is not None
                          else "review stale" if st["review"] else "not reviewed")
                tasks = f"{st['tasks_done']}/{st['tasks_total']}" if st["tasks_total"] else "-"
                options.append((f.name, f"{f.name:<34} [{arts}] tasks {tasks:<6} {review}"))
                if active and active.name == f.name:
                    default = i
            chosen = self.pick("Which feature do you want to work on?", options, default)
            if chosen is None:
                self.say("cancelled.")
                return True
            feat = next(f for f in feats if f.name == chosen)
        S.set_active_feature(cfg, feat)
        self.say(f"active feature: {feat.name}")
        return True

    def set_session(self, name: str, rest: list[str]) -> bool:
        """/harness and /model: a picker with no argument, or the value; `--save` keeps it for next time."""
        save = "--save" in rest
        rest = [r for r in rest if r != "--save"]
        value = rest[0] if rest else self.pick_session_value(name)
        if value is None:
            self.say("cancelled.")
            return True
        if name == "harness":
            return self.use_harness(value, save)
        return self.use_model(value, save)

    def pick_session_value(self, name: str) -> str | None:
        cfg = self.cfg()
        if name == "harness":
            current = self.harness or cfg.harness
            options = [(n, f"{n}{'  (current)' if n == current else ''}"
                        f"{'  (no sandbox)' if getattr(REGISTRY[n], 'unsafe', False) else ''}")
                       for n in self.installed_agents(cfg)]
            options.append(("auto", f"auto: the first installed{'  (current)' if current == 'auto' else ''}"))
            default = next((i for i, (v, _l) in enumerate(options) if v == current), 0)
            return self.pick("Which agent CLI?", options, default)
        agent = self.agent_name()
        models = REGISTRY[agent](cfg).known_models() if agent in REGISTRY else []
        current = self.model or cfg.model
        options = [("default", f"default: the CLI's own choice{'  (current)' if not current else ''}")]
        options += [(m, f"{m}  — {where}{'  (current)' if m == current else ''}") for m, where in models]
        if current and current not in [m for m, _w in models]:
            options.append((current, f"{current}  (current)"))
        options.append(("__other__", "another name…  (any model name the CLI accepts)"))
        default = next((i for i, (v, _l) in enumerate(options) if v == (current or "default")), 0)
        if not models:
            self.say(f"{agent} does not expose a list of its models; pick the default or type a name.")
        chosen = self.pick(f"Which model for {agent}?", options, default)
        if chosen == "__other__":
            chosen = self.ask("Model name: ").strip() or None
        return chosen

    def use_harness(self, value: str, save: bool) -> bool:
        if value not in REGISTRY and value != "auto":
            self.say(f"unknown harness {value!r} (claude | copilot | codex | auto)")
            return True
        unsafe_accepted = False
        if value != "auto" and getattr(REGISTRY[value], "unsafe", False):
            self.say(f"{value}: {REGISTRY[value](self.cfg()).permissions('implement')}.")
            if not self.confirm("Use it anyway?", default=False):
                return True
            self.unsafe = unsafe_accepted = True
        self.harness = None if value == "auto" else value
        where = "this session only"
        if save:
            cfg = self.cfg()
            cfg.harness = value
            if unsafe_accepted:
                cfg.unsafe = True                 # they just confirmed it: do not ask again next time
            cfg.save()
            where = "saved in .speckfish/config.json" + (" (including that you accept it)" if unsafe_accepted else "")
        self.say(f"harness = {value}  ({where}{'' if save else '; add --save to keep it'})")
        self.offer_skills(value)
        return True

    def offer_skills(self, name: str) -> None:
        """Spec Kit installs its skills per agent: a project set up for claude has none for codex."""
        cls = REGISTRY.get(name)
        if (cls is None or not cls.skill_dirs or not (self.root / ".specify").is_dir()
                or os.environ.get("SPECKFISH_SKIP_SKILL_CHECK") == "1"):
            return
        if cls(self.cfg()).has_skill(self.root, "speckit-specify"):
            return
        self.say(f"{name} has no Spec Kit skills in this project (it was set up for another agent), so its "
                 f"steps would fail.")
        if self.confirm(f"Install them now (speckfish init --integration {name})?"):
            self.run_cli(["init", "--integration", name])

    def use_model(self, value: str, save: bool) -> bool:
        self.model = None if value in ("default", "auto") else value
        where = "this session only"
        if save:
            cfg = self.cfg()
            cfg.model = self.model or ""
            cfg.save()
            where = "saved in .speckfish/config.json"
        shown = value if self.model else "default (the CLI's own choice)"
        self.say(f"model = {shown}  ({where}{'' if save else '; add --save to keep it'})")
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
                body = self.compose(title, may_be_empty)
                if body is None:
                    self.say("cancelled.")
                    return True
            # `--`: a list of principles starts with "- " and must not be read as an option
            rest = options + (["--", body] if body.startswith("-") else [body] if body else [])
        writes_code = name in CONFIRM or (name == "go" and "--implement" in rest)
        if writes_code and "--yes" not in rest and "--dry-run" not in rest:
            agent = self.agent_name()
            self.say(f"/{name} lets {agent} write code and run commands in this project without asking.")
            if agent in REGISTRY:
                self.say(f"  {agent} gets {REGISTRY[agent](self.cfg()).permissions('implement')}")
            if not self.confirm("Continue?", default=False):
                self.say("cancelled.")
                return True
            rest = ["--yes", *rest]          # before any `--`: after it everything is free text
        self.run_cli([name, *rest])
        self.say("")                         # air between a step's result and the next prompt
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
            word = rest[i]
            if word == "--":
                words += rest[i + 1:]
                break
            if word.startswith("--"):
                take = 2 if word in cls.VALUE_FLAGS else 1
                options += rest[i:i + take]
                i += take
                continue
            words.append(word)
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
        self.banner()
        if not self.initialised():
            if not self.wizard():
                return 0 if self.initialised() else 1
            if self.fullscreen:                           # the wizard's questions have done their job
                self.screen.clear()
                self.banner()
                for line in self.setup_summary:
                    self.say(line)
        self.welcome()
        while True:
            self.header()
            try:
                line = self.read_command()
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
    from . import screen as screen_mod
    ui = None
    tip = ""
    if is_interactive():
        from . import tui
        if tui.available():
            ui = tui.PtkUI(Path(root).resolve())
        else:
            tip = "tip: `pip install 'speckfish[tui]'` adds the / menu, autocompletion and arrow-key pickers."
    try:
        configured = Config.load(Path(root)).fullscreen
    except SystemExit:
        configured = True                      # a broken config.json is reported by the first command
    screen = AltScreen()
    use_screen = is_interactive() and screen_mod.wanted(configured) and screen.possible
    shell = Shell(Path(root), ui=ui, screen=screen if use_screen else None, tip=tip)
    if ui is not None:
        ui.bind(shell)
    with screen if use_screen else contextlib.nullcontext():
        return shell.run()
