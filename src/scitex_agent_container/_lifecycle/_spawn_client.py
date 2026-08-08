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

# DERIVED from the server's DECLARED answer-by deadline, never hand-picked.
# ``agents_start`` now GUARANTEES an answer within ``AGENT_START_DEADLINE_S``:
# 200/502 when the outcome is known, 202 when the spawn is still in flight — so
# a client only has to outlive that deadline plus transport, and
# ``client_timeout_for`` owns the margin. One constant, one derivation.
#
# This replaces TWO independently-picked constants (grace 20s there, budget 30s
# here) that had to stay ordered with no reviewer of either file able to see the
# other. Measured 2026-08-09: observed POST 21.97s — eight seconds of headroom
# on a host the server's own comment describes as idling at load 60-70. When
# that flips, the caller gets a TIMEOUT, which carries no status, so "slow" /
# "dead" / "already succeeded" become indistinguishable on a MUTATING route:
# that day the spawn had SUCCEEDED and was reported failed, and the natural
# retry can start a SECOND agent. Raising this number could not have fixed it —
# the server's wait is unbounded by construction (the OAuth settle window is
# held INSIDE an exclusive flock), so the fix had to be the server learning to
# SAY "still in flight". ``_handler_deadline`` is import-free (stdlib only).
from .._listen._handler_deadline import client_timeout_for

_DEFAULT_TIMEOUT_S = client_timeout_for()


# Request-construction plumbing — where do I send this, and as whom — now lives
# in ._listen_client_resolve. It was never spawn-specific: _host_exec_client
# already imported _parse_body / _resolve_base_url / _resolve_bearer FROM HERE,
# which is a module borrowing plumbing from a sibling because that is where it
# happened to get written down first. Re-exported so every existing import path
# (_host_exec_client, the MCP tools, _in_sif_broker, cli_pkg/lifecycle/_twin and
# the tests) keeps working byte-identically.
from ._listen_client_resolve import (  # noqa: F401
    SpawnRequestError,
    _parse_body,
    _read_bearer_token_file,
    _resolve_base_url,
    _resolve_bearer,
    _resolve_caller,
)


def _http_error_message(child_name: str, status: int, parsed: Any) -> str:
    """Build a status-aware error message for a non-2xx listen response.

    A 401/403 means the request REACHED the listen but was refused on
    credentials — surface that as an explicit ``auth/bearer`` problem so
    the operator fixes the token, NOT as 'cannot reach / timed out'
    (the misreport this card fixes). 401 = missing/invalid bearer; 403 =
    valid bearer but the ACL gate denied the caller. Every other non-2xx
    keeps the generic 'rejected: HTTP <code>' shape.
    """
    if status == 401:
        return (
            f"spawn of {child_name!r} rejected: listen returned HTTP 401 "
            f"(auth/bearer) — the spawn POST reached the host listen but "
            f"the bearer token was missing or invalid. Ensure "
            f"SAC_LISTEN_BEARER is injected, or that the host token file "
            f"~/.scitex/agent-container/tokens/listen-<host>.token is "
            f"readable from inside the container. Server said: {parsed!r}"
        )
    if status == 403:
        return (
            f"spawn of {child_name!r} rejected: listen returned HTTP 403 "
            f"(auth/acl) — the bearer authenticated but the listen's "
            f"check_spawn ACL denied this caller. Server said: {parsed!r}"
        )
    return (
        f"spawn of {child_name!r} rejected: listen returned HTTP {status} ({parsed!r})"
    )


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
    assume_yes: bool = False,
    force: bool = False,
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
    assume_yes
        Forwarded as ``assume_yes: true`` in the POST body when set.
        Bug fix (2026-07-05, reported by paper-scitex-clew): the host's
        ``/agents`` handler shells a fresh ``sac agents start <name>``
        subprocess, which re-runs the SAME interactive
        refuse-without-``--yes`` gate (``cli_pkg/lifecycle/
        _start_single.py::should_preview_and_require_yes``) that the
        ORIGINAL in-SIF caller's own ``-y`` already satisfied. Before
        this field existed there was no way for that consent to reach
        the host subprocess, so a brokered ``sac agents start <name>
        -y`` run from inside a container ALWAYS hit "refusing to start
        ... without --yes/-y" even though ``-y`` was explicitly passed
        at the top of the call chain. This does NOT weaken the
        human-at-a-TTY default-refuse safety net — it only lets the
        brokered/automated path assert consent that was already given.
    force
        Forwarded as ``force: true`` in the POST body when set. The host
        listen's ``/agents`` handler appends ``--force`` to its inner
        ``sac agents start`` argv, so a still-running agent is TORN DOWN
        and replaced instead of hitting the idempotent "already running →
        no-op" branch.

        Silent-degradation fix (incident 2026-07-12, scitex-storage): a
        RESTART issued from inside a SIF reaches ``agent_start(force=True)``,
        which brokers to the host — and before this field existed, ``force``
        was DROPPED at that boundary. The host then ran a plain
        ``sac agents start <name>``, saw the agent already up, no-op'd,
        printed "SUCC: <name> started" and exited 0. The restart reported
        success while nothing whatsoever cycled: same process, same pid,
        same stale credentials. Because no NEW container was launched, no
        ``apptainer_pid`` file appeared either, which is what tripped the
        listen's ``post_ack_no_apptainer_pid`` probe.

        Back-compat: only emitted when truthy, so a pre-fix host simply
        ignores the absent field and behaves exactly as before.

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
    # Consent-propagation fix (2026-07-05, paper-scitex-clew report): only
    # emit the key when truthy, same back-compat rationale as foreground/
    # one_shot above — pre-fix brokers simply ignore an absent field.
    if assume_yes:
        body["assume_yes"] = True
    # Silent-degradation fix (incident 2026-07-12): carry the caller's
    # ``force`` across the broker boundary. Dropping it turned an in-SIF
    # RESTART into a plain host-side start that no-op'd over the live agent
    # and still reported success. Same truthy-only back-compat rationale as
    # the fields above.
    if force:
        body["force"] = True

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
        # A real HTTP response arrived — the listen is REACHABLE, this is
        # NOT a transport failure. Read the body so the caller sees the
        # server's reason verbatim (ACL deny / auth error carry it).
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
            _http_error_message(child_name, exc.code, parsed),
            status=exc.code,
            body=parsed,
        ) from exc
    except (urlerror.URLError, OSError, ValueError) as exc:
        # No HTTP exchange happened — connection refused / DNS / timeout.
        # A 401/403 is NOT routed here: ``HTTPError`` (a URLError subclass)
        # is caught above first, so an authenticated-but-rejected request
        # never gets misreported as 'cannot reach / timed out'.
        #
        # But "no response on THIS route" is NOT yet "the daemon is
        # unreachable" — the old text asserted "unreachable; it may be
        # flapping" and was WRONG; the daemon was answering in 0.18s.
        #
        # This comment used to explain the split as a shared worker pool that
        # the public health path bypasses. THAT IS REFUTED (scitex-dev,
        # 2026-08-04): ``POST /v1/host_exec`` is authenticated and answered in
        # ~2.4s while ``POST /agents`` hung, same daemon, same minutes. The
        # failures track the ``/agents`` PREFIX, not authentication. So probe
        # BOTH the public path and an authenticated route on another prefix,
        # and let the two readings pick the message. See ._listen_probe.
        from ._listen_probe import (
            probe_listen_authed,
            probe_listen_health,
            transport_failure_message,
        )

        probe = probe_listen_health(base, opener=opener)
        authed = probe_listen_authed(base, tok, opener=opener)
        logger.warning(
            "spawn_client: POST %s transport error: %s (probe: listen "
            "serving=%s status=%s in %.2fs; authed serving=%s status=%s)",
            url,
            exc,
            probe.serving,
            probe.status,
            probe.elapsed_s,
            None if authed is None else authed.serving,
            None if authed is None else authed.status,
        )
        raise SpawnRequestError(
            transport_failure_message(
                verb="spawn",
                name=child_name,
                base=base,
                route="POST /agents",
                exc=exc,
                timeout_s=timeout_s,
                probe=probe,
                authed_probe=authed,
            )
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
            _http_error_message(child_name, status, parsed),
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
    if status == 202:
        # ACCEPTED, not completed: the server reached its declared deadline with
        # the spawn STILL IN FLIGHT, and said so. This is the honest answer that
        # REPLACES a client-side timeout, so two things must not happen to it.
        # It is not "the agent is running" — the outcome is genuinely unknown and
        # ``parsed["poll"]`` carries the route that will know. And it must NOT be
        # retried: a retry on a MUTATING route is exactly what starts a SECOND
        # agent, which is the damage the old timeout-as-error-channel caused.
        logger.info(
            "spawn_client: POST %s -> 202 accepted (phase=%s); spawn in flight, "
            "poll %s",
            url,
            parsed.get("phase"),
            parsed.get("poll"),
        )
    return parsed
