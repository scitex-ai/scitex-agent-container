"""Per-agent OAuth credential resolution for the apptainer runtime.

Extracted from ``_apptainer_runtime.py`` (512-line cap) — mirrors the
existing helper-module split (``_apptainer_build``,
``_apptainer_listen_env``, ``_apptainer_iso_flags``).

The single public entry point :func:`resolve_cred_file` decides WHICH
host-side ``.credentials.json`` gets bound into an agent container:

* ``spec.claude.account`` empty → the host's live
  ``~/.claude/.credentials.json`` (shared OAuth — current default).
* ``spec.claude.account`` set → a FROZEN BOOT-COPY of that saved
  account's snapshot, copied into the agent's own state dir so two
  agents pinned to two accounts never fight one mount, and a host
  ``/login`` never moves a pinned agent.

The copy is bound ``:rw`` by the caller so the in-container Claude CLI
can refresh the OAuth ``accessToken`` (~1h cadence) on the agent's
private copy. Changing ``spec.claude.account`` only takes effect on the
next ``sac agent restart`` (the copy happens at start).

Fail-loud (no silent fallbacks): when ``spec.claude.account`` is set
but the pinned store is ABSENT or EXPIRED, the start aborts with
:class:`PinnedAccountError` carrying the exact remedy. A pinned agent
must NEVER silently fall back to the host live file (a different
account) or run with a stale token — that would defeat the whole point
of account pinning and hand the agent the wrong identity.
"""

from __future__ import annotations

import shutil
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

    With ``spec.claude.account`` set, returns a frozen boot-copy of that
    store's snapshot. Raises :class:`PinnedAccountError` when the store
    is absent or its credential is already expired — NEVER falls back to
    the host live file for a pinned agent. See module docstring.
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

    dest = Path(state_dir).expanduser() / "claude" / ".credentials.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Preserve the per-agent dest if it is already fresher than the
    # snapshot. Without this guard, an agent restart would clobber a
    # token the in-container Claude CLI just refreshed (on its private
    # ``:rw`` copy) with the stale boot-time snapshot — and auth would
    # die at the next refresh cycle. Only overwrite when dest is
    # absent OR the snapshot's OAuth expiresAt is strictly newer.
    dest_expiry = _read_oauth_expiry_seconds(dest) if dest.is_file() else None
    if dest_expiry is None or expiry > dest_expiry:
        shutil.copy2(snapshot, dest)
    return dest


__all__ = ["PinnedAccountError", "resolve_cred_file"]
