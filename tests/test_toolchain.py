"""`implement` checks the project's build tools first (a real run found out about a missing JDK 17 minutes in)."""
from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from speckfish import cli as C
from speckfish import flow, toolchain
from speckfish import steps as S
from speckfish.config import Config
from speckfish.runner import StepResult


@pytest.fixture
def check(monkeypatch):
    monkeypatch.delenv("SPECKFISH_SKIP_TOOL_CHECK", raising=False)
    monkeypatch.delenv("JAVA_HOME", raising=False)
    monkeypatch.setattr(toolchain.shutil, "which", lambda t, path=None: None)


def test_the_project_files_say_what_is_needed(tmp_path):
    (tmp_path / "gradlew").write_text("x")
    (tmp_path / "package.json").write_text("{}")
    (tmp_path / "App.csproj").write_text("x")
    got = toolchain.needed(tmp_path, "adb, ")
    assert set(got) == {"java", "node", "npm", "dotnet", "adb"}
    assert got["adb"] == "listed in config require_tools" and got["java"] == "the project has gradlew"
    assert toolchain.needed(tmp_path / "empty") == {}


def test_missing_lists_only_what_is_not_there(tmp_path, check, monkeypatch):
    (tmp_path / "gradlew").write_text("x")
    assert toolchain.missing(tmp_path) == ["java (the project has gradlew)"]
    monkeypatch.setattr(toolchain.shutil, "which", lambda t, path=None: "/bin/x" if t == "java" else None)
    assert toolchain.missing(tmp_path) == []
    monkeypatch.setenv("SPECKFISH_SKIP_TOOL_CHECK", "1")
    assert toolchain.missing(tmp_path, "nothing-at-all") == []


def test_java_home_counts(tmp_path, check, monkeypatch):
    (tmp_path / "gradlew").write_text("x")
    monkeypatch.setenv("JAVA_HOME", str(tmp_path / "jdk"))
    monkeypatch.setattr(toolchain.shutil, "which", lambda t, path=None: "/jdk/bin/java" if path else None)
    assert toolchain.missing(tmp_path) == []


def _feature(root):
    d = root / "specs" / "001-a"
    d.mkdir(parents=True)
    (d / "tasks.md").write_text("# T\n- [ ] T001 a\n", encoding="utf-8")
    return d


def test_implement_does_not_start_without_the_tools(tmp_path, check, monkeypatch):
    (tmp_path / "gradlew").write_text("x")
    feat = _feature(tmp_path)
    ran = []
    monkeypatch.setattr(flow, "do_implement_chunk", lambda *a, **k: ran.append(1))
    out = flow.implement_loop(Config(root=tmp_path), feat, "rid", NS(name="fake"))
    assert not ran and out["stopped"] == "blocked" and out["error_kind"] == "blocked"
    assert "not installed: java (the project has gradlew)" in out["reason"] and "nothing was started" in out["reason"]
    assert C.flow_exit_code({"ok": False, "features": {"a": out}}) == C.EXIT_ENV
    # --dry-run only shows the command: the check does not apply
    monkeypatch.setattr(flow, "do_implement_chunk", lambda *a, **k: StepResult(
        step="x", kind="implement", ok=True, exit_code=0, duration_s=0.0, error="dry-run"))
    assert flow.implement_loop(Config(root=tmp_path), feat, "rid", NS(name="fake"), dry_run=True)["stopped"] is None


def test_nothing_to_do_is_not_blocked_by_a_missing_tool(tmp_path, check):
    (tmp_path / "gradlew").write_text("x")
    feat = _feature(tmp_path)
    (feat / "tasks.md").write_text("# T\n- [x] T001 a\n", encoding="utf-8")
    assert flow.implement_loop(Config(root=tmp_path), feat, "rid", NS(name="fake"))["stopped"] is None


def test_doctor_warns(tmp_path, check):
    (tmp_path / "gradlew").write_text("x")
    info = S.do_doctor(Config(root=tmp_path))
    assert info["project"]["build_tools_missing"] == ["java (the project has gradlew)"]
    assert "build tools not installed: java (the project has gradlew)" in C.doctor_summary(info)
