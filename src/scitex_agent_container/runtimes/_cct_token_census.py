"""WHO claims which bot — the observation half of the static collision check.

This is to :mod:`._cct_token_collision` what :mod:`._cct_poller_scan` is to
:mod:`._cct_poller_singleton`: it OBSERVES and classifies, and decides nothing.
It walks the spec tree, asks :func:`._cct_token_resolution.resolve_cct_token`
what each spec would take, and sorts the answers into the four populations that
matter. Whether the result is a fault is the other module's job.

FOUR POPULATIONS, AND ONLY ONE OF THEM CAN COLLIDE
--------------------------------------------------
* :attr:`SpecCensus.claims` — specs that WOULD hold a real bot token. Only
  these can collide, because only a held token can be held twice.
* :attr:`SpecCensus.disabled` — an explicitly EMPTY ``CCT_BOT_TOKEN`` in the
  spec. **Designed**: seven of the eight handymen carry it so only
  ``handyman-06`` polls the shared handyman bot. This is the invariant being
  upheld by hand, not a defect.
* :attr:`SpecCensus.no_channel` — the spec never asks for the rail.
* :attr:`SpecCensus.unresolved` — it asks and nothing resolves. A real fault,
  a DIFFERENT one (mute and deaf, not a collision), already owned by
  ``sac agents cct-audit`` and :mod:`._cct_rail_alarm`.

THE POPULATION IS THE SPEC TREE, NOT THE REGISTRY
-------------------------------------------------
``Registry.list_all()`` holds only agents that are RUNNING. A collision
between two stopped specs is still a collision — it returns the moment both
start — so the census reads ``<agents_root>/*/spec.yaml``, the same
enumeration ``sac agents cct-audit`` uses.

NO TOKEN VALUE IS EVER HELD HERE. :class:`TokenClaim` carries the
``sha256:<12hex>`` fingerprint, the slot NAME and the spec PATH, and nothing
else pool-derived.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from ._cct_token_resolution import (
    TOKEN_DISABLED,
    TOKEN_NO_CHANNEL,
    TOKEN_UNRESOLVED,
    CctTokenResolution,
    resolve_cct_token,
)
from ._secret_pool import PoolRead, _pool_source_label, read_pool


@dataclass(frozen=True)
class TokenClaim:
    """One spec's claim on one bot token. A fingerprint, never a value."""

    agent: str
    token_fp: str
    #: ``spec.host`` — where this agent is pinned, "" when unpinned. The
    #: remedy for a collision is a config decision about which agent yields,
    #: and that decision is unmakeable without knowing where each one runs.
    host: str = ""
    #: The pool slot that resolved, when the pool is what resolved it.
    slot: str = ""
    #: Which precedence step produced the token (:mod:`._cct_token_resolution`).
    source: str = ""
    #: The spec file this claim was read from.
    spec: str = ""

    def to_dict(self) -> dict:
        """JSON-friendly projection (for ``--json`` surfaces)."""
        return {
            "agent": self.agent,
            "token_fp": self.token_fp,
            "host": self.host or "",
            "slot": self.slot,
            "source": self.source,
            "spec": self.spec,
        }


@dataclass(frozen=True)
class SpecCensus:
    """Every spec, sorted into the four populations. No verdict, by design."""

    claims: tuple[TokenClaim, ...] = ()
    unresolved: tuple[str, ...] = ()
    disabled: tuple[str, ...] = ()
    no_channel: tuple[str, ...] = ()
    #: Specs sac could not load. Each is a claim it could not COMPUTE, which
    #: is why they are named rather than dropped: an unread spec could claim
    #: the same token as a read one.
    unreadable: tuple[str, ...] = ()
    #: How many spec FILES were looked at — the denominator R4 demands, and
    #: not the same as ``len(claims)``.
    examined: int = 0
    agents_dir: str = ""
    #: Whether a MISS against the pool read was conclusive (:class:`PoolRead`).
    pool_trusted: bool = True
    pool_source: str = ""

    @property
    def distinct_fingerprints(self) -> int:
        """How many distinct bots the claiming specs resolve to."""
        return len({c.token_fp for c in self.claims})


def spec_host(config) -> str:
    """``spec.host`` as a display string — "" when the agent is unpinned.

    ``host`` may be a LIST (a priority/fallback chain), in which case every
    entry is a place this agent could run and all of them matter to the
    remedy, so all are shown rather than silently reduced to the first.
    """
    raw = getattr(getattr(config, "hosts_spec", None), "host", "") or ""
    if isinstance(raw, (list, tuple)):
        return "|".join(str(h).strip() for h in raw if str(h).strip())
    return str(raw).strip()


def spec_paths(agents_dir: str | None = None) -> list[Path]:
    """Every ``<agents_dir>/<name>/spec.yaml``, sorted by name.

    ``agents_dir`` defaults to this host's ``agents_root()``. It is an explicit
    parameter rather than an env lookup so the caller states WHICH spec tree it
    means — useful for auditing a peer's synced tree, and it keeps the tests
    from having to intercept ``$SCITEX_DIR`` to say the same thing.

    RAISES :class:`FileNotFoundError` when the root is not a directory. An
    empty list means "enumerated, nothing there"; a missing root means "never
    looked", and a caller that cannot tell them apart reports a clean fleet it
    never read.
    """
    if agents_dir:
        root = Path(agents_dir).expanduser()
    else:
        from .._state.state_paths import agents_root

        root = agents_root()
    if not root.is_dir():
        # RAISE, do not return []. An absent spec tree is "could not be
        # enumerated", not "enumerated and found nothing" -- and the two render
        # identically downstream: zero claimants is a legitimate OK, so a
        # missing root would report the fleet CLEAN on the strength of never
        # having looked at it. That is the exact collapse this check exists to
        # refuse, and it was caught by the adversarial pass (CASE E: a spec tree
        # that does not exist returned "ok", scanned=True).
        #
        # check_token_collisions already converts this to COLLISION_UNKNOWN with
        # "Nothing was learned; this is not an all-clear." Raising routes the
        # case into that existing path rather than adding a second one.
        raise FileNotFoundError(
            f"spec tree {root} is not a directory, so no spec could be "
            "enumerated"
        )
    return [p for p in sorted(root.glob("*/spec.yaml")) if p.is_file()]


def census_from_resolutions(
    resolutions: Sequence[tuple[CctTokenResolution, str, str]],
    *,
    unreadable: Sequence[str] = (),
    examined: int | None = None,
    agents_dir: str = "",
    pool_trusted: bool = True,
    pool_source: str = "",
) -> SpecCensus:
    """Sort already-computed resolutions into the four populations.

    ``resolutions`` is ``(resolution, host, spec_path)`` per spec — a tuple
    rather than a richer object because the host and the path come from the
    SPEC, not from the resolution, and folding them into
    :class:`CctTokenResolution` would put fields on the writer's own return
    value that the writer has no business carrying.

    Pure over its input: the seam that lets the collision condition be built
    and asserted without a spec tree on disk.
    """
    claims: list[TokenClaim] = []
    unresolved: list[str] = []
    disabled: list[str] = []
    no_channel: list[str] = []

    for resolution, host, spec in resolutions:
        if resolution.claims_a_token:
            claims.append(
                TokenClaim(
                    agent=resolution.agent,
                    token_fp=resolution.token_fp,
                    host=host,
                    slot=resolution.slot,
                    source=resolution.source,
                    spec=spec,
                )
            )
        elif resolution.outcome == TOKEN_UNRESOLVED:
            unresolved.append(resolution.agent)
        elif resolution.outcome == TOKEN_DISABLED:
            disabled.append(resolution.agent)
        elif resolution.outcome == TOKEN_NO_CHANNEL:
            no_channel.append(resolution.agent)

    total = examined if examined is not None else len(resolutions) + len(unreadable)
    return SpecCensus(
        claims=tuple(claims),
        unresolved=tuple(unresolved),
        disabled=tuple(disabled),
        no_channel=tuple(no_channel),
        unreadable=tuple(unreadable),
        examined=total,
        agents_dir=agents_dir,
        pool_trusted=pool_trusted,
        pool_source=pool_source,
    )


def census_specs(
    *,
    agents_dir: str | None = None,
    pool: PoolRead | None = None,
) -> SpecCensus:
    """Walk the spec tree and classify every spec. Read-only.

    The pool is read ONCE and injected into every resolution: forking a bash
    per agent to source ~28 secret files would make a 122-spec sweep cost
    minutes and — worse — could produce rows that disagree with each other if
    the environment shifted mid-run.

    RAISES ``OSError`` when the spec root cannot be enumerated. That is
    deliberate and it is the caller's UNKNOWN: reporting zero collisions
    because nobody looked is the collapse the verdict module refuses.
    """
    from ..config import load_config
    from ._cct_rail_verdict import materialised_home

    read = pool if pool is not None else read_pool()
    paths = spec_paths(agents_dir)

    resolutions: list[tuple[CctTokenResolution, str, str]] = []
    unreadable: list[str] = []
    for path in paths:
        name = path.parent.name
        # stx-allow: fallback (reason: one unloadable spec must not abort a fleet-wide census; it becomes a NAMED unreadable row that drives UNKNOWN upstream, never a dropped one, because a spec sac cannot read is a claim it cannot compute)
        try:
            config = load_config(str(path))
        except Exception:  # stx-allow: fallback (reason: see inline comment)
            unreadable.append(name)
            continue
        resolutions.append(
            (
                resolve_cct_token(config, dest=materialised_home(config), pool=read),
                spec_host(config),
                str(path),
            )
        )

    return census_from_resolutions(
        resolutions,
        unreadable=unreadable,
        examined=len(paths),
        agents_dir=agents_dir or _default_agents_dir(),
        pool_trusted=read.trusted,
        pool_source=_pool_source_label(),
    )


def _default_agents_dir() -> str:
    """This host's agents root, as a string — a label on the result only."""
    # stx-allow: fallback (reason: a display label; a path-resolution failure must not turn a computed census into an exception)
    try:
        from .._state.state_paths import agents_root

        return str(agents_root())
    except Exception:  # stx-allow: fallback (reason: see inline comment)
        return ""


__all__ = [
    "SpecCensus",
    "TokenClaim",
    "census_from_resolutions",
    "census_specs",
    "spec_host",
    "spec_paths",
]
