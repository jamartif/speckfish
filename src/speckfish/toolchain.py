"""Do the tools the project builds with exist? Asked before `implement`, not discovered 17 minutes into it.

A real run on an Android project spent a whole chunk finding out that Java was not installed. The project says
what it needs: a `gradlew` needs Java, a `package.json` needs node, and a new project's `plan.md` says
"Kotlin" or "TypeScript" before any file exists. Both are read (marker files, the plan's Technical Context);
`config require_tools="java,adb"` adds your own. The rest of the specs is not searched.
"""
from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

# marker file (or glob) in the project root -> tools it needs
MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("gradlew", ("java",)), ("build.gradle", ("java",)), ("build.gradle.kts", ("java",)),
    ("pom.xml", ("java", "mvn")),
    ("package.json", ("node", "npm")),
    ("Cargo.toml", ("cargo",)), ("go.mod", ("go",)),
    ("*.csproj", ("dotnet",)), ("*.sln", ("dotnet",)),
    ("pubspec.yaml", ("flutter",)), ("Gemfile", ("ruby",)),
)


# words in the plan's "Technical Context" -> tool. A new solution has no gradlew or package.json yet; the plan
# is what already says "Kotlin, Gradle" or "TypeScript, Node 22".
PLAN_WORDS: tuple[tuple[str, str], ...] = (
    (r"kotlin|\bjava\b|jdk|gradle|android|spring boot", "java"),
    (r"typescript|javascript|\bnode(\.?js)?\b|\bnpm\b|\breact\b|\bvite\b|\bnext\.?js\b", "node"),
    (r"python|django|flask|fastapi|pytest", "python"),
    (r"\brust\b|cargo", "cargo"), (r"\bgolang\b|\bgo \d|\bgo\b\s*(?:1\.|$)", "go"),
    (r"c#|\.net\b|dotnet|asp\.net", "dotnet"), (r"flutter|\bdart\b", "flutter"),
    (r"\bruby\b|rails", "ruby"),
)
_CONTEXT_LINE = re.compile(r"^[\s*_>#-]*(language(?:/version)?|primary dependencies|testing|target platform|"
                           r"build(?: system)?|framework)[\s*_]*:[\s*_]*(.+)$", re.I | re.M)
ALIASES = {"python": ("python", "python3", "py")}


def from_plan(plan: Path) -> dict[str, str]:
    """tool -> why, read from the plan's Technical Context lines (Language/Version, Dependencies, Testing...)."""
    try:
        text = plan.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    out: dict[str, str] = {}
    for m in _CONTEXT_LINE.finditer(text):
        value = m.group(2)
        if "NEEDS CLARIFICATION" in value.upper():
            continue
        for pattern, tool in PLAN_WORDS:
            if tool not in out and re.search(pattern, value, re.I):
                out[tool] = f"plan.md says: {m.group(1).strip().lower()}: {value.strip()[:40]}"
    return out


def _have(tool: str) -> bool:
    if any(shutil.which(name) for name in ALIASES.get(tool, (tool,))):
        return True
    if tool == "java":                                   # gradlew and Maven honour JAVA_HOME
        home = os.environ.get("JAVA_HOME")
        return home is not None and shutil.which("java", path=str(Path(home) / "bin")) is not None
    return False


def needed(root: Path, extra: str = "", plan: Path | None = None) -> dict[str, str]:
    """tool -> why it is needed, in the order found."""
    out: dict[str, str] = {}
    for pattern, tools in MARKERS:
        found = next(iter(root.glob(pattern)), None) if any(c in pattern for c in "*?") else (
            root / pattern if (root / pattern).exists() else None)
        if found is None:
            continue
        for tool in tools:
            out.setdefault(tool, f"the project has {found.name}")
    for tool, why in (from_plan(plan).items() if plan else ()):
        out.setdefault(tool, why)
    for tool in (t.strip() for t in extra.split(",")):
        if tool:
            out[tool] = "listed in config require_tools"
    return out


def missing(root: Path, extra: str = "", plan: Path | None = None) -> list[str]:
    """One line per tool that is needed and not on PATH ('' list = all there, or the check is off)."""
    if os.environ.get("SPECKFISH_SKIP_TOOL_CHECK") == "1":
        return []
    return [f"{tool} ({why})" for tool, why in needed(root, extra, plan).items() if not _have(tool)]
