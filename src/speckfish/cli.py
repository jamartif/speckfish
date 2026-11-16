"""Entry point: `speckfish <command>`."""
from __future__ import annotations

import argparse
import contextlib
import json
import sys
import textwrap
from pathlib import Path

from . import __version__
from . import steps as S
from .config import Config, norm
from .harnesses import get_harness
from .prompts import SKILL_SPECS

EXIT_OK, EXIT_BLOCKERS, EXIT_ERROR, EXIT_ENV = 0, 1, 2, 3


def emit(obj, as_json: bool, human: str | None = None) -> None:
    if as_json:
        print(json.dumps(obj, indent=2, ensure_ascii=False))
    else:
        print(human if human is not None else json.dumps(obj, indent=2, ensure_ascii=False))


def _rel(path: str, root: str | None) -> str:
    """Short, readable path: relative to the project when possible."""
    if not path:
        return ""
    try:
        p = Path(path)
        if root and p.is_absolute():
            return str(p.relative_to(Path(root))).replace("\\", "/")
    except (ValueError, OSError):
        pass
    return str(path).replace("\\", "/")


def _dur(seconds) -> str:
    try:
        s = float(seconds)
    except (TypeError, ValueError):
        return "-"
    return f"{int(s // 60)}m{int(s % 60):02d}s" if s >= 60 else f"{s:.0f}s"


ERROR_HINTS = {
    "quota": "fix: wait for the limit to reset, or run the same step with another agent "
             "(`--harness copilot`). Nothing was lost: retry the same command.",
    "auth": "fix: authenticate the agent CLI (`claude` / `copilot` login) and retry.",
}


def _tasks_note(res: dict) -> str:
    done, total = res.get("tasks_done"), res.get("tasks_total")
    if done is None or not total:
        return ""
    return f"  tasks {done}/{total}"


def summarize(res: dict, root: str | None = None) -> str:
    """One screen of plain text: what ran, what it found, where the report is."""
    ok = res.get("ok")
    mark = "OK" if ok else "FAILED"
    head = f"{mark}  {res.get('step')}  ·  {_dur(res.get('duration_s'))}"
    if res.get("model"):
        head += f"  ·  {res['model']}"
    if res.get("cost"):
        unit = res.get("cost_unit") or "usd"
        value = f"${res['cost']:.2f}" if unit == "usd" else f"{float(res['cost']):.1f} {unit}"
        head += f"  ·  {value}"
    head += _tasks_note(res)
    if not ok and res.get("exit_code"):
        head += f"  ·  exit {res['exit_code']}"
    lines = [head]

    verdict = (res.get("verdict") or "").strip()
    if verdict:
        # the consolidated verdict is the one thing worth reading first
        for chunk in textwrap.wrap(verdict, width=96)[:6]:
            lines.append(f"  {chunk}")
        lines.append("")

    if res.get("meets_standard") is not None:
        std = "meets the standard" if res["meets_standard"] else "does NOT meet the standard"
        counts = (f"{len(res.get('blockers') or [])} blockers · {res.get('majors', 0)} majors"
                  f" · {res.get('nits', 0)} nits")
        lines.append(f"  {std}  ·  {counts}")

    for b in (res.get("blockers") or [])[:8]:
        lines.append(f"  BLOCKER  {b}")
    if res.get("denials"):
        names = sorted({(d.get("tool_name") if isinstance(d, dict) else str(d)) for d in res["denials"]})
        lines.append(f"  WARNING  {len(res['denials'])} permission denial(s): {', '.join(names)}"
                     f" — the agent worked without them")

    reports = res.get("reports") or []
    if reports:
        rel = [_rel(r, root) for r in reports]
        main = [r for r in rel if "consolidated" in r.lower()] or rel[:1]
        lines.append("")
        lines.append(f"  {len(rel)} report(s):")
        for r in main:
            lines.append(f"    {r}   <- start here")
        for r in rel:
            if r not in main:
                lines.append(f"    {r}")

    if res.get("error"):
        lines.append(f"  ERROR  {res['error']}")
        hint = ERROR_HINTS.get(res.get("error_kind") or "")
        if hint:
            lines.append(f"  {hint}")
    if res.get("artifact_dir"):
        lines.append("")
        lines.append(f"  artifacts: {_rel(res['artifact_dir'], root)}")
    if res.get("log"):
        lines.append(f"  log:       {_rel(res['log'], root)}")
    return "\n".join(lines)


def split_summary(out: dict, root: str | None = None) -> str:
    """Short and final: how many specs, where they are, how long it took. No feature dump, no JSON."""
    feats = out.get("features") or []
    specs = out.get("specs") or []
    total = sum(float(i.get("duration_s") or 0) for i in (out.get("steps") or {}).values())
    head = f"run {out.get('run_id')}  ·  {_dur(total)}"

    if out.get("error"):
        return "\n".join([head, f"  ERROR  {out['error']}"])
    if out.get("dry_run"):
        lines = [f"dry run: {len(feats)} feature(s) would be specified (nothing written)"]
        lines += [f"  {i}. {t}" for i, t in enumerate(feats, 1)]
        lines.append("")
        lines.append(f"  {head}")
        return "\n".join(lines)
    if out.get("inventory_only"):
        lines = [f"{len(feats)} feature(s) proposed  ·  nothing generated yet"]
        lines += [f"  {i}. {t}" for i, t in enumerate(feats, 1)]
        lines.append("")
        lines.append("  cut saved to specs/features.json — drop or merge entries there,")
        lines.append("  then re-run without --inventory-only to generate the specs")
        lines.append(f"  {head}")
        return "\n".join(lines)

    lines = [f"{len(specs)} spec(s) generated"]
    for item in specs:
        lines.append(f"  {_rel(item['path'], root)}")
    if out.get("stopped_at"):
        lines.append(f"  stopped at {out['stopped_at']}: "
                     f"{len(specs)} of {len(feats)} feature(s) written")
    elif len(specs) < len(feats):
        lines.append(f"  WARNING: {len(feats) - len(specs)} feature(s) produced no spec.md "
                     f"(see the log)")
    lines.append("")
    lines.append(f"  {head}")
    return "\n".join(lines)


# what each step produces, so `--skip-existing` knows what "already done" means
# Only steps that own a file can be skipped. `analyze`, `clarify` and `converge` write into the run
# directory (or append), so treating "tasks.md exists" as "analyze is done" would skip everything.
STEP_ARTIFACT = {
    "plan": "plan.md",
    "tasks": "tasks.md",
    "checklist": "checklists",
    "analyze": None,
    "clarify": None,
    "converge": None,
}


def add_feature_selection(sp) -> None:
    """The same resume flags everywhere: one feature, a list, a range, or all of them."""
    sp.add_argument("--all-features", action="store_true",
                    help="run this step on every feature in order, stopping at the first failure")
    sp.add_argument("--only", default=None,
                    help="only these features, by position: 2,4,5")
    sp.add_argument("--from-feature", type=int, default=None,
                    help="from this feature position onwards")
    sp.add_argument("--skip-existing", action="store_true",
                    help="skip the features that already have the artifact for this step")


def _positions(value: str) -> list[int] | None:
    if not value:
        return None
    try:
        return [int(x) for x in str(value).replace(" ", "").split(",") if x]
    except ValueError as exc:
        raise SystemExit(f"--only expects numbers, e.g. 2,4,5 (got {value!r})") from exc


def select_features(cfg, args) -> list:
    """Every step resolves `--feature` / `--only` / `--from-feature` / `--all-features` the same way."""
    feats = S.features_with_spec(cfg)
    only = _positions(getattr(args, "only", None))
    if getattr(args, "feature", None):
        return [S.find_feature(cfg, args.feature)]
    if only:
        return [f for i, f in enumerate(feats, start=1) if i in only]
    start = getattr(args, "from_feature", None)
    if start:
        return feats[max(0, int(start) - 1):]
    return feats


def batch_exit_code(batch: dict) -> int:
    """A quota stop is an environment problem (3), not a failed step (2): CI can tell them apart."""
    stopped = batch.get("stopped_at")
    if not stopped:
        return EXIT_OK
    entry = (batch.get("steps") or {}).get(stopped) or {}
    return EXIT_ENV if entry.get("error_kind") in ("quota", "auth") else EXIT_ERROR


def is_batch(args) -> bool:
    return bool(getattr(args, "all_features", False) or getattr(args, "only", None)
                or getattr(args, "from_feature", None))


def batch_summary(batch: dict, root: str | None = None) -> str:
    """Per-feature result of a --all-features run."""
    lines = [f"{batch.get('action')}  ·  {len(batch.get('order') or [])} feature(s)  ·  "
             f"run {batch.get('run_id')}"]
    for name, step in (batch.get("steps") or {}).items():
        ok = step.get("ok")
        mark = "OK    " if ok else "FAILED"
        line = f"  {mark} {name}  ·  {_dur(step.get('duration_s'))}"
        if step.get("cost"):
            line += f"  ·  ${float(step['cost']):.2f}"
        lines.append(line)
        if not ok and step.get("error"):
            lines.append(f"         {step['error']}")
    for switch in batch.get("switched") or []:
        lines.append(f"  fallback: {switch} (quota/auth on the primary harness)")
    if batch.get("skipped"):
        lines.insert(1, f"  skipped {len(batch['skipped'])} already done: {', '.join(batch['skipped'])}")
    if batch.get("stopped_at"):
        lines.append(f"  stopped at {batch['stopped_at']}: fix it and re-run, earlier features are done")
    else:
        lines.append("  all features done")
    if batch.get("total_cost"):
        lines.append(f"  total: ${batch['total_cost']:.2f}")
    detail = batch.get("detail_file") or "batch.json"
    lines.append(f"  per-feature detail: .speckfish/runs/{batch.get('run_id')}/{detail}")
    return "\n".join(lines)


def doctor_summary(info: dict) -> str:
    """Human-readable doctor output (nothing but facts, no JSON dumps, no ASCII banners)."""
    lines = [f"root: {info.get('root')}"]

    h = info.get("harness") or {}
    if h.get("error"):
        lines.append(f"harness: MISSING — {h['error']}")
    else:
        flags = "verified" if h.get("verified") else "unverified adapter"
        lines.append(f"harness: {h.get('harness')} {h.get('version', '')} ({flags})")

    s = info.get("specify") or {}
    if s.get("error"):
        lines.append(f"specify: MISSING — {s['error']}")
    else:
        state = "ok" if s.get("ok") else "FAILED"
        lines.append(f"specify: {s.get('version', 'unknown')}  [{state}]")
        lines.append(f"         via {' '.join(s.get('argv') or [])}")

    e = info.get("egm") or {}
    if e.get("present"):
        skills = e.get("skills") or []
        lines.append(f"egm:     {e.get('version')} at {e.get('path')}  ({len(skills)} skills)")
    else:
        lines.append("egm:     not found — run `speckfish init` (or set egm_root in "
                     ".speckfish/config.json)")

    p = info.get("project") or {}
    def yn(value) -> str:
        return "yes" if value else "no"

    lines.append(f"project: git={yn(p.get('has_git'))} specify={yn(p.get('has_specify'))} "
                 f"agent-skills={yn(p.get('has_claude_skills'))}")
    if p.get("feature"):
        lines.append(f"         latest feature: {p['feature']}")
    tools = info.get("tools") or {}
    missing = [t for t, v in tools.items() if not v.get("installed")]
    if missing:
        lines.append(f"tools:   missing {', '.join(missing)}")
    return "\n".join(lines)


def exit_for(res: dict) -> int:
    if not res.get("ok"):
        # out of quota or bad credentials is the environment, not the document
        if res.get("error_kind") in ("quota", "auth"):
            return EXIT_ENV
        return EXIT_ERROR
    # exit 1 only with blockers: "does not meet the standard" is read from the JSON
    kind = res.get("kind") or str(res.get("step") or "").split("-")[0]
    if kind in ("review", "drift") and res.get("blockers"):
        return EXIT_BLOCKERS
    return EXIT_OK


GLOBAL_FLAGS_VALUE = {"--root", "--harness", "--model", "--fallback"}
GLOBAL_FLAGS_BOOL = {"--json", "--stream", "--quiet", "--dry-run"}


def normalize_argv(argv: list[str]) -> list[str]:
    """Allow global flags before or after the subcommand (argparse only accepts them before)."""
    head: list[str] = []
    rest: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in GLOBAL_FLAGS_VALUE:
            head.append(a)
            if i + 1 < len(argv):
                head.append(argv[i + 1])
                i += 2
                continue
            i += 1
            continue
        if a in GLOBAL_FLAGS_BOOL:
            head.append(a)
            i += 1
            continue
        if a.startswith("--root=") or a.startswith("--harness="):
            head.append(a)
            i += 1
            continue
        rest.append(a)
        i += 1
    return head + rest


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--version", action="version", version=f"speckfish {__version__}",
                        help="show the version and exit")
    common.add_argument("--root", default=".", help="project root (default: cwd)")
    common.add_argument("--harness", default=None, help="claude | copilot | codex (default: auto)")
    common.add_argument("--fallback", default=None,
                        help="if a step stops for quota/auth, retry it once with this harness")
    common.add_argument("--model", default=None,
                        help="model passed to the agent CLI (default: the CLI's own choice)")
    common.add_argument("--json", action="store_true", help="JSON output on stdout")
    common.add_argument("--stream", action="store_true", default=True,
                        help="forward agent output to stderr (default)")
    common.add_argument("--quiet", action="store_true", help="do not forward agent output")
    common.add_argument("--no-progress", action="store_true",
                        help="do not print the live progress line while an agent works")
    common.add_argument("--dry-run", action="store_true", help="print the command without running it")
    p = argparse.ArgumentParser(prog="speckfish", parents=[common],
                                description="Headless orchestrator for Spec Kit + EGM over agent CLIs.")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("init", help="prepare the project: git + specify init + EGM")
    sp.add_argument("--integration", default=None,
                    help="claude | copilot | codex | all | comma list (default: every installed agent)")
    sp.add_argument("--no-egm", action="store_true", help="do not clone/install EGM")
    sp.add_argument("--no-editors", dest="editors", action="store_false", default=True,
                    help="skip the VS Code tasks and agent slash commands (they are generated by default)")
    sp.add_argument("--prefix", default="spfish",
                    help="prefix for the generated slash commands (default: spfish)")

    sub.add_parser("doctor", help="check environment, harness, specify and EGM")

    sp = sub.add_parser("specify", help="create/update spec.md")
    sp.add_argument("description", nargs="?", default="",
                    help="feature description, or @path/to/brief.md (omit with --only/--from-feature)")
    sp.add_argument("--run-id", default=None)
    sp.add_argument("--split", action="store_true",
                    help="split a big brief into independent features and specify each one")
    sp.add_argument("--max-features", type=int, default=6,
                    help="how many features --split may produce at most (default: 6)")
    sp.add_argument("--only", default=None,
                    help="with --split: generate only these features from specs/features.json "
                         "(e.g. 4,5,6,7) — continues an interrupted split without paying again")
    sp.add_argument("--from-feature", type=int, default=None,
                    help="with --split: generate from this feature number onwards")
    sp.add_argument("--inventory-only", action="store_true",
                    help="with --split: only list the features it would create, write specs/features.json "
                         "and stop (review the cut before paying for N specs)")
    sp.add_argument("--review", action="store_true",
                    help="run the EGM goldfish review on the spec(s) right after creating them")

    sp = sub.add_parser("plan", help="generate plan.md and design artifacts")
    sp.add_argument("stack", nargs="?", default="", help="stack and constraints")
    sp.add_argument("--run-id", default=None)
    sp.add_argument("--feature", default=None,
                    help="feature to plan: number (002), directory name or path (default: the active one)")
    add_feature_selection(sp)
    sp.add_argument("--with-clarify", action="store_true",
                    help="run clarify first; stop if it leaves questions unless --accept-defaults")
    sp.add_argument("--accept-defaults", action="store_true",
                    help="with --with-clarify: plan even if questions were left unanswered")


    sp = sub.add_parser("review", help="Goldfish review of a document")
    sp.add_argument("target", nargs="?", default=None, help="document (default: plan.md of the latest feature)")
    sp.add_argument("--profile", default=None, help="speckit | none (default: speckit)")
    sp.add_argument("--stage", default="uninterrupted", help="uninterrupted | feature_spec | implementation | 5..9")
    sp.add_argument("--target-file", default="plan.md", help="default document")
    sp.add_argument("--run-id", default=None)
    sp.add_argument("--interactive", action="store_true",
                    help="run the agent in this terminal so it can ask you questions")
    sp.add_argument("--with-analyze", action="store_true",
                    help="run the Spec Kit analyze step on this feature before reviewing it")
    add_feature_selection(sp)

    sp = sub.add_parser("drift", help="drift between designs and code")
    sp.add_argument("target", nargs="?", default=None, help="specs directory (default: latest feature)")
    sp.add_argument("--run-id", default=None)
    add_feature_selection(sp)

    sp = sub.add_parser("pipeline", help="specify → plan → review (→ drift)")
    sp.add_argument("description", help="feature description")
    sp.add_argument("--stack", default="")
    sp.add_argument("--target", default=None)
    sp.add_argument("--gate", action="store_true", help="stop when a step fails or there are blockers")
    sp.add_argument("--with-drift", action="store_true",
                    help="also run drift at the end (skipped if --gate stopped the chain)")
    sp.add_argument("--with-tasks", action="store_true", help="also generate tasks.md before the review")
    sp.add_argument("--with-analyze", action="store_true",
                    help="also run the spec/plan/tasks consistency analysis before the review")

    sp = sub.add_parser("editors", help="generate VS Code tasks and agent slash commands")
    sp.add_argument("--targets", default="vscode,copilot,claude,codex",
                    help="comma list of: vscode, copilot, claude, codex (default: all)")
    sp.add_argument("--command", default=None,
                    help="command the generated tasks call (default: auto-detect)")
    sp.add_argument("--codex-home", default=None,
                    help="Codex home directory for its prompts (default: $CODEX_HOME or ~/.codex)")
    sp.add_argument("--no-force", action="store_true", help="do not overwrite existing files")
    sp.add_argument("--prefix", default="spfish",
                    help="prefix for the generated slash commands (default: spfish)")

    # one subcommand per remaining Spec Kit skill (constitution, tasks, analyze, checklist, ...)
    for skill, spec in SKILL_SPECS.items():
        sp = sub.add_parser(skill, help=f"{spec['skill']} → writes {spec['writes']}")
        sp.add_argument("args", nargs="?", default="", help=spec["hint"] or "no arguments")
        sp.add_argument("--run-id", default=None)
        sp.add_argument("--feature", default=None,
                        help="feature to work on: number (002), name or path (default: the active one)")
        if skill not in ("constitution",):
            add_feature_selection(sp)
        if spec.get("dangerous"):
            sp.add_argument("--yes", action="store_true",
                            help="required: this step writes source code")

    def add_flow_options(sp, implement: bool = True) -> None:
        sp.add_argument("--max-rounds", type=int, default=2,
                        help="review -> revise rounds before giving up (default: 2)")
        sp.add_argument("--majors", action="store_true",
                        help="revise fixes the Majors too, not only the Blockers")
        if implement:
            sp.add_argument("--implement", action="store_true",
                            help="also write the code (needs --yes) and check drift afterwards")
            sp.add_argument("--yes", action="store_true", help="confirm that --implement writes code")
            sp.add_argument("--no-analyze", action="store_true", help="skip the analyze step")
            sp.add_argument("--no-drift", action="store_true", help="skip drift after implement")

    sp = sub.add_parser("go", help="idea -> specs -> plans -> tasks -> reviewed design (-> code), resumable")
    sp.add_argument("description", nargs="?", default="",
                    help="idea, or @path/to/brief.md (omit to resume the features that exist)")
    sp.add_argument("--stack", default="", help="stack and constraints for the plans")
    sp.add_argument("--split", dest="split", action="store_true", default=None,
                    help="cut the brief into independent features (automatic for a long brief)")
    sp.add_argument("--no-split", dest="split", action="store_false", help="never cut the brief")
    sp.add_argument("--max-features", type=int, default=6)
    sp.add_argument("--budget", type=float, default=None,
                    help="stop before a step once this much was spent (usd or credits)")
    sp.add_argument("--no-init", action="store_true", help="do not run init when .specify is missing")
    add_flow_options(sp)

    sp = sub.add_parser("next", help="run the one step this feature needs next")
    sp.add_argument("--feature", default=None, help="number, name or path (default: the current one)")
    sp.add_argument("--stack", default="", help="stack and constraints, when the next step is plan")
    sp.add_argument("--explain", action="store_true", help="only say what the next step is")
    add_flow_options(sp)

    sp = sub.add_parser("revise", help="review -> fix the blockers -> review again, until clean")
    sp.add_argument("--feature", default=None, help="number, name or path (default: the current one)")
    add_flow_options(sp, implement=False)

    sub.add_parser("status", help="where every feature stands, what is next and what it cost")

    sp = sub.add_parser("config", help="show or change the project defaults")
    sp.add_argument("set", nargs="?", default=None,
                    help="KEY=VALUE to store (harness, model, profile, egm_root, specify, language)")

    sub.add_parser("features", help="list features under specs/ and which one is active")

    sp = sub.add_parser("runs", help="list runs")
    sp.add_argument("--clean", action="store_true", help="delete old runs, keeping --keep newest")
    sp.add_argument("--keep", type=int, default=10, help="how many runs to keep (default: 10)")
    for _kind, _help in (("brainstorm", "EGM exploration: concepts brief from a vague idea"),
                         ("draft", "EGM exploration: design draft from a concepts brief")):
        sp = sub.add_parser(_kind, help=_help)
        sp.add_argument("material", nargs="?", default="",
                        help="idea, @path/to/brief.md or a file path")
        sp.add_argument("--run-id", default=None)

    sp = sub.add_parser("report", help="print the review/drift report of a run in the terminal")
    sp.add_argument("run_id", nargs="?", default=None,
                    help="run id (default: the latest run that has a report)")
    sp.add_argument("--full", action="store_true", help="print the whole report instead of the summary")
    sp.add_argument("--lines", type=int, default=40, help="how many lines to print without --full")

    sp = sub.add_parser("show", help="details of a run")
    sp.add_argument("run_id", nargs="?", default=None)
    return p


class Run:
    """Everything a command handler needs: config, parsed args and how to talk to the user."""

    def __init__(self, cfg: Config, args):
        self.cfg = cfg
        self.args = args
        self.as_json = args.json
        self.stream = not args.quiet
        self._harness = None

    @property
    def root(self) -> str:
        return norm(self.cfg.root)

    def harness(self):
        """Resolved lazily: `config`, `runs`, `show` and `version` must work with no agent installed."""
        if self._harness is None:
            self._harness = get_harness(self.cfg, self.args.harness)
        return self._harness

    def emit(self, obj, human: str | None = None) -> None:
        emit(obj, self.as_json, human)

    def finish(self, res) -> int:
        """Print one step's outcome and turn it into an exit code."""
        self.emit(res.to_dict(), summarize(res.to_dict(), self.root))
        return exit_for(res.to_dict())

    def finish_batch(self, batch: dict) -> int:
        self.emit(batch, batch_summary(batch, self.root))
        return batch_exit_code(batch)

    def batch_features(self, empty_hint: str) -> list | None:
        feats = select_features(self.cfg, self.args)
        if not feats:
            print(empty_hint, file=sys.stderr)
            return None
        return feats


NO_FEATURE_HINT = "no feature matched: run `speckfish specify` first, or check --only"
NO_SPEC_HINT = "no feature has a spec.md yet: run `speckfish specify` first"


def cmd_editors(run: Run) -> int:
    from .editors import generate
    args = run.args
    targets = [t.strip() for t in args.targets.split(",") if t.strip()]
    gen = generate(run.cfg, targets, args.command, args.codex_home,
                   force=not args.no_force, prefix=args.prefix)
    human = [f"command: {gen['command']}  prefix: {gen['prefix']}-*",
             f"written: {len(gen['written'])}"]
    human += [f"  {p}" for p in gen["written"]]
    if gen.get("codex_dir"):
        human.append(f"codex prompts (user-global): {gen['codex_dir']}")
    if gen.get("removed"):
        human.append(f"removed {len(gen['removed'])} stale command(s) from the old prefix")
    if gen["skipped"]:
        human += [f"skipped: {s['path']} ({s['reason']})" for s in gen["skipped"]]
    run.emit(gen, "\n".join(human))
    return EXIT_OK


def cmd_init(run: Run) -> int:
    args = run.args
    out = S.do_init(run.cfg, args.integration, skip_egm=args.no_egm,
                    editors=getattr(args, "editors", True),
                    editor_prefix=getattr(args, "prefix", "spfish"))
    ok_int = ", ".join(out.get("installed") or []) or "none"
    failed = [a["integration"] for a in out["actions"]
              if a.get("integration") and not a.get("specify_init")]
    human = [f"integrations installed: {ok_int}"]
    if failed:
        human.append(f"failed: {', '.join(failed)}")
    human.append(f"egm: {out.get('egm_root')}")
    run.emit(out, "\n".join(human))
    return EXIT_OK if all(a.get("specify_init", True) for a in out["actions"]) else EXIT_ERROR


def cmd_doctor(run: Run) -> int:
    info = S.do_doctor(run.cfg)
    run.emit(info, doctor_summary(info))
    ok = (info.get("harness") or {}).get("installed") and \
        (info.get("specify") or {}).get("ok") and (info.get("egm") or {}).get("present")
    return EXIT_OK if ok else EXIT_ENV


def _split_selection(run: Run) -> tuple[list[int] | None, int | None]:
    """The `--only` / `--from-feature` selection of `specify --split` (an error code when invalid)."""
    args, cfg = run.args, run.cfg
    only = None
    if args.only:
        try:
            only = [int(x) for x in str(args.only).replace(" ", "").split(",") if x]
        except ValueError:
            print("ERROR: --only expects numbers, e.g. 4,5,6,7", file=sys.stderr)
            return None, EXIT_ENV
    if args.from_feature and only is None:
        cut = cfg.root / "specs" / "features.json"
        total = 0
        if cut.exists():
            with contextlib.suppress(Exception):
                total = len(json.loads(cut.read_text(encoding="utf-8")).get("features") or [])
        if not total:
            print("ERROR: --from-feature needs specs/features.json "
                  "(run --split --inventory-only first)", file=sys.stderr)
            return None, EXIT_ENV
        only = list(range(args.from_feature, total + 1))
    return only, None


def cmd_specify(run: Run) -> int:
    args, cfg = run.args, run.cfg
    if args.split:
        only, error = _split_selection(run)
        if error is not None:
            return error
        out = S.do_specify_split(cfg, args.description, args.run_id, run.harness(),
                                 run.stream, args.dry_run, args.max_features,
                                 review=args.review,
                                 inventory_only=getattr(args, "inventory_only", False),
                                 only=only)
        run.emit(out, split_summary(out, run.root))
        return EXIT_OK if out.get("ok") or out.get("dry_run") else EXIT_ERROR
    return run.finish(S.do_specify(cfg, args.description, args.run_id, run.harness(), run.stream,
                                   args.dry_run, review=getattr(args, "review", False)))


def _refuse_chain_in_batch(args, flag: str) -> int:
    # chaining runs one feature at a time; silently dropping it would plan/review unchecked
    print(f"{flag} works on one feature at a time: drop --all-features/--only/--from-feature "
          f"or pass --feature NNN", file=sys.stderr)
    return EXIT_ENV


def cmd_plan(run: Run) -> int:
    args, cfg = run.args, run.cfg
    with_clarify = getattr(args, "with_clarify", False)
    if with_clarify and is_batch(args):
        return _refuse_chain_in_batch(args, "--with-clarify")
    if with_clarify:
        feature = args.feature or (S.active_feature(cfg).name if S.active_feature(cfg) else None)
        if not feature:
            print("--with-clarify needs a feature: pass --feature NNN", file=sys.stderr)
            return EXIT_ENV
        clar = S.do_skill(cfg, "clarify", "", args.run_id, run.harness(), run.stream,
                          args.dry_run, feature=feature)
        run.emit(clar.to_dict(), summarize(clar.to_dict(), run.root))
        if not clar.ok:
            return EXIT_ERROR
        if clar.verdict and not args.accept_defaults:
            print("\nclarify left questions above: answer them (or accept the defaults with "
                  "--accept-defaults) and re-run plan", file=sys.stderr)
            return EXIT_OK
        return run.finish(S.do_plan(cfg, args.stack, args.run_id, run.harness(), run.stream,
                                    args.dry_run, feature=feature))
    if is_batch(args):
        feats = run.batch_features(NO_FEATURE_HINT)
        if feats is None:
            return EXIT_ERROR
        return run.finish_batch(S.do_feature_batch(
            cfg, "plan", feats, run.harness(), args.run_id, stack=args.stack, stream=run.stream,
            dry_run=args.dry_run, skip_existing="plan.md" if args.skip_existing else None,
            fallback=getattr(args, "fallback", None)))
    return run.finish(S.do_plan(cfg, args.stack, args.run_id, run.harness(), run.stream,
                                args.dry_run, feature=args.feature))


def cmd_review(run: Run) -> int:
    args, cfg = run.args, run.cfg
    with_analyze = getattr(args, "with_analyze", False)
    if with_analyze and is_batch(args):
        return _refuse_chain_in_batch(args, "--with-analyze")
    if is_batch(args):
        feats = run.batch_features(NO_SPEC_HINT)
        if feats is None:
            return EXIT_ERROR
        batch = S.do_review_batch(cfg, feats, run.harness(), args.run_id, run.stream,
                                  args.dry_run, args.profile, args.stage,
                                  skip_existing=args.skip_existing,
                                  fallback=getattr(args, "fallback", None))
        run.emit(batch, batch_summary(batch, run.root))
        if not batch.get("done"):
            return batch_exit_code(batch)
        return EXIT_BLOCKERS if any((s.get("blockers") or [])
                                    for s in batch["steps"].values()) else EXIT_OK
    if with_analyze:
        # without a target the review defaults to the current feature: analyze that same one
        doc = Path(args.target) if args.target else S.resolve_target(cfg, None, args.target_file)
        ana = S.do_skill(cfg, "analyze", "", args.run_id, run.harness(), run.stream,
                         args.dry_run, feature=doc.parent.name or None)
        run.emit(ana.to_dict(), summarize(ana.to_dict(), run.root))
        if not ana.ok:
            return EXIT_ERROR
        return run.finish(S.do_review(cfg, str(doc), args.run_id, run.harness(), args.profile,
                                      args.stage, run.stream, args.dry_run, args.target_file,
                                      interactive=getattr(args, "interactive", False)))
    return run.finish(S.do_review(cfg, args.target, args.run_id, run.harness(), args.profile,
                                  args.stage, run.stream, args.dry_run, args.target_file,
                                  interactive=getattr(args, "interactive", False)))


def cmd_drift(run: Run) -> int:
    args, cfg = run.args, run.cfg
    if is_batch(args):
        feats = run.batch_features(NO_SPEC_HINT)
        if feats is None:
            return EXIT_ERROR
        return run.finish_batch(S.do_drift_batch(
            cfg, feats, run.harness(), args.run_id, run.stream, args.dry_run,
            skip_existing=args.skip_existing, fallback=getattr(args, "fallback", None)))
    return run.finish(S.do_drift(cfg, args.target, args.run_id, run.harness(), run.stream,
                                 args.dry_run))


def cmd_skill(run: Run) -> int:
    """constitution, tasks, analyze, checklist, clarify, implement, converge."""
    args, cfg = run.args, run.cfg
    spec = SKILL_SPECS[args.cmd]
    if spec.get("dangerous") and not getattr(args, "yes", False) and not args.dry_run:
        msg = (f"`{args.cmd}` is destructive: {spec['writes']}. "
               f"Re-run with --yes to confirm, or --dry-run to see the command first.")
        run.emit({"error": msg, "writes": spec["writes"]}, f"refusing: {msg}")
        return EXIT_ENV
    if is_batch(args):
        feats = run.batch_features(NO_FEATURE_HINT)
        if feats is None:
            return EXIT_ERROR
        return run.finish_batch(S.do_feature_batch(
            cfg, args.cmd, feats, run.harness(), args.run_id, args=args.args, stream=run.stream,
            dry_run=args.dry_run,
            skip_existing=STEP_ARTIFACT.get(args.cmd) if args.skip_existing else None,
            fallback=getattr(args, "fallback", None)))
    return run.finish(S.do_skill(cfg, args.cmd, args.args, args.run_id, run.harness(), run.stream,
                                 args.dry_run, feature=getattr(args, "feature", None)))


def cmd_explore(run: Run) -> int:
    """brainstorm / draft."""
    args, cfg = run.args, run.cfg
    material = (args.material or "").strip()
    if not material:
        print(f"{args.cmd} needs something to work on: an idea or @path/to/file.md", file=sys.stderr)
        return EXIT_ENV
    material, _src = S.prompts.expand_input(material, cfg.root)
    return run.finish(S.do_egm_explore(cfg, args.cmd, material, args.run_id, run.harness(),
                                       run.stream, args.dry_run))


def cmd_pipeline(run: Run) -> int:
    args = run.args
    out = S.do_pipeline(run.cfg, args.description, args.stack, args.target,
                        args.gate, args.with_drift, args.with_tasks, args.with_analyze,
                        run.stream, args.dry_run)
    run.emit(out, json.dumps(out, indent=2, ensure_ascii=False))
    if not out.get("ok"):
        return EXIT_ERROR
    if any((s.get("blockers") or []) for s in out["steps"].values()):
        return EXIT_BLOCKERS
    return EXIT_OK


def cmd_features(run: Run) -> int:
    rows = S.list_features(run.cfg)
    human = []
    for r in rows:
        mark = "*" if r["active"] else " "
        arts = "".join("+" if r[k] else "-" for k in ("spec", "plan", "tasks"))
        human.append(f"{mark} {r['name']:<34} [{arts}]")
    if rows:
        human.append("")
        human.append("* active (Spec Kit will work on it)   [+spec +plan +tasks]")
    run.emit({"features": rows}, "\n".join(human) or "(no features: run `speckfish specify`)")
    return EXIT_OK


def cmd_config(run: Run) -> int:
    from dataclasses import asdict
    args, cfg = run.args, run.cfg
    if args.set:
        if "=" not in args.set:
            run.emit({"error": "expected KEY=VALUE"}, "expected KEY=VALUE")
            return EXIT_ENV
        key, value = args.set.split("=", 1)
        key = key.strip()
        allowed = {"harness", "model", "profile", "egm_root", "specify", "language"}
        if key.startswith("timeout."):
            step = key.split(".", 1)[1]
            if not value.strip().isdigit():
                run.emit({"error": "timeout must be a number of seconds"},
                         "ERROR: timeout must be a number of seconds")
                return EXIT_ENV
            cfg.timeouts[step] = int(value.strip())
            path = cfg.save()
            run.emit({"saved": norm(path), key: cfg.timeouts[step]},
                     f"saved timeout.{step}={cfg.timeouts[step]}s → {norm(path)}")
            return EXIT_OK
        if key not in allowed:
            msg = f"unknown key '{key}' (allowed: {', '.join(sorted(allowed))})"
            run.emit({"error": msg}, f"ERROR: {msg}")
            return EXIT_ENV
        setattr(cfg, key, value.strip())
        path = cfg.save()
        run.emit({"saved": norm(path), key: value.strip()},
                 f"saved {key}={value.strip()} → {norm(path)}")
        return EXIT_OK
    data = asdict(cfg)
    data.pop("root", None)
    run.emit(data, "\n".join(f"{k}: {v}" for k, v in data.items()))
    return EXIT_OK


def cmd_runs(run: Run) -> int:
    args, cfg = run.args, run.cfg
    base = cfg.runs_dir
    runs = sorted([p.name for p in base.iterdir() if p.is_dir()], reverse=True) if base.exists() else []
    if args.clean:
        out = S.clean_runs(cfg, args.keep)
        run.emit(out, f"removed {len(out['removed'])} run(s); kept {len(out['kept'])}")
        return EXIT_OK
    rows = []
    for rid in runs:
        summary = S.run_summary(cfg, rid)
        rows.append({"run": rid, "steps": list((summary.get("steps") or {}).keys()),
                     "cost": summary.get("cost"), "cost_unit": summary.get("cost_unit"),
                     "duration_s": summary.get("duration_s")})
    human = []
    for r in rows:
        cost = f"{r['cost']} {r['cost_unit']}" if r["cost"] else "-"
        dur = f"{r['duration_s']}s" if r["duration_s"] else "-"
        human.append(f"{r['run']}  steps={len(r['steps'])}  cost={cost}  "
                     f"{dur}  [{','.join(r['steps'])}]")
    run.emit({"runs": rows}, "\n".join(human) or "(no runs)")
    return EXIT_OK


def cmd_report(run: Run) -> int:
    args, cfg = run.args, run.cfg
    base = cfg.runs_dir
    runs = sorted([p for p in base.iterdir() if p.is_dir()], reverse=True) if base.exists() else []
    target = None
    if args.run_id:
        cand = base / args.run_id
        target = cand if cand.is_dir() else None
    else:
        for d in runs:
            if list((d / "artifacts").glob("*/*.md")):
                target = d
                break
    if target is None:
        print(f"no report found in {_rel(norm(base), run.root)}"
              + (f" for run {args.run_id}" if args.run_id else " — run a review first"),
              file=sys.stderr)
        return EXIT_ERROR
    mds = sorted((target / "artifacts").glob("*/*.md"))
    main_reports = [m for m in mds if "consolidated" in m.name.lower()]
    chosen = (main_reports or mds)[0]
    text = chosen.read_text(encoding="utf-8", errors="replace")
    if run.as_json:
        emit({"run_id": target.name, "report": norm(chosen), "text": text}, True, "")
        return EXIT_OK
    body = text.splitlines()
    shown = body if args.full else body[: args.lines]
    print("\n".join(shown))
    if not args.full and len(body) > len(shown):
        print(f"\n… {len(body) - len(shown)} more line(s): {_rel(norm(chosen), run.root)}")
        print("  use `speckfish report --full` to print everything")
    return EXIT_OK


def cmd_show(run: Run) -> int:
    cfg = run.cfg
    base = cfg.runs_dir
    rid = run.args.run_id
    if not rid:
        cands = sorted([p for p in base.iterdir() if p.is_dir()]) if base.exists() else []
        rid = cands[-1].name if cands else None
    if not rid or not (base / rid).exists():
        run.emit({"error": "run not found"}, "run not found")
        return EXIT_ERROR
    data = S.run_summary(cfg, rid)
    human = [f"run {rid}: {len(data.get('steps') or {})} step(s)"]
    for step, info in (data.get("steps") or {}).items():
        cost = f"{info.get('cost')} {info.get('cost_unit')}" if info.get("cost") else "-"
        human.append(f"  {step:<12} {info.get('status'):<7} {info.get('duration_s')}s  "
                     f"cost={cost}  model={info.get('model') or '-'}  "
                     f"blockers={info.get('blockers')}")
    if data.get("cost"):
        human.append(f"total: {data['cost']} {data.get('cost_unit')} in {data.get('duration_s')}s")
    run.emit(data, "\n".join(human))
    return EXIT_OK


def flow_summary(out: dict) -> str:
    """Per feature: the steps that ran, where it stopped and why. One screen, no JSON."""
    lines = []
    if out.get("note"):
        lines.append(out["note"])
    if out.get("error"):
        lines.append(f"ERROR  {out['error']}")
    for name, feat in (out.get("features") or {}).items():
        st = feat.get("state") or {}
        tasks = f"  tasks {st['tasks_done']}/{st['tasks_total']}" if st.get("tasks_total") else ""
        review = ""
        if st.get("blockers") is not None:
            review = f"  review: {st['blockers']} blocker(s)"
        lines.append(f"{name}{tasks}{review}")
        for s in feat.get("steps") or []:
            mark = "OK    " if s.get("ok") else "FAILED"
            cost = ""
            if s.get("cost"):
                unit = s.get("cost_unit") or "usd"
                cost = f"  ${float(s['cost']):.2f}" if unit == "usd" else f"  {float(s['cost']):.1f} {unit}"
            lines.append(f"  {mark} {s.get('kind') or s.get('step'):<10} {_dur(s.get('duration_s'))}{cost}")
        for sw in feat.get("switched") or []:
            lines.append(f"  fallback {sw}")
        if feat.get("stopped"):
            lines.append(f"  stopped at {feat['stopped']}: {feat.get('reason')}")
        elif feat.get("reason"):
            lines.append(f"  {feat['reason']}")
    if out.get("spent"):
        cap = f" of {out['budget']:g}" if out.get("budget") else ""
        lines.append(f"spent {out['spent']}{cap}")
    if out.get("run_id"):
        lines.append(f"run {out['run_id']}")
    return "\n".join(lines)


def flow_exit_code(out: dict) -> int:
    """0 clean · 1 blockers left after the rounds · 2 a step failed · 3 quota/auth/budget."""
    if out.get("ok"):
        return EXIT_OK
    feats = list((out.get("features") or {}).values())
    if any(f.get("error_kind") in ("quota", "auth") or f.get("stopped") == "budget" for f in feats):
        return EXIT_ENV
    if feats and all(f.get("stopped") in (None, "revise") for f in feats) and out.get("blockers"):
        return EXIT_BLOCKERS
    return EXIT_ERROR


def _flow_options(args) -> dict:
    return {"implement": getattr(args, "implement", False),
            "analyze": not getattr(args, "no_analyze", False),
            "drift": not getattr(args, "no_drift", False),
            "max_rounds": args.max_rounds, "majors": args.majors}


def _refuse_unconfirmed_implement(run: Run) -> int | None:
    args = run.args
    if getattr(args, "implement", False) and not getattr(args, "yes", False) and not args.dry_run:
        msg = ("--implement writes code: re-run with --yes to confirm, or --dry-run to see "
               "the first step")
        run.emit({"error": msg}, f"refusing: {msg}")
        return EXIT_ENV
    return None


def cmd_go(run: Run) -> int:
    from . import flow
    args = run.args
    refused = _refuse_unconfirmed_implement(run)
    if refused is not None:
        return refused
    out = flow.do_go(run.cfg, args.description, stack=args.stack, split=args.split,
                     max_features=args.max_features, budget=args.budget,
                     fallback=getattr(args, "fallback", None), harness=run.harness(),
                     stream=run.stream, dry_run=args.dry_run, init=not args.no_init,
                     **_flow_options(args))
    run.emit(out, flow_summary(out))
    return flow_exit_code(out)


def _current_feature_or_error(run: Run) -> Path | None:
    args, cfg = run.args, run.cfg
    feat = S.find_feature(cfg, args.feature) if args.feature else S.current_feature(cfg)
    if feat is None:
        print("no feature yet: run `speckfish go \"<idea>\"` or `speckfish specify` first",
              file=sys.stderr)
    return feat


def cmd_next(run: Run) -> int:
    from . import flow
    args, cfg = run.args, run.cfg
    refused = _refuse_unconfirmed_implement(run)
    if refused is not None:
        return refused
    feat = _current_feature_or_error(run)
    if feat is None:
        return EXIT_ERROR
    opts = _flow_options(args)
    kind, why = flow.next_step(cfg, feat, implement=opts["implement"], analyze=opts["analyze"],
                               drift=opts["drift"])
    if args.explain or kind is None:
        info = {"feature": feat.name, "next": kind, "why": why,
                "command": flow.command_for(kind, feat.name) if kind else None}
        run.emit(info, f"{feat.name}: {why}" + (f"\n  -> {info['command']}" if kind else ""))
        return EXIT_OK
    rid = S.ensure_run(cfg, None)
    result = flow.advance(cfg, feat, rid, run.harness(), stack=args.stack,
                          fallback=getattr(args, "fallback", None), stream=run.stream,
                          dry_run=args.dry_run, only_one=True, **opts)
    st = flow.feature_state(cfg, feat)
    result["state"] = {k: st[k] for k in ("spec", "plan", "tasks", "tasks_done", "tasks_total",
                                          "blockers", "review_current")}
    out = {"run_id": rid, "ok": not result["stopped"], "blockers": st["blockers"] or 0,
           "features": {feat.name: result}}
    run.emit(out, flow_summary(out))
    return flow_exit_code(out)


def cmd_revise(run: Run) -> int:
    from . import flow
    args, cfg = run.args, run.cfg
    feat = _current_feature_or_error(run)
    if feat is None:
        return EXIT_ERROR
    rid = S.ensure_run(cfg, None)
    result = flow.do_fix_loop(cfg, feat, rid, run.harness(), max_rounds=args.max_rounds,
                              majors=args.majors, fallback=getattr(args, "fallback", None),
                              stream=run.stream, dry_run=args.dry_run)
    st = flow.feature_state(cfg, feat)
    result["state"] = {k: st[k] for k in ("spec", "plan", "tasks", "tasks_done", "tasks_total",
                                          "blockers", "review_current")}
    out = {"run_id": rid, "ok": bool(result.get("clean")), "blockers": st["blockers"] or 0,
           "features": {feat.name: result}}
    run.emit(out, flow_summary(out))
    return flow_exit_code(out)


def status_summary(rows: list[dict], spent: float, unit: str | None) -> str:
    if not rows:
        return "(no features: run `speckfish go \"<idea>\"`)"
    lines = []
    for r in rows:
        arts = "".join("+" if r[k] else "-" for k in ("spec", "plan", "tasks"))
        tasks = f"{r['tasks_done']}/{r['tasks_total']}" if r["tasks_total"] else "-"
        if r["blockers"] is not None:
            review = f"{r['blockers']} blocker(s)"
        else:
            review = "stale" if r["review"] else "never"
        lines.append(f"{r['name']:<34} [{arts}]  tasks {tasks:<7} review: {review}")
        nxt = r["next"]
        lines.append(f"  next: {nxt['why']}" + (f"  ->  {nxt['command']}" if nxt["command"] else ""))
    if spent:
        lines.append("")
        lines.append(f"spent so far: {spent} {unit or ''}".rstrip())
    return "\n".join(lines)


def cmd_status(run: Run) -> int:
    from . import flow
    rows = flow.status_rows(run.cfg)
    spent, unit = flow.total_cost(run.cfg)
    run.emit({"features": rows, "spent": spent, "unit": unit}, status_summary(rows, spent, unit))
    return EXIT_OK


# one handler per subcommand: adding a command is adding a row here and a parser above
HANDLERS = {
    "go": cmd_go, "next": cmd_next, "revise": cmd_revise, "status": cmd_status,
    "editors": cmd_editors, "init": cmd_init, "doctor": cmd_doctor, "specify": cmd_specify,
    "plan": cmd_plan, "review": cmd_review, "drift": cmd_drift, "pipeline": cmd_pipeline,
    "features": cmd_features, "config": cmd_config, "runs": cmd_runs, "report": cmd_report,
    "show": cmd_show, "brainstorm": cmd_explore, "draft": cmd_explore,
    **{name: cmd_skill for name in SKILL_SPECS},
}


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(normalize_argv(raw))
    cfg = Config.load(Path(args.root))
    if args.harness:
        cfg.harness = args.harness
    if args.model:
        cfg.model = args.model
    from . import progress as _progress
    _progress.ENABLED = not (getattr(args, "no_progress", False) or args.quiet or args.json)
    handler = HANDLERS.get(args.cmd)
    if handler is None:
        return EXIT_ERROR
    try:
        return handler(Run(cfg, args))
    except SystemExit as e:
        msg = str(e) or "environment error"
        emit({"error": msg}, args.json, f"ERROR: {msg}")
        return EXIT_ENV


if __name__ == "__main__":
    sys.exit(main())
