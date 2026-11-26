"""Reading and writing the small JSON files speckfish keeps (state, config, results).

Three things the plain `read_text` / `write_text` pairs did not give:

- `write_json_atomic`: a Ctrl+C or a crash in the middle of a write leaves the old file, never a
  truncated one that every later command trips over.
- `file_lock`: two speckfish processes on the same run (two CI jobs on one checkout) no longer lose each
  other's `state.json` entries by reading, modifying and writing at the same time.
- `read_json_cached`: `status` / `next` / `go` read the same `state.json` files many times per feature;
  the parsed copy is reused until the file's size or mtime changes.
"""
from __future__ import annotations

import contextlib
import json
import os
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

_CACHE: dict[str, tuple[tuple[int, int, int], Any]] = {}


def write_json_atomic(path: Path, data: object, **dumps: Any) -> None:
    """Write `data` as JSON so that readers see the old file or the new one, never half of it."""
    path = Path(path)
    dumps.setdefault("indent", 2)
    dumps.setdefault("ensure_ascii", False)
    text = json.dumps(data, **dumps)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        for attempt in range(5):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                # Windows: a reader (an editor, an antivirus scan) has the target open for a moment
                if attempt == 4:
                    raise
                time.sleep(0.05 * (attempt + 1))
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()


def write_text_atomic(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()


@contextlib.contextmanager
def file_lock(path: Path, timeout: float = 10.0, stale: float = 60.0) -> Iterator[bool]:
    """Exclusive lock around a read-modify-write of `path` (a `<path>.lock` file).

    Best effort by design: a lock older than `stale` seconds belongs to a dead process and is taken over,
    and after `timeout` the block runs anyway (yielding False) rather than hanging a pipeline.

    On Windows, creating the lock file while another process is deleting it fails with PermissionError, not
    FileExistsError: that is "busy, try again", never "nothing to protect" (treating it as the latter let
    two writers run unlocked and lose state entries).
    """
    lock = Path(str(path) + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + timeout
    held = False
    while True:
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            held = True
            break
        except (FileExistsError, PermissionError) as busy:
            if isinstance(busy, FileExistsError):
                try:
                    if time.time() - lock.stat().st_mtime > stale:
                        lock.unlink()
                        continue
                except OSError:
                    continue                 # it vanished between the two calls: try again
            if time.time() >= deadline:
                break
            time.sleep(0.02)
        except OSError:
            break                            # anything else (no such folder, read-only): nothing to protect
    try:
        yield held
    finally:
        if held:
            with contextlib.suppress(OSError):
                lock.unlink()


def read_json_retry(path: Path, attempts: int = 6) -> Any:
    """JSON of `path`, or None when it does not exist or is not valid JSON.

    A file being replaced can fail to open for a moment on Windows (PermissionError). That is retried, and if
    it persists the error is raised: reading it as "empty" would make the next write erase what was there.
    """
    for attempt in range(attempts):
        try:
            return json.loads(Path(path).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.05 * (attempt + 1))
        except (OSError, ValueError):
            return None
    return None


def read_json_cached(path: Path) -> Any:
    """Parsed JSON of `path`, or None when it is missing or unreadable.

    The returned object is shared: treat it as read-only (copy it before changing it).
    """
    key = str(path)
    try:
        st = os.stat(key)
    except OSError:
        _CACHE.pop(key, None)
        return None
    sig = (st.st_size, st.st_mtime_ns, st.st_ino)     # an atomic replace is a new inode
    hit = _CACHE.get(key)
    if hit is not None and hit[0] == sig:
        return hit[1]
    try:
        data = json.loads(Path(key).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = None
    if len(_CACHE) > 4096:
        _CACHE.clear()
    _CACHE[key] = (sig, data)
    return data
