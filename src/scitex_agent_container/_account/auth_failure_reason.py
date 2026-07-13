"""WHY an agent's auth is failing: REVOKED, or genuinely EXPIRED?

THE LYING ERROR MESSAGE
    Claude Code renders EVERY 401 as ``Login expired · Please run /login``.
    On this fleet that text is usually **false**, and the falsehood is exactly
    why the bug survived so long: it names a cause that did not happen and
    prescribes a remedy that is not needed.

    What actually happens (proven 2026-07-13, four agents died at once): the
    credential file is shared, an agent runs its own OAuth refresh, that refresh
    consumes the **single-use** ``refresh_token`` and rotates the access token —
    and every OTHER process still holding the previous access token is
    instantly **REVOKED**. Nothing expired. The token was taken away.

THE DISCRIMINATOR (cheap, and decisive)
    Read the agent's on-disk credential and compare ``claudeAiOauth.expiresAt``
    to now:

    * **expiresAt in the FUTURE** — the credential on disk is perfectly VALID, a
      fresh process would authenticate with it right now, and yet this agent is
      401-ing. The only way both can be true is that the token it holds IN
      MEMORY is no longer the token on disk: it was rotated out from under it.
      ⇒ ``revoked``. Remedy: **restart** — the agent re-reads the (valid) file
      and recovers. No human, no re-login.
    * **expiresAt in the PAST** — the credential really is past its lifetime.
      ⇒ ``expired``. Remedy: **login** — new credentials must actually be minted.

    That distinction is the whole value of this module: it is the difference
    between a 5-second automated restart and dragging the operator out of bed to
    re-authenticate. The banner cannot tell you which; ``expiresAt`` can.

WHAT THIS MODULE WILL NOT DO
    It does not claim an agent's auth IS failing — that is the watchdog's job
    (it reads the pane; see ``_runners._tmux.auth_status``). This module only
    explains a failure the watchdog has ALREADY established. Asked about a
    healthy agent it would happily answer ``revoked``, which would be nonsense:
    a valid on-disk credential is the NORMAL state. Diagnose only what is broken.
"""

from __future__ import annotations

import time
from pathlib import Path

__all__ = [
    "REASON_EXPIRED",
    "REASON_REVOKED",
    "REASON_UNKNOWN",
    "credential_path_for",
    "diagnose_reason",
    "remedy_for",
]

# The credential really is past ``expiresAt`` — a genuine expiry.
REASON_EXPIRED = "expired"
# The credential on disk is VALID, yet the agent 401s ⇒ the token it holds in
# memory was rotated away by somebody else's refresh. NOT an expiry.
REASON_REVOKED = "revoked"
# No readable credential / no numeric ``expiresAt`` ⇒ we genuinely do not know.
# We say so rather than guessing: a confident wrong cause is what got us here.
REASON_UNKNOWN = "unknown"

_REMEDY = {
    # Re-reading the (already-valid) credential file is all that is needed, and
    # only a restart makes the process do that — Claude Code never re-reads it.
    REASON_REVOKED: "restart",
    # Nothing on disk can authenticate; new credentials must be minted.
    REASON_EXPIRED: "login",
    # Restart first: it is cheap, safe, and cures the common (revoked) case. If
    # the agent comes back still failing, it is the expired case in disguise.
    REASON_UNKNOWN: "restart",
}


def remedy_for(reason: str) -> str:
    """``"restart"`` or ``"login"`` — what actually fixes this failure."""
    return _REMEDY.get(reason, "restart")


def credential_path_for(config: object, *, home: Path | None = None) -> Path:
    """The host-side ``.credentials.json`` that ``config``'s agent authenticates with.

    Mirrors ``runtimes._apptainer_creds.resolve_cred_file``'s resolution — an
    agent pinned to ``spec.claude.account`` reads that account's snapshot;
    everyone else reads the host live file — but NEVER raises. That resolver
    deliberately fails loudly (``PinnedAccountError``) on an absent/expired
    snapshot because it gates agent START. We are only DESCRIBING an already-
    broken agent, and an exception here would take out the whole fleet view.

    ``home`` is the real filesystem root to resolve against (default: the actual
    ``Path.home()``). It threads straight into ``account_store._store_path``,
    which already takes ``home`` for exactly this reason — so a test can point
    the whole resolution at a ``tmp_path`` holding real credential bytes instead
    of patching the module's internals.
    """
    base = home if home is not None else Path.home()
    account = getattr(getattr(config, "claude", None), "account", "") or ""
    if not account:
        return base / ".claude" / ".credentials.json"
    from .._state.account_store import _store_path

    return _store_path(None, base) / account / ".credentials.json"


def diagnose_reason(
    config: object,
    *,
    now: float | None = None,
    home: Path | None = None,
) -> str:
    """Why is THIS agent's auth failing? One of the three ``REASON_*`` above.

    Call only for an agent the watchdog has already flagged (see the module
    docstring: a valid credential is the normal state, so this would answer
    ``revoked`` for a perfectly healthy agent).

    ``now`` and ``home`` are real-collaborator injection seams (wall clock,
    filesystem root); production passes neither.

    Never raises: an unreadable or malformed credential yields
    :data:`REASON_UNKNOWN`, because "I could not tell" is a true statement and
    a fabricated cause is not.
    """
    # stx-allow: fallback (reason: this only ANNOTATES an already-detected
    # failure; a config/store hiccup must degrade to "unknown", never crash the
    # watchdog or the agent list.)
    try:
        from .creds_sync import _read_oauth_expiry_seconds

        expiry = _read_oauth_expiry_seconds(credential_path_for(config, home=home))
    except Exception:  # stx-allow: fallback (reason: see inline comment)
        return REASON_UNKNOWN
    if expiry is None:
        return REASON_UNKNOWN
    reference = now if now is not None else time.time()
    return REASON_EXPIRED if expiry <= reference else REASON_REVOKED
