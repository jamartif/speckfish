"""Sprint 4 of AUDITORIA.md: CI hardening (S7), run retention (P5), per-step metadata, SECURITY.md."""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from speckfish import cli as C
from speckfish import runner as R
from speckfish import steps as S
from speckfish.config import Config
from speckfish.harnesses import ClaudeHarness
from speckfish.runner import StepResult

ROOT = Path(__file__).resolve().parent.parent
yaml = pytest.importorskip("yaml")


def _workflows():
    return {p.name: yaml.safe_load(p.read_text(encoding="utf-8"))
            for p in sorted((ROOT / ".github" / "workflows").glob("*.yml"))}


def _steps(wf):
    for job in (wf.get("jobs") or {}).values():
        yield from job.get("steps") or []


# ---------------- S7: workflows ----------------

def test_every_action_is_pinned_to_a_commit_sha():
    unpinned = []
    for name, wf in _workflows().items():
        for step in _steps(wf):
            uses = step.get("uses")
            if uses and not re.fullmatch(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}", uses):
                unpinned.append((name, uses))
    assert not unpinned, f"pin these to a SHA (Dependabot keeps them current): {unpinned}"


def test_every_workflow_declares_its_permissions():
    for name, wf in _workflows().items():
        assert "permissions" in wf, f"{name} has no top-level permissions (it would get the repository default)"


def test_release_writes_only_in_the_job_that_publishes():
    wf = _workflows()["release.yml"]
    assert wf["permissions"] == {"contents": "read"}
    assert wf["jobs"]["release"]["permissions"] == {"contents": "write", "id-token": "write",
                                                    "attestations": "write"}
    assert "permissions" not in wf["jobs"]["test"]            # the test job inherits read-only
    assert any("attest-build-provenance" in (s.get("uses") or "") for s in wf["jobs"]["release"]["steps"])


def test_no_script_interpolates_text_an_outsider_controls():
    risky = re.compile(r"\$\{\{\s*(github\.event\.[\w.]+|github\.head_ref|github\.ref_name|inputs\.[\w-]+)")
    bad = []
    for name, wf in _workflows().items():
        for step in _steps(wf):
            for hit in risky.findall(step.get("run") or ""):
                bad.append((name, step.get("name"), hit))
    assert not bad, f"pass these through env: instead of the script text: {bad}"


def test_dependabot_covers_actions_and_pip():
    cfg = yaml.safe_load((ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8"))
    assert {u["package-ecosystem"] for u in cfg["updates"]} == {"github-actions", "pip"}


def test_ci_runs_lint_types_and_coverage():
    tests = _workflows()["tests.yml"]
    runs = " ".join(s.get("run") or "" for s in _steps(tests))
    assert "ruff check" in runs and "mypy" in runs and "--cov-fail-under" in runs


def test_the_dev_extra_installs_what_ci_runs():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    for tool in ("pytest-cov", "mypy", "ruff", "prompt_toolkit"):
        assert tool in text.split("[project.optional-dependencies]")[1].split("[tool.")[0], tool


# ---------------- SECURITY.md ----------------

def test_security_md_documents_the_controls_and_how_to_report():
    text = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
    for needle in ("Report a vulnerability", "speckfish trust", "--unsafe", "disposable", "--disallowedTools",
                   "stdin", "does not do", "SPECKFISH_ENV_ALLOWLIST", "--allow-outside-root"):
        assert needle in text, needle


# ---------------- P5: retention ----------------

def _cfg(tmp_path, **kw) -> Config:
    cfg = Config(root=tmp_path.resolve())
    for k, v in kw.items():
        setattr(cfg, k, v)
    return cfg


def _run(cfg, rid, steps=None, report=False):
    d = cfg.runs_dir / rid
    (d / "artifacts" / "review-001-a").mkdir(parents=True)
    if report:
        (d / "artifacts" / "review-001-a" / "consolidated_review_report.md").write_text("# r")
    (d / "state.json").write_text(json.dumps({"run_id": rid, "steps": steps or {}}))
    return d


def test_the_run_that_holds_a_features_only_review_is_never_deleted(tmp_path):
    cfg = _cfg(tmp_path)
    _run(cfg, "20260101-000001", {"review-001-a": {"status": "ok", "started_at": "2026-01-01T00:00:01"}})
    for i in range(2, 8):
        _run(cfg, f"20260101-00000{i}")
    out = S.clean_runs(cfg, 3)
    assert "20260101-000001" in out["protected"] and "20260101-000001" not in out["removed"]
    assert out["removed"] == ["20260101-000002", "20260101-000003", "20260101-000004"]
    assert (cfg.runs_dir / "20260101-000001").is_dir()


def test_a_superseded_review_does_not_protect_its_run(tmp_path):
    cfg = _cfg(tmp_path)
    _run(cfg, "20260101-000001", {"review-001-a": {"status": "ok", "started_at": "2026-01-01T00:00:01"}})
    _run(cfg, "20260101-000002", {"review-001-a": {"status": "ok", "started_at": "2026-01-01T00:00:02"}})
    for i in range(3, 6):
        _run(cfg, f"20260101-00000{i}")
    out = S.clean_runs(cfg, 2)
    assert "20260101-000001" in out["removed"] and "20260101-000002" in out["protected"]


def test_the_newest_run_with_a_report_survives_so_report_still_works(tmp_path):
    cfg = _cfg(tmp_path)
    _run(cfg, "20260101-000001", report=True)
    for i in range(2, 7):
        _run(cfg, f"20260101-00000{i}")
    out = S.clean_runs(cfg, 2)
    assert "20260101-000001" in out["protected"] and (cfg.runs_dir / "20260101-000001").is_dir()


def test_nothing_is_deleted_when_there_are_fewer_runs_than_the_limit(tmp_path):
    cfg = _cfg(tmp_path)
    _run(cfg, "a")
    _run(cfg, "b")
    assert S.clean_runs(cfg, 5) == {"kept": ["a", "b"], "removed": [], "protected": []}


def test_a_command_prunes_old_runs_and_keep_runs_zero_disables_it(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, keep_runs=3)
    cfg.save()
    for i in range(1, 8):
        _run(cfg, f"20260101-00000{i}")
    monkeypatch.setattr(C, "HANDLERS", {"status": lambda run: 0})
    C.main(["--root", str(tmp_path), "status"])
    assert sorted(p.name for p in cfg.runs_dir.iterdir()) == ["20260101-000005", "20260101-000006", "20260101-000007"]
    cfg.keep_runs = 0
    cfg.save()
    for i in range(1, 5):
        _run(cfg, f"20250101-00000{i}")
    C.main(["--root", str(tmp_path), "status"])
    assert len(list(cfg.runs_dir.iterdir())) == 7


def test_the_runs_command_does_not_prune_behind_your_back(tmp_path):
    cfg = _cfg(tmp_path, keep_runs=1)
    cfg.save()
    for i in range(1, 4):
        _run(cfg, f"20260101-00000{i}")
    C.main(["--root", str(tmp_path), "runs"])
    assert len(list(cfg.runs_dir.iterdir())) == 3


def test_keep_runs_is_validated_and_saved(tmp_path):
    assert C.main(["--root", str(tmp_path), "config", "keep_runs=lots"]) == C.EXIT_ENV
    assert C.main(["--root", str(tmp_path), "config", "keep_runs=12"]) == C.EXIT_OK
    assert Config.load(tmp_path).keep_runs == 12
    assert Config.load(tmp_path / "nothing").keep_runs == 50                  # the default


# ---------------- per-step metadata ----------------

def test_a_step_leaves_a_meta_file_with_no_prompt_and_no_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "s3cr3t-value")
    cfg = _cfg(tmp_path)
    prompt = "PRIVATE-BRIEF " + "x" * 400

    class Fake(ClaudeHarness):
        def build(self, **kw):
            inv = super().build(**kw)
            inv.argv = [sys.executable, "-c", "print('{\"type\":\"result\",\"total_cost_usd\":0.5}')",
                        "A" * 400]
            return inv

        def doctor(self):
            return {"version": "9.9.9 (Fake)"}

    res = R.run_step(cfg, Fake(cfg), "review", prompt, tmp_path, "rid", None, stream=False, progress=False,
                     kind="review")
    meta_path = cfg.runs_dir / "rid" / "logs" / "review.meta.json"
    raw = meta_path.read_text(encoding="utf-8")
    meta = json.loads(raw)
    assert "PRIVATE-BRIEF" not in raw and "s3cr3t-value" not in raw and "AWS_SECRET" not in raw
    assert meta["prompt_sha256"] == hashlib.sha256(prompt.encode()).hexdigest()
    assert meta["prompt_chars"] == len(prompt) and meta["prompt_via"] == "stdin"
    assert meta["harness"] == "claude" and meta["harness_version"] == "9.9.9 (Fake)"
    assert "no Bash, PowerShell" in meta["permissions"] and meta["permissions_setting"] == "scoped"
    assert "<400 chars>" in meta["argv"] and meta["env_filtered"] is False
    assert meta["kind"] == "review" and meta["cost"] == 0.5 and meta["ok"] == res.ok
    assert meta["egm"] is None


def test_the_meta_records_the_egm_commit_and_trust(tmp_path):
    from speckfish import trust
    cfg = _cfg(tmp_path)
    egm = tmp_path / "e"
    (egm / "skills").mkdir(parents=True)
    (egm / "plugin.json").write_text("{}")
    trust.record(egm)
    inv = NS(argv=["x"], stdin=None)
    res = StepResult(step="review", ok=True, exit_code=0, duration_s=1.0)
    R.write_meta(cfg, NS(name="fake"), inv, "p", res, "review", "review", "2026-01-01T00:00:00", egm,
                 R.run_dir_for(cfg, "rid"))
    meta = json.loads((cfg.runs_dir / "rid" / "logs" / "review.meta.json").read_text())
    assert meta["egm"]["path"].endswith("/e") and "trusted" in meta["egm"]
    assert meta["prompt_via"] == "argv" and meta["permissions"] == ""


def test_a_failing_meta_write_never_fails_the_step(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(R, "write_meta", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))

    class Echo(ClaudeHarness):
        def build(self, **kw):
            inv = super().build(**kw)
            inv.argv = [sys.executable, "-c", "print('ok')"]
            return inv

    res = R.run_step(cfg, Echo(cfg), "review", "p", tmp_path, "rid", None, stream=False, progress=False,
                     kind="review")
    assert res.exit_code == 0 and (cfg.runs_dir / "rid" / "state.json").exists()
