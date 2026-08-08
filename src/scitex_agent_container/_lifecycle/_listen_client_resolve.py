"""Where do I send this, and as whom — shared ``sac listen`` client plumbing.

Extracted from :mod:`._spawn_client`, which had grown to hold two unrelated
responsibilities: the spawn CALL (``request_spawn`` — its body shape, status
handling and transport diagnosis) and the generic question of how to address a
listen server at all. Only the first is about spawning.

The evidence that the second was never spawn-specific is that other modules
already reached into the spawn client to borrow it —
``_host_exec_client.py`` imports ``_parse_body`` / ``_resolve_base_url`` /
``_resolve_bearer`` from it, which is a module borrowing plumbing from a
sibling because that is where it happened to be written down first.

NAMED FOR WHAT IT IS, not for the caller that owned the file. ``_resolve_base_url``
and ``_resolve_bearer`` are currently implemented FIVE times across the tree
(``_spawn_client``, ``_restart_client``, ``_in_sif_http_client``,
``_state/_acl_broker_client``, ``_listen/_card_event_delivery``), and
``_resolve_caller`` twice. That is the same structural defect as the agent
listing existing twice (CLI + listen), where a fix reached one copy and not the
other. Migrating the remaining four onto this module is a separate card; this
module exists in a shape that makes that migration a deletion rather than
another move.
"""

from __future__ import annotations

import json
from typing import Any

__all__ = [
    "SpawnRequestError",
    "_parse_body",
    "_read_bearer_token_file",
    "_resolve_base_url",
    "_resolve_bearer",
    "_resolve_caller",
]


class SpawnRequestError(RuntimeError):
    """Raised when the host-side spawn POST cannot be completed.

    Carries the structured failure information so the caller (CLI / MCP
    tool / log line) can show *why* the spawn failed without re-parsing
    a free-text message:

    * ``status`` — HTTP status code returned by the listen server
      (``None`` for transport errors before any HTTP exchange happened
      or for missing-env failures).
    * ``body`` — parsed response dict, or the raw text fallback if the
      body was not JSON, or ``None`` when no body was received.

    The fail-loud invariants this class encodes (ADR-0010 + handoff §0):

    * A 403 ACL deny is an ERROR for the caller — never silently
      swallowed. The reason from the server is forwarded verbatim in
      ``body``.
    * A transport error (host listen unreachable, refused, timed out)
      is an ERROR — never converted to "ok with empty result".
    * A missing ``SAC_LISTEN_BASE_URL`` is an ERROR — the container is
      misconfigured (the apptainer runtime forgot to inject it), and
      pretending the spawn succeeded would leave the lineage row
      unwritten on the host.
    """

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        body: Any = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


def _resolve_base_url(explicit: str | None) -> str:
    """Return the listen-server base URL or raise loudly.

    Resolution order: explicit argument → ``SAC_LISTEN_BASE_URL``
    (via :func:`_env.getenv` so the long-form alias also works).
    Trailing slash is stripped so callers can safely concatenate
    ``/agents``.
    """
    if explicit:
        return explicit.rstrip("/")
    from .._env import getenv

    raw = getenv("LISTEN_BASE_URL", "") or ""
    raw = raw.strip()
    if not raw:
        raise SpawnRequestError(
            "spawn request requires SAC_LISTEN_BASE_URL (the host-stable "
            "`sac listen` URL injected into the container by the apptainer "
            "runtime). Got empty/unset."
        )
    return raw.rstrip("/")


def _resolve_bearer(explicit: str | None) -> str | None:
    """Return the listen-server bearer token, or ``None`` if unset.

    Resolution order:

    1. ``explicit`` argument (tests pass a value, or ``""`` to force the
       unauthenticated branch).
    2. ``SAC_LISTEN_BEARER`` env var — injected by the apptainer runtime
       (:mod:`runtimes._apptainer_listen_env`) for agents whose spec
       registers the ``server:sac`` channel.
    3. The host bearer **token file** at
       ``~/.scitex/agent-container/tokens/listen-<host>.token`` — the
       same credential the listen server validates against
       (:func:`_listen.tokens.default_token_path`). This fallback is
       what makes :func:`request_spawn` authenticate even when the
       spawning agent's spec did NOT include ``server:sac`` (so the
       runtime injected only ``SAC_LISTEN_BASE_URL``, not the bearer).
       Without it, the spawn POST goes out unauthenticated and the
       listen server rejects it with 401 — the bug this resolver path
       closes (card sac-agent-cannot-spawn-agents-listen-7878-...).

    Unlike ``SAC_LISTEN_BASE_URL``, an absent bearer is NOT fatal here:
    the listen server may have been started with bearer auth disabled,
    in which case the request still goes through unauthenticated. The
    server enforces its own auth contract; we just forward whatever
    credential we can resolve.
    """
    if explicit is not None:
        return explicit or None
    from .._env import getenv

    raw = getenv("LISTEN_BEARER", "") or ""
    tok = raw.strip()
    if tok:
        return tok
    # Env unset/empty — fall back to the on-disk host token file, the
    # same path the listen server reads its accepted token from. The
    # bind that exposes ``~/.scitex/agent-container`` into the container
    # makes this readable from inside the SIF.
    return _read_bearer_token_file()


def _read_bearer_token_file() -> str | None:
    """Return the host listen bearer from its token file, or ``None``.

    Never raises — a missing/unreadable token file yields ``None`` so
    the caller proceeds (and the server's 401 then surfaces as a clear
    auth error via :func:`request_spawn`).
    """
    from .._listen.tokens import default_token_path, read_token

    return read_token(default_token_path())


def _resolve_caller(explicit: str | None) -> str | None:
    """Return the spawning agent's identity, or ``None`` for the admin path.

    Reuses the same resolution rule as the in-process
    :func:`_lifecycle._spawn_gate.resolve_spawn_caller`: read ``SAC_NAME``
    from the parent container's env. An empty string is normalised to
    ``None`` (admin / human-operator launch — always allowed by the
    server's current root-only ``check_spawn`` policy).
    """
    if explicit is not None:
        return explicit or None
    from ._spawn_gate import resolve_spawn_caller

    return resolve_spawn_caller()


def _parse_body(raw: bytes) -> Any:
    """Return JSON-parsed body or raw text fallback."""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        # stx-allow: fallback (reason: non-JSON body surfaced verbatim
        # to the caller via SpawnRequestError.body — never silently
        # dropped or converted into a fake success).
        return raw.decode("utf-8", errors="replace")
