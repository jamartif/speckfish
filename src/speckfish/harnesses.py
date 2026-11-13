"""Adaptadores de harness: claude, copilot, codex.

Cada adaptador sabe:
  - construir la línea de comandos headless,
  - declarar si soporta esquema de salida estructurada,
  - cómo se llama la integración de Spec Kit,
  - qué caducidad/limitaciones tiene.
"""
from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from .config import Config, norm, run_cmd


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
        """(integración, opciones extra para `specify init`)."""
        raise NotImplementedError

    # ---- ejecución de un paso ----
    def build(self, kind: str, prompt: str, cwd: Path, artifact_dir: Path,
              egm_root: Path | None, budget: float | None,
              timeout: int, schema: dict | None) -> StepInvocation:
        raise NotImplementedError

    # ---- parseo de la salida ----
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

    def spec_kit_integration(self):
        return "claude", []

    def build(self, kind, prompt, cwd, artifact_dir, egm_root, budget, timeout, schema):
        b = self.binary() or "claude"
        argv = [b, "-p", prompt,
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
        }
        if not res["cost_usd"]:
            res["raw_tail"] = (stdout or stderr)[-1500:]
        return res


class CopilotHarness(Harness):
    name = "copilot"
    supports_schema = False
    supports_budget = True
    verified = True
    notes = ("Verificado en Windows: -p headless, --plugin-dir registra el plugin egm, "
             "--output-format json (JSONL), --max-ai-credits. Sin --json-schema: el veredicto "
             "se parsea del informe Markdown.")

    def binary(self):
        return shutil.which("copilot")

    def spec_kit_integration(self):
        return "copilot", []

    def build(self, kind, prompt, cwd, artifact_dir, egm_root, budget, timeout, schema):
        b = self.binary() or "copilot"
        argv = [b, "-p", prompt, "--output-format", "json",
                "--allow-all-tools", "--allow-all-paths"]
        if egm_root:
            argv += ["--plugin-dir", norm(egm_root), "--add-dir", norm(egm_root),
                     "--agent", "egm:egm"]
        credits = self.cfg.budgets.get("copilot", {}).get(kind)
        if credits:
            argv += ["--max-ai-credits", str(int(credits))]
        if self.cfg.model:
            argv += ["--model", self.cfg.model]
        return StepInvocation(argv=argv, schema_supported=False)

    def parse_output(self, stdout, stderr, exit_code):
        text_parts, credits, cost = [], None, None
        for line in (stdout or "").splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue
            for key in ("message", "text", "content", "output_text", "delta"):
                v = ev.get(key) if isinstance(ev, dict) else None
                if isinstance(v, str):
                    text_parts.append(v)
            data = ev.get("data") if isinstance(ev, dict) else None
            if isinstance(data, dict):
                for key in ("ai_credits", "credits", "total_ai_credits", "cost"):
                    if key in data:
                        credits = data[key]
        return {
            "credits": credits,
            "text_tail": ("\n".join(text_parts))[-3000:] or (stderr or "")[-1500:],
            "exit_code": exit_code,
        }


class CodexHarness(Harness):
    name = "codex"
    supports_schema = False
    supports_budget = False
    verified = False
    notes = ("Adaptador no verificado: codex CLI no está instalado en la máquina de pruebas. "
             "Usa `codex exec` + skills del plugin; revisa docs/codex.md de EGM.")

    def binary(self):
        return shutil.which("codex")

    def spec_kit_integration(self):
        return "codex", ["--integration-options=--skills"]

    def build(self, kind, prompt, cwd, artifact_dir, egm_root, budget, timeout, schema):
        b = self.binary() or "codex"
        argv = [b, "exec", "--skip-git-repo-check", prompt]
        if egm_root:
            argv += ["--add-dir", norm(egm_root)]
        return StepInvocation(argv=argv, schema_supported=False)

    def parse_output(self, stdout, stderr, exit_code):
        return {"text_tail": (stdout or stderr)[-3000:], "exit_code": exit_code}


REGISTRY = {"claude": ClaudeHarness, "copilot": CopilotHarness, "codex": CodexHarness}


def detect_order() -> list[str]:
    """Orden de preferencia cuando harness=auto: instalado y verificado primero."""
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
            raise SystemExit(f"harness desconocido: {want} (usa claude|copilot|codex)")
        h = REGISTRY[want](cfg)
        if not h.binary():
            raise SystemExit(f"harness '{want}' no está instalado (binario no encontrado en PATH)")
        return h
    for cand in detect_order():
        return REGISTRY[cand](cfg)
    raise SystemExit("no hay ningún CLI de agente instalado (claude, copilot o codex)")
