"""Harness adapters: claude, copilot, codex.

Cada adaptador sabe:
  - build the headless command line,
  - declare whether it supports a structured output schema,
  - report the Spec Kit integration name,
  - state its limitations.
"""
from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from .config import Config, agent_prefix, norm, run_cmd


@dataclass
class StepInvocation:
    argv: list[str]
    env: dict | None = None
    stdin: str | None = None
    schema_supported: bool = False
    cost_unit: str = "usd"


class Harness:
    name = "base"
    supports_schema = False
    supports_budget = False
    verified = False
    notes = ""

    def __init__(self, cfg: Config):
        self.cfg = cfg

    # ---- entorno ----
    def binary(self) -> str | None:
        raise NotImplementedError

    def doctor(self) -> dict:
        b = self.binary()
        info = {"harness": self.name, "binary": b, "installed": bool(b), "verified": self.verified}
        if b:
            code, out, err = run_cmd([b, "--version"], timeout=60)
            info["version"] = (out or err).strip().splitlines()[0] if (out or err).strip() else ""
            info["version_exit"] = code
        info["notes"] = self.notes
        return info

    def spec_kit_integration(self) -> tuple[str, list[str]]:
        """(Spec Kit integration name, extra `specify init` options)."""
        raise NotImplementedError

    # ---- step execution ----
    def build(self, kind: str, prompt: str, cwd: Path, artifact_dir: Path,
              egm_root: Path | None, budget: float | None,
              timeout: int, schema: dict | None) -> StepInvocation:
        raise NotImplementedError

    # ---- output parsing ----
    def parse_output(self, stdout: str, stderr: str, exit_code: int) -> dict:
        return {"raw_tail": (stdout or stderr or "")[-1500:]}


class ClaudeHarness(Harness):
    name = "claude"
    supports_schema = True
    supports_budget = True
    verified = True
    notes = "Verificado en Windows: slash /egm:*, --plugin-dir, --json-schema, --max-budget-usd."

    def binary(self):
        return shutil.which("claude")

    def prefix(self):
        return agent_prefix("claude")

    def spec_kit_integration(self):
        return "claude", []

    def build(self, kind, prompt, cwd, artifact_dir, egm_root, budget, timeout, schema):
        argv = (self.prefix() or ["claude"]) + ["-p", prompt,
                "--permission-mode", "acceptEdits",
                "--allowedTools", "Bash", "Write", "Edit", "Read", "Glob", "Grep", "Agent",
                "--output-format", "json"]
        if egm_root:
            argv += ["--plugin-dir", norm(egm_root)]
        if schema:
            argv += ["--json-schema", json.dumps(schema, ensure_ascii=False)]
        amount = budget if isinstance(budget, (int, float)) else \
            self.cfg.budgets.get("claude", {}).get(kind)
        if amount:
            argv += ["--max-budget-usd", str(amount)]
        if self.cfg.model:
            argv += ["--model", self.cfg.model]
        return StepInvocation(argv=argv, schema_supported=bool(schema))

    def parse_output(self, stdout, stderr, exit_code):
        data = {}
        try:
            data = json.loads(stdout[stdout.find("{"):stdout.rfind("}") + 1])
        except Exception:
            pass
        res = {
            "structured": data.get("structured_output"),
            "cost_usd": data.get("total_cost_usd"),
            "turns": data.get("num_turns"),
            "denials": data.get("permission_denials") or data.get("permission_denials_count") or [],
            "is_error": data.get("is_error"),
            "subtype": data.get("subtype"),
            "result_text": (data.get("result") or "")[:3000],
            "subagents": int((data.get("subagent_stats") or {}).get("spawned") or 0),
            "premium_requests": None,
        }
        if not res["cost_usd"]:
            res["raw_tail"] = (stdout or stderr)[-1500:]
        return res


class CopilotHarness(Harness):
    name = "copilot"
    supports_schema = False
    supports_budget = True
    verified = True
    notes = ("Verified on Windows: -p headless, --plugin-dir registers the egm plugin, "
             "--output-format json (JSONL), --max-ai-credits. No --json-schema: the verdict "
             "is parsed from the Markdown report.")

    def binary(self):
        return shutil.which("copilot")

    def prefix(self):
        return agent_prefix("copilot")

    def spec_kit_integration(self):
        return "copilot", []

    def build(self, kind, prompt, cwd, artifact_dir, egm_root, budget, timeout, schema):
        argv = (self.prefix() or ["copilot"]) + ["-p", prompt, "--output-format", "json",
                "--allow-all-tools", "--allow-all-paths"]
        if egm_root:
            argv += ["--plugin-dir", norm(egm_root), "--add-dir", norm(egm_root),
                     "--agent", "egm:egm"]
        credits = self.cfg.budgets.get("copilot", {}).get(kind)
        if credits:
            # the Copilot CLI rejects anything below 30 credits
            argv += ["--max-ai-credits", str(max(30, int(credits)))]
        if self.cfg.model:
            argv += ["--model", self.cfg.model]
        return StepInvocation(argv=argv, schema_supported=False)

    def parse_output(self, stdout, stderr, exit_code):
        """Copilot JSONL: message deltas, credits and subagents."""
        by_msg: dict[str, list[str]] = {}
        order: list[str] = []
        credits = None
        premium = None
        subagents = 0
        session_ms = None
        for line in (stdout or "").splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue
            t = ev.get("type")
            data = ev.get("data") or {}
            if t == "assistant.message_delta":
                mid = data.get("messageId") or "_"
                if mid not in by_msg:
                    by_msg[mid] = []
                    order.append(mid)
                by_msg[mid].append(data.get("deltaContent") or "")
            elif t == "assistant.message" and isinstance(data.get("content"), str):
                mid = data.get("messageId") or "_"
                by_msg.setdefault(mid, [])
                if mid not in order:
                    order.append(mid)
                by_msg[mid] = [data["content"]]
            elif t == "subagent.started":
                subagents += 1
            elif t == "session.usage_checkpoint":
                nano = data.get("totalNanoAiu")
                if isinstance(nano, (int, float)):
                    credits = round(nano / 1e9, 3)
                if isinstance(data.get("totalPremiumRequests"), int):
                    premium = data["totalPremiumRequests"]
            elif t == "result":
                usage = ev.get("usage") or {}
                if isinstance(usage.get("premiumRequests"), int):
                    premium = usage["premiumRequests"]
                if isinstance(usage.get("sessionDurationMs"), int):
                    session_ms = usage["sessionDurationMs"]
        text = ""
        for mid in reversed(order):
            tail = "".join(by_msg.get(mid) or [])
            if tail.strip():
                text = tail
                break
        return {
            "credits": credits,
            "premium_requests": premium,
            "subagents": subagents,
            "session_ms": session_ms,
            "text_tail": text[-3000:] or (stderr or "")[-1500:],
            "exit_code": exit_code,
        }


class CodexHarness(Harness):
    name = "codex"
    supports_schema = False
    supports_budget = False
    verified = False
    notes = ("Unverified adapter: codex CLI was not installed on the test machine. "
             "Uses `codex exec` plus the plugin skills; see EGM docs/codex.md.")

    def binary(self):
        return shutil.which("codex")

    def prefix(self):
        return agent_prefix("codex")

    def spec_kit_integration(self):
        return "codex", ["--integration-options=--skills"]

    def build(self, kind, prompt, cwd, artifact_dir, egm_root, budget, timeout, schema):
        argv = (self.prefix() or ["codex"]) + ["exec", "--skip-git-repo-check", prompt]
        if egm_root:
            argv += ["--add-dir", norm(egm_root)]
        return StepInvocation(argv=argv, schema_supported=False)

    def parse_output(self, stdout, stderr, exit_code):
        return {"text_tail": (stdout or stderr)[-3000:], "exit_code": exit_code}


REGISTRY = {"claude": ClaudeHarness, "copilot": CopilotHarness, "codex": CodexHarness}


def detect_order() -> list[str]:
    """Preference order when harness=auto: installed and verified first."""
    order = []
    if shutil.which("claude"):
        order.append("claude")
    if shutil.which("copilot"):
        order.append("copilot")
    if shutil.which("codex"):
        order.append("codex")
    return order


def get_harness(cfg: Config, name: str | None = None) -> Harness:
    want = name or cfg.harness or "auto"
    if want != "auto":
        if want not in REGISTRY:
            raise SystemExit(f"unknown harness: {want} (use claude|copilot|codex)")
        h = REGISTRY[want](cfg)
        if not h.binary():
            raise SystemExit(f"harness '{want}' is not installed (binary not found on PATH)")
        return h
    for cand in detect_order():
        return REGISTRY[cand](cfg)
    raise SystemExit("no agent CLI is installed (claude, copilot or codex)")
