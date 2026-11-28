"""Harness adapters: claude, copilot, codex.

Cada adaptador sabe:
  - build the headless command line,
  - declare whether it supports a structured output schema,
  - report the Spec Kit integration name,
  - state its limitations.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from .config import Config, agent_prefix, norm, run_cmd
from .registry import tools_profile


@dataclass
class StepInvocation:
    argv: list[str]
    env: dict | None = None
    stdin: str | None = None
    schema_supported: bool = False
    cost_unit: str = "usd"
    interactive: bool = False


class Harness:
    name = "base"
    echo_stdout = False        # True only for harnesses whose stdout is human readable
    slash_commands = False     # True when the CLI resolves `/speckit-*` and `/egm:*` in a prompt
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
            # `prefix()` knows how to launch npm shims on Windows; a bare path gives WinError 193
            argv = (self.prefix() or [b]) + ["--version"]
            try:
                code, out, err = run_cmd(argv, timeout=60)
            except Exception as e:      # doctor must report, never raise
                code, out, err = 1, "", str(e)
            text = (out or err).strip()
            info["version"] = text.splitlines()[0] if text else ""
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

    # ---- interactive (no -p: the human drives the dialogue) ----
    def build_interactive(self, kind: str, prompt: str, cwd: Path,
                          artifact_dir: Path, egm_root: Path | None) -> StepInvocation:
        raise NotImplementedError

    # ---- what the agent is allowed to do ----
    def permissions(self, kind: str) -> str:
        """One line a person can read before approving a step: what this agent gets for this kind of step."""
        return "the agent's default permissions"

    # ---- Spec Kit's skills: installed per agent, so a project set up for one has none for another ----
    skill_dirs: ClassVar[tuple[str, ...]] = ()       # where this agent's skills or commands live

    def has_skill(self, root: Path, skill: str) -> bool:
        """Is `skill` (e.g. speckit-implement) installed for this agent in the project?"""
        dotted = skill.replace("-", ".", 1)           # older layouts: speckit.implement.md
        for folder in self.skill_dirs:
            base = Path(root) / folder
            if (base / skill / "SKILL.md").is_file():
                return True
            if base.is_dir() and any(p.is_file() for pat in (f"{skill}*", f"{dotted}*") for p in base.glob(pat)):
                return True
        return False

    # ---- models ----
    def known_models(self) -> list[tuple[str, str]]:
        """[(name, where it comes from)] for `--model`. Only what this CLI lets us find out: no guessing.

        None of the three has a command that lists its models, so each adapter reads what it does expose; an
        empty list means "type the name yourself".
        """
        return []

    # ---- output parsing ----
    def parse_output(self, stdout: str, stderr: str, exit_code: int) -> dict:
        return {"raw_tail": (stdout or stderr or "")[-1500:]}


class ClaudeHarness(Harness):
    name = "claude"
    skill_dirs = (".claude/skills", ".claude/commands")
    slash_commands = True
    supports_schema = True
    supports_budget = True
    verified = True
    notes = ("Verified on Windows: /egm:* slash commands, --plugin-dir, --json-schema, "
             "--max-budget-usd, --output-format stream-json.")

    def binary(self):
        return shutil.which("claude")

    def prefix(self):
        return agent_prefix("claude")

    def spec_kit_integration(self):
        return "claude", []

    # the aliases `claude --help` documents for --model; they follow the newest model of each family
    MODEL_ALIASES: ClassVar[list[tuple[str, str]]] = [
        ("fable", "alias: the latest Fable"), ("opus", "alias: the latest Opus"),
        ("sonnet", "alias: the latest Sonnet")]

    def known_models(self) -> list[tuple[str, str]]:
        return list(self.MODEL_ALIASES)

    ALL_TOOLS: ClassVar[list[str]] = ["Bash", "PowerShell", "Write", "Edit", "Read", "Glob", "Grep", "Agent"]
    # tools removed (not merely "not pre-approved"): checked against the real CLI, a denied Bash is not
    # even loaded for the agent, while an allow-list only auto-approves and leaves other rules in force
    DENIED: ClassVar[dict[str, list[str]]] = {"readonly": ["Bash", "PowerShell"]}

    def tool_rules(self, kind: str) -> tuple[list[str], list[str]]:
        """(allowed, denied) tool names for a kind of step, per the `permissions` setting."""
        profile = "project" if self.cfg.permissions == "open" else tools_profile(kind)
        denied = list(self.DENIED.get(profile, []))
        return [t for t in self.ALL_TOOLS if t not in denied], denied

    def permissions(self, kind: str) -> str:
        allowed, denied = self.tool_rules(kind)
        text = "tools: " + ", ".join(allowed)
        return text + (f" (no {', '.join(denied)})" if denied else " (includes a shell: it can run any command)")

    def build(self, kind, prompt, cwd, artifact_dir, egm_root, budget, timeout, schema):
        allowed, denied = self.tool_rules(kind)
        # the prompt goes on stdin, not in argv: argv is visible to every user in `ps` and Windows caps it
        argv = (self.prefix() or ["claude"]) + ["-p",
                "--permission-mode", "acceptEdits", "--allowedTools", *allowed]
        if denied:
            argv += ["--disallowedTools", *denied]
        argv += ["--output-format", "stream-json", "--verbose"]
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
        return StepInvocation(argv=argv, stdin=prompt, schema_supported=bool(schema))

    def build_interactive(self, kind, prompt, cwd, artifact_dir, egm_root):
        argv = (self.prefix() or ["claude"]) + [
            prompt,
            "--permission-mode", "acceptEdits",
            "--allowedTools", "Bash", "PowerShell", "Write", "Edit", "Read", "Glob", "Grep",
            "Agent",
        ]
        if egm_root:
            argv += ["--plugin-dir", norm(egm_root)]
        if self.cfg.model:
            argv += ["--model", self.cfg.model]
        return StepInvocation(argv=argv, interactive=True)

    def parse_output(self, stdout, stderr, exit_code):
        data = {}
        with contextlib.suppress(Exception):
            data = json.loads(stdout[stdout.find("{"):stdout.rfind("}") + 1])
        if not data:
            # stream-json: one event per line, the last `result` holds the whole payload
            for line in (stdout or "").splitlines():
                line = line.strip()
                if not (line.startswith("{") and line.endswith("}")):
                    continue
                with contextlib.suppress(ValueError):
                    event = json.loads(line)
                    if isinstance(event, dict) and event.get("type") == "result":
                        data = event
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
            "model": _dominant_model(data.get("modelUsage") or {}),
        }
        if not res["cost_usd"]:
            res["raw_tail"] = (stdout or stderr)[-1500:]
        return res


class CopilotHarness(Harness):
    name = "copilot"
    skill_dirs = (".github/skills", ".github/prompts", ".github/agents")
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

    def permissions(self, kind: str) -> str:
        return "all tools and all paths (--allow-all-tools --allow-all-paths); this CLI has no per-step scoping here"

    def known_models(self) -> list[tuple[str, str]]:
        """The `choices` its own `--help` prints for --model, when it prints them."""
        argv = (self.prefix() or []) + ["--help"]
        if len(argv) < 2:
            return []
        try:
            _code, out, err = run_cmd(argv, timeout=30)
        except Exception:
            return []
        text = out or err
        m = re.search(r"--model\s+<[^>]*>(.{0,600}?)\(choices:\s*([^)]*)\)", text, re.S)
        if not m:
            return []
        names = [n.strip().strip("\"'") for n in m.group(2).replace("\n", " ").split(",")]
        return [(n, "listed by `copilot --help`") for n in names if n]

    def build(self, kind, prompt, cwd, artifact_dir, egm_root, budget, timeout, schema):
        argv = (self.prefix() or ["copilot"]) + ["-p", prompt, "--output-format", "json",
                "--allow-all-tools", "--allow-all-paths"]
        if egm_root:
            argv += ["--plugin-dir", norm(egm_root), "--add-dir", norm(egm_root),
                     "--agent", "egm:egm"]
        credits = budget if isinstance(budget, (int, float)) and budget else \
            self.cfg.budgets.get("copilot", {}).get(kind)
        if credits:
            # the Copilot CLI rejects anything below 30 credits
            argv += ["--max-ai-credits", str(max(30, int(credits)))]
        if self.cfg.model:
            argv += ["--model", self.cfg.model]
        return StepInvocation(argv=argv, schema_supported=False)

    def build_interactive(self, kind, prompt, cwd, artifact_dir, egm_root):
        argv = (self.prefix() or ["copilot"]) + ["--allow-all-tools", "--allow-all-paths"]
        if egm_root:
            argv += ["--plugin-dir", norm(egm_root), "--add-dir", norm(egm_root),
                     "--agent", "egm:egm"]
        if self.cfg.model:
            argv += ["--model", self.cfg.model]
        argv += [prompt]
        return StepInvocation(argv=argv, interactive=True)

    def parse_output(self, stdout, stderr, exit_code):
        """Copilot JSONL: message deltas, credits and subagents."""
        by_msg: dict[str, list[str]] = {}
        order: list[str] = []
        models: dict[str, int] = {}
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
            except ValueError:
                continue                 # not an event: copilot interleaves plain log lines
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
            elif (t == "model.call_final_result" and isinstance(data.get("model"), str)) or (t == "session.model_change" and isinstance(data.get("model"), str)):
                models[data["model"]] = models.get(data["model"], 0) + 1
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
            "model": _dominant_model(models),
            "session_ms": session_ms,
            "text_tail": text[-3000:] or (stderr or "")[-1500:],
            "exit_code": exit_code,
        }


CODEX_FILE_HINT = (
    "Writing files: there is no apply_patch command in PATH, so do not emit \"*** Begin Patch\" "
    "blocks. Write with PowerShell, forcing UTF-8 or the accents get mangled: "
    "[System.IO.File]::WriteAllText('<path>', $content, [System.Text.UTF8Encoding]::new($false)). "
    "Read with [System.IO.File]::ReadAllText('<path>', [System.Text.Encoding]::UTF8) or "
    "Get-Content -Raw -Encoding utf8. This machine's PowerShell 5.1 reports output encoding ibm850, so "
    "a plain Get-Content shows 'CatÃ¡logo' for a file that is correct on disk: that is the console, "
    "never rewrite a file to fix it, and do not spend turns grepping for mojibake. "
    "Do not install packages on this machine (no pip install of validators or tools): if a validator "
    "is missing, say so and validate by inspection. This does not apply to the project's own "
    "dependencies: when a task needs a NuGet, npm or pip dependency, add it to the project and "
    "restore it."
)


class CodexHarness(Harness):
    unsafe = True              # runs with the sandbox and the approvals off: needs explicit consent
    skill_dirs = (".agents/skills",)
    name = "codex"
    echo_stdout = False        # `codex exec --json` prints NDJSON: translated into activity lines
    supports_schema = False
    supports_budget = False
    verified = True
    notes = ("Verified on Windows with an OpenAI-compatible provider set in ~/.codex/config.toml. "
             "Runs `codex exec --skip-git-repo-check --dangerously-bypass-approvals-and-sandbox`: "
             "0.169 ignores --sandbox workspace-write in exec, so without the bypass it answers in "
             "prose and writes nothing. No sandbox and no approval prompts = full filesystem.")

    def binary(self):
        return shutil.which("codex")

    def prefix(self):
        return agent_prefix("codex")

    def spec_kit_integration(self):
        return "codex", ["--integration-options=--skills"]

    def permissions(self, kind: str) -> str:
        return "NO sandbox and NO approvals: it can read and write the whole filesystem and run anything"

    def known_models(self) -> list[tuple[str, str]]:
        """The model set in `~/.codex/config.toml` (codex has no command that lists them)."""
        home = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
        try:
            text = (home / "config.toml").read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        found = re.findall(r'^\s*model\s*=\s*"([^"]+)"', text, re.M)
        return [(n, "set in your Codex config") for n in dict.fromkeys(found)]

    def build(self, kind, prompt, cwd, artifact_dir, egm_root, budget, timeout, schema):
        # Measured on codex-cli 0.169: `--sandbox workspace-write` and
        # `-c sandbox_mode=workspace-write` are both ignored (`sandbox: read-only` in the banner) so
        # the agent answers in prose and never writes spec.md. With approvals + sandbox bypassed it
        # writes. Headless means nobody can approve, so this adapter declares the filesystem open.
        # --json: NDJSON events as they happen (that is what the progress line feeds on) instead of
        # one silent stream until the end
        prompt = prompt + "\n\n" + CODEX_FILE_HINT
        argv = (self.prefix() or ["codex"]) + ["exec", "--json", "--skip-git-repo-check",
                                               "--dangerously-bypass-approvals-and-sandbox", prompt]
        if egm_root:
            argv += ["--add-dir", norm(egm_root)]
        if self.cfg.model:
            argv += ["--model", self.cfg.model]
        return StepInvocation(argv=argv, schema_supported=False)

    def build_interactive(self, kind, prompt, cwd, artifact_dir, egm_root):
        argv = (self.prefix() or ["codex"]) + ["--dangerously-bypass-approvals-and-sandbox"]
        if egm_root:
            argv += ["--add-dir", norm(egm_root)]
        if self.cfg.model:
            argv += ["--model", self.cfg.model]
        argv += [prompt]
        return StepInvocation(argv=argv, interactive=True)

    def parse_output(self, stdout, stderr, exit_code):
        """codex --json: keep the last message, the token usage and any fatal error."""
        text, tokens, error = "", 0, ""
        for line in (stdout or "").splitlines():
            line = line.strip()
            if not (line.startswith("{") and line.endswith("}")):
                continue
            with contextlib.suppress(ValueError):
                event = json.loads(line)
                if not isinstance(event, dict):
                    continue
                etype = event.get("type")
                if etype in ("item.started", "item.completed"):
                    item = event.get("item") or {}
                    if item.get("type") == "agent_message" and item.get("text"):
                        text = str(item["text"])
                    elif item.get("type") == "error" and item.get("message"):
                        error = str(item["message"])
                elif etype == "turn.completed":
                    tokens = int((event.get("usage") or {}).get("output_tokens") or 0)
        return {"text_tail": text[-3000:] or (stdout or "")[-1500:], "exit_code": exit_code,
                "tokens": tokens, "error": error}


REGISTRY = {"claude": ClaudeHarness, "copilot": CopilotHarness, "codex": CodexHarness}


def _dominant_model(usage: dict) -> str | None:
    """Name of the model that did most work, from Claude `modelUsage` or a Copilot tally."""
    if not isinstance(usage, dict) or not usage:
        return None
    best, best_weight = None, -1
    for name, info in usage.items():
        if isinstance(info, dict):
            weight = (info.get("outputTokens") or 0) + (info.get("output_tokens") or 0)
        else:
            weight = info if isinstance(info, (int, float)) else 0
        if weight > best_weight:
            best, best_weight = name, weight
    return best


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


UNSAFE_HELP = ("`{name}` runs with its sandbox and approvals switched off, so it can read and write anything "
               "on this machine. Say you accept that: `speckfish config unsafe=true`, or `--unsafe` for one run "
               "(or SPECKFISH_UNSAFE=1 in CI, inside a disposable container).")


def _checked(cfg: Config, h: Harness) -> Harness:
    if getattr(h, "unsafe", False) and not cfg.unsafe:
        raise SystemExit(UNSAFE_HELP.format(name=h.name))
    return h


def get_harness(cfg: Config, name: str | None = None) -> Harness:
    want = name or cfg.harness or "auto"
    if want != "auto":
        if want not in REGISTRY:
            raise SystemExit(f"unknown harness: {want} (use claude|copilot|codex)")
        h = REGISTRY[want](cfg)
        if not h.binary():
            raise SystemExit(f"harness '{want}' is not installed (binary not found on PATH)")
        return _checked(cfg, h)
    for cand in detect_order():
        return _checked(cfg, REGISTRY[cand](cfg))
    raise SystemExit("no agent CLI is installed (claude, copilot or codex)")
