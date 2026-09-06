"""The floor's EVIDENCE, rolled up per host for the report.

Split out of :mod:`._engines_floor` on the module line budget, and it earns
its own file anyway: that module answers "does this spec get written?", while
this one answers "on what basis did the 100 that DID get written get written?".

WHY THE APPROVING SIDE NEEDS EVIDENCE TOO. Every refusal already prints its
measurement and its date. The approvals printed nothing, so a 100-spec sweep
recorded which hosts it had REFUSED and said not one word about which it had
judged CAPABLE, on what, or how old the measurement was.

That asymmetry is exactly where this design fails OPEN.
:data:`._engines_floor.HOST_SUPPORT` is a static table with no expiry, and the
floor deliberately does not probe — so the one thing that can go wrong without
anybody noticing is a row going STALE: a host rebuilt, rolled back, or
reinstalled onto an older sac after its ``measured_on`` date. In that case the
sweep writes those specs and the report is byte-for-byte indistinguishable
from a correct run. No roster date, no per-host verdict, nothing to prompt a
re-measure. This module puts the basis for the writes in the same artifact as
the writes.
"""

from __future__ import annotations

from dataclasses import dataclass

from ._engines_floor import SUPPORT_NO, EngineFloor, canonical_host, host_support

__all__ = ["FloorAudit", "HostAudit", "floor_audit"]


@dataclass(frozen=True)
class HostAudit:
    """One host the floor was consulted about, and the row it answered from.

    ``measured_on`` and ``evidence`` are copied from the roster verbatim
    rather than summarised: a reader deciding whether to trust an approval
    needs the same material a refusal already gives them.
    """

    host: str
    canonical: str
    support: str
    #: "" exactly when nobody measured this host — an unmeasured host has no
    #: measurement to date.
    measured_on: str
    evidence: str
    overridden: bool
    #: How many of THIS run's specs place themselves on this host. ``0`` for
    #: a host named by ``--host-supports-engines`` that no selected spec
    #: mentions — a claim that did nothing still has to be visible.
    specs: int

    @property
    def contradicts_a_measurement(self) -> bool:
        """Is this override arguing with a MEASURED negative?

        Categorically different from lifting an UNKNOWN: one says "nobody
        looked, I did", the other says "the roster looked and I disagree".
        Only the second can strand an agent on a validator by the module's
        own evidence, so only the second earns the loud line.
        """
        return self.overridden and self.support == SUPPORT_NO

    def as_dict(self) -> dict:
        return {
            "host": self.host,
            "canonical": self.canonical,
            "support": self.support,
            "measured_on": self.measured_on,
            "evidence": self.evidence,
            "overridden": self.overridden,
            "contradicts_a_measurement": self.contradicts_a_measurement,
            "specs": self.specs,
        }


@dataclass(frozen=True)
class FloorAudit:
    """The BASIS for everything this run would write, per host."""

    hosts: "tuple[HostAudit, ...]" = ()
    specs_with_no_declared_host: int = 0
    specs_with_an_unreadable_host: int = 0
    active: bool = True

    @property
    def counts(self) -> "dict[str, int]":
        """Specs per verdict — the count the approvals never carried.

        A spec that names two hosts is counted under each of them, so these
        need not sum to the number of specs. The question this answers is
        "how much of this run rests on that row", not "how many specs are
        there" — ``specs`` in the payload is the second question.
        """
        tally: dict[str, int] = {}
        for row in self.hosts:
            tally[row.support] = tally.get(row.support, 0) + row.specs
        return dict(sorted(tally.items()))

    @property
    def measured_on(self) -> "tuple[str, ...]":
        """Every distinct measurement date consulted, oldest first.

        A run's whole roster is only as current as its oldest row, and that
        date is what a reader needs in order to ask "is this still true?".
        """
        return tuple(sorted({r.measured_on for r in self.hosts if r.measured_on}))

    def as_dict(self) -> dict:
        return {
            "active": self.active,
            "measured_on": list(self.measured_on),
            "hosts": [r.as_dict() for r in self.hosts],
            "counts": self.counts,
            "specs_with_no_declared_host": self.specs_with_no_declared_host,
            "specs_with_an_unreadable_host": self.specs_with_an_unreadable_host,
        }


def floor_audit(floor: EngineFloor, spec_hosts) -> FloorAudit:
    """Roll up every host consulted for this run, with its measured row.

    ``spec_hosts`` is one entry per selected spec: the set of hosts that spec
    places itself on, or ``None`` when they could not be read. Both of those
    are counted rather than dropped, for the same reason the plan keeps
    ``unreadable`` as its own bucket.
    """
    per_host: "dict[str, int]" = {}
    no_host = 0
    unreadable = 0
    for hosts in spec_hosts:
        if hosts is None:
            unreadable += 1
            continue
        if not hosts:
            no_host += 1
            continue
        for host in hosts:
            per_host[host] = per_host.get(host, 0) + 1

    rows = [_audit_row(floor, host, per_host[host]) for host in per_host]
    named = {r.canonical for r in rows}
    # An override naming a host no selected spec mentions matched nothing. It
    # is still a claim someone made, so it is reported rather than dropped —
    # a lift typed for a host that is not in this batch reads as effective.
    rows += [_audit_row(floor, h, 0) for h in floor.allowed if h not in named]
    return FloorAudit(
        hosts=tuple(sorted(rows, key=lambda r: r.host)),
        specs_with_no_declared_host=no_host,
        specs_with_an_unreadable_host=unreadable,
        active=floor.active,
    )


def _audit_row(floor: EngineFloor, host: str, specs: int) -> HostAudit:
    state, record = host_support(host)
    return HostAudit(
        host=host,
        canonical=canonical_host(host),
        support=state,
        measured_on=record.measured_on if record else "",
        evidence=record.evidence if record else "",
        overridden=canonical_host(host) in floor.allowed,
        specs=specs,
    )
