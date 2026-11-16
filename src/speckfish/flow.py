"""The whole loop in few commands: status, next, revise and go (PLAN.md, phase 1).

Everything here is derived from files and from what earlier runs recorded, never from memory kept in
this process, so any of it can be interrupted and resumed with the same command.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from . import prompts
from . import steps as S
from .config import Config, norm
from .runner import StepResult, run_step
from .workspace import count_tasks

MAX_STEPS_PER_FEATURE = 24      # a guard against a loop that never settles


# ---------------- what earlier runs recorded ----------------

def _runs_newest_first(cfg: Config) -> list[Path]:
    if not cfg.runs_dir.exists():
        return []
    return sorted((p for p in cfg.runs_dir.iterdir() if p.is_dir()), reverse=True)


def latest_record(cfg: Config, kind: str, feature: str) -> dict | None:
    """The newest recorded step of this kind for this feature (`review-<feature>`, `review-<feature>-r2`)."""
    for run in _runs_newest_first(cfg):
        state = run / "state.json"
        if not state.exists():
            continue
        try:
            steps = json.loads(state.read_text(encoding="utf-8")).get("steps") or {}
        except (OSError, ValueError):
            continue
        hits = [(v.get("started_at") or "", k, v) for k, v in steps.items()
                if k == f"{kind}-{feature}" or k.startswith(f"{kind}-{feature}-r")]
        if hits:
            _when, key, entry = max(hits, key=lambda h: h[0])
            return {**entry, "key": key, "run": run.name}
    return None


def _started_at(entry: dict) -> float:
    try:
        return datetime.fromisoformat(entry["started_at"]).timestamp()
    except (KeyError, TypeError, ValueError):
        return 0.0


def review_is_current(feature: Path, entry: dict | None) -> bool:
    """A review counts only if no document was edited since it started (an edit during it counts too)."""
    if not entry or entry.get("status") != "ok":
        return False
    started = _started_at(entry)
    names = ["spec.md", "plan.md"]
    # implement ticks checkboxes in tasks.md: that is progress, not a change of the design
    if count_tasks(feature / "tasks.md")[0] == 0:
        names.append("tasks.md")
    docs = [feature / n for n in names]
    return all(d.stat().st_mtime <= started for d in docs if d.exists())


def _ok_record(entry: dict | None) -> dict | None:
    """A step that failed is not a step done: only successful records count."""
    return entry if entry and entry.get("status") == "ok" else None


def feature_state(cfg: Config, feature: Path) -> dict:
    name = feature.name
    done, total = count_tasks(feature / "tasks.md")
    review = latest_record(cfg, "review", name)
    return {
        "name": name, "path": norm(feature),
        "spec": (feature / "spec.md").exists(), "plan": (feature / "plan.md").exists(),
        "tasks": (feature / "tasks.md").exists(),
        "tasks_done": done, "tasks_total": total,
        "review": review, "review_current": review_is_current(feature, review),
        "blockers": int((review or {}).get("blockers") or 0) if review_is_current(feature, review) else None,
        "analyzed": _ok_record(latest_record(cfg, "analyze", name)),
        "drift": _ok_record(latest_record(cfg, "drift", name)),
    }


def next_step(cfg: Config, feature: Path, implement: bool = False, analyze: bool = True,
              drift: bool = True) -> tuple[str | None, str]:
    """(kind, why) of the step this feature needs next; kind None means there is nothing to do."""
    st = feature_state(cfg, feature)
    if not st["spec"]:
        return "specify", "no spec.md"
    if not st["plan"]:
        return "plan", "spec.md has no plan.md"
    if not st["tasks"]:
        return "tasks", "plan.md has no tasks.md"
    # analyze is the cheap check *before* a review: once the design was reviewed it has nothing to add
    if analyze and not st["analyzed"] and not st["review"]:
        return "analyze", "spec, plan and tasks were never cross-checked"
    if not st["review_current"]:
        return "review", ("never reviewed" if not st["review"]
                          else "the documents changed after the last review")
    if st["blockers"]:
        return "revise", f"the review left {st['blockers']} blocker(s)"
    if not implement:
        return None, "design reviewed with no blockers; add --implement --yes to write the code"
    if st["tasks_total"] and st["tasks_done"] < st["tasks_total"]:
        return "implement", f"{st['tasks_total'] - st['tasks_done']} task(s) pending"
    if drift and not st["drift"]:
        return "drift", "code written; compare it with the design"
    return None, "done"


def command_for(kind: str, feature: str) -> str:
    extra = " --yes" if kind == "implement" else ""
    return f"speckfish {kind} --feature {feature}{extra}"


def status_rows(cfg: Config) -> list[dict]:
    rows = []
    for f in S.features_with_spec(cfg):
        st = feature_state(cfg, f)
        kind, why = next_step(cfg, f, implement=True)
        st["next"] = {"kind": kind, "why": why, "command": command_for(kind, f.name) if kind else None}
        rows.append(st)
    return rows


def total_cost(cfg: Config) -> dict[str, float]:
    """What every run spent, per unit: usd and credits are different currencies and are never added."""
    totals: dict[str, float] = {}
    for run in _runs_newest_first(cfg):
        summary = S.run_summary(cfg, run.name)
        if summary.get("cost"):
            unit = summary.get("cost_unit") or "usd"
            totals[unit] = round(totals.get(unit, 0.0) + float(summary["cost"]), 3)
    return totals


# ---------------- revise ----------------

def review_report_for(cfg: Config, feature: str) -> Path | None:
    """The newest consolidated review report written for this feature."""
    hits = []
    for run in _runs_newest_first(cfg):
        hits += list((run / "artifacts").glob(f"review-{feature}*/consolidated_review_report.md"))
    return max(hits, key=lambda p: p.stat().st_mtime) if hits else None


def revise_prompt(feature: Path, report: Path, majors: bool = False) -> str:
    scope = "every Blocker and every Major" if majors else "every Blocker"
    return (
        f"Revise the design documents of the feature in {norm(feature)} so that the findings of a "
        f"review are resolved.\n\n"
        f"Read the consolidated review report at {norm(report)} and fix {scope} it lists. Edit only "
        f"the documents of that feature folder (spec.md, plan.md, tasks.md, research.md, "
        f"data-model.md, contracts/). Do not write source code and do not edit the report.\n\n"
        f"{prompts.NO_QUESTIONS} Where a finding needs a decision, take the most conservative option "
        f"and record it in the document under a 'Decisions' heading, with the finding it answers. "
        f"If a finding is wrong, say so under 'Decisions' instead of changing the design.\n\n"
        f"Finish with a one-line summary of what changed.")


def _snapshot(feature: Path) -> dict:
    snap = {}
    for p in feature.rglob("*"):
        if p.is_file():
            st = p.stat()
            snap[str(p.relative_to(feature))] = (st.st_size, st.st_mtime_ns)
    return snap


def unique_label(cfg: Config, rid: str, base: str) -> str:
    """`review-001-a`, then `review-001-a-r2`, ... so two rounds in one run do not overwrite."""
    state = cfg.runs_dir / rid / "state.json"
    try:
        used = set(json.loads(state.read_text(encoding="utf-8")).get("steps") or {})
    except (OSError, ValueError):
        used = set()
    if base not in used:
        return base
    n = 2
    while f"{base}-r{n}" in used:
        n += 1
    return f"{base}-r{n}"


def do_revise(cfg: Config, feature: Path, rid: str, harness, majors: bool = False,
              stream: bool = True, dry_run: bool = False) -> StepResult:
    report = review_report_for(cfg, feature.name)
    label = unique_label(cfg, rid, f"revise-{feature.name}")
    if report is None:
        return StepResult(step=label, kind="revise", ok=False, exit_code=2, duration_s=0.0,
                          error=f"no review report for {feature.name}: run `speckfish review` first")
    prompt = S.with_language(revise_prompt(feature, report, majors), cfg)
    before = _snapshot(feature)
    res = run_step(cfg, harness, label, prompt, cfg.root, rid, cfg.egm_path(), kind="revise",
                   stream=stream, dry_run=dry_run)
    if res.ok and not dry_run and _snapshot(feature) == before:
        res.ok = False
        res.error = "the agent answered but changed no document in the feature folder"
    return res


# ---------------- one step of one feature ----------------

def run_kind(cfg: Config, kind: str, feature: Path, rid: str, harness, *, stack: str = "",
             stream: bool = True, dry_run: bool = False, majors: bool = False) -> StepResult:
    """Run one step for one feature with a label that stays unique within the run."""
    name = feature.name
    S.set_active_feature(cfg, feature)
    label = unique_label(cfg, rid, f"{kind}-{name}")
    if kind == "plan":
        return S.do_plan(cfg, stack, rid, harness, stream, dry_run, feature=name, step_name=label)
    if kind == "review":
        target = feature / "plan.md" if (feature / "plan.md").exists() else feature / "spec.md"
        return S.do_review(cfg, str(target), rid, harness, stream=stream, dry_run=dry_run,
                           default_file=target.name, step_name=label)
    if kind == "drift":
        return S.do_drift(cfg, str(feature), rid, harness, stream=stream, dry_run=dry_run,
                          step_name=label)
    if kind == "revise":
        return do_revise(cfg, feature, rid, harness, majors, stream, dry_run)
    return S.do_skill(cfg, kind, "", rid, harness, stream, dry_run, feature=name, step_name=label)


def run_with_fallback(cfg: Config, fallback: str | None, harness, call) -> tuple[StepResult, str | None]:
    """`call(harness)`; on a quota/auth stop retry once with the fallback harness."""
    res = call(harness)
    alt = S._fallback_for(cfg, fallback)
    if S._needs_fallback(res) and alt is not None:
        return call(alt), f"{harness.name} -> {alt.name}"
    return res, None


class Budget:
    """A cap on what one `go` may spend. Units are whatever the harness reports (usd or credits)."""

    def __init__(self, limit: float | None):
        self.limit = limit
        self.spent = 0.0

    def add(self, res: StepResult) -> None:
        if res.cost:
            self.spent += float(res.cost)

    @property
    def exhausted(self) -> bool:
        return bool(self.limit) and self.spent >= self.limit


def advance(cfg: Config, feature: Path, rid: str, harness, *, implement: bool = False,
            analyze: bool = True, drift: bool = True, stack: str = "", max_rounds: int = 2,
            majors: bool = False, fallback: str | None = None, budget: Budget | None = None,
            stream: bool = True, dry_run: bool = False, only_one: bool = False) -> dict:
    """Take a feature as far as the options allow, one step at a time, stopping at the first problem."""
    budget = budget or Budget(None)
    out: dict = {"name": feature.name, "steps": [], "stopped": None, "reason": "", "switched": []}
    revisions = implements = converges = 0
    for _ in range(MAX_STEPS_PER_FEATURE):
        kind, why = next_step(cfg, feature, implement=implement, analyze=analyze, drift=drift)
        out["reason"] = why
        if kind is None:
            return out
        if kind == "specify":
            out["stopped"] = "specify"
            return out
        if kind == "revise":
            if revisions >= max_rounds:
                out["stopped"] = "revise"
                out["reason"] = f"{why} after {max_rounds} revision round(s): read the report"
                return out
            revisions += 1
        if kind == "implement":
            if implements >= max_rounds + 1:
                out["stopped"] = "implement"
                out["reason"] = f"{why} after {implements} implement pass(es)"
                return out
            implements += 1
        if budget.exhausted:
            out["stopped"] = "budget"
            out["reason"] = f"budget of {budget.limit:g} reached ({budget.spent:.2f} spent)"
            return out
        res, switched = run_with_fallback(cfg, fallback, harness, lambda h, k=kind: run_kind(
            cfg, k, feature, rid, h, stack=stack, stream=stream, dry_run=dry_run, majors=majors))
        if switched:
            out["switched"].append(f"{kind}: {switched}")
        budget.add(res)
        out["steps"].append({"kind": kind, **res.to_dict()})
        if not res.ok:
            out["stopped"] = kind
            out["reason"] = res.error or "the step failed"
            out["error_kind"] = res.error_kind
            return out
        if dry_run or only_one:
            return out
        if kind == "drift" and implement and converges < 1 and (res.structured or {}).get("drift_found"):
            # the code diverged from the design: add the missing work to tasks.md, then implement again
            converges += 1
            conv = run_kind(cfg, "converge", feature, rid, harness, stream=stream, dry_run=dry_run)
            budget.add(conv)
            out["steps"].append({"kind": "converge", **conv.to_dict()})
            if not conv.ok:
                out["stopped"] = "converge"
                out["reason"] = conv.error or "converge failed"
                return out
    out["stopped"] = "loop"
    out["reason"] = f"no settled state after {MAX_STEPS_PER_FEATURE} steps"
    return out


# ---------------- revise loop: review -> fix -> review ----------------

def do_fix_loop(cfg: Config, feature: Path, rid: str, harness, *, max_rounds: int = 2,
                majors: bool = False, fallback: str | None = None, stream: bool = True,
                dry_run: bool = False) -> dict:
    """Review the feature, fix the blockers, review again; stop when clean or out of rounds."""
    out = advance(cfg, feature, rid, harness, implement=False, analyze=False, drift=False,
                  max_rounds=max_rounds, majors=majors, fallback=fallback, stream=stream,
                  dry_run=dry_run)
    st = feature_state(cfg, feature)
    out["blockers"] = st["blockers"]
    out["clean"] = bool(st["review_current"] and not st["blockers"])
    return out


# ---------------- go ----------------

def _split_wanted(material: str, cfg: Config, force: bool) -> bool:
    if force:
        return True
    body, _src = prompts.expand_input(material, cfg.root)
    return len(body) > 6000


def do_go(cfg: Config, idea: str, *, stack: str = "", split: bool | None = None,
          max_features: int = 6, implement: bool = False, analyze: bool = True,
          drift: bool = True, max_rounds: int = 2, majors: bool = False,
          budget: float | None = None, fallback: str | None = None, harness=None,
          stream: bool = True, dry_run: bool = False, init: bool = True) -> dict:
    """idea -> specs -> plans -> tasks -> reviewed design (-> code -> drift), resumable."""
    h = harness or S.get_harness(cfg)
    rid = S.ensure_run(cfg, None)
    meter = Budget(budget)
    out: dict = {"run_id": rid, "harness": h.name, "features": {}, "steps": {}, "ok": True,
                 "stopped_at": None, "blockers": 0}
    if init and not (cfg.root / ".specify").exists() and not dry_run:
        out["init"] = S.do_init(cfg, None)
    if not S.features_with_spec(cfg):
        if not idea.strip():
            out.update(ok=False, stopped_at="specify",
                       error="no feature yet: pass an idea or @brief.md")
            return out
        if _split_wanted(idea, cfg, bool(split)) and split is not False:
            sp = S.do_specify_split(cfg, idea, rid, h, stream, dry_run, max_features)
            out["steps"]["specify"] = {"ok": bool(sp.get("ok") or sp.get("dry_run")),
                                       "specs": [s["path"] for s in sp.get("specs", [])],
                                       "error": sp.get("error", "")}
            ok = out["steps"]["specify"]["ok"]
            for step in (sp.get("steps") or {}).values():
                if step.get("cost"):
                    meter.spent += float(step["cost"])
        else:
            res = S.do_specify(cfg, idea, rid, h, stream, dry_run)
            meter.add(res)
            out["steps"]["specify"] = res.to_dict()
            ok = res.ok
        if not ok:
            out.update(ok=False, stopped_at="specify")
            return out
        if dry_run:
            return out
    elif idea.strip():
        out["note"] = "features already exist: resuming them (the idea was not specified again)"

    for f in S.features_with_spec(cfg):
        result = advance(cfg, f, rid, h, implement=implement, analyze=analyze, drift=drift,
                         stack=stack, max_rounds=max_rounds, majors=majors, fallback=fallback,
                         budget=meter, stream=stream, dry_run=dry_run)
        st = feature_state(cfg, f)
        result["state"] = {k: st[k] for k in ("spec", "plan", "tasks", "tasks_done", "tasks_total",
                                              "blockers", "review_current")}
        out["features"][f.name] = result
        if st["blockers"]:
            out["blockers"] += int(st["blockers"])
        if result["stopped"]:
            out["ok"] = False
            out["stopped_at"] = out["stopped_at"] or f"{f.name}:{result['stopped']}"
            if result["stopped"] in ("budget",) or result.get("error_kind") in ("quota", "auth"):
                break
        if dry_run:
            break
    out["spent"] = round(meter.spent, 3)
    out["budget"] = budget
    run_dir = cfg.runs_dir / rid
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "go.json").write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    return out
