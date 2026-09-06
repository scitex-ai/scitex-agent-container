"""The VERSION FLOOR — will the host that parses this spec accept ``engines:``?

THE HAZARD, MEASURED. A sac that predates engines support does not ignore an
unknown ``engines:`` key: it REJECTS the whole spec. Reproduced 2026-09-06 by
extracting the parent of the commit that added the feature and running THAT
validator against a real fleet spec::

    engines added by   0d61e077  2026-09-03
    its parent         46a5d40a
    parent has config/_engine_types.py?   NO
    CONTROL: parent has config/_types.py?  YES  (so the extract is a real tree)

    business/spec.yaml, engines block stripped   -> 0 errors
    the SAME spec + an engines block             -> 1 error:
        "Unknown spec field 'engines'. Use spec.extensions for custom data;
         known keys: [a2a, access, apptainer, ...]"

The control is the load-bearing half: the spec is otherwise clean under that
old validator, so the ONE error the engines block introduces is the whole
difference. Write that block into a spec whose target host runs a pre-engines
sac and the spec stops loading — the agent stops starting, at a validator on a
machine nobody is watching.

WHY THE REFUSAL HAPPENS HERE AND NOT THERE. The constitution's rule is that a
declaration which cannot be honoured must FAIL rather than evaporate. It would
fail — remotely, silently, later. So the failure is pulled forward to PLAN
time, where a human is reading the output.

HOW THE FLOOR LEARNS. From this recorded roster, not from an ssh probe at plan
time, and that is a deliberate trade:

  * a probe of seven hosts costs seven ssh round trips on every dry run, and
    the sweep's whole value is that a dry run is cheap enough to re-read;
  * a host that does not answer produces UNKNOWN, which this module must
    refuse anyway — so an unreachable host converts a fast refusal into a
    slow one and changes no verdict;
  * a probe answers "what is installed right now", which is not reviewable.
    A recorded fact carries its date, its method and its control, lands in a
    diff, and can be argued with.

FAIL CLOSED, ALWAYS. A host with no record here is UNKNOWN and is REFUSED. It
is never assumed capable — assuming capability is precisely the write that
strands an agent. Absence from the table is a refusal, so extending the fleet
cannot silently widen what the sweep will write.

THE MEASUREMENT BEHIND THE TABLE (2026-09-06, from scitex-compute-04). Each
host was asked, over ssh, whether ``config/_engine_types.py`` exists in every
root that holds ``scitex_agent_container`` and whether it contains
``apply_default_engine`` — FIX PRESENCE, not a version string, because a
version string is a claim about a package's metadata and the metadata sits
outside the bind mount the fleet actually imports from. Every probe carried a
positive control (how many candidate roots it saw, how many held the package)
so an empty answer could not be mistaken for a measurement.

    scitex-compute-01   2 roots hold the package, both engines=True
    scitex-compute-03   2 roots hold the package, both engines=True
    scitex-compute-04   2 roots hold the package, both engines=True
    scitex-laptop-01    3 roots hold the package; the editable ~/proj checkout
                        is True. The third (~/.local site-packages) holds a
                        scitex_agent_container DIRECTORY with neither
                        _engine_types.py nor _types.py — a partial remnant,
                        not a sac that could load a spec at all, so it is not
                        evidence against the checkout that does.
    spartan             3 roots hold the package, NONE has _engine_types.py
                        (control: config/_types.py IS present in that same
                         checkout, so the probe read a real tree)
    scitex-compute-02   NOT MEASURABLE — see below

``ywata-note-win`` IS ``scitex-laptop-01``. The 14 specs pinned on the retired
name are NOT refused, and the reason is a measurement rather than a courtesy:
asked over both ssh aliases, the machine answers ``hostname`` ->
``ywata-note-win`` in both cases. It is one machine with two names, and that
machine's sac parses engines. Refusing those 14 would be the floor firing on a
NAMING question it was not built to answer — the rename is its own card — and
a floor that refuses for the wrong reason is the reason people route around
the tool. The alias is recorded as a measured fact, not as an assumption.

``scitex-compute-02`` IS REFUSED, and this is the fail-closed rule doing its
job on the only host where it bites. The probe ran (it answered ``hostname``)
and found NO sac at all: nothing named ``sac`` on ``PATH``, no ``/opt/venv*``,
no ``~/proj/scitex-agent-container``, and a bounded ``find / -maxdepth 8``
matching no ``_engine_types.py`` under any ``scitex_agent_container`` path —
with ``/etc/hostname`` returned by the same find as the positive control that
it ran at all. "No install located" is NOT "an install that predates engines";
it is not knowing which sac would parse the spec. So the 3 specs pinned there
are refused by name, for a human to resolve, rather than written blind.

LIFTING IT. ``--host-supports-engines HOST`` (repeatable) tells the sweep that
a named host is capable. It is per-HOST on purpose: a blunt global bypass gets
typed by habit, while naming the machine makes the claim explicit, reviewable
in the shell history and recorded in the JSON payload. Naming every host that
appears lifts the floor completely, so it is not a wall — it is a statement
someone has to make.
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = [
    "ENGINES_ADDED_COMMIT",
    "ENGINES_ADDED_DATE",
    "HOST_ALIASES",
    "HOST_SUPPORT",
    "REFUSED_HOST_NOT_MEASURED",
    "REFUSED_HOST_PREDATES_ENGINES",
    "REFUSED_HOST_UNDECLARED",
    "REFUSED_HOST_UNREADABLE",
    "SUPPORT_NO",
    "SUPPORT_UNKNOWN",
    "SUPPORT_YES",
    "EngineFloor",
    "FloorVerdict",
    "HostRecord",
    "canonical_host",
    "host_support",
]

#: The commit that taught sac ``spec.engines``, and the day it landed. Any
#: checkout older than this rejects the key outright.
ENGINES_ADDED_COMMIT = "0d61e077"
ENGINES_ADDED_DATE = "2026-09-03"

SUPPORT_YES = "supports-engines"
SUPPORT_NO = "predates-engines"
#: Not in the table. Refused — never read as "probably fine".
SUPPORT_UNKNOWN = "not-measured"


@dataclass(frozen=True)
class HostRecord:
    """One MEASURED fact about one host, with the date and the method.

    ``evidence`` is not decoration: it is what lets a reader decide whether
    to trust the row without re-running the probe, and what makes a stale row
    arguable rather than authoritative.
    """

    support: str
    measured_on: str
    evidence: str


#: Measured by FIX PRESENCE — see the module docstring for the probe and its
#: controls. A host that is missing from this table is refused.
HOST_SUPPORT: "dict[str, HostRecord]" = {
    "scitex-compute-01": HostRecord(
        SUPPORT_YES,
        "2026-09-06",
        "2 roots hold scitex_agent_container; config/_engine_types.py present "
        "with apply_default_engine in both",
    ),
    "scitex-compute-03": HostRecord(
        SUPPORT_YES,
        "2026-09-06",
        "2 roots hold scitex_agent_container; config/_engine_types.py present "
        "with apply_default_engine in both",
    ),
    "scitex-compute-04": HostRecord(
        SUPPORT_YES,
        "2026-09-06",
        "2 roots hold scitex_agent_container; config/_engine_types.py present "
        "with apply_default_engine in both",
    ),
    "scitex-laptop-01": HostRecord(
        SUPPORT_YES,
        "2026-09-06",
        "3 roots hold scitex_agent_container; the editable ~/proj checkout has "
        "config/_engine_types.py with apply_default_engine. The third root is "
        "a partial remnant with neither _engine_types.py nor _types.py, so it "
        "is not a sac that could load a spec at all",
    ),
    "spartan": HostRecord(
        SUPPORT_NO,
        "2026-09-06",
        "3 roots hold scitex_agent_container and NONE has "
        "config/_engine_types.py; control: config/_types.py IS present in the "
        "editable checkout, so the probe read a real tree",
    ),
}

#: One machine, two names. ``hostname`` answers ``ywata-note-win`` over BOTH
#: ssh aliases (measured 2026-09-06), so the 14 specs pinned on the retired
#: name run on the host recorded as ``scitex-laptop-01``. Renaming them is a
#: separate card; this table only stops the floor from refusing them for a
#: reason that is not about engines.
HOST_ALIASES: "dict[str, str]" = {
    "ywata-note-win": "scitex-laptop-01",
}

# Refusal reasons. CONSTANT strings, nothing interpolated, so a 119-spec sweep
# groups by reason and prints one line per KIND — the same contract
# ``_engines_line``'s REFUSED_* constants keep. The variable half (which host,
# what was measured) travels in the detail.
REFUSED_HOST_PREDATES_ENGINES = (
    "the target host runs a sac that PREDATES spec.engines and would reject "
    "the block as an unknown spec field — the agent would stop starting"
)
REFUSED_HOST_NOT_MEASURED = (
    "no measured record of whether the target host can parse spec.engines; an "
    "unmeasured host is REFUSED, never assumed capable"
)
REFUSED_HOST_UNDECLARED = (
    "the spec names no host, so which sac would parse the block is unknown"
)
REFUSED_HOST_UNREADABLE = (
    "the spec's host could not be read, so which sac would parse the block is "
    "unknown"
)

_LIFT = (
    "Pass --host-supports-engines {host} if you know better; the claim is "
    "recorded in the JSON payload."
)


def canonical_host(host: str) -> str:
    """Resolve a known alias to the name the roster records.

    An unknown name is returned unchanged — it then misses the table and is
    refused, which is the fail-closed answer for a host nobody measured.
    """
    name = str(host).strip()
    return HOST_ALIASES.get(name, name)


def host_support(host: str) -> "tuple[str, HostRecord | None]":
    """``(state, record)`` for one host name, alias-resolved.

    ``record`` is None exactly when the state is :data:`SUPPORT_UNKNOWN`,
    because an unmeasured host has no measurement to carry.
    """
    record = HOST_SUPPORT.get(canonical_host(host))
    if record is None:
        return SUPPORT_UNKNOWN, None
    return record.support, record


@dataclass(frozen=True)
class FloorVerdict:
    """Does the floor stop this spec being written, and exactly why?

    Deliberately not castable to a bool: ``blocks`` is the question, and
    ``reason`` / ``detail`` are what a refusal has to carry to be actionable
    — which spec, which host, and what was measured about it.
    """

    blocks: bool
    reason: str = ""
    detail: str = ""

    def __post_init__(self) -> None:
        if self.blocks and not self.reason:
            raise ValueError(
                "a blocking floor verdict must NAME its reason; a refusal "
                "without one is indistinguishable from a silent skip"
            )


_PASS = FloorVerdict(False)


@dataclass(frozen=True)
class EngineFloor:
    """The floor, plus the per-host claims an operator made to lift it.

    ``allowed`` holds the hosts named with ``--host-supports-engines``. They
    are alias-resolved on the way in, so lifting ``scitex-laptop-01`` also
    lifts the 14 specs that spell the same machine ``ywata-note-win`` —
    otherwise the override would silently miss the specs it was typed for.
    """

    allowed: "frozenset[str]" = field(default_factory=frozenset)

    @classmethod
    def with_overrides(cls, hosts: "tuple[str, ...]" = ()) -> "EngineFloor":
        return cls(allowed=frozenset(canonical_host(h) for h in hosts if h))

    def _describe(self, host: str) -> str:
        """One host's recorded verdict, spelled out.

        Only ever called for a host :meth:`verdict_for` has already found
        NOT overridden, so there is no override branch here — a guard for a
        case the caller cannot produce is noise a reader has to rule out.
        """
        state, record = host_support(host)
        canonical = canonical_host(host)
        named = host if canonical == host else f"{host} (= {canonical})"
        if record is None:
            return f"{named}: {SUPPORT_UNKNOWN} — absent from the measured roster"
        return f"{named}: {state}, measured {record.measured_on} — {record.evidence}"

    def verdict_for(self, hosts: "set[str] | None") -> FloorVerdict:
        """Judge the hosts one spec places itself on.

        ``None`` means the hosts could NOT BE READ and ``set()`` means the
        spec names none. Both are refusals and they are kept apart, because
        "I could not read it" and "it says nothing" want different fixes.

        A DEFINITE negative outranks an unknown: when a spec names both a
        host measured as pre-engines and one nobody measured, the pre-engines
        host is the one to report — it is the fact, and the unknown is the
        absence of one.
        """
        if hosts is None:
            return FloorVerdict(True, REFUSED_HOST_UNREADABLE)
        if not hosts:
            return FloorVerdict(
                True,
                REFUSED_HOST_UNDECLARED,
                "a spec with no host could start on any machine, including one "
                "that would reject the block",
            )
        predates: list[str] = []
        unmeasured: list[str] = []
        for host in sorted(hosts):
            if canonical_host(host) in self.allowed:
                continue
            state, _ = host_support(host)
            if state == SUPPORT_NO:
                predates.append(host)
            elif state == SUPPORT_UNKNOWN:
                unmeasured.append(host)
        if predates:
            return FloorVerdict(
                True,
                REFUSED_HOST_PREDATES_ENGINES,
                "; ".join(self._describe(h) for h in predates)
                + ". "
                + _LIFT.format(host=predates[0]),
            )
        if unmeasured:
            return FloorVerdict(
                True,
                REFUSED_HOST_NOT_MEASURED,
                "; ".join(self._describe(h) for h in unmeasured)
                + ". "
                + _LIFT.format(host=unmeasured[0]),
            )
        return _PASS
