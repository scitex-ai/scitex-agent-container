"""In-container HTTP client for the host-side spawn-proxy endpoint.

Lets an agent running INSIDE an apptainer container ask the host's
``sac listen`` to spawn a child agent on the bare host. This is the
canonical (ADR-0010 mechanism #3) spawn path — the only sanctioned
agent-driven spawn, because the listen-server gate
(:func:`_listen._acl.check_spawn`) and the lineage recorder
(:func:`_state.state_db_nodes.record_lineage`) run on every accepted
request. Apptainer-in-apptainer is avoided structurally: the child is
booted on the bare host, never nested.

Transport contract
------------------

Endpoint: ``POST {SAC_LISTEN_BASE_URL}/agents``
(canonical control-plane route, see :mod:`_listen.server`).

Auth: ``Authorization: Bearer {SAC_LISTEN_BEARER}`` — both injected
into the container by :mod:`runtimes._apptainer_listen_env` alongside
the channel-adapter env. Missing ``SAC_LISTEN_BASE_URL`` raises
:class:`SpawnRequestError` (fail loud — there is no useful default).

Body:

    {"name": "<child>",
     "caller": "<spawning-agent>",        # auto-resolved from SAC_NAME
     "spec": {...},                       # optional inline spec
     "overwrite": false}                  # optional, default false

The server's ``agents_start`` handler runs ``check_spawn(caller=...)``
BEFORE any runtime work. On allow it records the ``caller → child``
lineage edge and shells ``sac agent start <name>`` on the bare host.
On deny it returns 403 with an ACL reason; we surface that verbatim
as :class:`SpawnRequestError` (status=403) so the caller fails LOUD
rather than swallowing the deny.

Stdlib-only on purpose
----------------------

Mirrors :mod:`_network.hub_client`: ``urllib.request`` (no ``httpx``)
+ injected ``opener`` callable for tests. Containers may not have the
heavier HTTP stack loaded at MCP-tool invocation time, and a spawn
request is a single one-shot POST — no streaming, no async.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable
from urllib import error as urlerror
from urllib import request as urlrequest

logger = logging.getLogger(__name__)

__all__ = ["SpawnRequestError", "request_spawn"]

_DEFAULT_TIMEOUT_S = 30.0


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

    Unlike ``SAC_LISTEN_BASE_URL``, an absent bearer is NOT fatal here:
    the listen server may have been started with bearer auth disabled,
    in which case the request still goes through. The server enforces
    its own auth contract; we just forward whatever credential the
    runtime injected.
    """
    if explicit is not None:
        return explicit or None
    from .._env import getenv

    raw = getenv("LISTEN_BEARER", "") or ""
    return raw.strip() or None


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


def request_spawn(
    child_name: str,
    *,
    caller: str | None = None,
    spec: dict | None = None,
    overwrite: bool = False,
    base_url: str | None = None,
    bearer: str | None = None,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
    opener: Callable | None = None,
    foreground: bool = False,
    one_shot: bool = False,
) -> dict:
    """POST a spawn request to the host listen server; FAIL LOUD on error.

    Parameters
    ----------
    child_name
        The agent to start (must already be registered on the host, OR
        passed inline via ``spec``).
    caller
        The spawning agent's identity for the listen-server's
        ``check_spawn`` gate + lineage edge. Defaults to ``SAC_NAME``
        from the container env via :func:`_resolve_caller`.
    spec
        Optional inline spec dict (``{apiVersion, kind, spec}``) — the
        server materialises it under
        ``~/.scitex/agent-container/agents/<name>/spec.yaml`` and then
        starts it. Use for ephemeral / per-turn children.
    overwrite
        Forwarded as the ``overwrite`` body field; only meaningful with
        ``spec``. Defaults to ``False`` (409 on clash).
    base_url
        Override ``SAC_LISTEN_BASE_URL``. Tests pass an in-process
        listen-server URL; production passes ``None``.
    bearer
        Override ``SAC_LISTEN_BEARER``. Tests pass either an explicit
        value or ``""`` to force the unauthenticated branch.
    timeout_s
        Per-request HTTP timeout (seconds). Defaults to 30 — long enough
        for the server's ``sac agent start`` subprocess to return.
    opener
        Optional ``urllib.request.urlopen``-shaped callable. Default
        ``urlrequest.urlopen``; tests inject a fake opener that returns
        a ``urllib.response``-shaped object (no monkeypatching).
    foreground
        Forwarded as ``foreground: true`` in the POST body when set.
        The host listen's ``/agents`` handler appends ``--foreground``
        to its inner ``sac agents start`` argv, so the apptainer runtime
        takes the foreground branch (``subprocess.run`` blocks until the
        capsule exits) instead of the background branch (Popen + return
        rc=0 immediately). Required for the one-shot cohort case so the
        capsule's actual rc + stderr surface up the chain into
        ``STARTUP_FAILED.stderr_tail`` (clew dogfood 2026-06-06: without
        this, the post-ack liveness probe sees a still-alive Popen pid
        and reports SUCC, but the capsule dies later, silently).
    one_shot
        Forwarded as ``one_shot: true`` in the POST body when set.
        The host listen propagates ``--one-shot`` to its inner argv;
        the capsule runs one SDK turn (its ``startup_prompts``) and
        exits. Pairs naturally with ``foreground=True`` for the
        cohort capsule shape.

    Returns
    -------
    dict
        The server's parsed JSON body on 2xx — the ``agents_start``
        handler returns ``{name, returncode, stdout, stderr}``. The
        caller can branch on ``returncode``; ``returncode != 0`` means
        the gate passed but the bare-host ``sac agent start`` itself
        failed (e.g. a spec validation error on the host).

    Raises
    ------
    SpawnRequestError
        On missing base URL, transport failure, non-2xx HTTP status
        (including 403 ACL deny), or malformed-but-otherwise-OK body
        the server itself would reject.
    """
    if not isinstance(child_name, str) or not child_name:
        raise SpawnRequestError("child_name must be a non-empty string")

    base = _resolve_base_url(base_url)
    tok = _resolve_bearer(bearer)
    resolved_caller = _resolve_caller(caller)

    body: dict[str, Any] = {"name": child_name}
    if resolved_caller:
        body["caller"] = resolved_caller
    if spec is not None:
        body["spec"] = spec
        body["overwrite"] = bool(overwrite)
    # Cohort one-shot diagnostic (clew dogfood 2026-06-06, lead msg
    # d96a468c): only emit the keys when truthy so the wire shape is
    # back-compat with pre-α brokers (they ignore the absent fields).
    if foreground:
        body["foreground"] = True
    if one_shot:
        body["one_shot"] = True

    payload = json.dumps(body).encode("utf-8")
    url = f"{base}/agents"
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if tok:
        headers["Authorization"] = f"Bearer {tok}"

    req = urlrequest.Request(url, data=payload, method="POST", headers=headers)
    opener_fn = opener if opener is not None else urlrequest.urlopen

    try:
        with opener_fn(req, timeout=timeout_s) as resp:
            raw = resp.read()
            status = int(getattr(resp, "status", 200))
    except urlerror.HTTPError as exc:
        # Non-2xx — read the body if any so the caller sees the
        # server's reason verbatim (ACL deny carries it).
        raw_body = b""
        try:
            raw_body = exc.read() or b""
        except Exception:  # stx-allow: defensive — body read on a half-closed HTTPError stream may itself fail; we already have status + URL.  # noqa: BLE001
            pass
        parsed = _parse_body(raw_body)
        logger.warning(
            "spawn_client: POST %s returned HTTP %s body=%r",
            url,
            exc.code,
            parsed,
        )
        raise SpawnRequestError(
            f"spawn of {child_name!r} rejected: listen returned HTTP "
            f"{exc.code} ({parsed!r})",
            status=exc.code,
            body=parsed,
        ) from exc
    except (urlerror.URLError, OSError, ValueError) as exc:
        logger.warning("spawn_client: POST %s transport error: %s", url, exc)
        raise SpawnRequestError(
            f"spawn of {child_name!r} failed: cannot reach listen at {base!r} ({exc})"
        ) from exc

    parsed = _parse_body(raw)
    if status < 200 or status >= 300:
        # Some opener implementations don't raise HTTPError for non-2xx
        # — guard explicitly so a misbehaving server can't masquerade
        # as success.
        logger.warning(
            "spawn_client: POST %s returned HTTP %s body=%r",
            url,
            status,
            parsed,
        )
        raise SpawnRequestError(
            f"spawn of {child_name!r} rejected: listen returned HTTP "
            f"{status} ({parsed!r})",
            status=status,
            body=parsed,
        )

    if not isinstance(parsed, dict):
        raise SpawnRequestError(
            f"spawn of {child_name!r} succeeded transport-wise but the "
            f"listen response was not a JSON object: {parsed!r}",
            status=status,
            body=parsed,
        )
    return parsed
