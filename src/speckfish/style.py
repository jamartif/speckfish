"""Colour and glyphs for the terminal, only where they will be understood.

Colour is on when the stream is a terminal, `NO_COLOR` is not set and `TERM` is not `dumb`. On Windows the
console has to be told to interpret escape codes: that is tried once, and if it fails there is simply no
colour (plain text always works). Nothing here is ever written to a pipe, a log or JSON output.
"""
from __future__ import annotations

import os
import re
import sys

_CODES = {"bold": "1", "dim": "2", "red": "31", "green": "32", "yellow": "33", "cyan": "36", "grey": "90"}
_WINDOWS_VT: bool | None = None


def _windows_vt() -> bool:
    """Switch on escape-code processing for this console (Windows 10+). False when it cannot be done."""
    global _WINDOWS_VT
    if _WINDOWS_VT is None:
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32           # type: ignore[attr-defined,unused-ignore]
            handle = kernel32.GetStdHandle(-11)         # STD_OUTPUT_HANDLE
            mode = ctypes.c_uint32()
            ok = bool(kernel32.GetConsoleMode(handle, ctypes.byref(mode)))
            _WINDOWS_VT = ok and bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
        except Exception:
            _WINDOWS_VT = False
    return bool(_WINDOWS_VT)


def supports_vt(stream=None) -> bool:
    """A real terminal that understands escape codes (cursor, screen modes, colour)."""
    stream = stream if stream is not None else sys.stdout
    if os.environ.get("TERM") == "dumb" or not getattr(stream, "isatty", lambda: False)():
        return False
    return _windows_vt() if os.name == "nt" else True


def supports_color(stream=None) -> bool:
    return not os.environ.get("NO_COLOR") and supports_vt(stream)


def paint(text: str, *styles: str, on: bool = True) -> str:
    """`text` wrapped in the named styles; unchanged when colour is off."""
    codes = ";".join(_CODES[s] for s in styles if s in _CODES)
    return f"\x1b[{codes}m{text}\x1b[0m" if on and codes else text


_LINE_RULES = (
    (re.compile(r"^(OK\b)"), ("green", "bold")),
    (re.compile(r"^(FAILED\b)"), ("red", "bold")),
    (re.compile(r"^(\s*)(BLOCKER|ERROR)\b"), ("red",)),
    (re.compile(r"^(\s*)WARNING\b"), ("yellow",)),
    (re.compile(r"^(\s*)(artifacts|log|agent):"), ("grey",)),
)


def colorize_summary(text: str) -> str:
    """Colour the lines of a step summary by what they say; the words themselves are untouched."""
    out = []
    for line in text.split("\n"):
        for rx, styles in _LINE_RULES:
            if rx.match(line):
                line = paint(line, *styles)
                break
        else:
            if line.rstrip().endswith("<- start here"):
                line = paint(line, "cyan")
        out.append(line)
    return "\n".join(out)
