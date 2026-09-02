"""Agent-list AUTH status — "green" vs "green AND actually able to call the API".

THE PROBLEM THIS SOLVES
    ``sac agents list`` derived its Status from a LIVENESS probe: does the tmux
    session exist, and is its pane process alive? For a wedged agent the answer
    is YES — so it renders ``running``, in green, forever. Meanwhile every API
    call it makes is rejected, it sits under an auth banner, and it does nothing
    at all. Claude Code never re-reads the credentials file, so only a restart
    recovers it; none of that is visible from liveness. The operator could not
    tell working-green from dead-green, and on a fleet of ~30 agents sharing one
    OAuth account that ambiguity has cost him real time.

    So: **tmux-up is not operational**, and Status must say which.

WHY ``auth-failed`` AND NOT "login required"
    Claude Code prints ``Login expired · Please run /login`` for every 401, and
    on this fleet that text is usually FALSE. The real mechanism (proven
    2026-07-13, four agents lost at once): a sibling agent ran its own OAuth
    refresh, consumed the single-use ``refresh_token``, rotated the access token,
    and thereby REVOKED the token every other process was holding. Nothing
    expired, and no login is required — a restart is.

    So the status asserts only the part we can verify: this agent's auth is
    failing / it cannot call the API. The CAUSE is carried separately, as a
    diagnosis (``auth_reason``: revoked / expired / unknown) with the remedy it
    implies (``auth_remedy``: restart / login) — see
    ``_account.auth_failure_reason``. Trusting that banner is why this bug
    survived so long; the list must not repeat its mistake.

CACHE-READ ONLY — NEVER PROBE HERE
    The auth check is expensive and cannot live on this path. Deciding whether a
    banner is REAL (a wedged agent) or PROSE (an agent merely discussing the
    incident) requires capturing the agent's pane TWICE, seconds apart, and
    confirming the banner is frozen — see ``cli_pkg._auth_status`` /
    ``_runners._tmux.auth_status``. Doing that per row would make ``sac agents
    list`` take MINUTES and would undo PR #635's perf work (the list was
    ~296ms/row).

    Instead the WATCHDOG probes and PERSISTS its verdict to state.db, and this
    module only READS THAT CACHE — one bulk query for the whole fleet
    (:func:`all_auth_states`), then a dict lookup per row. The honesty rules
    (stale evidence, superseded-by-restart) live in ``_state.auth_state`` as
    pure functions; we apply them and hand the renderer plain fields.
"""

from __future__ import annotations

__all__ = [
    "LIVE_STATUSES",
    "STATUS_AUTH_FAILED",
    "all_auth_states",
    "is_live_status",
    "resolve_auth",
]

# The status of an agent that is UP but NOT OPERATIONAL. Kept DISTINCT from
# ``running`` because telling those two apart at a glance is the entire point.
STATUS_AUTH_FAILED = "auth-failed"

# Statuses meaning "this agent's process is UP". ``auth-failed`` MUST be a
# member, and every consumer must go through :func:`is_live_status` rather than
# comparing against ``"running"`` itself, because two call sites break otherwise:
#
#   * ``_agent_list_render`` — the DEFAULT view shows only live rows. An
#     auth-failed row filtered out as "not running" would HIDE the one status the
#     operator asked to be shown: an absurd own-goal.
#   * ``lifecycle._selection._enumerate_running`` — the live set swept by BOTH
#     ``restart --all-running`` and ``stop --all-running``. A restart is the cure
#     for the common (revoked) failure, so dropping auth-failed rows would skip
#     exactly the agents that most need restarting.
#
# One definition, imported by both, so those rules cannot drift apart.
LIVE_STATUSES: tuple[str, ...] = ("running", STATUS_AUTH_FAILED)


def is_live_status(status: str | None) -> bool:
    """True when ``status`` means the agent's process is up (see LIVE_STATUSES)."""
    return (status or "") in LIVE_STATUSES


def all_auth_states() -> dict[str, dict]:
    """``{agent_name: cached_verdict}`` for the whole fleet, in ONE db read.

    Shaped exactly like ``_agent_list._all_port_claims``: a single bulk query,
    looked up per row from the returned dict — never a per-row db hit.

    Tolerant by design. A state.db that does not exist, an ``agent_auth_state``
    table no watchdog has ever written, or any storage hiccup all map to ``{}`` —
    "nobody has been checked yet" — which every row then renders honestly as
    UNKNOWN rather than as verified-green. An auth-cache miss must never crash
    ``sac agents list``, and must never slow it down.
    """
    # stx-allow: fallback (reason: the auth cache is an ENRICHMENT of the list;
    # a missing/locked/schema-less db means "not checked yet", never a failure.)
    try:
        from ..._state.auth_state import list_auth_states

        return list_auth_states()
    except Exception:  # stx-allow: fallback (reason: see inline comment)
        return {}


def resolve_auth(
    name: str,
    states: dict[str, dict],
    started_at: str | None,
    status: str,
) -> tuple[dict, str]:
    """``(auth_fields, status)`` for one row — the status may be UPGRADED.

    Returns the seven always-present auth keys (see
    ``_state.auth_state.verdict_for``) plus the status they imply: a LIVE agent
    carrying a current ``auth_failed`` verdict becomes :data:`STATUS_AUTH_FAILED`
    instead of ``running``. Any other status is returned untouched.

    A NON-live agent gets the no-verdict shape rather than its cached row. The
    verdict describes a LIVE process, so reporting ``auth_failed`` for a STOPPED
    agent would be a claim about a process that is not even there — and would
    light up the fleet view with alarms about agents nobody is running. Keys stay
    present-but-empty so the ``--json`` row shape is uniform.
    """
    from ..._state.auth_state import verdict_for

    if not is_live_status(status):
        return verdict_for(None), status
    auth = verdict_for(states.get(name), started_at=started_at)
    if auth["auth_failed"]:
        return auth, STATUS_AUTH_FAILED
    return auth, status
