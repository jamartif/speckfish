"""Phase 2 (PLAN.md): kind vs label, step registry, one batch runner, command handler table."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from speckfish import cli as C
from speckfish import registry as REG
from speckfish import runner as R
from speckfish import steps as S
from speckfish import workspace as W
from speckfish.config import DEFAULTS, Config
from speckfish.prompts import SKILL_SPECS
from speckfish.runner import StepResult


def _cfg(tmp_path) -> Config:
    return Config(root=tmp_path.resolve())


def _feature(root: Path, name: str) -> Path:
    d = root / "specs" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "spec.md").write_text("# spec", encoding="utf-8")
    return d


# ---- kind and label are different things ----

def test_a_batch_label_uses_the_budget_and_timeout_of_its_kind(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.timeouts["review"] = 4321
    seen = {}

    def build(**k):
        seen.update(k)
        return NS(argv=["x"], env={})

    h = NS(name="fake", build=build)
    res = R.run_step(cfg, h, "review-001-foo", "p", tmp_path, "rid", None, dry_run=True,
                     kind="review")
    assert seen["kind"] == "review"                 # the budget table is keyed by kind
    assert seen["timeout"] == 4321                  # and so is the timeout
    assert res.kind == "review" and res.step == "review-001-foo"
    assert res.artifact_dir.endswith("artifacts/review-001-foo")


def test_kind_is_derived_from_a_legacy_label(tmp_path):
    cfg = _cfg(tmp_path)
    seen = {}
    h = NS(name="fake", build=lambda **k: seen.update(k) or NS(argv=["x"], env={}))
    res = R.run_step(cfg, h, "drift-002-bar", "p", tmp_path, "rid", None, dry_run=True)
    assert seen["kind"] == "drift" and res.kind == "drift"


def test_exit_for_reads_the_kind_not_the_label():
    blocked = {"step": "review-001-foo", "kind": "review", "ok": True, "blockers": ["b"]}
    assert C.exit_for(blocked) == C.EXIT_BLOCKERS
    assert C.exit_for({"step": "plan-001-foo", "kind": "plan", "ok": True, "blockers": ["b"]}) == C.EXIT_OK


# ---- the registry knows every step ----

def test_every_spec_kit_skill_is_in_the_registry():
    assert set(SKILL_SPECS) <= set(REG.STEPS)


def test_every_kind_has_a_timeout_and_a_known_schema():
    from speckfish.prompts import SCHEMAS
    for kind, spec in REG.STEPS.items():
        assert REG.timeout_key(kind) in DEFAULTS["timeouts"], kind
        assert not spec.schema or spec.schema in SCHEMAS, kind


def test_destructive_steps_match_the_prompts():
    registry = {k for k, v in REG.STEPS.items() if v.destructive}
    prompts_ = {k for k, v in SKILL_SPECS.items() if v.get("dangerous")}
    assert registry == prompts_


def test_requirements_form_a_chain():
    assert REG.STEPS["plan"].requires == ("spec.md",)
    assert REG.STEPS["tasks"].requires == (REG.STEPS["plan"].produces,)
    assert REG.STEPS["implement"].requires == (REG.STEPS["tasks"].produces,)


def test_missing_output_after_an_idle_close(tmp_path):
    feat = _feature(tmp_path, "001-a")
    res = StepResult(step="plan", ok=True, exit_code=0, duration_s=1.0)
    assert "no plan.md" in REG.missing_output("plan", tmp_path, res, feat)
    (feat / "plan.md").write_text("# p", encoding="utf-8")
    assert REG.missing_output("plan", tmp_path, res, feat) == ""
    rev = StepResult(step="review", ok=True, exit_code=0, duration_s=1.0)
    assert REG.missing_output("review", tmp_path, rev, feat) == "no report was written"
    rev.reports = ["r.md"]
    assert REG.missing_output("review", tmp_path, rev, feat) == ""


def test_workspace_feature_dir_prefers_the_active_feature(tmp_path):
    old = _feature(tmp_path, "001-a")
    _feature(tmp_path, "002-b")
    assert W.feature_dir(tmp_path).name == "002-b"          # nothing active: the newest
    (tmp_path / ".specify").mkdir()
    (tmp_path / ".specify" / "feature.json").write_text(
        '{"feature_directory": "specs/001-a"}', encoding="utf-8")
    assert W.feature_dir(tmp_path) == tmp_path / "specs" / old.name


# ---- one batch runner for review, drift and the Spec Kit steps ----

def _ok(label: str, cost: float | None = None) -> StepResult:
    return StepResult(step=label, ok=True, exit_code=0, duration_s=1.0, cost=cost)


def test_run_batch_stops_at_the_first_failure_and_sums_cost(tmp_path):
    cfg = _cfg(tmp_path)
    feats = [_feature(tmp_path, "001-a"), _feature(tmp_path, "002-b"), _feature(tmp_path, "003-c")]
    (cfg.runs_dir / "rid").mkdir(parents=True)
    calls = []

    def run_one(h, f):
        calls.append(f.name)
        if f.name == "002-b":
            return StepResult(step="x", ok=False, exit_code=1, duration_s=1.0, cost=2.0)
        return _ok("x", cost=1.0)

    out = S.run_batch(cfg, "plan", feats, NS(name="fake"), "rid", run_one)
    assert calls == ["001-a", "002-b"]
    assert out["stopped_at"] == "002-b" and out["done"] is False
    assert out["total_cost"] == 3.0
    assert (cfg.runs_dir / "rid" / "batch.json").exists()


def test_run_batch_skips_and_prepares(tmp_path):
    cfg = _cfg(tmp_path)
    feats = [_feature(tmp_path, "001-a"), _feature(tmp_path, "002-b")]
    (cfg.runs_dir / "rid").mkdir(parents=True)
    prepared, ran = [], []
    out = S.run_batch(cfg, "tasks", feats, NS(name="fake"), "rid",
                      lambda h, f: ran.append(f.name) or _ok("t"),
                      skip=lambda f: f.name == "001-a", prepare=lambda f: prepared.append(f.name),
                      detail_file="batch-x.json")
    assert out["skipped"] == ["001-a"] and ran == prepared == ["002-b"]
    assert out["done"] is True and out["detail_file"] == "batch-x.json"


def test_run_batch_retries_once_with_the_fallback_harness(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    feats = [_feature(tmp_path, "001-a")]
    (cfg.runs_dir / "rid").mkdir(parents=True)
    monkeypatch.setattr(S, "get_harness", lambda cfg_, name=None: NS(name="codex"))
    used = []

    def run_one(h, f):
        used.append(h.name)
        if h.name == "claude":
            return StepResult(step="x", ok=False, exit_code=1, duration_s=1.0, error_kind="quota")
        return _ok("x")

    out = S.run_batch(cfg, "review", feats, NS(name="claude"), "rid", run_one, fallback="codex")
    assert used == ["claude", "codex"]
    assert out["switched"] == ["001-a -> codex"] and out["done"] is True


# ---- the command table ----

def test_every_subcommand_has_a_handler():
    parser = C.build_parser()
    choices = next(a for a in parser._actions if getattr(a, "choices", None)).choices
    assert set(choices) <= set(C.HANDLERS), set(choices) - set(C.HANDLERS)


@pytest.fixture
def project(tmp_path):
    (tmp_path / ".speckfish").mkdir()
    (tmp_path / ".speckfish" / "config.json").write_text("{}", encoding="utf-8")
    return tmp_path


def test_a_plain_review_runs_the_review(project, monkeypatch):
    """`speckfish review` with no flags used to fall through every branch and exit 2 silently."""
    _feature(project, "001-a")
    (project / "specs" / "001-a" / "plan.md").write_text("# p", encoding="utf-8")
    seen = []
    monkeypatch.setattr(C, "get_harness", lambda cfg, name=None: NS(name="fake"))
    monkeypatch.setattr(S, "do_review", lambda cfg, target, *a, **k:
                        seen.append(target) or _ok("review"))
    code = C.main(["--root", str(project), "--json", "review"])
    assert code == C.EXIT_OK
    assert seen == [None]                    # the default target is resolved inside do_review


def test_a_handler_error_is_an_environment_exit(project, capsys, monkeypatch):
    monkeypatch.setattr(C, "get_harness", lambda cfg, name=None: NS(name="fake"))
    code = C.main(["--root", str(project), "--json", "review", "missing/plan.md"])
    assert code == C.EXIT_ENV
    assert "does not exist" in capsys.readouterr().out
