"""Pre-flight helpers for ``agent_start`` — liveness, account rotation,
strict-drift resolution, and launch-time spec-source drift.

Extracted from ``_start.py`` (split for the 512-line module limit).
``_start.agent_start`` imports these; behaviour is unchanged.
"""

from __future__ import annotations

import sys
import traceback
from typing import Any, Callable

from ..config import AgentConfig


def _verify_real_liveness(
    config: AgentConfig,
    runtime,
    *,
    instances_oracle: Callable[[], list[dict]] | None = None,
) -> bool:
    """Return True iff the agent is *demonstrably* running on this host.

    The pre-fix call site treated ``registry.exists(name) and
    runtime.is_running(config)`` as the already-running signal. Both
    are necessary but not sufficient:

      * ``registry.exists`` is a JSON file on disk — a forced ``rm``,
        a stale entry from a prior boot, or a crash-during-write leaves
        the file behind even though no agent is running.
      * ``runtime.is_running`` checks the per-runtime PID file with
        ``os.kill(pid, 0)`` — on a Linux PID-wraparound the same pid
        can belong to a completely unrelated process and the probe
        returns True.

    Either of those false positives causes the no-op branch in
    :func:`agent_start` to swallow a real start request silently and
    return rc=0. The cross-host ``instances`` table is the third
    independent signal — it is written by ``record_local_instance``
    inside the *real* start path and removed by ``agent_stop`` /
    ``cleanup_stale``. We require an active row before treating the
    agent as already-running. If the row is absent, the registry/PID
    pair is inconsistent → fall through to a real start instead of
    the silent no-op.

    The ``instances_oracle`` seam is the no-mocks knob for tests; it
    defaults to a host-unfiltered :func:`state_db.list_active_instances`
    call (we want ANY active row for the name, not just rows on the
    current host — handover may have moved it).
    """
    if instances_oracle is None:
        from .._state.state_db import list_active_instances as _default

        def instances_oracle():  # type: ignore[no-redef]
            # Explicit host=None so the call is unambiguous and the
            # fixture-isolated test reads through the same module.
            return _default(host=None)

    try:
        rows = instances_oracle()
    except Exception:
        # stx-allow: fallback (reason: a missing/locked state.db must
        # not block the start path; degrade to "no liveness evidence"
        # which causes the caller to launch fresh rather than silently
        # no-op)
        return False
    for row in rows or ():
        if row.get("name") == config.name:
            return True
    return False


def _resolve_strict_drift(strict_drift: bool | None) -> bool:
    """Resolve effective strict-drift mode (arg wins, else env).

    ``strict_drift=True/False`` from ``--strict-drift`` takes priority.
    ``None`` falls back to ``SAC_STRICT_DRIFT`` (``1``/``true``/``yes``
    → strict). Read through the sac env helper so either prefix works.
    """
    if strict_drift is not None:
        return strict_drift
    from .._env import getenv as _sac_env

    raw = (_sac_env("STRICT_DRIFT", "") or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _rotate_to_healthy_account(
    config: AgentConfig,
    *,
    log_stream: Any = None,
) -> None:
    """Rotate ``config.claude.account`` to a healthy stored account.

    CREDS-PHASE1 wiring. Only acts on PINNED agents
    (``spec.claude.account`` non-empty). For an unpinned agent the
    runtime continues to bind the host's live ``.credentials.json``
    untouched.

    On a pinned agent:

    * If the pinned snapshot is healthy → no-op (config unchanged).
    * If the pinned snapshot is EXPIRED/ABSENT but another stored
      account has a fresh snapshot → ``config.claude.account`` is
      mutated to that account and a one-line rotation notice is
      printed to ``log_stream`` (default ``sys.stderr``). The runtime
      then binds that account's snapshot ``:rw`` directly via
      :func:`runtimes._apptainer_creds.resolve_cred_file` (operator
      #15 — the prior boot-copy path was the root cause of the
      2026-06-01 fleet outage; refreshes now write back to the
      snapshot itself, never expiring).
    * If NOTHING is healthy → :class:`_creds.NoHealthyAccountError`
      propagates (fail loud, no silent stale-token launch). Agent is
      NOT started.

    See :mod:`scitex_agent_container._creds._pick_healthy` for the
    health model — non-expired snapshot = healthy. Cap-induced 429s
    still surface from claude in-turn; the picker only avoids
    known-stale auth at boot.
    """
    pinned = getattr(getattr(config, "claude", None), "account", "") or ""
    if not pinned:
        return  # unpinned agent — host live OAuth, untouched.

    from .._creds import pick_healthy_account

    picked = pick_healthy_account(pinned)
    if picked == pinned:
        return  # pinned is healthy — no rotation, no log line.

    config.claude.account = picked
    stream = log_stream if log_stream is not None else sys.stderr
    print(
        f"[sac:creds] agent '{config.name}' rotated account: "
        f"{pinned!r} -> {picked!r} (pinned snapshot unhealthy; "
        f"rotated to the first healthy stored account)",
        file=stream,
    )


def _check_spec_source_drift_at_launch(
    config_path: str, agent_name: str, strict_drift: bool | None
) -> None:
    """Run the launch-time drift check; warn loud (or block if strict).

    Fully guarded: the underlying check never raises except the
    deliberate strict-mode :class:`SpecSourceDriftError`. We let that
    propagate (the CLI / caller turns it into a non-zero exit); any
    other unexpected failure here is swallowed so a launch is never
    crashed by the drift guard.
    """
    from .._drift import SpecSourceDriftError, warn_if_spec_source_drifted

    strict = _resolve_strict_drift(strict_drift)
    try:
        warn_if_spec_source_drifted(config_path, agent=agent_name, strict=strict)
    except SpecSourceDriftError:
        # Deliberate strict-mode block — propagate so the caller exits
        # non-zero. This is the ONE thing this guard is allowed to raise.
        raise
    except Exception:  # stx-allow: fallback (reason: the drift guard must NEVER crash a launch; any unexpected error degrades to "no check ran" and the agent proceeds)
        traceback.print_exc()
