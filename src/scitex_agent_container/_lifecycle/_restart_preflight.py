"""Pre-STOP successor-credential auth pre-flight for agent restart.

INCIDENT ``incident-agent-self-restart-one-way-20260712`` (P1,
operator-escalated). An agent self-restart brokered through ``sac
listen`` is a ONE-WAY trip: the STOP half succeeds but the re-launched
container boots DEAD — ``claude`` answers "Login expired · Please run
/login" to every prompt — stranding the agent (it cannot even report
its own failure). A MANUAL ``sac agents restart`` from the operator's
shell recovers it, because a human simply re-runs it against a
now-healthy credential.

Root cause (verified against the code):

* The launch-time credential gate is TIMESTAMP-ONLY. Both branches —
  :func:`runtimes._apptainer_auth_bind._assert_credential_unexpired`
  (explicit ``spec.claude.credentials_file``) and
  :func:`runtimes._apptainer_creds.resolve_cred_file` →
  ``PinnedAccountError`` (``spec.claude.account`` snapshot) — check
  only ``claudeAiOauth.expiresAt`` against the wall clock. A snapshot
  whose ``expiresAt`` is in the FUTURE but whose ``refresh_token`` (and,
  in the incident, its ``access_token``) was SERVER-INVALIDATED
  (operator re-``/login`` elsewhere, or the account used on another
  host) passes the gate, binds, boots, and 401s.

* Unpinned/pool agents RE-PICK their account on every restart
  (:func:`_creds.pick_healthy_account`, quota-conditional). The picker's
  health model is ALSO timestamp-only (a non-expired ``expiresAt`` reads
  ``VALID``), so a restart can SWAP onto a stale-but-unexpired snapshot
  the running container was never using. The incident log shows exactly
  this swap ("selected credentials_files pool entry: account
  ywata1989-gmail-com ...").

The fix here is the SAFETY NET the one-way trip was missing: BEFORE the
stop, resolve the credential the SUCCESSOR container would launch on
(the same resolution the bind does, on the already-account-rotated
config) and PROBE its REAL usability by exercising the refresh grant
(:func:`_account.token_refresh.refresh_account_credentials`). When the
token endpoint EVALUATES the grant and REFUSES it (the confirmed
incident class), :func:`assert_successor_auth_usable` raises
:class:`RestartPreflightAbort` and the caller NEVER reaches the stop —
so the running container is LEFT UP and the agent keeps working on its
current, known-good auth.

Fail-OPEN discipline (this is load-bearing): a false-negative that
wrongly blocks a HEALTHY restart is WORSE than the bug. We therefore
abort ONLY on an unambiguous grant REJECTION
(:data:`_account.token_refresh.FAILURE_REJECTED`). A transport / moved-
endpoint / network / unparseable-2xx failure is NOT a token problem and
must never block a restart — those PROCEED (with a WARN). And a
successful probe-refresh even HEALS the successor (the freshly-minted
access_token is atomically persisted to the snapshot the successor
binds).

Token values NEVER leave this module — only expiry booleans, account
labels, paths, and the endpoint/status ``reason`` sentence
(``refresh_account_credentials``'s contract; it never returns tokens).
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from ..config import AgentConfig

logger = logging.getLogger(__name__)

# Do NOT spend the single-use refresh grant probing a token this fresh.
# Mirrors the `sac accounts refresh` CLI's own "skipped; token still fresh
# (TTL >= 2h)" guard — which this module previously bypassed by calling
# `refresh_account_credentials` directly. See probe_credential_usable().
_PROBE_MIN_TTL_S = 2 * 3600

__all__ = [
    "RestartPreflightAbort",
    "assert_successor_auth_usable",
    "preflight_from_config_path",
    "probe_credential_usable",
    "resolve_successor_credential",
]


def _credential_ttl_seconds(credential_path: Path) -> float | None:
    """Seconds until ``credential_path``'s access token expires.

    ``None`` when the TTL cannot be determined (missing / unreadable /
    unparseable file, or no numeric ``expiresAt``) — the caller then falls
    through to the real probe rather than assuming freshness, so an
    undetermined TTL never silently skips the check. Never raises, and never
    reads or returns a token VALUE — only the expiry timestamp.
    """
    # stx-allow: fallback (reason: an unreadable/!unparseable credential must
    # not crash a restart — return None so the caller PROBES rather than
    # assuming the token is fresh; that is the conservative direction)
    try:
        data = json.loads(Path(credential_path).read_text())
    except Exception:
        return None
    oauth = data.get("claudeAiOauth", data) if isinstance(data, dict) else {}
    expires_at = oauth.get("expiresAt") if isinstance(oauth, dict) else None
    if not isinstance(expires_at, (int, float)):
        return None
    return (float(expires_at) / 1000.0) - time.time()


class RestartPreflightAbort(RuntimeError):
    """Abort a restart BEFORE the running container is stopped.

    Raised by :func:`assert_successor_auth_usable` when the credential the
    successor container would launch on is unexpired-by-timestamp but its
    refresh grant is REJECTED by the token endpoint (the confirmed
    one-way-restart incident class). The message names the account, the
    resolved credential path, and the remedy; it confirms the running
    container was LEFT UP. Because the raise happens before ``agent_stop``,
    the live agent keeps running on its current, known-good credential.
    """


def resolve_successor_credential(config: AgentConfig) -> tuple[Path | None, str]:
    """Resolve ``(credential_path, account_label)`` the SUCCESSOR will bind.

    Mirrors :func:`runtimes._apptainer_auth_bind.credentials_file_bind`'s
    source resolution EXACTLY, on the ALREADY-account-rotated ``config`` (so
    it names the credential the launch will actually mount, swaps included):

      1. explicit ``spec.claude.credentials_file`` (a pool collapses into
         this field via :func:`_lifecycle._start_preflight.
         _rotate_among_credentials_files`) → that path; label = its parent
         dir name (the fleet account slug),
      2. ``spec.claude.account`` → the per-account snapshot via
         :func:`runtimes._apptainer_creds.resolve_cred_file`; label = the
         account name.

    Returns ``(None, "")`` — pre-flight is a NO-OP — for:

      * provider / openai-family backends (API key, no OAuth to probe), and
      * pure-unpinned host-live agents (no account / credentials_file /
        credentials_files). Their account NEVER swaps on restart, so the
        swap-to-a-stale-snapshot mechanism does not apply, and probing would
        needlessly rotate the operator's live ``~/.claude`` login token.

    May raise the SAME fail-loud the launch resolution raises —
    :class:`runtimes._apptainer_creds.PinnedAccountError` (absent/expired
    account snapshot) or :class:`FileNotFoundError` (missing designated
    file). Propagating those is the desired behaviour: they become an
    abort-BEFORE-stop (strictly better than today's stop-then-fail, where
    the same error tears down a running agent it then cannot restart).
    """
    from ..runtimes._apptainer_provider import (
        openai_provider_active,
        provider_active,
    )

    # API-key backends have no OAuth credential to probe.
    if provider_active(config) or openai_provider_active(config):
        return None, ""

    claude_spec = getattr(config, "claude", None)
    designated = str(getattr(claude_spec, "credentials_file", "") or "").strip()
    if designated:
        src = Path(designated).expanduser()
        if not src.is_file():
            # Mirror credentials_file_bind's fail-loud for a missing pinned
            # file — but BEFORE the stop, so the running agent is left up.
            raise FileNotFoundError(
                f"spec.claude.credentials_file points at {src}, which is not "
                "a file — refusing to stop the running agent to launch a "
                "successor with an unresolvable credential."
            )
        return src, src.parent.name

    account = str(getattr(claude_spec, "account", "") or "").strip()
    if account:
        # Same resolver the SDK/TUI bind uses; raises PinnedAccountError on
        # an absent/expired snapshot (a legit abort-before-stop).
        from ..runtimes._apptainer_creds import resolve_cred_file

        src = resolve_cred_file(config, Path("/dev/null"))
        return src, account

    # Unpinned host-live — out of scope (never swaps).
    return None, ""


def probe_credential_usable(
    credential_path: Path, *, opener: Any = None
) -> tuple[bool, str | None, str | None]:
    """Probe REAL usability of an OAuth credential via the refresh grant.

    Delegates to :func:`_account.token_refresh.refresh_account_credentials`
    (POST the refresh grant, atomic write-back on success). Returns
    ``(usable, failure_kind, reason)`` — NEVER a token value.

    ``usable`` is ``True`` when EITHER:

      * the refresh SUCCEEDED — a fresh access_token was minted and
        atomically persisted, so the successor binds an already-warmed
        credential; OR
      * the failure is NOT a genuine grant rejection (transport / moved
        endpoint / network / unparseable-2xx / ``no-refresh-token`` /
        ``missing-file``). This is the FAIL-OPEN rule: a network blip is not
        a dead token, and blocking a healthy restart is worse than the bug.

    ``usable`` is ``False`` ONLY on
    :data:`_account.token_refresh.FAILURE_REJECTED` — the endpoint EVALUATED
    the grant and REFUSED it (HTTP 4xx ``invalid_grant`` / ``invalid_request``
    after ``refresh_account_credentials``'s own concurrent-rotation re-read
    retry). That is the confirmed stale-but-unexpired incident class: the
    refresh_token is genuinely dead, so booting on it 401s "Login expired".

    ``opener`` is the urllib injection seam ``refresh_account_credentials``
    exposes — passed through untouched so tests exercise the REAL refresh /
    classification path against an injected transport (no mocks).

    FRESH-TOKEN SHORT-CIRCUIT (INCIDENT 2026-07-13 — the probe WAS the outage)
    =========================================================================
    This probe verifies a credential BY REFRESHING IT. The OAuth
    ``refresh_token`` is **SINGLE-USE**, so the probe CONSUMES it and mints +
    persists a NEW access_token. That is a MUTATION, not an inspection — and
    on a shared account it is a fleet-wide one: every OTHER agent pinned to
    that account is still holding the PREVIOUS token, which the rotation
    leaves behind. They 401, and Claude Code renders a 401 as the misleading
    "Login expired · Please run /login" while nothing has expired at all.

    Because this runs on EVERY ``sac agents restart`` (pre-stop) AND in
    ``agent_start``'s force branch, restarting agents to "fix" them ROTATED
    the token again on each one, killing the agents just restarted. The
    operator observed exactly that: even a manual restart came back
    login-required. He was chasing a tail the tool was wagging for him.

    Note it also called ``refresh_account_credentials`` DIRECTLY, bypassing
    the ``sac accounts refresh`` CLI's "skipped; token still fresh
    (TTL >= 2h)" guard — so it refreshed UNCONDITIONALLY, even against a
    token with seven hours of life left.

    So: DO NOT PROBE A FRESH TOKEN. A credential with hours of TTL will boot
    fine; refreshing it to "check" is pure cost. We only spend the single-use
    grant when the token is genuinely near expiry — the same threshold the
    host timer uses, where a refresh was due anyway.

    Residual risk, accepted deliberately: a stale-but-unexpired snapshot
    (future ``expiresAt``, server-invalidated grant) now passes the gate and
    the successor boots and 401s — the original incident this probe was built
    for. That failure is LOCAL (one agent fails to come up, and the watchdog
    restarts it) whereas probing is GLOBAL (every restart kills every other
    agent on the account). Trading a fleet-wide outage for a single-agent
    retry is the right way round. A truly non-mutating probe (a cheap
    authenticated read, not a refresh) is the proper fix and is carded.
    """
    from .._account.token_refresh import (
        FAILURE_REJECTED,
        refresh_account_credentials,
    )

    ttl_s = _credential_ttl_seconds(credential_path)
    if ttl_s is not None and ttl_s >= _PROBE_MIN_TTL_S:
        return (
            True,
            "skipped-token-fresh",
            f"token TTL {ttl_s / 3600:.1f}h >= {_PROBE_MIN_TTL_S / 3600:g}h — "
            f"not probing: a probe refresh would consume the SINGLE-USE "
            f"refresh_token and rotate the shared token, revoking it for "
            f"every other agent on this account",
        )

    result = refresh_account_credentials(credential_path, opener=opener)
    if result.get("success"):
        return True, None, None
    kind = result.get("failure_kind")
    reason = result.get("error")
    if kind == FAILURE_REJECTED:
        return False, kind, reason
    # FAIL-OPEN: transport / response / no-refresh-token / missing-file are
    # NOT a proven-dead token. Proceed rather than wrongly block a healthy
    # restart (the caller logs the WARN).
    return True, kind, reason


def assert_successor_auth_usable(
    config: AgentConfig, *, opener: Any = None
) -> None:
    """PRE-STOP auth pre-flight — raise iff the successor credential is dead.

    ``config`` MUST already be account-rotated (as ``agent_start`` leaves it
    after :func:`_lifecycle._start_preflight._rotate_to_healthy_account`), so
    the probed credential is EXACTLY the one the launch will bind.

    * Nothing to probe (provider / openai / unpinned) → return quietly.
    * Successor credential usable → return quietly (a successful probe-
      refresh also HEALS it — the successor boots on a warm token).
    * Refresh grant REJECTED → raise :class:`RestartPreflightAbort` naming
      the account, path, and remedy. The caller has NOT stopped the running
      container, so the agent is LEFT UP.
    * Ambiguous / transport failure → return quietly with a WARN (fail-open).

    Token values never appear in logs or the raised message.
    """
    name = getattr(config, "name", "?")
    credential_path, label = resolve_successor_credential(config)
    if credential_path is None:
        return  # provider / openai / unpinned — nothing to probe.

    usable, kind, reason = probe_credential_usable(credential_path, opener=opener)
    if usable:
        if kind is not None:
            logger.warning(
                "restart auth pre-flight for %r: successor credential for "
                "account %r could not be positively verified (%s: %s) — "
                "PROCEEDING (fail-open; this is NOT a token rejection, so it "
                "must not block a healthy restart). If the successor boots "
                "dead, run `sac accounts refresh %s` and retry.",
                name,
                label,
                kind,
                reason,
                label,
            )
        return

    raise RestartPreflightAbort(
        f"refusing to restart {name!r}: the successor credential it would "
        f"launch on (account {label!r}, {credential_path}) is unexpired by "
        f"timestamp but its refresh grant was REJECTED by the token endpoint "
        f"(auth pre-flight failed) — booting on it would 401 with "
        f'"Login expired · Please run /login" to every prompt. The running '
        f"container has been LEFT UP (not stopped), so {name!r} keeps working "
        f"on its current credential. Fix: `sac accounts refresh {label}` (or "
        f"`claude /login` as that account, then `sac accounts sync-live`), "
        f"then retry the restart. [{reason}]"
    )


def preflight_from_config_path(config_path: str, *, opener: Any = None) -> None:
    """Path-based pre-flight entry for :func:`_lifecycle._stop.agent_restart`.

    ``agent_restart`` holds a spec PATH (not a loaded config) and stops the
    agent BEFORE the successor ``agent_start`` loads + rotates it. So this
    entry reproduces the launch's account resolution itself — load the spec,
    run the SAME :func:`_lifecycle._start_preflight._rotate_to_healthy_account`
    pick (which may itself raise :class:`_creds.NoHealthyAccountError`, a
    legit abort-before-stop) — then probes the resolved successor credential.

    The rotation's ``[sac:creds]`` notice is suppressed here (throwaway
    stream): this is a dry probe; the REAL ``agent_start`` emits the operator-
    facing rotation line for the launch that actually happens.
    """
    import io

    from ..config import load_config
    from ._start_preflight import _rotate_to_healthy_account

    config = load_config(config_path)
    # Resolve the SAME successor account the launch will pick. NoHealthyAccountError
    # propagates as an abort-before-stop (better than stop-then-fail today).
    _rotate_to_healthy_account(config, log_stream=io.StringIO())
    assert_successor_auth_usable(config, opener=opener)
