"""What a step changed in the project: task checkboxes, git working tree, the current feature."""
from __future__ import annotations

import itertools
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


def feature_json_dir(root: Path) -> Path | None:
    """The folder `.specify/feature.json` points at, only if it exists and is inside the project.

    The file is written by the agent: an absolute path or `../..` in it must not move tasks, reviews or
    `implement` to some other directory.
    """
    try:
        rel = json.loads((root / ".specify" / "feature.json").read_text(encoding="utf-8")
                         ).get("feature_directory") or ""
    except (OSError, ValueError, AttributeError):
        return None
    if not rel:
        return None
    base = Path(root).resolve()
    try:
        target = (base / rel).resolve()
        target.relative_to(base)
    except (ValueError, OSError):
        return None
    return target if target.is_dir() else None


def task_progress(root: Path) -> tuple[int, int] | None:
    """(done, total) checkboxes in the active feature's tasks.md — the honest measure of implement."""
    active = feature_json_dir(root)
    candidates = [active / "tasks.md"] if active else []
    candidates += sorted((root / "specs").glob("*/tasks.md"), reverse=True)
    for path in candidates:
        if not path.exists():
            continue
        done, total = count_tasks(path)
        if total:
            return done, total
    return None


_NOISE_DIRS = {".speckfish", ".agents", ".claude"}          # tool state, never the user's work
_NOISE_FILES = {".gitignore", "CLAUDE.md", "AGENTS.md"}
_BUILD_DIRS = {"bin", "obj"}                                  # .NET output: bin/Debug, obj/Release, ...


def _porcelain_path(line: str) -> str:
    path = line[3:].strip().strip('"')
    return path.split(" -> ")[-1] if " -> " in path else path


def _is_noise(path: str) -> bool:
    """Tool state or build output, by path *segments*: `src/bin/main.rs` is real work, `bin/Debug/x.dll` is not."""
    parts = [p for p in path.replace("\\", "/").split("/") if p]
    if not parts:
        return False
    if parts[-1] in _NOISE_FILES or any(p in _NOISE_DIRS for p in parts[:-1]):
        return True
    return any(p in _BUILD_DIRS and nxt in ("Debug", "Release") for p, nxt in itertools.pairwise(parts))


def _git_porcelain(root: Path) -> list[str] | None:
    """`git status --porcelain` lines that are real work, not build output or tool state (None: unknown)."""
    import subprocess as sp

    from .config import find_git
    try:
        # find_git, not a bare "git": on Windows with App Control the one on PATH can be blocked
        out = sp.run([find_git(), "status", "--porcelain", "-uall"], cwd=str(root), capture_output=True,
                     text=True, timeout=60, encoding="utf-8", errors="replace").stdout
    except (OSError, sp.SubprocessError, SystemExit):
        return None
    return [ln for ln in out.splitlines() if ln.strip() and not _is_noise(_porcelain_path(ln))]


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
        path = _porcelain_path(ln)
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
    active = feature_json_dir(root)
    if active is not None:
        return active
    feats = sorted((root / "specs").glob("*/spec.md"), key=lambda p: p.stat().st_mtime)
    return feats[-1].parent if feats else None


TASK_LINE = re.compile(r"^\s*-\s*\[(?P<mark>[ xX])\]\s*(?P<id>T\d+)?\s*(?P<text>.*?)\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")
_WORDS = re.compile(r"\s+")


# `[MANUAL]` in a task's text: a person has to do it (a physical device, a decision, a login). speckfish does
# not hand it to the agent, and lists it as yours instead of calling the feature unfinished.
_MANUAL = re.compile(r"\[manual\]|\(manual\)", re.I)
# what the agent writes when it cannot finish a task: `BLOCKED: T002 - needs the phone` (or `**Blocked:** ...`)
_BLOCKED = re.compile(r"^[\s*_>#`-]*blocked[\s*_`]*[:：][\s*_`]*(.+?)\s*$", re.I | re.M)


def parse_blocked(text: str) -> list[str]:
    """The reasons an agent gave for not finishing, one per `BLOCKED:` line of its final message."""
    return [m.group(1).strip(" *_`")[:300] for m in _BLOCKED.finditer(text or "") if m.group(1).strip(" *_`")]


def open_manual(tasks: list[dict]) -> list[str]:
    """Ids of the `[MANUAL]` tasks that are still open."""
    return [t["id"] for t in tasks if t.get("manual") and not t["done"]]


def parse_tasks(path: Path) -> list[dict]:
    """Every task of a tasks.md in order: {id, done, text, phase, manual}."""
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
            tasks.append({"id": ident, "done": m.group("mark") != " ", "text": text, "phase": phase,
                          "manual": bool(_MANUAL.search(text))})
    return tasks


def next_chunk(tasks: list[dict], size: int) -> list[dict]:
    """The next `size` pending tasks the agent can do, in file order (size <= 0: all of them).

    `[MANUAL]` tasks are left out: they are for a person.
    """
    pending = [t for t in tasks if not t["done"] and not t.get("manual")]
    return pending if size <= 0 else pending[:size]
