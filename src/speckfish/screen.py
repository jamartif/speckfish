"""Full-screen mode for the console: the terminal's alternate screen, as `vim` or `less` use it.

On entry the terminal shows a blank screen and your earlier commands are out of sight; on exit the terminal is
exactly as it was, with its scrollback intact. The catch is that the alternate screen has no scrollback of its
own, so whatever scrolls off the top is gone: that is why long output goes through a small pager (`page`), and
why everything a step does is also on disk under `.speckfish/runs/`.

It is switched on only for a real terminal that understands escape codes (on Windows the console has to be
told to; if that fails there is simply no full screen). The screen is always given back: on a normal exit, an
exception, `atexit`, SIGTERM and SIGHUP.
"""
from __future__ import annotations

import atexit
import contextlib
import os
import shutil
import signal
import sys
from collections.abc import Callable

from .style import supports_vt

ENTER = "\x1b[?1049h\x1b[2J\x1b[H"      # alternate screen on, clear it, cursor home
LEAVE = "\x1b[?1049l"                    # alternate screen off: the terminal is as it was
CLEAR = "\x1b[2J\x1b[H"
TITLE = "\x1b]0;{}\x07"


def wanted(configured: bool = True) -> bool:
    """Full screen unless the config or SPECKFISH_FULLSCREEN=0 turns it off."""
    env = os.environ.get("SPECKFISH_FULLSCREEN")
    if env is not None:
        return env.strip().lower() not in ("0", "false", "no", "off")
    return configured


class AltScreen:
    def __init__(self, stream=None, title: str = "speckfish"):
        self.stream = stream if stream is not None else sys.stdout
        self.title = title
        self.active = False
        self._exit_hook = False
        self._old_handlers: dict[int, object] = {}

    @property
    def possible(self) -> bool:
        return supports_vt(self.stream)

    def _write(self, payload: str) -> None:
        with contextlib.suppress(Exception):          # a closed or odd stream must never break the exit path
            self.stream.write(payload)
            self.stream.flush()

    def enter(self) -> bool:
        if self.active or not self.possible:
            return self.active
        self._write(ENTER + TITLE.format(self.title))
        self.active = True
        if not self._exit_hook:
            self._exit_hook = True
            atexit.register(self.leave)         # once, however many times the screen is entered
        self._catch_signals()
        return True

    def leave(self) -> None:
        if not self.active:
            return
        self.active = False
        self._write(LEAVE)
        self._restore_signals()

    def clear(self) -> None:
        if self.active:
            self._write(CLEAR)

    def __enter__(self) -> AltScreen:
        self.enter()
        return self

    def __exit__(self, *_exc) -> None:
        self.leave()

    # a killed terminal tab must not leave the shell stuck on the alternate screen
    def _catch_signals(self) -> None:
        if os.name == "nt":
            return
        for name in ("SIGTERM", "SIGHUP"):
            sig = getattr(signal, name, None)
            if sig is None:
                continue
            with contextlib.suppress(ValueError, OSError):       # only the main thread may set handlers
                self._old_handlers[sig] = signal.signal(sig, self._on_signal)

    def _restore_signals(self) -> None:
        for sig, handler in self._old_handlers.items():
            with contextlib.suppress(ValueError, OSError, TypeError):
                signal.signal(sig, handler)         # type: ignore[arg-type]
        self._old_handlers.clear()

    def _on_signal(self, signum, _frame) -> None:
        self.leave()
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)


def rows() -> int:
    return shutil.get_terminal_size((100, 30)).lines


def page(text: str, write: Callable[[str], None], ask: Callable[[str], str], height: int | None = None) -> None:
    """Show `text`; if it does not fit on one screen, a page at a time (Enter = next page, q = the rest).

    On the alternate screen anything that scrolls past the top is lost, so a long report must not be dumped.
    """
    lines = text.rstrip("\n").split("\n")
    room = max(5, (height or rows()) - 4)
    if len(lines) <= room:
        write(text if text.endswith("\n") else text + "\n")
        return
    i = 0
    while i < len(lines):
        write("\n".join(lines[i:i + room]) + "\n")
        i += room
        if i >= len(lines):
            return
        try:
            answer = ask(f"-- {min(i, len(lines))}/{len(lines)} lines · Enter: more · q: skip the rest -- ")
        except (EOFError, KeyboardInterrupt):
            return
        if answer.strip().lower() in ("q", "quit", "n", "no"):
            write(f"({len(lines) - i} more line(s) skipped; the full text is on disk under .speckfish/)\n")
            return
