"""Project config and path helpers."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .store import write_json_atomic

STATE_DIR = ".speckfish"

DEFAULTS = {
    "harness": "auto",            # auto | claude | copilot | codex
    "egm_root": "",               # path to the EGM checkout (auto-detected when empty)
    "specify": "auto",            # auto | uv | binary
    "model": "",                  # forwarded to the harness when set
    "profile": "speckit",         # review criteria profile
    "language": "en",             # language of the generated specs/plans/reports
    "timeouts": {"init": 300, "specify": 1800, "plan": 1800, "tasks": 1800,
                 "analyze": 1200, "review": 2400, "drift": 1200, "implement": 3600,
                 "clarify": 1200, "checklist": 1200, "converge": 1800,
                 "brainstorm": 1200, "draft": 1200, "constitution": 1200, "revise": 3600,
                 "reconcile": 1200},
    "budgets": {"claude": {"specify": 1.5, "plan": 2.0, "review": 3.0, "drift": 1.5},
                "copilot": {"specify": 30, "plan": 30, "review": 30, "drift": 30}},
}


def env_number(name: str, default: float, minimum: float = 0.0) -> float:
    """A numeric environment variable; anything unusable (text, negative) falls back to the default."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value >= minimum else default


def is_windows() -> bool:
    return os.name == "nt"


def norm(p: str | Path) -> str:
    """Absolute path with "/" (safe inside prompts and JSON)."""
    return str(Path(p).expanduser().resolve()).replace("\\", "/")


def _native(path: str | Path) -> Path:
    """Accept MSYS-style paths (/c/Users/x) on Windows, where Python cannot resolve them."""
    text = str(path)
    if is_windows():
        # Path("/c/x") already normalised the separators to backslashes
        probe = text.replace("\\", "/")
        m = re.match(r"^/([a-zA-Z])(/.*)?$", probe)
        if m:
            text = f"{m.group(1).upper()}:{m.group(2) or '/'}"
    return Path(text).expanduser()


@dataclass
class Config:
    root: Path
    harness: str = "auto"
    egm_root: str = ""
    specify: str = "auto"
    model: str = ""
    profile: str = "speckit"
    language: str = "en"
    fallback: str = ""            # second agent CLI for a step that stops on quota/auth
    permissions: str = "scoped"   # scoped: read-only steps get no shell | open: every step gets every tool
    unsafe: bool = False          # allow an agent that runs with no sandbox (codex)
    keep_runs: int = 50           # old run folders deleted after each command (0 = never)
    env_allowlist: str = ""       # comma list (NAME or PREFIX_*): the only variables the agent inherits
    egm_root_from_env: bool = field(default=False, repr=False)      # runtime only, never saved
    timeouts: dict = field(default_factory=lambda: dict(DEFAULTS["timeouts"]))
    budgets: dict = field(default_factory=lambda: json.loads(json.dumps(DEFAULTS["budgets"])))

    # ---------- persistence ----------
    @staticmethod
    def path(root: Path) -> Path:
        return root / STATE_DIR / "config.json"

    SCHEMA = 2          # bump when a stored default would shadow a better one

    @classmethod
    def load(cls, root: Path) -> Config:
        root = _native(root).resolve()   # `--root ~/repo` and `/c/repo` both work
        p = cls.path(root)
        cfg = cls(root=root)
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                raise SystemExit(f"cannot read {p}: {e}. Fix or delete it (`speckfish init` rewrites "
                                 f"the defaults)") from e
            if not isinstance(data, dict):
                raise SystemExit(f"{p} must contain a JSON object, not {type(data).__name__}")
            # configs written by older builds stored every default, so a better timeout default
            # could never reach an existing project: drop that block once and migrate
            if int(data.get("schema") or 1) < cls.SCHEMA:
                data.pop("timeouts", None)
            for k in ("harness", "egm_root", "specify", "model", "profile", "language", "fallback",
                  "permissions", "unsafe", "env_allowlist"):
                if k in data:
                    setattr(cfg, k, data[k])
            if isinstance(data.get("keep_runs"), int) and data["keep_runs"] >= 0:
                cfg.keep_runs = data["keep_runs"]
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
            cfg.egm_root_from_env = True       # chosen by the person running speckfish, not by the repo
        if os.environ.get("SPECKFISH_UNSAFE") == "1":
            cfg.unsafe = True
        return cfg

    def save(self) -> Path:
        p = self.path(self.root)
        p.parent.mkdir(parents=True, exist_ok=True)
        data = asdict(self)
        data.pop("root", None)
        data.pop("egm_root_from_env", None)
        data["schema"] = self.SCHEMA
        # never write a default back: a stored default would outlive a better one in a later build
        if data.get("timeouts") == DEFAULTS["timeouts"]:
            data.pop("timeouts", None)
        if data.get("budgets") == DEFAULTS["budgets"]:
            data.pop("budgets", None)
        write_json_atomic(p, data)
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

# what an agent needs to start and reach its API when the environment is filtered (env_allowlist)
_BASE_ENV = {"PATH", "HOME", "USER", "USERNAME", "LOGNAME", "SHELL", "TERM", "LANG", "LANGUAGE", "TZ",
             "TMPDIR", "TEMP", "TMP", "SYSTEMROOT", "SYSTEMDRIVE", "COMSPEC", "PATHEXT", "USERPROFILE",
             "APPDATA", "LOCALAPPDATA", "PROGRAMFILES", "PROGRAMDATA", "WINDIR", "HOMEDRIVE", "HOMEPATH",
             "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_RUNTIME_DIR", "SSL_CERT_FILE",
             "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS", "REQUESTS_CA_BUNDLE", "HTTP_PROXY", "HTTPS_PROXY",
             "NO_PROXY", "http_proxy", "https_proxy", "no_proxy", "ALL_PROXY", "all_proxy", "CODEX_HOME"}
_BASE_PREFIXES = ("LC_", "ANTHROPIC_", "CLAUDE_", "COPILOT_", "CODEX_", "OPENAI_", "SPECKFISH_")


def agent_env(extra: dict | None = None, allowlist: str = "") -> dict:
    """The environment an agent process gets.

    By default it inherits everything (as before). With an allowlist (config `env_allowlist` or the
    `SPECKFISH_ENV_ALLOWLIST` variable: `NAME,PREFIX_*`) it gets only the basics it needs to start, its own
    provider's variables, and what the list names: a prompt-injected agent cannot read `AWS_SECRET_ACCESS_KEY`
    or `GITHUB_TOKEN` from an environment that never had them.
    """
    spec = (allowlist or os.environ.get("SPECKFISH_ENV_ALLOWLIST") or "").strip()
    if not spec:
        return {**os.environ, **(extra or {})}
    names = {x.strip() for x in spec.split(",") if x.strip()}
    prefixes = tuple(n[:-1] for n in names if n.endswith("*"))
    exact = {n for n in names if not n.endswith("*")} | _BASE_ENV
    keep = {k: v for k, v in os.environ.items()
            if k in exact or k.upper() in exact or k.startswith(_BASE_PREFIXES + prefixes)}
    return {**keep, **(extra or {})}


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
    for pat in ("*/npm-loader.js", "*/index.js", "*/bin/*.js", "*/bin/*.mjs",
                "*/*/npm-loader.js", "*/*/index.js", "*/*/bin/*.js"):
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
        cands += [r"C:\Program Files\Git\cmd\git.exe", r"C:\Program Files\Git\bin\git.exe",
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
    except OSError as e:
        # WinError 193 (a shim CreateProcess cannot run) is an OSError, not FileNotFoundError
        return 126, "", f"cannot run {args[0]}: {e}"
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout after {timeout}s"


def read_version(cmd: str) -> str:
    code, out, err = run_cmd([cmd, "--version"], timeout=60)
    txt = (out or err).strip().splitlines()
    return txt[0] if txt else f"exit={code}"


# ---------- EGM report parsing ----------

SEV_BLOCK = re.compile(r"^#+\s*.*\b(blockers?|bloqueantes?)\b", re.I)
SEV_MAJOR = re.compile(r"^#+\s*.*\b(majors?|mayor(?:es)?)\b", re.I)
SEV_NIT = re.compile(r"^#+\s*.*\b(nits?|menor(?:es)?)\b", re.I)
# "### B1. Title", "### M12: Title": one heading per finding under a severity heading
FINDING_HEADING = re.compile(r"^(#+)\s*[A-Za-z]{1,2}-?\d+[.:)\s]")
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
    section_level = 0
    in_table = False
    for line in lines:
        finding = FINDING_HEADING.match(line)
        if finding and section and len(finding.group(1)) > section_level:
            # one subheading per finding (what the Codex review writes): each one is an item
            if section == "blockers":
                out["blockers"].append(line.lstrip("# ").strip()[:300])
            elif section == "majors":
                out["majors"] += 1
            else:
                out["nits"] += 1
            in_table = False
            continue
        hashes = len(line) - len(line.lstrip("#")) if line.startswith("#") else 0
        for name, rx in (("blockers", SEV_BLOCK), ("majors", SEV_MAJOR), ("nits", SEV_NIT)):
            if rx.match(line):
                section, section_level, in_table = name, hashes, False
                break
        else:
            if line.startswith("#"):
                section, section_level, in_table = None, 0, False
                continue
        if line.startswith("#"):
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
    for i, line in enumerate(lines[:60]):
        low = line.lower()
        if "veredicto" in low or "verdict" in low:
            out["verdict"] = line.strip("# *:")[:400]
            if line.startswith("#"):
                # a bare "## Verdict" heading: the verdict is the first sentence under it
                for nxt in lines[i + 1:i + 8]:
                    if nxt.strip() and not nxt.startswith("#"):
                        out["verdict"] = nxt.strip(" *:")[:400]
                        break
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
