"""The whole loop in few commands: status, next, revise and go (PLAN.md, phase 1).

Everything here is derived from files and from what earlier runs recorded, never from memory kept in
this process, so any of it can be interrupted and resumed with the same command.
"""
from __future__ import annotations

import contextlib
import json
import re
from datetime import datetime
from pathlib import Path

from . import prompts, toolchain
from . import steps as S
from .config import Config, find_report, norm, parse_consolidated
from .runner import StepResult, run_step
from .store import read_json_cached, write_json_atomic
from .workspace import changed_besides, count_tasks, git_snapshot, next_chunk, open_manual, parse_tasks

_MARKS = re.compile(r"\[[ xX]\]")

MAX_STEPS_PER_FEATURE = 24      # a guard against a loop that never settles
CHUNK_DEFAULT = 5               # tasks per implement call: a whole feature (90-170 tasks) is too much


# ---------------- what earlier runs recorded ----------------

def _runs_newest_first(cfg: Config) -> list[Path]:
    if not cfg.runs_dir.exists():
        return []
    return sorted((p for p in cfg.runs_dir.iterdir() if p.is_dir()), reverse=True)


_KINDS = ("review", "analyze", "drift")
_REVISION = re.compile(r"-r\d+$")


class RunIndex:
    """The newest recorded step per (kind, feature) over every run, built from one pass over the runs.

    `latest_record` used to walk the runs newest-first and parse each `state.json` again for every kind
    of every feature: with 20 features and 500 runs that was thousands of JSON parses per `status`.
    """

    def __init__(self, cfg: Config):
        self.records: dict[tuple[str, str], dict] = {}
        for run in _runs_newest_first(cfg):
            data = read_json_cached(run / "state.json")
            steps = (data.get("steps") if isinstance(data, dict) else None) or {}
            for key, entry in steps.items():
                for kind in _KINDS:
                    if not key.startswith(kind + "-"):
                        continue
                    rest = key[len(kind) + 1:]
                    # `review-001-a-r2` is a second round of 001-a (or a feature that really ends in -r2)
                    for feature in {rest, _REVISION.sub("", rest)}:
                        when = (entry.get("started_at") or "") if isinstance(entry, dict) else ""
                        current = self.records.get((kind, feature))
                        if current is None or when > (current.get("started_at") or ""):
                            self.records[(kind, feature)] = {**entry, "key": key, "run": run.name}
                    break

    def get(self, kind: str, feature: str) -> dict | None:
        return self.records.get((kind, feature))


_INDEX: dict = {}


def run_index(cfg: Config) -> RunIndex:
    """The index, rebuilt only when a run folder or a state.json changed (a step just recorded one)."""
    runs = _runs_newest_first(cfg)
    sig = []
    for run in runs:
        try:
            st = (run / "state.json").stat()
            sig.append((run.name, st.st_size, st.st_mtime_ns, st.st_ino))
        except OSError:
            sig.append((run.name, 0, 0, 0))
    key = (str(cfg.runs_dir), tuple(sig))
    if _INDEX.get("key") != key:
        _INDEX["key"], _INDEX["index"] = key, RunIndex(cfg)
    return _INDEX["index"]


def latest_record(cfg: Config, kind: str, feature: str) -> dict | None:
    """The newest recorded step of this kind for this feature (`review-<feature>`, `review-<feature>-r2`)."""
    if kind in _KINDS:
        return run_index(cfg).get(kind, feature)
    for run in _runs_newest_first(cfg):
        data = read_json_cached(run / "state.json")
        steps = (data.get("steps") if isinstance(data, dict) else None) or {}
        pat = re.compile(rf"{re.escape(kind)}-{re.escape(feature)}(-r\d+)?")
        hits = [(v.get("started_at") or "", k, v) for k, v in steps.items() if pat.fullmatch(k)]
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


def _review_counts(review: dict | None) -> tuple[int, int]:
    """(blockers, majors) of a recorded review, read again from its consolidated report.

    The counts saved in `state.json` come from the parser of the speckfish that ran the review; a
    report written in a format that parser did not know was recorded as 0 and stayed 0. The report
    on disk is the source of truth, so it is parsed again now and the larger count wins.
    """
    review = review or {}
    blockers, majors = int(review.get("blockers") or 0), int(review.get("majors") or 0)
    art = review.get("artifact_dir")
    if art:
        report = find_report(Path(art), "consolidated_review_report.md")
        if report is not None:
            parsed = parse_consolidated(report)
            blockers = max(blockers, len(parsed.get("blockers") or []))
            majors = max(majors, int(parsed.get("majors") or 0))
    return blockers, majors


def feature_state(cfg: Config, feature: Path) -> dict:
    name = feature.name
    tasks = parse_tasks(feature / "tasks.md")
    done, total = sum(1 for t in tasks if t["done"]), len(tasks)
    review = latest_record(cfg, "review", name)
    current = review_is_current(feature, review)           # stats the documents: once, not twice
    blockers, majors = _review_counts(review) if current else (None, None)
    return {
        "name": name, "path": norm(feature),
        "spec": (feature / "spec.md").exists(), "plan": (feature / "plan.md").exists(),
        "tasks": (feature / "tasks.md").exists(),
        "tasks_done": done, "tasks_total": total,
        "tasks_pending": len(next_chunk(tasks, 0)),      # what the agent can still do
        "tasks_manual": open_manual(tasks),              # what is left for a person
        "review": review, "review_current": current,
        "blockers": blockers, "majors": majors,
        "analyzed": _ok_record(latest_record(cfg, "analyze", name)),
        "drift": _ok_record(latest_record(cfg, "drift", name)),
    }


def next_step(cfg: Config, feature: Path, implement: bool = False, analyze: bool = True,
              drift: bool = True, majors: bool = False,
              state: dict | None = None) -> tuple[str | None, str]:
    """(kind, why) of the step this feature needs next; kind None means there is nothing to do.

    Pass `state` (from `feature_state`) when you already have it: it is not computed twice.
    """
    st = state or feature_state(cfg, feature)
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
    if majors and st["majors"]:
        return "revise", f"the review left {st['majors']} major finding(s)"
    if not implement:
        return None, "design reviewed with no blockers; add --implement --yes to write the code"
    pending = st["tasks_pending"] if "tasks_pending" in st else st["tasks_total"] - st["tasks_done"]
    if st["tasks_total"] and pending > 0:
        return "implement", f"{pending} task(s) pending"
    if drift and not st["drift"]:
        return "drift", "code written; compare it with the design"
    manual = st.get("tasks_manual") or []
    if manual:
        return None, f"done, except {len(manual)} [MANUAL] task(s) left for you: {', '.join(manual)}"
    return None, "done"


def command_for(kind: str, feature: str) -> str:
    extra = " --yes" if kind == "implement" else ""
    return f"speckfish {kind} --feature {feature}{extra}"


def status_rows(cfg: Config) -> list[dict]:
    rows = []
    for f in S.features_with_spec(cfg):
        st = feature_state(cfg, f)
        kind, why = next_step(cfg, f, implement=True, state=st)
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
              stream: bool = True, dry_run: bool = False, report: Path | None = None) -> StepResult:
    """One fix pass over the feature's documents; `report` overrides the recorded review's report."""
    report = report or review_report_for(cfg, feature.name)
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


# ---------------- implement, a chunk of tasks at a time ----------------
# One agent call over a whole tasks.md (90-170 tasks) ends early: it fixes "the critical points",
# summarises and never ticks. A few tasks per call, named one by one, and a check that they got ticked.

def chunk_args(chunk: list[dict]) -> str:
    listing = "\n".join(f"- {t['id']} {t['text']}".rstrip() for t in chunk)
    return (
        f"Implement ONLY these {len(chunk)} task(s), in order, and nothing else:\n{listing}\n\n"
        f"Write the real code and tests each one asks for. The moment a task is finished (its code "
        f"exists and the project builds), change that task's `- [ ]` to `- [x]` in tasks.md: one "
        f"edit per task, never left for the end. Do not start any other task. When all of them are "
        f"done, or one is blocked, stop and say which. For every task you could not finish (a missing "
        f"tool, a device, a decision you cannot make) do not tick it and end your final message with one "
        f"line per task: `BLOCKED: <task id> - <the reason>`.")


def reconcile_prompt(feature: Path, tasks: list[dict]) -> str:
    listing = "\n".join(f"- {t['id']} {t['text']}".rstrip() for t in tasks)
    return (
        f"These tasks of {norm(feature / 'tasks.md')} were attempted but are not ticked:\n{listing}\n\n"
        f"For each one, check the repository: if the code (and the test, when the task asks for one) "
        f"exists and is complete, change its `- [ ]` to `- [x]` in tasks.md. If it is missing or "
        f"incomplete, leave it unticked. Do not write or change any code and do not edit anything "
        f"except the checkboxes. {prompts.NO_QUESTIONS}\n\n"
        f"Finish with one line: which tasks you ticked and which you left.")


def _unticked(feature: Path, ids: list[str]) -> list[dict]:
    return [t for t in parse_tasks(feature / "tasks.md") if t["id"] in ids and not t["done"]]


def do_implement_chunk(cfg: Config, feature: Path, rid: str, harness, chunk: int = CHUNK_DEFAULT,
                       stream: bool = True, dry_run: bool = False) -> StepResult:
    """Implement the next `chunk` pending tasks, then make sure the ones that got done are ticked."""
    name = feature.name
    tasks = next_chunk(parse_tasks(feature / "tasks.md"), chunk)
    label = unique_label(cfg, rid, f"implement-{name}")
    if not tasks:
        return StepResult(step=label, kind="implement", ok=True, exit_code=0, duration_s=0.0,
                          verdict="no pending task")
    ids = [t["id"] for t in tasks]
    res = S.do_skill(cfg, "implement", chunk_args(tasks), rid, harness, stream, dry_run,
                     feature=name, step_name=label)
    if dry_run:
        return res
    left = _unticked(feature, ids)
    stuck_on_ticks = (res.error or "").startswith("implement finished without ticking")
    recovered, reconciled_ids = False, []
    if left and (res.ok or stuck_on_ticks):
        # the agent wrote code but left the boxes alone: check the work against the code and tick it
        tasks_md = feature / "tasks.md"
        text_before = tasks_md.read_text(encoding="utf-8", errors="replace")
        snap_before = git_snapshot(cfg.root)
        rec = run_step(cfg, harness, unique_label(cfg, rid, f"reconcile-{name}"),
                       S.with_language(reconcile_prompt(feature, left), cfg), cfg.root, rid,
                       cfg.egm_path(), kind="reconcile", stream=stream)
        res.reconcile_message = rec.agent_message
        if rec.cost:
            res.cost = float(res.cost or 0) + float(rec.cost)
        text_after = tasks_md.read_text(encoding="utf-8", errors="replace")
        stray = changed_besides(snap_before, git_snapshot(cfg.root), tasks_md)
        if _MARKS.sub("[ ]", text_before) != _MARKS.sub("[ ]", text_after) or stray:
            res.ok, res.error = False, "reconcile edited more than checkboxes"
            res.verdict = "reconcile edited more than checkboxes"
            return res
        newly = [t["id"] for t in parse_tasks(tasks_md) if t["id"] in ids and t["done"]
                 and t["id"] in {x["id"] for x in left}]
        reconciled_ids = newly
        left = _unticked(feature, ids)
        recovered = True
    ticked = len(ids) - len(left)
    res.tasks_done, res.tasks_total = count_tasks(feature / "tasks.md")
    res.verdict = f"{ticked}/{len(ids)} of this chunk ticked  ({res.tasks_done}/{res.tasks_total} overall)"
    if reconciled_ids:
        res.verdict += "  [reconcile ticked: " + ", ".join(reconciled_ids) + "]"
    if recovered and ticked and stuck_on_ticks and res.exit_code == 0:
        res.ok, res.error = True, ""          # only the missing ticks failed it, and the reconcile fixed that
    elif not ticked and res.ok:
        res.ok = False
        res.error = (f"none of the {len(ids)} task(s) of this chunk got ticked, "
                     f"even after checking the code against them")
    if not ticked and res.blocked:
        # the agent said why: that is the news, not "ticked nothing". Do not retry what it cannot do
        res.ok = False
        res.error_kind = "blocked"
        res.error = "blocked: " + "; ".join(res.blocked)[:400]
    return res


def implement_loop(cfg: Config, feature: Path, rid: str, harness, *, chunk: int = CHUNK_DEFAULT,
                   max_chunks: int = 0, fallback: str | None = None, budget: Budget | None = None,
                   stream: bool = True, dry_run: bool = False) -> dict:
    """Implement a feature chunk by chunk until every task is ticked or it stops making progress."""
    budget = budget or Budget(None)
    out: dict = {"name": feature.name, "steps": [], "stopped": None, "reason": "", "switched": []}
    stalled, sticky = 0, {}
    gone = [] if dry_run else toolchain.missing(cfg.root, cfg.require_tools, feature / "plan.md")
    if gone and next_chunk(parse_tasks(feature / "tasks.md"), 0):
        out.update(stopped="blocked", error_kind="blocked",
                   reason="not installed: " + "; ".join(gone) + ". Install it (or set "
                          "SPECKFISH_SKIP_TOOL_CHECK=1) and run `implement` again; nothing was started")
        return out
    for _ in range(max_chunks or 10_000):
        tasks = parse_tasks(feature / "tasks.md")
        done, total = sum(1 for t in tasks if t["done"]), len(tasks)
        if not total or not next_chunk(tasks, 0):
            manual = open_manual(tasks)
            out["reason"] = (f"all {total} task(s) ticked" if not manual else
                             f"the agent's part is done; {len(manual)} task(s) marked [MANUAL] are left for "
                             f"you: {', '.join(manual)}")
            return out
        if budget.exhausted:
            out.update(stopped="budget",
                       reason=budget.describe())
            return out
        res, switched = run_with_fallback(cfg, fallback, harness, lambda h: do_implement_chunk(
            cfg, feature, rid, h, chunk, stream, dry_run), sticky)
        if switched:
            out["switched"].append(f"implement: {switched}")
        budget.add(res)
        out["steps"].append({"kind": "implement", **res.to_dict()})
        if not res.ok:
            out.update(stopped="blocked" if res.error_kind == "blocked" else "implement",
                       reason=res.error or "the chunk failed", error_kind=res.error_kind)
            return out
        if dry_run:
            return out
        stalled = stalled + 1 if count_tasks(feature / "tasks.md")[0] <= done else 0
        if stalled >= 2:
            out.update(stopped="implement", reason="two chunks in a row ticked nothing")
            return out
    done, total = count_tasks(feature / "tasks.md")
    ran = len(out["steps"])
    out["reason"] = f"{total - done} task(s) pending after {ran} chunk(s): run it again to continue"
    return out


# ---------------- one step of one feature ----------------

def run_kind(cfg: Config, kind: str, feature: Path, rid: str, harness, *, stack: str = "",
             stream: bool = True, dry_run: bool = False, majors: bool = False,
             chunk: int = CHUNK_DEFAULT) -> StepResult:
    """Run one step for one feature with a label that stays unique within the run."""
    name = feature.name
    S.set_active_feature(cfg, feature)
    if kind == "implement":
        return do_implement_chunk(cfg, feature, rid, harness, chunk, stream, dry_run)
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


def run_with_fallback(cfg: Config, fallback: str | None, harness, call,
                      sticky: dict | None = None) -> tuple[StepResult, str | None]:
    """`call(harness)`; on a quota/auth stop retry once with the fallback harness.

    With a `sticky` dict, once the main harness was out of quota the fallback is used straight away
    for the rest of that loop instead of failing on the main one again at every step.
    """
    if sticky and sticky.get("alt") is not None:
        return call(sticky["alt"]), None
    res = call(harness)
    alt = S._fallback_for(cfg, fallback)
    if S._needs_fallback(res) and alt is not None:
        if sticky is not None:
            sticky["alt"] = alt
        return call(alt), f"{harness.name} -> {alt.name}"
    return res, None


class Budget:
    """A cap on what one `go` may spend, in ONE currency.

    Claude reports dollars, Copilot reports credits, and the two must never be added: a fallback agent in
    another currency used to count its 30 credits as 30 dollars and stop the chain. The cap applies to
    the unit of the first cost seen (`unit` fixes it up front); spending in any other unit is tracked in
    `other` and shown, but does not count against the limit. `None` or `0` means no limit.
    """

    def __init__(self, limit: float | None, unit: str | None = None):
        self.limit = limit
        self.unit = unit
        self.counted = 0.0
        self.other: dict[str, float] = {}

    @property
    def spent(self) -> float:
        return self.counted

    @spent.setter
    def spent(self, value: float) -> None:
        self.counted = float(value)

    def add_cost(self, cost, unit: str | None) -> None:
        if not cost:
            return
        unit = unit or "usd"
        if self.unit is None:
            self.unit = unit
        if unit == self.unit:
            self.counted += float(cost)
        else:
            self.other[unit] = self.other.get(unit, 0.0) + float(cost)

    def add(self, res: StepResult) -> None:
        self.add_cost(res.cost, res.cost_unit)

    @property
    def exhausted(self) -> bool:
        return bool(self.limit) and self.counted >= self.limit

    def describe(self) -> str:
        unit = f" {self.unit}" if self.unit else ""
        return f"budget of {self.limit:g}{unit} reached ({self.counted:.2f} spent)"


def advance(cfg: Config, feature: Path, rid: str, harness, *, implement: bool = False,
            analyze: bool = True, drift: bool = True, stack: str = "", max_rounds: int = 2,
            majors: bool = False, fallback: str | None = None, budget: Budget | None = None,
            stream: bool = True, dry_run: bool = False, only_one: bool = False,
            chunk: int = CHUNK_DEFAULT) -> dict:
    """Take a feature as far as the options allow, one step at a time, stopping at the first problem."""
    budget = budget or Budget(None)
    out: dict = {"name": feature.name, "steps": [], "stopped": None, "reason": "", "switched": []}
    revisions = converges = stalled = design_steps = 0
    sticky: dict = {}
    while True:
        kind, why = next_step(cfg, feature, implement=implement, analyze=analyze, drift=drift,
                              majors=majors)
        out["reason"] = why
        if kind is None:
            return out
        # only design steps count against the guard: implement is bounded by the task list and by
        # the "two chunks ticked nothing" rule, and a feature can need dozens of chunks
        if kind != "implement":
            design_steps += 1
            if design_steps > MAX_STEPS_PER_FEATURE:
                out["stopped"] = "loop"
                out["reason"] = f"no settled state after {design_steps - 1} design steps"
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
        done_before = count_tasks(feature / "tasks.md")[0] if kind == "implement" else 0
        if budget.exhausted:
            out["stopped"] = "budget"
            out["reason"] = budget.describe()
            return out
        res, switched = run_with_fallback(cfg, fallback, harness, lambda h, k=kind: run_kind(
            cfg, k, feature, rid, h, stack=stack, stream=stream, dry_run=dry_run, majors=majors,
            chunk=chunk), sticky)
        if switched:
            out["switched"].append(f"{kind}: {switched}")
        budget.add(res)
        out["steps"].append({"kind": kind, **res.to_dict()})
        if not res.ok:
            out["stopped"] = kind
            out["reason"] = res.error or "the step failed"
            out["error_kind"] = res.error_kind
            return out
        if kind == "implement":
            stalled = stalled + 1 if count_tasks(feature / "tasks.md")[0] <= done_before else 0
            if stalled >= 2:
                out["stopped"] = "implement"
                out["reason"] = "two chunks in a row ticked nothing"
                return out
        if dry_run or only_one:
            return out
        if kind == "drift" and implement and converges < 1 and (res.structured or {}).get("drift_found"):
            # the code diverged from the design: add the missing work to tasks.md, then implement again
            converges += 1
            conv, switched = run_with_fallback(cfg, fallback, harness, lambda h: run_kind(
                cfg, "converge", feature, rid, h, stream=stream, dry_run=dry_run), sticky)
            if switched:
                out["switched"].append(f"converge: {switched}")
            budget.add(conv)
            out["steps"].append({"kind": "converge", **conv.to_dict()})
            if not conv.ok:
                out["stopped"] = "converge"
                out["reason"] = conv.error or "converge failed"
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
    body, src = prompts.expand_input(material, cfg.root)
    size = len(body)
    if src:
        # for an `@file` the "body" is only the instruction to read it: measure the file itself
        with contextlib.suppress(OSError):
            size = max(size, Path(src).stat().st_size)
    return size > 6000


def do_go(cfg: Config, idea: str, *, stack: str = "", split: bool | None = None,
          max_features: int = 6, implement: bool = False, analyze: bool = True,
          chunk: int = CHUNK_DEFAULT,
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
                meter.add_cost(step.get("cost"), step.get("cost_unit"))
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
                         chunk=chunk, budget=meter, stream=stream, dry_run=dry_run)
        st = feature_state(cfg, f)
        result["state"] = {k: st[k] for k in ("spec", "plan", "tasks", "tasks_done", "tasks_total",
                                              "tasks_manual", "blockers", "review_current")}
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
    out["spent_unit"] = meter.unit
    if meter.other:
        out["spent_other"] = {u: round(v, 3) for u, v in meter.other.items()}
    out["budget"] = budget
    run_dir = cfg.runs_dir / rid
    run_dir.mkdir(parents=True, exist_ok=True)
    write_json_atomic(run_dir / "go.json", out)
    return out
