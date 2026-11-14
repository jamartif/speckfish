"""Step execution: subprocess, timeout with process-tree kill, logging and state."""
from __future__ import annotations

import contextlib
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
    denials: list = field(default_factory=list)
    verdict: str = ""
    error_kind: str = ""
    run_id: str = ""
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
        with contextlib.suppress(Exception):
            proc.kill()


def execute(harness: Harness, inv: StepInvocation, step: str, cwd: Path,
            run_dir: Path, timeout: int, stream: bool = True,
            progress: bool = True) -> tuple[int, str, str, float, bool]:
    """Launch the subprocess, write the log and honour the timeout by killing the tree.

    stdout is read on a thread so the timeout does not depend on the child
    printing anything (a blocking readline ignores deadlines).
    """
    import threading

    log_path = run_dir / "logs" / f"{step}.log"
    from .progress import Progress

    echo_lines = getattr(harness, "echo_stdout", False)
    # always show it: a harness that echoes prose still leaves you without a clock or a stage
    meter = Progress(step, enabled=progress, plain_only=echo_lines, root=str(cwd))
    started = time.time()
    meter.start()
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
                meter.feed(line)
                if echo and stream and not is_noise_line(line):
                    sys.stderr.write(line if line.endswith("\n") else line + "\n")
                    sys.stderr.flush()
        except Exception:
            pass

    t_out = threading.Thread(target=pump, args=(proc.stdout, out_chunks, echo_lines), daemon=True)
    t_err = threading.Thread(target=pump, args=(proc.stderr, err_chunks, False), daemon=True)
    t_out.start()
    t_err.start()
    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_tree(proc)
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=30)
    t_out.join(timeout=15)
    t_err.join(timeout=5)
    with contextlib.suppress(Exception):
        log.close()
    meter.stop()
    code = 124 if timed_out else (proc.returncode if proc.returncode is not None else 1)
    return code, "".join(out_chunks), "".join(err_chunks), time.time() - started, timed_out


def is_noise_line(line: str) -> bool:
    """JSON payloads and very long lines are log material, not console output."""
    text = (line or "").strip()
    if len(text) > 1200:
        return True
    if text.startswith("{") and text.endswith("}"):
        try:
            json.loads(text)
            return True
        except ValueError:
            return False
    return False


def _json_payloads(text: str) -> list[dict]:
    """Every JSON object the agent CLI printed, newest last (it emits NDJSON or one big object)."""
    found = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not (line.startswith("{") and line.endswith("}")):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            found.append(obj)
    if not found:
        start = (text or "").find("{")
        if start >= 0:
            try:
                obj = json.loads(text[start:])
                if isinstance(obj, dict):
                    found.append(obj)
            except ValueError:
                pass
    return found


def error_kind(out: str, err: str) -> str:
    """Classify a failed run: quota / auth / agent error / unknown."""
    payloads = _json_payloads(out) or _json_payloads(err)
    for obj in reversed(payloads):
        api = str(obj.get("api_error") or "").lower()
        status = obj.get("api_error_status")
        if obj.get("type") == "rate_limit_event":
            info = obj.get("rate_limit_info") or {}
            if str(info.get("status") or "") == "rejected":
                return "quota"
        if "usage_limit" in api or "rate_limit" in api or status == 429:
            return "quota"
        if status in (401, 403) or "auth" in api or "credit" in api:
            return "auth"
        if obj.get("is_error") or obj.get("error") == "rate_limit":
            return "quota" if obj.get("error") == "rate_limit" else "agent"
    low = (err or out or "").lower()
    if "usage limit" in low or "rate limit" in low or "429" in low:
        return "quota"
    if "not logged in" in low or "unauthorized" in low or "credit" in low:
        return "auth"
    return "unknown"


def _reset_hint(payloads: list[dict]) -> str:
    """`resets 20:00` when the agent tells us when the window reopens."""
    for obj in reversed(payloads):
        info = {}
        if obj.get("type") == "rate_limit_event":
            info = obj.get("rate_limit_info") or {}
        info = info or ((obj.get("api_error_params") or {}).get("rate_limit_info") or {})
        stamp = info.get("resetsAt")
        if isinstance(stamp, (int, float)) and stamp > 0:
            try:
                when = datetime.fromtimestamp(stamp).strftime("%H:%M")
            except (OverflowError, OSError, ValueError):
                return ""
            window = "weekly" if "seven_day" in str(info.get("rateLimitType") or "") else ""
            return f" · {window} window reopens at {when}".replace("  ", " ")
    return ""


def _best_payload(payloads: list[dict]) -> dict:
    """The payload that carries the human message, not just the last event."""
    for obj in reversed(payloads):
        if obj.get("result") or obj.get("error") or obj.get("api_error"):
            return obj
    return payloads[-1] if payloads else {}


def human_error(out: str, err: str, code: int) -> str:
    """A message a human can act on — never the agent's JSON payload."""
    payloads = _json_payloads(out) or _json_payloads(err)
    obj = _best_payload(payloads)
    message = str(obj.get("result") or obj.get("error") or "").strip()
    if message in ("rate_limit", "error"):
        message = ""
    api = str(obj.get("api_error") or "").strip()
    status = obj.get("api_error_status")
    hint = _reset_hint(payloads)

    kind = error_kind(out, err)
    if kind == "quota":
        detail = message or api or "rate limit reached"
        return f"the agent ran out of quota (HTTP {status or 429}): {detail}{hint}"
    if kind == "auth":
        return f"the agent rejected the credentials: {message or api or 'not authenticated'}{hint}"
    if kind == "agent" and message:
        return message[:300]
    if message:
        return f"exit {code}: {message[:300]}"

    tail = [ln.strip() for ln in (err or out or "").splitlines() if ln.strip()][-2:]
    if not tail:
        return f"exit {code} with no output"
    return f"exit {code}: " + " / ".join(t[-160:] for t in tail)


def postprocess(res: StepResult, step: str, artifact_dir: Path, root: Path | None = None) -> None:
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
        res.verdict = ((res.structured or {}).get("verdict") or parsed.get("verdict") or "").strip()
        if not res.reports:
            res.error = res.error or "no reports were written to the artifact directory"
            res.ok = False
    elif step == "specify":
        # a run that writes no spec.md is a failure, not a success with a nice essay
        root = Path(root) if root else None
        if res.ok and root is not None and not list((root / "specs").glob("*/spec.md")):
            res.ok = False
            res.error = ("the agent answered but wrote no spec.md under specs/ "
                         "(read-only sandbox, or it only printed the spec)")
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
                  "model": res.model, "interactive": res.interactive,
                  "majors": res.majors, "nits": res.nits,
                  "meets_standard": res.meets_standard, "verdict": res.verdict,
                  "denials": res.denials, "artifact_dir": res.artifact_dir,
                  "log": res.log})
    state["steps"][step] = entry
    state_path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    (run_dir / f"{step}.result.json").write_text(
        json.dumps(res.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")


def run_step(cfg: Config, harness: Harness, step: str, prompt: str, cwd: Path,
             run_id: str, egm_root: Path | None, timeout: int | None = None,
             budget: float | None = None, schema: dict | None = None,
             stream: bool = True, dry_run: bool = False,
             progress: bool = True) -> StepResult:
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
    code, out, err, duration, timed_out = execute(harness, inv, step, cwd, run_dir, timeout, stream,
                                                  progress)
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
    res.denials = parsed.get("denials") or []
    res.raw_tail = parsed.get("text_tail") or parsed.get("result_text") or parsed.get("raw_tail") or ""
    if timed_out:
        res.error = f"timeout after {timeout}s"
        res.ok = False
    elif code != 0:
        res.error = human_error(out, err, code)
        res.error_kind = error_kind(out, err)
    res.run_id = run_id
    postprocess(res, step, artifact_dir, cwd)
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
    res.run_id = run_id
    postprocess(res, step, artifact_dir, cwd)
    record_state(cfg, run_dir, run_id, res, step, harness, started_at)
    return res
