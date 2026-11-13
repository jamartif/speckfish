"""Project config and path helpers."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path

STATE_DIR = ".speckfish"

DEFAULTS = {
    "harness": "auto",            # auto | claude | copilot | codex
    "egm_root": "",               # path to the EGM checkout (auto-detected when empty)
    "specify": "auto",            # auto | uv | binary
    "model": "",                  # forwarded to the harness when set
    "profile": "speckit",         # review criteria profile
    "timeouts": {"init": 300, "specify": 600, "plan": 600, "review": 1200, "drift": 600},
    "budgets": {"claude": {"specify": 1.5, "plan": 2.0, "review": 3.0, "drift": 1.5},
                "copilot": {"specify": 30, "plan": 30, "review": 30, "drift": 30}},
}


def is_windows() -> bool:
    return os.name == "nt"


def norm(p: str | Path) -> str:
    """Absolute path with "/" (safe inside prompts and JSON)."""
    return str(Path(p).expanduser().resolve()).replace("\\", "/")


@dataclass
class Config:
    root: Path
    harness: str = "auto"
    egm_root: str = ""
    specify: str = "auto"
    model: str = ""
    profile: str = "speckit"
    timeouts: dict = field(default_factory=lambda: dict(DEFAULTS["timeouts"]))
    budgets: dict = field(default_factory=lambda: json.loads(json.dumps(DEFAULTS["budgets"])))

    # ---------- persistence ----------
    @staticmethod
    def path(root: Path) -> Path:
        return root / STATE_DIR / "config.json"

    @classmethod
    def load(cls, root: Path) -> Config:
        root = Path(root).resolve()
        p = cls.path(root)
        cfg = cls(root=root)
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            for k in ("harness", "egm_root", "specify", "model", "profile"):
                if k in data:
                    setattr(cfg, k, data[k])
            for k in ("timeouts", "budgets"):
                if k in data:
                    merged = dict(getattr(cfg, k))
                    merged.update(data[k] or {})
                    setattr(cfg, k, merged)
        env_h = os.environ.get("SPECKFISH_HARNESS")
        if env_h:
            cfg.harness = env_h
        env_e = os.environ.get("SPECKFISH_EGM_ROOT")
        if env_e:
            cfg.egm_root = env_e
        return cfg

    def save(self) -> Path:
        p = self.path(self.root)
        p.parent.mkdir(parents=True, exist_ok=True)
        data = asdict(self)
        data.pop("root", None)
        p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        return p

    # ---------- working paths ----------
    @property
    def state_dir(self) -> Path:
        return self.root / STATE_DIR

    @property
    def runs_dir(self) -> Path:
        return self.state_dir / "runs"

    def egm_path(self) -> Path | None:
        """Locate the EGM checkout (used for --plugin-dir)."""
        candidates = []
        if self.egm_root:
            candidates.append(Path(self.egm_root))
        candidates += [
            self.root / "vendor" / "egm",
            self.root / "vendor" / "elephant-goldfish-model",
            Path.home() / ".speckfish" / "egm",
        ]
        for c in candidates:
            if (c / "plugin.json").exists() and (c / "skills").is_dir():
                return c.resolve()
        return None


# ---------- process helpers ----------

def which(cmd: str) -> str | None:
    return shutil.which(cmd)


def node_entry(name: str) -> str | None:
    """Find the real .js entry of an npm CLI (skips Windows .cmd shims)."""
    exe = shutil.which(name)
    if not exe:
        return None
    npm_root = Path(exe).parent / "node_modules"
    if not npm_root.is_dir():
        return None
    cands = []
    for pat in ("*/npm-loader.js", "*/index.js", "*/*/npm-loader.js", "*/*/index.js"):
        cands += list(npm_root.glob(pat))
    for c in cands:
        if f"/{name}/" in c.as_posix():
            return str(c)
    return None


def agent_prefix(name: str) -> list[str]:
    """argv prefix to launch an agent CLI without going through cmd.exe.

    On Windows `shutil.which("copilot")` returns an extension-less shim that
    CreateProcess rejects (WinError 193), and `cmd /c` would break prompt
    quoting. We launch `node <entry>.js` when it is an npm package.
    """
    exe = shutil.which(name)
    if not exe:
        return []
    if is_windows():
        p = Path(exe)
        if p.suffix.lower() in ("", ".cmd", ".bat"):
            js = node_entry(name)
            node = shutil.which("node") or "node"
            if js:
                return [node, js]
            return ["cmd.exe", "/c", str(p)]
    return [exe]


_GIT: list[str] = []


def find_git() -> str:
    """Locate an executable git. On Windows the `git` on PATH can be blocked by
    App Control (WinError 5), so candidate paths are validated."""
    global _GIT
    if _GIT:
        return _GIT[0]
    cands: list[str] = []
    w = shutil.which("git")
    if w:
        cands.append(w)
    if is_windows():
        cands += [r"C:\Program Files\Git\cmd\git.exe", r"C:\Program Files\Gitin\git.exe",
                  r"C:\Program Files (x86)\Git\cmd\git.exe"]
    else:
        cands += ["/usr/bin/git", "/usr/local/bin/git", "/opt/homebrew/bin/git"]
    for c in cands:
        if not Path(c).exists():
            continue
        code, out, _ = run_cmd([c, "--version"], timeout=30)
        if code == 0 and "git version" in out.lower():
            _GIT = [c]
            return c
    raise SystemExit("cannot find an executable `git` (check App Control restrictions)")


def run_cmd(args: list[str], cwd: Path | None = None, timeout: int | None = None,
            env: dict | None = None) -> tuple[int, str, str]:
    """Run a command and return (exit code, stdout, stderr)."""
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    try:
        p = subprocess.run(args, cwd=str(cwd) if cwd else None, capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           timeout=timeout, env=full_env)
        return p.returncode, p.stdout or "", p.stderr or ""
    except FileNotFoundError as e:
        return 127, "", f"not found: {e}"
    except PermissionError as e:
        return 126, "", f"permission denied (App Control?): {e}"
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout after {timeout}s"


def read_version(cmd: str) -> str:
    code, out, err = run_cmd([cmd, "--version"], timeout=60)
    txt = (out or err).strip().splitlines()
    return txt[0] if txt else f"exit={code}"


# ---------- EGM report parsing ----------

SEV_BLOCK = re.compile(r"^#+\s*.*(blocker|bloqueante)", re.I)
SEV_MAJOR = re.compile(r"^#+\s*.*(major|mayor)", re.I)
SEV_NIT = re.compile(r"^#+\s*.*(nit|menor)", re.I)
BULLET = re.compile(r"^\s*(?:[-*]|\d+[.)])\s+(.*\S)")
TABLE_ROW = re.compile(r"^\s*\|(.+)\|\s*$")
TABLE_SEP = re.compile(r"^\s*\|[\s:|-]+\|\s*$")


def _row_text(line: str) -> str | None:
    """Return the action text of an EGM table row or of a bullet line."""
    m = BULLET.match(line)
    if m:
        return m.group(1).strip()
    if TABLE_SEP.match(line):
        return None
    m = TABLE_ROW.match(line)
    if m:
        cells = [c.strip() for c in m.group(1).split("|")]
        cells = [c for c in cells if c and not set(c) <= set("-: ")]
        if not cells:
            return None
        # the first cell is usually the id (A1); the action is the next long cell
        for c in cells:
            if len(c) > 3:
                return c
        return cells[0]
    return None


def parse_consolidated(report: Path) -> dict:
    """Extract verdict and counts from the EGM consolidated report (free form: bullets or tables)."""
    out = {"meets_standard": None, "blockers": [], "majors": 0, "nits": 0, "verdict": ""}
    if not report or not report.exists():
        return out
    text = report.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    section = None
    in_table = False
    for line in lines:
        if SEV_BLOCK.match(line):
            section, in_table = "blockers", False
            continue
        if SEV_MAJOR.match(line):
            section, in_table = "majors", False
            continue
        if SEV_NIT.match(line):
            section, in_table = "nits", False
            continue
        if line.startswith("#"):
            section, in_table = None, False
            continue
        if not section:
            continue
        is_table = bool(TABLE_ROW.match(line))
        if is_table and not in_table:
            in_table = True
            continue  # table header
        row = _row_text(line)
        if not row:
            if line.strip() == "" and in_table:
                in_table = False
            continue
        if section == "blockers":
            out["blockers"].append(row[:300])
        elif section == "majors":
            out["majors"] += 1
        else:
            out["nits"] += 1
    for line in lines[:40]:
        low = line.lower()
        if "veredicto" in low or "verdict" in low:
            out["verdict"] = line.strip("# *:")[:400]
            break
    blob = text[:4000].lower()
    NEGATIVE = ("no cumple", "no se cumple", "does not meet", "doesn't meet",
                "not meet the standard", "fails the standard", "non-compliant")
    if any(t in blob for t in NEGATIVE) or out["blockers"]:
        out["meets_standard"] = False
    elif "cumple" in blob or "meets the standard" in blob or "compliant" in blob:
        out["meets_standard"] = True
    return out


def find_report(artifact_dir: Path, pattern: str = "consolidated_review_report.md") -> Path | None:
    if not artifact_dir.exists():
        return None
    if any(ch in pattern for ch in "*?["):
        hits = sorted(artifact_dir.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
        return hits[0] if hits else None
    exact = artifact_dir / pattern
    if exact.exists():
        return exact
    cands = sorted(artifact_dir.glob("*report*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    return cands[0] if cands else None
