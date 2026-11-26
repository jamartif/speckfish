"""Which EGM checkouts speckfish will load as an agent plugin.

EGM is loaded with `--plugin-dir`, so its skills and agents run with the permissions of the agent. A
repository can carry its own `vendor/egm` or point `egm_root` at a folder of its choosing in
`.speckfish/config.json`: running speckfish on someone else's code must not execute that plugin without
you having looked at it. The rule is the one `direnv allow` uses:

- an EGM folder is trusted when its content hash is in `~/.speckfish/trusted.json`;
- `speckfish init` records the hash of the pinned checkout it just verified, and `speckfish trust` records
  the one you are looking at on purpose;
- a folder under `~/.speckfish/` (yours, not the project's) and a path given in `SPECKFISH_EGM_ROOT` are
  trusted without a record, and `SPECKFISH_TRUST_ALL=1` switches the check off (a disposable CI container).

Changing a single byte of a trusted checkout makes it untrusted again.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from pathlib import Path

from .config import Config, norm
from .store import read_json_cached, write_json_atomic

_SKIP_DIRS = {".git", "__pycache__", "node_modules"}
_HASHES: dict[str, str] = {}


def home_dir() -> Path:
    return Path(os.environ.get("SPECKFISH_HOME") or (Path.home() / ".speckfish"))


def store_path() -> Path:
    return home_dir() / "trusted.json"


def tree_hash(path: Path) -> str:
    """sha256 over every file's relative path and bytes, in a fixed order, ignoring `.git` and caches."""
    root = Path(path).resolve()
    key = str(root)
    if key in _HASHES:
        return _HASHES[key]
    digest = hashlib.sha256()
    for current, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS)
        for name in sorted(files):
            full = Path(current) / name
            digest.update(full.relative_to(root).as_posix().encode("utf-8") + b"\0")
            try:
                digest.update(full.read_bytes())
            except OSError:
                digest.update(b"<unreadable>")
            digest.update(b"\0")
    _HASHES[key] = digest.hexdigest()
    return _HASHES[key]


def _load() -> dict:
    data = read_json_cached(store_path())
    return data if isinstance(data, dict) else {}


def record(path: Path, commit: str = "") -> dict:
    """Trust this checkout as it is now."""
    root = Path(path).resolve()
    _HASHES.pop(str(root), None)
    entry = {"hash": tree_hash(root), "commit": commit, "at": datetime.now().isoformat(timespec="seconds")}
    store = dict(_load())
    store[str(root)] = entry
    write_json_atomic(store_path(), store)
    return entry


def forget_cache() -> None:
    _HASHES.clear()


def status(cfg: Config, egm: Path) -> tuple[bool, str]:
    """(trusted, why not). `why not` is a message that says what to do."""
    if os.environ.get("SPECKFISH_TRUST_ALL") == "1":
        return True, ""
    root = Path(egm).resolve()
    try:
        root.relative_to(home_dir().resolve())
        return True, ""
    except ValueError:
        pass
    env_root = os.environ.get("SPECKFISH_EGM_ROOT")
    if env_root and Path(env_root).resolve() == root:
        return True, ""
    entry = _load().get(str(root))
    if entry and entry.get("hash") == tree_hash(root):
        return True, ""
    where = "inside this project" if _inside(cfg.root, root) else "chosen by this project's config"
    if entry:
        return False, (f"EGM at {norm(root)} changed since you trusted it. Read what changed, then run "
                       f"`speckfish trust` to accept it.")
    return False, (f"EGM at {norm(root)} ({where}) is not trusted, and it would run with the agent's "
                   f"permissions. Look at it, then run `speckfish trust` (or `speckfish init`, which "
                   f"installs the pinned version and trusts it).")


def _inside(base: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(Path(base).resolve())
        return True
    except ValueError:
        return False


def require(cfg: Config, egm: Path | None) -> None:
    """Refuse (SystemExit, exit code 3) to run an agent with an EGM plugin that is not trusted."""
    if egm is None:
        return
    ok, why = status(cfg, egm)
    if not ok:
        raise SystemExit(why)


def describe(path: Path) -> dict:
    return {"path": norm(path), "hash": tree_hash(path),
            "files": sum(len(f) for _r, _d, f in os.walk(path) if ".git" not in _r)}


def dump_store() -> str:
    return json.dumps(_load(), indent=2)
