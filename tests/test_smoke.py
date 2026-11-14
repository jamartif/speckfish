"""Smoke tests: no network, no agent CLI is executed."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from speckfish import prompts
from speckfish.cli import build_parser, exit_for, normalize_argv
from speckfish.config import Config, find_report, parse_consolidated
from speckfish.harnesses import ClaudeHarness, CodexHarness, CopilotHarness
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
    # Deliberately a REAL Spanish EGM report: the parser has to cope with both languages.
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
    assert "exec" in inv.argv
    assert inv.argv[inv.argv.index("exec") + 1] == "--skip-git-repo-check"
    # 0.169 ignores --sandbox workspace-write in exec: only the bypass actually writes files
    assert "--dangerously-bypass-approvals-and-sandbox" in inv.argv


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
    assert all("█" not in line and "╗" not in line for line in lines)
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
    assert "gpt-6-luna" in out and "OK" in out


def test_summary_is_readable(tmp_path):
    """The console must show what was found, not a wall of paths."""
    from speckfish.cli import summarize
    from speckfish.config import norm
    root = tmp_path
    art = root / ".speckfish" / "runs" / "20260101-1" / "artifacts" / "review"
    out = summarize({
        "step": "review", "ok": True, "exit_code": 0, "duration_s": 202.0,
        "cost": 1.2886208000000001, "cost_unit": "usd", "model": "claude-sonnet-5-5",
        "verdict": "Apto con cambios. No hay Blockers. Hay 9 acciones Major consolidadas.",
        "meets_standard": False, "blockers": [], "majors": 9, "nits": 7,
        "reports": [art / "consolidated_review_report.md", art / "zoom_out_report.md"],
        "artifact_dir": art, "log": root / ".speckfish" / "logs" / "review.log",
    }, norm(root))

    assert "$1.29" in out                      # rounded, not 1.2886208000000001
    assert "3m22s" in out                      # 202 s in human form
    assert "does NOT meet the standard" in out and "9 majors" in out
    assert "Apto con cambios" in out           # the verdict is the headline
    assert str(root) not in out                # paths are relative to the project
    assert "consolidated_review_report.md   <- start here" in out
    assert '"verdict"' not in out              # never a JSON blob


# ---------- pipeline composition ----------
def test_pipeline_dry_run_lists_all_steps(tmp_path, monkeypatch):
    from speckfish import steps as S
    # hermetic: CI has no agent CLI installed, so inject the adapter instead of detecting one
    monkeypatch.setattr(S, "get_harness", lambda cfg=None, name=None: ClaudeHarness(cfg))
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


# ---------- editor entry points ----------
def test_tasks_json_structure():
    from speckfish.editors import STEPS, tasks_json
    data = tasks_json("python -m speckfish")
    assert data["version"] == "2.0.0"
    assert len(data["tasks"]) == len(STEPS)
    labels = [t["label"] for t in data["tasks"]]
    assert "spfish: review" in labels and "spfish: review-interactive" in labels
    assert {i["id"] for i in data["inputs"]} >= {"idea", "stack", "principles"}
    for t in data["tasks"]:
        assert t["command"] == "python -m speckfish"
        assert t["type"] == "shell" and t["problemMatcher"] == []


def test_copilot_prompt_frontmatter():
    from speckfish.editors import STEPS, copilot_prompt
    txt = copilot_prompt("review", STEPS["review"], "speckfish")
    assert txt.startswith("---\n")
    assert "name: 'spfish-review'" in txt          # short prefix, does not blur into /speckit-*
    assert "agent: 'agent'" in txt
    assert "speckfish review" in txt


def test_claude_command_only_takes_arguments_when_useful():
    from speckfish.editors import STEPS, claude_command
    with_args = claude_command("review", STEPS["review"], "speckfish")
    no_args = claude_command("doctor", STEPS["doctor"], "speckfish")
    assert "$ARGUMENTS" in with_args
    assert "$ARGUMENTS" not in no_args
    assert "allowed-tools" in with_args


def test_generate_writes_every_target(tmp_path):
    from speckfish.editors import generate
    codex_home = tmp_path / "codexhome"
    cfg = Config(root=tmp_path)
    out = generate(cfg, ["vscode", "copilot", "claude", "codex"], "speckfish",
                   str(codex_home), force=True)
    assert (tmp_path / ".vscode" / "tasks.json").exists()
    assert (tmp_path / ".github" / "prompts" / "spfish-review.prompt.md").exists()
    assert (tmp_path / ".claude" / "commands" / "spfish-review.md").exists()
    assert (codex_home / "prompts" / "spfish-review.md").exists()
    from speckfish.editors import STEPS as EDITOR_STEPS
    assert len(out["written"]) == 1 + 3 * len(EDITOR_STEPS)
    assert out["command"] == "speckfish"

    # --no-force keeps user edits
    (tmp_path / ".vscode" / "tasks.json").write_text("{}", encoding="utf-8")
    out2 = generate(cfg, ["vscode"], "speckfish", None, force=False)
    assert out2["written"] == [] and out2["skipped"]
    assert (tmp_path / ".vscode" / "tasks.json").read_text(encoding="utf-8") == "{}"


def test_custom_prefix_renames_every_command(tmp_path):
    from speckfish.editors import generate, tasks_json
    cfg = Config(root=tmp_path)
    out = generate(cfg, ["vscode", "copilot", "claude", "codex"], "speckfish", None,
                   force=True, prefix="xyz")
    assert out["prefix"] == "xyz"
    assert (tmp_path / ".github" / "prompts" / "xyz-review.prompt.md").exists()
    assert (tmp_path / ".claude" / "commands" / "xyz-drift.md").exists()
    assert not list((tmp_path / ".claude" / "commands").glob("spfish-*"))
    assert tasks_json("speckfish", prefix="xyz")["tasks"][0]["label"].startswith("xyz: ")


def test_generate_subset_of_targets(tmp_path):
    from speckfish.editors import generate
    cfg = Config(root=tmp_path)
    out = generate(cfg, ["claude"], "speckfish", None, force=True)
    assert all(".claude/commands" in p.replace("\\", "/") for p in out["written"])
    assert not (tmp_path / ".vscode").exists()


# ---------- remaining Spec Kit skills ----------
def test_every_speckit_skill_has_a_command():
    from speckfish.prompts import SKILL_SPECS
    assert set(SKILL_SPECS) == {"constitution", "tasks", "analyze", "checklist",
                                "clarify", "implement", "converge"}
    parser = build_parser()
    for skill in SKILL_SPECS:
        ns = parser.parse_args([skill])
        assert ns.cmd == skill


def test_skill_prompt_shapes():
    from speckfish import prompts as P
    con = P.skill_prompt("constitution", "quality first", Path("/tmp/a"))
    assert con.startswith("/speckit-constitution quality first")
    assert "nobody can answer questions" in con.lower()

    auto = P.skill_prompt("constitution", "", Path("/tmp/a"))
    assert "propose a short, concrete constitution" in auto   # no principles given

    cl = P.skill_prompt("clarify", "", Path("/tmp/a"))
    assert "questions.md" in cl and "do NOT ask questions" in cl

    cop = P.skill_prompt_for("copilot", "tasks", "", Path("/tmp/a"), Path("/egm"))
    assert "speckit-tasks" in cop and "/speckit-tasks" not in cop


def test_implement_requires_confirmation(tmp_path):
    from speckfish import cli as C
    (tmp_path / ".speckfish").mkdir()
    (tmp_path / ".speckfish" / "config.json").write_text("{}", encoding="utf-8")
    code = C.main(["--root", str(tmp_path), "--json", "implement"])
    assert code == 3                                   # refused, nothing written
    assert not (tmp_path / ".speckfish" / "runs").exists()


# ---------- markdown input files ----------
def test_read_input_inlines_small_files(tmp_path):
    from speckfish.prompts import read_input
    f = tmp_path / "plantilla.md"
    f.write_text("# Principios\n1. tests primero\n", encoding="utf-8")
    text, src = read_input("@plantilla.md", tmp_path)
    assert "tests primero" in text and "source material" in text
    assert src == f
    assert str(f).replace("\\", "/") in text


def test_read_input_accepts_bare_relative_path(tmp_path):
    from speckfish.prompts import read_input
    (tmp_path / "docs").mkdir()
    f = tmp_path / "docs" / "constitucion.md"
    f.write_text("calidad\n", encoding="utf-8")
    text, src = read_input("docs/constitucion.md", tmp_path)
    assert "calidad" in text and src is not None


def test_read_input_leaves_normal_text_alone():
    from speckfish.prompts import read_input
    text, src = read_input("a JSON task CLI", None)
    assert text == "a JSON task CLI" and src is None


def test_read_input_references_big_files(tmp_path):
    from speckfish.prompts import INLINE_LIMIT, read_input
    f = tmp_path / "grande.md"
    f.write_text("x" * (INLINE_LIMIT + 100), encoding="utf-8")
    text, src = read_input("@grande.md", tmp_path)
    assert "read that file" in text and "x" * 50 not in text


def test_missing_input_file_fails_loudly(tmp_path):
    from speckfish.prompts import read_input
    with pytest.raises(SystemExit):
        read_input("@no-existe.md", tmp_path)


def test_constitution_uses_markdown_template(tmp_path):
    from speckfish.prompts import skill_prompt
    f = tmp_path / "constitution-template.md"
    f.write_text("# Constitution\n- No external dependencies\n", encoding="utf-8")
    p = skill_prompt("constitution", "@constitution-template.md", tmp_path / "art", tmp_path)
    assert "No external dependencies" in p
    assert p.startswith("/speckit-constitution")


# ---------- CLI surface: version, config, runs ----------
def test_version_flag(capsys):
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["--version"])
    assert exc.value.code == 0
    assert "speckfish" in capsys.readouterr().out


def test_config_roundtrip(tmp_path):
    from speckfish import cli as C
    (tmp_path / ".speckfish").mkdir(parents=True)
    assert C.main(["--root", str(tmp_path), "config", "profile=none"]) == 0
    stored = (tmp_path / ".speckfish" / "config.json").read_text(encoding="utf-8")
    assert '"profile": "none"' in stored
    assert C.main(["--root", str(tmp_path), "config", "nope=1"]) == 3      # unknown key
    assert C.main(["--root", str(tmp_path), "config", "no-equals"]) == 3


def test_run_totals_sums_cost_and_flags_mixed_units():
    from speckfish.steps import run_totals
    steps = {"a": {"cost": 0.5, "cost_unit": "usd", "duration_s": 10.0},
             "b": {"cost": 1.25, "cost_unit": "usd", "duration_s": 5.5}}
    out = run_totals(steps)
    assert out["cost"] == 1.75 and out["duration_s"] == 15.5 and "cost_note" not in out

    mixed = run_totals({"a": {"cost": 0.5, "cost_unit": "usd", "duration_s": 1.0},
                        "b": {"cost": 2.0, "cost_unit": "credits", "duration_s": 1.0}})
    assert "mixed units" in mixed["cost_note"]


def test_clean_runs_keeps_newest(tmp_path):
    from speckfish.steps import clean_runs
    cfg = Config(root=tmp_path)
    for name in ("20260101-000001", "20260102-000001", "20260103-000001"):
        (cfg.runs_dir / name).mkdir(parents=True)
        (cfg.runs_dir / name / "state.json").write_text("{}", encoding="utf-8")
    out = clean_runs(cfg, keep=2)
    assert out["removed"] == ["20260101-000001"]
    assert sorted(p.name for p in cfg.runs_dir.iterdir()) == ["20260102-000001", "20260103-000001"]


def test_pipeline_with_tasks_and_analyze(tmp_path, monkeypatch):
    from speckfish import steps as S
    monkeypatch.setattr(S, "get_harness", lambda cfg=None, name=None: ClaudeHarness(cfg))
    egm = tmp_path / "egm"
    (egm / "skills" / "x").mkdir(parents=True)
    (egm / "plugin.json").write_text('{"version": "0"}', encoding="utf-8")
    cfg = Config(root=tmp_path)
    cfg.egm_root = str(egm)
    (tmp_path / "specs" / "001-x").mkdir(parents=True)
    for f in ("spec.md", "plan.md"):
        (tmp_path / "specs" / "001-x" / f).write_text("# x", encoding="utf-8")
    out = S.do_pipeline(cfg, "idea", "stack", None, gate=False, with_drift=False,
                        with_tasks=True, with_analyze=True, stream=False, dry_run=True)
    assert list(out["steps"]) == ["specify", "plan", "tasks", "analyze", "review"]


def test_generate_removes_commands_from_the_old_prefix(tmp_path):
    from speckfish.editors import generate
    cfg = Config(root=tmp_path)
    prompts = tmp_path / ".github" / "prompts"
    cmds = tmp_path / ".claude" / "commands"
    prompts.mkdir(parents=True)
    cmds.mkdir(parents=True)
    (prompts / "speckfish-review.prompt.md").write_text("old", encoding="utf-8")
    (cmds / "speckfish-review.md").write_text("old", encoding="utf-8")
    (cmds / "mine.md").write_text("user file", encoding="utf-8")   # not ours: never touched

    out = generate(cfg, ["copilot", "claude"], "speckfish", None, force=True)
    assert len(out["removed"]) == 2
    assert not (prompts / "speckfish-review.prompt.md").exists()
    assert (cmds / "mine.md").exists()
    assert (cmds / "spfish-review.md").exists()


# ---------- specify --split (a big brief into independent features) ----------
def test_split_prompt_asks_for_json_and_expands_files(tmp_path):
    from speckfish.prompts import split_prompt
    f = tmp_path / "brief.md"
    f.write_text("# Brief\n- exportar CSV\n- etiquetas\n", encoding="utf-8")
    p = split_prompt("@brief.md", 4, tmp_path)
    assert "exportar CSV" in p
    assert "Answer ONLY with JSON" in p
    assert "at most 4 features" in p


def test_extract_features_from_structured_and_from_raw_text():
    from speckfish.runner import StepResult
    from speckfish.steps import extract_features

    structured = StepResult(step="split", ok=True, exit_code=0, duration_s=1.0,
                            structured={"features": [{"title": "A", "description": "aa"},
                                                     {"title": "B", "description": "bb"}]})
    assert [f["title"] for f in extract_features(structured)] == ["A", "B"]

    # Copilot/Codex: no --json-schema, the JSON arrives as prose
    raw = StepResult(step="split", ok=True, exit_code=0, duration_s=1.0,
                     raw_tail='Here you go:\n{"features":[{"title":"X","description":"xx"}]}\nDone')
    assert [f["title"] for f in extract_features(raw)] == ["X"]

    empty = StepResult(step="split", ok=True, exit_code=0, duration_s=1.0, raw_tail="no json here")
    assert extract_features(empty) == []


def test_split_creates_one_spec_per_feature(tmp_path, monkeypatch):
    from speckfish import steps as S
    from speckfish.runner import StepResult

    cfg = Config(root=tmp_path)
    cfg.egm_root = str(tmp_path)
    calls = []

    def fake_run_step(cfg_, h, step, prompt, cwd, rid, egm, **kw):
        calls.append(step)
        if step == "specify-split":
            return StepResult(step=step, ok=True, exit_code=0, duration_s=1.0,
                              structured={"features": [{"title": "one", "description": "d1"},
                                                       {"title": "two", "description": "d2"},
                                                       {"title": "three", "description": "d3"}]})
        assert "/speckit-specify" in prompt
        return StepResult(step=step, ok=True, exit_code=0, duration_s=1.0)

    monkeypatch.setattr(S, "run_step", fake_run_step)
    out = S.do_specify_split(cfg, "brief", None, harness=ClaudeHarness(cfg))
    assert out["features"] == ["one", "two", "three"]
    assert calls == ["specify-split", "specify-1", "specify-2", "specify-3"]
    assert (tmp_path / ".speckfish" / "runs" / out["run_id"] / "split.json").exists()


def test_split_dry_run_calls_the_agent_once(tmp_path, monkeypatch):
    from speckfish import steps as S

    cfg = Config(root=tmp_path)
    cfg.egm_root = str(tmp_path)
    out = S.do_specify_split(cfg, "brief", "rid", harness=ClaudeHarness(cfg), dry_run=True)
    assert out["dry_run"] is True
    assert list(out["steps"]) == ["split"]
    assert out["steps"]["split"]["error"] == "dry-run"


# ---------- EGM right after specifying ----------
def test_split_review_runs_egm_per_spec(tmp_path, monkeypatch):
    from speckfish import steps as S
    from speckfish.runner import StepResult

    cfg = Config(root=tmp_path)
    cfg.egm_root = str(tmp_path)
    feat_dir = tmp_path / "specs" / "001-x"
    feat_dir.mkdir(parents=True)
    (feat_dir / "spec.md").write_text("# spec", encoding="utf-8")
    reviewed = []

    def fake_run_step(cfg_, h, step, prompt, cwd, rid, egm, **kw):
        if step == "specify-split":
            return StepResult(step=step, ok=True, exit_code=0, duration_s=1.0,
                              structured={"features": [{"title": "one", "description": "d"}]})
        return StepResult(step=step, ok=True, exit_code=0, duration_s=1.0,
                          structured={"feature_dir": str(feat_dir)})

    def fake_review(cfg_, target=None, run_id=None, harness=None, profile=None, stage=None,
                    stream=True, dry_run=False, default_file="plan.md", interactive=False):
        reviewed.append(target)
        return StepResult(step="review", ok=True, exit_code=0, duration_s=1.0)

    monkeypatch.setattr(S, "run_step", fake_run_step)
    monkeypatch.setattr(S, "do_review", fake_review)

    out = S.do_specify_split(cfg, "brief", None, harness=ClaudeHarness(cfg), review=True)
    assert reviewed == [str(feat_dir / "spec.md")]      # EGM reviewed the spec it just wrote
    assert "review-1" in out["steps"]


def test_permission_denials_are_reported_in_the_summary():
    from speckfish.cli import summarize
    out = summarize({"step": "specify", "ok": True, "exit_code": 0, "duration_s": 1.0,
                     "denials": [{"tool_name": "PowerShell"}]})
    assert "permission denial" in out and "PowerShell" in out


# ---------- feature selection ----------
def _project(tmp_path, names=("002-open-remember-repos", "003-safe-staging-commit")):
    specs = tmp_path / "specs"
    for n in names:
        d = specs / n
        d.mkdir(parents=True)
        (d / "spec.md").write_text("# s", encoding="utf-8")
    return tmp_path


def test_list_and_active_feature(tmp_path):
    from speckfish.steps import active_feature, list_features, set_active_feature
    _project(tmp_path)
    cfg = Config(root=tmp_path)
    assert active_feature(cfg) is None
    rows = list_features(cfg)
    assert [r["name"] for r in rows] == ["002-open-remember-repos", "003-safe-staging-commit"]
    assert rows[0]["spec"] and not rows[0]["plan"]

    set_active_feature(cfg, tmp_path / "specs" / "003-safe-staging-commit")
    assert active_feature(cfg).name == "003-safe-staging-commit"
    assert json.loads((tmp_path / ".specify" / "feature.json").read_text(encoding="utf-8"))[
        "feature_directory"] == "specs/003-safe-staging-commit"
    assert [r["name"] for r in list_features(cfg) if r["active"]] == ["003-safe-staging-commit"]


def test_find_feature_by_number_name_and_path(tmp_path):
    from speckfish.steps import find_feature
    _project(tmp_path)
    cfg = Config(root=tmp_path)
    assert find_feature(cfg, "002").name == "002-open-remember-repos"
    assert find_feature(cfg, "002-open").name == "002-open-remember-repos"
    assert find_feature(cfg, "specs/003-safe-staging-commit").name == "003-safe-staging-commit"
    with pytest.raises(SystemExit):
        find_feature(cfg, "099")


def test_plan_feature_flag_sets_active_and_names_it_in_the_prompt(tmp_path):
    from speckfish import steps as S
    _project(tmp_path)
    (tmp_path / "vendor").mkdir()
    cfg = Config(root=tmp_path)
    cfg.egm_root = str(tmp_path)
    res = S.do_plan(cfg, "Swift 6", None, harness=ClaudeHarness(cfg), dry_run=True,
                    feature="002")
    assert "002-open-remember-repos" in res.log
    assert json.loads((tmp_path / ".specify" / "feature.json").read_text(encoding="utf-8"))[
        "feature_directory"] == "specs/002-open-remember-repos"


def test_msys_style_root_is_understood_on_windows():
    from speckfish.config import _native, is_windows
    if not is_windows():
        pytest.skip("Windows only")
    assert _native("/c/Users/x/repo") == Path("C:/Users/x/repo")


# ---------- output language ----------
def test_language_line_defaults_to_english():
    from speckfish.prompts import language_line
    assert "in English" in language_line("en")
    assert "in Spanish" in language_line("es")
    assert language_line("") == ""
    assert "in Klingon" in language_line("klingon")     # unknown code still works


def test_prompts_carry_the_language_instruction(tmp_path):
    from speckfish import steps as S
    cfg = Config(root=tmp_path)
    assert cfg.language == "en"                          # English by default
    out = S.with_language("do the thing", cfg)
    assert out.startswith("do the thing") and "in English" in out
    assert S.with_language(out, cfg).count("in English") == 1   # never twice

    cfg.language = "es"
    assert "in Spanish" in S.with_language("do the thing", cfg)
    cfg.language = ""
    assert S.with_language("do the thing", cfg) == "do the thing"


def test_language_is_a_config_key(tmp_path):
    from speckfish import cli as C
    (tmp_path / ".speckfish").mkdir(parents=True)
    assert C.main(["--root", str(tmp_path), "config", "language=es"]) == 0
    stored = (tmp_path / ".speckfish" / "config.json").read_text(encoding="utf-8")
    assert '"language": "es"' in stored


# ---------- reading the report in the terminal ----------
def _fake_review_run(tmp_path, body=None, run_id="20260101-120000"):
    art = tmp_path / ".speckfish" / "runs" / run_id / "artifacts" / "review"
    art.mkdir(parents=True)
    (art / "consolidated_review_report.md").write_text(body or "# Report\n" + "line\n" * 60,
                                                      encoding="utf-8")
    return tmp_path


def test_report_prints_the_consolidated_report(tmp_path, capsys):
    from speckfish import cli as C
    _fake_review_run(tmp_path)
    assert C.main(["--root", str(tmp_path), "report", "--lines", "5"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("# Report")
    assert "use `speckfish report --full`" in out        # tells you how to see the rest


def test_report_full_prints_everything(tmp_path, capsys):
    from speckfish import cli as C
    _fake_review_run(tmp_path)
    assert C.main(["--root", str(tmp_path), "report", "--full"]) == 0
    out = capsys.readouterr().out
    assert out.count("line") == 60 and "more line(s)" not in out


def test_report_without_runs_fails_clearly(tmp_path, capsys):
    from speckfish import cli as C
    assert C.main(["--root", str(tmp_path), "report"]) == 2
    assert "no report found" in capsys.readouterr().err


# ---------- failures must be readable too ----------
QUOTA_JSON = (
    '{"duration_api_ms":792,"session_id":"x","permission_denials":[],"is_error":true,'
    '"num_turns":1,"api_error_status":429,"api_error_origin":"first_party",'
    '"api_error":"usage_limit_reached","result":"You\'ve hit your weekly limit \u00b7 resets 8pm '
    '(Europe/Madrid)","type":"result","subagent_stats":{"spawned":0}}'
)


def test_quota_failure_is_reported_as_a_sentence():
    from speckfish.runner import error_kind, human_error
    assert error_kind(QUOTA_JSON, "") == "quota"
    msg = human_error(QUOTA_JSON, "", 1)
    assert "ran out of quota" in msg and "weekly limit" in msg
    assert "{" not in msg and "subagent_stats" not in msg


def test_quota_failure_exits_as_environment():
    from speckfish.cli import summarize
    res = {"step": "review", "ok": False, "exit_code": 1, "duration_s": 6.0,
           "error": "the agent ran out of quota (HTTP 429): weekly limit",
           "error_kind": "quota"}
    out = summarize(res, "C:/repo")
    assert "ERROR" in out and "--harness copilot" in out     # tells you what to do
    assert "{" not in out


def test_auth_failure_is_classified():
    from speckfish.runner import error_kind, human_error
    raw = '{"is_error":true,"api_error_status":401,"api_error":"authentication_error"}'
    assert error_kind(raw, "") == "auth"
    assert "credentials" in human_error(raw, "", 1)


def test_plain_text_failure_keeps_a_short_tail():
    from speckfish.runner import human_error
    err = "l1\nl2\nl3\ncache miss is old\nboom: cannot start\nsecond line"
    msg = human_error("", err, 7)
    assert msg.startswith("exit 7:") and "boom: cannot start" in msg
    assert "cache miss" not in msg            # only the last useful lines


def test_error_without_output_says_so():
    from speckfish.runner import human_error
    assert human_error("", "", 1) == "exit 1 with no output"


# ---------- live progress while the agent works ----------
def _stream_events():
    """Real shape of claude --output-format stream-json events."""
    return [
        '{"type":"system","subtype":"init","tools":["Agent","Read"]}',
        '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Read",'
        '"input":{"file_path":"specs/002/spec.md"}}]}}',
        '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Agent",'
        '"input":{"subagent_type":"egm:egm-zoom-out"}}]}}',
        '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Agent",'
        '"input":{"subagent_type":"egm:egm-comprehension"}}]}}',
        '{"type":"result","subtype":"success","total_cost_usd":0.5,"num_turns":4,'
        '"structured_output":{"verdict":"ok","meets_standard":true,"blockers":[],"majors":0,'
        '"nits":0,"reports":[]},"usage":{"input_tokens":10,"output_tokens":5}}',
    ]


def test_progress_shows_subagents_and_elapsed_time():
    import io

    from speckfish.progress import Progress
    buf = io.StringIO()
    p = Progress("review", enabled=True, stream=buf, plain_every=0)
    for line in _stream_events():
        p.feed(line)
    status = p.status()
    assert status.startswith("[review] ")
    assert "subagents 2" in status
    assert "egm:egm-comprehension" in status          # tells you where it is
    assert "tools 3" in status
    p.stop()


def test_progress_never_breaks_a_run_on_garbage():
    import io

    from speckfish.progress import Progress
    buf = io.StringIO()
    p = Progress("plan", enabled=True, stream=buf, plain_every=0)
    for junk in ("", "not json", "{broken", "[]", '{"type":"weird"}', None):
        p.feed(junk)
    assert p.status().startswith("[plan]")
    p.stop()


def test_progress_prints_a_line_without_a_tty():
    import io

    from speckfish.progress import Progress
    buf = io.StringIO()
    p = Progress("review", enabled=True, stream=buf, plain_every=0)
    p.feed(_stream_events()[3])
    p._draw()
    assert "[review] 00:0" in buf.getvalue()


def test_progress_can_be_switched_off_globally():
    import io

    from speckfish import progress as P
    buf = io.StringIO()
    old, P.ENABLED = P.ENABLED, False
    try:
        p = P.Progress("review", enabled=True, stream=buf, plain_every=0)
        assert p.enabled is False
        p.feed(_stream_events()[3])
        p.start()
        p.stop()
        assert buf.getvalue() == ""
    finally:
        P.ENABLED = old


def test_claude_parses_stream_json_events():
    from speckfish.harnesses import ClaudeHarness
    out = "\n".join(_stream_events()) + "\n"
    parsed = ClaudeHarness.parse_output(None, out, "", 0)
    assert parsed["cost_usd"] == 0.5
    assert parsed["structured"]["verdict"] == "ok"
    assert parsed["model"] is None or isinstance(parsed["model"], str)


def test_real_claude_stream_quota_fixture(tmp_path):
    """A captured stream-json run that hit the weekly limit (fixture, not invented)."""
    from speckfish.runner import error_kind, human_error
    raw = (Path(__file__).parent / "fixtures" / "claude_quota_stream.jsonl").read_text(encoding="utf-8")
    assert error_kind(raw, "") == "quota"
    msg = human_error(raw, "", 1)
    assert "ran out of quota" in msg and "429" in msg
    assert "reopens at" in msg                      # tells you when to retry
    assert "rate_limit_event" not in msg and '"type"' not in msg


def test_progress_notices_a_rate_limit():
    import io

    from speckfish.progress import Progress
    buf = io.StringIO()
    p = Progress("review", enabled=True, stream=buf, plain_every=0)
    raw = (Path(__file__).parent / "fixtures" / "claude_quota_stream.jsonl").read_text(encoding="utf-8")
    for line in raw.splitlines():
        p.feed(line)
    assert p.status().startswith("[review]")
    p.stop()


# ---------- codex adapter: what a real run taught us ----------
def test_codex_bypasses_sandbox_or_it_cannot_write(tmp_path):
    cfg = _cfg(tmp_path)
    argv = CodexHarness(cfg).build("specify", "p", tmp_path, tmp_path, None,
                                   None, 60, None).argv
    assert "--dangerously-bypass-approvals-and-sandbox" in argv


def test_designated_spec_is_verified_not_assumed(tmp_path):
    """A step that says ok but writes no spec.md is a failure (codex read-only sandbox did this)."""
    from speckfish.runner import StepResult, postprocess
    res = StepResult(step="specify", ok=True, exit_code=0, duration_s=70.0)
    postprocess(res, "specify", tmp_path / "art", tmp_path)
    assert res.ok is False
    assert "wrote no spec.md" in res.error


def test_specification_that_exists_passes_the_check(tmp_path):
    from speckfish.runner import StepResult, postprocess
    (tmp_path / "specs" / "001-x").mkdir(parents=True)
    (tmp_path / "specs" / "001-x" / "spec.md").write_text("# s", encoding="utf-8")
    res = StepResult(step="specify", ok=True, exit_code=0, duration_s=10.0)
    postprocess(res, "specify", tmp_path / "art", tmp_path)
    assert res.ok is True and res.error == ""


def test_node_entry_finds_a_bin_js_package(tmp_path, monkeypatch):
    """codex ships bin/codex.js, not index.js: the shim prefix must still resolve."""
    from speckfish import config as C
    binjs = tmp_path / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
    binjs.parent.mkdir(parents=True)
    binjs.write_text("// entry", encoding="utf-8")
    shim = tmp_path / "codex"
    shim.write_text("", encoding="utf-8")
    monkeypatch.setattr(C.shutil, "which", lambda n: str(shim) if n == "codex" else None)
    assert C.node_entry("codex") == str(binjs)


def test_doctor_never_raises_when_the_binary_is_broken(tmp_path, monkeypatch):
    from speckfish import harnesses as H
    cfg = _cfg(tmp_path)

    def boom(*_a, **_k):
        raise OSError(193, "not a valid Win32 application")

    monkeypatch.setattr(H, "run_cmd", boom)
    monkeypatch.setattr(H.CodexHarness, "binary", lambda self: "C:/fake/codex")
    info = H.CodexHarness(cfg).doctor()
    assert info["installed"] is True
    assert info["version_exit"] == 1          # reported, not raised


# ---------- split inventory on harnesses without --json-schema ----------
def test_inventory_json_is_recovered_from_prose_log(tmp_path):
    """codex/copilot answer in prose; the log also carries a schema that must not win."""
    from speckfish.steps import json_object_with
    log = ('noise\n'
           '{"type":"object","properties":{"features":{"type":"array"}}}\n'
           '{"features":[{"title":"short name"}]}\n'
           'the plan:\n{"features":[{"title":"A","description":"da","why":"wa"},'
           '{"title":"B","description":"db","why":"wb"}],"notes":"two"}')
    found = json_object_with(log, "features")
    assert [f["title"] for f in found["features"]] == ["A", "B"]
    assert found["notes"] == "two"


def test_inventory_payload_reads_the_struct_log(tmp_path):
    from speckfish.config import Config
    from speckfish.runner import StepResult
    from speckfish.steps import inventory_payload
    log = tmp_path / "split.log"
    log.write_text('{"features":[{"title":"X","description":"d"}]}', encoding="utf-8")
    res = StepResult(step="specify-split", ok=True, exit_code=0, duration_s=1.0,
                     log=str(log), structured=None)
    payload = inventory_payload(res, Config(root=tmp_path))
    assert payload["features"][0]["title"] == "X"


# ---------- one step over every feature ----------
def test_feature_batch_stops_at_the_first_failure(tmp_path, monkeypatch):
    from speckfish import steps as S
    from speckfish.runner import StepResult
    for n in ("001-a", "002-b", "003-c"):
        d = tmp_path / "specs" / n
        d.mkdir(parents=True)
        (d / "spec.md").write_text("# s", encoding="utf-8")
    cfg = Config(root=tmp_path)
    seen = []

    def fake_plan(cfg_, stack, run_id=None, harness=None, stream=True, dry_run=False, feature=None):
        seen.append(feature)
        ok = feature != "002-b"                      # the middle one breaks
        return StepResult(step="plan", ok=ok, exit_code=0 if ok else 2, duration_s=1.0,
                          error="" if ok else "boom", cost=0.5, cost_unit="usd")

    monkeypatch.setattr(S, "do_plan", fake_plan)
    feats = S.features_with_spec(cfg)
    batch = S.do_feature_batch(cfg, "plan", feats, harness=SimpleNamespace(name="fake"))
    assert seen == ["001-a", "002-b"]                # 003 never ran
    assert batch["stopped_at"] == "002-b"
    assert batch["done"] is False
    assert batch["total_cost"] == 1.0


def test_batch_summary_says_where_it_stopped():
    from speckfish.cli import batch_summary
    text = batch_summary({"action": "tasks", "run_id": "r1", "order": ["001-a", "002-b"],
                          "steps": {"001-a": {"ok": True, "duration_s": 61.0},
                                    "002-b": {"ok": False, "duration_s": 12.0, "error": "boom"}},
                          "stopped_at": "002-b", "total_cost": 0.0})
    assert "OK     001-a" in text and "FAILED 002-b" in text
    assert "stopped at 002-b" in text and "boom" in text


def test_all_features_reaches_every_feature(tmp_path, monkeypatch):
    from speckfish import cli as C
    from speckfish import steps as S
    from speckfish.runner import StepResult
    for n in ("001-a", "002-b"):
        d = tmp_path / "specs" / n
        d.mkdir(parents=True)
        (d / "spec.md").write_text("# s", encoding="utf-8")
    calls = []
    monkeypatch.setattr(S, "do_plan", lambda cfg, stack, rid=None, h=None, stream=True,
                        dry_run=False, feature=None: (calls.append(feature),
                                                      StepResult(step="plan", ok=True,
                                                                 exit_code=0, duration_s=1.0))[1])
    monkeypatch.setattr(C, "get_harness", lambda *a, **k: SimpleNamespace(name="fake"))
    monkeypatch.setattr(S, "get_harness", lambda cfg: SimpleNamespace(name="fake"))
    assert C.main(["--root", str(tmp_path), "plan", "x", "--all-features"]) == 0
    assert calls == ["001-a", "002-b"]
