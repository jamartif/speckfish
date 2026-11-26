"""Rich front end for the console, only when `prompt_toolkit` is installed (`pip install speckfish[tui]`).

What it adds over plain `input()`:
- a `/` menu that opens as you type, with each command's help text;
- `@file.md` path completion, limited to the project folder;
- history across sessions (`.speckfish/history`) and a status toolbar with the suggested next step;
- a multi-line editor (Enter = new line, an empty line or Esc+Enter sends) that can start from earlier text;
- arrow-key pickers for the agent and the feature.

The completion rules are plain functions in `shell.py` (`complete_commands`, `complete_paths`), so they
are tested without a terminal. This module only wires them into prompt_toolkit and imports it lazily.
"""
from __future__ import annotations

import contextlib
import importlib.util
from pathlib import Path

from .shell import complete_commands, complete_paths


def available() -> bool:
    return importlib.util.find_spec("prompt_toolkit") is not None


class PtkUI:
    """The same three operations the plain console has: a command line, an editor and a picker."""

    def __init__(self, root: Path, *, input=None, output=None, history: bool = True):
        from prompt_toolkit import PromptSession
        from prompt_toolkit.completion import Completer, Completion
        from prompt_toolkit.history import FileHistory, InMemoryHistory

        self.root = Path(root).resolve()
        self._input, self._output = input, output
        self._toolbar = ""
        self._shell = None
        outer = self

        class SpeckfishCompleter(Completer):
            def get_completions(self, document, complete_event):
                text = document.text_before_cursor
                if text.startswith("/") and " " not in text:
                    for name, helptext in complete_commands(outer.catalog(), text):
                        yield Completion(name, start_position=-len(text), display_meta=helptext[:60])
                    return
                word = text.rsplit(None, 1)[-1] if text.strip() and not text.endswith(" ") else ""
                if word.startswith("@"):
                    for path in complete_paths(outer.root, word[1:]):
                        yield Completion(f"@{path}", start_position=-len(word))

        history_obj = InMemoryHistory()
        if history:
            hist_dir = self.root / ".speckfish"
            with contextlib.suppress(OSError):
                hist_dir.mkdir(exist_ok=True)
                history_obj = FileHistory(str(hist_dir / "history"))
        self._completer = SpeckfishCompleter()
        self._session = PromptSession(
            completer=self._completer, complete_while_typing=True, history=history_obj,
            bottom_toolbar=lambda: self._toolbar, input=input, output=output)

    # ---------- wiring ----------
    def bind(self, shell) -> None:
        self._shell = shell

    def catalog(self) -> list[tuple[str, str]]:
        return self._shell.catalog if self._shell is not None else []

    def set_toolbar(self, text: str) -> None:
        self._toolbar = text

    # ---------- the three operations ----------
    def command(self) -> str:
        """The main prompt. EOFError (Ctrl+D) and KeyboardInterrupt (Ctrl+C) reach the caller."""
        return self._session.prompt("speckfish › ")

    def editor(self, title: str, may_be_empty: bool, initial: str = "") -> str | None:
        """Multi-line input. Enter adds a line; Enter on an empty line (or Esc+Enter) sends."""
        from prompt_toolkit import PromptSession
        from prompt_toolkit.key_binding import KeyBindings

        kb = KeyBindings()

        @kb.add("enter")
        def _(event):
            buf = event.current_buffer
            text = buf.text
            if not text.strip():
                if may_be_empty:
                    buf.validate_and_handle()
                return                                  # nothing typed and text is required: ignore
            if text.endswith("\n") or "\n" not in text and text.startswith("@"):
                buf.validate_and_handle()               # empty line after content, or a lone @file
            else:
                buf.insert_text("\n")

        hint = ("empty line sends · @file.md imports · Esc+Enter sends · /cancel aborts"
                + (" · empty = let the agent propose" if may_be_empty else ""))
        print(f"{title}  ({hint})")
        session = PromptSession(multiline=True, key_bindings=kb, completer=self._completer,
                                complete_while_typing=True, input=self._input, output=self._output,
                                prompt_continuation=lambda width, line_no, wrap: "│ ")
        try:
            text = session.prompt("│ ", default=initial)
        except (KeyboardInterrupt, EOFError):
            return None
        text = text.strip()
        return None if text == "/cancel" else text

    def pick(self, title: str, options: list[tuple[str, str]], default: int = 0) -> str | None:
        """Arrow-key list. Enter chooses, Esc/Ctrl+C backs out (None)."""
        from prompt_toolkit.application import Application
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.layout import Layout
        from prompt_toolkit.layout.containers import Window
        from prompt_toolkit.layout.controls import FormattedTextControl

        state = {"i": max(0, min(default, len(options) - 1))}
        kb = KeyBindings()

        @kb.add("up")
        @kb.add("k")
        def _up(event):
            state["i"] = (state["i"] - 1) % len(options)

        @kb.add("down")
        @kb.add("j")
        def _down(event):
            state["i"] = (state["i"] + 1) % len(options)

        @kb.add("enter")
        def _choose(event):
            event.app.exit(result=options[state["i"]][0])

        @kb.add("escape", eager=True)
        @kb.add("c-c")
        def _back(event):
            event.app.exit(result=None)

        def render():
            lines = [("bold", f"{title}  (↑/↓, Enter to choose, Esc to cancel)\n")]
            for n, (_value, label) in enumerate(options):
                marker = "❯ " if n == state["i"] else "  "
                lines.append(("reverse" if n == state["i"] else "", f"{marker}{label}\n"))
            return lines

        app = Application(layout=Layout(Window(FormattedTextControl(render), always_hide_cursor=True)),
                          key_bindings=kb, full_screen=False, input=self._input, output=self._output)
        return app.run()
