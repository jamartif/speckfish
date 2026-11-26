#!/usr/bin/env python3
"""Regenerate the README screenshots from real runs of speckfish.

    python docs/screenshots/capture.py            # all of them, into docs/screenshots/*.png

What is real: speckfish itself, running in a real pseudo-terminal of 110x32 cells (`pty`), with its real setup
wizard, `specify init`, the pinned EGM checkout, prompt_toolkit menus and pickers, and the real result parsing.
What the screen shows is interpreted by a terminal emulator (`pyte`) and drawn to PNG with Chromium.

What is not real: the agent. `stubs/claude` plays a short fixed session (the same stream-json events the real CLI
prints) and `stubs/copilot` / `stubs/codex` only answer `--version`, so the picker has three agents to list. A
screenshot therefore costs nothing, needs no login and looks the same every time. Nothing here is a mock-up of
speckfish's own output.

Needs: pyte, node with playwright (Chromium), uv (for Spec Kit), git, network (Spec Kit templates, EGM).
"""
from __future__ import annotations

import fcntl
import html
import os
import select
import shutil
import struct
import subprocess
import sys
import tempfile
import termios
import time
from pathlib import Path

import pty
import pyte

HERE = Path(__file__).resolve().parent
SRC = HERE.parent.parent / "src"
COLS, ROWS = 110, 32

THEME = {  # a dark theme close to Windows Terminal's defaults
    "bg": "#16181d", "fg": "#d4d7de",
    "black": "#2b2f38", "red": "#e06c75", "green": "#98c379", "brown": "#e5c07b", "blue": "#61afef",
    "magenta": "#c678dd", "cyan": "#56b6c2", "white": "#d4d7de",
    "brightblack": "#6b7280", "brightred": "#ef8a93", "brightgreen": "#b1d98f", "brightbrown": "#f0d28f",
    "brightblue": "#7bc0ff", "brightmagenta": "#d894ee", "brightcyan": "#73cfdb", "brightwhite": "#ffffff",
}


class Term:
    """A program running in a pseudo-terminal, with its screen kept by a terminal emulator."""

    def __init__(self, argv, cwd, env):
        self.screen = pyte.Screen(COLS, ROWS)
        self.stream = pyte.ByteStream(self.screen)
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            os.chdir(cwd)
            os.execvpe(argv[0], argv, env)
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLS, 0, 0))
        self.raw = b""

    def pump(self, seconds=0.25):
        end = time.time() + seconds
        while time.time() < end:
            ready, _, _ = select.select([self.fd], [], [], 0.05)
            if not ready:
                continue
            try:
                chunk = os.read(self.fd, 65536)
            except OSError:
                return False
            if not chunk:
                return False
            self.raw += chunk
            self.stream.feed(chunk)
            if b"\x1b[6n" in chunk:        # prompt_toolkit asks where the cursor is: answer like a terminal
                os.write(self.fd, f"\x1b[{self.screen.cursor.y + 1};{self.screen.cursor.x + 1}R".encode())
        return True

    def text(self):
        return "\n".join(self.screen.display)

    def wait_for(self, needle, timeout=60):
        end = time.time() + timeout
        while time.time() < end:
            self.pump(0.2)
            if needle in self.text():
                return True
        raise TimeoutError(f"{needle!r} never appeared; screen was:\n{self.text()}")

    def send(self, data: str, settle=0.4):
        os.write(self.fd, data.encode())
        self.pump(settle)

    def close(self):
        try:
            os.kill(self.pid, 15)
        except OSError:
            pass
        try:
            os.waitpid(self.pid, 0)
        except OSError:
            pass


# ---------------------------------------------------------------- drawing

def _colour(name, default):
    if name == "default":
        return default
    if name in THEME:
        return THEME[name]
    if len(name) == 6 and all(c in "0123456789abcdefABCDEF" for c in name):
        return "#" + name
    return default


def to_html(term: Term, title: str) -> str:
    buf = term.screen.buffer
    last = max((y for y in range(ROWS) if any(buf[y][x].data.strip() for x in range(COLS))), default=0)
    rows = []
    for y in range(min(ROWS - 1, last + 1) + 1):
        spans, cur, text = [], None, []

        def flush():
            if text:
                spans.append((cur, "".join(text)))

        for x in range(COLS):
            ch = buf[y][x]
            if ch.data == "":                 # the second cell of a wide character
                continue
            fg, bg = _colour(ch.fg, THEME["fg"]), _colour(ch.bg, None)
            if ch.reverse:
                fg, bg = bg or THEME["bg"], fg
            style = (fg, bg, ch.bold, ch.italics, ch.underscore)
            if style != cur:
                flush()
                cur, text = style, []
            text.append(ch.data)
        flush()
        parts = []
        for (fg, bg, bold, italics, under), s in spans:
            css = f"color:{fg};"
            if bg:
                css += f"background:{bg};"
            if bold:
                css += "font-weight:700;"
            if italics:
                css += "font-style:italic;"
            if under:
                css += "text-decoration:underline;"
            parts.append(f'<span style="{css}">{html.escape(s)}</span>')
        rows.append("".join(parts).rstrip() or " ")
    body = "\n".join(rows)
    return f"""<!doctype html><meta charset="utf-8"><style>
body {{ margin:0; background:transparent; padding:28px; }}
.win {{ display:inline-block; background:{THEME['bg']}; border-radius:10px; overflow:hidden;
        box-shadow:0 12px 40px rgba(0,0,0,.45), 0 0 0 1px rgba(255,255,255,.08); }}
.bar {{ background:#262a33; padding:9px 14px; display:flex; align-items:center; gap:7px;
        font:12px 'DejaVu Sans',sans-serif; color:#9aa0ad; }}
.bar i {{ width:11px; height:11px; border-radius:50%; display:inline-block; }}
.bar span {{ margin-left:10px; }}
pre {{ margin:0; padding:14px 18px 16px; font:13px/1.28 'DejaVu Sans Mono','Liberation Mono',monospace;
       color:{THEME['fg']}; white-space:pre; }}
</style><div class="win"><div class="bar"><i style="background:#ff5f56"></i><i style="background:#ffbd2e"></i>
<i style="background:#27c93f"></i><span>{html.escape(title)}</span></div><pre>{body}</pre></div>"""


def shot(term: Term, name: str, title="Terminal — speckfish"):
    html_path = Path(tempfile.mkdtemp()) / "s.html"
    html_path.write_text(to_html(term, title), encoding="utf-8")
    out = HERE / f"{name}.png"
    js = ("const {chromium}=require('playwright');(async()=>{const b=await chromium.launch();"
          "const p=await b.newPage({deviceScaleFactor:2});await p.goto('file://'+process.argv[1]);"
          "await (await p.$('.win')).screenshot({path:process.argv[2],omitBackground:true});await b.close();})();")
    subprocess.run(["node", "-e", js, str(html_path), str(out)], check=True)
    if not out.exists() or out.stat().st_size < 2000:        # a silent no-op once looked like success
        raise RuntimeError(f"the screenshot {out} was not written")
    print("wrote", out.relative_to(HERE.parent.parent))


# ---------------------------------------------------------------- scenes

def environment(work: Path, home: Path) -> dict:
    stubs = HERE / "stubs"
    return {**os.environ, "PYTHONPATH": str(SRC), "PATH": f"{stubs}:{os.environ['PATH']}",
            "TERM": "xterm-256color", "COLUMNS": str(COLS), "LINES": str(ROWS), "SPECKFISH_HOME": str(home),
            "NO_COLOR": "", "STUB_PACE": "0.9", "LANG": "C.UTF-8"}


def seed_feature(work: Path):
    """What a team would already have after /specify and /plan: written by hand, the review reads it."""
    feat = work / "specs" / "001-exception-tickets"
    feat.mkdir(parents=True, exist_ok=True)
    (feat / "spec.md").write_text("# Exception tickets\n\nAn API that receives an exception and opens a "
                                  "ticket for the team to review.\n", encoding="utf-8")
    (feat / "plan.md").write_text("# Plan\n\nPOST /exceptions -> create a ticket through the ticketing API.\n",
                                  encoding="utf-8")
    (feat / "tasks.md").write_text("# Tasks\n- [ ] T001 Create the endpoint\n- [ ] T002 Map exception to ticket\n"
                                   "- [ ] T003 Tests\n", encoding="utf-8")


def main():
    only = set(sys.argv[1:])
    base = Path(tempfile.mkdtemp(prefix="shots-"))
    projects = Path("/tmp/projects")                 # a short, ordinary path: it shows in the banner
    shutil.rmtree(projects, ignore_errors=True)
    work, home = projects / "my-api", base / "home"
    work.mkdir(parents=True)
    home.mkdir()
    env = environment(work, home)
    argv = [sys.executable, "-m", "speckfish"]

    t = Term(argv, work, env)
    try:
        # 1. first run: the wizard
        t.wait_for("Set it up now?")
        t.pump(0.5)
        t.send("y\r", 1.0)
        t.wait_for("Which agent CLI")
        t.pump(0.6)
        if not only or "01-wizard" in only:
            shot(t, "01-wizard")
        t.send("\r", 0.8)                       # claude
        t.wait_for("Fallback")
        t.send("\r", 0.6)                       # no fallback
        t.send("\r", 0.5)                       # language: en
        t.wait_for("Type /help", timeout=240)   # the real `specify init` and the pinned EGM clone
        t.pump(1.0)
        if not only or "02-ready" in only:
            shot(t, "02-ready")

        # 2. the / menu
        t.send("/", 0.9)
        t.pump(0.6)
        if not only or "03-menu" in only:
            shot(t, "03-menu")
        t.send("\x7f", 0.3)

        # 3. free text step: editor, preview, running, result
        seed_feature(work)
        t.send("/constitution\r", 0.8)
        t.wait_for("Principles of the project")
        t.send(".NET 10, treat warnings as errors\r", 0.3)
        t.send("SOLID principles\r", 0.3)
        t.send("global exception handling\r", 0.3)
        t.send("\r", 0.8)
        t.wait_for("Send?")
        if not only or "04-preview" in only:
            shot(t, "04-preview")
        t.send("\r", 0.3)
        t.wait_for("listing .specify", timeout=30)      # mid-run: the live line, in plain words
        if not only or "05-running" in only:
            shot(t, "05-running")
        t.wait_for("agent:", timeout=60)
        t.pump(0.8)
        if not only or "06-done" in only:
            shot(t, "06-done")

        # 4. a review with blockers
        t.send("/clear\r", 0.8)
        t.send("/review\r", 0.5)
        t.wait_for("Consolidated review", timeout=5) if False else None
        t.wait_for("BLOCKER", timeout=60)
        t.pump(0.8)
        if not only or "07-review" in only:
            shot(t, "07-review")

        # 5. the model picker
        t.send("/model\r", 1.0)
        t.wait_for("Which model")
        t.send("\x1b[B", 0.3)
        t.pump(0.4)
        if not only or "08-model" in only:
            shot(t, "08-model")
        t.send("\x1b", 0.5)
        t.send("/exit\r", 0.5)
    finally:
        t.close()
        shutil.rmtree(base, ignore_errors=True)
        shutil.rmtree(projects, ignore_errors=True)


if __name__ == "__main__":
    main()
