"""The steps speckfish knows: what each needs, what it produces and how its outcome is checked.

One entry per *kind* of step (`review`, `plan`, ...). A batch runs the same kind once per feature under
a different *label* (`review-001-foo`); everything here is about the kind, never the label.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .config import find_report, norm, parse_consolidated
from .workspace import _git_dirty, task_progress


@dataclass
class Ctx:
    """What a validator may look at once the agent has exited."""
    artifact_dir: Path
    root: Path | None = None
    tasks_before: tuple[int, int] | None = None
    git_before: dict | None = None


Validator = Callable[["object", Ctx], None]      # (StepResult, Ctx) -> None, mutates the result


def _md_reports(res, ctx: Ctx) -> None:
    res.reports = [norm(p) for p in sorted(ctx.artifact_dir.glob("*.md"))]


def validate_review(res, ctx: Ctx) -> None:
    report = find_report(ctx.artifact_dir, "consolidated_review_report.md")
    if report is None:
        report = find_report(ctx.artifact_dir)
    parsed = parse_consolidated(report) if report else {}
    _md_reports(res, ctx)
    if res.structured:
        res.blockers = res.structured.get("blockers") or parsed.get("blockers", [])
        res.majors = int(res.structured.get("majors") or parsed.get("majors", 0) or 0)
        res.nits = int(res.structured.get("nits") or parsed.get("nits", 0) or 0)
        res.meets_standard = res.structured.get("meets_standard")
    else:
        res.blockers = parsed.get("blockers", [])
        res.majors = parsed.get("majors", 0)
        res.nits = parsed.get("nits", 0)
        res.meets_standard = parsed.get("meets_standard")
    res.verdict = ((res.structured or {}).get("verdict") or parsed.get("verdict") or "").strip()
    if not res.reports:
        res.error = res.error or "no reports were written to the artifact directory"
        res.ok = False


def validate_specify(res, ctx: Ctx) -> None:
    # a run that writes no spec.md is a failure, not a success with a nice essay
    root = Path(ctx.root) if ctx.root else None
    if res.ok and root is not None and not list((root / "specs").glob("*/spec.md")):
        res.ok = False
        res.error = ("the agent answered but wrote no spec.md under specs/ "
                     "(read-only sandbox, or it only printed the spec)")


def validate_clarify(res, ctx: Ctx) -> None:
    # headless clarify cannot answer: it leaves the questions (and the defaults it assumed)
    _md_reports(res, ctx)
    questions = ctx.artifact_dir / "questions.md"
    if questions.exists():
        text = questions.read_text(encoding="utf-8", errors="replace")
        asks = [ln for ln in text.splitlines() if ln.strip().startswith(("-", "*"))]
        res.verdict = (f"{len(asks)} clarification question(s), each with the default it assumed "
                       f"— the spec was NOT modified. Read them, then either accept the defaults, "
                       f"edit spec.md, or re-run with --interactive")
    elif res.ok:
        res.ok = False
        res.error = ("clarify finished but wrote no questions.md "
                     "(nothing was ambiguous, or it answered in the chat)")


def make_validate_explore(kind: str) -> Validator:
    def validate(res, ctx: Ctx) -> None:
        from .prompts import EXPLORE
        _md_reports(res, ctx)
        expected = ctx.artifact_dir / EXPLORE[kind]["writes"]
        if res.ok and not expected.exists():
            res.ok = False
            res.error = (f"the agent finished but wrote no {expected.name} "
                         f"(check the log: these skills expect a human to answer)")
    return validate


def validate_drift(res, ctx: Ctx) -> None:
    _md_reports(res, ctx)
    if not res.reports:
        res.error = res.error or "no drift report was written"
        res.ok = False


def validate_implement(res, ctx: Ctx) -> None:
    root = ctx.root
    # "preparation finished" is not an implementation: this step must change the repository
    if root is not None and res.exit_code == 0 and not _git_dirty(root, ctx.git_before):
        res.ok = False
        res.error = ("the agent answered but wrote no code: nothing changed in the repository. "
                     "It likely stopped at the skill's preparation phase")
    if ctx.tasks_before is not None and root is not None:
        after = task_progress(root)
        if after:
            done_before, _total_before = ctx.tasks_before
            done_after, total = after            # the agent may have added tasks: count them
            res.tasks_done, res.tasks_total = done_after, total
            res.verdict = f"{done_after}/{total} tasks done"
            if res.exit_code == 0 and done_after <= done_before and done_before < total:
                res.ok = False
                res.error = (f"implement finished without ticking a single task in tasks.md "
                             f"({done_before}/{total} before, {done_after}/{total} after)")


@dataclass(frozen=True)
class StepSpec:
    kind: str
    scope: str = "feature"                 # "project" | "feature"
    requires: tuple[str, ...] = ()         # files in the feature folder; "a|b" = either one
    produces: str = ""                     # file or folder in the feature folder / project
    timeout_key: str = ""                  # key in config `timeouts` (default: the kind)
    schema: str = ""                       # key in prompts.SCHEMAS
    validate: Validator | None = None
    reports: bool = False                  # the step's output is Markdown reports in its artifact dir
    destructive: bool = False
    optional: bool = False                 # not part of the minimum chain
    notes: str = field(default="", compare=False)


def _spec(kind: str, **kw) -> tuple[str, StepSpec]:
    return kind, StepSpec(kind=kind, **kw)


STEPS: dict[str, StepSpec] = dict([
    _spec("constitution", scope="project", produces=".specify/memory/constitution.md"),
    _spec("specify", scope="project", schema="specify", validate=validate_specify),
    # the inventory call of `specify --split`: it writes specs/features.json, never a spec
    _spec("split", scope="project", timeout_key="specify"),
    _spec("clarify", requires=("spec.md",), validate=validate_clarify, reports=True, optional=True),
    _spec("plan", requires=("spec.md",), produces="plan.md", schema="plan"),
    _spec("tasks", requires=("plan.md",), produces="tasks.md"),
    _spec("analyze", requires=("spec.md", "plan.md", "tasks.md"), optional=True),
    _spec("checklist", requires=("spec.md",), produces="checklists", optional=True),
    _spec("review", requires=("plan.md|spec.md",), schema="review", validate=validate_review,
          reports=True),
    # not a Spec Kit skill: speckfish asks the agent to fix the design after a review
    _spec("revise", requires=("plan.md|spec.md",)),
    # ticks the tasks an implement chunk finished but did not mark: reads code, writes only tasks.md
    _spec("reconcile", requires=("tasks.md",)),
    _spec("implement", requires=("tasks.md",), validate=validate_implement, destructive=True),
    _spec("drift", requires=("spec.md",), schema="drift", validate=validate_drift, reports=True),
    _spec("converge", requires=("tasks.md",), optional=True),
    _spec("brainstorm", scope="project", validate=make_validate_explore("brainstorm"),
          reports=True, optional=True),
    _spec("draft", scope="project", validate=make_validate_explore("draft"),
          reports=True, optional=True),
])


def spec_for(kind: str) -> StepSpec | None:
    return STEPS.get(kind)


def timeout_key(kind: str) -> str:
    spec = STEPS.get(kind)
    return (spec.timeout_key or spec.kind) if spec else kind


def missing_output(kind: str, root: Path, res, feature_dir: Path | None) -> str:
    """After an agent was closed for inactivity: what this step should have written ('' = fine)."""
    spec = STEPS.get(kind)
    if spec is None:
        return "" if res.ok else (res.error or "the step did not finish")
    if spec.reports:
        return "" if res.reports else "no report was written"
    if spec.produces and spec.scope == "feature" and kind in ("plan", "tasks"):
        if feature_dir is None or not (feature_dir / spec.produces).exists():
            return f"no {spec.produces} was written"
        return ""
    # specify and implement already validated their own output in the validator
    return "" if res.ok else (res.error or "the step did not finish")
