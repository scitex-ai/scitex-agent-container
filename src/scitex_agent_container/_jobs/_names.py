"""Job-name grammar: the LOCAL name an operator types vs the CANONICAL id.

Two names for one job, and keeping them straight is the whole module:

* the **canonical** ``JobSpec.name`` — globally unique across the whole
  SciTeX ecosystem, because ``scitex_dev.jobs.discover_jobs`` keys its
  de-duplication on it and the systemd renderer derives the UNIT FILENAME
  from it verbatim (``<name>.service`` / ``<name>.timer``);
* the **local** short name — what the operator types inside the owning
  package's own CLI, where "which package" is already answered by the
  command they are running (``sac dev timer install accounts-refresh``).

Every verb that takes a name accepts EITHER form and resolves to the
canonical one, so a copy-pasted canonical id from ``--json`` output or
from a unit filename keeps working.

WHY THE PREFIX IS A NAMED CONSTANT AND NOT AN INLINE LITERAL
============================================================
:data:`JOB_PREFIX` is the seam for the ecosystem-wide rename to
``scitex-<pkg>-<name>`` (operator decision, 2026-08-11). That rename
changes the DERIVED UNIT FILENAME of every job sac owns, which is a live
double-supervisor hazard on any host where the current units are
installed and running: the new unit is a DIFFERENT file, so installing it
without first stopping and removing the old one leaves two units running
the same command with independent state. ``sac.accounts-refresh`` is the
fleet's SOLE OAuth refresher against a SINGLE-USE refresh token, so two
of it revoke each other's access token fleet-wide.

The rename therefore does not ride along with a CLI-surface change; the
migration verb that enforces stop -> remove -> install and makes
install-before-uninstall impossible is what closes it on each host.
Flipping this one constant is the code half; the verb is the host half,
and — see below — the code half has to land FIRST for the verb to work
at all.

WHY TWO PREFIXES ARE LIVE AT ONCE, AND WHY THAT IS THE SAFE SHAPE
=================================================================
:data:`JOB_PREFIX` is now the canonical ``scitex-agent-container-``, but
:data:`LEGACY_JOB_PREFIX` (``sac.``) is still RECOGNISED, because one job
deliberately keeps its old name until an operator-supervised cutover:
``sac.accounts-refresh``, the fleet's sole OAuth refresher.

THE ORDER WAS INVERTED 2026-08-19, and this paragraph used to argue the
opposite, so read the correction rather than the memory of it. The old
rule was "rename the SPEC now and cut the UNIT over later is the one
shape that must not ship", on the grounds that ``sac dev timer status
accounts-refresh`` would then resolve to a name no unit carries and
report the fleet's sole OAuth refresher as absent while it runs.

That hazard is real and it is now ACCEPTED, because the alternative is
not available. ``sac dev timer install`` resolves only names a JobSpec
DECLARES, so a held spec can never be installed under its new name:

    $ sac dev timer install scitex-agent-container-accounts-refresh
    no job named 'scitex-agent-container-accounts-refresh' here

"Rename both together" was therefore never reachable — attempted in the
old order on 2026-08-18, stop and remove succeeded, install failed on
exactly that lookup, and the fleet had zero refreshers for ~2 minutes.
The spec leads and the unit follows, or the cutover never happens.

What the accepted window costs is bounded and worth naming: a MISREPORT
on one manual verb, which cannot escalate by itself, because nothing in
this package installs or enables a timer automatically — the backend
declines to run ``systemctl`` and the apply path only PRINTS the enable
line. Two racing refreshers still require a human to type the install
command against a host that already carries the old unit.

So the declared name now LEADS the deployed unit, by necessity. Both prefixes are
readable until :mod:`._migrate` records that cutover as done, and
``JOB_PREFIX`` alone is what new names are minted with.
"""

from __future__ import annotations

from typing import Iterable

#: Canonical-name prefix for every job sac owns.
#:
#: The ecosystem-wide form decided 2026-08-11: ``scitex-<pkg>-<name>``,
#: hyphens only — no ``.`` and no ``_``, because the systemd renderer
#: derives the unit filename from this verbatim and a dot in a unit name
#: reads as a systemd template/instance separator to every human who has
#: ever typed ``systemctl``.
JOB_PREFIX = "scitex-agent-container-"

#: The pre-2026-08-11 prefix. Still RECOGNISED (never minted) so the one
#: job awaiting a supervised cutover keeps a name that matches its live
#: unit — see the module docstring for why that direction is the safe one.
LEGACY_JOB_PREFIX = "sac."

#: Every prefix a name of ours may carry, longest first so ``local()``
#: strips the most specific match rather than a prefix of a prefix.
_PREFIXES: tuple[str, ...] = tuple(
    sorted((JOB_PREFIX, LEGACY_JOB_PREFIX), key=len, reverse=True)
)


def canonical(name: str) -> str:
    """Return the canonical ``JobSpec.name`` for a local-or-canonical name.

    Idempotent: an already-canonical name is returned unchanged, so a
    value copied out of ``--json`` output or off a unit filename resolves
    to itself instead of being prefixed twice. A LEGACY-prefixed name is
    likewise returned unchanged rather than re-prefixed — it names a real
    deployed unit, and rewriting it here would point every verb at a unit
    that does not exist.
    """
    if not name:
        raise ValueError("job name must be non-empty")
    return name if is_ours(name) else JOB_PREFIX + name


def local(name: str) -> str:
    """Return the short name an operator types, for a canonical name."""
    for prefix in _PREFIXES:
        if name.startswith(prefix):
            return name[len(prefix) :]
    return name


def is_ours(name: str) -> bool:
    """True when ``name`` is a job THIS package owns, under either prefix."""
    return any(name.startswith(p) for p in _PREFIXES)


def candidates(typed: str) -> tuple[str, ...]:
    """Every canonical form ``typed`` could mean, preferred first.

    A local name is ambiguous while two prefixes are live, so resolution
    tries the canonical form first and the legacy form second. An input
    that already carries a prefix is unambiguous and yields exactly one.
    """
    if not typed:
        raise ValueError("job name must be non-empty")
    if is_ours(typed):
        return (typed,)
    return tuple(dict.fromkeys((JOB_PREFIX + typed, LEGACY_JOB_PREFIX + typed)))


def resolve(typed: str, declared: Iterable[str]) -> str:
    """Resolve a typed name against the jobs actually declared.

    Returns the canonical name. Raises :class:`KeyError` naming every
    available local name when the typed one matches nothing — a verb that
    silently does nothing for a typo is how a scheduled job quietly stops
    being scheduled.
    """
    names = list(declared)
    for want in candidates(typed):
        if want in names:
            return want
    raise KeyError(
        f"no job named {typed!r} here; available: "
        + (", ".join(sorted(local(n) for n in names)) or "(none)")
    )


__all__ = [
    "JOB_PREFIX",
    "LEGACY_JOB_PREFIX",
    "candidates",
    "canonical",
    "is_ours",
    "local",
    "resolve",
]
