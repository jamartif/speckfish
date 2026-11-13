"""Entry point: `speckfish <command>`."""
from __future__ import annotations

import argparse
import json
import sys
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


def summarize(res: dict) -> str:
    lines = [f"run: {res.get('run_id', '-')}  step: {res.get('step')}  ok: {res.get('ok')}"
             f"  exit: {res.get('exit_code')}  {res.get('duration_s')}s"
             + (f"  cost: {res.get('cost')} {res.get('cost_unit')}" if res.get("cost") else "")
             + (f"  model: {res.get('model')}" if res.get("model") else "")]
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
        return EXIT_ERROR
    # exit 1 only with blockers: "does not meet the standard" is read from the JSON
    if res.get("step") in ("review", "drift") and res.get("blockers"):
        return EXIT_BLOCKERS
    return EXIT_OK


GLOBAL_FLAGS_VALUE = {"--root", "--harness", "--model"}
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
    common.add_argument("--model", default=None,
                        help="model passed to the agent CLI (default: the CLI's own choice)")
    common.add_argument("--json", action="store_true", help="JSON output on stdout")
    common.add_argument("--stream", action="store_true", default=True,
                        help="forward agent output to stderr (default)")
    common.add_argument("--quiet", action="store_true", help="do not forward agent output")
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
    sp.add_argument("--interactive", action="store_true",
                    help="run the agent in this terminal so it can ask you questions")

    sp = sub.add_parser("drift", help="drift between designs and code")
    sp.add_argument("target", nargs="?", default=None, help="specs directory (default: latest feature)")
    sp.add_argument("--run-id", default=None)

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
        if spec.get("dangerous"):
            sp.add_argument("--yes", action="store_true",
                            help="required: this step writes source code")

    sp = sub.add_parser("config", help="show or change the project defaults")
    sp.add_argument("set", nargs="?", default=None,
                    help="KEY=VALUE to store (harness, model, profile, egm_root, specify)")

    sp = sub.add_parser("runs", help="list runs")
    sp.add_argument("--clean", action="store_true", help="delete old runs, keeping --keep newest")
    sp.add_argument("--keep", type=int, default=10, help="how many runs to keep (default: 10)")
    sp = sub.add_parser("show", help="details of a run")
    sp.add_argument("run_id", nargs="?", default=None)
    return p


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(normalize_argv(raw))
    cfg = Config.load(Path(args.root))
    if args.harness:
        cfg.harness = args.harness
    if args.model:
        cfg.model = args.model
    as_json = args.json
    stream = not args.quiet
    try:
        if args.cmd == "editors":
            from .editors import generate
            targets = [t.strip() for t in args.targets.split(",") if t.strip()]
            gen = generate(cfg, targets, args.command, args.codex_home,
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
            emit(gen, as_json, "\n".join(human))
            return EXIT_OK

        if args.cmd == "init":
            out = S.do_init(cfg, args.integration, skip_egm=args.no_egm,
                            editors=getattr(args, "editors", True),
                            editor_prefix=getattr(args, "prefix", "spfish"))
            ok_int = ", ".join(out.get("installed") or []) or "none"
            failed = [a["integration"] for a in out["actions"]
                      if a.get("integration") and not a.get("specify_init")]
            human = [f"integrations installed: {ok_int}"]
            if failed:
                human.append(f"failed: {', '.join(failed)}")
            human.append(f"egm: {out.get('egm_root')}")
            emit(out, as_json, "\n".join(human))
            return EXIT_OK if all(a.get("specify_init", True) for a in out["actions"]) else EXIT_ERROR
        if args.cmd == "doctor":
            info = S.do_doctor(cfg)
            emit(info, as_json, doctor_summary(info))
            ok = (info.get("harness") or {}).get("installed") and \
                 (info.get("specify") or {}).get("ok") and (info.get("egm") or {}).get("present")
            return EXIT_OK if ok else EXIT_ENV
        # resolved lazily: `config`, `runs`, `show` and `version` must work with no agent installed
        _harness = {}

        def need_harness():
            if "h" not in _harness:
                _harness["h"] = get_harness(cfg, args.harness)
            return _harness["h"]

        if args.cmd == "specify":
            res = S.do_specify(cfg, args.description, args.run_id, need_harness(), stream, args.dry_run)
        elif args.cmd == "plan":
            res = S.do_plan(cfg, args.stack, args.run_id, need_harness(), stream, args.dry_run)
        elif args.cmd == "review":
            res = S.do_review(cfg, args.target, args.run_id, need_harness(), args.profile,
                              args.stage, stream, args.dry_run, args.target_file,
                              interactive=getattr(args, "interactive", False))
        elif args.cmd == "drift":
            res = S.do_drift(cfg, args.target, args.run_id, need_harness(), stream, args.dry_run)
        elif args.cmd in SKILL_SPECS:
            spec = SKILL_SPECS[args.cmd]
            if spec.get("dangerous") and not getattr(args, "yes", False) and not args.dry_run:
                msg = (f"`{args.cmd}` is destructive: {spec['writes']}. "
                       f"Re-run with --yes to confirm, or --dry-run to see the command first.")
                emit({"error": msg, "writes": spec["writes"]}, as_json, f"refusing: {msg}")
                return EXIT_ENV
            res = S.do_skill(cfg, args.cmd, args.args, args.run_id, need_harness(), stream, args.dry_run)
        elif args.cmd == "pipeline":
            out = S.do_pipeline(cfg, args.description, args.stack, args.target,
                                args.gate, args.with_drift, args.with_tasks, args.with_analyze,
                                stream, args.dry_run)
            emit(out, as_json, json.dumps(out, indent=2, ensure_ascii=False))
            if not out.get("ok"):
                return EXIT_ERROR
            if any((s.get("blockers") or []) for s in out["steps"].values()):
                return EXIT_BLOCKERS
            return EXIT_OK
        elif args.cmd == "config":
            from dataclasses import asdict
            if args.set:
                if "=" not in args.set:
                    emit({"error": "expected KEY=VALUE"}, as_json, "expected KEY=VALUE")
                    return EXIT_ENV
                key, value = args.set.split("=", 1)
                key = key.strip()
                allowed = {"harness", "model", "profile", "egm_root", "specify"}
                if key not in allowed:
                    msg = f"unknown key '{key}' (allowed: {', '.join(sorted(allowed))})"
                    emit({"error": msg}, as_json, f"ERROR: {msg}")
                    return EXIT_ENV
                setattr(cfg, key, value.strip())
                path = cfg.save()
                emit({"saved": norm(path), key: value.strip()}, as_json,
                     f"saved {key}={value.strip()} → {norm(path)}")
                return EXIT_OK
            data = asdict(cfg)
            data.pop("root", None)
            human = [f"{k}: {v}" for k, v in data.items()]
            emit(data, as_json, "\n".join(human))
            return EXIT_OK
        elif args.cmd == "runs":
            base = cfg.runs_dir
            runs = sorted([p.name for p in base.iterdir() if p.is_dir()], reverse=True) if base.exists() else []
            if args.clean:
                out = S.clean_runs(cfg, args.keep)
                emit(out, as_json, f"removed {len(out['removed'])} run(s); kept "
                                   f"{len(out['kept'])}")
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
            emit({"runs": rows}, as_json, "\n".join(human) or "(no runs)")
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
            data = S.run_summary(cfg, rid)
            human = [f"run {rid}: {len(data.get('steps') or {})} step(s)"]
            for step, info in (data.get("steps") or {}).items():
                cost = f"{info.get('cost')} {info.get('cost_unit')}" if info.get("cost") else "-"
                human.append(f"  {step:<12} {info.get('status'):<7} {info.get('duration_s')}s  "
                             f"cost={cost}  model={info.get('model') or '-'}  "
                             f"blockers={info.get('blockers')}")
            if data.get("cost"):
                human.append(f"total: {data['cost']} {data.get('cost_unit')} in "
                             f"{data.get('duration_s')}s")
            emit(data, as_json, "\n".join(human))
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
