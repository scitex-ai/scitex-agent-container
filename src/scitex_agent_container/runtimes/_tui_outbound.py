"""Outbound completion-report plumbing for ``runtime: tui`` agents (DB-backed).

INBOUND wakes are delivered by :mod:`_tui_turn_bridge`; this is the
SYMMETRIC outbound half — the dispatch-correlated completion report a TUI
agent owes its requester, at SDK parity with
:func:`_runners._session_completion.push_completion`
(``{agent, dispatch_id, status, summary}`` over the bus, persisted to
``channel_events``, closing the sender's dispatch-ledger row).

A TUI has no in-process turn envelope, so the requester identity
(``from_agent`` + ``dispatch_id``) cannot ride the tmux-injected text, and
the pane gives no reliable turn-complete signal. So:

* the bridge RECORDS each requester-bearing inbound wake into the
  DB-backed inbound ledger (:mod:`_state.inbound_ledger` — the receiver
  mirror of the sender's :mod:`_state.dispatch_ledger`), via
  :func:`record_dispatch`;
* on claude's NATIVE ``Stop`` (a reliable turn-complete signal), a Stop
  hook calls :func:`flush_one_completion`, which atomically CLAIMS the
  oldest pending inbound, pulls the reply summary from the transcript
  claude hands the hook, and pushes the completion to the requester.

The SAME ``state.db`` is bound into the container at ``/state/<name>``, so
the host-side bridge writer and the in-container hook reader share it
(``open_db`` WAL + ``busy_timeout`` make the cross-process access safe).
This is the sac state store doing what it is for — durable, ACID,
queryable communication state — not an ad-hoc side file.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

__all__ = [
    "record_dispatch",
    "summarize_transcript",
    "flush_one_completion",
    "main",
]


def record_dispatch(
    *,
    agent: str,
    from_agent: str,
    dispatch_id: Optional[str] = None,
) -> dict[str, Any] | None:
    """Record a requester-bearing inbound wake into the inbound ledger.

    No-op (returns ``None``) when ``from_agent`` is empty — an operator
    ``sac agents send`` without a peer, or a boot turn, has nobody to
    report back to. Returns the ledger row's IDENTITY otherwise.

    ``db_path`` is gone: the ledger moved to PostgreSQL, so there is no
    state.db to point at. The bridge no longer needs the agent's host-side
    file bound into the container for this to work — an endpoint suffices.

    The return type changed from ``int`` to the identity mapping, and that is
    safe here rather than merely tolerable: measured 2026-08-20, no caller in
    this repo binds this function's return value at all.
    """
    if not from_agent:
        return None
    from .._state.inbound_ledger import record_inbound

    return record_inbound(
        agent=agent,
        from_agent=from_agent,
        dispatch_id=dispatch_id,
    )


def summarize_transcript(
    transcript_path: Path,
    *,
    cap: int = 2_000,
) -> tuple[str, str]:
    """Return ``(status, summary)`` from a claude session transcript.

    Reads the JSONL transcript claude hands the Stop hook and returns the
    LAST assistant text block as the summary (bounded to ``cap`` chars —
    the full text stays in the transcript). ``status`` is ``"success"``
    when an assistant reply is found and ``"unknown"`` otherwise — NEVER a
    fabricated success. Tolerant of schema drift: unparseable lines are
    skipped; several known assistant-content shapes are accepted.
    """
    from .._runners._session_completion import STATUS_SUCCESS, STATUS_UNKNOWN

    path = Path(transcript_path)
    if not path.is_file():
        return STATUS_UNKNOWN, ""
    last_text = ""
    try:
        raw_lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:  # stx-allow: fallback (reason: unreadable transcript → unknown status with no summary, never crash the Stop hook)
        return STATUS_UNKNOWN, ""
    for ln in raw_lines:
        ln = ln.strip()
        if not ln:
            continue
        try:
            rec = json.loads(ln)
        except json.JSONDecodeError:
            continue
        text = _assistant_text(rec)
        if text:
            last_text = text
    if not last_text:
        return STATUS_UNKNOWN, ""
    if len(last_text) > cap:
        last_text = last_text[:cap] + "…"
    return STATUS_SUCCESS, last_text


def _assistant_text(rec: Any) -> str:
    """Extract assistant text from one transcript record, else ''.

    Accepts the common claude transcript shapes:
      * ``{"type"|"role": "assistant", "message": {"content": [...]}}``
      * ``{"role": "assistant", "content": [...]|"..."}``
    Content blocks may be ``{"type": "text", "text": "..."}`` or bare
    strings. Anything else yields '' (skipped).
    """
    if not isinstance(rec, dict):
        return ""
    role = rec.get("role") or rec.get("type")
    if role != "assistant":
        return ""
    message = rec.get("message")
    content = (
        message.get("content") if isinstance(message, dict) else rec.get("content")
    )
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            t = block.get("text")
            if isinstance(t, str):
                parts.append(t)
    return "\n".join(p for p in parts if p).strip()


def flush_one_completion(
    *,
    agent: str,
    transcript_path: Path | None,
    listen_url: str,
    bearer: Optional[str],
    push_fn: Any = None,
) -> bool:
    """Claim the oldest pending inbound for ``agent`` and push its report.

    Returns ``True`` when a dispatch was claimed and a push attempted,
    ``False`` when none was pending (the common no-op — most turns have no
    requester). The claim is atomic (:func:`inbound_ledger.claim_oldest_pending`),
    so a Stop-hook retry or two concurrent hooks never double-report. On a
    push failure the row is settled ``failed`` and the error re-raised so
    the Stop hook surfaces it loudly; on success it is settled ``reported``.

    ``push_fn`` is a test seam ``(*, agent, requester, report, listen_url,
    bearer, dispatch_id) -> Any``; production uses
    :func:`_session_completion.push_completion` via ``asyncio.run``.
    """
    from .._state.inbound_ledger import (
        STATUS_FAILED,
        STATUS_REPORTED,
        claim_oldest_pending,
        mark_reported,
    )

    claimed = claim_oldest_pending(agent=agent)
    if claimed is None:
        return False
    # The claimed mapping IS the handle now — the ledger's autoincrement id
    # is gone, and nothing here needed it to be a number. See
    # `_state.inbound_ledger` for the measurement behind that.
    handle = claimed
    requester = str(claimed.get("from_agent") or "").strip()
    dispatch_id = claimed.get("dispatch_id")
    did = dispatch_id if isinstance(dispatch_id, str) and dispatch_id else None

    from .._runners._session_completion import build_completion_report

    status, summary = summarize_transcript(
        Path(transcript_path) if transcript_path else Path("/nonexistent")
    )
    report = build_completion_report(
        agent=agent, dispatch_id=did, status=status, summary_text=summary
    )

    if (
        push_fn is None
    ):  # pragma: no cover - production wires the real async push (asyncio.run + network); unit tests inject push_fn, the e2e path exercises this
        import asyncio

        from .._runners._session_completion import push_completion

        def _push(**kw: Any) -> Any:
            return asyncio.run(push_completion(**kw))

        push_fn = _push

    try:
        push_fn(
            agent=agent,
            requester=requester,
            report=report,
            listen_url=listen_url,
            bearer=bearer,
            dispatch_id=did,
        )
    except Exception:
        mark_reported(handle, status=STATUS_FAILED)
        raise
    mark_reported(handle, status=STATUS_REPORTED)
    log.info(
        "tui-outbound: completion report pushed to %s (dispatch_id=%s, status=%s)",
        requester,
        did,
        status,
    )
    return True


def main(argv: list[str] | None = None) -> int:
    """Stop-hook entrypoint: report the just-completed turn's completion.

    Invoked by the agent's in-container ``Stop`` hook, which pipes claude's
    Stop payload JSON on stdin. Resolves the agent name, ``state.db``,
    bus URL and bearer from the in-container env the runtime injects
    (``SCITEX_AGENT_CONTAINER_AGENT`` / ``…_STATE_DB`` /
    ``SAC_LISTEN_BASE_URL`` / ``SAC_LISTEN_BEARER``), reads
    ``transcript_path`` from stdin for the reply summary, and flushes the
    OLDEST pending inbound dispatch (one per ``Stop`` = one completed turn).

    Returns 0 always — a Stop hook must NOT wedge the agent's turn loop. A
    missing context (non-sac session) or an empty queue is a clean no-op;
    a delivery failure is logged loud (and the ledger row is marked
    ``failed`` by :func:`flush_one_completion`) but still exits 0.
    """
    import os
    import sys

    del argv
    transcript_path: Path | None = None
    try:
        raw = sys.stdin.read()
    except Exception:  # stx-allow: fallback (reason: no/closed stdin → summary degrades to unknown, never crash the hook)
        raw = ""
    if raw.strip():
        try:
            payload = json.loads(raw)
            tp = payload.get("transcript_path") if isinstance(payload, dict) else None
            if isinstance(tp, str) and tp:
                transcript_path = Path(tp)
        except json.JSONDecodeError:
            transcript_path = None

    agent = os.environ.get("SCITEX_AGENT_CONTAINER_AGENT", "").strip()
    listen_url = os.environ.get("SAC_LISTEN_BASE_URL", "").strip()
    bearer = os.environ.get("SAC_LISTEN_BEARER", "").strip() or None
    # SCITEX_AGENT_CONTAINER_STATE_DB IS NO LONGER PART OF THIS GATE, and
    # dropping it is the point rather than tidying. It named the file
    # the ledger used to live in; the ledger is PostgreSQL now and this path
    # reads nothing from it. Left in place, an agent without that variable
    # would silently stop reporting completions — the gate would be refusing
    # on a fact that no longer bears on whether the work can be done.
    #
    # The two that remain are the two this path actually needs: who to report
    # AS, and where the bus is.
    if not agent or not listen_url:
        # Not a wired sac TUI agent context (or no bus) — nothing to report.
        return 0
    try:
        flush_one_completion(
            agent=agent,
            transcript_path=transcript_path,
            listen_url=listen_url,
            bearer=bearer,
        )
    except Exception as exc:  # stx-allow: fallback (reason: a completion-push failure must not wedge the agent's turn loop; the row is already marked failed + this logs loud at WARNING to stderr and the rotating ~/.scitex/logging/runtime/scitex-<date>.log via scitex-logging, so the requester can read it there)
        log.warning("tui-outbound: completion flush failed: %s", exc)
    return 0


if __name__ == "__main__":  # pragma: no cover -- exercised as a Stop-hook subprocess
    raise SystemExit(main())
