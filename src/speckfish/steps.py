"""Pasos del pipeline: init, specify, plan, review, drift, doctor."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from . import prompts
from .config import Config, find_git, norm, run_cmd, which
from .harnesses import Harness, get_harness
from .runner import StepResult, run_step, run_dir_for, new_run_id


# ---------------- infrastructura ----------------

SPECIFY_VIA_UV = ["uv", "tool", "run", "--from", "specify-cli", "specify"]


def specify_argv(cfg: Config) -> list[str]:
    """Devuelve la forma de invocar `specify` que funcione en esta máquina."""
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
    raise SystemExit("no encuentro `specify` ni `uv` para ejecutar Spec Kit")


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

def do_init(cfg: Config, integration: str | None, force_git: bool = True,
            skip_egm: bool = False) -> dict:
    root = cfg.root
    out: dict = {"root": norm(root), "actions": []}
    if not (root / ".git").exists() and force_git:
        code, o, e = run_cmd([find_git(), "init"], cwd=root, timeout=120)
        out["actions"].append({"git_init": code == 0, "detail": (o or e)[-300:]})
    harness_name = integration or "auto"
    if harness_name == "auto":
        h = get_harness(cfg)
    else:
        h = get_harness(cfg, harness_name)
    integration_name, extra = h.spec_kit_integration()
    args = ["init", "--here", "--force", "--non-interactive",
            "--integration", integration_name] + extra
    code, o, e = run_specify(cfg, args, root, timeout=300)
    out["actions"].append({"specify_init": code == 0, "integration": integration_name,
                           "argv": specify_argv(cfg) + args,
                           "detail": (o or e)[-600:]})
    if not skip_egm and not cfg.egm_path():
        out["actions"].append(install_egm(cfg))
    egm = cfg.egm_path()
    out["egm_root"] = norm(egm) if egm else None
    out["harness"] = h.name
    out["config"] = norm(cfg.save())
    (cfg.state_dir / "init.json").write_text(json.dumps(out, indent=2, ensure_ascii=False),
                                             encoding="utf-8")
    return out


def install_egm(cfg: Config, repo: str = "google/elephant-goldfish-model") -> dict:
    """Clona EGM en vendor/egm (no toca la config global del usuario)."""
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
        info["specify"] = {"argv": argv, "ok": code == 0,
                           "detail": (o or e).strip().splitlines()[:3]}
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


# ---------------- pasos del agente ----------------

def do_specify(cfg: Config, idea: str, run_id: str | None = None,
               harness: Harness | None = None, stream: bool = True,
               dry_run: bool = False) -> StepResult:
    h = harness or get_harness(cfg)
    rid = ensure_run(cfg, run_id)
    if h.name == "claude":
        prompt = prompts.specify_prompt(idea)
    else:
        prompt = (f"/speckit-specify {idea}\n\n{prompts.NO_QUESTIONS}"
                  if h.name == "claude" else
                  f"{idea}\n\n{prompts.NO_QUESTIONS}\n\nEjecuta el flujo de especificación de "
                  f"Spec Kit (skill speckit-specify) tal como está definido en el proyecto.")
    return run_step(cfg, h, "specify", prompt, cfg.root, rid, cfg.egm_path(),
                    schema=prompts.SCHEMAS["specify"], stream=stream, dry_run=dry_run)


def do_plan(cfg: Config, stack: str, run_id: str | None = None,
            harness: Harness | None = None, stream: bool = True,
            dry_run: bool = False) -> StepResult:
    h = harness or get_harness(cfg)
    rid = ensure_run(cfg, run_id)
    prompt = (prompts.plan_prompt(stack) if h.name == "claude"
              else f"Stack y restricciones: {stack}\n\n{prompts.NO_QUESTIONS}\n\n"
                   f"Ejecuta el flujo de planificación de Spec Kit (skill speckit-plan).")
    return run_step(cfg, h, "plan", prompt, cfg.root, rid, cfg.egm_path(),
                    schema=prompts.SCHEMAS["plan"], stream=stream, dry_run=dry_run)


def resolve_target(cfg: Config, target: str | None, default_file: str = "plan.md") -> Path:
    if target:
        p = Path(target)
        if not p.is_absolute():
            p = cfg.root / p
        if not p.exists():
            raise SystemExit(f"no existe el documento a revisar: {p}")
        return p
    feat = latest_feature(cfg.root)
    if not feat:
        raise SystemExit("no hay specs en el proyecto; ejecuta `speckfish specify` primero")
    p = feat / default_file
    if not p.exists():
        alt = feat / "spec.md"
        if alt.exists():
            return alt
        raise SystemExit(f"no encuentro {default_file} ni spec.md en {feat}")
    return p


def do_review(cfg: Config, target: str | None = None, run_id: str | None = None,
              harness: Harness | None = None, profile: str | None = None,
              stage: str = "uninterrupted", stream: bool = True,
              dry_run: bool = False, default_file: str = "plan.md") -> StepResult:
    h = harness or get_harness(cfg)
    rid = ensure_run(cfg, run_id)
    doc = resolve_target(cfg, target, default_file)
    artifact = cfg.runs_dir / rid / "artifacts" / "review"
    egm = cfg.egm_path()
    if h.name == "claude":
        if not egm:
            raise SystemExit("EGM no encontrado: ejecuta `speckfish init` o define egm_root en .speckfish/config.json")
        prompt = prompts.review_prompt(doc, artifact, profile or cfg.profile, stage)
    else:
        prompt = prompts.egm_prompt_for(h.name, "review", str(doc), artifact,
                                        egm or cfg.root, profile or cfg.profile, stage)
    return run_step(cfg, h, "review", prompt, cfg.root, rid, egm,
                    schema=prompts.SCHEMAS["review"], stream=stream, dry_run=dry_run)


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
        raise SystemExit(f"no existe el directorio a analizar: {d}")
    artifact = cfg.runs_dir / rid / "artifacts" / "drift"
    egm = cfg.egm_path()
    if h.name == "claude":
        if not egm:
            raise SystemExit("EGM no encontrado: ejecuta `speckfish init`")
        prompt = prompts.drift_prompt(str(d), artifact)
    else:
        prompt = prompts.egm_prompt_for(h.name, "drift", str(d), artifact, egm or cfg.root)
    return run_step(cfg, h, "drift", prompt, cfg.root, rid, egm,
                    schema=prompts.SCHEMAS["drift"], stream=stream, dry_run=dry_run)


def do_pipeline(cfg: Config, idea: str, stack: str = "", target: str | None = None,
                gate: bool = False, with_drift: bool = False,
                stream: bool = True, dry_run: bool = False) -> dict:
    h = get_harness(cfg)
    rid = ensure_run(cfg, None)
    results = {"run_id": rid, "harness": h.name, "steps": {}}
    for name, fn, kwargs in (
        ("specify", do_specify, {"idea": idea}),
        ("plan", do_plan, {"stack": stack}),
        ("review", do_review, {"target": target}),
    ):
        res = fn(cfg, **kwargs, run_id=rid, harness=h, stream=stream, dry_run=dry_run)
        results["steps"][name] = res.to_dict()
        if gate and not res.ok and name != "review":
            results["stopped_at"] = name
            break
        if gate and name == "review" and res.blockers:
            results["stopped_at"] = name
            break
    if with_drift and not dry_run:
        res = do_drift(cfg, run_id=rid, harness=h, stream=stream, dry_run=dry_run)
        results["steps"]["drift"] = res.to_dict()
    results["ok"] = all(s.get("ok") for s in results["steps"].values())
    run_dir = cfg.runs_dir / rid
    (run_dir / "pipeline.json").write_text(json.dumps(results, indent=2, ensure_ascii=False),
                                           encoding="utf-8")
    return results
