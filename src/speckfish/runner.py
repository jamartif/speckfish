"""Step execution: subprocess, timeout with process-tree kill, logging and state."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .config import Config, is_windows, norm
from .harnesses import Harness, StepInvocation


@dataclass
class StepResult:
    step: str
    ok: bool
    exit_code: int
    duration_s: float
    cost: float | None = None
    cost_unit: str = "usd"
    reports: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    majors: int = 0
    nits: int = 0
    meets_standard: bool | None = None
    structured: dict | None = None
    error: str = ""
    log: str = ""
    artifact_dir: str = ""
    subagents: int = 0
    premium_requests: int | None = None
    model: str | None = None
    writes: str = ""
    interactive: bool = False
    raw_tail: str = ""

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if k not in ("raw_tail",)}


def new_run_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def run_dir_for(cfg: Config, run_id: str) -> Path:
    d = cfg.runs_dir / run_id
    (d / "artifacts").mkdir(parents=True, exist_ok=True)
    (d / "logs").mkdir(parents=True, exist_ok=True)
    return d


def _kill_tree(proc: subprocess.Popen) -> None:
    try:
        if is_windows():
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def execute(harness: Harness, inv: StepInvocation, step: str, cwd: Path,
            run_dir: Path, timeout: int, stream: bool = True) -> tuple[int, str, str, float, bool]:
    """Launch the subprocess, write the log and honour the timeout by killing the tree.

    stdout is read on a thread so the timeout does not depend on the child
    printing anything (a blocking readline ignores deadlines).
    """
    import threading

    log_path = run_dir / "logs" / f"{step}.log"
    started = time.time()
    popen_kwargs = dict(cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        stdin=subprocess.DEVNULL, text=True, encoding="utf-8",
                        errors="replace", env={**os.environ, **(inv.env or {})})
    if is_windows():
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True
    proc = subprocess.Popen(inv.argv, **popen_kwargs)
    out_chunks: list[str] = []
    err_chunks: list[str] = []
    log = log_path.open("w", encoding="utf-8")
    log.write("$ " + " ".join(inv.argv) + "\n\n")

    def pump(pipe, chunks, echo=False):
        try:
            for line in pipe:
                chunks.append(line)
                log.write(line)
                log.flush()
                if echo and stream:
                    sys.stderr.write(line if line.endswith("\n") else line + "\n")
                    sys.stderr.flush()
        except Exception:
            pass

    t_out = threading.Thread(target=pump, args=(proc.stdout, out_chunks, True), daemon=True)
    t_err = threading.Thread(target=pump, args=(proc.stderr, err_chunks, False), daemon=True)
    t_out.start(); t_err.start()
    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_tree(proc)
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            pass
    t_out.join(timeout=15)
    t_err.join(timeout=5)
    try:
        log.close()
    except Exception:
        pass
    code = 124 if timed_out else (proc.returncode if proc.returncode is not None else 1)
    return code, "".join(out_chunks), "".join(err_chunks), time.time() - started, timed_out


def postprocess(res: StepResult, step: str, artifact_dir: Path) -> None:
    """Collect reports and verdict counts written by the agent for this step."""
    from .config import find_report, parse_consolidated

    if step == "review":
        report = find_report(artifact_dir, "consolidated_review_report.md")
        if report is None:
            report = find_report(artifact_dir)
        parsed = parse_consolidated(report) if report else {}
        res.reports = [norm(p) for p in sorted(artifact_dir.glob("*.md"))]
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
        if not res.reports:
            res.error = res.error or "no reports were written to the artifact directory"
            res.ok = False
    elif step == "drift":
        res.reports = [norm(p) for p in sorted(artifact_dir.glob("*.md"))]
        if not res.reports:
            res.error = res.error or "no drift report was written"
            res.ok = False


def record_state(cfg: Config, run_dir: Path, run_id: str, res: StepResult,
                 step: str, harness: Harness, started_at: str) -> None:
    state_path = run_dir / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    state.setdefault("run_id", run_id)
    state.setdefault("steps", {})
    entry = state["steps"].get(step, {})
    entry.update({"status": "ok" if res.ok else "failed", "started_at": started_at,
                  "duration_s": res.duration_s, "cost": res.cost, "cost_unit": res.cost_unit,
                  "blockers": len(res.blockers), "reports": res.reports,
                  "exit_code": res.exit_code, "harness": harness.name,
                  "model": res.model, "interactive": res.interactive})
    state["steps"][step] = entry
    state_path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    (run_dir / f"{step}.result.json").write_text(
        json.dumps(res.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")


def run_step(cfg: Config, harness: Harness, step: str, prompt: str, cwd: Path,
             run_id: str, egm_root: Path | None, timeout: int | None = None,
             budget: float | None = None, schema: dict | None = None,
             stream: bool = True, dry_run: bool = False) -> StepResult:
    run_dir = run_dir_for(cfg, run_id)
    artifact_dir = run_dir / "artifacts" / step
    artifact_dir.mkdir(parents=True, exist_ok=True)
    timeout = timeout or int(cfg.timeouts.get(step, 600))
    inv = harness.build(kind=step, prompt=prompt, cwd=cwd, artifact_dir=artifact_dir,
                        egm_root=egm_root, budget=budget, timeout=timeout, schema=schema)
    if dry_run:
        return StepResult(step=step, ok=True, exit_code=0, duration_s=0.0,
                          log=" ".join(inv.argv), artifact_dir=norm(artifact_dir),
                          error="dry-run")
    started_at = datetime.now().isoformat(timespec="seconds")
    code, out, err, duration, timed_out = execute(harness, inv, step, cwd, run_dir, timeout, stream)
    parsed = harness.parse_output(out, err, code) or {}
    res = StepResult(step=step, ok=(code == 0 and not timed_out), exit_code=code,
                     duration_s=round(duration, 1), log=norm(run_dir / "logs" / f"{step}.log"),
                     artifact_dir=norm(artifact_dir))
    res.cost = parsed.get("cost_usd") or parsed.get("credits")
    res.cost_unit = "credits" if parsed.get("credits") is not None else "usd"
    res.structured = parsed.get("structured")
    res.subagents = int(parsed.get("subagents") or 0)
    res.premium_requests = parsed.get("premium_requests")
    res.model = parsed.get("model") or (cfg.model or None)
    res.raw_tail = parsed.get("text_tail") or parsed.get("result_text") or parsed.get("raw_tail") or ""
    if timed_out:
        res.error = f"timeout after {timeout}s"
        res.ok = False
    elif code != 0:
        res.error = f"exit {code}: {(err or out)[-500:]}"
    postprocess(res, step, artifact_dir)
    record_state(cfg, run_dir, run_id, res, step, harness, started_at)
    return res


def run_interactive(cfg: Config, harness: Harness, step: str, prompt: str, cwd: Path,
                    run_id: str, egm_root: Path | None = None,
                    dry_run: bool = False) -> StepResult:
    """Run the agent in the operator's terminal: it asks, the human answers.

    stdio is inherited (no capture, no timeout). The prompt tells the agent it is an
    interactive session and that it must ask every open question.
    """
    run_dir = run_dir_for(cfg, run_id)
    artifact_dir = run_dir / "artifacts" / step
    artifact_dir.mkdir(parents=True, exist_ok=True)
    inv = harness.build_interactive(kind=step, prompt=prompt, cwd=cwd,
                                    artifact_dir=artifact_dir, egm_root=egm_root)
    if dry_run:
        return StepResult(step=step, ok=True, exit_code=0, duration_s=0.0, interactive=True,
                          log=" ".join(inv.argv), artifact_dir=norm(artifact_dir),
                          error="dry-run")
    started_at = datetime.now().isoformat(timespec="seconds")
    log_path = run_dir / "logs" / f"{step}.log"
    log_path.write_text("$ " + " ".join(inv.argv) + "\n\n"
                        "(interactive: the transcript lives in the operator terminal)\n",
                        encoding="utf-8")
    started = time.time()
    try:
        proc = subprocess.Popen(inv.argv, cwd=str(cwd),
                                env={**os.environ, **(inv.env or {})})
    except OSError as e:
        res = StepResult(step=step, ok=False, exit_code=126, duration_s=0.0, interactive=True,
                         error=f"cannot launch {inv.argv[0]}: {e}",
                         artifact_dir=norm(artifact_dir), log=norm(log_path))
        record_state(cfg, run_dir, run_id, res, step, harness, started_at)
        return res
    try:
        code = proc.wait()
    except KeyboardInterrupt:
        _kill_tree(proc)
        code = 130
    res = StepResult(step=step, ok=(code == 0), exit_code=code,
                     duration_s=round(time.time() - started, 1), interactive=True,
                     artifact_dir=norm(artifact_dir), log=norm(log_path))
    if code != 0:
        res.error = f"exit {code}"
    postprocess(res, step, artifact_dir)
    record_state(cfg, run_dir, run_id, res, step, harness, started_at)
    return res
