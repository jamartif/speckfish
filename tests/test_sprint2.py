"""Sprint 2 of AUDITORIA.md: state integrity (B3), run index (P2), budget units (B2), minor fixes (B5, P6)."""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from speckfish import cli as C
from speckfish import flow, prompts, store
from speckfish import runner as R
from speckfish import steps as S
from speckfish import workspace as W
from speckfish.config import Config
from speckfish.runner import StepResult


def _cfg(tmp_path) -> Config:
    return Config(root=tmp_path.resolve())


# ---------------- B3: atomic writes, lock, unique run ids ----------------

def test_a_failed_write_leaves_the_old_file_intact(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    store.write_json_atomic(path, {"v": 1})

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        store.write_json_atomic(path, {"v": 2})
    monkeypatch.undo()
    assert json.loads(path.read_text()) == {"v": 1}
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")], "temp file left behind"


def test_the_lock_serialises_a_read_modify_write(tmp_path):
    path = tmp_path / "counter.json"
    store.write_json_atomic(path, {"n": 0})

    def bump():
        for _ in range(25):
            with store.file_lock(path):
                n = json.loads(path.read_text())["n"]
                time.sleep(0.001)                     # widen the race window
                store.write_json_atomic(path, {"n": n + 1})

    threads = [threading.Thread(target=bump) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert json.loads(path.read_text())["n"] == 100


def test_record_state_keeps_every_entry_when_steps_finish_at_once(tmp_path):
    cfg = _cfg(tmp_path)
    run_dir = R.run_dir_for(cfg, "rid")

    def record(label):
        res = StepResult(step=label, ok=True, exit_code=0, duration_s=1.0)
        R.record_state(cfg, run_dir, "rid", res, label, NS(name="fake"), "2026-01-01T00:00:00")

    threads = [threading.Thread(target=record, args=(f"plan-{i:03d}",)) for i in range(40)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(json.loads((run_dir / "state.json").read_text())["steps"]) == 40


def test_a_stale_lock_from_a_dead_process_is_taken_over(tmp_path):
    path = tmp_path / "x.json"
    lock = tmp_path / "x.json.lock"
    lock.write_text("")
    old = time.time() - 600
    os.utime(lock, (old, old))
    with store.file_lock(path, timeout=1) as held:
        assert held is True


def test_a_busy_lock_does_not_hang_forever(tmp_path):
    path = tmp_path / "x.json"
    (tmp_path / "x.json.lock").write_text("")
    started = time.time()
    with store.file_lock(path, timeout=0.2, stale=3600) as held:
        assert held is False                          # proceeds anyway, best effort
    assert time.time() - started < 2


def test_run_ids_started_in_the_same_second_get_their_own_folder(tmp_path):
    cfg = _cfg(tmp_path)
    ids = [S.ensure_run(cfg, None) for _ in range(5)]
    assert len(set(ids)) == 5
    assert all((cfg.runs_dir / i).is_dir() for i in ids)
    assert sorted(ids) == ids


def test_an_explicit_run_id_is_reused_not_renumbered(tmp_path):
    cfg = _cfg(tmp_path)
    assert S.ensure_run(cfg, "20260101-000000") == "20260101-000000"
    assert S.ensure_run(cfg, "20260101-000000") == "20260101-000000"


def test_config_and_feature_pointer_are_written_atomically(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    cfg.language = "es"
    cfg.save()
    calls = []
    real = os.replace
    monkeypatch.setattr(os, "replace", lambda a, b: (calls.append(Path(b).name), real(a, b))[1])
    cfg.save()
    feat = tmp_path / "specs" / "001-a"
    feat.mkdir(parents=True)
    S.set_active_feature(cfg, feat)
    assert "config.json" in calls and "feature.json" in calls


# ---------------- read cache ----------------

def test_the_cache_reuses_the_parse_and_notices_a_change(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    store.write_json_atomic(path, {"a": 1})
    parses = []
    real = json.loads
    monkeypatch.setattr(store.json, "loads", lambda t, **k: (parses.append(1), real(t, **k))[1])
    assert store.read_json_cached(path) == {"a": 1}
    assert store.read_json_cached(path) == {"a": 1}
    assert len(parses) == 1
    store.write_json_atomic(path, {"a": 2})
    assert store.read_json_cached(path) == {"a": 2}
    path.write_text("{ broken")
    assert store.read_json_cached(path) is None
    path.unlink()
    assert store.read_json_cached(path) is None


def test_run_summary_does_not_leak_totals_into_the_cache(tmp_path):
    cfg = _cfg(tmp_path)
    run_dir = R.run_dir_for(cfg, "rid")
    store.write_json_atomic(run_dir / "state.json", {"run_id": "rid", "steps": {
        "plan": {"cost": 1.0, "cost_unit": "usd", "duration_s": 2}}})
    first = S.run_summary(cfg, "rid")
    first["steps"]["plan"]["cost"] = 99                       # a caller mutating its copy of a step...
    assert first["cost"] == 1.0
    assert "cost" not in store.read_json_cached(run_dir / "state.json")      # ...totals stay out of the file view


# ---------------- P2: the run index ----------------

def _record(cfg, rid, label, when, blockers=0):
    run_dir = R.run_dir_for(cfg, rid)
    res = StepResult(step=label, ok=True, exit_code=0, duration_s=1.0, blockers=["b"] * blockers)
    R.record_state(cfg, run_dir, rid, res, label, NS(name="fake"), when)


def test_the_index_returns_the_newest_record_per_kind_and_feature(tmp_path):
    cfg = _cfg(tmp_path)
    _record(cfg, "20260101-000001", "review-001-a", "2026-01-01T00:00:01", blockers=3)
    _record(cfg, "20260101-000002", "review-001-a", "2026-01-01T00:00:02", blockers=1)
    _record(cfg, "20260101-000002", "review-001-a-r2", "2026-01-01T00:00:03", blockers=0)
    _record(cfg, "20260101-000002", "review-002-b", "2026-01-01T00:00:04", blockers=5)
    _record(cfg, "20260101-000002", "drift-001-a", "2026-01-01T00:00:05")
    rec = flow.latest_record(cfg, "review", "001-a")
    assert rec["key"] == "review-001-a-r2" and rec["blockers"] == 0
    assert flow.latest_record(cfg, "review", "002-b")["blockers"] == 5
    assert flow.latest_record(cfg, "drift", "001-a")["key"] == "drift-001-a"
    assert flow.latest_record(cfg, "analyze", "001-a") is None
    assert flow.latest_record(cfg, "review", "003-c") is None


def test_the_index_sees_a_step_recorded_after_it_was_built(tmp_path):
    cfg = _cfg(tmp_path)
    assert flow.latest_record(cfg, "review", "001-a") is None
    _record(cfg, "20260101-000001", "review-001-a", "2026-01-01T00:00:01")
    assert flow.latest_record(cfg, "review", "001-a")["key"] == "review-001-a"
    _record(cfg, "20260101-000001", "review-001-a-r2", "2026-01-01T00:00:09")
    assert flow.latest_record(cfg, "review", "001-a")["key"] == "review-001-a-r2"
    _record(cfg, "20260101-000009", "review-001-a", "2026-01-01T00:01:00")       # a brand new run folder
    assert flow.latest_record(cfg, "review", "001-a")["run"] == "20260101-000009"


def test_status_over_many_features_and_runs_stays_fast(tmp_path):
    cfg = _cfg(tmp_path)
    for i in range(20):
        d = tmp_path / "specs" / f"{i:03d}-f{i}"
        d.mkdir(parents=True)
        for n in ("spec.md", "plan.md"):
            (d / n).write_text("# x")
        (d / "tasks.md").write_text("- [x] T1 a\n- [ ] T2 b\n")
    for r in range(300):
        steps = {f"review-{i:03d}-f{i}": {"status": "ok", "started_at": "2026-01-01T00:00:00", "blockers": 0}
                 for i in range(0, 20, 4)}
        rd = cfg.runs_dir / f"20260101-{r:06d}"
        rd.mkdir(parents=True)
        (rd / "state.json").write_text(json.dumps({"run_id": rd.name, "steps": steps}))
    started = time.time()
    rows = flow.status_rows(cfg)
    assert len(rows) == 20
    cold = time.time() - started
    assert cold < 5.0, "status is slow again"                 # was ~1 s on a laptop; CI runners are slower
    started = time.time()
    flow.status_rows(cfg)                                     # warm: nothing changed, nothing re-parsed
    warm = time.time() - started
    assert warm < 3.0 and warm < cold * 1.5 + 0.3


def test_next_step_reuses_a_state_it_is_given(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    feat = tmp_path / "specs" / "001-a"
    feat.mkdir(parents=True)
    (feat / "spec.md").write_text("# s")
    st = flow.feature_state(cfg, feat)
    monkeypatch.setattr(flow, "feature_state", lambda *a, **k: pytest.fail("computed twice"))
    assert flow.next_step(cfg, feat, state=st)[0] == "plan"


# ---------------- B2: one currency per budget ----------------

def test_a_fallback_in_another_currency_does_not_count_against_the_limit():
    b = flow.Budget(5.0)
    b.add(StepResult(step="a", ok=True, exit_code=0, duration_s=1, cost=1.5, cost_unit="usd"))
    b.add(StepResult(step="b", ok=True, exit_code=0, duration_s=1, cost=30, cost_unit="credits"))
    assert b.spent == 1.5 and b.unit == "usd" and b.other == {"credits": 30.0}
    assert not b.exhausted
    b.add(StepResult(step="c", ok=True, exit_code=0, duration_s=1, cost=4.0, cost_unit="usd"))
    assert b.exhausted and "5 usd reached" in b.describe()


def test_the_budget_unit_can_be_fixed_up_front():
    b = flow.Budget(50, unit="credits")
    b.add(StepResult(step="a", ok=True, exit_code=0, duration_s=1, cost=3.0, cost_unit="usd"))
    assert b.spent == 0 and b.other == {"usd": 3.0}
    b.add_cost(60, "credits")
    assert b.exhausted


# ---------------- B5 ----------------

@pytest.mark.parametrize("path,noise", [
    ("src/bin/main.rs", False), ("bin/Debug/app.dll", True), ("src/App/obj/Release/x.dll", True),
    ("obj/project.assets.json", False), (".speckfish/runs/a/state.json", True),
    ("docs/CLAUDE.md", True), (".gitignore", True), ("src/.claude/settings.json", True),
    ("src/gitignore_helper.py", False), ("a/b.py", False), ("", False),
])
def test_noise_is_judged_by_path_segments(path, noise):
    assert W._is_noise(path) is noise


def test_a_change_under_a_real_bin_folder_counts_as_work(tmp_path):
    import subprocess
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "src" / "bin").mkdir(parents=True)
    (tmp_path / "src" / "bin" / "main.rs").write_text("fn main() {}")
    assert W._git_dirty(tmp_path) is True
    (tmp_path / "src" / "bin" / "main.rs").unlink()
    (tmp_path / "bin" / "Debug").mkdir(parents=True)
    (tmp_path / "bin" / "Debug" / "x.dll").write_text("x")
    assert W._git_dirty(tmp_path) is False


def test_porcelain_without_a_usable_git_is_unknown_not_a_crash(tmp_path, monkeypatch):
    import speckfish.config as cfgmod
    monkeypatch.setattr(cfgmod, "find_git", lambda: (_ for _ in ()).throw(SystemExit("no git")))
    assert W._git_porcelain(tmp_path) is None
    assert W._git_dirty(tmp_path) is True                      # unknown: do not fail the step over it


def _features(root, *names):
    for n in names:
        d = root / "specs" / n
        d.mkdir(parents=True, exist_ok=True)
        (d / "spec.md").write_text("# s")


def test_an_ambiguous_feature_prefix_is_an_error_and_exact_or_number_win(tmp_path):
    cfg = _cfg(tmp_path)
    _features(tmp_path, "001-open", "002-open-repos", "010-x")
    with pytest.raises(SystemExit, match="matches 2 features"):
        S.find_feature(cfg, "00")
    assert S.find_feature(cfg, "001-open").name == "001-open"
    assert S.find_feature(cfg, "2").name == "002-open-repos"
    assert S.find_feature(cfg, "002-op").name == "002-open-repos"
    assert S.find_feature(cfg, "10").name == "010-x"
    with pytest.raises(SystemExit, match="not found"):
        S.find_feature(cfg, "zzz")


def test_feature_json_cannot_point_outside_the_project(tmp_path):
    root = tmp_path / "proj"
    (root / ".specify").mkdir(parents=True)
    _features(root, "001-a")
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    ptr = root / ".specify" / "feature.json"
    for bad in (str(outside), "../elsewhere", "specs/missing"):
        ptr.write_text(json.dumps({"feature_directory": bad}))
        assert W.feature_json_dir(root) is None
        assert S.active_feature(_cfg(root)) is None
        assert W.feature_dir(root).name == "001-a"             # falls back to the newest spec
    ptr.write_text(json.dumps({"feature_directory": "specs/001-a"}))
    assert W.feature_json_dir(root).name == "001-a"
    ptr.write_text("[]")
    assert W.feature_json_dir(root) is None


def test_clean_runs_with_a_negative_keep_deletes_nothing_it_should_not(tmp_path):
    cfg = _cfg(tmp_path)
    for n in ("a", "b", "c"):
        (cfg.runs_dir / n).mkdir(parents=True)
    assert S.clean_runs(cfg, 2)["removed"] == ["a"]
    assert S.clean_runs(cfg, -5)["removed"] == ["b", "c"]       # negative = keep none, as 0 does


def test_copilot_uses_the_budget_it_is_given(tmp_path):
    from speckfish.harnesses import CopilotHarness
    h = CopilotHarness(_cfg(tmp_path))
    inv = h.build("review", "p", tmp_path, tmp_path, None, 45, 600, None)
    assert inv.argv[inv.argv.index("--max-ai-credits") + 1] == "45"
    inv = h.build("review", "p", tmp_path, tmp_path, None, None, 600, None)
    assert inv.argv[inv.argv.index("--max-ai-credits") + 1] == "30"      # the configured default


def test_specify_review_is_kept_and_blockers_fail_the_run(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    spec = tmp_path / "specs" / "001-a" / "spec.md"
    spec.parent.mkdir(parents=True)
    spec.write_text("# s")
    monkeypatch.setattr(S, "run_step", lambda *a, **k: StepResult(
        step="specify", ok=True, exit_code=0, duration_s=1, structured={"spec_path": str(spec)}))
    monkeypatch.setattr(S, "do_review", lambda *a, **k: StepResult(
        step="review", kind="review", ok=True, exit_code=0, duration_s=1, blockers=["B1 no auth"]))
    res = S.do_specify(cfg, "idea", "rid", NS(name="fake", slash_commands=False), stream=False, review=True)
    assert res.review["blockers"] == ["B1 no auth"]
    assert C.exit_for(res.to_dict()) == C.EXIT_BLOCKERS
    text = C.summarize(res.to_dict())
    assert "review of the spec" in text and "BLOCKER  B1 no auth" in text
    monkeypatch.setattr(S, "do_review", lambda *a, **k: StepResult(
        step="review", kind="review", ok=False, exit_code=1, duration_s=1, error="boom", error_kind="quota"))
    res = S.do_specify(cfg, "idea", "rid", NS(name="fake", slash_commands=False), stream=False, review=True)
    assert C.exit_for(res.to_dict()) == C.EXIT_ENV


def test_a_specify_without_review_is_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "run_step", lambda *a, **k: StepResult(step="specify", ok=True, exit_code=0,
                                                                  duration_s=1))
    res = S.do_specify(_cfg(tmp_path), "idea", "rid", NS(name="fake", slash_commands=False), stream=False)
    assert res.review is None and C.exit_for(res.to_dict()) == C.EXIT_OK


def test_error_kind_accepts_payloads_already_extracted():
    out = json.dumps({"type": "result", "is_error": True, "api_error_status": 429, "result": "limit"})
    payloads = R._json_payloads(out)
    assert R.error_kind(out, "", payloads) == R.error_kind(out, "") == "quota"
    assert "quota" in R.human_error(out, "", 1)


# ---------------- P6 ----------------

def test_the_plan_prompt_carries_the_stack_once(tmp_path):
    brief = tmp_path / "stack.md"
    brief.write_text("UNIQUE-MARKER stdlib only")
    text = prompts.plan_prompt(f"@{brief}", tmp_path, tmp_path / "specs" / "001-a")
    assert text.count("UNIQUE-MARKER") == 1
    assert text.startswith("/speckit-plan ") and "001-a" in text
    assert prompts.plan_prompt("", tmp_path).startswith("/speckit-plan")


# ---------------- Windows behaviours, simulated: found by CI, where state entries were lost ----------------

def test_a_permission_error_creating_the_lock_means_busy_not_nothing_to_protect(tmp_path, monkeypatch):
    """Windows raises PermissionError (not FileExistsError) when the lock file is being deleted."""
    real_open, calls = os.open, []

    def flaky(path, flags, *a, **k):
        if str(path).endswith(".lock") and len(calls) < 4:
            calls.append(1)
            raise PermissionError(13, "Permission denied")
        return real_open(path, flags, *a, **k)

    monkeypatch.setattr(os, "open", flaky)
    with store.file_lock(tmp_path / "state.json", timeout=5) as held:
        assert held is True                                   # it waited and got the lock
    assert len(calls) == 4


def test_a_lock_that_is_never_free_gives_up_unlocked_after_the_timeout(tmp_path, monkeypatch):
    real_open = os.open

    def always(path, flags, *a, **k):
        if str(path).endswith(".lock"):
            raise PermissionError(13, "Permission denied")
        return real_open(path, flags, *a, **k)

    monkeypatch.setattr(os, "open", always)
    started = time.time()
    with store.file_lock(tmp_path / "state.json", timeout=0.3) as held:
        assert held is False
    assert time.time() - started < 3


def test_other_errors_creating_the_lock_still_mean_nothing_to_protect(tmp_path, monkeypatch):
    def broken(path, flags, *a, **k):
        raise FileNotFoundError(2, "no such folder")

    monkeypatch.setattr(os, "open", broken)
    with store.file_lock(tmp_path / "state.json", timeout=5) as held:
        assert held is False


def test_two_writers_in_one_process_never_share_a_temp_file(tmp_path, monkeypatch):
    seen = []
    real = os.replace
    monkeypatch.setattr(os, "getpid", lambda: 4242)           # the same pid, as every thread has
    monkeypatch.setattr(os, "replace", lambda a, b: (seen.append(Path(a).name), real(a, b))[1])
    for i in range(3):
        store.write_json_atomic(tmp_path / "state.json", {"i": i})
    assert len(set(seen)) == 3 and all(n.startswith(".state.json.4242.") and n.endswith(".tmp") for n in seen)


def test_a_file_that_is_busy_for_a_moment_is_read_after_a_retry(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    path.write_text('{"a": 1}')
    real, fails = Path.read_text, [2]

    def busy(self, *a, **k):
        if self == path and fails[0]:
            fails[0] -= 1
            raise PermissionError(13, "sharing violation")
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", busy)
    assert store.read_json_retry(path) == {"a": 1}
    assert store.read_json_retry(tmp_path / "missing.json") is None
    path.write_text("{ broken")
    assert store.read_json_retry(path) is None


def test_a_file_that_stays_busy_raises_instead_of_reading_as_empty(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    path.write_text('{"a": 1}')
    monkeypatch.setattr(Path, "read_text", lambda self, *a, **k: (_ for _ in ()).throw(PermissionError(13, "busy")))
    with pytest.raises(PermissionError):
        store.read_json_retry(path, attempts=2)


def test_record_state_keeps_the_history_when_the_file_is_busy_for_a_moment(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    run_dir = R.run_dir_for(cfg, "rid")
    _record(cfg, "rid", "plan-001-a", "2026-01-01T00:00:01")
    state_path = run_dir / "state.json"
    real, fails = Path.read_text, [1]

    def busy(self, *a, **k):
        if self == state_path and fails[0]:
            fails[0] -= 1
            raise PermissionError(13, "sharing violation")
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", busy)
    _record(cfg, "rid", "tasks-001-a", "2026-01-01T00:00:02")
    assert set(json.loads(real(state_path, encoding="utf-8"))["steps"]) == {"plan-001-a", "tasks-001-a"}


def test_record_state_does_not_erase_the_history_when_the_file_stays_unreadable(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    run_dir = R.run_dir_for(cfg, "rid")
    _record(cfg, "rid", "plan-001-a", "2026-01-01T00:00:01")
    state_path = run_dir / "state.json"
    before = state_path.read_text(encoding="utf-8")
    real = Path.read_text
    monkeypatch.setattr(store.time, "sleep", lambda s: None)

    def locked(self, *a, **k):
        if self == state_path:
            raise PermissionError(13, "locked")
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", locked)
    with pytest.raises(PermissionError):
        _record(cfg, "rid", "tasks-001-a", "2026-01-01T00:00:02")
    monkeypatch.undo()
    assert state_path.read_text(encoding="utf-8") == before
