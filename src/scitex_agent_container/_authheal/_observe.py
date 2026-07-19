"""Record WHAT THE PASS SAW, separately from what it then did about it.

The restarter's own reports say what it DECIDED. This module writes the prior,
weaker, more durable fact — that at time T a wedge was observed on agent A —
into the shared auth-event log (:mod:`.._authevents`), where it can be read
alongside the credential rotations that caused it.

WHY THE OBSERVATION IS WORTH A RECORD OF ITS OWN
    ``auth-heal.log`` carried the wedge age inside its restart lines, so the
    only way to learn how long an agent had been stuck was to read lines about
    restarts. When those restarts stopped working the ``age=`` field just kept
    climbing (one reached 262200s — three days) inside messages that each
    announced a remedy. Recording the OBSERVATION on its own — every pass,
    ``--check`` runs included — means the duration of a wedge is established by
    repeated sightings that owe nothing to the restarter's claims about itself.

This module emits. It never decides anything and never restarts anything.
"""

from __future__ import annotations

from pathlib import Path

from .._authevents import log_auth_failure_observed, resolve_account_for_agent

__all__ = ["observe_wedge"]

#: Written onto every record so a reader can tell which emitter produced it —
#: this restarter, or (one day) another one on the same rail.
SOURCE = "sac.restart-login-expired"


def observe_wedge(
    name: str,
    *,
    specs_dir: Path | None = None,
    event_log: Path | None = None,
    now: float | None = None,
) -> str | None:
    """Record one observed wedge; return the account HINT for reuse.

    The account is resolved from the agent's spec and is a hint, not proof of
    the credential the wedged process is holding in memory — an agent started
    before a reassignment holds the old one. ``None`` when undeterminable, and
    ``None`` is written as ``null``: present and explicitly unknown.

    Returns the hint so the caller can label its restart records with the SAME
    value, rather than resolving twice and risking two records that disagree
    about one agent at one instant.

    Fail-open: the write is best-effort and its failure is not reported here,
    because nothing upstream may act on it. Losing this line must never cost
    the recovery it describes.
    """
    account = resolve_account_for_agent(name, specs_dir=specs_dir)
    log_auth_failure_observed(
        agent=name,
        account=account,
        # NO http_status. A frozen pane banner is a RENDERING, not a status
        # code read off a response — and Claude Code prints "Login expired"
        # for ANY 401, sometimes when nothing expired at all. Synthesising a
        # 401 here would fabricate the single field this log exists to make
        # trustworthy. Null means: we saw a wedge, we did not see a status.
        http_status=None,
        detail=(
            "system auth banner frozen above the prompt across two captures "
            "(corroborated login-expired wedge)"
        ),
        path=event_log,
        now=now,
        extra={"source": SOURCE, "detector": "tui-pane"},
    )
    return account
