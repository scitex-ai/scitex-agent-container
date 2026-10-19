"""TUI tmux session names: the BARE canonical form + the ``tui-`` legacy form.

New sessions are minted BARE — the session name IS the agent name — so sac's
sessions no longer carry a namespace prefix. Sessions minted before this
change are named ``tui-<agent>`` and keep working: every lookup tries the
canonical name first and the legacy name second.

The shape mirrors :mod:`.._jobs._names` (canonical ``JOB_PREFIX`` minted,
``LEGACY_JOB_PREFIX`` still recognised): one module owns the grammar, every
caller resolves through it, and no caller hardcodes the prefix a second time.

A pasted legacy name stays unambiguous: ``candidates("tui-foo")`` is
``("tui-foo", "tui-tui-foo")`` — the bare slot already names the legacy
session, so resolution finds it whichever of the two readings was meant.
"""

from __future__ import annotations

from typing import Iterable, Mapping, Sequence

#: The pre-cutover prefix. Still RECOGNISED (never minted) so agents started
#: by older sac versions keep resolving until they are restarted.
LEGACY_TUI_PREFIX = "tui-"

__all__ = [
    "LEGACY_TUI_PREFIX",
    "agent_of_session",
    "candidate_session_names",
    "canonical_session_name",
    "legacy_session_name",
    "resolve_against_snapshot",
    "resolve_live_session",
    "session_aliases",
    "session_and_aliases",
    "session_present",
]


def canonical_session_name(agent: str) -> str:
    """The session name sac mints for ``agent``: the bare agent name."""
    if not agent:
        raise ValueError("agent name must be non-empty")
    return agent


def legacy_session_name(agent: str) -> str:
    """The pre-cutover ``tui-<agent>`` session name. Recognised, never minted."""
    if not agent:
        raise ValueError("agent name must be non-empty")
    return f"{LEGACY_TUI_PREFIX}{agent}"


def candidate_session_names(agent: str) -> tuple[str, str]:
    """Every session name ``agent`` could be running under, preferred first.

    Canonical bare first, legacy ``tui-`` second. An input that already
    carries the legacy prefix is unambiguous in the bare slot — the first
    candidate IS that live session — so it still yields exactly two tries.
    """
    if not agent:
        raise ValueError("agent name must be non-empty")
    legacy = (
        agent if agent.startswith(LEGACY_TUI_PREFIX) else f"{LEGACY_TUI_PREFIX}{agent}"
    )
    if legacy == agent:
        return (agent, f"{LEGACY_TUI_PREFIX}{agent}")
    return (agent, legacy)


def resolve_live_session(agent: str, sessions: Iterable[str] | None) -> str:
    """The live session for ``agent``: bare when present, else legacy, else bare.

    ``sessions`` is a tmux session listing (or ``None`` when the listing
    could not be read). A ``None`` listing resolves to the canonical name —
    the default a minting path needs — never to a guess about a legacy one.
    """
    bare, legacy = candidate_session_names(agent)
    if sessions is None:
        return bare
    live = set(sessions)
    if bare in live:
        return bare
    if legacy in live:
        return legacy
    return bare


def session_present(agent: str, sessions: Iterable[str] | None) -> bool | None:
    """Three-valued presence: True / False / None (``sessions`` unreadable).

    ``None`` when the listing itself could not be read — absence of evidence,
    never evidence of absence (the same rule as the delivery router).
    """
    if sessions is None:
        return None
    live = set(sessions)
    bare, legacy = candidate_session_names(agent)
    return bare in live or legacy in live


def agent_of_session(session: str) -> str:
    """The agent name a session belongs to: one legacy prefix level stripped."""
    if session.startswith(LEGACY_TUI_PREFIX):
        return session[len(LEGACY_TUI_PREFIX) :]
    return session


def session_aliases(session_name: str) -> tuple[str, ...]:
    """Extra names worth checking alongside an already-resolved ``session_name``.

    For a bare ``foo`` this is ``("tui-foo",)`` — the legacy session the same
    agent may still be running under. For an already-prefixed name there is
    nothing further to try.
    """
    if session_name.startswith(LEGACY_TUI_PREFIX):
        return ()
    return (f"{LEGACY_TUI_PREFIX}{session_name}",)


def session_and_aliases(session_name: str) -> tuple[str, ...]:
    """``session_name`` followed by :func:`session_aliases` (preferred first)."""
    return (session_name, *session_aliases(session_name))


def resolve_against_snapshot(
    session_name: str, snapshot: Sequence[str] | set[str] | Mapping[str, object] | None
) -> bool | None:
    """Three-valued presence for an already-resolved session name + snapshot."""
    if snapshot is None:
        return None
    live = set(snapshot)
    return session_name in live or any(a in live for a in session_aliases(session_name))
