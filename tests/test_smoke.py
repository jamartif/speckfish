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


def test_module_entrypoint_is_importable():
    """`python -m speckfish` is the fallback when the console-script .exe is blocked."""
    import importlib
    m = importlib.import_module("speckfish.__main__")
    assert hasattr(m, "main")


# ---------- doctor output hygiene ----------
BANNER = """
               ███████╗██████╗ ███████╗ ██████╗██╗███████╗██╗   ██╗
               ██╔════╝██╔══██╗██╔════╝██╔════╝██║██╔════╝╚██╗ ██╔╝
               ╚════██║██╔═══╝ ██╔══╝  ██║     ██║██╔══╝    ╚██╔╝
     CLI Version    1.3.2
          Python    3.12.0
        Platform    Windows
"""


def test_parse_specify_version_extracts_numbers():
    from speckfish.steps import parse_specify_version
    got = parse_specify_version(BANNER)
    assert "1.3.2" in got and "3.12.0" in got
    assert "█" not in got


def test_banner_free_tail_drops_ascii_art():
    from speckfish.steps import banner_free_tail
    lines = banner_free_tail(BANNER, keep=2)
    assert all("█" not in l and "╗" not in l for l in lines)
    assert lines[-1].startswith("Platform") or "3.12.0" in lines[-1]


def test_doctor_summary_is_readable():
    from speckfish.cli import doctor_summary
    info = {
        "root": "C:/x",
        "harness": {"harness": "claude", "version": "2.1.323", "verified": True,
                    "installed": True},
        "specify": {"ok": True, "version": "specify-cli 1.3.2 · Python 3.12.0",
                    "argv": ["C:/bin/specify.EXE"], "detail": []},
        "egm": {"present": False, "path": None},
        "project": {"has_git": True, "has_specify": True, "has_claude_skills": False,
                    "feature": None},
        "tools": {"git": {"installed": True}, "uv": {"installed": True}},
    }
    out = doctor_summary(info)
    assert "cli.Version" not in out
    assert "specify-cli 1.3.2" in out
    assert "not found" in out
    assert "{" not in out                       # no JSON dumps


# ---------- multi-integration init ----------
def test_resolve_integrations_modes():
    from speckfish.steps import resolve_integrations
    assert resolve_integrations("all") == ["claude", "copilot", "codex"]
    assert resolve_integrations("copilot,claude") == ["copilot", "claude"]
    auto = resolve_integrations(None)          # only what is installed here
    assert auto and all(x in ("claude", "copilot", "codex") for x in auto)


def test_resolve_integrations_rejects_unknown():
    from speckfish.steps import resolve_integrations
    with pytest.raises(SystemExit):
        resolve_integrations("claude,gemini")


# ---------- agent / model reporting ----------
def test_claude_reports_dominant_model():
    import json as _json
    cfg = Config(root=Path("."))
    payload = _json.dumps({
        "result": "ok", "total_cost_usd": 0.97, "num_turns": 4,
        "modelUsage": {
            "claude-haiku-4": {"inputTokens": 10, "outputTokens": 100},
            "claude-sonnet-5-5": {"inputTokens": 10, "outputTokens": 9000},
        },
    })
    out = ClaudeHarness(cfg).parse_output(payload, "", 0)
    assert out["model"] == "claude-sonnet-5-5"          # the one that did the work


def test_copilot_reports_dominant_model():
    cfg = Config(root=Path("."))
    lines = [
        '{"type":"model.call_final_result","data":{"model":"gpt-6-luna"}}',
        '{"type":"model.call_final_result","data":{"model":"gpt-6-luna"}}',
        '{"type":"model.call_final_result","data":{"model":"mai-code-1.1-flash"}}',
        '{"type":"subagent.started","data":{}}',
        '{"type":"session.usage_checkpoint","data":{"totalNanoAiu":630000000}}',
    ]
    out = CopilotHarness(cfg).parse_output("\n".join(lines), "", 0)
    assert out["model"] == "gpt-6-luna"
    assert out["credits"] == 0.63
    assert out["subagents"] == 1


def test_model_flag_reaches_every_harness(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.model = "claude-sonnet-5-5"
    for H in (ClaudeHarness, CopilotHarness):
        inv = H(cfg).build("review", "p", tmp_path, tmp_path / "a", tmp_path / "e",
                           budget=None, timeout=60, schema=None)
        assert "--model" in inv.argv and "claude-sonnet-5-5" in inv.argv
    inv = CodexHarness(cfg).build("review", "p", tmp_path, tmp_path / "a", tmp_path / "e",
                                  None, 60, None)
    assert "--model" in inv.argv


def test_summary_shows_model():
    from speckfish.cli import summarize
    out = summarize({"step": "review", "ok": True, "exit_code": 0, "duration_s": 1.0,
                     "model": "gpt-6-luna"})
    assert "model: gpt-6-luna" in out


# ---------- pipeline composition ----------
def test_pipeline_dry_run_lists_all_steps(tmp_path, monkeypatch):
    from speckfish import steps as S
    egm = tmp_path / "egm"
    (egm / "skills" / "egm-review").mkdir(parents=True)
    (egm / "plugin.json").write_text('{"version": "0.0.0"}', encoding="utf-8")
    cfg = Config(root=tmp_path)
    cfg.egm_root = str(egm)
    (tmp_path / "specs" / "001-x").mkdir(parents=True)
    (tmp_path / "specs" / "001-x" / "spec.md").write_text("# s", encoding="utf-8")
    (tmp_path / "specs" / "001-x" / "plan.md").write_text("# p", encoding="utf-8")
    out = S.do_pipeline(cfg, "idea", "stack", None, gate=False, with_drift=True,
                        stream=False, dry_run=True)
    assert list(out["steps"]) == ["specify", "plan", "review", "drift"]
    assert out["steps"]["drift"]["error"] == "dry-run"


def test_pipeline_gate_skips_drift_when_stopped(tmp_path, monkeypatch):
    from speckfish import steps as S
    cfg = Config(root=tmp_path)

    class Boom:
        name = "claude"

    def failing(*a, **k):
        from speckfish.runner import StepResult
        return StepResult(step="plan", ok=False, exit_code=2, duration_s=0.0, error="boom")

    monkeypatch.setattr(S, "do_specify", failing)
    monkeypatch.setattr(S, "get_harness", lambda cfg=None, name=None: Boom())
    out = S.do_pipeline(cfg, "idea", "stack", None, gate=True, with_drift=True,
                        stream=False, dry_run=True)
    assert out.get("stopped_at") == "specify"
    assert "drift" not in out["steps"]
    assert "gate stopped at" in out.get("drift_skipped", "")
