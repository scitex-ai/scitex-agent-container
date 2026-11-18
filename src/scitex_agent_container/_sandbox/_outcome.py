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
import shutil
import sqlite3
import time
from collections import Counter
from pathlib import Path
from typing import Any

__all__ = [
    "SCHEMA",
    "atomic_write_json",
    "atomic_write_text",
    "hermes_transcript",
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


def hermes_usage(harness_db: Path) -> dict[str, Any] | None:
    """Token / cost columns of Hermes' ``sessions`` table, when Hermes ran."""
    if not Path(harness_db).is_file():
        return None
    try:
        con = sqlite3.connect(f"file:{harness_db}?mode=ro", uri=True, timeout=2)
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


#: Hermes file tools whose ``path`` argument names a file the agent READ.
_READ_TOOLS = {"read_file": "path", "search_files": "path", "view_file": "path"}
_MAX_ARG = 400


def _snapshot(harness_db: Path, scratch: Path) -> Path:
    """Copy ``state.db`` with its WAL/SHM so a read sees the live rows."""
    scratch.mkdir(parents=True, exist_ok=True)
    for p in Path(harness_db).parent.glob(Path(harness_db).name + "*"):
        shutil.copyfile(p, scratch / p.name)
    return scratch / Path(harness_db).name


def _tool_calls(rows) -> tuple[list[dict[str, Any]], Any]:
    result_bytes = {
        cid: len((content or "").encode())
        for role, _, cid, content, _ in rows
        if role == "tool" and cid
    }
    calls: list[dict[str, Any]] = []
    first_assistant = None
    for role, tool_calls, _, _, ts in rows:
        if role != "assistant":
            continue
        if first_assistant is None:
            first_assistant = ts
        try:
            items = json.loads(tool_calls) if tool_calls else []
        except ValueError:
            continue
        for item in items if isinstance(items, list) else []:
            fn = item.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                args = {"_raw": fn.get("arguments")}
            if not isinstance(args, dict):
                args = {"_raw": args}
            calls.append(
                {
                    "t": ts,
                    "tool": fn.get("name"),
                    "args": {k: str(v)[:_MAX_ARG] for k, v in args.items()},
                    "result_bytes": result_bytes.get(
                        item.get("id") or item.get("call_id")
                    ),
                }
            )
    return calls, first_assistant


def hermes_transcript(harness_db: Path, scratch: Path) -> dict[str, Any] | None:
    """Tool calls and files read, from Hermes' own message transcript.

    ``source`` names where the data came from: the harness transcript, not a
    syscall trace. ``files_read`` lists the ``path`` argument of Hermes'
    file-reading tools (with offset/limit and the size of the result the
    agent received); shell commands appear in ``tool_calls`` only, because
    what a command reads cannot be known from its text.
    """
    if not Path(harness_db).is_file():
        return None
    source = "harness transcript (hermes state.db messages)"
    try:
        con = sqlite3.connect(f"file:{_snapshot(harness_db, scratch)}?mode=ro", uri=True, timeout=2)
        try:
            rows = con.execute(
                "SELECT role, tool_calls, tool_call_id, content, timestamp "
                "FROM messages ORDER BY id"
            ).fetchall()
        finally:
            con.close()
    except (sqlite3.Error, OSError) as exc:
        return {"source": source, "error": str(exc)}
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    calls, first_assistant = _tool_calls(rows)
    files_read = [
        {
            "path": c["args"].get(_READ_TOOLS[c["tool"]]),
            "tool": c["tool"],
            "offset": c["args"].get("offset"),
            "limit": c["args"].get("limit"),
            "result_bytes": c["result_bytes"],
            "t": c["t"],
        }
        for c in calls
        if c["tool"] in _READ_TOOLS and c["args"].get(_READ_TOOLS[c["tool"]])
    ]
    return {
        "source": source,
        "first_assistant_message_at": first_assistant,
        "tool_calls": calls[:MAX_LISTED],
        "tool_call_counts": dict(Counter(c["tool"] for c in calls)),
        "files_read": files_read[:MAX_LISTED],
        "bytes_read_via_file_tools": sum(f["result_bytes"] or 0 for f in files_read),
        "truncated": len(calls) > MAX_LISTED,
    }
