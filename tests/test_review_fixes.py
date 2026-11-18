"""Findings of the review of the implement/go work: tasks.md reading, loop guard, reconcile limits."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from test_implement_chunks import _cfg, _fake_reconcile, _fake_skill, _feature

from speckfish import cli as C
from speckfish import flow
from speckfish import steps as S
from speckfish import workspace as W
from speckfish.runner import StepResult

# ---- reading tasks.md ----

NESTED = """# Tasks
- [x] T001 top
  - [ ] sub task one
    - [ ] deep sub task
```
- [ ] T999 an example inside a code block
```
- [ ] T002 last
"""


def test_count_and_parse_agree_on_indented_subtasks_and_ignore_code_blocks(tmp_path):
    f = tmp_path / "tasks.md"
    f.write_text(NESTED, encoding="utf-8")
    tasks = W.parse_tasks(f)
    assert [t["done"] for t in tasks] == [True, False, False, False]
    assert W.count_tasks(f) == (1, 4)
    assert "T999" not in [t["id"] for t in tasks]


def test_a_task_without_a_number_keeps_its_id_when_a_line_is_inserted(tmp_path):
    f = tmp_path / "tasks.md"
    f.write_text("- [ ] first\n- [ ] second\n", encoding="utf-8")
    before = {t["text"]: t["id"] for t in W.parse_tasks(f)}
    f.write_text("- [ ] zero\n- [ ] first\n- [ ] second\n", encoding="utf-8")
    after = {t["text"]: t["id"] for t in W.parse_tasks(f)}
    assert before["second"] == after["second"] and before["first"] == after["first"]


def test_latest_record_does_not_mix_a_feature_with_a_longer_name(tmp_path):
    cfg = _cfg(tmp_path)
    run = cfg.runs_dir / "20261117-000000"
    run.mkdir(parents=True)
    (run / "state.json").write_text(json.dumps({"steps": {
        "review-001-auth": {"started_at": "2026-11-17T00:00:01"},
        "review-001-auth-roles": {"started_at": "2026-11-17T00:00:09"},
        "review-001-auth-r2": {"started_at": "2026-11-17T00:00:05"}}}), encoding="utf-8")
    assert flow.latest_record(cfg, "review", "001-auth")["key"] == "review-001-auth-r2"
    assert flow.latest_record(cfg, "review", "001-auth-roles")["key"] == "review-001-auth-roles"


# ---- the loop guard ----

def test_advance_does_not_stop_a_long_implement_at_the_design_step_cap(tmp_path, monkeypatch):
    lines = "".join(f"- [ ] T{i:03d} task {i}\n" for i in range(1, 41))
    feat = _feature(tmp_path, lines)
    cfg = _cfg(tmp_path)

    def next_step(cfg_, feature, **k):
        done, total = W.count_tasks(feature / "tasks.md")
        return (None, "done") if done >= total else ("implement", "tasks pending")

    def run_kind(cfg_, kind, feature, rid, h, **k):
        path = feature / "tasks.md"
        text = path.read_text(encoding="utf-8")
        path.write_text(text.replace("- [ ]", "- [x]", 1), encoding="utf-8")
        return StepResult(step="implement", kind="implement", ok=True, exit_code=0, duration_s=0.0)

    monkeypatch.setattr(flow, "next_step", next_step)
    monkeypatch.setattr(flow, "run_kind", run_kind)
    out = flow.advance(cfg, feat, "rid", NS(name="fake"), implement=True, chunk=1)
    assert out["stopped"] is None and len(out["steps"]) == 40 > flow.MAX_STEPS_PER_FEATURE


def test_design_steps_still_hit_the_cap(tmp_path, monkeypatch):
    feat = _feature(tmp_path)
    monkeypatch.setattr(flow, "next_step", lambda *a, **k: ("analyze", "again"))
    monkeypatch.setattr(flow, "run_kind", lambda *a, **k: StepResult(
        step="analyze", kind="analyze", ok=True, exit_code=0, duration_s=0.0))
    out = flow.advance(_cfg(tmp_path), feat, "rid", NS(name="fake"))
    assert out["stopped"] == "loop" and "design steps" in out["reason"]


# ---- reconcile ----

def test_an_unrelated_error_is_not_forgiven_by_reconcile(tmp_path, monkeypatch):
    feat = _feature(tmp_path)
    _fake_skill(monkeypatch, feat, [], ok=False, exit_code=0, error="the build is broken")
    rec = _fake_reconcile(monkeypatch, feat, ["T002"])
    res = flow.do_implement_chunk(_cfg(tmp_path), feat, "rid", NS(name="fake"), chunk=5)
    assert rec == [] and res.ok is False and res.error == "the build is broken"


def test_a_reconcile_that_touches_code_is_rejected(tmp_path, monkeypatch):
    feat = _feature(tmp_path)
    _fake_skill(monkeypatch, feat, [], ok=True)
    _fake_reconcile(monkeypatch, feat, ["T002", "T003"])
    snaps = iter([{}, {" M src/app.py": (10, 1)}])
    monkeypatch.setattr(flow, "git_snapshot", lambda root: next(snaps))
    res = flow.do_implement_chunk(_cfg(tmp_path), feat, "rid", NS(name="fake"), chunk=2)
    assert res.ok is False and "more than checkboxes" in res.error


def test_a_reconcile_that_rewrites_task_text_is_rejected(tmp_path, monkeypatch):
    feat = _feature(tmp_path)
    _fake_skill(monkeypatch, feat, [], ok=True)

    def run_step(cfg, h, label, prompt, cwd, rid, egm, **k):
        p = feat / "tasks.md"
        p.write_text(p.read_text(encoding="utf-8").replace("Add the API project", "Nothing"),
                     encoding="utf-8")
        return StepResult(step=label, kind="reconcile", ok=True, exit_code=0, duration_s=0.0)

    monkeypatch.setattr(flow, "run_step", run_step)
    monkeypatch.setattr(flow, "git_snapshot", lambda root: {})
    res = flow.do_implement_chunk(_cfg(tmp_path), feat, "rid", NS(name="fake"), chunk=2)
    assert res.ok is False and "more than checkboxes" in res.error


def test_the_reconcile_records_which_tasks_it_ticked(tmp_path, monkeypatch):
    feat = _feature(tmp_path)
    _fake_skill(monkeypatch, feat, [], ok=True)
    _fake_reconcile(monkeypatch, feat, ["T002"])
    monkeypatch.setattr(flow, "git_snapshot", lambda root: {})
    res = flow.do_implement_chunk(_cfg(tmp_path), feat, "rid", NS(name="fake"), chunk=2)
    assert res.ok is True and "reconcile ticked: T002" in res.verdict


# ---- options and fallback ----

@pytest.mark.parametrize("flag", ["--chunk", "--max-chunks"])
def test_negative_chunk_options_are_refused(flag):
    with pytest.raises(SystemExit):
        C.build_parser().parse_args(["implement", "--yes", flag, "-1"])


def test_the_loop_message_counts_the_chunks_it_ran(tmp_path, monkeypatch):
    feat = _feature(tmp_path)
    monkeypatch.setattr(flow, "do_implement_chunk", lambda cfg, feature, rid, h, *a, **k: (
        _tick_one(feature)))
    out = flow.implement_loop(_cfg(tmp_path), feat, "rid", NS(name="fake"), chunk=1, max_chunks=2)
    assert "after 2 chunk(s)" in out["reason"]


def _tick_one(feature: Path) -> StepResult:
    p = feature / "tasks.md"
    p.write_text(p.read_text(encoding="utf-8").replace("- [ ]", "- [x]", 1), encoding="utf-8")
    return StepResult(step="implement", kind="implement", ok=True, exit_code=0, duration_s=0.0)


def test_a_sticky_fallback_is_not_retried_on_the_main_harness(tmp_path, monkeypatch):
    alt = NS(name="alt")
    main = NS(name="main")
    monkeypatch.setattr(S, "_fallback_for", lambda cfg, name: alt)
    monkeypatch.setattr(S, "_needs_fallback", lambda res: res.error == "quota")
    calls = []

    def call(h):
        calls.append(h.name)
        return StepResult(step="x", kind="x", ok=h is alt, exit_code=0, duration_s=0.0,
                          error="" if h is alt else "quota")

    sticky: dict = {}
    cfg = _cfg(tmp_path)
    flow.run_with_fallback(cfg, "alt", main, call, sticky)
    flow.run_with_fallback(cfg, "alt", main, call, sticky)
    assert calls == ["main", "alt", "alt"]


def test_budget_zero_means_no_limit():
    b = flow.Budget(0)
    b.spent = 99.0
    assert b.exhausted is False


# ---- consolidated report written with one heading per finding (seen with the Codex review) ----

HEADING_REPORT = """# Consolidated Goldfish Review Report

## Verdict

The plan is not implementation-ready. Four blockers can violate required invariants.

## Blockers

### B1. Lifecycle transitions serialize on different locks
Text with the word unit and monitoring.

### B2. Retry rules can duplicate incidents
More text.

## Major Findings

### M1. Transport retries conflict
### M2. Pipeline order is not implementable

## Nits

### N1. Wording
"""


def test_a_report_with_one_heading_per_finding_is_counted(tmp_path):
    from speckfish.config import parse_consolidated
    f = tmp_path / "consolidated_review_report.md"
    f.write_text(HEADING_REPORT, encoding="utf-8")
    out = parse_consolidated(f)
    assert [b.split(".")[0] for b in out["blockers"]] == ["B1", "B2"]
    assert out["majors"] == 2 and out["nits"] == 1
    assert out["meets_standard"] is False
    assert out["verdict"].startswith("The plan is not implementation-ready")


def test_a_word_inside_a_heading_does_not_switch_the_severity(tmp_path):
    from speckfish.config import parse_consolidated
    f = tmp_path / "r.md"
    f.write_text("## Blockers\n\n### B1. Unit tests and monitoring gaps\n\n### B2. Limit rules\n",
                 encoding="utf-8")
    out = parse_consolidated(f)
    assert len(out["blockers"]) == 2 and out["nits"] == 0


# ---- blockers are read again from the report, not trusted from state.json ----

def _record_review(cfg, feat, artifact_dir, blockers=0, majors=0):
    run = cfg.runs_dir / "20261118-000000"
    run.mkdir(parents=True, exist_ok=True)
    (run / "state.json").write_text(json.dumps({"steps": {f"review-{feat.name}": {
        "started_at": "2999-01-01T00:00:00", "status": "ok", "blockers": blockers, "majors": majors,
        "artifact_dir": str(artifact_dir)}}}), encoding="utf-8")


def test_blockers_saved_as_zero_are_counted_again_from_the_report(tmp_path):
    feat = _feature(tmp_path)
    art = tmp_path / "art"
    art.mkdir()
    (art / "consolidated_review_report.md").write_text(HEADING_REPORT, encoding="utf-8")
    cfg = _cfg(tmp_path)
    _record_review(cfg, feat, art)                         # an older speckfish recorded 0
    st = flow.feature_state(cfg, feat)
    assert st["blockers"] == 2 and st["majors"] == 2
    kind, why = flow.next_step(cfg, feat)
    assert kind == "revise" and "2 blocker" in why


def test_majors_only_trigger_a_revision_when_asked(tmp_path):
    feat = _feature(tmp_path)
    art = tmp_path / "art"
    art.mkdir()
    (art / "consolidated_review_report.md").write_text(
        "## Major Findings\n\n### M1. One\n### M2. Two\n", encoding="utf-8")
    cfg = _cfg(tmp_path)
    _record_review(cfg, feat, art)
    assert flow.next_step(cfg, feat)[0] is None
    kind, why = flow.next_step(cfg, feat, majors=True)
    assert kind == "revise" and "2 major" in why
