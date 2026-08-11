"""SAC-from-SAC broker — in-SIF detection + host-listen spawn POST.

Operator-mandated 2026-06-01: an agent running INSIDE an apptainer SIF
must NOT try to ``apptainer exec`` a child (no nested apptainer on most
HPCs / on the supported deployment shape). Instead, when ``sac agents
start <child>`` (or the ``agent.start`` API) is invoked inside a SIF,
the spawn MUST be brokered through the host-side ``sac listen`` server,
which lives on the bare host, owns container lifecycle, and shells the
real ``sac agent start`` against the bare host's apptainer.

This module is the in-SIF half of that broker. Two collaborators:

* :func:`is_in_sif` — pure env detection (``APPTAINER_CONTAINER`` or the
  legacy ``SINGULARITY_CONTAINER`` set non-empty).
* :func:`broker_start_to_host` — thin wrapper over
  :func:`_lifecycle._spawn_client.request_spawn` that surfaces a
  distinct :class:`InSifBrokerError` so the integration point in
  :func:`_lifecycle._start.agent_start` can fail loud with one error
  type instead of leaking ``SpawnRequestError``.

Wiring contract (see ``runtimes._apptainer_listen_env``):

* ``SAC_LISTEN_BASE_URL`` and ``SAC_LISTEN_BEARER`` are injected at
  container launch by the apptainer runtime — both are already in
  place for every sac-managed agent.
* The host listen ``POST /agents`` handler re-runs
  :func:`_listen._acl.check_spawn` and records the ``caller → child``
  lineage edge, so the gate is enforced by the host (the single source
  of truth) regardless of how the in-SIF caller asked.

FAIL-LOUD invariants (ADR-0010 / handoff §0):

* Missing ``SAC_LISTEN_BASE_URL`` → :class:`InSifBrokerError` with the
  env var name in the message (operator must fix the runtime
  injection); never silently treated as "skip the broker".
* Host listen unreachable → :class:`InSifBrokerError` carrying the
  transport reason verbatim; never silently swallowed.
* 403 / 409 / 5xx from host listen → :class:`InSifBrokerError` with
  ``status`` populated; never converted to "ok with empty result".
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable

# Module level, NOT deferred into the function. The deferred ``_spawn_client``
# import below is guarded by a "avoids a cycle if the spawn client ever grows
# back-references" comment; that reasoning does not transfer here.
# ``_listen/__init__.py`` is EMPTY and ``_handler_deadline`` imports nothing but
# ``time``, so this can neither cycle nor drag the listen server in — and
# ``_spawn_client``, which this module already calls on the production path,
# imports the very same name at ITS module level. A function-local import here
# would buy nothing and hide the coupling that is the whole point: this client's
# timeout is DERIVED from the server's declared deadline.
from .._listen._handler_deadline import client_timeout_for

logger = logging.getLogger(__name__)

__all__ = [
    "IN_SIF_ENV_VARS",
    "InSifBrokerError",
    "broker_start_to_host",
    "is_in_sif",
    "maybe_broker_in_sif_spawn",
]


# Apptainer sets ``APPTAINER_CONTAINER`` to the SIF path; legacy
# Singularity sets ``SINGULARITY_CONTAINER``. Both are honoured because
# operators routinely run apptainer compiled with singularity-compat
# (and some HPC sites still ship the older binary under the original
# name). An empty value is treated as "not in a SIF" — a stray
# ``--env APPTAINER_CONTAINER=`` should not flip the broker on.
IN_SIF_ENV_VARS = ("APPTAINER_CONTAINER", "SINGULARITY_CONTAINER")


class InSifBrokerError(RuntimeError):
    """Raised when the in-SIF spawn broker cannot reach the host listen.

    Carries the structured failure shape so the caller (CLI / MCP tool /
    log line) can show *why* the spawn failed without re-parsing the
    free-text message:

    * ``status`` — HTTP status code from the host listen server
      (``None`` for transport errors / missing env failures).
    * ``body`` — parsed response dict / text fallback / ``None``.

    A separate error type (rather than re-raising
    :class:`_spawn_client.SpawnRequestError`) keeps the integration
    point in :func:`_lifecycle._start.agent_start` clean: one ``except``
    catches every in-SIF broker failure mode without coupling that file
    to the spawn-client transport details.
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


def is_in_sif() -> bool:
    """Return True iff the current process is running inside an apptainer SIF.

    Pure env detection — no syscalls, no filesystem probes. Apptainer
    sets ``APPTAINER_CONTAINER`` (the SIF path) inside every container;
    legacy Singularity uses ``SINGULARITY_CONTAINER``. Either non-empty
    value signals "in a SIF". An explicit empty value (e.g. ``--env
    APPTAINER_CONTAINER=``) is treated as "not in a SIF" — the agent's
    runtime never emits an empty value, so a present-but-empty form is
    almost always operator typo and would be confusing to trigger the
    broker on.
    """
    for name in IN_SIF_ENV_VARS:
        val = os.environ.get(name)
        if val:  # non-empty string
            return True
    return False


def broker_start_to_host(
    child_name: str,
    *,
    caller: str | None = None,
    spec: dict | None = None,
    overwrite: bool = False,
    base_url: str | None = None,
    bearer: str | None = None,
    timeout_s: float | None = None,
    opener: Callable | None = None,
    foreground: bool = False,
    one_shot: bool = False,
    assume_yes: bool = False,
    force: bool = False,
) -> dict:
    """POST a spawn request to the host-side ``sac listen``; FAIL LOUD on error.

    Thin re-shape of :func:`_lifecycle._spawn_client.request_spawn` that
    translates every failure mode into :class:`InSifBrokerError`. This
    keeps the integration point in :func:`agent_start` from importing
    spawn-client internals (it imports only this module and catches
    one error type).

    Parameters
    ----------
    child_name
        The agent to start. Must be registered on the host OR provided
        inline via ``spec``.
    caller
        Override the auto-resolved caller identity. Defaults to
        ``SAC_NAME`` from the container env (resolved by the spawn
        client), which is what the host's ``check_spawn`` gate keys
        off. ``None`` and the empty string are both normalised to the
        admin path inside the spawn client.
    spec
        Optional inline spec dict ``{apiVersion, kind, spec}`` — the
        host materialises it under ``~/.scitex/agent-container/agents/
        <name>/spec.yaml`` and then starts it. Use for ephemeral /
        per-turn children whose YAML is not yet on the host.
    overwrite
        Forwarded as the ``overwrite`` body field; only meaningful with
        ``spec``. Defaults to ``False`` (409 on clash, surfaced as
        :class:`InSifBrokerError`).
    base_url
        Override ``SAC_LISTEN_BASE_URL``. Tests pass an in-process URL;
        production passes ``None`` so the env-injected value wins.
    bearer
        Override ``SAC_LISTEN_BEARER``. Tests pass an explicit value or
        ``""`` to force the unauthenticated branch; production passes
        ``None``.
    timeout_s
        Per-request HTTP timeout (seconds). ``None`` (the default)
        DERIVES it from the server's declared answer-by deadline via
        :func:`.._listen._handler_deadline.client_timeout_for` — the
        deadline plus ``CLIENT_MARGIN_S``. Pass a number only to
        override (tests do).

        IT USED TO DEFAULT TO A HAND-PICKED ``30.0``, "long enough for
        the host's ``sac agent start`` subprocess to return on a healthy
        box". That reasoning predates the deadline model, and the number
        landed EXACTLY on ``AGENT_START_DEADLINE_S``: the server is
        entitled to spend the whole 30s before answering, and
        ``client_timeout_for`` exists to add the margin that lets the
        answer arrive. Hardcoding the deadline here cancelled that
        margin, so the 202 "accepted, still in flight" — the message the
        server sends PRECISELY so a slow spawn is not mistaken for a
        dead one — lost the race by construction.

        Measured 2026-08-11: a spawn over this path was reported as "no
        response" while the host had ACCEPTED it and worked on it for
        5m12s (``started_at`` 06:15:08Z, ``failed_at`` 06:20:20Z, phase
        ``container_creation``). The caller read its own impatience as
        the peer being dead and escalated a healthy route as wedged.

        ``_spawn_client`` had already named this shape when it replaced
        "TWO independently-picked constants ... that had to stay ordered
        with no reviewer of either file able to see the other". This
        module held a THIRD copy, and it was the one nobody noticed.
    opener
        Optional ``urllib.request.urlopen``-shaped callable. The
        in-SIF integration tests pass a fake opener so the wire shape
        is exercised without an actual network round-trip.
    assume_yes
        Forwarded to :func:`_lifecycle._spawn_client.request_spawn` as
        ``assume_yes`` (wire field ``assume_yes: true``). Bug fix
        (2026-07-05, paper-scitex-clew report): the host's ``/agents``
        handler re-runs the same interactive refuse-without-``--yes``
        gate the ORIGINAL in-SIF caller's ``-y`` already satisfied —
        without threading this through, that consent never reached the
        host subprocess and every brokered start refused itself. See
        :func:`_lifecycle._spawn_client.request_spawn`'s docstring for
        the full contract.

    Returns
    -------
    dict
        The host's parsed JSON body on 2xx — the ``agents_start``
        handler returns ``{name, returncode, stdout, stderr}``. The
        caller can branch on ``returncode``; ``returncode != 0`` means
        the gate passed but the host-side ``sac agent start`` itself
        failed (e.g. a spec validation error on the host).

    Raises
    ------
    InSifBrokerError
        On missing base URL, transport failure, non-2xx HTTP status
        (including 403 ACL deny), or malformed response body.
    """
    # Local import — the spawn-client module is small and stdlib-only,
    # but routing the import through here keeps the module-level
    # surface lean and avoids a cycle if the spawn client ever grows
    # back-references to lifecycle modules.
    from ._spawn_client import SpawnRequestError, request_spawn

    # Resolve at CALL time, not import time: ``client_timeout_for`` reads the
    # server's deadline live, so a deployment that moves the deadline moves
    # this with it. A module-level ``_DEFAULT = client_timeout_for()`` would
    # snapshot the value once and quietly stop tracking — the same freeze that
    # made a 30.0 sitting on top of a 30.0 deadline look correct for months.
    if timeout_s is None:
        timeout_s = client_timeout_for()

    try:
        return request_spawn(
            child_name,
            caller=caller,
            spec=spec,
            overwrite=overwrite,
            base_url=base_url,
            bearer=bearer,
            timeout_s=timeout_s,
            opener=opener,
            foreground=foreground,
            one_shot=one_shot,
            assume_yes=assume_yes,
            force=force,
        )
    except SpawnRequestError as exc:
        # Re-throw under the broker's own error type so the integration
        # point in :func:`agent_start` catches one type instead of two.
        # Status + body are preserved verbatim — the operator-facing
        # message in the spawn client already names ``SAC_LISTEN_BASE_URL``
        # when relevant, so the fail-loud invariant carries through.
        logger.warning(
            "in-SIF broker spawn of %r failed: %s (status=%s)",
            child_name,
            exc,
            exc.status,
        )
        raise InSifBrokerError(str(exc), status=exc.status, body=exc.body) from exc


def maybe_broker_in_sif_spawn(
    name: str,
    *,
    dry_run: bool,
    opener: Callable | None = None,
    foreground: bool = False,
    one_shot: bool = False,
    assume_yes: bool = False,
    force: bool = False,
) -> bool:
    """Single-call broker chokepoint for the in-SIF redirect in agent_start.

    Encodes the full operator-mandated SAC-from-SAC dispatch contract so
    the integration point in :func:`_lifecycle._start.agent_start` stays
    a one-liner. Behaviour:

    * Not in a SIF, or ``dry_run`` — returns ``False`` (caller continues
      with the existing local flow). ``--dry-run`` is for inspecting the
      planned LOCAL workspace argv / files, never the host's — so we
      leave it alone.
    * In a SIF — POSTs the spawn RPC to host listen via
      :func:`broker_start_to_host`. On host-accepted + ``returncode==0``
      returns ``True`` (caller MUST return True and skip the local
      runtime). On host-accepted + ``returncode!=0``, raises
      :class:`RuntimeError` with the host's response embedded. On any
      transport / 4xx / 5xx failure, the underlying
      :class:`InSifBrokerError` propagates unchanged.

    Fail-loud invariants:
      * Missing ``SAC_LISTEN_BASE_URL`` → :class:`InSifBrokerError`
        (apptainer runtime forgot to inject it; never silently skip).
      * Host listen 4xx / 5xx / transport error → :class:`InSifBrokerError`
        carrying status + body verbatim.
      * Host accepted the request but ``sac agent start`` itself failed
        → :class:`RuntimeError` naming the agent and returncode, with
        the full host response in the message for debug-without-ssh.

    ``foreground`` / ``one_shot``: when the parent ``sac agents start``
    invocation carried these flags, propagate them through the body so
    the host listen's ``/agents`` handler appends ``--foreground`` /
    ``--one-shot`` to its inner ``sac agents start`` argv. This switches
    the host-side apptainer runtime to the foreground branch
    (``subprocess.run`` blocks until the capsule exits) so the
    capsule's actual exit code + stderr flow up the chain and land in
    ``STARTUP_FAILED.stderr_tail`` on crash — the cohort one-shot
    diagnostic clew needs (lead msg d96a468c 2026-06-06). Without this
    propagation, the inner runtime takes the background branch (Popen
    + return rc=0 immediately) and the post-ack liveness probe sees a
    still-alive Popen pid, returning SUCC while the capsule dies
    silently later.

    ``force``: propagate the caller's own ``--force`` across the broker
    boundary so the host's ``/agents`` handler appends ``--force`` to its
    inner ``sac agents start`` argv.

    THIS FIELD IS LOAD-BEARING FOR RESTART CORRECTNESS (incident
    2026-07-12, scitex-storage). ``agent_restart`` calls
    ``agent_start(force=True)`` precisely because a restart must REPLACE
    the process; but this broker fires BEFORE that force is ever consulted
    locally, so before this parameter existed the flag was silently
    dropped here. The host then ran a plain, unforced ``sac agents start
    <name>``, hit the idempotent "already running → no-op" branch
    (``_start.py``), printed ``SUCC: <name> started`` and exited 0. The
    restart reported success over an agent that never cycled — same
    process, same pid, same stale credentials — which is the precise class
    of lie ``_stop.py``'s force/gate comments already fight on the LOCAL
    path. It also explains the ``post_ack_no_apptainer_pid`` that followed:
    no new container was launched, so no ``apptainer_pid`` was ever
    written.

    ``assume_yes``: propagate the caller's own ``-y``/``--yes`` consent
    through to the host's ``/agents`` handler (bug fix 2026-07-05,
    paper-scitex-clew report). The host handler shells a fresh
    ``sac agents start <name>`` subprocess that re-runs the SAME
    interactive refuse-without-``--yes`` gate the caller already
    satisfied at the top of this call chain; without this field that
    consent never reached the host subprocess and the brokered start
    ALWAYS refused itself with "refusing to start ... without
    --yes/-y", even when ``-y`` was explicitly given. See
    :func:`broker_start_to_host` / :func:`_lifecycle._spawn_client.
    request_spawn` for the full wire contract.
    """
    if dry_run or not is_in_sif():
        return False

    result = broker_start_to_host(
        name,
        opener=opener,
        foreground=foreground,
        one_shot=one_shot,
        assume_yes=assume_yes,
        force=force,
    )
    rc = result.get("returncode") if isinstance(result, dict) else None
    if rc != 0:
        raise RuntimeError(
            f"sac-from-sac broker: host listen accepted the spawn of "
            f"{name!r} but `sac agent start` on the host failed "
            f"(returncode={rc}). Host response: {result!r}"
        )
    return True
