"""Resolve declared-intent binds at launch — the runtime half.

``config._bind_intent`` decides what a bind entry SAYS. This module
decides, at ``apptainer exec`` build time, what it DOES on THIS machine
right now: is the source there, is this one of the hosts it named, and
does sac owe it a directory first. Load time is the wrong place for all
three — ``sac agents list`` on a coordinator loads specs for agents that
run on other machines, and it must not mkdir on their behalf nor decide
their source-existence questions from the wrong host.

THE GATE ORDER, and the rule it settles
---------------------------------------
Each entry passes three gates, in this order:

  1. ``hosts:`` — is this entry declared for THIS machine at all?
  2. ``ensure:`` — does sac owe the source a directory?
  3. ``required:`` — if the source is still absent, is that fatal?

That order IS the answer to "``hosts:`` excludes this host AND
``required: true`` — skip or fatal?". It SKIPS.

``hosts:`` and ``required:`` are different questions. ``hosts:`` asks
whether the entry is declared here; ``required:`` asks, of an entry that
IS declared here, whether a missing source is tolerable. On a host the
list excludes, the entry is simply not declared, so ``required:`` is
never reached — a host gate can never produce a fatal.

The alternative (fatal) was rejected on measurement, not taste: since
``required: true`` is the default, fataling would force every
host-conditional entry to ALSO write ``required: false``, conflating the
two axes; and ``/mnt/c`` — the entry this feature exists for — appears in
101 of the fleet's 107 specs and is absent on every non-WSL host, so the
"surprising" branch would be the everyday one. A gate that fatals on the
common path is not a gate.

Because the case is common AND the rule is surprising, the skip line
names the host list, this host, AND ``required: true`` when it was set,
so an operator scanning a launch log sees why a mandatory-looking bind
did not mount.

NOTHING IS EVER DROPPED QUIETLY. Every skip is a WARNING naming the bind
and the reason, and is returned to the caller as a :class:`BindSkip`. A
silently-dropped mount is how an agent comes up looking healthy while
reaching none of its data — the failure mode this whole feature is meant
to make impossible, so it must not be reintroduced by the fix.

``ensure: dir`` failure is LOUD (``RuntimeError``), never a skip, and
that holds even under ``required: false``: "optional" excuses a source
that was never there, not a creation the operator asked for and that
failed. Degrading it would hide a broken mount behind a green boot.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from ..config._bind_intent import BindIntent

__all__ = [
    "BindSkip",
    "declared_bind_intents",
    "local_host_names",
    "resolve_bind_intents",
    "resolve_spec_binds",
    "spec_bind_flags",
]

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BindSkip:
    """One bind that did NOT mount, and why. Never silent."""

    spec: str
    reason: str


def local_host_names() -> set[str]:
    """Every host spelling that denotes THIS machine, lowercased.

    Delegates to the fleet's existing authority
    (``cli_pkg.lifecycle._common._local_host_names``, which itself unions
    ``config._host.resolve_hostname()``, the bare short hostname and the
    ``config.yaml`` alias registry) rather than inventing a second one —
    a bind that disagrees with dispatch about which machine this is would
    be worse than no host gate at all.

    The import is local: ``runtimes`` sits below ``cli_pkg`` in the
    layering, and a module-level import would invert that.
    """
    try:
        from ..cli_pkg.lifecycle._common import _local_host_names

        names = _local_host_names()
    except ImportError:  # stx-allow: fallback (reason: the CLI layer is optional at runtime; config._host is the same resolver the union is built on, so the gate degrades to the canonical name rather than to "match nothing")
        from ..config._host import resolve_hostname

        names = {resolve_hostname()}
    return {str(n).strip().lower() for n in names if str(n).strip()}


def declared_bind_intents(config) -> list[BindIntent]:
    """The declared intents for ``config``, defaulting to "all required".

    ``ApptainerSpec.binds`` stays the SSoT for WHICH binds exist;
    ``bind_intents`` only DECORATES those same strings with their
    declared conditions. When the two disagree — a config built
    programmatically with ``ApptainerSpec(binds=[...])`` and no intents,
    as plenty of call sites and tests do — the strings win and every
    entry is treated as required and unconditional, which is exactly what
    a bare string means. The desync therefore fails SAFE (today's
    behaviour), never into silently skipping a mount.
    """
    ap = getattr(config, "apptainer", None)
    if ap is None:
        return []
    binds = [str(b) for b in getattr(ap, "binds", None) or []]
    intents = list(getattr(ap, "bind_intents", None) or [])
    if len(intents) == len(binds) and all(
        getattr(i, "spec", None) == b for i, b in zip(intents, binds)
    ):
        return intents
    return [BindIntent(spec=b) for b in binds]


def _source_of(bind_spec: str) -> str:
    """Host source (everything before the first ``:``) of a bind string."""
    return bind_spec.split(":", 1)[0]


def _ensure_dir(intent: BindIntent, source: str, agent: str) -> None:
    """Create ``source`` (with parents) for ``ensure: dir``. Loud on failure."""
    try:
        Path(source).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(
            f"Agent {agent!r}: apptainer.binds entry {intent.spec!r} declares "
            f"`ensure: dir`, but creating the source directory {source!r} "
            f"FAILED: {exc}. This is NOT downgraded to a skip — you asked for "
            "the directory to exist, and a bind whose source could not be "
            "created would leave the agent running against nothing. Fix the "
            "path (a non-directory in the way? a read-only parent?), or drop "
            "`ensure: dir` and mount a source that already exists."
        ) from exc


def resolve_bind_intents(
    intents: list[BindIntent],
    *,
    host_names: set[str],
    agent: str = "",
) -> tuple[list[str], list[BindSkip]]:
    """Apply the three gates; return ``(binds_to_emit, skips)``.

    Order-preserving: the emitted list is the declared list minus the
    skips, in declaration order, so the argv a plain-string spec produces
    is byte-identical to the pre-intent one.
    """
    emit: list[str] = []
    skips: list[BindSkip] = []
    for intent in intents:
        # Gate 1 — hosts. Wins over `required:` (see module docstring).
        if intent.hosts:
            declared = {h.strip().lower() for h in intent.hosts}
            if not (declared & host_names):
                skips.append(
                    BindSkip(
                        intent.spec,
                        f"hosts: {list(intent.hosts)} does not include this "
                        f"host {sorted(host_names)} "
                        f"(required: {str(intent.required).lower()} — the "
                        "host gate is evaluated first, so this is a skip, "
                        "not a failure)",
                    )
                )
                continue
        source = _source_of(intent.spec)
        # Gate 2 — ensure. Failure is loud, never a skip.
        if intent.ensure == "dir":
            _ensure_dir(intent, source, agent)
        # Gate 3 — required. Absent source + required: true is emitted
        # unchanged, so apptainer FATALs exactly as it always has.
        if not intent.required and not os.path.exists(source):
            skips.append(
                BindSkip(
                    intent.spec,
                    f"required: false — source {source!r} does not exist "
                    "on this host",
                )
            )
            continue
        emit.append(intent.spec)
    return emit, skips


def resolve_spec_binds(config) -> list[str]:
    """The choke-point entry: resolve ``spec.apptainer.binds`` for THIS host.

    Returns the bind strings to hand to ``--bind``, and logs one WARNING
    per skip. A spec whose entries are all plain strings resolves to the
    identical list it always did, with nothing logged.
    """
    agent = str(getattr(config, "name", "") or "")
    emit, skips = resolve_bind_intents(
        declared_bind_intents(config),
        host_names=local_host_names(),
        agent=agent,
    )
    for skip in skips:
        logger.warning(
            "bind SKIPPED [agent %s]: %s -- %s", agent, skip.spec, skip.reason
        )
    return emit


def spec_bind_flags(config, *, home_backing: Path) -> list[str]:
    """The curated ``--bind`` flags for ``spec.apptainer.binds``.

    Extracted verbatim from ``build_run_argv`` (which sat at the 512-line
    cap) so every declared-bind concern — intent resolution, the
    fleet-default merge and argv emission — lives in ONE module, mirroring
    the sibling ``overlay_flags`` / ``tmpfs_workdir_flags`` /
    ``nested_build_flags`` extractions.

    P3a-2 (operator directive feedback_scitex_todo_single_shared_store,
    lead a2a 214dd26d): the fleet-default binds are PREPENDED so every
    agent inherits the shared stores even when its spec omits the line; an
    explicit spec entry to the same destination overrides the default (de-
    dup by destination — see ``_p3a_default_binds``).

    ADR-0003 D6 follow-up: when a destination sits under ``/home/agent/``
    (the host-side ``runtime/<name>/home/`` bind), apptainer no longer
    scaffolds parent directories — the host dir IS the filesystem at
    ``/home/agent`` — so the parent is pre-created on the host side here,
    giving the bind somewhere to land. Only binds that SURVIVED resolution
    get that treatment: a skipped entry must not leave a directory behind.
    """
    from ._p3a_default_binds import apply_default_binds

    flags: list[str] = []
    for bind in apply_default_binds(resolve_spec_binds(config)):
        if ":" in bind:
            _, _, rest = bind.partition(":")
            dst = rest.split(":", 1)[0]
            if dst.startswith("/home/agent/"):
                rel = dst[len("/home/agent/") :]
                (home_backing / rel).mkdir(parents=True, exist_ok=True)
        flags += ["--bind", bind]
    return flags
