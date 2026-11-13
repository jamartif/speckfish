"""Tests de humo: sin red, sin invocar CLIs de agente."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from speckfish import prompts
from speckfish.cli import build_parser, normalize_argv, exit_for
from speckfish.config import Config, parse_consolidated, find_report
from speckfish.harnesses import ClaudeHarness, CopilotHarness, CodexHarness
from speckfish.runner import run_step

FIX = Path(__file__).parent / "fixtures" / "consolidated_review_report.md"


# ---------- CLI ----------
def test_normalize_mueve_flags_globales_al_frente():
    assert normalize_argv(["review", "--root", "../demo", "--json"]) == \
        ["--root", "../demo", "--json", "review"]
    assert normalize_argv(["--root", "x", "doctor"]) == ["--root", "x", "doctor"]


def test_root_aceptado_antes_y_despues_del_subcomando():
    for argv in (["--root", "../demo", "review"], ["review", "--root", "../demo"]):
        args = build_parser().parse_args(normalize_argv(argv))
        assert args.root == "../demo"
        assert args.cmd == "review"


def test_exit_codes():
    assert exit_for({"ok": True, "step": "plan"}) == 0
    assert exit_for({"ok": True, "step": "review", "blockers": ["x"]}) == 1
    assert exit_for({"ok": True, "step": "review", "meets_standard": False}) == 0  # sin blockers
    assert exit_for({"ok": False, "step": "review"}) == 2


# ---------- prompts ----------
def test_review_prompt_incluye_stage_perfil_y_artifact():
    p = prompts.review_prompt("specs/001-x/plan.md", Path("C:/tmp/run/artifacts/review"),
                              profile="speckit")
    assert "Step=uninterrupted" in p
    assert "EGM artifact directory:" in p
    assert "Alternatives Considered" in p  # el perfil evita el ruido de formato
    assert "no hagas" in p.lower() or "no pidas" in p.lower()


def test_review_prompt_perfil_none_no_incluye_criterios():
    p = prompts.review_prompt("doc.md", Path("/tmp"), profile="none")
    assert "CRITERIOS DE REVISIÓN" not in p


def test_prompt_copilot_no_usa_slash_command():
    p = prompts.egm_prompt_for("copilot", "review", "specs/x/plan.md",
                               Path("/tmp/art"), Path("/egm"))
    assert "/egm:egm-review" not in p
    assert "EGM package root:" in p and "docs/copilot.md" in p


# ---------- parsing de informes ----------
def test_parse_consolidado_real():
    out = parse_consolidated(FIX)
    assert out["meets_standard"] is False
    assert len(out["blockers"]) == 2
    assert "alternatives" in out["blockers"][0].lower()
    assert out["majors"] >= 5
    assert out["nits"] >= 3


def test_find_report_por_patron(tmp_path):
    (tmp_path / "consolidated_review_report.md").write_text("# x", encoding="utf-8")
    assert find_report(tmp_path).name == "consolidated_review_report.md"
    assert find_report(tmp_path, "*DRIFT_REPORT.md") is None


# ---------- harnesses ----------
def _cfg(tmp_path: Path) -> Config:
    cfg = Config(root=tmp_path)
    cfg.egm_root = str(tmp_path / "egm")
    return cfg


def test_claude_argv(tmp_path):
    cfg = _cfg(tmp_path)
    h = ClaudeHarness(cfg)
    inv = h.build("review", "prompt", tmp_path, tmp_path / "art", tmp_path / "egm",
                  budget=None, timeout=60, schema=prompts.REVIEW_SCHEMA)
    a = inv.argv
    assert "-p" in a and "--json-schema" in a and "--plugin-dir" in a
    assert "--max-budget-usd" in a          # presupuesto por paso
    assert "acceptEdits" in a
    assert "Agent" in a                      # sin Agent no hay subagentes Goldfish


def test_copilot_argv(tmp_path):
    cfg = _cfg(tmp_path)
    h = CopilotHarness(cfg)
    inv = h.build("review", "prompt", tmp_path, tmp_path / "art", tmp_path / "egm",
                  budget=None, timeout=60, schema=None)
    a = inv.argv
    assert "-p" in a and "--agent" in a and "--allow-all-tools" in a
    assert "--max-ai-credits" in a and "--output-format" in a
    assert not inv.schema_supported


def test_codex_argv(tmp_path):
    cfg = _cfg(tmp_path)
    inv = CodexHarness(cfg).build("review", "p", tmp_path, tmp_path, None,
                                  None, 60, None)
    assert inv.argv[1] == "exec"


def test_dry_run_no_ejecuta(tmp_path):
    cfg = _cfg(tmp_path)
    res = run_step(cfg, ClaudeHarness(cfg), "review", "prompt", tmp_path, "run1",
                   tmp_path / "egm", dry_run=True)
    assert res.ok and res.error == "dry-run"
    assert "-p" in res.log
    assert (tmp_path / ".speckfish" / "runs" / "run1" / "artifacts" / "review").is_dir()


def test_agent_prefix_no_usa_shims_de_windows():
    from speckfish.config import agent_prefix, is_windows
    if not is_windows():
        pytest.skip("solo Windows")
    pre = agent_prefix("copilot")
    if not pre:
        pytest.skip("copilot no instalado")
    assert pre[0].lower().endswith("node.exe") or pre[0].lower().endswith("node")
    assert pre[1].endswith(".js")
    assert ".cmd" not in pre[1]
