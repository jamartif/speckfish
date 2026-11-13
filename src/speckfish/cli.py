"""Entry point: `speckfish <command>`."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import steps as S
from .config import Config, norm
from .harnesses import get_harness

EXIT_OK, EXIT_BLOCKERS, EXIT_ERROR, EXIT_ENV = 0, 1, 2, 3


def emit(obj, as_json: bool, human: str | None = None) -> None:
    if as_json:
        print(json.dumps(obj, indent=2, ensure_ascii=False))
    else:
        print(human if human is not None else json.dumps(obj, indent=2, ensure_ascii=False))


def summarize(res: dict) -> str:
    lines = [f"run: {res.get('run_id', '-')}  step: {res.get('step')}  ok: {res.get('ok')}"
             f"  exit: {res.get('exit_code')}  {res.get('duration_s')}s"
             + (f"  cost: {res.get('cost')} {res.get('cost_unit')}" if res.get("cost") else "")]
    if res.get("meets_standard") is not None:
        lines.append(f"meets standard: {res['meets_standard']}  "
                     f"blockers: {len(res.get('blockers') or [])}  majors: {res.get('majors')}  "
                     f"nits: {res.get('nits')}")
    for b in (res.get("blockers") or [])[:8]:
        lines.append(f"  🔴 {b}")
    if res.get("reports"):
        lines.append("reports:")
        lines += [f"  - {r}" for r in res["reports"]]
    if res.get("error"):
        lines.append(f"ERROR: {res['error']}")
    if res.get("artifact_dir"):
        lines.append(f"artifacts: {res['artifact_dir']}")
    if res.get("log"):
        lines.append(f"log: {res['log']}")
    return "\n".join(lines)


def exit_for(res: dict) -> int:
    if not res.get("ok"):
        return EXIT_ERROR
    # exit 1 only with blockers: "does not meet the standard" is read from the JSON
    if res.get("step") in ("review", "drift") and res.get("blockers"):
        return EXIT_BLOCKERS
    return EXIT_OK


GLOBAL_FLAGS_VALUE = {"--root", "--harness"}
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
    common.add_argument("--root", default=".", help="project root (default: cwd)")
    common.add_argument("--harness", default=None, help="claude | copilot | codex (default: auto)")
    common.add_argument("--json", action="store_true", help="JSON output on stdout")
    common.add_argument("--stream", action="store_true", default=True,
                        help="forward agent output to stderr (default)")
    common.add_argument("--quiet", action="store_true", help="do not forward agent output")
    common.add_argument("--dry-run", action="store_true", help="print the command without running it")
    p = argparse.ArgumentParser(prog="speckfish", parents=[common],
                                description="Headless orchestrator for Spec Kit + EGM over agent CLIs.")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("init", help="prepare the project: git + specify init + EGM")
    sp.add_argument("--integration", default=None, help="claude | copilot | codex (default: auto)")
    sp.add_argument("--no-egm", action="store_true", help="do not clone/install EGM")

    sub.add_parser("doctor", help="check environment, harness, specify and EGM")

    sp = sub.add_parser("specify", help="create/update spec.md")
    sp.add_argument("description", help="feature description")
    sp.add_argument("--run-id", default=None)

    sp = sub.add_parser("plan", help="generate plan.md and design artifacts")
    sp.add_argument("stack", nargs="?", default="", help="stack and constraints")
    sp.add_argument("--run-id", default=None)

    sp = sub.add_parser("review", help="Goldfish review of a document")
    sp.add_argument("target", nargs="?", default=None, help="document (default: plan.md of the latest feature)")
    sp.add_argument("--profile", default=None, help="speckit | none (default: speckit)")
    sp.add_argument("--stage", default="uninterrupted", help="uninterrupted | feature_spec | implementation | 5..9")
    sp.add_argument("--target-file", default="plan.md", help="default document")
    sp.add_argument("--run-id", default=None)

    sp = sub.add_parser("drift", help="drift between designs and code")
    sp.add_argument("target", nargs="?", default=None, help="specs directory (default: latest feature)")
    sp.add_argument("--run-id", default=None)

    sp = sub.add_parser("pipeline", help="specify → plan → review (→ drift)")
    sp.add_argument("description", help="feature description")
    sp.add_argument("--stack", default="")
    sp.add_argument("--target", default=None)
    sp.add_argument("--gate", action="store_true", help="stop when a step fails or there are blockers")
    sp.add_argument("--with-drift", action="store_true")

    sub.add_parser("runs", help="list runs")
    sp = sub.add_parser("show", help="details of a run")
    sp.add_argument("run_id", nargs="?", default=None)
    return p


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(normalize_argv(raw))
    cfg = Config.load(Path(args.root))
    if args.harness:
        cfg.harness = args.harness
    as_json = args.json
    stream = not args.quiet
    try:
        if args.cmd == "init":
            out = S.do_init(cfg, args.integration, skip_egm=args.no_egm)
            emit(out, as_json, f"init ok: harness={out.get('harness')} egm={out.get('egm_root')}")
            return EXIT_OK if all(a.get("specify_init", True) for a in out["actions"]) else EXIT_ERROR
        if args.cmd == "doctor":
            info = S.do_doctor(cfg)
            human = [f"root: {info['root']}",
                     f"harness: {json.dumps(info.get('harness'), ensure_ascii=False)}",
                     f"specify: {json.dumps(info.get('specify'), ensure_ascii=False)}",
                     f"egm: {json.dumps(info.get('egm'), ensure_ascii=False)}",
                     f"project: {json.dumps(info.get('project'), ensure_ascii=False)}"]
            emit(info, as_json, "\n".join(human))
            ok = (info.get("harness") or {}).get("installed") and \
                 (info.get("specify") or {}).get("ok") and (info.get("egm") or {}).get("present")
            return EXIT_OK if ok else EXIT_ENV
        h = get_harness(cfg, args.harness)
        if args.cmd == "specify":
            res = S.do_specify(cfg, args.description, args.run_id, h, stream, args.dry_run)
        elif args.cmd == "plan":
            res = S.do_plan(cfg, args.stack, args.run_id, h, stream, args.dry_run)
        elif args.cmd == "review":
            res = S.do_review(cfg, args.target, args.run_id, h, args.profile,
                              args.stage, stream, args.dry_run, args.target_file)
        elif args.cmd == "drift":
            res = S.do_drift(cfg, args.target, args.run_id, h, stream, args.dry_run)
        elif args.cmd == "pipeline":
            out = S.do_pipeline(cfg, args.description, args.stack, args.target,
                                args.gate, args.with_drift, stream, args.dry_run)
            emit(out, as_json, json.dumps(out, indent=2, ensure_ascii=False))
            if not out.get("ok"):
                return EXIT_ERROR
            if any((s.get("blockers") or []) for s in out["steps"].values()):
                return EXIT_BLOCKERS
            return EXIT_OK
        elif args.cmd == "runs":
            base = cfg.runs_dir
            runs = sorted([p.name for p in base.iterdir() if p.is_dir()], reverse=True) if base.exists() else []
            emit({"runs": runs}, as_json, "\n".join(runs) or "(no runs)")
            return EXIT_OK
        elif args.cmd == "show":
            base = cfg.runs_dir
            rid = args.run_id
            if not rid:
                cands = sorted([p for p in base.iterdir() if p.is_dir()]) if base.exists() else []
                rid = cands[-1].name if cands else None
            if not rid or not (base / rid).exists():
                emit({"error": "run not found"}, as_json, "run not found")
                return EXIT_ERROR
            state = base / rid / "state.json"
            data = json.loads(state.read_text(encoding="utf-8")) if state.exists() else {}
            emit(data, as_json, json.dumps(data, indent=2, ensure_ascii=False))
            return EXIT_OK
        else:
            return EXIT_ERROR
        emit(res.to_dict(), as_json, summarize(res.to_dict()))
        return exit_for(res.to_dict())
    except SystemExit as e:
        msg = str(e) or "environment error"
        emit({"error": msg}, as_json, f"ERROR: {msg}")
        return EXIT_ENV


if __name__ == "__main__":
    sys.exit(main())
