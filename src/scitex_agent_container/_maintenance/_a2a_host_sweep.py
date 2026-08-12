"""The ``spec.a2a.host`` sweep: plan it, and verify it changed nothing else.

:mod:`._spec_sweep_plan` builds the plan and
:mod:`._layers_migration_apply` applies it; this module supplies the two
a2a-specific pieces — the editor to run, and the post-write gate.

The gate matters more than usual here. This migration's whole claim is ZERO
behaviour change: it writes into each spec the exact value the code already
falls back to, so what the file says changes and what the process binds does
not. A claim like that is worth nothing unless something checks it, and it
cannot be checked before the write (the check is "does the written document
still parse to the same thing?"). So the honest shape is the one
``apply_migration`` already implements — write, verify, and UNDO if the
verification fails — with this module answering "verified how".

:func:`verify_hosts` re-parses every written spec and demands two things:

  * ``spec.a2a.host`` is now present and equals the expected value, and
  * the document is otherwise IDENTICAL to the pre-write parse.

The second is what catches a line editor that inserted its key in the right
place and mangled something else on the way — the failure mode a per-file
"did my key land?" check is blind to.

Three-valued on purpose: a spec that no longer PARSES after the write is
neither "fine" nor "wrong value", it is ``unparsable``, and it is reported as
its own category because the two have different causes and different fixes.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path

import yaml

from ..config._a2a_defaults import DEFAULT_A2A_HOST
from ..config._a2a_host_line import insert_a2a_host
from ._layers_migration_model import MigrationPlan
from ._spec_sweep_plan import plan_spec_sweep


def plan_a2a_host_sweep(
    agents_root: Path, host: str = DEFAULT_A2A_HOST
) -> MigrationPlan:
    """Plan the sweep over ``agents_root``. Reads only."""
    return plan_spec_sweep(agents_root, lambda text: insert_a2a_host(text, host))


def _without_a2a_host(doc: "dict | None") -> "dict | None":
    """A copy of ``doc`` with ``spec.a2a.host`` removed, if it is there.

    Deep-copied so the caller's parse is never mutated: the comparison is run
    against documents the caller still needs intact for its own reporting.
    """
    if not isinstance(doc, dict):
        return doc
    out = copy.deepcopy(doc)
    a2a = (out.get("spec") or {}).get("a2a")
    if isinstance(a2a, dict):
        a2a.pop("host", None)
    return out


@dataclass(frozen=True)
class HostDeclarationDiff:
    """What re-parsing the written specs proved. ``safe`` gates the rollback.

    ``wrong`` and ``drifted`` are kept apart because they fail the same gate
    for opposite reasons: ``wrong`` means the key we meant to write is not
    what we meant it to be, ``drifted`` means the key is right and something
    ELSE moved. Reporting both as "verification failed" would leave whoever
    reads the rollback with no idea which of their assumptions broke.
    """

    checked: "tuple[str, ...]" = ()
    #: Written, re-parsed, but ``spec.a2a.host`` is not the expected value.
    wrong: "tuple[str, ...]" = ()
    #: Written and no longer parses as YAML at all.
    unparsable: "tuple[str, ...]" = ()
    #: Parses, host is right, but the rest of the document changed.
    drifted: "tuple[str, ...]" = ()

    @property
    def safe(self) -> bool:
        return not (self.wrong or self.unparsable or self.drifted)

    def summary(self) -> str:
        parts = [f"{len(self.checked)} spec(s) re-parsed"]
        for label, names in (
            ("WRONG host", self.wrong),
            ("UNPARSABLE", self.unparsable),
            ("DRIFTED", self.drifted),
        ):
            if names:
                parts.append(f"{len(names)} {label} ({', '.join(names)})")
        if self.safe:
            parts.append("host declared as expected, documents otherwise identical")
        return "; ".join(parts)


def verify_hosts(
    plan: MigrationPlan,
    before: "dict[str, dict | None]",
    host: str = DEFAULT_A2A_HOST,
) -> HostDeclarationDiff:
    """Re-parse every spec the plan wrote and compare against ``before``.

    ``before`` maps agent name to that spec's PRE-WRITE parse, captured by the
    caller while the originals were still on disk. Passing it in rather than
    re-deriving it is deliberate: after the write the original is only in the
    archive, and a verification that reads the archive would be checking the
    rollback path with the rollback path.
    """
    checked: list[str] = []
    wrong: list[str] = []
    unparsable: list[str] = []
    drifted: list[str] = []

    for edit in plan.writable:
        agent = edit.agent
        checked.append(agent)
        try:
            after_doc = yaml.safe_load(edit.path.read_text())
        except (
            OSError,
            yaml.YAMLError,
        ):  # stx-allow: fallback (reason: an unreadable/unparsable post-write spec IS the verification failure; record it and keep checking the rest)
            unparsable.append(agent)
            continue

        declared = ((after_doc or {}).get("spec") or {}).get("a2a") or {}
        if not isinstance(declared, dict) or declared.get("host") != host:
            wrong.append(agent)
            continue

        if _without_a2a_host(after_doc) != _without_a2a_host(before.get(agent)):
            drifted.append(agent)

    return HostDeclarationDiff(
        checked=tuple(checked),
        wrong=tuple(wrong),
        unparsable=tuple(unparsable),
        drifted=tuple(drifted),
    )


def parse_specs(plan: MigrationPlan) -> "dict[str, dict | None]":
    """Parse every spec the plan would write, BEFORE anything is written.

    A spec that does not parse here maps to None, which
    :func:`verify_hosts` will then compare against — so a file that was
    already broken before the sweep does not masquerade as damage the sweep
    caused.
    """
    out: "dict[str, dict | None]" = {}
    for edit in plan.writable:
        try:
            out[edit.agent] = yaml.safe_load(edit.path.read_text())
        except (
            OSError,
            yaml.YAMLError,
        ):  # stx-allow: fallback (reason: a pre-existing parse failure is recorded as None, not treated as sweep damage)
            out[edit.agent] = None
    return out


__all__ = [
    "HostDeclarationDiff",
    "parse_specs",
    "plan_a2a_host_sweep",
    "verify_hosts",
]
