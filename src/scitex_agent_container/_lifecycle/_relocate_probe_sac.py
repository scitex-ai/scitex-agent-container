"""The five facts about sac ITSELF on the target: can it be reached, and would it start this agent.

Split out of :mod:`_relocate_probe_adapter`, which owns the other eight, for the
reason its own siblings were split: a reader asking "what did we learn about sac
over there" should find a file about sac rather than five methods in the middle
of a class about credentials and ports. They belong together because they answer
ONE question in stages — is sac installed, can the PATH we use see it, and does
its own start command have any objection to this agent — and because four of the
five were added or changed by the same 2026-08-12 finding.

WHY THREE PATH QUESTIONS AND NOT ONE. They are not redundancies, they are the
three different PATHs a person or a program can mean, and the fleet has hosts
where they disagree:

    sac_path      ``command -v sac`` under the BARE non-interactive ssh PATH.
                  What an operator typing ``ssh host sac …`` gets, and what
                  scitex-compute-04 answers "No such file or directory" to.
    sac_usable    the same lookup under the PATH THIS SCRIPT RAN WITH — raw plus
                  the peer's ``env_preamble``, which is what
                  :class:`._relocate_shell.Shell` prepends to every command a
                  relocation sends. THE ONE THE CHECK NEEDS.
    sac_found     found by looking harder: the login shell, then the venvs sac
                  is installed into across this fleet. Separates "not installed"
                  from "installed and unreachable", which need opposite fixes.

Reading only the first is what failed ywata-note-win, which declares a working
``env_preamble`` and was still told to declare one.

THE START-ACCEPTANCE FACT IS THE TARGET'S OWN VERDICT, QUOTED. sac's spec-source
drift guard refuses a boot from a repo that is BEHIND its upstream; that guard is
the thing that will refuse, so it is asked, and its answer is parsed rather than
recomputed. An older sac that cannot answer leaves the line absent, which is
UNKNOWN — never "no drift".

A mixin over :class:`._relocate_probe_adapter.TargetBatch`, so all five read the
SAME memoized round trip. It expects that class's ``_field`` / ``readout`` /
``_preamble``.
"""

from __future__ import annotations

from ._relocate_probe import FactUnavailable

__all__ = ["SacFacts"]


class SacFacts:
    """Mixin: the sac-on-the-target facts. Expects ``TargetBatch``'s attributes."""

    def sac_on_path(self) -> bool:
        """Whether ``command -v sac`` answers under the RAW ssh PATH.

        An empty value is the ANSWER (nothing on PATH), not a missing one — the
        script prints the line either way. Only a line that never arrived is
        unknown, which ``_field`` raises for.
        """
        return bool(self._field("sac_path", "sac-on-PATH").strip())

    def sac_resolved_path(self) -> str:
        """Where sac actually is, or ``""`` for looked-and-found-nothing.

        The empty string is load-bearing and must NOT be turned into a raise:
        it is what separates "sac is not installed on this host" from "sac is
        installed and the ssh PATH cannot see it", and those need opposite
        fixes. Only an absent line is undetermined.
        """
        return self._field("sac_found", "sac-location").strip()

    def sac_usable_path(self) -> str:
        """Where sac resolves under the PATH THE PROBE ITSELF RAN WITH.

        That PATH is the raw ssh one plus the peer's ``env_preamble`` — exactly
        what every relocation command runs under — so this answers the question
        the feature depends on, where :meth:`sac_on_path` answers a stricter one.
        Empty is the ANSWER, not a missing one; only an absent line is unknown.
        """
        return self._field("sac_usable", "sac-under-relocation-PATH").strip()

    def preamble_declared(self) -> bool:
        """Whether a peer ``env_preamble`` was declared AND sent with this probe.

        Observed by construction rather than measured on the target: the prober
        knows what it prepended. It qualifies the three PATH facts, and it is
        what stops the failure hint recommending a setting that is already set
        (:func:`._relocate_checks._sac_path_hint`).
        """
        return bool(self._preamble.strip())

    def spec_source_drift(self):
        """What the TARGET'S OWN sac says about the spec source it would boot from.

        Parsed from ``<state>|<behind>|<ahead>|<repo>|<upstream>``, printed by
        the target's own :func:`.._drift._local.check_spec_source_drift`. A
        missing line means its sac could not answer — an older one without the
        symbol, or a section that timed out — and that is UNKNOWN, not "no
        drift". The dirty count rides along on its own line and is optional:
        it is evidence for the hint, never part of the verdict, so a target
        without git still yields a usable drift answer.
        """
        from ._relocate_preflight_facts import SpecSourceDrift

        parts = self._field("startdrift", "start-acceptance").split("|")
        if not parts[0].strip():
            raise FactUnavailable(
                "the target's start-acceptance probe answered with no drift state; "
                "its sac ran and could not name a verdict"
            )

        def _count(index: int) -> int:
            raw = parts[index].strip() if len(parts) > index else ""
            return int(raw) if raw.isdigit() else 0

        dirty = (self.readout().fields.get("startdirty") or "").strip()
        return SpecSourceDrift(
            state=parts[0].strip(),
            behind=_count(1),
            ahead=_count(2),
            repo=parts[3].strip() if len(parts) > 3 else "",
            upstream=parts[4].strip() if len(parts) > 4 else "",
            dirty=int(dirty) if dirty.isdigit() else None,
        )
