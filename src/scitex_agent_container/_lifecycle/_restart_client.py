"""In-container HTTP client for the host-side restart-proxy endpoint.

The restart sibling of :mod:`._spawn_client`. Lets an agent running
INSIDE an apptainer container ask the host's ``sac listen`` to restart
a peer agent on the bare host. ``sac agents restart <name>`` resolves
a peer by its LOCAL registry row + local ``state.db`` — from inside a
SIF that lookup fails ("Agent not found in registry"), so the restart
must be brokered to the host the same way spawn is.

Transport contract
------------------

Endpoint: ``POST {SAC_LISTEN_BASE_URL}/agents/{name}/restart``.

Auth: ``Authorization: Bearer {SAC_LISTEN_BEARER}`` — same credential
the spawn bypass uses (env-injected by the apptainer runtime, with a
fallback to the on-disk host token file). Missing
``SAC_LISTEN_BASE_URL`` raises :class:`RestartRequestError` (fail loud
— there is no useful default).

Body::

    {"caller": "<requesting-agent>"}   # auto-resolved from SAC_NAME

The server's ``agent_restart`` handler runs
:func:`._listen._acl.check_lineage_acl` (the MANAGE gate) BEFORE any
runtime work. On allow it shells ``sac agents restart <name> --yes
--json`` on the bare host. On deny it returns 403 with an ACL reason;
we surface that verbatim as :class:`RestartRequestError` (status=403)
so the caller fails LOUD rather than swallowing the deny.

Stdlib-only on purpose — mirrors :mod:`._spawn_client`: ``urllib``
(no ``httpx``) + an injectable ``opener`` callable for tests.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable
from urllib import error as urlerror
from urllib import request as urlrequest

logger = logging.getLogger(__name__)

__all__ = ["RestartRequestError", "request_restart"]

_DEFAULT_TIMEOUT_S = 60.0


class RestartRequestError(RuntimeError):
    """Raised when the host-side restart POST cannot be completed.

    Carries the structured failure info (``status`` + ``body``) so the
    caller (CLI / MCP tool / log line) can show *why* the restart failed
    without re-parsing a free-text message. Mirrors
    :class:`._spawn_client.SpawnRequestError`:

    * a 403 ACL deny is an ERROR — never silently swallowed; the
      server's reason is forwarded verbatim in ``body``,
    * a transport error (host listen unreachable) is an ERROR — never
      converted to "ok with empty result",
    * a missing ``SAC_LISTEN_BASE_URL`` is an ERROR — the container is
      misconfigured.
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

    Resolution order: explicit argument → ``SAC_LISTEN_BASE_URL`` (via
    :func:`_env.getenv` so the long-form alias also works). Trailing
    slash is stripped so callers can safely concatenate the path.
    """
    if explicit:
        return explicit.rstrip("/")
    from .._env import getenv

    raw = (getenv("LISTEN_BASE_URL", "") or "").strip()
    if not raw:
        raise RestartRequestError(
            "restart request requires SAC_LISTEN_BASE_URL (the host-stable "
            "`sac listen` URL injected into the container by the apptainer "
            "runtime). Got empty/unset."
        )
    return raw.rstrip("/")


def _resolve_bearer(explicit: str | None) -> str | None:
    """Return the listen-server bearer token, or ``None`` if unset.

    Identical resolution to :func:`._spawn_client._resolve_bearer`:
    explicit arg → ``SAC_LISTEN_BEARER`` env → the on-disk host token
    file. An absent bearer is NOT fatal (the listen may run with bearer
    auth disabled); the server enforces its own auth contract.
    """
    if explicit is not None:
        return explicit or None
    from .._env import getenv

    tok = (getenv("LISTEN_BEARER", "") or "").strip()
    if tok:
        return tok
    from .._listen.tokens import default_token_path, read_token

    return read_token(default_token_path())


def _resolve_caller(explicit: str | None) -> str | None:
    """Return the requesting agent's identity, or ``None`` for admin.

    Reuses the spawn caller rule: read ``SAC_NAME`` from the container
    env (via :func:`._spawn_gate.resolve_spawn_caller`). An empty string
    normalises to ``None`` (administrative / operator path).
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
        # stx-allow: fallback (reason: non-JSON body surfaced verbatim to
        # the caller via RestartRequestError.body — never silently dropped
        # or converted into a fake success).
        return raw.decode("utf-8", errors="replace")


def _http_error_message(name: str, status: int, parsed: Any) -> str:
    """Build a status-aware error message for a non-2xx listen response.

    A 401/403 means the request REACHED the listen but was refused on
    credentials/ACL — surface that explicitly so the operator fixes the
    token or the manage authority, not "cannot reach / timed out".
    """
    if status == 401:
        return (
            f"restart of {name!r} rejected: listen returned HTTP 401 "
            f"(auth/bearer) — the restart POST reached the host listen but "
            f"the bearer token was missing or invalid. Ensure "
            f"SAC_LISTEN_BEARER is injected, or that the host token file is "
            f"readable from inside the container. Server said: {parsed!r}"
        )
    if status == 403:
        return (
            f"restart of {name!r} rejected: listen returned HTTP 403 "
            f"(auth/acl) — the bearer authenticated but the listen's "
            f"check_lineage_acl MANAGE gate denied this caller. Server "
            f"said: {parsed!r}"
        )
    return (
        f"restart of {name!r} rejected: listen returned HTTP "
        f"{status} ({parsed!r})"
    )


def request_restart(
    name: str,
    *,
    caller: str | None = None,
    fresh: bool = False,
    base_url: str | None = None,
    bearer: str | None = None,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
    opener: Callable | None = None,
) -> dict:
    """POST a restart request to the host listen server; FAIL LOUD on error.

    Parameters
    ----------
    name
        The agent to restart (must be registered / resolvable on the
        host — the restart runs on the bare host where its row lives).
    caller
        The requesting agent's identity for the listen-server's
        ``check_lineage_acl`` MANAGE gate. Defaults to ``SAC_NAME`` from
        the container env via :func:`_resolve_caller`.
    fresh
        When True, ask the host to start a NEW Claude session
        (``start --force --fresh``) instead of a plain resuming restart —
        the deterministic recovery for an agent wedged on a boot prompt
        whose queued-input buffer returns on every resuming restart.
    base_url
        Override ``SAC_LISTEN_BASE_URL``. Tests pass an in-process
        listen URL; production passes ``None``.
    bearer
        Override ``SAC_LISTEN_BEARER``. Tests pass an explicit value or
        ``""`` to force the unauthenticated branch.
    timeout_s
        Per-request HTTP timeout (seconds). Defaults to 60 — a restart
        does a stop + settle + start on the host, longer than a spawn.
    opener
        Optional ``urllib.request.urlopen``-shaped callable for tests.

    Returns
    -------
    dict
        The server's parsed JSON body on 2xx — the ``agent_restart``
        handler returns ``{name, returncode, stdout, stderr}``.

    Raises
    ------
    RestartRequestError
        On missing base URL, transport failure, non-2xx HTTP status
        (including 403 ACL deny), or a malformed (non-object) body.
    """
    if not isinstance(name, str) or not name:
        raise RestartRequestError("name must be a non-empty string")

    base = _resolve_base_url(base_url)
    tok = _resolve_bearer(bearer)
    resolved_caller = _resolve_caller(caller)

    body: dict[str, Any] = {}
    if resolved_caller:
        body["caller"] = resolved_caller
    # Omitted (default) keeps the byte-identical plain-restart body; set only
    # when a fresh (no-resume) restart is requested.
    if fresh:
        body["fresh"] = True

    payload = json.dumps(body).encode("utf-8")
    url = f"{base}/agents/{name}/restart"
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
        # A real HTTP response arrived — the listen is REACHABLE. Read the
        # body so the caller sees the server's reason verbatim (the ACL
        # deny / auth error carry it).
        raw_body = b""
        try:
            raw_body = exc.read() or b""
        except Exception:  # stx-allow: defensive — body read on a half-closed HTTPError stream may itself fail; we already have status + URL.  # noqa: BLE001
            pass
        parsed = _parse_body(raw_body)
        logger.warning(
            "restart_client: POST %s returned HTTP %s body=%r",
            url,
            exc.code,
            parsed,
        )
        raise RestartRequestError(
            _http_error_message(name, exc.code, parsed),
            status=exc.code,
            body=parsed,
        ) from exc
    except (urlerror.URLError, OSError, ValueError) as exc:
        # No HTTP exchange happened — connection refused / DNS / timeout.
        # A 401/403 is NOT routed here (HTTPError is caught above first),
        # so an authenticated-but-rejected request never gets misreported
        # as 'cannot reach / timed out'.
        #
        # MEASURE before naming the cause. This used to assert "the host
        # listen broker is unreachable; it may be flapping" — a diagnosis
        # nobody had measured, and (2026-07-14) a WRONG one: the daemon was
        # answering an unauthenticated GET in 0.18s while this authenticated
        # POST hung. Probe the cheap public path and let the EVIDENCE pick
        # the message. See ._listen_probe.
        from ._listen_probe import probe_listen_health, transport_failure_message

        probe = probe_listen_health(base, opener=opener)
        logger.warning(
            "restart_client: POST %s transport error: %s (probe: listen "
            "serving=%s status=%s in %.2fs)",
            url,
            exc,
            probe.serving,
            probe.status,
            probe.elapsed_s,
        )
        raise RestartRequestError(
            transport_failure_message(
                verb="restart",
                name=name,
                base=base,
                route=f"POST /agents/{name}/restart",
                exc=exc,
                timeout_s=timeout_s,
                probe=probe,
            )
        ) from exc

    parsed = _parse_body(raw)
    if status < 200 or status >= 300:
        logger.warning(
            "restart_client: POST %s returned HTTP %s body=%r",
            url,
            status,
            parsed,
        )
        raise RestartRequestError(
            _http_error_message(name, status, parsed),
            status=status,
            body=parsed,
        )

    if not isinstance(parsed, dict):
        raise RestartRequestError(
            f"restart of {name!r} succeeded transport-wise but the listen "
            f"response was not a JSON object: {parsed!r}",
            status=status,
            body=parsed,
        )
    return parsed
