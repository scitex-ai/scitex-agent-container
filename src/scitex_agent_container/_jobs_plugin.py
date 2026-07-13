"""Federated scheduled-job provider for the ``scitex_dev.jobs`` group.

Registered via the ``scitex_dev.jobs`` entry point (see ``pyproject.toml``)
so ``scitex-dev ecosystem {cron,systemd,daemon}`` and ``sac dev`` surface
sac's own periodic jobs through the single ecosystem aggregator.

The ``scitex_dev.jobs`` import is LAZY (inside :func:`provide_jobs`) so a
scitex-dev that predates the jobs contract (PyPI lag) does not break the
entry-point's import-time metadata — the provider only needs ``JobSpec``
the moment ``discover_jobs()`` actually calls it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from scitex_dev.jobs import JobSpec


def provide_jobs() -> "list[JobSpec]":
    """Return sac's federated scheduled jobs.

    Two jobs today:

    * ``sac.accounts-refresh`` (``kind="timer"``) — a headless OAuth
      access-token refresh for EVERY stored Claude account, including the
      active one (``--include-active``), mirroring the rotated token back
      into the live ``~/.claude`` login (``--sync-active-login``).
    * ``sac.listen`` (``kind="service"``) — the host-level HTTP/JSON
      control plane (``127.0.0.1:7878``: push hub, spawn broker, lead
      inbox).

    Why ``sac.accounts-refresh`` is not ``--skip-active``: under the
    pre-2026-07-08 two-refresher model both the host timer and the
    in-container CLI redeemed the same single-use refresh_token, so
    skipping the active account was the race guard (2026-06-04 neurovista
    401 storm). Since 2026-07-08 agents bind the credential ``:ro`` and
    never refresh, making this timer the SOLE refresher — so
    ``--skip-active`` stopped guarding a race and instead starved the one
    account every agent uses, whose ~8h access_token then expired and
    401'd the whole fleet (2026-07-09/10 total stall).
    ``--sync-active-login`` keeps the operator's live session valid across
    the single-use refresh_token rotation.

    Why ``sac.listen`` is federated here: it had NO SUPERVISOR. Declaring
    it as a ``kind="service"`` JobSpec hands it to scitex-dev's supervisor
    (``scitex-dev service ensure sac.listen`` — systemd ``--user`` with
    ``Restart=`` where a user manager is reachable, else a respawn-loop
    keep-alive), so it auto-starts on boot and comes back on ANY exit.
    This replaces the fragile cron-based watchdog (``sac-listen-watch.sh``,
    ``*/2`` cron) that died twice on 2026-07-05 (clew incident
    ``clew-incident-sac-host-listen-down``) and left the whole fleet cut
    off from the host after a CLEAN shutdown that nothing restarted.
    """
    from scitex_dev.jobs import JobSpec

    return [
        JobSpec(
            name="sac.accounts-refresh",
            schedule="0 */2 * * *",  # every 2h
            command=(
                "sac accounts refresh --all --include-active "
                "--sync-active-login"
            ),
            description=(
                "Headless OAuth access-token refresh for all stored Claude "
                "accounts including the active one (sole-refresher model), "
                "mirroring the rotation into the live ~/.claude login."
            ),
            # 2026-06-11 (lead msg c5212862): scitex_dev.jobs.JobSpec kind
            # taxonomy is {"service","timer","cron"} since scitex-dev #153.
            # ``sac.accounts-refresh`` is a periodic systemd --user timer
            # (token TTL ~7h, refresh every 2h) → ``kind="timer"`` with the
            # cadence carried by ``on_unit_active_sec`` below. The legacy
            # ``kind="systemd"`` is no longer accepted; it raises
            # ``ValueError`` at construction time and ``scitex-dev
            # ecosystem up`` silently drops sac's whole provider
            # (provider-isolated, WARN-only), leaving the OAuth refresh
            # unmanaged.
            kind="timer",
            on_boot_sec="15min",
            on_unit_active_sec="2h",
            timeout_sec=120,
        ),
        JobSpec(
            name="sac.listen",
            kind="service",
            schedule="",
            command="sac listen",
            description=(
                "Host-level HTTP/JSON control plane for sac agents "
                "(loopback 127.0.0.1:7878) — push hub, spawn broker, lead "
                "inbox. No env vars required: SAC_LISTEN_BEARER "
                "self-resolves from the on-disk token file "
                "(~/.scitex/agent-container/tokens/listen-<host>.token) "
                "when unset (PR #470)."
            ),
            # restart_policy="always", NOT "on-failure": Incident
            # 2026-06-26 (scripts/systemd/sac-listen.service) found that
            # a clean 0-exit (e.g. an unexpected SIGTERM that uvicorn
            # turns into a graceful shutdown) is NOT covered by
            # Restart=on-failure, and that is exactly how the fleet lost
            # a2a comms silently with nothing restarting the listen.
            # "always" covers every non-`systemctl stop` exit; JobSpec's
            # ALLOWED_RESTART_POLICIES includes "always" for kind=
            # "service" specifically for this reason.
            restart_policy="always",
            timeout_sec=30,
            # No watchdog_sec: sac listen is a plain Type=simple daemon
            # that does not call sd_notify(WATCHDOG=1), so requesting a
            # watchdog here would just cause systemd to kill-and-restart
            # it every interval (see JobSpec.watchdog_sec docstring).
            # Wedge (hang, not crash) detection is instead covered by the
            # hand-maintained companion
            # scripts/systemd/sac-listen-health.{service,timer} probe,
            # which this JobSpec does NOT replace — see
            # scripts/systemd/README.md for the coexistence note.
        ),
    ]


__all__ = ["provide_jobs"]
