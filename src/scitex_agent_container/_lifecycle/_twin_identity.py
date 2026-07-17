"""Twin identity — the naming algebra and the boot identity gate.

WHO a twin is, kept separate from what it inherits (``_twin.py``: spec
derivation + the host-side transcript seed). Three things live here:

* **Deterministic naming** (``--tag``). :func:`twin_name_for_tag` /
  :func:`resolve_twin_name` — a tag resolves to ``<parent>-forked-<tag>``,
  the SAME id every time. The operator's shape (2026-07-17): 「agent id は
  fork と descriptive name で決定的に付けると良さそう」. WHY it matters: the
  legacy default bumps ``<parent>-twin`` → ``-2`` → ``-3``, so re-running one
  command MINTS A NEW AGENT each time. We already carry ~170 worktrees; a
  second sprawl source is not affordable. A deterministic id is also
  addressable WITHOUT a lookup (you can stop/delete it by name you already
  know) and reapable by name shape.
* **Deterministic session ids.** :func:`twin_session_uuid` — uuid5 of the
  twin's own id, so the session the twin forks into is derivable from its
  name alone rather than discovered from disk.
* **The boot identity gate.** :func:`assert_twin_identity` — refuses to boot
  a twin whose injected identity does not hold. See :class:`TwinIdentityError`
  for why a prompt cannot do this job.

This module is the LEAF of the twin package (``_twin`` imports it, never the
reverse) and therefore owns the shared exception hierarchy: identity/naming is
the first thing that can fail — at command time, before any seeding exists to
fail at.
"""

from __future__ import annotations

import re
import uuid
from typing import Any, Iterable

# Env var carrying the parent's name into the twin's container. Presence
# marks a spec as a twin (the sole trigger for ``seed_twin_from_parent``)
# AND is the value the twin passes as ``assignee=`` to keep card ownership
# with the parent.
TWIN_PARENT_ENV = "SAC_TWIN_PARENT"
# scitex-todo author-identity var — set to the TWIN so writes attribute to it.
TODO_AGENT_ENV = "SCITEX_TODO_AGENT_ID"
# sac self-name var — owned by ``listen_env_flags`` (injected from config.name),
# so we must not let an inherited spec.env copy shadow it with the parent's.
SELF_NAME_ENV = "SAC_NAME"

# Infix joining a twin to its parent: ``<parent>-forked-<tag>``. A twin id
# states BOTH its lineage and its mission, and re-running the same tag
# resolves to the SAME id instead of minting a new agent.
TWIN_NAME_INFIX = "-forked-"
# A tag is a DESCRIPTIVE slug — lowercase alnum with inner hyphens.
# Deliberately strict: the tag becomes an agent id, which is also a directory
# name, an a2a address and a tmux session name, so anything needing quoting or
# escaping is rejected at the door rather than corrupting a path later.
_TWIN_TAG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_TWIN_TAG_MAX = 40
# Fixed namespace for :func:`twin_session_uuid` — the value of
# ``uuid5(NAMESPACE_DNS, "twin.agents.scitex-agent-container")``, hardcoded so
# the derivation is stable across processes AND releases. Recomputing it from
# a string at import would silently re-key every twin the day someone edits
# that string.
TWIN_SESSION_NAMESPACE = uuid.UUID("ba9c1a1a-5a2c-5687-a872-cb2c07a473c6")

__all__ = [
    "SELF_NAME_ENV",
    "TODO_AGENT_ENV",
    "TWIN_NAME_INFIX",
    "TWIN_PARENT_ENV",
    "TWIN_SESSION_NAMESPACE",
    "TwinIdentityError",
    "TwinSeedError",
    "assert_twin_identity",
    "resolve_twin_name",
    "twin_name_for_tag",
    "twin_session_uuid",
    "validate_twin_tag",
]


class TwinSeedError(RuntimeError):
    """Raised when host-side twin context-inheritance cannot complete.

    Fail-loud contract: a twin whose parent has no resolvable live session,
    or whose parent transcript is missing on disk, must NOT boot into an
    empty session pretending to have inherited the parent's context — that
    would silently defeat the entire point of a twin. The twin start aborts
    with this error instead.

    Also the base for every twin precondition failure (bad ``--tag``, taken
    name, broken identity) so one ``except TwinSeedError`` at each call site
    catches the whole family.
    """


class TwinIdentityError(TwinSeedError):
    """Raised when a twin's injected identity does not hold at boot.

    WHY THIS IS A GATE AND NOT A PROMPT: a twin boots INTO THE PARENT'S
    TRANSCRIPT, in which the parent says "I am <parent>" in its own voice,
    hundreds of turns deep. That transcript is far more persuasive to the
    model than one prompt line asserting otherwise — on 2026-07-03 we hit
    exactly this and two agents believed they were one identity. Env is the
    only channel that outranks the transcript, because it is not text the
    model can argue with; so we verify the env really carries the twin's
    identity BEFORE the transcript gets a chance to speak, and refuse the
    boot if it does not.

    SCOPE — ADR-0019 draws a line here, keep it: this gates AGENT IDENTITY,
    which env carries. It does NOT gate CARD OWNERSHIP. ``add_task`` has no
    env default for the owner (verified against ``scitex_todo._store``), so
    there is nothing to assert with, and the prompt-level
    ``assignee=<parent>`` rule stays load-bearing. Gate what an env can
    carry; prompt what it cannot.
    """


def validate_twin_tag(tag: str) -> str:
    """Return the normalised ``tag``, or raise :class:`TwinSeedError`.

    A tag must be a DESCRIPTIVE lowercase slug (``review-pr-712``,
    ``figures``) — the thing that makes ``<parent>-forked-<tag>`` readable at
    a glance, which is the whole point of the operator's naming ask and the
    opposite of an opaque ``claude/agent-<hash>``. Fails loud rather than
    sanitising: silently rewriting a caller's tag would break the
    determinism contract (the id you asked for is the id you get), and the
    id ends up as a path / a2a address / tmux session name where a stray
    space or slash corrupts something far from here.
    """
    raw = "" if tag is None else str(tag).strip()
    if not raw:
        raise TwinSeedError(
            "twin --tag must be a non-empty descriptive slug (e.g. "
            "'review-pr-712'); it names the twin's mission in its agent id."
        )
    if len(raw) > _TWIN_TAG_MAX:
        raise TwinSeedError(
            f"twin --tag {raw!r} is {len(raw)} chars; the max is "
            f"{_TWIN_TAG_MAX} (the tag becomes part of an agent id, which is "
            "also a directory and tmux session name)."
        )
    if not _TWIN_TAG_RE.match(raw):
        raise TwinSeedError(
            f"twin --tag {raw!r} is not a valid descriptive slug. Use "
            "lowercase letters/digits separated by single hyphens (e.g. "
            "'review-pr-712', 'figures'). The tag becomes part of the twin's "
            "agent id — a directory name, an a2a address and a tmux session "
            "name — so it must need no quoting or escaping."
        )
    return raw


def twin_name_for_tag(parent_name: str, tag: str) -> str:
    """Return the DETERMINISTIC twin id ``<parent>-forked-<tag>``.

    Pure and total: the same ``(parent, tag)`` always yields the same id, so
    re-running a twin command targets the SAME twin rather than minting a
    new one. Validates the tag (fail loud) on the way through.
    """
    return f"{parent_name}{TWIN_NAME_INFIX}{validate_twin_tag(tag)}"


def twin_session_uuid(twin_name: str) -> str:
    """Return the twin's DETERMINISTIC session uuid, derived from its id.

    ``uuid5(TWIN_SESSION_NAMESPACE, twin_name)`` — a real RFC-4122 v5 UUID,
    which is exactly what ``claude --session-id <uuid>`` requires ("must be a
    valid UUID") and what ``ClaudeAgentOptions(session_id=...)`` forwards.

    WHY derive rather than generate: the twin's session id becomes a property
    OF ITS NAME. Anything that knows the twin's id can compute the session it
    forked into — no lookup, no marker file to read, and a re-run of the same
    tag maps to the same session id instead of scattering one twin's history
    across a fresh uuid per attempt.
    """
    return str(uuid.uuid5(TWIN_SESSION_NAMESPACE, twin_name))


def resolve_twin_name(
    parent_name: str,
    requested: str | None,
    existing: Iterable[str] | None,
    tag: str | None = None,
) -> str:
    """Return the twin's agent name.

    Three modes, in precedence order:

    * ``tag`` → the DETERMINISTIC ``<parent>-forked-<tag>`` (never bumped —
      that is the point: the same tag is the same twin).
    * ``requested`` → returned verbatim (the caller decides whether a clash
      is an error).
    * neither → the legacy default ``<parent>-twin``, bumped to
      ``<parent>-twin-2`` / ``-3`` / ... on the first free suffix so a parent
      can carry several live twins at once.

    ``tag`` and ``requested`` are MUTUALLY EXCLUSIVE (fail loud): they are
    two answers to one question, and silently letting one win would make the
    resulting id depend on an invisible precedence rule.
    """
    if tag is not None and requested:
        raise TwinSeedError(
            "twin --tag and --name are mutually exclusive: --tag derives the "
            f"deterministic id {parent_name}{TWIN_NAME_INFIX}<tag>, while "
            "--name sets an arbitrary one. Pass exactly one."
        )
    if tag is not None:
        return twin_name_for_tag(parent_name, tag)
    if requested:
        return requested
    taken = {str(n) for n in (existing or ())}
    base = f"{parent_name}-twin"
    if base not in taken:
        return base
    n = 2
    while f"{base}-{n}" in taken:
        n += 1
    return f"{base}-{n}"


def assert_twin_identity(config: Any) -> bool:
    """Verify a twin's injected identity at boot, or raise.

    A strict no-op (returns ``False``) for a non-twin — no
    :data:`TWIN_PARENT_ENV` in ``config.env``. For a twin, ALL must hold or
    the boot is refused with :class:`TwinIdentityError`:

      1. :data:`TODO_AGENT_ENV` is present and non-empty — the twin has an
         author identity at all;
      2. it is NOT the parent's name — the failure that actually bit us:
         an inherited ``spec.env`` copy still saying the parent, so every
         write the twin makes is attributed to (and looks like) the parent;
      3. it EQUALS the twin's own ``config.name`` — the agent sac is
         starting and the identity scitex-todo will stamp are one thing.
         Any drift here means two names for one process, and every later
         "who did this?" answer is a coin flip.

    Runs on EVERY twin boot, not just the first: the inherited transcript
    keeps insisting "I am <parent>" for the twin's whole life, so the gate
    that outranks it cannot be a first-boot-only step. Called from
    ``_twin.seed_twin_from_parent`` BEFORE its first-boot early-return.

    Returns ``True`` iff the identity was checked and holds.
    """
    env = getattr(config, "env", None) or {}
    parent_name = str(env.get(TWIN_PARENT_ENV, "") or "").strip()
    if not parent_name:
        return False

    twin_name = str(getattr(config, "name", "") or "").strip()
    injected = str(env.get(TODO_AGENT_ENV, "") or "").strip()

    if not injected:
        raise TwinIdentityError(
            f"twin {twin_name!r}: {TODO_AGENT_ENV} is not set in its env, so "
            f"its scitex-todo writes would fall back to the ambient identity "
            f"— which, in a container booting {parent_name}'s transcript, is "
            f"how a twin starts writing as {parent_name}. Refusing to boot. "
            "(derive_twin_spec sets this; a hand-edited spec.env dropped it.)"
        )
    if injected == parent_name:
        raise TwinIdentityError(
            f"twin {twin_name!r}: {TODO_AGENT_ENV} is {injected!r} — its "
            f"PARENT's identity, not its own. The twin would author every "
            f"scitex-todo write as {parent_name} while running as a separate "
            "agent, which is the two-agents-one-identity bug of 2026-07-03. "
            "Refusing to boot. (Expected "
            f"{TODO_AGENT_ENV}={twin_name!r}; an inherited spec.env copy "
            "shadowing it is the usual cause.)"
        )
    if injected != twin_name:
        raise TwinIdentityError(
            f"twin {twin_name!r}: {TODO_AGENT_ENV} is {injected!r}, which is "
            f"neither its own name nor a recognised identity. sac would start "
            f"the agent as {twin_name!r} while scitex-todo attributed its "
            f"writes to {injected!r} — one process, two names, and no way to "
            "answer 'who did this?' afterwards. Refusing to boot."
        )
    return True
