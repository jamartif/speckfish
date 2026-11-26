"""Sprint 1 of AUDITORIA.md: one regression test per finding (B1, P1, S2, S3, S4, B4, P4)."""
from __future__ import annotations

import json
import subprocess
import time
from types import SimpleNamespace as NS

import pytest

from speckfish import cli as C
from speckfish import editors as E
from speckfish import runner as R
from speckfish import steps as S
from speckfish.config import Config, env_number
from speckfish.runner import StepResult

GIT = ["git", "-c", "user.email=t@t", "-c", "user.name=t"]


def _cfg(tmp_path) -> Config:
    return Config(root=tmp_path.resolve())


# ---------------- B1: --dry-run must not erase the approved cut ----------------

def _split(cfg, **kw):
    fake = NS(name="fake")
    return S.do_specify_split(cfg, "a brief", "rid", fake, stream=False, **kw)


def _stub_inventory(monkeypatch, structured, ok=True):
    def fake_run_step(cfg, h, step, prompt, cwd, rid, egm, **kw):
        return StepResult(step=step, ok=ok, exit_code=0, duration_s=0.0, structured=structured)

    monkeypatch.setattr(S, "run_step", fake_run_step)


def test_dry_run_split_keeps_the_saved_cut(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    cut = tmp_path / "specs" / "features.json"
    cut.parent.mkdir(parents=True)
    cut.write_text(json.dumps({"features": [{"title": "approved", "description": "d"}]}))
    _stub_inventory(monkeypatch, None)                  # a dry run has no structured output
    out = _split(cfg, dry_run=True)
    assert out["dry_run"] is True
    assert json.loads(cut.read_text())["features"][0]["title"] == "approved"


def test_an_empty_inventory_does_not_overwrite_the_saved_cut(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    cut = tmp_path / "specs" / "features.json"
    cut.parent.mkdir(parents=True)
    cut.write_text(json.dumps({"features": [{"title": "approved", "description": "d"}]}))
    _stub_inventory(monkeypatch, {})
    out = _split(cfg)
    assert "error" in out
    assert json.loads(cut.read_text())["features"][0]["title"] == "approved"


def test_inventory_only_still_writes_the_cut(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _stub_inventory(monkeypatch, {"features": [{"title": "A", "description": "a"}]})
    _split(cfg, inventory_only=True)
    saved = json.loads((tmp_path / "specs" / "features.json").read_text())
    assert saved["features"][0]["title"] == "A"


# ---------------- P1: json_object_with is linear ----------------

def test_json_object_with_handles_megabytes_quickly():
    event = json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "x" * 200}]}})
    text = "\n".join([event] * 25_000) + '\n{"features":[{"title":"a","description":"b"}]}'
    assert len(text) > 5_000_000
    started = time.time()
    found = S.json_object_with(text, "features")
    assert found["features"][0]["title"] == "a"
    assert time.time() - started < 2.0, "json_object_with is quadratic again"


def test_json_object_with_does_no_decoding_when_the_key_is_absent():
    text = "{" * 200_000
    started = time.time()
    assert S.json_object_with(text, "features") == {}
    assert time.time() - started < 0.5


def test_json_object_with_still_prefers_the_richest_object_and_skips_schemas():
    schema = '{"$schema":"x","features":[{"title":"s"}],"properties":{}}'
    small = '{"features":[{"title":"one"}]}'
    big = '{"features":[{"title":"a","why":"w"},{"title":"b","why":"w"}]}'
    found = S.json_object_with(f"{schema}\nprose {small}\nmore {big}\ntail", "features")
    assert [f["title"] for f in found["features"]] == ["a", "b"]
    nested = '{"event":{"features":[{"title":"deep","description":"d"}]}}'
    assert S.json_object_with(nested, "features")["features"][0]["title"] == "deep"


# ---------------- S3: VS Code tasks never go through a shell ----------------

@pytest.mark.parametrize("cmd", ["speckfish", "python -m speckfish"])
def test_vscode_tasks_start_a_process_not_a_shell(cmd):
    data = E.tasks_json(cmd)
    assert data["tasks"]
    for t in data["tasks"]:
        assert t["type"] == "process", t["label"]
        assert " " not in t["command"], "the executable must be one token"
        assert all(isinstance(a, str) for a in t["args"])
    # the typed text stays one argument: nothing splits or expands it
    specify = next(t for t in data["tasks"] if t["label"].endswith(": specify"))
    assert specify["args"][-1] == "${input:idea}"


def test_the_module_form_is_split_into_executable_and_arguments():
    task = E.tasks_json("python -m speckfish")["tasks"][0]
    assert (task["command"], task["args"][:2]) == ("python", ["-m", "speckfish"])


# ---------------- S4: .speckfish/ stays out of git ----------------

def test_gitignore_is_created_and_extended_once(tmp_path):
    assert S.ensure_gitignore(tmp_path, [".speckfish/"]) == [".speckfish/"]
    first = (tmp_path / ".gitignore").read_text()
    assert ".speckfish/" in first and first.startswith("# speckfish")
    assert S.ensure_gitignore(tmp_path, [".speckfish/"]) == []          # idempotent
    assert (tmp_path / ".gitignore").read_text() == first


def test_gitignore_keeps_what_was_there_and_recognises_variants(tmp_path):
    (tmp_path / ".gitignore").write_text("node_modules/\n.speckfish\n")
    assert S.ensure_gitignore(tmp_path, [".speckfish/"]) == []
    (tmp_path / ".gitignore").write_text("node_modules/")               # no trailing newline
    assert S.ensure_gitignore(tmp_path, [".speckfish/", "vendor/egm/"]) == [".speckfish/", "vendor/egm/"]
    lines = (tmp_path / ".gitignore").read_text().splitlines()
    assert lines[0] == "node_modules/" and ".speckfish/" in lines and "vendor/egm/" in lines


def test_init_adds_the_state_dir_to_gitignore(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "run_specify", lambda *a, **k: (0, "ok", ""))
    monkeypatch.setattr(S, "which", lambda name: "/bin/" + name)
    monkeypatch.setattr(S, "install_egm", lambda cfg, ref=None: {"egm": "skipped"})
    out = S.do_init(_cfg(tmp_path), "claude", editors=False)
    assert {"gitignore": [".speckfish/"]} in out["actions"]
    assert ".speckfish/" in (tmp_path / ".gitignore").read_text()


def test_doctor_warns_when_the_state_dir_is_tracked_by_git(tmp_path, monkeypatch):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    assert S.is_git_ignored(tmp_path, ".speckfish") is False
    S.ensure_gitignore(tmp_path, [".speckfish/"])
    assert S.is_git_ignored(tmp_path, ".speckfish") is True
    assert S.is_git_ignored(tmp_path / "nowhere", ".speckfish") is None   # no repo: cannot tell
    info = {"root": "r", "project": {"has_git": True, "state_ignored": False}}
    assert "WARNING: .speckfish/ is not in .gitignore" in C.doctor_summary(info)
    info["project"]["state_ignored"] = True
    assert "WARNING" not in C.doctor_summary(info)


# ---------------- S2: EGM is pinned ----------------

@pytest.fixture
def egm_remote(tmp_path, monkeypatch):
    """A local stand-in for github.com/google/elephant-goldfish-model, with a tag and a later commit."""
    remote = tmp_path / "remote"
    (remote / "skills").mkdir(parents=True)
    (remote / "plugin.json").write_text('{"name":"egm","version":"0.1.1"}')
    (remote / "skills" / "egm-review").mkdir()
    (remote / "skills" / "egm-review" / "SKILL.md").write_text("review")
    run = lambda *a: subprocess.run([*GIT, "-C", str(remote), *a], check=True, capture_output=True, text=True)  # noqa: E731
    run("init", "-q")
    run("add", "-A")
    run("commit", "-q", "-m", "release")
    tagged = run("rev-parse", "HEAD").stdout.strip()
    run("tag", "v0.1.1")
    (remote / "plugin.json").write_text('{"name":"egm","version":"0.2.0"}')
    run("commit", "-q", "-am", "newer")
    newer = run("rev-parse", "HEAD").stdout.strip()
    run("tag", "v0.2.0")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", f"url.file://{remote}.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", f"https://github.com/{S.EGM_REPO}")
    monkeypatch.setattr(S, "EGM_COMMIT", tagged)
    return NS(tagged=tagged, newer=newer)


def test_egm_defaults_to_the_pinned_tag_and_verifies_its_commit(tmp_path, egm_remote):
    work = tmp_path / "project"
    work.mkdir()
    out = S.install_egm(_cfg(work))
    assert out["egm"] == "cloned" and out["ref"] == S.EGM_REF and out["commit"] == egm_remote.tagged
    assert json.loads((work / "vendor" / "egm" / "plugin.json").read_text())["version"] == "0.1.1"


def test_a_moved_tag_is_refused(tmp_path, egm_remote, monkeypatch):
    monkeypatch.setattr(S, "EGM_COMMIT", "0" * 40)           # upstream tag no longer points at what was pinned
    work = tmp_path / "project"
    work.mkdir()
    out = S.install_egm(_cfg(work))
    assert out["egm"] == "failed" and "refusing to use it" in out["detail"]


def test_an_existing_checkout_is_left_alone_unless_a_ref_is_asked_for(tmp_path, egm_remote):
    work = tmp_path / "project"
    work.mkdir()
    S.install_egm(_cfg(work))
    again = S.install_egm(_cfg(work))
    assert again["egm"] == "already_present" and again["commit"] == egm_remote.tagged
    moved = S.install_egm(_cfg(work), ref="v0.2.0")
    assert moved["egm"] == "updated" and moved["commit"] == egm_remote.newer
    assert json.loads((work / "vendor" / "egm" / "plugin.json").read_text())["version"] == "0.2.0"


def test_an_unknown_ref_fails_without_touching_the_checkout(tmp_path, egm_remote):
    work = tmp_path / "project"
    work.mkdir()
    S.install_egm(_cfg(work))
    out = S.install_egm(_cfg(work), ref="v9.9.9")
    assert out["egm"] == "failed"
    assert S.egm_commit(work / "vendor" / "egm") == egm_remote.tagged


def test_doctor_reports_the_commit_and_whether_it_is_the_pinned_one():
    info = {"root": "r", "egm": {"present": True, "version": "0.1.1", "path": "/p", "skills": ["a"],
                                 "commit": "a" * 40, "pinned": False}}
    text = C.doctor_summary(info)
    assert "NOT the pinned" in text and "aaaaaaaaaaaa" in text
    info["egm"]["pinned"] = True
    assert "[pinned]" in C.doctor_summary(info)


def test_the_pin_constants_are_well_formed():
    assert S.EGM_REF.startswith("v") and len(S.EGM_COMMIT) == 40 and set(S.EGM_COMMIT) <= set("0123456789abcdef")


# ---------------- B4: errors that used to end in a traceback ----------------

def test_a_corrupt_config_is_an_environment_error_that_names_the_file(tmp_path, capsys):
    path = Config.path(tmp_path.resolve())
    path.parent.mkdir(parents=True)
    path.write_text("{ not json")
    assert C.main(["--root", str(tmp_path), "config"]) == C.EXIT_ENV
    captured = capsys.readouterr()
    assert "config.json" in captured.out + captured.err
    path.write_text("[1, 2]")
    assert C.main(["--root", str(tmp_path), "config"]) == C.EXIT_ENV


def test_the_corrupt_config_message_says_which_file(tmp_path):
    path = Config.path(tmp_path.resolve())
    path.parent.mkdir(parents=True)
    path.write_text("{ not json")
    with pytest.raises(SystemExit) as e:
        Config.load(tmp_path)
    assert str(path) in str(e.value)


@pytest.mark.parametrize("raw,expected", [("abc", 600), ("", 600), ("-5", 600), ("nan", 600),
                                          ("30", 30), ("2.5", 2.5)])
def test_env_numbers_fall_back_instead_of_raising(monkeypatch, raw, expected):
    monkeypatch.setenv("SPECKFISH_X", raw)
    got = env_number("SPECKFISH_X", 600)
    assert got == expected or (raw == "nan" and got != got) or (raw == "nan" and got == 600)


def test_a_non_numeric_idle_limit_does_not_break_a_step(tmp_path, monkeypatch):
    monkeypatch.setenv("SPECKFISH_IDLE_SECONDS", "soon")
    monkeypatch.setenv("SPECKFISH_RESULT_GRACE", "-1")
    run_dir = R.run_dir_for(_cfg(tmp_path), "rid")
    inv = NS(argv=["python", "-c", "print('hi')"], env=None)
    code, out, _err, _dur, timed_out, idle = R.execute(NS(echo_stdout=False), inv, "x", tmp_path,
                                                       run_dir, 30, stream=False, progress=False)
    assert (code, timed_out, idle) == (0, False, False) and "hi" in out


def test_a_binary_that_cannot_start_is_a_failed_step_with_a_log(tmp_path):
    run_dir = R.run_dir_for(_cfg(tmp_path), "rid")
    inv = NS(argv=[str(tmp_path / "no-such-agent"), "-p", "x"], env=None)
    code, out, err, _dur, timed_out, idle = R.execute(NS(echo_stdout=False), inv, "x", tmp_path,
                                                      run_dir, 30, stream=False, progress=False)
    assert code == 127 and not timed_out and not idle
    assert "cannot launch" in err
    assert "cannot launch" in (run_dir / "logs" / "x.log").read_text()


def test_a_step_whose_binary_vanished_is_reported_not_raised(tmp_path):
    cfg = _cfg(tmp_path)

    class Gone(NS):
        def build(self, **kw):
            return NS(argv=[str(tmp_path / "gone")], env=None)

        def parse_output(self, out, err, code):
            return {}

    res = R.run_step(cfg, Gone(name="gone", echo_stdout=False), "review", "p", tmp_path, "rid", None,
                     stream=False, progress=False, kind="review")
    assert res.ok is False and "cannot launch" in res.error


def test_a_truncated_state_json_does_not_stop_the_next_step(tmp_path):
    cfg = _cfg(tmp_path)
    run_dir = R.run_dir_for(cfg, "rid")
    (run_dir / "state.json").write_text('{"run_id": "rid", "steps": {"plan": {"sta')   # killed mid-write
    res = StepResult(step="tasks", ok=True, exit_code=0, duration_s=1.0)
    R.record_state(cfg, run_dir, "rid", res, "tasks", NS(name="fake"), "2026-01-01T00:00:00")
    assert json.loads((run_dir / "state.json").read_text())["steps"]["tasks"]["status"] == "ok"


def test_run_summary_of_a_corrupt_state_is_empty_not_a_traceback(tmp_path):
    cfg = _cfg(tmp_path)
    run_dir = R.run_dir_for(cfg, "rid")
    (run_dir / "state.json").write_text("{ broken")
    assert S.run_summary(cfg, "rid") == {}
    (run_dir / "state.json").write_text("[]")
    assert S.run_summary(cfg, "rid") == {}


# ---------------- P4: the poll interval is configurable ----------------

def test_the_poll_interval_is_short_in_tests_and_one_second_by_default(monkeypatch):
    assert env_number("SPECKFISH_POLL_SECONDS", 1.0, minimum=0.01) == 0.1          # conftest sets 0.1
    monkeypatch.delenv("SPECKFISH_POLL_SECONDS")
    assert env_number("SPECKFISH_POLL_SECONDS", 1.0, minimum=0.01) == 1.0
    monkeypatch.setenv("SPECKFISH_POLL_SECONDS", "0")
    assert env_number("SPECKFISH_POLL_SECONDS", 1.0, minimum=0.01) == 1.0          # 0 would busy-loop
