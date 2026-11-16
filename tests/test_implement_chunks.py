"""implement in chunks: a whole tasks.md in one call ends early and ticks nothing (seen on a real run)."""
from __future__ import annotations

import os
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from speckfish import cli as C
from speckfish import flow
from speckfish import runner as R
from speckfish import steps as S
from speckfish import workspace as W
from speckfish.config import Config
from speckfish.runner import StepResult

TASKS = """# Tasks

## Phase 1: Setup
- [x] T001 Create the solution
- [ ] T002 [P] Add the API project
- [ ] T003 Add the test project

## Phase 2: Foundational
- [ ] T004 [US1] Identity model in src/Domain/Identity.cs
- [ ] T005 [US1] Identity tests
"""


def _cfg(tmp_path) -> Config:
    return Config(root=tmp_path.resolve())


def _feature(root: Path, text: str = TASKS, name: str = "001-a") -> Path:
    d = root / "specs" / name
    d.mkdir(parents=True, exist_ok=True)
    for fname, body in (("spec.md", "# s"), ("plan.md", "# p"), ("tasks.md", text)):
        (d / fname).write_text(body, encoding="utf-8")
        os.utime(d / fname, (time.time() - 10_000,) * 2)
    return d


def _tick(feature: Path, ids: list[str]) -> None:
    path = feature / "tasks.md"
    text = path.read_text(encoding="utf-8")
    for i in ids:
        text = text.replace(f"- [ ] {i} ", f"- [x] {i} ")
    path.write_text(text, encoding="utf-8")


# ---- reading tasks.md ----

def test_parse_tasks_reads_ids_state_and_phase(tmp_path):
    feat = _feature(tmp_path)
    tasks = W.parse_tasks(feat / "tasks.md")
    assert [t["id"] for t in tasks] == ["T001", "T002", "T003", "T004", "T005"]
    assert [t["done"] for t in tasks] == [True, False, False, False, False]
    assert tasks[1]["phase"] == "Phase 1: Setup" and tasks[3]["phase"] == "Phase 2: Foundational"
    assert tasks[1]["text"].startswith("[P] Add the API project")


def test_next_chunk_takes_pending_tasks_in_order():
    tasks = [{"id": f"T{i}", "done": i == 1, "text": "", "phase": ""} for i in range(1, 7)]
    assert [t["id"] for t in W.next_chunk(tasks, 2)] == ["T2", "T3"]
    assert [t["id"] for t in W.next_chunk(tasks, 0)] == ["T2", "T3", "T4", "T5", "T6"]
    assert W.next_chunk([{"id": "T1", "done": True, "text": "", "phase": ""}], 5) == []


def test_a_missing_tasks_file_is_empty(tmp_path):
    assert W.parse_tasks(tmp_path / "nope.md") == []


def test_chunk_args_names_every_task_and_forbids_the_rest():
    text = flow.chunk_args([{"id": "T002", "text": "Add the API project"},
                            {"id": "T003", "text": "Add the test project"}])
    assert "ONLY these 2 task(s)" in text and "T002 Add the API project" in text
    assert "`- [x]`" in text and "Do not start any other task" in text


# ---- one chunk ----

def _fake_skill(monkeypatch, feature: Path, tick: list[str], ok: bool = True, error: str = "",
                exit_code: int = 0):
    calls = []

    def do_skill(cfg, skill, args, rid, h, stream, dry, feature=None, step_name=None):
        calls.append((skill, args))
        _tick(feat_dir, tick)
        return StepResult(step=step_name, kind="implement", ok=ok, exit_code=exit_code,
                          duration_s=1.0, error=error, cost=1.0)

    feat_dir = feature
    monkeypatch.setattr(S, "do_skill", do_skill)
    return calls


def _fake_reconcile(monkeypatch, feature: Path, tick: list[str]):
    calls = []

    def run_step(cfg, h, label, prompt, cwd, rid, egm, **k):
        calls.append((label, k.get("kind"), prompt))
        _tick(feature, tick)
        return StepResult(step=label, kind="reconcile", ok=True, exit_code=0, duration_s=1.0, cost=0.5)

    monkeypatch.setattr(flow, "run_step", run_step)
    return calls


def test_a_chunk_that_ticks_everything_needs_no_reconcile(tmp_path, monkeypatch):
    feat = _feature(tmp_path)
    skill = _fake_skill(monkeypatch, feat, ["T002", "T003"])
    rec = _fake_reconcile(monkeypatch, feat, [])
    res = flow.do_implement_chunk(_cfg(tmp_path), feat, "rid", NS(name="fake"), chunk=2)
    assert res.ok is True and rec == []
    assert "T002" in skill[0][1] and "T003" in skill[0][1] and "T004" not in skill[0][1]
    assert res.verdict == "2/2 of this chunk ticked  (3/5 overall)"


def test_code_written_but_not_ticked_is_reconciled(tmp_path, monkeypatch):
    """What happened on the real run: the agent did part of the work and left the boxes alone."""
    feat = _feature(tmp_path)
    _fake_skill(monkeypatch, feat, [], ok=False, exit_code=0,
                error="implement finished without ticking a single task in tasks.md (1/5 before, 1/5 after)")
    rec = _fake_reconcile(monkeypatch, feat, ["T002", "T003"])
    res = flow.do_implement_chunk(_cfg(tmp_path), feat, "rid", NS(name="fake"), chunk=2)
    assert len(rec) == 1 and rec[0][1] == "reconcile"
    assert "Do not write or change any code" in rec[0][2] and "T002" in rec[0][2]
    assert res.ok is True and res.error == ""
    assert res.cost == 1.5                                  # the chunk and its reconcile
    assert res.tasks_done == 3


def test_nothing_ticked_even_after_reconcile_is_a_failure(tmp_path, monkeypatch):
    feat = _feature(tmp_path)
    _fake_skill(monkeypatch, feat, [], ok=True)
    _fake_reconcile(monkeypatch, feat, [])
    res = flow.do_implement_chunk(_cfg(tmp_path), feat, "rid", NS(name="fake"), chunk=2)
    assert res.ok is False and "none of the 2 task(s)" in res.error


def test_a_crashed_agent_is_not_reconciled(tmp_path, monkeypatch):
    feat = _feature(tmp_path)
    _fake_skill(monkeypatch, feat, [], ok=False, exit_code=1, error="the agent ran out of quota")
    rec = _fake_reconcile(monkeypatch, feat, ["T002"])
    res = flow.do_implement_chunk(_cfg(tmp_path), feat, "rid", NS(name="fake"), chunk=2)
    assert rec == [] and res.ok is False


def test_no_pending_task_is_a_no_op(tmp_path):
    feat = _feature(tmp_path, "- [x] T001 done\n")
    res = flow.do_implement_chunk(_cfg(tmp_path), feat, "rid", NS(name="fake"))
    assert res.ok is True and res.verdict == "no pending task"


# ---- the loop ----

def _fake_chunks(monkeypatch, per_call: int, cost: float = 1.0):
    """Each chunk ticks `per_call` pending tasks."""
    calls = []

    def chunk(cfg, feature, rid, h, chunk=15, stream=True, dry_run=False):
        pending = W.next_chunk(W.parse_tasks(feature / "tasks.md"), chunk)
        _tick(feature, [t["id"] for t in pending[:per_call]])
        calls.append(len(pending))
        return StepResult(step="implement-x", kind="implement", ok=True, exit_code=0,
                          duration_s=1.0, cost=cost)

    monkeypatch.setattr(flow, "do_implement_chunk", chunk)
    return calls


def test_the_loop_runs_chunks_until_every_task_is_ticked(tmp_path, monkeypatch):
    feat = _feature(tmp_path)                                    # 4 pending
    calls = _fake_chunks(monkeypatch, per_call=2)
    out = flow.implement_loop(_cfg(tmp_path), feat, "rid", NS(name="fake"), chunk=2)
    assert len(calls) == 2 and out["stopped"] is None
    assert out["reason"] == "all 5 task(s) ticked"


def test_the_loop_stops_when_two_chunks_tick_nothing(tmp_path, monkeypatch):
    feat = _feature(tmp_path)
    calls = _fake_chunks(monkeypatch, per_call=0)
    out = flow.implement_loop(_cfg(tmp_path), feat, "rid", NS(name="fake"), chunk=2)
    assert len(calls) == 2
    assert out["stopped"] == "implement" and "ticked nothing" in out["reason"]


def test_max_chunks_leaves_the_rest_for_the_next_run(tmp_path, monkeypatch):
    feat = _feature(tmp_path)
    calls = _fake_chunks(monkeypatch, per_call=1)
    out = flow.implement_loop(_cfg(tmp_path), feat, "rid", NS(name="fake"), chunk=1, max_chunks=2)
    assert len(calls) == 2 and out["stopped"] is None
    assert "2 task(s) pending after 2 chunk(s)" in out["reason"]


def test_the_budget_stops_before_the_next_chunk(tmp_path, monkeypatch):
    feat = _feature(tmp_path)
    calls = _fake_chunks(monkeypatch, per_call=1, cost=2.0)
    out = flow.implement_loop(_cfg(tmp_path), feat, "rid", NS(name="fake"), chunk=1,
                              budget=flow.Budget(3.0))
    assert len(calls) == 2 and out["stopped"] == "budget"


def test_a_failed_chunk_stops_the_loop(tmp_path, monkeypatch):
    feat = _feature(tmp_path)
    monkeypatch.setattr(flow, "do_implement_chunk", lambda *a, **k: StepResult(
        step="x", kind="implement", ok=False, exit_code=1, duration_s=1.0, error="boom",
        error_kind="quota"))
    out = flow.implement_loop(_cfg(tmp_path), feat, "rid", NS(name="fake"))
    assert out["stopped"] == "implement" and out["error_kind"] == "quota"


def test_go_implements_in_chunks_and_then_checks_drift(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    feat = _feature(tmp_path, "- [ ] T001 a\n- [ ] T002 b\n- [ ] T003 c\n- [ ] T004 d\n- [ ] T005 e\n")
    for label in ("analyze-001-a", "review-001-a"):
        res = StepResult(step=label, ok=True, exit_code=0, duration_s=1.0)
        R.record_state(cfg, R.run_dir_for(cfg, "r0"), "r0", res, label, NS(name="fake"),
                       datetime.now().isoformat(timespec="seconds"))
    calls = _fake_chunks(monkeypatch, per_call=2)
    drift = []
    def do_drift(cfg_, target, rid, h, **k):
        drift.append(1)
        res = StepResult(step=k["step_name"], kind="drift", ok=True, exit_code=0, duration_s=1.0)
        R.record_state(cfg, R.run_dir_for(cfg, rid), rid, res, k["step_name"], NS(name="fake"),
                       datetime.now().isoformat(timespec="seconds"))     # what run_step does
        return res

    monkeypatch.setattr(S, "do_drift", do_drift)
    out = flow.advance(cfg, feat, "rid", NS(name="fake"), implement=True, chunk=2)
    assert calls == [2, 2, 1] and drift == [1] and out["stopped"] is None


# ---- the command ----

@pytest.fixture
def project(tmp_path):
    (tmp_path / ".speckfish").mkdir()
    (tmp_path / ".speckfish" / "config.json").write_text("{}", encoding="utf-8")
    return tmp_path


def test_implement_without_yes_is_refused(project):
    _feature(project)
    assert C.main(["--root", str(project), "implement"]) == C.EXIT_ENV


def test_implement_passes_the_chunk_options_through(project, monkeypatch):
    _feature(project)
    seen = {}

    def loop(cfg, feat, rid, h, **k):
        seen.update(k)
        seen["feature"] = feat.name
        return {"name": feat.name, "steps": [], "stopped": None, "reason": "all 5 task(s) ticked",
                "switched": []}

    monkeypatch.setattr(C, "get_harness", lambda cfg, name=None: NS(name="fake"))
    monkeypatch.setattr(flow, "implement_loop", loop)
    code = C.main(["--root", str(project), "implement", "--yes", "--chunk", "5", "--max-chunks", "2",
                   "--budget", "9"])
    assert code == C.EXIT_OK
    assert seen["chunk"] == 5 and seen["max_chunks"] == 2 and seen["budget"].limit == 9.0
    assert seen["feature"] == "001-a"


def test_the_default_chunk_is_five(project):
    args = C.build_parser().parse_args(["implement", "--yes"])
    assert args.chunk == 5 and args.max_chunks == 0
    assert C.build_parser().parse_args(["go", "--implement", "--yes"]).chunk == 5
