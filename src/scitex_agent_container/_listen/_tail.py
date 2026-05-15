"""SSE tail of per-agent ``session.jsonl`` (extracted from ``server.py``).

The handler ``GET /agents/<name>/tail?since=<iso>&follow=<bool>`` streams
each JSONL record as an SSE frame and (when ``follow=true``) keeps the
file open, emitting heartbeats every 15s while idle.
"""

from __future__ import annotations

import asyncio
import json as _json
import os
from datetime import datetime
from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse


def _sse_frame(event: str | None, data: str) -> bytes:
    head = f"event: {event}\n" if event else ""
    return (head + f"data: {data}\n\n").encode("utf-8")


def _parse_iso_ts(value: str) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    s = value.rstrip("Z")
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _record_ts(record: dict) -> datetime | None:
    for key in ("ts", "timestamp"):
        raw = record.get(key)
        if raw is None:
            continue
        parsed = _parse_iso_ts(raw) if isinstance(raw, str) else None
        if parsed is not None:
            return parsed
    return None


def _runtime_session_jsonl(name: str) -> Path:
    return (
        Path(os.path.expanduser("~"))
        / ".scitex"
        / "agent-container"
        / "runtime"
        / name
        / "session.jsonl"
    )


async def _stream_tail(
    path: Path,
    since: datetime | None,
    follow: bool,
    heartbeat_interval: float = 15.0,
    poll_interval: float = 0.5,
):
    line_no = 0
    seen_since = since is None
    if not path.is_file():
        if not follow:
            return

    while not path.is_file():
        await asyncio.sleep(poll_interval)

    last_heartbeat = asyncio.get_event_loop().time()
    with path.open("r", encoding="utf-8") as fh:
        while True:
            line = fh.readline()
            if line:
                line_no += 1
                line = line.rstrip("\n")
                if not line:
                    continue
                try:
                    record = _json.loads(line)
                except _json.JSONDecodeError:
                    record = {"raw": line}

                if since is not None:
                    rec_ts = _record_ts(record) if isinstance(record, dict) else None
                    if rec_ts is None:
                        if not seen_since:
                            continue
                    elif rec_ts < since:
                        continue
                    else:
                        seen_since = True

                payload = _json.dumps({"line_no": line_no, "record": record})
                yield _sse_frame(None, payload)
                last_heartbeat = asyncio.get_event_loop().time()
                continue

            if not follow:
                return
            now = asyncio.get_event_loop().time()
            if now - last_heartbeat >= heartbeat_interval:
                yield b": keep-alive\n\n"
                last_heartbeat = now
            try:
                await asyncio.sleep(poll_interval)
            except (asyncio.CancelledError, GeneratorExit):
                raise


async def agent_tail(request: Request) -> Response:
    name = request.path_params["name"]
    since_raw = request.query_params.get("since")
    follow_raw = request.query_params.get("follow", "false")
    follow = str(follow_raw).lower() in ("1", "true", "yes")
    since = _parse_iso_ts(since_raw) if since_raw else None

    path = _runtime_session_jsonl(name)
    if not follow and not path.is_file():
        return JSONResponse(
            {"error": f"no session.jsonl for {name!r}"}, status_code=404
        )

    return StreamingResponse(
        _stream_tail(path, since, follow),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
