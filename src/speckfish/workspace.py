"""What a step changed in the project: task checkboxes, git working tree, the current feature."""
from __future__ import annotations

import json
import re
from pathlib import Path


def count_tasks(path: Path) -> tuple[int, int]:
    """(done, total) task checkboxes in one tasks.md; (0, 0) when it does not exist.

    Same reading as `parse_tasks` (indented subtasks count, code blocks do not), so the loop and the
    chunk picker can never disagree about what is pending.
    """
    tasks = parse_tasks(path)
    return sum(1 for t in tasks if t["done"]), len(tasks)


def task_progress(root: Path) -> tuple[int, int] | None:
    """(done, total) checkboxes in the active feature's tasks.md — the honest measure of implement."""
    try:
        feat = json.loads((root / ".specify" / "feature.json").read_text(encoding="utf-8"))
        rel = feat.get("feature_directory") or ""
    except (OSError, ValueError):
        rel = ""
    candidates = [root / rel / "tasks.md"] if rel else []
    candidates += sorted((root / "specs").glob("*/tasks.md"), reverse=True)
    for path in candidates:
        if not path.exists():
            continue
        done, total = count_tasks(path)
        if total:
            return done, total
    return None


_GIT_NOISE = (".speckfish/", "/bin/", "/obj/", "bin/Debug", "obj/Debug", ".gitignore", "CLAUDE.md",
              "AGENTS.md", ".agents/", ".claude/")


def _git_porcelain(root: Path) -> list[str] | None:
    """`git status --porcelain` lines that are real work, not build output or tool state (None: unknown)."""
    import subprocess as sp
    try:
        out = sp.run(["git", "status", "--porcelain", "-uall"], cwd=str(root), capture_output=True,
                     text=True, timeout=60, encoding="utf-8", errors="replace").stdout
    except (OSError, sp.SubprocessError):
        return None
    return [ln for ln in out.splitlines()
            if ln.strip() and not any(n in ln.replace("\\", "/") for n in _GIT_NOISE)]


def git_snapshot(root: Path) -> dict | None:
    """What the working tree looks like right now: each changed path with its size and mtime.

    A file that was already modified before the step and is modified again keeps the same porcelain
    line, so comparing lines alone cannot tell "the agent wrote code" from "the repo was already dirty".
    """
    lines = _git_porcelain(root)
    if lines is None:
        return None
    snap: dict = {}
    for ln in lines:
        path = ln[3:].strip().strip('"')
        if " -> " in path:
            path = path.split(" -> ")[-1]
        try:
            st = (Path(root) / path).stat()
            snap[ln] = (st.st_size, st.st_mtime_ns)
        except OSError:
            snap[ln] = None
    return snap


def _git_dirty(root: Path, before: dict | None = None) -> bool:
    """The step changed real work. With a `before` snapshot only what changed *during* the step counts."""
    if before is not None:
        after = git_snapshot(root)
        if after is None:
            return True                  # cannot tell: do not fail a step over it
        return after != before
    interesting = _git_porcelain(root)
    if interesting is None:
        return True                      # cannot tell: do not fail a step over it
    return bool(interesting)


def changed_besides(before: dict | None, after: dict | None, allowed: Path) -> list[str]:
    """Paths whose state differs between two `git_snapshot`s, leaving out `allowed` (None: unknown)."""
    if before is None or after is None:
        return []
    allowed_name = allowed.name
    changed = [ln for ln in after if before.get(ln, "~") != after[ln]]
    return [ln for ln in changed if not ln.rstrip().endswith(allowed_name)]


def feature_dir(root: Path) -> Path | None:
    """The feature Spec Kit points at (`.specify/feature.json`), else the newest one."""
    try:
        rel = json.loads((root / ".specify" / "feature.json").read_text(encoding="utf-8")
                         ).get("feature_directory") or ""
    except (OSError, ValueError):
        rel = ""
    if rel and (root / rel).is_dir():
        return root / rel
    feats = sorted((root / "specs").glob("*/spec.md"), key=lambda p: p.stat().st_mtime)
    return feats[-1].parent if feats else None


TASK_LINE = re.compile(r"^\s*-\s*\[(?P<mark>[ xX])\]\s*(?P<id>T\d+)?\s*(?P<text>.*?)\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")
_WORDS = re.compile(r"\s+")


def parse_tasks(path: Path) -> list[dict]:
    """Every task of a tasks.md in order: {id, done, text, phase}."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    tasks, phase, in_code = [], "", False
    for ln in lines:
        if _FENCE.match(ln):
            in_code = not in_code
            continue
        if in_code:
            continue                     # a `- [ ]` inside a code block is an example, not a task
        if ln.startswith("#"):
            phase = ln.lstrip("#").strip()
            continue
        m = TASK_LINE.match(ln)
        if m:
            text = m.group("text")
            # a task without a T-number is named by its text, so an inserted line does not shift it
            ident = m.group("id") or "~" + _WORDS.sub(" ", text).strip().lower()[:80]
            tasks.append({"id": ident, "done": m.group("mark") != " ", "text": text, "phase": phase})
    return tasks


def next_chunk(tasks: list[dict], size: int) -> list[dict]:
    """The next `size` pending tasks, in file order (size <= 0: every pending task)."""
    pending = [t for t in tasks if not t["done"]]
    return pending if size <= 0 else pending[:size]
