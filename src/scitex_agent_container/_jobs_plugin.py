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

    Currently one job: ``sac.accounts-refresh`` — a headless OAuth
    access-token refresh for every stored Claude account, skipping the
    active one (``--skip-active``) so the in-use refresh_token is never
    rotated out from under the live session.
    """
    from scitex_dev.jobs import JobSpec

    return [
        JobSpec(
            name="sac.accounts-refresh",
            schedule="0 */2 * * *",  # every 2h
            command="sac accounts refresh --all --skip-active",
            description=(
                "Headless OAuth access-token refresh for all stored Claude "
                "accounts, skipping the active one (avoids refresh-token "
                "rotation race)."
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
        )
    ]


__all__ = ["provide_jobs"]
