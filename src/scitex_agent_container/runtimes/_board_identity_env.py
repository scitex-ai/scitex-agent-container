"""Board-identity env injection + the unexpanded-``${VAR}`` validator.

WHY THIS MODULE EXISTS (INCIDENT 2026-07-19, live data corruption)
==================================================================
scitex-cards renamed its identity variable
``SCITEX_TODO_AGENT_ID`` -> ``SCITEX_CARDS_AGENT_ID`` and now warns on every
read that the ``SCITEX_TODO_*`` spelling is "honoured for one transition
window only". sac injected ONLY the old name. A ``.mcp.json`` that had been
migrated to reference the NEW name therefore expanded ``${SCITEX_CARDS_AGENT_ID}``
against a container env where that variable did not exist — and the LITERAL
placeholder text survived into the store. Rows in the live cards DB carry
``created_by = '${SCITEX_CARDS_AGENT_ID}'``: the variable NAME, unexpanded,
persisted as though it were an answer. The board cannot say who wrote those
cards. The count kept CLIMBING while the bug was live — 7 when first
reported, 15 measured a few hours later on 2026-07-19 — because every new
card written by an affected agent added one. Treat any figure here as a
floor at its timestamp, not a total.

Two independent defects, so two independent fixes here.

1. THE MISSING NAME — :func:`apply_board_identity_alias`
--------------------------------------------------------
sac now injects BOTH spellings with the SAME value. Not a swap: the fleet's
installed scitex-cards versions differ, so an agent still running a
pre-rename build reads only ``SCITEX_TODO_AGENT_ID`` and dropping it would
break that agent's writes exactly the way the missing new name broke these.
Both names cost one extra ``--env`` flag and cover both halves of the fleet
for the length of the transition window.

REMOVING THE LEGACY NAME — the condition. Drop
:data:`LEGACY_BOARD_ID_ENV` from this module (and the alias that mirrors it)
once BOTH hold:

  a. Every scitex-cards install reachable by the fleet is at or past the
     release that removed the ``SCITEX_TODO_*`` compatibility shim — i.e.
     scitex-cards has actually CLOSED its transition window, not merely
     announced it. The observable signal is that the deprecation warning
     ("deprecated SCITEX_TODO_* environment names in use") no longer appears
     on a read, because the code emitting it is gone.
  b. No agent spec still declares ``SCITEX_TODO_AGENT_ID`` — neither in
     ``spec.env`` nor in ``apptainer.raw_args`` (those specs live in the
     operator's dotfiles repo, not in sac, so this is a cross-repo check).

Until both are true, injecting both names is strictly safer than either one
alone. Removing it earlier re-creates this incident with the names swapped.

2. THE NON-ANSWER STORED AS AN ANSWER — :func:`reject_unexpanded_env`
----------------------------------------------------------------------
The deeper bug is not the rename. It is that a value meaning "I could not
resolve this" was accepted and written as data. ``${SCITEX_CARDS_AGENT_ID}``
is not an agent id; it is the shape of a substitution that did not happen.
Per the constitution §2 rule — "Give the dataclass a validator, so a
malformed answer fails where it is built, not three layers downstream" — any
value that still LOOKS like an unexpanded shell substitution is rejected at
the boundary where sac sets it, loudly, naming the variable, the offending
value, and the likely cause. It is never stored and never silently replaced
by a default: a silent default would put a WRONG author on the card, which is
worse than a launch that stops and says why.

Note the asymmetry with :func:`.._to_home_text.interpolate_env`, which
deliberately LEAVES ``${VAR}`` literals in materialised *files* for runtime
expansion inside the container. That is correct there — the file is expanded
later, by the agent's own environment. It is exactly what must never happen
*here*: this module renders the container's ``--env`` flags, which are the
end of the line. Nothing downstream expands them, so a ``${VAR}`` reaching
this point has already failed.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Iterable, Mapping

logger = logging.getLogger(__name__)

#: The board-identity variable scitex-cards reads TODAY.
BOARD_ID_ENV = "SCITEX_CARDS_AGENT_ID"

#: The pre-rename spelling. Injected alongside :data:`BOARD_ID_ENV` for the
#: transition window only — see the module docstring for the two conditions
#: that must both hold before this is removed.
LEGACY_BOARD_ID_ENV = "SCITEX_TODO_AGENT_ID"

#: The whole value is nothing but a substitution ref: ``${VAR}``.
_WHOLE_REF_RE = re.compile(r"^\$\{.*\}$", re.DOTALL)

#: A substitution ref appears anywhere in the value: ``prefix-${VAR}-suffix``.
_ANY_REF = "${"


class UnexpandedEnvValueError(ValueError):
    """An env value still contains an unexpanded ``${VAR}`` substitution.

    Raised at the boundary where sac SETS a container env var, so the launch
    fails where the bad value is built rather than three layers downstream in
    somebody else's database.
    """


def assert_expanded(name: str, value: str) -> None:
    """Raise :class:`UnexpandedEnvValueError` if ``value`` never expanded.

    Rejects a value that IS a substitution ref (``${VAR}``) and one that
    merely CONTAINS one (``x-${VAR}``); the second is just as unexpanded and
    just as unusable as data, and letting it through would leave a
    half-resolved identity on the board.

    The message names all three things a reader needs in order to act: the
    variable being set, the value that is wrong, and the likeliest cause
    (a renamed variable whose new name nobody exports).
    """
    if not isinstance(value, str) or _ANY_REF not in value:
        return
    shape = "is" if _WHOLE_REF_RE.match(value) else "contains"
    raise UnexpandedEnvValueError(
        f"refusing to set {name}={value!r}: the value {shape} an UNEXPANDED "
        f"shell substitution, which means the variable it refers to was not "
        f"set in the environment doing the expansion. Storing it would write "
        f"the variable NAME where a value belongs (INCIDENT 2026-07-19: cards "
        f"recorded created_by='${{{BOARD_ID_ENV}}}'). Likely cause: a "
        f"RENAMED variable that is not exported — a config referencing the new "
        f"name while only the old name is injected expands to nothing and the "
        f"literal ${{...}} survives. Export the referenced variable, or fix "
        f"the reference to name one that is actually set."
    )


def reject_unexpanded_env(env: Mapping[str, Any]) -> dict[str, str]:
    """Validate every pair in ``env``; return it as a plain ``{str: str}``.

    Pure — the input is not mutated. Raises on the FIRST offending pair
    (keys are visited in sorted order so the failure is deterministic and a
    re-run names the same variable).
    """
    out: dict[str, str] = {str(k): str(v) for k, v in env.items()}
    for key in sorted(out):
        assert_expanded(key, out[key])
    return out


def raw_args_env(raw_args: Iterable[Any] | None) -> dict[str, str]:
    """Extract ``KEY=VALUE`` pairs that ``raw_args`` sets via apptainer ``--env``.

    Both spellings that occur in real specs are parsed, because both occur:

    * SPLIT — ``["--env", "SCITEX_TODO_AGENT_ID=name"]`` (two argv elements)
    * GLUED — ``["--env=SCITEX_TODO_AGENT_ID=name"]`` (one argv element)

    Later duplicates win, matching apptainer's own ``--env`` last-wins
    precedence. Returns ``{}`` for ``None`` / empty. Malformed entries (a
    trailing ``--env`` with nothing after it, a value with no ``=``) are
    skipped rather than raising: this is a READ of an operator escape hatch
    sac does not own, and sac must not refuse to launch over a flag it merely
    failed to recognise.
    """
    tokens = [str(a) for a in (raw_args or [])]
    found: dict[str, str] = {}
    index = 0
    while index < len(tokens):
        token = tokens[index]
        pair: str | None = None
        if token == "--env" and index + 1 < len(tokens):
            pair = tokens[index + 1]
            index += 2
        elif token.startswith("--env="):
            pair = token[len("--env=") :]
            index += 1
        else:
            index += 1
            continue
        key, sep, value = pair.partition("=")
        if sep and key:
            found[key] = value
    return found


def apply_board_identity_alias(
    env: Mapping[str, Any],
    *,
    raw_args: Iterable[Any] | None = None,
    agent_name: str | None = None,
) -> dict[str, str]:
    """Ensure the CANONICAL board identity is set. Returns a NEW dict.

    ASYMMETRIC BY DESIGN — reads both names, writes only the new one.

    The identity is READ from either spelling, because specs are mid-migration
    and many still declare only the legacy name. It is WRITTEN only as
    :data:`BOARD_ID_ENV`. sac must never re-introduce the legacy name: the
    operator is removing ``SCITEX_TODO_*`` from the specs entirely
    (2026-07-19), and an injector that helpfully mirrored it back would
    silently undo that migration one launch at a time — the deleted variable
    would keep reappearing and nobody would understand why.

    So this function is also the MIGRATION BRIDGE: a spec that still declares
    only the legacy name still gets a working canonical identity, which is
    what lets the legacy name be deleted from specs incrementally instead of
    in one flag-day sweep.

    Verified before narrowing (2026-07-19): scitex-cards 0.17.0 resolves from
    :data:`BOARD_ID_ENV` alone and emits no deprecation warning, so dropping
    the legacy write costs nothing against the installed fleet.

    The EFFECTIVE identity is resolved the way apptainer resolves it, so what
    is written can never disagree with what the container actually receives:
    ``raw_args`` are appended to the argv AFTER the ``--env`` flags rendered
    from ``env`` (see ``_apptainer_build_argv.build_run_argv``), and apptainer
    ``--env`` is last-wins — so a ``raw_args``-declared identity OVERRIDES the
    one in ``spec.env``. That is why ``raw_args`` is consulted here at all:
    for most of the fleet the identity is declared ONLY there, and a value
    derived from ``spec.env`` alone would mirror an identity the agent never
    runs with (or mirror nothing).

    An identity already declared explicitly is never clobbered — this only
    FILLS IN the canonical name when it is absent. Every value is validated,
    so a ``${...}`` identity fails here instead of reaching a card.
    """
    merged = reject_unexpanded_env(env)
    from_raw = raw_args_env(raw_args)
    for name in (BOARD_ID_ENV, LEGACY_BOARD_ID_ENV):
        if name in from_raw:
            assert_expanded(name, from_raw[name])

    current = (from_raw.get(BOARD_ID_ENV) or merged.get(BOARD_ID_ENV) or "").strip()
    legacy = (
        from_raw.get(LEGACY_BOARD_ID_ENV) or merged.get(LEGACY_BOARD_ID_ENV) or ""
    ).strip()
    identity = current or legacy
    derived_from_name = False
    if not identity:
        # NEITHER SPELLING IS DECLARED. Before 2026-08-20 this returned an env
        # with NO board identity at all, and the agent launched unable to say
        # who it was. proj-scitex-hub reported exactly that: scitex-cards
        # unusable, no SCITEX_CARDS_AGENT_ID, no SCITEX_CARDS_DB. The fleet
        # baseline promises every agent that its identity "is already wired
        # into your environment" and tells it to report a failure to resolve
        # as a sac bug rather than work around it. This is that bug.
        #
        # THE AGENT'S OWN NAME IS NOT A GUESS, and that is measured rather than
        # asserted. Across compute-03's 136 specs and compute-04's 121: 96
        # declare an identity and it EQUALS the agent name; 9 differ, of which
        # 6 are TEMPLATES carrying placeholders and 3 are deliberate aliases
        # (scitex-agent-container-04 -> scitex-agent-container,
        # scitex-hub-mobile-ux -> scitex-hub, _template_handyman ->
        # local-coder); 16-17 declare nothing at all. Every one of the aliases
        # DECLARES its identity, and this branch only runs when none is
        # declared — so deriving can never override a deliberate alias. It
        # only fills the hole the 16-17 fall into.
        #
        # This is the opposite of the silent default this module warns about.
        # That warning is about substituting a value when the answer is
        # UNKNOWN, which puts a wrong author on a card. Here the answer is
        # known: the spec's own name is the authority sac launched the agent
        # under. Writing nothing is what produced a wrong (empty) author.
        if not agent_name or not str(agent_name).strip():
            return merged
        identity = str(agent_name).strip()
        assert_expanded(BOARD_ID_ENV, identity)
        derived_from_name = True

    if BOARD_ID_ENV in from_raw or (merged.get(BOARD_ID_ENV) or "").strip():
        return merged

    merged[BOARD_ID_ENV] = identity
    if derived_from_name:
        logger.info(
            "board_identity: injected %s=%s (the spec declared NEITHER "
            "spelling, so the identity is the agent's own name — without this "
            "the agent launches unable to say who it is)",
            BOARD_ID_ENV,
            identity,
        )
    else:
        logger.info(
            "board_identity: injected %s=%s (derived from the legacy %s, which "
            "is read but deliberately NOT written back)",
            BOARD_ID_ENV,
            identity,
            LEGACY_BOARD_ID_ENV,
        )
    return merged


__all__ = [
    "BOARD_ID_ENV",
    "LEGACY_BOARD_ID_ENV",
    "UnexpandedEnvValueError",
    "apply_board_identity_alias",
    "assert_expanded",
    "raw_args_env",
    "reject_unexpanded_env",
]
