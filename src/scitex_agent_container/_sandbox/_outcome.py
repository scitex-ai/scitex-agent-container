"""The per-run outcome record (``<run_dir>/out/outcome.json``) and its parts.

The record is written ATOMICALLY (temp file + ``os.replace``) at every
transition — ``prepared`` (sac start) -> ``running`` (supervisor, before the
container starts) -> ``exited`` (supervisor, child reaped) -> ``finalized``
(sac stop, or the next start) — so a reader never sees a partial or empty
file, and a supervisor killed mid-run still leaves a record whose
``exit.code`` is ``null`` with the reason stated.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

__all__ = [
    "SCHEMA",
    "atomic_write_json",
    "atomic_write_text",
    "hermes_usage",
    "manifest",
    "manifest_diff",
    "read_json",
    "utc_now",
]

SCHEMA = "scitex-agent-container/sandbox-outcome/v1"
MAX_LISTED = 5_000


def utc_now() -> str:
    t = time.time()
    return (
        time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t))
        + f".{int(t * 1000) % 1000:03d}Z"
    )


def atomic_write_text(path: Path, text: str, mode: int = 0o600) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        os.write(fd, text.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


def atomic_write_json(path: Path, doc: dict) -> None:
    atomic_write_text(path, json.dumps(doc, indent=2, sort_keys=True) + "\n")


def read_json(path: Path) -> dict | None:
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def manifest(root: Path) -> dict[str, list]:
    """{relpath: [size, mtime_ns]} of regular files under ``root`` (no symlink follow)."""
    out: dict[str, list] = {}
    root = Path(root)
    if not root.is_dir():
        return out
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for fn in sorted(filenames):
            p = Path(dirpath) / fn
            try:
                st = p.lstat()
            except OSError:
                continue
            if p.is_symlink() or not p.is_file():
                continue
            out[str(p.relative_to(root))] = [st.st_size, st.st_mtime_ns]
    return out


def manifest_diff(before: dict, after: dict) -> dict[str, Any]:
    created = sorted(set(after) - set(before))
    deleted = sorted(set(before) - set(after))
    modified = sorted(k for k in set(after) & set(before) if after[k] != before[k])
    trunc = any(len(x) > MAX_LISTED for x in (created, deleted, modified))
    return {
        "created": created[:MAX_LISTED],
        "modified": modified[:MAX_LISTED],
        "deleted": deleted[:MAX_LISTED],
        "counts": {
            "created": len(created),
            "modified": len(modified),
            "deleted": len(deleted),
        },
        "truncated": trunc,
    }



def hermes_usage(state_db: Path) -> dict[str, Any] | None:
    """Token / cost columns of Hermes' ``sessions`` table, when Hermes ran."""
    if not Path(state_db).is_file():
        return None
    try:
        con = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True, timeout=2)
    except sqlite3.Error as exc:
        return {"harness": "hermes", "error": str(exc)}
    try:
        cols = [r[1] for r in con.execute("PRAGMA table_info(sessions)")]
        rows = (
            [dict(zip(cols, r)) for r in con.execute("SELECT * FROM sessions")]
            if cols
            else []
        )
    except sqlite3.Error as exc:
        return {"harness": "hermes", "error": str(exc)}
    finally:
        con.close()
    tok = [c for c in cols if "token" in c]
    totals = {
        c: sum(r.get(c) or 0 for r in rows)
        for c in tok
        if all(isinstance(r.get(c), (int, float, type(None))) for r in rows)
    }
    keep = [c for c in cols if any(k in c for k in ("token", "cost", "model", "id"))]
    return {
        "harness": "hermes",
        "source": "state.db sessions",
        "sessions": len(rows),
        "token_totals": totals,
        "per_session": [{c: r.get(c) for c in keep} for r in rows][:200],
    }
