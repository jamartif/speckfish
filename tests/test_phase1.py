"""Phase 1 (PLAN.md): status, next, revise and go — derived from files and recorded steps."""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from speckfish import cli as C
from speckfish import flow
from speckfish import runner as R
from speckfish import steps as S
from speckfish.config import Config
from speckfish.runner import StepResult

OLD = time.time() - 10_000          # design documents written long before any recorded step
RID = "20260101-000000"


def _cfg(tmp_path) -> Config:
    return Config(root=tmp_path.resolve())


def _write(path: Path, text: str, mtime: float = OLD) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    os.utime(path, (mtime, mtime))


def _feature(root: Path, name: str = "001-a", **docs: str) -> Path:
    d = root / "specs" / name
    _write(d / "spec.md", "# spec")
    for fname, text in docs.items():
        _write(d / fname.replace("_", "."), text)
    return d


def _record(cfg: Config, label: str, ok: bool = True, blockers: int = 0, cost: float | None = None,
            when: float | None = None, rid: str = RID) -> None:
    """What a finished step leaves in state.json."""
    res = StepResult(step=label, ok=ok, exit_code=0 if ok else 1, duration_s=1.0, cost=cost,
                     blockers=["b"] * blockers)
    started = datetime.fromtimestamp(when or time.time() - 5_000).isoformat(timespec="seconds")
    R.record_state(cfg, R.run_dir_for(cfg, rid), rid, res, label, NS(name="fake"), started)


# ---- next_step reads files and recorded steps ----

def test_next_step_follows_the_chain(tmp_path):
    cfg = _cfg(tmp_path)
    feat = _feature(tmp_path)
    assert flow.next_step(cfg, feat)[0] == "plan"
    _write(feat / "plan.md", "# p")
    assert flow.next_step(cfg, feat)[0] == "tasks"
    _write(feat / "tasks.md", "- [ ] T1\n- [ ] T2\n")
    assert flow.next_step(cfg, feat)[0] == "analyze"
    _record(cfg, "analyze-001-a")
    assert flow.next_step(cfg, feat)[0] == "review"
    _record(cfg, "review-001-a", blockers=2)
    kind, why = flow.next_step(cfg, feat)
    assert kind == "revise" and "2 blocker" in why
    _record(cfg, "review-001-a-r2", blockers=0, when=time.time() - 100)
    kind, why = flow.next_step(cfg, feat)
    assert kind is None and "--implement" in why
    assert flow.next_step(cfg, feat, implement=True)[0] == "implement"


def test_an_edit_after_the_review_makes_it_stale(tmp_path):
    cfg = _cfg(tmp_path)
    feat = _feature(tmp_path, plan_md="# p", tasks_md="- [ ] T1\n")
    _record(cfg, "analyze-001-a")
    _record(cfg, "review-001-a", blockers=0)
    assert flow.next_step(cfg, feat)[0] is None
    _write(feat / "plan.md", "# p revised", mtime=time.time())
    assert flow.next_step(cfg, feat)[0] == "review"


def test_ticking_tasks_is_progress_not_a_design_change(tmp_path):
    cfg = _cfg(tmp_path)
    feat = _feature(tmp_path, plan_md="# p", tasks_md="- [ ] T1\n- [ ] T2\n")
    _record(cfg, "analyze-001-a")
    _record(cfg, "review-001-a", blockers=0)
    _write(feat / "tasks.md", "- [x] T1\n- [ ] T2\n", mtime=time.time())
    assert flow.next_step(cfg, feat, implement=True)[0] == "implement"     # not "review"
    _write(feat / "tasks.md", "- [x] T1\n- [x] T2\n", mtime=time.time())
    assert flow.next_step(cfg, feat, implement=True)[0] == "drift"
    _record(cfg, "drift-001-a")
    assert flow.next_step(cfg, feat, implement=True) == (None, "done")


def test_a_failed_review_does_not_count(tmp_path):
    cfg = _cfg(tmp_path)
    feat = _feature(tmp_path, plan_md="# p", tasks_md="- [ ] T1\n")
    _record(cfg, "analyze-001-a")
    _record(cfg, "review-001-a", ok=False)
    assert flow.next_step(cfg, feat)[0] == "review"


def test_status_rows_and_total_cost(tmp_path):
    cfg = _cfg(tmp_path)
    _feature(tmp_path, "001-a", plan_md="# p", tasks_md="- [x] T1\n- [ ] T2\n")
    _feature(tmp_path, "002-b")
    _record(cfg, "plan-001-a", cost=1.5)
    _record(cfg, "tasks-001-a", cost=0.5)
    rows = flow.status_rows(cfg)
    assert [r["name"] for r in rows] == ["001-a", "002-b"]
    assert rows[0]["tasks_done"] == 1 and rows[0]["tasks_total"] == 2
    assert rows[1]["next"]["kind"] == "plan"
    assert rows[1]["next"]["command"] == "speckfish plan --feature 002-b"
    assert flow.total_cost(cfg) == {"usd": 2.0}
    text = C.status_summary(rows, {"usd": 2.0, "credits": 4.5})
    assert "001-a" in text and "->  speckfish plan --feature 002-b" in text and "spent so far: 2 usd + 4.5 credits" in text


# ---- revise ----

def test_revise_prompt_names_the_report_and_forbids_code(tmp_path):
    text = flow.revise_prompt(tmp_path / "specs" / "001-a", tmp_path / "r.md")
    assert "r.md" in text and "Do not write source code" in text and "every Blocker" in text
    assert "and every Major" in flow.revise_prompt(tmp_path, tmp_path / "r.md", majors=True)


def _report(cfg: Config, name: str) -> Path:
    path = cfg.runs_dir / RID / "artifacts" / f"review-{name}" / "consolidated_review_report.md"
    path.parent.mkdir(parents=True)
    path.write_text("## Blockers\n- x\n", encoding="utf-8")
    return path


def test_revise_without_a_report_says_to_review_first(tmp_path):
    cfg = _cfg(tmp_path)
    feat = _feature(tmp_path, plan_md="# p")
    res = flow.do_revise(cfg, feat, RID, NS(name="fake"))
    assert res.ok is False and "run `speckfish review` first" in res.error


def _fake_agent(script: str) -> NS:
    return NS(name="fake", parse_output=lambda out, err, code: {},
              build=lambda **k: NS(argv=[sys.executable, "-c", script], env={}))


def test_revise_fails_when_no_document_changed(tmp_path):
    cfg = _cfg(tmp_path)
    feat = _feature(tmp_path, plan_md="# p")
    _report(cfg, feat.name)
    res = flow.do_revise(cfg, feat, RID, _fake_agent("print('fixed it')"))
    assert res.ok is False and "changed no document" in res.error


def test_revise_passes_when_a_document_changed(tmp_path):
    cfg = _cfg(tmp_path)
    feat = _feature(tmp_path, plan_md="# p")
    _report(cfg, feat.name)
    script = f"open(r'{feat / 'plan.md'}', 'w').write('# plan, revised')"
    res = flow.do_revise(cfg, feat, RID, _fake_agent(script))
    assert res.ok is True and res.kind == "revise"
    assert (cfg.runs_dir / RID / "state.json").exists()


def test_labels_stay_unique_within_a_run(tmp_path):
    cfg = _cfg(tmp_path)
    assert flow.unique_label(cfg, RID, "review-001-a") == "review-001-a"
    _record(cfg, "review-001-a")
    assert flow.unique_label(cfg, RID, "review-001-a") == "review-001-a-r2"
    _record(cfg, "review-001-a-r2")
    assert flow.unique_label(cfg, RID, "review-001-a") == "review-001-a-r3"


# ---- advance: the loop, with fake steps that write what the real ones write ----

class FakeFlow:
    """Stands in for the agent steps: each writes its output and records itself like run_step does."""

    def __init__(self, cfg: Config, monkeypatch, blockers: list[int], cost: float = 1.0,
                 drift_found: bool = False):
        self.cfg, self.calls, self.blockers = cfg, [], list(blockers)
        self.cost, self.drift_found = cost, drift_found
        self.t = time.time() - 5_000          # a fake clock, so "before" and "after" are unambiguous
        for kind, fn in (("plan", self.plan), ("review", self.review), ("drift", self.drift),
                         ("skill", self.skill)):
            monkeypatch.setattr(S, {"plan": "do_plan", "review": "do_review", "drift": "do_drift",
                                    "skill": "do_skill"}[kind], fn)
        monkeypatch.setattr(flow, "do_revise", self.revise)

    def _done(self, kind, label, feat: Path | None, **extra) -> StepResult:
        self.calls.append(kind)
        ok = extra.pop("ok", True)
        res = StepResult(step=label, kind=kind, ok=ok, exit_code=0 if ok else 1, duration_s=1.0,
                         cost=self.cost, **extra)
        R.record_state(self.cfg, R.run_dir_for(self.cfg, RID), RID, res, label, NS(name="fake"),
                       datetime.fromtimestamp(self.t).isoformat(timespec="seconds"))
        return res

    def _feat(self, name: str) -> Path:
        return self.cfg.root / "specs" / name

    def plan(self, cfg, stack, rid, h, stream, dry, feature=None, step_name=None):
        _write(self._feat(feature) / "plan.md", "# p")
        return self._done("plan", step_name, None)

    def skill(self, cfg, skill, args, rid, h, stream, dry, feature=None, step_name=None):
        feat = self._feat(feature)
        if skill == "tasks":
            _write(feat / "tasks.md", "- [ ] T1\n- [ ] T2\n")
        if skill == "converge":
            _write(feat / "tasks.md", "- [x] T1\n- [x] T2\n- [ ] T3\n", mtime=self.t)
        if skill == "implement":
            _write(feat / "tasks.md", "- [x] T1\n- [x] T2\n- [x] T3\n", mtime=self.t)
        return self._done(skill, step_name, feat)

    def review(self, cfg, target, rid, h, **k):
        n = self.blockers.pop(0) if self.blockers else 0
        self.t += 10                           # the review starts after every edit so far
        return self._done("review", k["step_name"], None, blockers=["b"] * n)

    def revise(self, cfg, feature, rid, h, majors=False, stream=True, dry_run=False):
        self.t += 10
        _write(feature / "plan.md", "# p revised", mtime=self.t)
        return self._done("revise", flow.unique_label(cfg, rid, f"revise-{feature.name}"), feature)

    def drift(self, cfg, target, rid, h, **k):
        res = self._done("drift", k["step_name"], None)
        res.structured = {"drift_found": self.drift_found}
        return res


def _prepare(tmp_path, monkeypatch, **kw):
    cfg = _cfg(tmp_path)
    feat = _feature(tmp_path)
    fake = FakeFlow(cfg, monkeypatch, **kw)
    return cfg, feat, fake


def test_advance_plans_reviews_revises_and_reviews_again(tmp_path, monkeypatch):
    cfg, feat, fake = _prepare(tmp_path, monkeypatch, blockers=[2, 0])
    out = flow.advance(cfg, feat, RID, NS(name="fake"))
    assert fake.calls == ["plan", "tasks", "analyze", "review", "revise", "review"]
    assert out["stopped"] is None and "--implement" in out["reason"]
    assert flow.feature_state(cfg, feat)["blockers"] == 0


def test_advance_gives_up_after_max_rounds(tmp_path, monkeypatch):
    cfg, feat, fake = _prepare(tmp_path, monkeypatch, blockers=[3, 3, 3, 3])
    out = flow.advance(cfg, feat, RID, NS(name="fake"), max_rounds=1)
    assert fake.calls.count("revise") == 1
    assert out["stopped"] == "revise" and "1 revision round" in out["reason"]


def test_advance_implements_then_checks_drift(tmp_path, monkeypatch):
    cfg, feat, fake = _prepare(tmp_path, monkeypatch, blockers=[0])
    flow.advance(cfg, feat, RID, NS(name="fake"), implement=True)
    assert fake.calls == ["plan", "tasks", "analyze", "review", "implement", "drift"]


def test_drift_found_converges_and_implements_again(tmp_path, monkeypatch):
    cfg, feat, fake = _prepare(tmp_path, monkeypatch, blockers=[0], drift_found=True)
    flow.advance(cfg, feat, RID, NS(name="fake"), implement=True)
    assert fake.calls[-3:] == ["drift", "converge", "implement"]
    assert fake.calls.count("converge") == 1                # one convergence pass, never a loop


def test_advance_stops_at_a_failing_step(tmp_path, monkeypatch):
    cfg, feat, fake = _prepare(tmp_path, monkeypatch, blockers=[0])
    monkeypatch.setattr(S, "do_plan", lambda *a, **k: StepResult(
        step="plan-001-a", kind="plan", ok=False, exit_code=1, duration_s=1.0, error="boom"))
    out = flow.advance(cfg, feat, RID, NS(name="fake"))
    assert out["stopped"] == "plan" and out["reason"] == "boom" and fake.calls == []


def test_the_budget_stops_before_the_next_step(tmp_path, monkeypatch):
    cfg, feat, fake = _prepare(tmp_path, monkeypatch, blockers=[0], cost=1.0)
    out = flow.advance(cfg, feat, RID, NS(name="fake"), budget=flow.Budget(2.0))
    assert fake.calls == ["plan", "tasks"]
    assert out["stopped"] == "budget" and "budget of 2 reached" in out["reason"]


def test_only_one_runs_a_single_step(tmp_path, monkeypatch):
    cfg, feat, fake = _prepare(tmp_path, monkeypatch, blockers=[0])
    flow.advance(cfg, feat, RID, NS(name="fake"), only_one=True)
    assert fake.calls == ["plan"]


def test_fix_loop_reports_clean(tmp_path, monkeypatch):
    cfg, feat, fake = _prepare(tmp_path, monkeypatch, blockers=[1, 0])
    out = flow.do_fix_loop(cfg, feat, RID, NS(name="fake"))
    assert out["clean"] is True and out["blockers"] == 0
    assert "analyze" not in fake.calls                     # revise does not run the whole chain


# ---- go ----

def test_go_without_features_or_idea_asks_for_one(tmp_path):
    cfg = _cfg(tmp_path)
    (tmp_path / ".specify").mkdir()
    out = flow.do_go(cfg, "", harness=NS(name="fake"))
    assert out["ok"] is False and "pass an idea" in out["error"]


def test_go_specifies_then_takes_each_feature_to_a_reviewed_design(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    (tmp_path / ".specify").mkdir()
    fake = FakeFlow(cfg, monkeypatch, blockers=[0])

    def specify(cfg_, idea, rid, h, stream, dry, **k):
        _feature(tmp_path)
        return StepResult(step="specify", kind="specify", ok=True, exit_code=0, duration_s=1.0, cost=1.0)

    monkeypatch.setattr(S, "do_specify", specify)
    out = flow.do_go(cfg, "a task cli", harness=NS(name="fake"))
    assert out["ok"] is True and out["spent"] >= 1.0
    assert fake.calls == ["plan", "tasks", "analyze", "review"]
    assert (cfg.runs_dir / out["run_id"] / "go.json").exists()
    assert out["features"]["001-a"]["state"]["blockers"] == 0


def test_go_resumes_existing_features_instead_of_specifying_again(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    (tmp_path / ".specify").mkdir()
    _feature(tmp_path)
    FakeFlow(cfg, monkeypatch, blockers=[0])
    monkeypatch.setattr(S, "do_specify", lambda *a, **k: pytest.fail("specified again"))
    out = flow.do_go(cfg, "another idea", harness=NS(name="fake"))
    assert "resuming" in out["note"] and out["ok"] is True


def test_a_long_brief_is_split_automatically(tmp_path):
    cfg = _cfg(tmp_path)
    assert flow._split_wanted("short idea", cfg, False) is False
    assert flow._split_wanted("x" * 7000, cfg, False) is True
    assert flow._split_wanted("short idea", cfg, True) is True


# ---- the CLI ----

@pytest.fixture
def project(tmp_path):
    (tmp_path / ".speckfish").mkdir()
    (tmp_path / ".speckfish" / "config.json").write_text("{}", encoding="utf-8")
    return tmp_path


def test_go_implement_needs_yes(project, capsys):
    code = C.main(["--root", str(project), "go", "an idea", "--implement"])
    assert code == C.EXIT_ENV and "--yes" in capsys.readouterr().out


def test_next_explain_says_what_comes_next(project, capsys):
    _feature(project)
    code = C.main(["--root", str(project), "next", "--explain"])
    out = capsys.readouterr().out
    assert code == C.EXIT_OK and "speckfish plan --feature 001-a" in out


def test_status_prints_the_features(project, capsys):
    _feature(project, plan_md="# p")
    assert C.main(["--root", str(project), "status"]) == C.EXIT_OK
    out = capsys.readouterr().out
    assert "001-a" in out and "next:" in out


def test_status_json(project, capsys):
    import json
    _feature(project)
    C.main(["--root", str(project), "--json", "status"])
    data = json.loads(capsys.readouterr().out)
    assert data["features"][0]["next"]["kind"] == "plan"


def test_flow_exit_codes():
    assert C.flow_exit_code({"ok": True}) == C.EXIT_OK
    assert C.flow_exit_code({"ok": False, "blockers": 2,
                             "features": {"a": {"stopped": "revise"}}}) == C.EXIT_BLOCKERS
    assert C.flow_exit_code({"ok": False, "features": {"a": {"stopped": "plan",
                                                              "error_kind": "quota"}}}) == C.EXIT_ENV
    assert C.flow_exit_code({"ok": False, "features": {"a": {"stopped": "budget"}}}) == C.EXIT_ENV
    assert C.flow_exit_code({"ok": False, "features": {"a": {"stopped": "plan"}}}) == C.EXIT_ERROR


def test_flow_summary_is_a_short_text():
    out = {"run_id": "r1", "spent": 3.0, "budget": 5.0, "features": {"001-a": {
        "steps": [{"kind": "plan", "ok": True, "duration_s": 61, "cost": 1.5, "cost_unit": "usd"}],
        "stopped": "revise", "reason": "left blockers",
        "state": {"tasks_done": 0, "tasks_total": 4, "blockers": 2}}}}
    text = C.flow_summary(out)
    assert "001-a  tasks 0/4  review: 2 blocker(s)" in text
    assert "OK     plan" in text and "stopped at revise: left blockers" in text
    assert "spent 3.0 of 5" in text and "{" not in text


def test_the_new_commands_have_handlers():
    for name in ("go", "next", "revise", "status"):
        assert name in C.HANDLERS
        C.build_parser().parse_args([name])


def test_costs_in_different_units_are_never_added(tmp_path):
    cfg = _cfg(tmp_path)
    for rid, unit, cost in (("20260101-000001", "usd", 1.5), ("20260101-000002", "credits", 30.0)):
        res = StepResult(step="plan-x", ok=True, exit_code=0, duration_s=1.0, cost=cost, cost_unit=unit)
        R.record_state(cfg, R.run_dir_for(cfg, rid), rid, res, "plan-x", NS(name="fake"),
                       datetime.now().isoformat(timespec="seconds"))
    assert flow.total_cost(cfg) == {"usd": 1.5, "credits": 30.0}


def test_analyze_is_not_asked_once_the_design_was_reviewed(tmp_path):
    """A project with reviews recorded but no analyze record must not be sent back to analyze."""
    cfg = _cfg(tmp_path)
    feat = _feature(tmp_path, plan_md="# p", tasks_md="- [ ] T1\n")
    _record(cfg, "review-001-a", blockers=0)
    assert flow.next_step(cfg, feat) == (None, "design reviewed with no blockers; "
                                                "add --implement --yes to write the code")
