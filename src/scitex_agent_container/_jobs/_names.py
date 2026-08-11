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

The rename therefore does not ride along with a CLI-surface change; it
ships with the migration verb that enforces stop -> remove -> install and
makes install-before-uninstall impossible. Flipping this one constant is
the code half of that change.
"""

from __future__ import annotations

from typing import Iterable

#: Canonical-name prefix for every job sac owns.
#:
#: Kept as a constant so the ecosystem rename to
#: ``scitex-agent-container-`` is a one-line change guarded by the
#: migration verb (see the module docstring), rather than a literal
#: scattered across the CLI, the provider and the audit.
JOB_PREFIX = "sac."


def canonical(name: str) -> str:
    """Return the canonical ``JobSpec.name`` for a local-or-canonical name.

    Idempotent: an already-canonical name is returned unchanged, so a
    value copied out of ``--json`` output or off a unit filename resolves
    to itself instead of being prefixed twice.
    """
    if not name:
        raise ValueError("job name must be non-empty")
    return name if name.startswith(JOB_PREFIX) else JOB_PREFIX + name


def local(name: str) -> str:
    """Return the short name an operator types, for a canonical name."""
    return name[len(JOB_PREFIX) :] if name.startswith(JOB_PREFIX) else name


def is_ours(name: str) -> bool:
    """True when ``name`` is a job THIS package owns."""
    return name.startswith(JOB_PREFIX)


def resolve(typed: str, declared: Iterable[str]) -> str:
    """Resolve a typed name against the jobs actually declared.

    Returns the canonical name. Raises :class:`KeyError` naming every
    available local name when the typed one matches nothing — a verb that
    silently does nothing for a typo is how a scheduled job quietly stops
    being scheduled.
    """
    names = list(declared)
    want = canonical(typed)
    if want in names:
        return want
    raise KeyError(
        f"no job named {typed!r} here; available: "
        + (", ".join(sorted(local(n) for n in names)) or "(none)")
    )


__all__ = ["JOB_PREFIX", "canonical", "is_ours", "local", "resolve"]
