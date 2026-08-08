"""What must be true about the TARGET before a relocation touches anything.

Every check here was learned by doing this move by hand on 2026-08-07, and each
one exists because rewriting `host:` alone produced an agent that STARTED,
reported HEALTHY, and did nothing. That is the worst failure shape available: it
looks exactly like success, so nobody goes looking.

    binds        the spec bound /mnt/c — a Windows drive absent on the nas
    card store   SCITEX_CARDS_DB is 5432 here and 5442 there
    credentials  the nas had a stale file (expired 2026-05-23, empty
                 refreshToken) that sac loaded IN PREFERENCE to the good one;
                 every turn 401'd while `sac agents health` still said healthy
    runtime      `tui` is rejected by the nas's older sac
    schema       a top-level `provider:` key is rejected by that same validator
  + reachability, image presence, free ports, and whether the hub is reachable
    FROM the target (the nas's services bind 127.0.0.1, so "I can reach it" from
    here proves nothing about there)

CREDENTIALS IS THE ONE TO READ TWICE. Checking PRESENCE passes on an expired
file. The check has to be about VALIDITY, and the failure it prevents is silent:
a healthy-looking agent whose every turn 401s.

WHY THERE IS NO I/O IN THIS MODULE. It evaluates FACTS someone else gathered —
`TargetFacts` in, `Check`s out. sac does not learn how to probe a host here, and
a caller can substitute observations from ssh, from a listen daemon, or from a
test without this file changing. It also means every check is unit-testable
against the exact broken state we hit in production, which is the only way to
know a check would have caught it.

UNKNOWN NEVER COUNTS AS PASS. Each check is three-valued, and
:func:`preflight` refuses on unknowns just as it refuses on failures — reported
separately so the reader can tell "this is wrong" from "I could not tell". An
unknown folded into pass is precisely how the 08-07 move reported healthy: the
credential check had no answer, and something treated that as fine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

__all__ = [
    "CHECK_BINDS",
    "CHECK_CARD_STORE",
    "CHECK_CREDENTIALS",
    "CHECK_HUB_FROM_TARGET",
    "CHECK_IMAGE",
    "CHECK_PORTS",
    "CHECK_REACHABLE",
    "CHECK_RUNTIME",
    "CHECK_SCHEMA",
    "Check",
    "PreflightReport",
    "TargetFacts",
    "preflight",
]

CHECK_REACHABLE: Final = "target_reachable"
CHECK_IMAGE: Final = "image_present"
CHECK_BINDS: Final = "binds_exist_on_target"
CHECK_CARD_STORE: Final = "card_store_reachable"
CHECK_CREDENTIALS: Final = "credentials_valid"
CHECK_RUNTIME: Final = "runtime_supported"
CHECK_SCHEMA: Final = "spec_schema_accepted"
CHECK_PORTS: Final = "ports_free"
CHECK_HUB_FROM_TARGET: Final = "hub_reachable_from_target"


@dataclass(frozen=True)
class Check:
    """One preflight answer.

    ``ok`` is three-valued: ``True`` pass, ``False`` fail, ``None`` COULD NOT
    DETERMINE. ``hint`` is required whenever the answer is not a pass — an error
    that only says what broke is half-written; it must also say what to do.
    """

    name: str
    ok: bool | None
    detail: str
    hint: str = ""

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("Check.name must be non-empty")
        if self.ok not in (True, False, None):
            raise ValueError(f"Check.ok must be True/False/None, got {self.ok!r}")
        if not self.detail:
            raise ValueError(f"Check {self.name!r}: detail must be non-empty")
        if self.ok is not True and not self.hint:
            raise ValueError(
                f"Check {self.name!r}: a non-passing check must carry a hint saying what to do about it"
            )


@dataclass(frozen=True)
class TargetFacts:
    """What was OBSERVED about the target host. Gathering happens elsewhere.

    Every field is ``| None`` and defaults to ``None`` meaning NOT OBSERVED —
    distinct from an observed negative. A prober that could not answer must say
    so rather than reporting a falsy default, because a default that reads as
    "absent" turns a failed probe into a confident wrong answer.
    """

    reachable: bool | None = None
    image_present: bool | None = None
    #: Bind SOURCE paths that do not exist on the target (e.g. /mnt/c on nas).
    missing_bind_sources: tuple[str, ...] | None = None
    card_store_url: str | None = None
    card_store_reachable: bool | None = None
    #: Seconds until the target-local credential expires. Negative = expired.
    credential_expires_in_s: float | None = None
    credential_refresh_token_present: bool | None = None
    supported_runtimes: tuple[str, ...] | None = None
    #: Top-level spec keys the target's validator rejects (e.g. "provider").
    rejected_spec_keys: tuple[str, ...] | None = None
    ports_in_use: tuple[int, ...] | None = None
    hub_reachable_from_target: bool | None = None


@dataclass(frozen=True)
class PreflightReport:
    """The whole answer, in one shape.

    ``ok`` is three-valued and derived, never asserted independently of the
    checks: True only when every check passed, None when nothing failed but
    something is unknown, False when anything failed. Unknowns are reported
    SEPARATELY from failures so a reader can tell "this is wrong" from "I could
    not tell" — and neither reads as go.
    """

    agent: str
    to_host: str
    checks: tuple[Check, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.agent:
            raise ValueError("PreflightReport.agent must be non-empty")
        if not self.to_host:
            raise ValueError("PreflightReport.to_host must be non-empty")

    @property
    def failed(self) -> tuple[Check, ...]:
        return tuple(c for c in self.checks if c.ok is False)

    @property
    def unknown(self) -> tuple[Check, ...]:
        return tuple(c for c in self.checks if c.ok is None)

    @property
    def ok(self) -> bool | None:
        if self.failed:
            return False
        if self.unknown:
            return None
        return True

    def blocking_reasons(self) -> tuple[str, ...]:
        """Every reason this relocation must not proceed, each with its hint.

        Failures first, then unknowns. Empty only when the answer is a clean go.
        """
        return tuple(
            f"{c.name}: {c.detail} -> {c.hint}" for c in (self.failed + self.unknown)
        )


def _unobserved(name: str, what: str) -> Check:
    return Check(
        name=name,
        ok=None,
        detail=f"{what} was not observed on the target",
        hint=(
            "run the probe that supplies this fact before deciding; an unobserved "
            "check is not a passing one, and proceeding on it is how a relocation "
            "reports healthy while doing nothing"
        ),
    )


def _check_reachable(facts: TargetFacts, to_host: str) -> Check:
    if facts.reachable is None:
        return _unobserved(CHECK_REACHABLE, "reachability")
    if not facts.reachable:
        return Check(
            name=CHECK_REACHABLE,
            ok=False,
            detail=f"{to_host} did not answer",
            hint=f"check ssh/network to {to_host}; nothing else in this report is meaningful until it answers",
        )
    return Check(name=CHECK_REACHABLE, ok=True, detail=f"{to_host} answered")


def _check_image(facts: TargetFacts, to_host: str) -> Check:
    if facts.image_present is None:
        return _unobserved(CHECK_IMAGE, "the agent image")
    if not facts.image_present:
        return Check(
            name=CHECK_IMAGE,
            ok=False,
            detail=f"the agent's image is absent on {to_host}",
            hint=f"build or copy the SIF to {to_host} before relocating; a missing image fails at boot, after the lease has moved",
        )
    return Check(name=CHECK_IMAGE, ok=True, detail="image present")


def _check_binds(facts: TargetFacts, to_host: str) -> Check:
    if facts.missing_bind_sources is None:
        return _unobserved(CHECK_BINDS, "bind sources")
    if facts.missing_bind_sources:
        missing = ", ".join(facts.missing_bind_sources)
        return Check(
            name=CHECK_BINDS,
            ok=False,
            detail=f"bind sources absent on {to_host}: {missing}",
            hint=(
                "remove or re-point these binds in the spec for the target host "
                "(2026-08-07: /mnt/c is a Windows drive that does not exist on the nas)"
            ),
        )
    return Check(
        name=CHECK_BINDS, ok=True, detail="every bind source exists on the target"
    )


def _check_card_store(facts: TargetFacts, to_host: str) -> Check:
    if facts.card_store_reachable is None:
        return _unobserved(CHECK_CARD_STORE, "the card store")
    if not facts.card_store_reachable:
        url = facts.card_store_url or "(no url recorded)"
        return Check(
            name=CHECK_CARD_STORE,
            ok=False,
            detail=f"card store {url} not reachable from {to_host}",
            hint=(
                "set SCITEX_CARDS_DB to the target's own store before relocating "
                "(2026-08-07: 5432 here, 5442 there) — an agent that cannot reach its "
                "board runs and records nothing"
            ),
        )
    return Check(
        name=CHECK_CARD_STORE, ok=True, detail=f"card store reachable from {to_host}"
    )


def _check_credentials(facts: TargetFacts) -> Check:
    """VALIDITY, not presence. Presence passes on an expired file."""
    if (
        facts.credential_expires_in_s is None
        or facts.credential_refresh_token_present is None
    ):
        return _unobserved(CHECK_CREDENTIALS, "credential validity")
    if facts.credential_expires_in_s <= 0:
        return Check(
            name=CHECK_CREDENTIALS,
            ok=False,
            detail=f"target credential expired {abs(facts.credential_expires_in_s):.0f}s ago",
            hint=(
                "refresh or replace the target-local credential; sac loads it IN PREFERENCE "
                "to a good one, so every turn 401s while `sac agents health` still says healthy"
            ),
        )
    if not facts.credential_refresh_token_present:
        return Check(
            name=CHECK_CREDENTIALS,
            ok=False,
            detail="target credential has an empty refreshToken",
            hint=(
                "replace it — it is valid now and unrenewable, so the agent dies at the "
                "first refresh with no warning beforehand"
            ),
        )
    return Check(
        name=CHECK_CREDENTIALS,
        ok=True,
        detail=f"credential valid for {facts.credential_expires_in_s:.0f}s with a refresh token",
    )


def _check_runtime(facts: TargetFacts, runtime: str) -> Check:
    if facts.supported_runtimes is None:
        return _unobserved(CHECK_RUNTIME, "supported runtimes")
    if runtime not in facts.supported_runtimes:
        supported = ", ".join(facts.supported_runtimes) or "(none reported)"
        return Check(
            name=CHECK_RUNTIME,
            ok=False,
            detail=f"target does not support runtime {runtime!r}; it supports: {supported}",
            hint=(
                "either upgrade sac on the target or set the spec's runtime to one it accepts "
                "(2026-08-07: the nas's sac 0.21.9 rejected 'tui')"
            ),
        )
    return Check(name=CHECK_RUNTIME, ok=True, detail=f"runtime {runtime!r} supported")


def _check_schema(facts: TargetFacts) -> Check:
    if facts.rejected_spec_keys is None:
        return _unobserved(CHECK_SCHEMA, "spec-schema acceptance")
    if facts.rejected_spec_keys:
        keys = ", ".join(facts.rejected_spec_keys)
        return Check(
            name=CHECK_SCHEMA,
            ok=False,
            detail=f"target's validator rejects spec key(s): {keys}",
            hint=(
                "remove those keys for the target, or upgrade its sac "
                "(2026-08-07: a top-level 'provider:' key was rejected by the older validator)"
            ),
        )
    return Check(
        name=CHECK_SCHEMA, ok=True, detail="target's validator accepts the spec"
    )


def _check_ports(facts: TargetFacts, required_ports: tuple[int, ...]) -> Check:
    if facts.ports_in_use is None:
        return _unobserved(CHECK_PORTS, "port availability")
    clashes = tuple(p for p in required_ports if p in facts.ports_in_use)
    if clashes:
        return Check(
            name=CHECK_PORTS,
            ok=False,
            detail=f"port(s) already in use on the target: {', '.join(str(p) for p in clashes)}",
            hint="free them or reassign the agent's ports in the spec before relocating",
        )
    return Check(
        name=CHECK_PORTS, ok=True, detail="required ports are free on the target"
    )


def _check_hub_from_target(facts: TargetFacts, to_host: str) -> Check:
    if facts.hub_reachable_from_target is None:
        return _unobserved(CHECK_HUB_FROM_TARGET, "hub reachability FROM the target")
    if not facts.hub_reachable_from_target:
        return Check(
            name=CHECK_HUB_FROM_TARGET,
            ok=False,
            detail=f"the hub is not reachable from {to_host}",
            hint=(
                "check what the hub's services bind to — reaching them from HERE proves "
                "nothing about THERE (the nas binds 127.0.0.1, so nothing cross-host reaches it)"
            ),
        )
    return Check(
        name=CHECK_HUB_FROM_TARGET, ok=True, detail=f"hub reachable from {to_host}"
    )


def preflight(
    *,
    agent: str,
    to_host: str,
    facts: TargetFacts,
    runtime: str,
    required_ports: tuple[int, ...] = (),
) -> PreflightReport:
    """Evaluate every check against observed facts. Touches nothing.

    Returns the full report rather than the first failure: the operator asked
    for a dry run, and a dry run that stops at the first problem makes him run
    it N times to find N problems.
    """
    checks = (
        _check_reachable(facts, to_host),
        _check_image(facts, to_host),
        _check_binds(facts, to_host),
        _check_card_store(facts, to_host),
        _check_credentials(facts),
        _check_runtime(facts, runtime),
        _check_schema(facts),
        _check_ports(facts, required_ports),
        _check_hub_from_target(facts, to_host),
    )
    return PreflightReport(agent=agent, to_host=to_host, checks=checks)
