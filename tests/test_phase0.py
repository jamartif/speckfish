"""Regression tests for the Phase 0 fixes (see PLAN.md): batches, labels, prompts and exit codes."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from speckfish import cli as C
from speckfish import prompts as P
from speckfish import runner as R
from speckfish import steps as S
from speckfish.config import Config
from speckfish.harnesses import CodexHarness
from speckfish.runner import StepResult

REPORT = """# Consolidated review

Veredicto: no cumple el estándar.

## Blockers
- Falta la estrategia de pruebas

## Majors
- Un major
"""


def _cfg(tmp_path) -> Config:
    return Config(root=tmp_path.resolve())


def _feature(root: Path, name: str, plan: bool = False) -> Path:
    d = root / "specs" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "spec.md").write_text("# spec", encoding="utf-8")
    if plan:
        (d / "plan.md").write_text("# plan", encoding="utf-8")
    return d


def _fake_harness(script: str) -> NS:
    return NS(name="fake", parse_output=lambda out, err, code: {},
              build=lambda **k: NS(argv=[sys.executable, "-c", script], env={}))


# ---- 1 · a batch label must still be validated as its kind of step ----

def test_step_kind_strips_the_feature_from_a_batch_label():
    assert R.step_kind("review-001-foo") == "review"
    assert R.step_kind("drift-002-bar") == "drift"
    assert R.step_kind("specify-3") == "specify"
    assert R.step_kind("implement") == "implement"
    assert R.step_kind("specify-split") == "split"


def test_a_batch_review_reads_its_blockers(tmp_path):
    art = tmp_path / "art"
    art.mkdir()
    (art / "consolidated_review_report.md").write_text(REPORT, encoding="utf-8")
    res = StepResult(step="review-001-foo", ok=True, exit_code=0, duration_s=1.0)
    R.postprocess(res, "review-001-foo", art, tmp_path)
    assert res.blockers and "pruebas" in res.blockers[0]
    assert C.exit_for(StepResult(step="review", ok=True, exit_code=0, duration_s=1.0,
                                 blockers=res.blockers).to_dict()) == C.EXIT_BLOCKERS


def test_a_batch_drift_without_a_report_fails(tmp_path):
    art = tmp_path / "art"
    art.mkdir()
    res = StepResult(step="drift-001-foo", ok=True, exit_code=0, duration_s=1.0)
    R.postprocess(res, "drift-001-foo", art, tmp_path)
    assert res.ok is False and "drift report" in res.error


def test_a_batch_specify_without_a_spec_fails(tmp_path):
    res = StepResult(step="specify-2", ok=True, exit_code=0, duration_s=1.0)
    R.postprocess(res, "specify-2", tmp_path / "art", tmp_path)
    assert res.ok is False and "wrote no spec.md" in res.error


def test_the_split_inventory_is_not_a_spec(tmp_path):
    res = StepResult(step="specify-split", ok=True, exit_code=0, duration_s=1.0)
    R.postprocess(res, "specify-split", tmp_path / "art", tmp_path)
    assert res.ok is True


# ---- 2 · drift writes where run_step looks ----

def test_drift_artifact_dir_follows_the_step_label(tmp_path):
    cfg = _cfg(tmp_path)
    feat = _feature(tmp_path, "001-a")
    res = S.do_drift(cfg, str(feat), "rid", CodexHarness(cfg), dry_run=True,
                     step_name="drift-001-a")
    assert res.artifact_dir.endswith("artifacts/drift-001-a")
    assert "artifacts/drift-001-a" in res.log          # the prompt tells the agent the same folder


# ---- 3 · one log per label ----

def test_each_label_gets_its_own_log_and_state_entry(tmp_path):
    cfg = _cfg(tmp_path)
    h = _fake_harness("print('hi')")
    a = R.run_step(cfg, h, "plan", "p", tmp_path, "rid", None, progress=False,
                   artifact_name="plan-001-a")
    b = R.run_step(cfg, h, "plan", "p", tmp_path, "rid", None, progress=False,
                   artifact_name="plan-002-b")
    logs = cfg.runs_dir / "rid" / "logs"
    assert (logs / "plan-001-a.log").exists() and (logs / "plan-002-b.log").exists()
    assert a.log.endswith("plan-001-a.log") and Path(a.log).exists()
    assert b.log.endswith("plan-002-b.log") and Path(b.log).exists()
    steps = S.run_summary(cfg, "rid")["steps"]
    assert {"plan-001-a", "plan-002-b"} <= set(steps)


# ---- 4/6 · split: one review per spec, prompts for the harness in use ----

def test_split_reviews_do_not_overwrite_each_other(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _feature(tmp_path, "001-a")
    (cfg.runs_dir / "rid").mkdir(parents=True)
    seen_prompts, review_labels = [], []

    def fake_run_step(cfg, h, step, prompt, *a, **k):
        seen_prompts.append(prompt)
        return StepResult(step=step, ok=True, exit_code=0, duration_s=1.0)

    def fake_review(cfg, target, rid, h, *a, **k):
        review_labels.append(k.get("step_name"))
        return StepResult(step="review", ok=True, exit_code=0, duration_s=1.0)

    monkeypatch.setattr(S, "run_step", fake_run_step)
    monkeypatch.setattr(S, "do_review", fake_review)
    pairs = [(1, {"title": "a", "description": "x"}), (2, {"title": "b", "description": "y"})]
    S._specify_each(cfg, CodexHarness(cfg), "rid", pairs, {"steps": {}, "specs": []}, review=True)
    assert review_labels == ["review-1", "review-2"]
    assert all("/speckit-specify" not in p for p in seen_prompts)   # codex has no slash commands


# ---- 5 · specify/plan prompts for harnesses without slash commands ----

def test_specify_prompt_for_codex_inlines_the_file_and_language(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.language = "es"
    (tmp_path / "brief.md").write_text("# Mi brief\nUna CLI de tareas", encoding="utf-8")
    prompt = S.specify_prompt_for(cfg, CodexHarness(cfg), "@brief.md")
    assert "Una CLI de tareas" in prompt and "@brief.md" not in prompt.split("<<<")[0].split("from")[0]
    assert "Spanish" in prompt


# ---- 7 · an agent closed by inactivity is judged by what it wrote ----

def test_idle_close_keeps_a_specify_that_wrote_its_spec(tmp_path, monkeypatch):
    monkeypatch.setenv("SPECKFISH_IDLE_SECONDS", "2")
    cfg = _cfg(tmp_path)
    script = ("import pathlib, time; d = pathlib.Path('specs/001-a'); d.mkdir(parents=True); "
              "(d / 'spec.md').write_text('# s'); time.sleep(40)")
    res = R.run_step(cfg, _fake_harness(script), "specify", "p", tmp_path, "rid", None,
                     progress=False)
    assert res.ok is True and "stayed alive" in res.error


def test_idle_close_fails_a_plan_that_wrote_no_plan(tmp_path, monkeypatch):
    monkeypatch.setenv("SPECKFISH_IDLE_SECONDS", "2")
    cfg = _cfg(tmp_path)
    _feature(tmp_path, "001-a")
    res = R.run_step(cfg, _fake_harness("import time; time.sleep(40)"), "plan", "p", tmp_path,
                     "rid", None, progress=False)
    assert res.ok is False and "no plan.md" in res.error


# ---- 8 · implement is judged by what changed during the step ----

def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(root), check=True, capture_output=True)


def test_a_repo_that_was_already_dirty_is_not_an_implementation(tmp_path):
    _git(tmp_path, "init")
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.py").write_text("x = 1\n", encoding="utf-8")
    before = R.git_snapshot(tmp_path)
    assert before                                   # the repo was already dirty
    assert R._git_dirty(tmp_path, before) is False  # the step changed nothing
    (src / "a.py").write_text("x = 12345\n", encoding="utf-8")
    assert R._git_dirty(tmp_path, before) is True   # the step edited a file that was already dirty
    before = R.git_snapshot(tmp_path)
    (src / "b.py").write_text("y = 2\n", encoding="utf-8")
    assert R._git_dirty(tmp_path, before) is True   # a new file


# ---- 9 · no control characters hiding in the sources ----

def test_sources_have_no_control_characters():
    for path in (Path(__file__).parent.parent / "src" / "speckfish").glob("*.py"):
        bad = {ch for ch in path.read_text(encoding="utf-8")
               if ord(ch) < 32 and ch not in "\n\r\t"}
        assert not bad, f"{path.name} contains {sorted(map(ord, bad))}"


# ---- 10 · Spec Kit skills are read from where the integration installs them ----

@pytest.mark.parametrize("harness,folder", [("copilot", ".github/skills"), ("codex", ".agents/skills")])
def test_skill_prompt_points_at_the_installed_skill(harness, folder, tmp_path):
    text = P.skill_prompt_for(harness, "tasks", "", tmp_path / "art", tmp_path, tmp_path)
    assert f"{folder}/speckit-tasks/SKILL.md" in text
    assert "docs/" not in text


# ---- 11 · options that cannot work together say so ----

@pytest.fixture
def project(tmp_path):
    (tmp_path / ".speckfish").mkdir()
    (tmp_path / ".speckfish" / "config.json").write_text("{}", encoding="utf-8")
    return tmp_path


def test_plan_with_clarify_refuses_a_batch(project, capsys):
    code = C.main(["--root", str(project), "plan", "x", "--with-clarify", "--all-features"])
    assert code == C.EXIT_ENV
    assert "one feature at a time" in capsys.readouterr().err


def test_review_with_analyze_refuses_a_batch(project, capsys):
    code = C.main(["--root", str(project), "review", "--with-analyze", "--only", "1"])
    assert code == C.EXIT_ENV
    assert "one feature at a time" in capsys.readouterr().err


def test_review_with_analyze_and_no_target_analyzes_the_current_feature(project, monkeypatch):
    feat = _feature(project, "001-a", plan=True)
    calls = []
    monkeypatch.setattr(C, "get_harness", lambda cfg, name=None: NS(name="fake"))
    monkeypatch.setattr(S, "do_skill", lambda cfg, skill, args, rid, h, stream, dry, feature=None, **k:
                        calls.append(("analyze", feature)) or
                        StepResult(step="analyze", ok=True, exit_code=0, duration_s=1.0))
    monkeypatch.setattr(S, "do_review", lambda cfg, target, *a, **k:
                        calls.append(("review", Path(target).parent.name)) or
                        StepResult(step="review", ok=True, exit_code=0, duration_s=1.0))
    code = C.main(["--root", str(project), "--json", "review", "--with-analyze"])
    assert code == C.EXIT_OK
    assert calls == [("analyze", feat.name), ("review", feat.name)]


# ---- 12 · every step defaults to the same feature ----

def test_review_defaults_to_the_active_feature_not_the_newest(tmp_path):
    cfg = _cfg(tmp_path)
    old = _feature(tmp_path, "001-a", plan=True)
    new = _feature(tmp_path, "002-b", plan=True)
    import os
    base = old.stat().st_mtime
    os.utime(old, (base, base))                  # latest_feature orders by the folder's mtime
    os.utime(new, (base + 100, base + 100))
    S.set_active_feature(cfg, old)
    assert S.current_feature(cfg) == old.resolve()
    assert S.resolve_target(cfg, None, "plan.md").parent == old.resolve()
    (tmp_path / ".specify" / "feature.json").unlink()
    assert S.current_feature(cfg) == new                    # nothing active: the newest one


# ---- 13 · quota / auth are not guessed from digits and ordinary words ----

@pytest.mark.parametrize("err,kind", [
    ("HTTP 429 Too Many Requests", "quota"),
    ("error: status 429", "quota"),
    ("compiled 1429 files; updated the credit card form", "unknown"),
    ("listening on port 4290", "unknown"),
    ("insufficient credits", "auth"),
    ("not logged in", "auth"),
])
def test_error_kind_needs_context(err, kind):
    assert R.error_kind("", err) == kind
