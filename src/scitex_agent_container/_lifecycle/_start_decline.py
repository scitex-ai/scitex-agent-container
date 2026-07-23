"""The wire signal for 'the operator was asked and did not consent'.

A refusal is not a failure: nothing was mutated, nothing was launched.
``write_marker`` must never mint a STARTUP_FAILED for it, but the
listen's only discriminator on the brokered subprocess is its exit code
(non-zero), which a decline also produces (``_start_single.py`` exits 1
on refusal). A sentinel this module both emits (the refusal branch) and
matches (``write_marker``) sidesteps any exit-code collision — including
the observed rc=2 from a stale host ``sac`` binary's "No such command"
error, which an exit-code-based carve-out would silently swallow.
"""

from __future__ import annotations

__all__ = ["DECLINE_SENTINEL", "start_was_declined"]

DECLINE_SENTINEL = "sac:start-declined"


def start_was_declined(stdout: str, stderr: str) -> bool:
    return DECLINE_SENTINEL in (stdout or "") or DECLINE_SENTINEL in (stderr or "")
