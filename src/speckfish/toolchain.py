"""Do the tools the project builds with exist? Asked before `implement`, not discovered 17 minutes into it.

A real run on an Android project spent a whole chunk finding out that Java was not installed. The project says
what it needs: a `gradlew` needs Java, a `package.json` needs node. Those are detected from marker files;
`config require_tools="java,adb"` adds your own. Nothing is guessed from the text of the specs.
"""
from __future__ import annotations

import os
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


def _have(tool: str) -> bool:
    if shutil.which(tool):
        return True
    if tool == "java":                                   # gradlew and Maven honour JAVA_HOME
        home = os.environ.get("JAVA_HOME")
        return home is not None and shutil.which("java", path=str(Path(home) / "bin")) is not None
    return False


def needed(root: Path, extra: str = "") -> dict[str, str]:
    """tool -> why it is needed, in the order found."""
    out: dict[str, str] = {}
    for pattern, tools in MARKERS:
        found = next(iter(root.glob(pattern)), None) if any(c in pattern for c in "*?") else (
            root / pattern if (root / pattern).exists() else None)
        if found is None:
            continue
        for tool in tools:
            out.setdefault(tool, f"the project has {found.name}")
    for tool in (t.strip() for t in extra.split(",")):
        if tool:
            out[tool] = "listed in config require_tools"
    return out


def missing(root: Path, extra: str = "") -> list[str]:
    """One line per tool that is needed and not on PATH ('' list = all there, or the check is off)."""
    if os.environ.get("SPECKFISH_SKIP_TOOL_CHECK") == "1":
        return []
    return [f"{tool} ({why})" for tool, why in needed(root, extra).items() if not _have(tool)]
