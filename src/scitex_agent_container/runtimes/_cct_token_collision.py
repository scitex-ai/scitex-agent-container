"""ONE bot token per AGENT, checked in the SPECS — before any process starts.

THE FAULT, AND WHY THE EXISTING DETECTOR CANNOT SEE IT
-----------------------------------------------------
Telegram's ``getUpdates`` admits exactly ONE consumer per bot token, GLOBALLY.
:mod:`._cct_poller_singleton` observes that invariant by reading ``/proc``, and
it is HOST-SCOPED. Measured 2026-08-22: ``scitex-hub`` on compute-04 and
``proj-scitex-hub`` on compute-03 resolved to the SAME bot and both ran a live
telegram server — and the per-host process check returned OK on BOTH hosts
while the conflict was live across them.

Killing the process ended that incident and fixed nothing: the two SPECS still
resolve to the same slot, so the collision returns on the next start. That is
the gap this closes, and it needs no cross-host probing at all, because the
fault is STATIC — it is visible in the specs plus the pool, before anything
runs.

WHAT THIS IS
------------
A verdict over :mod:`._cct_token_census`, which asks
:func:`._cct_token_resolution.resolve_cct_token` — the SAME derivation
:func:`._cct_token_pool.ensure_cct_bot_token` writes with, not a second
opinion about it — what each spec would take. Two agents on one fingerprint is
the fault, and the report names BOTH agents and BOTH hosts, because the remedy
is a config decision about which one yields.

Read-only. It starts nothing, writes nothing, and never reads a token value:
only ``sha256:<12hex>`` fingerprints, slot NAMES and pool source PATHS leave
here — the same strings sac's logs already carry.

AN AGENT WITH NO TOKEN IS NEVER A VIOLATION HERE
------------------------------------------------
Three populations hold nothing and so cannot collide, and conflating them is
how a check stops being read. The census keeps them apart; this module counts
all three in :meth:`TokenCollisionVerdict.population` and alarms on none:

* DISABLED — an explicit empty ``CCT_BOT_TOKEN``. **This is the invariant
  upheld by hand** (the handyman family); flagging it would be flagging the
  answer.
* NO-CHANNEL — the spec never asks for the rail.
* UNRESOLVED — it asks and nothing resolves. Named in every result, never
  folded silently into the clean count, and deliberately NOT alarming HERE: it
  is a different fault (mute and deaf, not a collision), it already has two
  owners — ``sac agents cct-audit`` and :mod:`._cct_rail_alarm` — and it is the
  MAJORITY of the fleet, because the channel request is inherited from the spec
  templates. Measured 2026-08-12: 81 specs declare the channel and 15 resolve a
  token. An alarm that fires on 66 agents forever is one the operator learns to
  scroll past, which is the failure this subsystem exists to prevent.

:data:`SCOPE_NOTE` states the other half of the boundary in every result: this
sees ACROSS hosts and CANNOT see an orphaned process holding a token whose spec
no longer requests it. The two checks are complements; neither alone is
sufficient, and each result points at the other.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from ._cct_token_census import SpecCensus, TokenClaim, census_specs
from ._secret_pool import PoolRead, _pool_source_label, read_pool

#: Every spec that claims a bot claims a DIFFERENT one. Zero claimants is OK:
#: nothing can conflict with nothing.
COLLISION_OK = "ok"
#: Two or more specs resolve to the SAME bot token. This is the 409 conflict,
#: predicted from configuration rather than found after the fact.
COLLISION_VIOLATION = "violation"
#: sac could not assert the invariant — the pool read was inconclusive, a spec
#: would not load, or the spec tree could not be enumerated. NOT a soft OK.
COLLISION_UNKNOWN = "unknown"

#: The limit of this check, stated in every result rather than left to be
#: discovered — the sibling check states its own, and the two are opposite.
#:
#: This one is STATIC and FLEET-WIDE: it reads SPECS plus the pool, so it sees
#: the cross-host collision ``_cct_poller_singleton`` structurally cannot. It
#: sees no PROCESS, so an ORPHANED poller still holding a token whose spec no
#: longer requests it is invisible here — which is exactly what the host-local
#: check is for.
SCOPE_NOTE = (
    "STATIC and FLEET-WIDE: this reads SPECS + the secrets pool, so it sees a "
    "collision split across hosts, which the host-local process check "
    "structurally cannot. It sees no PROCESS at all, so an ORPHANED poller "
    "holding a token whose spec no longer requests it is invisible here — run "
    "`sac doctor --pollers` on each host for that half. Neither check alone is "
    "a fleet all-clear."
)


@dataclass(frozen=True)
class TokenCollision:
    """Two or more specs resolving to the SAME bot token. The fault itself."""

    token_fp: str
    claims: tuple[TokenClaim, ...]

    @property
    def agents(self) -> tuple[str, ...]:
        return tuple(c.agent for c in self.claims)

    @property
    def hosts(self) -> tuple[str, ...]:
        return tuple(c.host or "(unpinned)" for c in self.claims)

    @property
    def cross_host(self) -> bool:
        """True when the claimants are pinned to DIFFERENT hosts.

        The case no per-host process check can ever see, and the shape of the
        2026-08-22 incident. Worth naming in the report, because it tells the
        reader that killing a process on one host has not settled it.
        """
        return len({c.host for c in self.claims if c.host}) > 1

    def describe(self) -> str:
        """``agent@host + agent@host`` — both names, both hosts, one line."""
        return " + ".join(f"{c.agent}@{c.host or 'unpinned'}" for c in self.claims)

    def to_dict(self) -> dict:
        """JSON-friendly projection (for ``--json`` surfaces)."""
        return {
            "token_fp": self.token_fp,
            "agents": list(self.agents),
            "hosts": list(self.hosts),
            "cross_host": self.cross_host,
            "claims": [c.to_dict() for c in self.claims],
        }


@dataclass(frozen=True)
class TokenCollisionVerdict:
    """The fleet-wide static verdict. ``state`` is OK / VIOLATION / UNKNOWN."""

    state: str
    census: SpecCensus = SpecCensus()
    collisions: tuple[TokenCollision, ...] = ()
    #: False when the spec tree itself could not be enumerated — the reason a
    #: zero count must not read as OK.
    scanned: bool = True
    detail: str = ""

    @property
    def is_alarming(self) -> bool:
        """True for the two states a human must be told about."""
        return self.state in (COLLISION_VIOLATION, COLLISION_UNKNOWN)

    @property
    def distinct_fingerprints(self) -> int:
        """How many distinct bots the claiming specs resolve to."""
        return self.census.distinct_fingerprints

    def population(self) -> str:
        """What was actually examined. Never let a clean count stand alone.

        A peer published a clean census that was 3-of-10 and read as 3-of-3.
        ``0 collisions across 0 specs`` and ``0 across 24`` are different facts
        and must not render the same, so every count that shaped this verdict
        is stated: how many specs, how many claim a bot, and each of the three
        populations that hold none.
        """
        c = self.census
        return (
            f"{c.examined} spec(s) examined, "
            f"{len(c.claims)} claim a bot token "
            f"({c.distinct_fingerprints} distinct), "
            f"{len(c.unresolved)} request the rail and resolve nothing, "
            f"{len(c.disabled)} deliberately tokenless, "
            f"{len(c.no_channel)} never request it, "
            f"{len(c.unreadable)} unreadable"
        )

    def summary(self) -> str:
        """One-line human summary of the verdict."""
        c = self.census
        if self.state == COLLISION_VIOLATION:
            worst = "; ".join(
                f"{x.token_fp} claimed by {x.describe()}" for x in self.collisions
            )
            return f"{len(self.collisions)} bot token(s) claimed twice: {worst}"
        if self.state == COLLISION_UNKNOWN:
            if not self.scanned:
                return "unknown — the spec tree could not be enumerated"
            if c.unreadable:
                return (
                    f"unknown — {len(c.unreadable)} of {c.examined} spec(s) would "
                    "not load, so their claims were never computed"
                )
            return "unknown — the pool read was not conclusive"
        return (
            f"{len(c.claims)} spec(s) claim a bot token, "
            f"{c.distinct_fingerprints} distinct — no bot is claimed twice"
        )

    def hint(self) -> str:
        """What to DO. Empty for OK — an all-clear needs no remedy.

        The two alarming states need DIFFERENT actions: a violation is a config
        decision about which agent yields; an unknown is an unread instrument
        whose vantage point must be fixed first.
        """
        c = self.census
        if self.state == COLLISION_VIOLATION:
            pairs = "; ".join(x.describe() for x in self.collisions)
            return (
                "Two or more SPECS resolve to the same bot token, so whichever "
                "of them run are 409-ing each other and the operator's inbound "
                "messages are dropped. Killing a process does NOT fix this — the "
                f"specs still collide ({pairs}), so it returns on the next "
                "start. Decide which agent OWNS the bot, then for each of the "
                "others do ONE of: (a) give it its own bot and name the slot "
                "with CCT_BOT_TOKEN_SLOT under spec.apptainer.env, (b) set "
                "CCT_BOT_TOKEN to an empty value under spec.apptainer.env so it "
                "is tokenless BY DECLARATION (the handyman pattern), or (c) drop "
                "'server:claude-code-telegrammer' from spec.claude.channels if "
                "it needs no Telegram rail. Then re-run: it must read ok. sac "
                "does not choose for you and does not refuse the start — this is "
                "a detector."
            )
        if self.state == COLLISION_UNKNOWN:
            if not self.scanned:
                return (
                    f"The spec tree {c.agents_dir or '(unresolved)'} could not be "
                    "enumerated, so NOTHING was learned — this is not an "
                    "all-clear. Re-run where the specs live."
                )
            if c.unreadable:
                return (
                    "These specs would not load, so sac could not compute which "
                    "bot they take and cannot assert the invariant over them: "
                    + ", ".join(c.unreadable)
                    + ". Fix them (`sac agents check <name>`) and re-run."
                )
            return (
                "The pool read was NOT conclusive, so a slot that did not "
                f"resolve may simply be one sac never looked at ({c.pool_source}). "
                "A collision FOUND under such a read is still real; a clean "
                "result is not. Re-run from where agents are STARTED (the pool "
                "resolves from the launching process env), or set "
                "SAC_SECRETS_ENVRC, before believing this row."
            )
        return ""

    def to_dict(self) -> dict:
        """JSON-friendly projection (for ``--json`` surfaces).

        Same shape as the sibling poller check's — ``state`` / ``detail`` /
        ``summary`` / ``hint`` / ``population`` / ``scope_note`` — so the two
        halves of the one-token-one-poller invariant read alike under
        ``sac doctor --json``.
        """
        c = self.census
        return {
            "state": self.state,
            "scope": "fleet-static",
            "scope_note": SCOPE_NOTE,
            "specs_examined": c.examined,
            "claims": [x.to_dict() for x in c.claims],
            "distinct_fingerprints": c.distinct_fingerprints,
            "collisions": [x.to_dict() for x in self.collisions],
            "unresolved": list(c.unresolved),
            "disabled": list(c.disabled),
            "no_channel": list(c.no_channel),
            "unreadable": list(c.unreadable),
            "population": self.population(),
            "pool_trusted": c.pool_trusted,
            "pool_source": c.pool_source,
            "scanned": self.scanned,
            "agents_dir": c.agents_dir,
            "detail": self.detail,
            "summary": self.summary(),
            "hint": self.hint(),
        }


def group_collisions(claims: Sequence[TokenClaim]) -> tuple[TokenCollision, ...]:
    """Fingerprints claimed by two or more specs, in fingerprint order.

    Pure over its input — the seam that lets the collision condition be
    constructed and asserted without a spec tree, next to the test that
    constructs it with two real ones.
    """
    by_fp: dict[str, list[TokenClaim]] = {}
    for claim in claims:
        if claim.token_fp:
            by_fp.setdefault(claim.token_fp, []).append(claim)
    return tuple(
        TokenCollision(token_fp=fp, claims=tuple(group))
        for fp, group in sorted(by_fp.items())
        if len(group) > 1
    )


def verdict_for(census: SpecCensus) -> TokenCollisionVerdict:
    """Decide OK / VIOLATION / UNKNOWN for an already-computed census.

    A VIOLATION outranks an UNKNOWN, for the sibling check's reason: a
    duplicate that HAS been computed is a fact, and one unreadable spec does
    not make it less true. The reverse ordering would let a single broken YAML
    file mute a live outage.
    """
    collisions = group_collisions(census.claims)
    if collisions:
        worst = "; ".join(f"{c.token_fp} <- {c.describe()}" for c in collisions)
        cross = (
            "At least one pair is pinned to DIFFERENT hosts, which no per-host "
            "process check can see. "
            if any(c.cross_host for c in collisions)
            else ""
        )
        return TokenCollisionVerdict(
            state=COLLISION_VIOLATION,
            census=census,
            collisions=collisions,
            detail=(
                f"{len(census.claims)} spec(s) claim a bot token but resolve to "
                f"only {census.distinct_fingerprints} distinct one(s). Telegram's "
                "getUpdates admits ONE consumer per token, so every duplicated "
                f"fingerprint below is a 409 conflict loop waiting to start: "
                f"{worst}. {cross}({SCOPE_NOTE})"
            ),
        )

    if census.unreadable:
        return TokenCollisionVerdict(
            state=COLLISION_UNKNOWN,
            census=census,
            detail=(
                f"{len(census.unreadable)} of {census.examined} spec(s) could not "
                "be loaded, so sac could not compute which bot they take: an "
                "unread spec could claim the same token as a read one. "
                "Unreadable: " + ", ".join(census.unreadable) + f". ({SCOPE_NOTE})"
            ),
        )

    if not census.pool_trusted:
        return TokenCollisionVerdict(
            state=COLLISION_UNKNOWN,
            census=census,
            detail=(
                "no two specs resolve to the same bot token in the pool sac read "
                f"({census.pool_source}) — but that read is NOT conclusive, so a "
                "slot that did not resolve may be one sac never looked at rather "
                "than one that is absent. A collision FOUND under such a read "
                f"would still be real; this clean result is not. ({SCOPE_NOTE})"
            ),
        )

    if not census.claims:
        return TokenCollisionVerdict(
            state=COLLISION_OK,
            census=census,
            detail=(
                f"{census.examined} spec(s) examined and NONE claims a bot token, "
                f"so nothing can conflict with nothing. ({SCOPE_NOTE})"
            ),
        )

    return TokenCollisionVerdict(
        state=COLLISION_OK,
        census=census,
        detail=(
            f"{len(census.claims)} spec(s) claim a bot token and no two claim the "
            "same one: count(distinct fingerprints) == count(claiming specs) == "
            f"{len(census.claims)}. {len(census.disabled)} spec(s) declare an "
            f"EMPTY token and hold none; {len(census.unresolved)} request the "
            "rail and resolve nothing (a DIFFERENT fault — mute and deaf, not a "
            f"collision; see `sac agents cct-audit`). ({SCOPE_NOTE})"
        ),
    )


def check_token_collisions(
    *,
    agents_dir: str | None = None,
    pool: PoolRead | None = None,
) -> TokenCollisionVerdict:
    """Do two registered specs resolve to the SAME Telegram bot token?

    Returns a three-valued verdict:

    * :data:`COLLISION_OK` — every spec that claims a bot claims a different
      one. Zero claimants is OK: nothing can conflict.
    * :data:`COLLISION_VIOLATION` — at least one fingerprint is claimed by two
      or more specs. Both agents and both hosts are named.
    * :data:`COLLISION_UNKNOWN` — the spec tree could not be enumerated, a spec
      would not load, or the pool read was inconclusive. Never folded into OK.

    Read-only: it loads specs and reads the pool ONCE, and touches nothing.
    ``agents_dir`` audits a different spec tree (e.g. a peer's synced copy);
    the POOL is still read from HERE, which is part of the measurement — see
    ``sac agents cct-audit`` on vantage point.
    """
    read = pool if pool is not None else read_pool()
    # stx-allow: fallback (reason: a spec root that cannot be enumerated is the
    # UNKNOWN verdict this function exists to be able to give — reporting zero
    # collisions because nobody looked is the exact collapse this check
    # refuses, so the failure must become a state, not an exception.)
    try:
        census = census_specs(agents_dir=agents_dir, pool=read)
    except Exception as exc:  # stx-allow: fallback (reason: see inline comment)
        return TokenCollisionVerdict(
            state=COLLISION_UNKNOWN,
            census=SpecCensus(
                agents_dir=agents_dir or "",
                pool_trusted=read.trusted,
                pool_source=_pool_source_label(),
            ),
            scanned=False,
            detail=(
                f"the spec tree could not be enumerated ({exc}), so no claim was "
                "computed at all. Nothing was learned; this is not an all-clear."
            ),
        )
    return verdict_for(census)


__all__ = [
    "COLLISION_OK",
    "COLLISION_UNKNOWN",
    "COLLISION_VIOLATION",
    "SCOPE_NOTE",
    "TokenCollision",
    "TokenCollisionVerdict",
    "check_token_collisions",
    "group_collisions",
    "verdict_for",
]
