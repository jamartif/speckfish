"""Step execution: subprocess, timeout with process-tree kill, logging and state."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import proctree
from .config import Config, agent_env, env_number, is_windows, norm
from .harnesses import Harness, StepInvocation
from .registry import missing_output
from .store import file_lock, read_json_retry, write_json_atomic
from .workspace import _git_dirty, feature_dir, git_snapshot, task_progress  # noqa: F401  (re-exported)


@dataclass
class StepResult:
    step: str
    ok: bool
    exit_code: int
    duration_s: float
    kind: str = ""
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
    tasks_done: int | None = None
    tasks_total: int | None = None
    error_kind: str = ""
    run_id: str = ""
    writes: str = ""
    interactive: bool = False
    raw_tail: str = ""
    review: dict | None = None     # `specify --review`: the spec's review, as a result dict
    activity: dict | None = None   # what the agent did: actions, commands, files written, subagents
    suspended_s: float = 0.0       # time the computer was suspended during the step (not counted)

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if k not in ("raw_tail",)}


def format_span(seconds: float) -> str:
    """`4h 08m`, `1m 35s`, `40s`: a length of time a person can read."""
    total = round(seconds)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m {secs:02d}s" if minutes else f"{secs}s"


def new_run_id(cfg: Config | None = None) -> str:
    """`20261126-101500`; with a `cfg` the folder is claimed on the spot (`-2`, `-3`, ... if taken).

    Resolution is one second, so two processes started together (two CI jobs on one checkout) used to
    share a run folder and overwrite each other's state. `mkdir` is atomic: exactly one of them wins a name.
    """
    base = datetime.now().strftime("%Y%m%d-%H%M%S")
    if cfg is None:
        return base
    cfg.runs_dir.mkdir(parents=True, exist_ok=True)
    n = 1
    while True:
        rid = base if n == 1 else f"{base}-{n}"
        try:
            (cfg.runs_dir / rid).mkdir()
            return rid
        except FileExistsError:
            n += 1


def run_dir_for(cfg: Config, run_id: str) -> Path:
    d = cfg.runs_dir / run_id
    (d / "artifacts").mkdir(parents=True, exist_ok=True)
    (d / "logs").mkdir(parents=True, exist_ok=True)
    return d


def _kill_tree(proc: subprocess.Popen, job: proctree.Job | None = None) -> tuple[bool, str]:
    """Stop the agent and everything it started, and say whether that worked (see proctree)."""
    return proctree.kill_tree(proc, job, windows=is_windows())


def _stop_and_report(proc: subprocess.Popen, job: proctree.Job | None) -> bool:
    """Kill the tree and tell the person if it did not die: a silent failure looks like "Ctrl+C does nothing"."""
    try:
        ok, how = _kill_tree(proc, job)
    except KeyboardInterrupt:                       # Ctrl+C again: stop being polite
        with contextlib.suppress(Exception):
            proc.kill()
        ok, how = proc.poll() is not None, "forced"
    if not ok:
        sys.stderr.write(f"WARNING: the agent (pid {proc.pid}) is still running — {how}.\n"
                         f"  Stop it by hand: {proctree.manual_hint(proc.pid, is_windows())}\n")
        sys.stderr.flush()
    return ok


def execute(harness: Harness, inv: StepInvocation, step: str, cwd: Path,
            run_dir: Path, timeout: int, stream: bool = True,
            progress: bool = True,
            label: str | None = None,
            env_allowlist: str = "",
            stats: dict | None = None) -> tuple[int, str, str, float, bool, bool]:
    """Launch the subprocess, write the log and honour the timeout by killing the tree.

    stdout is read on a thread so the timeout does not depend on the child
    printing anything (a blocking readline ignores deadlines).
    """
    import threading

    # a batch runs one step once per feature: the log is named by the label or they overwrite
    log_path = run_dir / "logs" / f"{label or step}.log"
    from .progress import Progress

    echo_lines = getattr(harness, "echo_stdout", False)
    # always show it: a harness that echoes prose still leaves you without a clock or a stage
    meter = Progress(step, enabled=progress, plain_only=echo_lines, root=str(cwd))
    started = time.time()
    meter.start()
    has_stdin = getattr(inv, "stdin", None) is not None
    popen_kwargs = dict(cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        stdin=subprocess.PIPE if has_stdin else subprocess.DEVNULL, text=True,
                        encoding="utf-8", errors="replace",
                        env=agent_env(inv.env, env_allowlist))
    if is_windows():
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(inv.argv, **popen_kwargs)
    except OSError as e:
        # the binary vanished, is not executable or is a Windows shim: a failed step, not a traceback,
        # and the progress line must not be left running
        meter.stop()
        message = f"cannot launch {inv.argv[0]}: {e}"
        log_path.write_text("$ " + " ".join(inv.argv) + "\n\n" + message + "\n", encoding="utf-8")
        return 127, "", message, time.time() - started, False, False
    # Windows: one job for the whole tree, so a single call can stop it (and the OS does if we die)
    job = proctree.start(proc) if is_windows() else None
    out_chunks: list[str] = []
    err_chunks: list[str] = []
    log = log_path.open("w", encoding="utf-8")
    log.write("$ " + " ".join(inv.argv) + "\n\n")
    if has_stdin:
        log.write("--- prompt (sent on stdin) ---\n" + inv.stdin + "\n--- end of prompt ---\n\n")
    if has_stdin:
        def feed() -> None:
            # a thread: a prompt bigger than the pipe buffer must not block us while the agent writes output
            try:
                proc.stdin.write(inv.stdin)
                proc.stdin.close()
            except (OSError, ValueError):
                pass                             # the agent exited before reading it all

        threading.Thread(target=feed, daemon=True).start()

    activity = {"t": time.time(), "final": False}

    def pump(pipe, chunks, echo=False):
        try:
            for line in pipe:
                activity["t"] = time.time()
                if _is_final_event(line):
                    activity["final"] = True
                chunks.append(line)
                log.write(line)
                log.flush()
                meter.feed(line)
                if echo and stream and not is_noise_line(line):
                    sys.stderr.write(line if line.endswith("\n") else line + "\n")
                    sys.stderr.flush()
        except Exception:  # noqa: S110  a reader thread must never raise: the step result is the product
            pass

    t_out = threading.Thread(target=pump, args=(proc.stdout, out_chunks, echo_lines), daemon=True)
    t_err = threading.Thread(target=pump, args=(proc.stderr, err_chunks, False), daemon=True)
    t_out.start()
    t_err.start()
    timed_out = False
    idle_closed = False
    # an agent can finish its work and never exit (EGM's final pass does this): a long silence with
    # the process still alive is a finished run, not a hang worth burning the whole timeout on
    idle_limit = int(env_number("SPECKFISH_IDLE_SECONDS", 600))
    # once the agent has printed its final event ("result" / "turn.completed") a process that does not
    # exit is only hanging on its way out (seen with Claude on Windows: 10 minutes after the last report)
    grace = int(env_number("SPECKFISH_RESULT_GRACE", 45))
    # how often the timeout and the inactivity rule are looked at: it is the latency of both
    poll = env_number("SPECKFISH_POLL_SECONDS", 1.0, minimum=0.01)
    # A pause longer than this between two looks at the process is the computer being suspended (sleep,
    # Modern Standby, hibernation), not the agent being silent: everything is frozen while it lasts. Counting
    # it made a step "wrote nothing for 600s" and last four hours, and killed it on waking.
    suspend_gap = env_number("SPECKFISH_SUSPEND_SECONDS", 30.0, minimum=1.0)
    suspended = 0.0
    guard = proctree.ConsoleGuard(enabled=is_windows())
    try:
        with guard:
            last_look = time.time()
            while True:
                try:
                    proc.wait(timeout=poll)
                    exited = True
                except subprocess.TimeoutExpired:
                    exited = False
                now = time.time()
                gap, last_look = now - last_look, now
                if gap > suspend_gap:
                    lost = gap - poll
                    suspended += lost
                    started += lost                              # the deadlines move by what was lost
                    activity["t"] = min(activity["t"] + lost, now)   # and so does the silence clock
                    _say_suspended(meter, label or step, lost)
                if exited:
                    break
                guard.tick()                    # an agent that put the console in raw mode kills Ctrl+C
                limit = min(idle_limit, grace) if activity["final"] and idle_limit else idle_limit
                if limit and now - activity["t"] >= limit:
                    idle_closed = True
                elif now - started >= timeout:
                    timed_out = True
                else:
                    continue
                _stop_and_report(proc, job)
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=30)
                break
    except KeyboardInterrupt:
        # Ctrl+C reaches speckfish, not the agent's own process group: stop the whole tree or it
        # keeps running (and writing, with the sandbox off) after the command is gone
        meter.stop()
        sys.stderr.write("\nStopping the agent…  (Ctrl+C again forces it)\n")
        sys.stderr.flush()
        _stop_and_report(proc, job)
        with contextlib.suppress(Exception):
            log.close()
        if job is not None:
            job.close()
        raise
    # the clock stops here, before the work of closing: joining the readers and clearing the status line can
    # block (a stuck console, a slow disk) and that is not time the agent spent
    duration = time.time() - started
    if stats is not None:
        stats["suspended_s"] = suspended
    t_out.join(timeout=15)
    t_err.join(timeout=5)
    with contextlib.suppress(Exception):
        log.close()
    meter.stop()
    if job is not None:
        job.close()                  # anything the agent left running goes with it (the job kills on close)
    code = 124 if timed_out else (proc.returncode if proc.returncode is not None else 1)
    return (code, "".join(out_chunks), "".join(err_chunks), duration, timed_out,
            idle_closed)


def _say_suspended(meter, label: str, lost: float) -> None:
    """Tell the person the computer was paused and that the pause is not counted."""
    text = f"the computer was suspended for {format_span(lost)}; that time is not counted"
    if getattr(meter, "enabled", False):
        meter.activity(f"warning: {text}", milestone=True, style="yellow")
    else:
        sys.stderr.write(f"[{label}] {text}\n")
        sys.stderr.flush()


def _is_final_event(line: str) -> bool:
    """The agent CLI's last event of a turn: Claude/Copilot `result`, Codex `turn.completed`."""
    text = (line or "").lstrip()
    if not text.startswith("{") or not ('"result"' in text or "turn.completed" in text):
        return False
    try:
        obj = json.loads(text)
    except ValueError:
        return False
    return isinstance(obj, dict) and obj.get("type") in ("result", "turn.completed")


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


# a bare "429" or "credit" appears in ports, ids and ordinary prose: ask for the HTTP / billing context
_HTTP_429 = re.compile(r"(http|status|error|code)[^\n]{0,24}\b429\b|\b429\b[^\n]{0,24}too many requests")
_NO_CREDITS = re.compile(r"(insufficient|no|out of|exhausted|run out of)\s+(ai\s+)?credits?\b"
                         r"|credits?\s+(exhausted|required|depleted)")


def error_kind(out: str, err: str, payloads: list[dict] | None = None) -> str:
    """Classify a failed run: quota / auth / agent error / unknown.

    `payloads` is what `_json_payloads` already extracted; pass it to avoid parsing a big stream twice.
    """
    if payloads is None:
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
    if "usage limit" in low or "rate limit" in low or _HTTP_429.search(low):
        return "quota"
    if "not logged in" in low or "unauthorized" in low or _NO_CREDITS.search(low):
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
    if (err or "").startswith("cannot launch"):
        return err.strip()[:300]            # ours, complete: the tail logic below would cut the path
    payloads = _json_payloads(out) or _json_payloads(err)
    obj = _best_payload(payloads)
    message = str(obj.get("result") or obj.get("error") or "").strip()
    if message in ("rate_limit", "error"):
        message = ""
    api = str(obj.get("api_error") or "").strip()
    status = obj.get("api_error_status")
    hint = _reset_hint(payloads)

    kind = error_kind(out, err, payloads)
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


def step_kind(step: str) -> str:
    """Legacy: the kind hidden in a label (`review-001-foo` -> `review`). Callers now pass `kind=`."""
    if step == "specify-split":
        return "split"
    return step.split("-")[0]


def postprocess(res: StepResult, step: str, artifact_dir: Path, root: Path | None = None,
                before: tuple[int, int] | None = None,
                git_before: dict | None = None, kind: str | None = None) -> None:
    """Run the validator the registry declares for this kind of step (reports, verdicts, outputs)."""
    from .registry import Ctx, spec_for

    spec = spec_for(kind or step_kind(step))
    if spec is not None and spec.validate is not None:
        spec.validate(res, Ctx(artifact_dir=artifact_dir, root=root, tasks_before=before,
                               git_before=git_before))


def record_state(cfg: Config, run_dir: Path, run_id: str, res: StepResult,
                 step: str, harness: Harness, started_at: str) -> None:
    state_path = run_dir / "state.json"
    # read-modify-write under a lock, and written atomically: another process on the same run (or a
    # Ctrl+C in the middle of the write) must not lose entries or leave a truncated file behind
    with file_lock(state_path):
        # missing or not JSON (an older interrupted write): start it again. A file that is merely busy is
        # retried and then raised, never taken for empty (that would erase the run's history)
        state = read_json_retry(state_path) or {}
        if not isinstance(state, dict):
            state = {}
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
                      "log": res.log, "tasks_done": res.tasks_done, "tasks_total": res.tasks_total})
        state["steps"][step] = entry
        write_json_atomic(state_path, state)
    write_json_atomic(run_dir / f"{step}.result.json", res.to_dict())


def _base_step(cfg: Config, step: str) -> str:
    """Legacy: `review-001-foo` -> `review`. New code passes `kind=` to run_step instead."""
    if step in cfg.timeouts:
        return step
    for key in cfg.timeouts:
        if step.startswith(f"{key}-"):
            return key
    return step


_VERSIONS: dict[str, str] = {}


def _agent_version(harness: Harness) -> str:
    """`claude --version` and friends, asked once per process (it costs a subprocess)."""
    name = getattr(harness, "name", "")
    if name not in _VERSIONS:
        try:
            _VERSIONS[name] = str(harness.doctor().get("version") or "")
        except Exception:                         # a fake or broken adapter: the metadata is a courtesy
            _VERSIONS[name] = ""
    return _VERSIONS[name]


def write_meta(cfg: Config, harness: Harness, inv: StepInvocation, prompt: str, res: StepResult,
               kind: str, label: str, started_at: str, egm_root: Path | None, run_dir: Path) -> None:
    """`logs/<label>.meta.json`: what is needed to understand or repeat this step, with no secrets in it.

    The prompt itself stays in the log; here only its length and hash. Long arguments (the prompt, a JSON
    schema) are replaced by their size, and environment variables are never listed, only whether the
    allowlist was on.
    """
    from . import __version__, trust
    argv = [a if len(a) <= 300 else f"<{len(a)} chars>" for a in inv.argv]
    egm: dict | None = None
    if egm_root:
        from .steps import egm_commit
        egm = {"path": norm(egm_root), "commit": egm_commit(Path(egm_root)),
               "trusted": trust.status(cfg, Path(egm_root))[0]}
    describe = getattr(harness, "permissions", None)
    meta = {
        "label": label, "kind": kind, "speckfish": __version__, "started_at": started_at,
        "ok": res.ok, "exit_code": res.exit_code, "duration_s": res.duration_s, "suspended_s": res.suspended_s,
        "cost": res.cost, "cost_unit": res.cost_unit, "model": res.model,
        "harness": harness.name, "harness_version": _agent_version(harness),
        "argv": argv, "prompt_via": "stdin" if getattr(inv, "stdin", None) is not None else "argv",
        "prompt_chars": len(prompt), "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "permissions": describe(kind) if callable(describe) else "",
        "permissions_setting": cfg.permissions,
        "env_filtered": bool(cfg.env_allowlist or os.environ.get("SPECKFISH_ENV_ALLOWLIST")),
        "egm": egm,
    }
    write_json_atomic(run_dir / "logs" / f"{label}.meta.json", meta)


def run_step(cfg: Config, harness: Harness, step: str, prompt: str, cwd: Path,
             run_id: str, egm_root: Path | None, timeout: int | None = None,
             budget: float | None = None, schema: dict | None = None,
             stream: bool = True, dry_run: bool = False,
             progress: bool = True, artifact_name: str | None = None,
             kind: str | None = None) -> StepResult:
    """Run one agent step.

    `kind` is what the step *is* (review, plan, ...): budgets, timeouts and validation follow it.
    The label (`artifact_name`, else `step`) is *which run of it* (`review-001-foo`): folders, logs
    and state follow that. Callers that do not pass `kind` get it derived from the label.
    """
    from . import trust
    from .registry import timeout_key

    trust.require(cfg, egm_root)             # before anything is created or launched
    run_dir = run_dir_for(cfg, run_id)
    # a batch runs the same step once per feature: each one needs its own folder and log or they overwrite
    label = artifact_name or step
    kind = kind or step_kind(_base_step(cfg, step))
    artifact_dir = run_dir / "artifacts" / label
    artifact_dir.mkdir(parents=True, exist_ok=True)
    # the timeout belongs to the kind of step, not to its label
    timeout = timeout or int(cfg.timeouts.get(timeout_key(kind), 600))
    inv = harness.build(kind=kind, prompt=prompt, cwd=cwd, artifact_dir=artifact_dir,
                        egm_root=egm_root, budget=budget, timeout=timeout, schema=schema)
    if dry_run:
        stdin = getattr(inv, "stdin", None)
        shown = " ".join(inv.argv) + (f"\n--- prompt (stdin) ---\n{stdin}" if stdin else "")
        return StepResult(step=step, kind=kind, ok=True, exit_code=0, duration_s=0.0,
                          log=shown, artifact_dir=norm(artifact_dir), error="dry-run")
    started_at = datetime.now().isoformat(timespec="seconds")
    is_implement = kind == "implement"
    before_tasks = task_progress(Path(cwd)) if is_implement else None
    before_git = git_snapshot(Path(cwd)) if is_implement else None
    from . import progress as _progress
    _progress.take_last()                       # forget what an earlier step left
    stats: dict = {}
    code, out, err, duration, timed_out, idle_closed = execute(
        harness, inv, step, cwd, run_dir, timeout, stream, progress, label=label,
        env_allowlist=cfg.env_allowlist, stats=stats)
    activity = _progress.take_last()
    idle_limit = int(env_number("SPECKFISH_IDLE_SECONDS", 600))
    if idle_closed and not timed_out:
        # the agent was stopped by us after going quiet, so the exit code is ours, not its verdict:
        # what the step wrote decides (postprocess below), not the signal that ended the process
        code = 0
    parsed = harness.parse_output(out, err, code) or {}
    res = StepResult(step=step, kind=kind, ok=(code == 0 and not timed_out), exit_code=code,
                     duration_s=round(duration, 1), log=norm(run_dir / "logs" / f"{label}.log"),
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
        res.error = (f"timed out after {timeout}s — the agent may simply need longer. "
                     f"Raise it with `speckfish config timeout.{timeout_key(kind)}="
                    f"{int(timeout * 2)}` "
                     f"and check the log for how far it got")
        res.ok = False
    elif code != 0:
        res.error = human_error(out, err, code)
        res.error_kind = error_kind(out, err)
    res.run_id = run_id
    res.activity = activity
    res.suspended_s = round(stats.get("suspended_s", 0.0), 1)
    postprocess(res, step, artifact_dir, cwd, before=before_tasks, git_before=before_git, kind=kind)
    if idle_closed:
        # the work landed but the CLI never exited: keep it when the step's own output is there
        missing = missing_output(kind, Path(cwd), res, feature_dir(Path(cwd)))
        if missing:
            res.ok = False
            res.error = (f"the agent wrote nothing for {idle_limit}s and was stopped; "
                         f"{missing}")
        else:
            res.error = (f"the agent wrote nothing for {idle_limit}s yet stayed alive; stopped it and "
                         f"kept what it had already written")
    # keyed by label: a batch runs `plan` once per feature and each needs its own entry
    record_state(cfg, run_dir, run_id, res, label, harness, started_at)
    with contextlib.suppress(Exception):          # metadata must never fail a step that already ran
        write_meta(cfg, harness, inv, prompt, res, kind, label, started_at, egm_root, run_dir)
    return res


def run_interactive(cfg: Config, harness: Harness, step: str, prompt: str, cwd: Path,
                    run_id: str, egm_root: Path | None = None,
                    dry_run: bool = False) -> StepResult:
    """Run the agent in the operator's terminal: it asks, the human answers.

    stdio is inherited (no capture, no timeout). The prompt tells the agent it is an
    interactive session and that it must ask every open question.
    """
    from . import trust
    trust.require(cfg, egm_root)
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
