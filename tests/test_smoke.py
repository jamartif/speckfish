"""Smoke tests: no network, no agent CLI is executed."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from speckfish import prompts
from speckfish.cli import build_parser, normalize_argv, exit_for
from speckfish.config import Config, parse_consolidated, find_report
from speckfish.harnesses import ClaudeHarness, CopilotHarness, CodexHarness
from speckfish.runner import run_interactive, run_step

FIX = Path(__file__).parent / "fixtures" / "consolidated_review_report.md"


# ---------- CLI ----------
def test_normalize_moves_global_flags_to_the_front():
    assert normalize_argv(["review", "--root", "../demo", "--json"]) == \
        ["--root", "../demo", "--json", "review"]
    assert normalize_argv(["--root", "x", "doctor"]) == ["--root", "x", "doctor"]


def test_root_accepted_before_and_after_the_subcommand():
    for argv in (["--root", "../demo", "review"], ["review", "--root", "../demo"]):
        args = build_parser().parse_args(normalize_argv(argv))
        assert args.root == "../demo"
        assert args.cmd == "review"


def test_exit_codes():
    assert exit_for({"ok": True, "step": "plan"}) == 0
    assert exit_for({"ok": True, "step": "review", "blockers": ["x"]}) == 1
    assert exit_for({"ok": True, "step": "review", "meets_standard": False}) == 0  # no blockers
    assert exit_for({"ok": False, "step": "review"}) == 2


# ---------- prompts ----------
def test_review_prompt_includes_stage_profile_and_artifact():
    p = prompts.review_prompt("specs/001-x/plan.md", Path("C:/tmp/run/artifacts/review"),
                              profile="speckit")
    assert "Step=uninterrupted" in p
    assert "EGM artifact directory:" in p
    assert "Alternatives Considered" in p  # the profile removes the Spec Kit format noise
    assert "do not ask" in p.lower()


def test_review_prompt_profile_none_has_no_criteria():
    p = prompts.review_prompt("doc.md", Path("/tmp"), profile="none")
    assert "REVIEW CRITERIA" not in p


def test_copilot_prompt_does_not_use_slash_command():
    p = prompts.egm_prompt_for("copilot", "review", "specs/x/plan.md",
                               Path("/tmp/art"), Path("/egm"))
    assert "/egm:egm-review" not in p
    assert "EGM package root:" in p and "docs/copilot.md" in p


# ---------- report parsing ----------
def test_parse_real_consolidated_report():
    out = parse_consolidated(FIX)
    assert out["meets_standard"] is False
    assert len(out["blockers"]) == 2
    assert "alternatives" in out["blockers"][0].lower()
    assert out["majors"] >= 5
    assert out["nits"] >= 3


def test_find_report_by_pattern(tmp_path):
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
    assert "--max-budget-usd" in a          # per-step budget
    assert "acceptEdits" in a
    assert "Agent" in a                      # without Agent there are no Goldfish subagents


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


def test_dry_run_does_not_execute(tmp_path):
    cfg = _cfg(tmp_path)
    res = run_step(cfg, ClaudeHarness(cfg), "review", "prompt", tmp_path, "run1",
                   tmp_path / "egm", dry_run=True)
    assert res.ok and res.error == "dry-run"
    assert "-p" in res.log
    assert (tmp_path / ".speckfish" / "runs" / "run1" / "artifacts" / "review").is_dir()


def test_agent_prefix_avoids_windows_shims():
    from speckfish.config import agent_prefix, is_windows
    if not is_windows():
        pytest.skip("Windows only")
    pre = agent_prefix("copilot")
    if not pre:
        pytest.skip("copilot not installed")
    assert pre[0].lower().endswith("node.exe") or pre[0].lower().endswith("node")
    assert pre[1].endswith(".js")
    assert ".cmd" not in pre[1]


# ---------- interactive mode ----------
def test_claude_interactive_argv_has_no_print_flag(tmp_path):
    cfg = _cfg(tmp_path)
    inv = ClaudeHarness(cfg).build_interactive("review", "/egm:egm-review doc.md", tmp_path,
                                               tmp_path / "art", tmp_path / "egm")
    assert inv.interactive is True
    assert "-p" not in inv.argv                      # no print mode: the dialogue stays alive
    assert "--json-schema" not in inv.argv
    assert "--plugin-dir" in inv.argv
    assert inv.argv[1].startswith("/egm:egm-review")


def test_copilot_interactive_argv_has_no_prompt_flag(tmp_path):
    cfg = _cfg(tmp_path)
    inv = CopilotHarness(cfg).build_interactive("review", "EGM package root: /egm", tmp_path,
                                                tmp_path / "art", tmp_path / "egm")
    assert "-p" not in inv.argv
    assert "--agent" in inv.argv and "egm:egm" in inv.argv
    assert "--allow-all-tools" in inv.argv


def test_interactive_review_prompt_asks_questions():
    p = prompts.review_prompt("doc.md", Path("/tmp/art"), interactive=True)
    assert "ask every open question" in p.lower()
    assert "Step=" not in p                          # let EGM show its preset menu
    assert "nobody can answer questions" not in p.lower()


def test_headless_review_prompt_forbids_questions():
    p = prompts.review_prompt("doc.md", Path("/tmp/art"))
    assert "nobody can answer questions" in p.lower()
    assert "Step=uninterrupted" in p


def test_interactive_dry_run_shows_command(tmp_path):
    cfg = _cfg(tmp_path)
    res = run_interactive(cfg, ClaudeHarness(cfg), "review", "/egm:egm-review x.md", tmp_path,
                          "run-int", tmp_path / "egm", dry_run=True)
    assert res.interactive is True
    assert "/egm:egm-review x.md" in res.log
