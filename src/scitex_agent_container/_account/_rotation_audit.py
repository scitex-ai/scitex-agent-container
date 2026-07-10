"""Structured credential-rotation audit log.

Operator-mandated ESSENTIAL fix ("fix permanently as a system"): every
time a stored Claude account credential is rotated — refreshed, switched,
auto-rotated on quota, or snapshotted from a live login — this module
appends ONE structured JSONL record describing WHAT rotated FROM→TO and
WHY. It closes the "mystery expiry" gap: a credential that silently dies
now leaves a durable, greppable trail of every rotation that touched it.

Security contract (HARD)
------------------------
FULL tokens / refresh_tokens are NEVER written. Only an OPAQUE
``sha256:<12hex>`` fingerprint of a token is recorded, so a rotation of
the token material itself (FROM fingerprint → TO fingerprint) is visible
WITHOUT exposing the secret. :func:`fingerprint_token` is the only place
a token value is touched, and it emits a one-way hash prefix.

Durability
----------
Append-only JSONL at ``<accounts-store>/rotation-audit.jsonl`` — one JSON
object per line, created on first use. The write is best-effort and
NEVER raises: an audit failure must never break the actual rotation the
caller is performing.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import socket
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Filename of the append-only audit log, relative to the accounts store.
AUDIT_FILENAME = "rotation-audit.jsonl"


class _DynamicStderrHandler(logging.StreamHandler):
    """A ``StreamHandler`` that writes to the PROCESS stderr at EMIT time.

    Resolves ``sys.__stderr__`` (the original fd-2 stream) per emit, falling
    back to ``sys.stderr``. Targeting ``__stderr__`` keeps the audit line on
    the true process stderr — the stream agents capture into their logs —
    while deliberately bypassing any in-process Python-level stdout/stderr
    swap (e.g. a test harness' output capture). This guarantees the audit
    echo can never bleed into an unrelated command's captured stdout and
    corrupt a ``--json`` contract, without silencing the line for agents.
    """

    def emit(self, record: logging.LogRecord) -> None:
        self.stream = sys.__stderr__ or sys.stderr
        super().emit(record)


def _ensure_audit_logger() -> None:
    """Give the audit logger its OWN dedicated stderr handler, once.

    The audit line is emitted at INFO for operator/agent visibility, but it
    is deliberately kept OFF the ancestor/root logger chain
    (``propagate = False``): a credential rotation is frequently triggered
    from inside a ``--json`` CLI command whose stdout is a machine-parsed
    contract, and a stray INFO line riding an ancestor handler that happens
    to target stdout would corrupt that JSON. Owning a single dynamic-stderr
    handler guarantees the line lands on stderr (captured in agent logs) and
    NEVER on any command's stdout.
    """
    if getattr(logger, "_sac_audit_configured", False):
        return
    handler = _DynamicStderrHandler()
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter("[%(asctime)s] rotation-audit: %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger._sac_audit_configured = True  # type: ignore[attr-defined]

#: The closed set of rotation events. See module docstring / task spec.
#: ``reactive-rotate`` (task #13, op-2026-06-12-13) is the REACTIVE
#: sibling of ``auto-rotate``: a live 429/403/textual/auth signal —
#: classified via ``_account.rate_limit_classifier`` — forced the
#: rotation, as opposed to ``auto-rotate``'s periodic threshold poll.
#: See ``_account.rotate_account.ROTATE_EVENT``.
_KNOWN_EVENTS = frozenset(
    {"refresh", "switch", "auto-rotate", "reactive-rotate", "save", "sync-live"}
)


def fingerprint_token(token: str | None) -> str | None:
    """Return an OPAQUE, one-way fingerprint of a token, or ``None``.

    A ``sha256:<first-12-hex>`` prefix of the SHA-256 of the token. This
    is stable (same token → same fingerprint, so a FROM→TO rotation is
    visible) but non-reversible — the full secret is NEVER recoverable
    from the fingerprint, and the fingerprint never contains any
    substring of the token itself.

    ``None``/empty input yields ``None``.
    """
    if not token:
        return None
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return f"sha256:{digest[:12]}"


def _resolve_host() -> str:
    """Best-effort canonical host label for the audit record.

    Prefers the same resolver the rest of sac uses
    (``_state.state_db_hostname.resolve_host``) so the audit host matches
    what appears elsewhere; falls back to ``socket.gethostname()``.
    """
    # stx-allow: fallback (reason: host resolution is a cosmetic label on
    # the audit record; a resolver import/lookup failure must degrade to
    # the short hostname, never break the audit write.)
    try:
        from .._state.state_db_hostname import resolve_host

        return resolve_host(None)
    except Exception:  # stx-allow: fallback (reason: see inline comment)
        try:
            return socket.gethostname()
        except Exception:  # stx-allow: fallback (reason: see inline comment)
            return "unknown"


def log_rotation_event(
    *,
    store: Path,
    event: str,
    from_account: str | None,
    to_account: str | None,
    reason: str,
    from_token_fp: str | None = None,
    to_token_fp: str | None = None,
    refresh_token_fp: str | None = None,
    now: float | None = None,
    extra: dict[str, Any] | None = None,
) -> Path | None:
    """Append one rotation-audit record and echo it via the logger.

    Writes a single JSON object as one line to
    ``<store>/rotation-audit.jsonl`` (created if missing) capturing the
    five mandated fields plus host + opaque token fingerprints.

    Parameters
    ----------
    store
        The accounts store ROOT directory (the parent of the per-account
        dirs). The audit file is written directly under it. Passing the
        already-resolved store avoids re-walking the state cascade and
        keeps tests hermetic.
    event
        One of ``refresh`` / ``switch`` / ``auto-rotate`` / ``save`` /
        ``sync-live``. Unknown values are still written (forward-compat)
        but logged at WARNING.
    from_account
        Slug/email rotating away from (or the account being refreshed).
    to_account
        Slug/email rotating to. For ``refresh`` this equals
        ``from_account``.
    reason
        WHY the rotation happened (human/trigger string).
    from_token_fp, to_token_fp, refresh_token_fp
        OPAQUE ``sha256:<hex>`` fingerprints (from :func:`fingerprint_token`).
        Callers MUST pass fingerprints, never raw tokens.
    now
        Wall clock override (unix seconds) for tests.
    extra
        Optional extra JSON-serialisable fields merged into the record.

    Returns
    -------
    Path | None
        The audit file path on success, ``None`` on any failure (the
        write is best-effort — an audit failure never breaks a rotation).
    """
    now_ts = now if now is not None else time.time()
    ts_iso = datetime.fromtimestamp(now_ts, tz=timezone.utc).isoformat()

    if event not in _KNOWN_EVENTS:
        logger.warning(
            "rotation-audit: unknown event %r (recording anyway)", event
        )

    record: dict[str, Any] = {
        "timestamp_utc": ts_iso,
        "event": event,
        "from_account": from_account,
        "to_account": to_account,
        "reason": reason,
        "host": _resolve_host(),
        "from_token_fp": from_token_fp,
        "to_token_fp": to_token_fp,
        "refresh_token_fp": refresh_token_fp,
        "pid": os.getpid(),
    }
    if extra:
        for key, val in extra.items():
            record.setdefault(key, val)

    # stx-allow: fallback (reason: the audit write is a durable side-record;
    # a filesystem / serialisation failure must NEVER break the actual
    # credential rotation the caller is performing — degrade to log-only.)
    try:
        store_path = Path(store)
        store_path.mkdir(parents=True, exist_ok=True)
        audit_path = store_path / AUDIT_FILENAME
        line = json.dumps(record, ensure_ascii=False)
        with audit_path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        _ensure_audit_logger()
        logger.info(
            "rotation-audit: event=%s from=%s to=%s reason=%s",
            event,
            from_account,
            to_account,
            reason,
        )
        return audit_path
    except Exception as exc:  # stx-allow: fallback (reason: see inline comment)
        logger.warning(
            "rotation-audit: failed to append record (event=%s): %s",
            event,
            exc,
        )
        return None


__all__ = ["AUDIT_FILENAME", "fingerprint_token", "log_rotation_event"]
