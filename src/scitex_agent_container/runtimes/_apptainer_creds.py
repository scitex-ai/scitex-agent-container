"""Per-agent OAuth credential resolution for the apptainer runtime.

Extracted from ``_apptainer_runtime.py`` (512-line cap) — mirrors the
existing helper-module split (``_apptainer_build``,
``_apptainer_listen_env``, ``_apptainer_iso_flags``).

The single public entry point :func:`resolve_cred_file` decides WHICH
host-side ``.credentials.json`` gets bound into an agent container:

* ``spec.claude.account`` empty → the host's live
  ``~/.claude/.credentials.json`` (shared OAuth — current default).
  Bound ``:ro`` by the caller (master-host single-refresher model): the
  agent READS the host live file but never refreshes it.

* ``spec.claude.account`` set → the saved account's snapshot itself at
  ``~/.scitex/agent-container/accounts/<acct>/.credentials.json``,
  bound ``:ro`` by the caller. The agent reads the snapshot but never
  writes it; the host-side ``sac-accounts-refresh`` timer is the SOLE
  refresher and keeps the snapshot fresh (a DIRECTORY bind on the
  snapshot's parent makes the timer's atomic-replace refreshes visible
  to the container without a restart — see ``_apptainer_auth``).

Fix for the 2026-06-01 fleet-wide silent outage (operator task #15):
the prior implementation COPIED the snapshot into a per-agent state-dir
``dest`` and bound that copy. Refreshes landed on the copy; the source
snapshot drifted stale. After ~8h, every SDK turn 401'd silently (the
telegram bridge still marked inbound 👀, but the agent could not
complete a turn). The fix is structural — bind the snapshot itself.
The snapshot is the single source of truth for ALL agents pinned to
that account; under the master-host single-refresher model the
host-side ``sac-accounts-refresh`` timer keeps that one snapshot fresh
and the ``:ro`` agents read it. (Before 2026-07-08 the bind was ``:rw``
and the in-container CLI refreshed the snapshot itself — that kept it
fresh but made every agent a refresher, and an agent refresh consumes
the single-use OAuth refresh_token, churning any freshly-logged-in
account. The ``:ro`` flip moves refresh responsibility to the sole
host-side timer.)

Fail-loud (no silent fallbacks): when ``spec.claude.account`` is set
but the pinned snapshot is ABSENT, has no numeric ``expiresAt``, or is
ALREADY EXPIRED, the start aborts with :class:`PinnedAccountError`
carrying the exact remedy. A pinned agent must NEVER silently fall
back to the host live file (a different account) or run with a stale
token — that would defeat the whole point of account pinning and hand
the agent the wrong identity.

Sharing semantics with the :ro bind:

* Different accounts → different snapshot files → no conflict.
* Same account, multiple agents → all share the SAME snapshot mount,
  READ-ONLY. None of them refresh it; the host-side timer does. The
  token is fungible across agents pinned to the same account by
  definition, so the timer's single refresh serves every pinned agent
  at once. Same-account agents share an IDENTITY, so sharing the file
  matches the model.
"""

from __future__ import annotations

import time
from pathlib import Path

from ..config import AgentConfig


class PinnedAccountError(RuntimeError):
    """Raised when ``spec.claude.account`` points at an absent/expired store.

    The message names the store path and the two remedies:
    ``claude /login`` to that account (then ``sac accounts sync-live``),
    or ``sac accounts save <name>`` on the credential-holding host.
    Surfaced at ``sac agents start`` so a pinned agent never launches
    with the wrong account or a dead token.
    """


def resolve_cred_file(
    config: AgentConfig,
    state_dir: Path,
    *,
    now: float | None = None,
) -> Path | None:
    """Return the host-side ``.credentials.json`` to bind for ``config``.

    With no ``spec.claude.account`` set, returns the host live file
    (``None`` only when it does not exist — caller skips the bind).

    With ``spec.claude.account`` set, returns the per-account SNAPSHOT
    file directly (no per-agent copy). The caller binds it ``:ro`` — the
    agent reads the snapshot but never refreshes it. The snapshot is the
    single source of truth for every agent pinned to this account, kept
    fresh by the host-side ``sac-accounts-refresh`` timer (the sole
    refresher under the master-host single-refresher model).

    Raises :class:`PinnedAccountError` when the snapshot is absent, has
    no numeric ``expiresAt``, or is already expired — NEVER falls back
    to the host live file for a pinned agent and NEVER hands out a
    dead token. See module docstring.

    The ``state_dir`` parameter is preserved in the signature for
    backward compatibility with the caller's existing kwarg shape
    (``_apptainer_auth.auth_argv``); it is no longer used because the
    resolver no longer copies the snapshot into ``state_dir``. The
    ``now`` parameter remains a real-time injection seam for tests.
    """
    host_cred = Path.home() / ".claude" / ".credentials.json"
    acct = getattr(getattr(config, "claude", None), "account", "") or ""
    if not acct:
        return host_cred

    from .._account.creds_sync import _read_oauth_expiry_seconds
    from .._state.account_store import _store_path

    store = _store_path(None, Path.home())
    snapshot = store / acct / ".credentials.json"
    if not snapshot.is_file():
        raise PinnedAccountError(
            f"spec.claude.account='{acct}' has no credential snapshot at "
            f"{snapshot}. Refusing to fall back to a different account. "
            f"Fix: run `claude /login` to that account then "
            f"`sac accounts sync-live`, or `sac accounts save {acct}` on "
            "the credential-holding host, then restart this agent."
        )

    now_ts = now if now is not None else time.time()
    expiry = _read_oauth_expiry_seconds(snapshot)
    if expiry is None:
        raise PinnedAccountError(
            f"spec.claude.account='{acct}' snapshot at {snapshot} is "
            "missing a numeric `claudeAiOauth.expiresAt`. Refusing to "
            f"launch with an unverifiable token. Fix: `claude /login` to "
            f"that account then `sac accounts sync-live`."
        )
    if expiry <= now_ts:
        ago = int(now_ts - expiry)
        raise PinnedAccountError(
            f"spec.claude.account='{acct}' snapshot at {snapshot} expired "
            f"{ago} seconds ago. Refusing to launch a pinned agent with a "
            f"stale token. Fix: `claude /login` to that account then "
            f"`sac accounts sync-live`, and restart this agent."
        )

    # Return the SNAPSHOT itself — the caller binds it ``:ro``. The
    # agent reads this file directly; the host-side timer refreshes it.
    # No per-agent copy, no stale-copy clobber risk, no
    # auth-dies-after-8h failure mode (operator task #15).
    return snapshot


__all__ = ["PinnedAccountError", "resolve_cred_file"]
