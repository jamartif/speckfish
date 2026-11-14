"""Pipeline steps: init, specify, plan, review, drift, doctor."""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from . import prompts
from .config import Config, find_git, norm, run_cmd, which
from .harnesses import Harness, get_harness
from .runner import StepResult, new_run_id, run_dir_for, run_interactive, run_step

# ---------------- infrastructura ----------------

SPECIFY_VIA_UV = ["uv", "tool", "run", "--from", "specify-cli", "specify"]

# Spec Kit prints an ASCII-art banner; these characters never appear in real output we want.
BANNER_CHARS = set("█▓▒░▄▀■□▪▫╔╗╚╝║═╠╣╦╩╬╭╮╯╰─│┌┐└┘├┤┬┴┼")


def banner_free_tail(text: str, keep: int = 2) -> list[str]:
    """Drop the block-character banner from a CLI output, keeping the last useful lines."""
    lines = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if sum(ch in BANNER_CHARS for ch in line) > 3:
            continue
        lines.append(line)
    return lines[-keep:]


def parse_specify_version(text: str) -> str:
    """Pull a readable version out of `specify version` output."""
    blob = text or ""
    m = re.search(r"CLI\s*Version\s*([\w.\-]+)", blob, re.I)
    version = m.group(1) if m else ""
    if not version:
        m = re.search(r"specify-cli\s+v?([\w.\-]+)", blob, re.I)
        version = m.group(1) if m else ""
    if not version:
        m = re.search(r"\bv?(\d+\.\d+\.\d+)\b", blob)
        version = m.group(1) if m else ""
    py = re.search(r"Python\s*([\w.\-]+)", blob, re.I)
    parts = [f"specify-cli {version}" if version else "specify-cli (unknown version)"]
    if py:
        parts.append(f"Python {py.group(1)}")
    return " · ".join(parts)


def specify_argv(cfg: Config) -> list[str]:
    """Return the `specify` invocation that works on this machine."""
    if cfg.specify == "uv":
        return list(SPECIFY_VIA_UV)
    if cfg.specify == "binary":
        return ["specify"]
    exe = which("specify")
    if exe:
        code, out, err = run_cmd([exe, "version"], timeout=120)
        if code == 0 and ("Specify" in out or "Version" in out):
            return [exe]
    if which("uv"):
        return list(SPECIFY_VIA_UV)
    raise SystemExit("cannot find `specify` or `uv` to run Spec Kit")


def run_specify(cfg: Config, args: list[str], cwd: Path, timeout: int = 600) -> tuple[int, str, str]:
    base = specify_argv(cfg)
    return run_cmd(base + args, cwd=cwd, timeout=timeout)


def latest_feature(root: Path) -> Path | None:
    specs = root / "specs"
    if not specs.is_dir():
        return None
    cands = [p for p in specs.iterdir() if p.is_dir() and (p / "spec.md").exists()]
    return max(cands, key=lambda p: p.stat().st_mtime) if cands else None


def ensure_run(cfg: Config, run_id: str | None) -> str:
    rid = run_id or new_run_id()
    run_dir_for(cfg, rid)
    return rid


# ---------------- init / doctor ----------------

SUPPORTED_INTEGRATIONS = ["claude", "copilot", "codex"]


def resolve_integrations(spec: str | None) -> list[str]:
    """Turn the --integration value into the list of Spec Kit integrations to install.

    "all" installs every supported integration; "auto" (default) picks every agent CLI
    that is actually installed, falling back to the detected harness.
    Comma-separated values are honoured: "claude,copilot".
    """
    raw = (spec or "auto").strip().lower()
    if raw in ("all", "*"):
        return list(SUPPORTED_INTEGRATIONS)
    if raw != "auto":
        items = [x.strip() for x in raw.split(",") if x.strip()]
        unknown = [x for x in items if x not in SUPPORTED_INTEGRATIONS]
        if unknown:
            raise SystemExit(f"unknown integration(s): {', '.join(unknown)} "
                             f"(supported: {', '.join(SUPPORTED_INTEGRATIONS)} or 'all')")
        return items
    installed = [name for name in SUPPORTED_INTEGRATIONS if which(name)]
    return installed or ["claude"]


def harness_for_integration(cfg: Config, integration: str) -> Harness:
    try:
        return get_harness(cfg, integration)
    except SystemExit:
        # the adapter exists even when its CLI is not installed: used only for its metadata
        from .harnesses import REGISTRY
        return REGISTRY[integration](cfg)


def do_init(cfg: Config, integration: str | None, force_git: bool = True,
            skip_egm: bool = False, editors: bool = True,
            editor_prefix: str = "spfish") -> dict:
    root = cfg.root
    out: dict = {"root": norm(root), "actions": []}
    if not (root / ".git").exists() and force_git:
        code, o, e = run_cmd([find_git(), "init"], cwd=root, timeout=120)
        out["actions"].append({"git_init": code == 0, "detail": (o or e)[-300:]})
    integrations = resolve_integrations(integration)
    for name in integrations:
        h = harness_for_integration(cfg, name)
        sk_integration, extra = h.spec_kit_integration()
        args = ["init", "--here", "--force", "--non-interactive",
                "--integration", sk_integration] + extra
        if not which(name):
            # Spec Kit aborts when the agent CLI is missing; the skills are still useful
            args.append("--ignore-agent-tools")
        code, o, e = run_specify(cfg, args, root, timeout=300)
        out["actions"].append({"integration": name, "specify_init": code == 0,
                               "argv": specify_argv(cfg) + args,
                               "detail": banner_free_tail(o or e, keep=2)})
    if not skip_egm and not cfg.egm_path():
        out["actions"].append(install_egm(cfg))
    egm = cfg.egm_path()
    out["egm_root"] = norm(egm) if egm else None
    out["integrations"] = integrations
    out["installed"] = [a["integration"] for a in out["actions"]
                        if a.get("specify_init") and a.get("integration")]
    out["harness"] = integrations[0] if integrations else None
    if editors:
        from .editors import generate
        out["editors"] = generate(cfg, prefix=editor_prefix)
    out["config"] = norm(cfg.save())
    out["note"] = ("Spec Kit keeps only the last integration in .specify/integration.json; "
                   "the skills of the others stay on disk. Re-run `speckfish init` to repair.")
    (cfg.state_dir / "init.json").write_text(json.dumps(out, indent=2, ensure_ascii=False),
                                             encoding="utf-8")
    return out


def install_egm(cfg: Config, repo: str = "google/elephant-goldfish-model") -> dict:
    """Clone EGM into vendor/egm (leaves the user global config untouched)."""
    dest = cfg.root / "vendor" / "egm"
    if (dest / "plugin.json").exists():
        return {"egm": "already_present", "path": norm(dest)}
    dest.parent.mkdir(parents=True, exist_ok=True)
    code, o, e = run_cmd([find_git(), "clone", "--depth", "1",
                          f"https://github.com/{repo}", str(dest)], timeout=300)
    ok = code == 0 and (dest / "plugin.json").exists()
    return {"egm": "cloned" if ok else "failed", "path": norm(dest),
            "detail": (e or o)[-400:]}


def do_doctor(cfg: Config) -> dict:
    info: dict = {"root": norm(cfg.root), "tools": {}, "harness": None,
                  "egm": None, "specify": None, "project": {}}
    for tool in ("git", "uv", "node"):
        info["tools"][tool] = {"installed": bool(which(tool))}
    # harness
    try:
        h = get_harness(cfg)
        info["harness"] = h.doctor()
    except SystemExit as e:
        info["harness"] = {"error": str(e)}
    # specify
    try:
        argv = specify_argv(cfg)
    except SystemExit as e:
        info["specify"] = {"error": str(e)}
        argv = None
    if argv:
        code, o, e = run_cmd(argv + ["version"], timeout=180)
        out = o or e
        info["specify"] = {"argv": argv, "ok": code == 0,
                           "version": parse_specify_version(out),
                           "detail": banner_free_tail(out)}
    # egm
    egm = cfg.egm_path()
    info["egm"] = {"path": norm(egm) if egm else None, "present": bool(egm)}
    if egm:
        info["egm"]["version"] = json.loads(
            (egm / "plugin.json").read_text(encoding="utf-8")).get("version")
        info["egm"]["skills"] = sorted(p.name for p in (egm / "skills").iterdir() if p.is_dir())
        if info["harness"] and info["harness"].get("binary") == which("copilot"):
            code, o, e = run_cmd([which("copilot"), "--plugin-dir", norm(egm),
                                  "plugin", "list"], timeout=120,
                                 env={"COPILOT_OFFLINE": "true"})
            info["egm"]["copilot_registration"] = (o or e).strip()[-400:]
    # proyecto
    info["project"] = {
        "has_git": (cfg.root / ".git").exists(),
        "has_specify": (cfg.root / ".specify").exists(),
        "has_claude_skills": (cfg.root / ".claude" / "skills").is_dir(),
        "feature": norm(latest_feature(cfg.root)) if latest_feature(cfg.root) else None,
    }
    return info



# ---------------- feature selection ----------------

def list_features(cfg: Config) -> list[dict]:
    """Every feature directory under specs/, with the artifacts it already has."""
    specs_dir = cfg.root / "specs"
    active = active_feature(cfg)
    rows = []
    if not specs_dir.is_dir():
        return rows
    for d in sorted(p for p in specs_dir.iterdir() if p.is_dir()):
        rows.append({
            "name": d.name,
            "path": norm(d),
            "spec": (d / "spec.md").exists(),
            "plan": (d / "plan.md").exists(),
            "tasks": (d / "tasks.md").exists(),
            "active": bool(active and active.name == d.name),
        })
    return rows


def active_feature(cfg: Config) -> Path | None:
    """The feature Spec Kit considers current (`.specify/feature.json`)."""
    f = cfg.root / ".specify" / "feature.json"
    if not f.exists():
        return None
    try:
        rel = json.loads(f.read_text(encoding="utf-8")).get("feature_directory")
    except Exception:
        return None
    if not rel:
        return None
    p = Path(rel)
    if not p.is_absolute():
        p = cfg.root / p
    return p if p.is_dir() else None


def set_active_feature(cfg: Config, feature: Path) -> Path:
    """Point Spec Kit at this feature (writes `.specify/feature.json`)."""
    feature = feature.resolve()
    fdir = cfg.root / ".specify"
    fdir.mkdir(parents=True, exist_ok=True)
    try:
        rel = feature.relative_to(cfg.root.resolve()).as_posix()
    except ValueError:
        rel = str(feature)
    (fdir / "feature.json").write_text(json.dumps({"feature_directory": rel}, indent=2),
                                      encoding="utf-8")
    return feature


def find_feature(cfg: Config, wanted: str) -> Path:
    """Resolve `--feature` by path, directory name, or number (002 / 002-open-remember-repos)."""
    raw = (wanted or "").strip().strip('"').strip("'")
    if not raw:
        raise SystemExit("empty --feature value")
    for cand in (Path(raw), cfg.root / raw):
        if cand.is_dir():
            return cand.resolve()
    specs_dir = cfg.root / "specs"
    if not specs_dir.is_dir():
        raise SystemExit("no specs/ directory here: run `speckfish specify` first")
    names = sorted(p.name for p in specs_dir.iterdir() if p.is_dir())
    for name in names:
        if name == raw or name.startswith(raw) or name.split("-")[0] == raw.zfill(3):
            return (specs_dir / name).resolve()
    raise SystemExit(f"feature not found: {raw}. Available: {', '.join(names) or 'none'}")


# ---------------- agent steps ----------------

def with_language(prompt: str, cfg: Config) -> str:
    """Append the output-language instruction once, so every artifact uses the same language."""
    line = prompts.language_line(getattr(cfg, "language", "") or "")
    if line and line not in prompt:
        return prompt + "\n\n" + line
    return prompt


BATCHABLE = ("plan", "tasks", "analyze")


def features_with_spec(cfg: Config) -> list[Path]:
    """Every feature that already has a spec.md, in name order (the order the user created them)."""
    specs_dir = cfg.root / "specs"
    if not specs_dir.is_dir():
        return []
    return [d for d in sorted(specs_dir.iterdir()) if d.is_dir() and (d / "spec.md").exists()]


def do_feature_batch(cfg: Config, action: str, features: list[Path], harness: Harness | None = None,
                     run_id: str | None = None, stack: str = "", args: str = "",
                     stream: bool = True, dry_run: bool = False) -> dict:
    """Run one step over every feature, in order, stopping at the first failure.

    Spec Kit itself works on a single active feature, so N features mean N agent runs; this walks
    them without hiding each result.
    """
    h = harness or get_harness(cfg)
    rid = ensure_run(cfg, run_id)
    out: dict = {"run_id": rid, "action": action, "harness": h.name, "steps": {},
                 "order": [f.name for f in features], "stopped_at": None, "total_cost": 0.0}
    for f in features:
        set_active_feature(cfg, f)
        if action == "plan":
            res = do_plan(cfg, stack, rid, h, stream, dry_run, feature=f.name)
        else:
            res = do_skill(cfg, action, args, rid, h, stream, dry_run, feature=f.name)
        out["steps"][f.name] = res.to_dict()
        if res.cost and res.cost_unit == "usd":
            out["total_cost"] += float(res.cost)
        if not res.ok:
            out["stopped_at"] = f.name
            break
    out["done"] = out["stopped_at"] is None
    return out


def do_specify(cfg: Config, idea: str, run_id: str | None = None,
               harness: Harness | None = None, stream: bool = True,
               dry_run: bool = False, review: bool = False) -> StepResult:
    h = harness or get_harness(cfg)
    rid = ensure_run(cfg, run_id)
    if h.name == "claude":
        prompt = with_language(prompts.specify_prompt(idea, cfg.root), cfg)
    else:
        prompt = (f"/speckit-specify {idea}\n\n{prompts.NO_QUESTIONS}"
                  if h.name == "claude" else
                  f"{idea}\n\n{prompts.NO_QUESTIONS}\n\nRun the Spec Kit specification flow "
                  f"(speckit-specify skill) as defined in this project.")
    res = run_step(cfg, h, "specify", prompt, cfg.root, rid, cfg.egm_path(),
                   schema=prompts.SCHEMAS["specify"], stream=stream, dry_run=dry_run)
    if review and not dry_run and res.ok:
        spec = spec_path_from(res) or (latest_feature(cfg.root) or Path()) / "spec.md"
        if Path(spec).exists():
            do_review(cfg, str(spec), rid, h, stream=stream, default_file="spec.md")
    return res


def spec_path_from(res: StepResult) -> str | None:
    """The spec.md the agent says it wrote (from the structured result), if any."""
    data = res.structured or {}
    for key in ("spec_path", "feature_dir"):
        value = data.get(key)
        if not value:
            continue
        p = Path(value)
        if p.suffix == ".md":
            return str(p) if p.exists() else None
        candidate = p / "spec.md"
        if candidate.exists():
            return str(candidate)
    return None


def do_plan(cfg: Config, stack: str, run_id: str | None = None,
            harness: Harness | None = None, stream: bool = True,
            dry_run: bool = False, feature: str | None = None) -> StepResult:
    h = harness or get_harness(cfg)
    rid = ensure_run(cfg, run_id)
    fdir = set_active_feature(cfg, find_feature(cfg, feature)) if feature else active_feature(cfg)
    prompt = (with_language(prompts.plan_prompt(stack, cfg.root, fdir), cfg) if h.name == "claude"
              else f"Stack and constraints: {stack}\n\n{prompts.NO_QUESTIONS}\n\n"
                   f"Work on the feature in {norm(fdir) if fdir else 'the current feature'}."
                   f"\n\nRun the Spec Kit planning flow (speckit-plan skill).")
    return run_step(cfg, h, "plan", prompt, cfg.root, rid, cfg.egm_path(),
                    schema=prompts.SCHEMAS["plan"], stream=stream, dry_run=dry_run)


def resolve_target(cfg: Config, target: str | None, default_file: str = "plan.md") -> Path:
    if target:
        p = Path(target)
        if not p.is_absolute():
            p = cfg.root / p
        if not p.exists():
            raise SystemExit(f"target document does not exist: {p}")
        return p
    feat = latest_feature(cfg.root)
    if not feat:
        raise SystemExit("no specs in this project; run `speckfish specify` first")
    p = feat / default_file
    if not p.exists():
        alt = feat / "spec.md"
        if alt.exists():
            return alt
        raise SystemExit(f"cannot find {default_file} or spec.md in {feat}")
    return p


def do_review(cfg: Config, target: str | None = None, run_id: str | None = None,
              harness: Harness | None = None, profile: str | None = None,
              stage: str = "uninterrupted", stream: bool = True,
              dry_run: bool = False, default_file: str = "plan.md",
              interactive: bool = False) -> StepResult:
    h = harness or get_harness(cfg)
    rid = ensure_run(cfg, run_id)
    doc = resolve_target(cfg, target, default_file)
    artifact = cfg.runs_dir / rid / "artifacts" / "review"
    egm = cfg.egm_path()
    if h.name == "claude":
        if not egm:
            raise SystemExit("EGM not found: run `speckfish init` or set egm_root in .speckfish/config.json")
        prompt = with_language(prompts.review_prompt(doc, artifact, profile or cfg.profile, stage,
                                                     interactive=interactive), cfg)
    else:
        prompt = with_language(prompts.egm_prompt_for(
            h.name, "review", str(doc), artifact, egm or cfg.root,
            profile or cfg.profile, stage, interactive=interactive), cfg)
    if interactive:
        # the agent takes over this terminal: it asks, the operator answers
        return run_interactive(cfg, h, "review", prompt, cfg.root, rid, egm, dry_run=dry_run)
    return run_step(cfg, h, "review", prompt, cfg.root, rid, egm,
                    schema=prompts.SCHEMAS["review"], stream=stream, dry_run=dry_run)


def do_skill(cfg: Config, skill: str, args: str = "", run_id: str | None = None,
             harness: Harness | None = None, stream: bool = True,
             dry_run: bool = False, feature: str | None = None) -> StepResult:
    """Run any of the remaining Spec Kit skills (constitution, tasks, analyze, ...)."""
    h = harness or get_harness(cfg)
    rid = ensure_run(cfg, run_id)
    artifact = cfg.runs_dir / rid / "artifacts" / skill
    spec = prompts.SKILL_SPECS[skill]
    fdir = None
    if feature:
        fdir = set_active_feature(cfg, find_feature(cfg, feature))
    elif skill in ("tasks", "analyze", "checklist", "clarify", "converge", "implement"):
        fdir = active_feature(cfg)
    if h.name == "claude":
        prompt = with_language(prompts.skill_prompt(skill, args, artifact, cfg.root, fdir), cfg)
    else:
        prompt = with_language(prompts.skill_prompt_for(
            h.name, skill, args, artifact, cfg.egm_path() or cfg.root, cfg.root), cfg)
    res = run_step(cfg, h, skill, prompt, cfg.root, rid, cfg.egm_path(),
                   stream=stream, dry_run=dry_run)
    res.writes = spec.get("writes", "")  # type: ignore[attr-defined]
    return res


def json_object_with(text: str, key: str) -> dict:
    """Best JSON object in `text` whose `key` is a non-empty list.

    Harnesses without `--json-schema` (codex, copilot) answer the inventory in prose, so the JSON
    arrives inside the transcript instead of `structured_output` — and the same log may also contain
    JSON *schemas* (with `properties`) or one-item examples, which must not win.
    """
    decoder = json.JSONDecoder()
    best: dict = {}
    best_len = 0
    idx = 0
    while True:
        start = text.find("{", idx)
        if start < 0:
            break
        idx = start + 1
        try:
            obj, end = decoder.raw_decode(text[start:])
        except ValueError:
            continue
        if not isinstance(obj, dict) or "$schema" in obj or "properties" in obj:
            continue
        items = obj.get(key)
        if not isinstance(items, list) or not items:
            continue
        if not all(isinstance(i, dict) for i in items):
            continue
        score = len(items) + sum(1 for i in items if i.get("description") or i.get("why"))
        if score > best_len:
            best, best_len = obj, score
    return best


def inventory_payload(res, cfg: Config) -> dict:
    """The split inventory, from structured output or recovered from the step log."""
    data = res.structured if isinstance(res.structured, dict) else {}
    if data.get("features"):
        return data
    # harnesses that keep the transcript in memory (raw_tail) before it is on disk
    tail = getattr(res, "raw_tail", "") or ""
    if tail:
        found = json_object_with(tail, "features")
        if found:
            return found
    for source in (res.log,):
        if not source:
            continue
        try:
            text = Path(source).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        found = json_object_with(text, "features")
        if found:
            return found
    return data


def do_specify_split(cfg: Config, material: str, run_id: str | None = None,
                     harness: Harness | None = None, stream: bool = True,
                     dry_run: bool = False, max_features: int = 6,
                     review: bool = False, inventory_only: bool = False) -> dict:
    """Split a big brief into independent features and specify each one.

    Two calls to the agent: an inventory (JSON) and then one `/speckit-specify` per feature. Spec Kit
    itself allows exactly one feature per invocation, so a brief with three ideas would otherwise land
    in a single mixed spec.
    """
    h = harness or get_harness(cfg)
    rid = ensure_run(cfg, run_id)
    out: dict = {"run_id": rid, "harness": h.name, "steps": {}, "features": [], "specs": [],
                 "started": datetime.now().isoformat(timespec="seconds")}

    inventory = run_step(cfg, h, "specify-split", with_language(prompts.split_prompt(material, max_features, cfg.root), cfg),
                         cfg.root, rid, cfg.egm_path(), schema=prompts.SPLIT_SCHEMA,
                         stream=stream, dry_run=dry_run)
    out["steps"]["split"] = inventory.to_dict()
    payload = inventory_payload(inventory, cfg)
    features = payload.get("features") or []
    out["features"] = [f.get("title", "") for f in features]
    # the inventory is the cheap, high-value half: let the user approve the cut before paying for N specs
    (cfg.root / "specs").mkdir(parents=True, exist_ok=True)
    (cfg.root / "specs" / "features.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    if dry_run or inventory_only:
        out["inventory_only"] = bool(inventory_only)
        out["dry_run"] = bool(dry_run)
        return out

    if not features:
        out["error"] = "the agent did not return a usable feature list"
        out["raw"] = (inventory.raw_tail or "")[:1500]
        return out

    for i, feat in enumerate(features, start=1):
        idea = f"{feat.get('title', '')}. {feat.get('description', '')}".strip()
        res = run_step(cfg, h, f"specify-{i}", prompts.specify_prompt(idea, cfg.root),
                       cfg.root, rid, cfg.egm_path(), schema=prompts.SCHEMAS["specify"],
                       stream=stream)
        out["steps"][f"specify-{i}"] = res.to_dict()
        written = spec_path_from(res) or (latest_feature(cfg.root) or Path()) / "spec.md"
        if Path(written).exists():
            out["specs"].append({"feature": feat.get("title", ""), "path": norm(written)})
        if not res.ok:
            out["stopped_at"] = f"specify-{i}"
            break
        if review:
            spec = spec_path_from(res) or (latest_feature(cfg.root) or Path()) / "spec.md"
            if Path(spec).exists():
                rev = do_review(cfg, str(spec), rid, h, stream=stream, default_file="spec.md")
                out["steps"][f"review-{i}"] = rev.to_dict()
    out["ok"] = all(s.get("ok") for k, s in out["steps"].items() if k != "split") and bool(features)
    (cfg.runs_dir / rid / "split.json").write_text(json.dumps(out, indent=2, ensure_ascii=False),
                                                   encoding="utf-8")
    return out


def extract_features(res, cfg: Config | None = None) -> list[dict]:
    """Pull [{title, description}] out of a split step, whatever the harness returned."""
    found = inventory_payload(res, cfg or Config(root=Path.cwd())).get("features") or []
    feats = []
    for item in found:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        desc = str(item.get("description") or "").strip()
        if title or desc:
            feats.append({"title": title or desc[:60], "description": desc or title})
    return feats


def do_drift(cfg: Config, target: str | None = None, run_id: str | None = None,
             harness: Harness | None = None, stream: bool = True,
             dry_run: bool = False) -> StepResult:
    h = harness or get_harness(cfg)
    rid = ensure_run(cfg, run_id)
    if target:
        d = Path(target)
        if not d.is_absolute():
            d = cfg.root / d
    else:
        feat = latest_feature(cfg.root)
        d = feat if feat else cfg.root / "specs"
    if not d.exists():
        raise SystemExit(f"target directory does not exist: {d}")
    artifact = cfg.runs_dir / rid / "artifacts" / "drift"
    egm = cfg.egm_path()
    if h.name == "claude":
        if not egm:
            raise SystemExit("EGM not found: run `speckfish init`")
        prompt = with_language(prompts.drift_prompt(str(d), artifact), cfg)
    else:
        prompt = with_language(prompts.egm_prompt_for(h.name, "drift", str(d), artifact,
                                                      egm or cfg.root), cfg)
    return run_step(cfg, h, "drift", prompt, cfg.root, rid, egm,
                    schema=prompts.SCHEMAS["drift"], stream=stream, dry_run=dry_run)


def do_pipeline(cfg: Config, idea: str, stack: str = "", target: str | None = None,
                gate: bool = False, with_drift: bool = False, with_tasks: bool = False,
                with_analyze: bool = False, stream: bool = True,
                dry_run: bool = False) -> dict:
    """specify → plan → (tasks) → (analyze) → review → (drift), one run, with an optional gate."""
    h = get_harness(cfg)
    rid = ensure_run(cfg, None)
    results = {"run_id": rid, "harness": h.name, "steps": {}, "writes": {}}
    plan_of_steps: list[tuple[str, str]] = [("specify", "idea"), ("plan", "stack")]
    if with_tasks:
        plan_of_steps.append(("tasks", "tasks"))
    if with_analyze:
        plan_of_steps.append(("analyze", "analyze"))
    plan_of_steps.append(("review", "review"))

    for step, kind in plan_of_steps:
        if kind == "idea":
            res = do_specify(cfg, idea, rid, h, stream, dry_run)
        elif kind == "stack":
            res = do_plan(cfg, stack, rid, h, stream, dry_run)
        elif kind == "review":
            res = do_review(cfg, target, rid, h, stream=stream, dry_run=dry_run)
        else:
            res = do_skill(cfg, step, "", rid, h, stream, dry_run)
        results["steps"][step] = res.to_dict()
        blocked = bool(res.blockers) if step == "review" else False
        if gate and (not res.ok or blocked):
            results["stopped_at"] = step
            break

    if with_drift:
        if results.get("stopped_at"):
            # the gate already stopped the chain: auditing drift now would review a half-built story
            results["drift_skipped"] = f"gate stopped at {results['stopped_at']}"
        else:
            res = do_drift(cfg, run_id=rid, harness=h, stream=stream, dry_run=dry_run)
            results["steps"]["drift"] = res.to_dict()

    results["ok"] = all(s.get("ok") for s in results["steps"].values())
    results.update(run_totals(results["steps"]))
    run_dir = cfg.runs_dir / rid
    (run_dir / "pipeline.json").write_text(json.dumps(results, indent=2, ensure_ascii=False),
                                           encoding="utf-8")
    return results


def run_totals(steps: dict) -> dict:
    """Aggregate cost and duration for a run (costs are only summed when the unit matches)."""
    total_cost, unit = 0.0, None
    total_time = 0.0
    mixed = False
    for res in steps.values():
        cost = res.get("cost")
        if cost:
            if unit is None:
                unit = res.get("cost_unit")
            elif unit != res.get("cost_unit"):
                mixed = True
            total_cost += float(cost)
        total_time += float(res.get("duration_s") or 0)
    out = {"duration_s": round(total_time, 1)}
    if unit:
        out["cost"] = round(total_cost, 3)
        out["cost_unit"] = unit
        if mixed:
            out["cost_note"] = "mixed units (some steps reported a different currency/credit unit)"
    return out


def run_summary(cfg: Config, run_id: str) -> dict:
    """Read state.json for a run and add totals."""
    state_path = cfg.runs_dir / run_id / "state.json"
    if not state_path.exists():
        return {}
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state.update(run_totals(state.get("steps", {})))
    return state


def clean_runs(cfg: Config, keep: int = 10) -> dict:
    """Delete old run directories, keeping the newest `keep`."""
    import shutil as _shutil
    runs = sorted([p for p in cfg.runs_dir.iterdir() if p.is_dir()]) if cfg.runs_dir.exists() else []
    doomed = runs[:-keep] if keep > 0 else runs
    removed = []
    for p in doomed:
        _shutil.rmtree(p, ignore_errors=True)
        removed.append(p.name)
    return {"kept": [p.name for p in runs[-keep:]] if keep > 0 else [], "removed": removed}
