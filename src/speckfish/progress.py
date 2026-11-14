"""Live progress for long agent steps.

A three-minute silent subprocess looks like a hang. This renders one self-updating line while the
agent works, built only from what the CLI already prints (NDJSON events), and costs nothing extra:

    [review] 01:42 | subagents 5 | Agent egm:egm-zoom-out

On a pipe (not a TTY) it prints one line every 15 s instead, so logs stay readable. It never raises:
progress is a courtesy, the step result is the product.
"""

from __future__ import annotations

import contextlib
import json
import re
import sys
import threading
import time

ENABLED = True          # the CLI clears this for --no-progress / --json / --quiet


# tools worth naming in the status line, in the order we prefer to show them
# what to expect, so a long silence is not read as a hang
EXPECTED = {
    "review": "usually 3-5 min", "drift": "usually 1-2 min", "specify": "usually 30-60 s",
    "plan": "usually 30-60 s", "tasks": "usually 30-60 s", "analyze": "usually 30-60 s",
    "implement": "several minutes", "specify-split": "a few minutes",
    "brainstorm": "1-2 min", "draft": "1-2 min",
}

_TOOL_LABELS = {
    "Agent": "subagent", "Task": "subagent", "Bash": "bash", "PowerShell": "shell",
    "Read": "read", "Write": "write", "Edit": "edit", "Glob": "glob", "Grep": "grep",
    "WebFetch": "fetch", "WebSearch": "search",
}


def _mmss(seconds: float) -> str:
    total = int(seconds)
    return f"{total // 60:02d}:{total % 60:02d}"


_MAX_ACTIVITY = 100

# the shell wrapper the agent adds around every command, so we can show what it actually ran:
# `"C:\...\powershell.exe" -NoProfile -Command "..."` -> keep only the command
_EXE_PREFIX = re.compile(r'^(?:"[^"]*(?:powershell|pwsh|cmd|bash|sh|node)\.exe"'
                         r'|[^"\s]*(?:powershell|pwsh|cmd|bash|sh|node)\.exe"?'
                         r'|/[^\s]*(?:bash|sh|zsh)|(?:bash|sh|zsh))\s+', re.I)
_FLAG = re.compile(r'^-{1,2}[A-Za-z][\w-]*$')


def _short(text: str, limit: int = _MAX_ACTIVITY) -> str:
    """One readable line: drop the shell wrapper, collapse whitespace, cap the length."""
    text = " ".join(str(text or "").split())
    # `"C:\...\powershell.exe" -NoProfile -Command "Get-Content ..."` -> `Get-Content ...`
    prefix = _EXE_PREFIX.match(text)
    if prefix:
        text = text[prefix.end():]
    # keep peeling the wrapper's flags: -NoProfile, -Command, -lc, /c
    while True:
        head, _, rest = text.partition(" ")
        if not rest or not (_FLAG.match(head) or head.lower() == "/c"):
            break
        text = rest
    text = text.strip().strip('"').strip("'").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


class Progress:
    """Thread-safe, best-effort status line for one running step."""

    def __init__(self, label: str, enabled: bool = True, stream=None, every: float = 1.0,
                 plain_every: float = 10.0, hint: str | None = None, plain_only: bool = False,
                 root: str = ""):
        self.label = label
        self.root = str(root or "").replace("\\", "/")
        self.hint = hint if hint is not None else EXPECTED.get(label, "")
        self.enabled = bool(enabled and ENABLED)
        self.stream = stream if stream is not None else sys.stderr
        self.every = every
        self.plain_every = plain_every
        # `plain_only` when the agent already streams prose to the same terminal: a carriage-return
        # line would be overwritten by its output, so emit standalone lines instead (never nothing)
        self.is_tty = bool(getattr(self.stream, "isatty", lambda: False)()) and not plain_only
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._t0 = time.monotonic()
        self._last_plain = 0.0
        self._printed = 0
        self._last_activity = ""
        self.subagents: list[str] = []
        self.tools = 0
        self.last_tool = ""
        self.done = False
        self.finished = False
        self.tokens = 0

    # ---------- lifecycle ----------
    def __enter__(self) -> Progress:
        self.start()
        return self

    def __exit__(self, *_exc) -> None:
        self.stop()

    def start(self) -> None:
        if not self.enabled:
            return
        self._thread = threading.Thread(target=self._tick, daemon=True)
        self._thread.start()
        self._draw(force=True)

    def stop(self) -> None:
        if not self.enabled:
            return
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self.done = True
        if self.is_tty:
            self._clear()

    # ---------- feeding ----------
    def feed(self, line: str) -> None:
        """Update the counters from one line of the agent's output. Never raises."""
        if not self.enabled:
            return
        text = (line or "").strip()
        if not (text.startswith("{") and text.endswith("}")):
            return
        try:
            event = json.loads(text)
        except ValueError:
            return
        if not isinstance(event, dict):
            return
        with contextlib.suppress(Exception):      # a malformed event must never break a run
            self._absorb(event)

    def _absorb(self, event: dict) -> None:
        etype = str(event.get("type") or "")
        # --- claude stream-json ---
        if etype == "assistant":
            for block in (event.get("message") or {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    self._tool(str(block.get("name") or ""), block.get("input") or {})
        elif etype == "result":
            usage = event.get("usage") or {}
            self.tokens = int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0)
            self.finished = True
        # --- codex exec --json ---
        elif etype in ("item.started", "item.completed"):
            self._codex_item(event.get("item") or {}, started=etype == "item.started")
        elif etype == "turn.completed":
            self.tokens = int((event.get("usage") or {}).get("output_tokens") or 0)
            self.finished = True
        # --- copilot JSONL ---
        elif "subagent" in etype:
            name = str(event.get("name") or event.get("agent") or event.get("subagent_type") or "")
            if name and name not in self.subagents:
                self.subagents.append(name)
            self.last_tool = "subagent"
        elif "tool" in etype:
            self._tool(str(event.get("name") or event.get("tool") or ""), event)

    def _codex_item(self, item: dict, started: bool) -> None:
        """codex exec --json items: command_execution, file_change, agent_message, error."""
        kind = str(item.get("type") or "")
        if kind == "command_execution":
            if not started:
                self.tools += 1
                self.last_tool = "shell"
                cmd = _short(item.get("command") or "")
                if cmd:
                    self.activity(f"$ {cmd}")
        elif kind == "file_change":
            changes = item.get("changes") or []
            for change in (changes if isinstance(changes, list) else []):
                path = str((change or {}).get("path") or "")
                if path:
                    self.activity(f"wrote {self.relpath(path)}")
        elif kind in ("agent_message", "reasoning") and not started:
            if str(item.get("text") or item.get("reasoning") or "").strip():
                self.last_tool = "thinking"
        elif kind == "error" and not started:
            msg = _short(item.get("message") or "", 100)
            if msg and "Model metadata" not in msg:      # harmless provider warning
                self.activity(f"warning: {msg}")

    def _tool(self, name: str, payload: dict) -> None:
        if not name:
            return
        self.tools += 1
        self.last_tool = _TOOL_LABELS.get(name, name.lower())
        if name in ("Agent", "Task"):
            sub = str(payload.get("subagent_type") or payload.get("description") or "").strip()
            if sub and sub not in self.subagents:
                self.subagents.append(sub)
                self.activity(f"subagent {_short(sub, 60)}")
        elif name in ("Write", "Edit"):
            path = str(payload.get("file_path") or payload.get("path") or "")
            if path:
                verb = "wrote" if name == "Write" else "edited"
                self.activity(f"{verb} {self.relpath(path)}")
        elif name == "Read":
            path = str(payload.get("file_path") or payload.get("path") or "")
            if path:
                self.activity(f"read {self.relpath(path)}")
        elif name in ("Bash", "PowerShell"):
            cmd = str(payload.get("command") or "")
            if cmd:
                self.activity(f"$ {_short(cmd)}")

    def relpath(self, path: str) -> str:
        """Show paths relative to the project: absolute ones add nothing readable."""
        text = str(path or "").replace("\\", "/")
        if self.root and text.lower().startswith(self.root.lower().rstrip("/") + "/"):
            text = text[len(self.root.rstrip("/")) + 1:]
        return _short(text, 70)

    # ---------- activity ----------
    def activity(self, text: str) -> None:
        """Permanent line for something that actually happened."""
        if not self.enabled or not text:
            return
        with self._lock:
            if text == self._last_activity:      # a repeated action is noise
                return
            self._last_activity = text
            stamp = _mmss(time.monotonic() - self._t0)
            if self.is_tty:
                self._clear()
                self._printed = 0
            self.stream.write(f"[{self.label}] {stamp} | {text}" + "\n")
            self.stream.flush()

    # ---------- rendering ----------
    def status(self) -> str:
        head = f"[{self.label}] {_mmss(time.monotonic() - self._t0)}"
        extra = []
        if self.subagents:
            extra.append(f"subagents {len(self.subagents)}")
        if self.tools:
            extra.append(f"tools {self.tools}")
        if self.subagents:
            extra.append(f"last {self.subagents[-1]}")
        elif self.last_tool:
            extra.append(self.last_tool)
        if self.finished:
            extra.append("finishing")
        if not extra and self.hint:
            extra.append(f"starting, {self.hint}")
        return head + ((" | " + " | ".join(extra)) if extra else "")

    def _tick(self) -> None:
        while not self._stop.wait(self.every):
            self._draw()

    def _draw(self, force: bool = False) -> None:
        text = self.status()
        if self.is_tty:
            self._write(f"\r{text}{' ' * max(0, self._printed - len(text))}")
            self._printed = len(text)
            return
        now = time.monotonic()
        if force or now - self._last_plain >= self.plain_every:
            self._last_plain = now
            self.stream.write(text + "\n")
            self.stream.flush()

    def _clear(self) -> None:
        if self._printed:
            self._write("\r" + " " * self._printed + "\r")

    def _write(self, payload: str) -> None:
        with self._lock:
            try:
                self.stream.write(payload)
                self.stream.flush()
            except Exception:
                self.enabled = False
