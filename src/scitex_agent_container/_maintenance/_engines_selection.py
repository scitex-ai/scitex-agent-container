"""WHICH specs the ``spec.engines`` sweep touches — and what it left out.

Split from :mod:`._engines_migration`, which now owns only "what would this do
to them". The two questions have different failure modes, and every failure
mode on THIS side has the same shape: a spec.yaml that exists on disk and is
absent from the count, with nothing in the report to say so. That is the
"reports N done over a fleet of N+1" defect, and a selection is only safe if
it NAMES everything it declined to look at.

FOUR THINGS ARE NAMED RATHER THAN DROPPED, and :class:`SpecSelection` carries
all four:

  ``skipped_templates``   the ``_``-prefixed dirs ``sac agents create`` copies.
  ``shadowed``            a second root's spec.yaml for a name an earlier root
                          already supplied. Exactly one copy may be written —
                          writing both migrates one agent twice, into the copy
                          that loads and into a stale one — but the loser is a
                          real file still carrying the legacy shape, and
                          earlier-root-wins is DETERMINISTIC, so no later run
                          can reach it. Measured before this existed: 4
                          spec.yaml on disk across two roots, ``specs: 3``, and
                          the fourth in no bucket and no payload field.
  ``unmatched_agents``    a ``--agent`` value that matched nothing. Set
                          membership discards a typo or a renamed agent in
                          silence, so ``-a a -a b -a c`` with ``c`` renamed
                          covers two of three on every run and exits 0.
  ``unmatched_hosts``     the same for ``--host``.

AN UNREADABLE SPEC IS KEPT, NEVER EXCLUDED. ``--host`` reads each spec to
decide, and a spec it cannot read reaches the plan as ``unreadable`` — which
makes the plan unsafe to apply. Excluding it would make the batching flag the
thing that disarms the guard against an unsafe apply. For the same reason an
unreadable spec SUPPRESSES the unmatched-host report: "no spec declares that
host" is a claim, and a spec whose hosts could not be read is not evidence for
it.

WHICH COPY WINS, AND WHY THAT IS NOT A CLAIM. Earlier roots win, in the order
the resolver hands them over. The sweep's caller
(:func:`..cli_pkg._agents_migrate_engines.default_spec_roots`) is what makes
that order agree with :func:`...config._resolve.resolve_config`'s own
precedence; this module just does not reorder them. On a genuine collision
that resolver REFUSES to pick and raises ``AmbiguousRegistryScope``, so a
colliding name is a pre-existing fleet fault a human resolves — which is
exactly why the loser is reported rather than dropped.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

__all__ = [
    "ShadowedSpec",
    "SpecSelection",
    "read_spec_text",
    "select_spec_paths",
    "select_spec_paths_over_roots",
]


def read_spec_text(path: Path) -> str:
    """A spec's text with its LINE ENDINGS INTACT.

    ``Path.read_text`` opens in universal-newline mode, which silently turns
    every ``\\r\\n`` into ``\\n`` before the caller sees a byte. A CRLF spec
    read that way and written back is rewritten END TO END — the
    unreviewable whole-file diff the operator asked this sweep to avoid —
    and ``_yaml_line_edit.split_ending``'s CRLF handling becomes unreachable
    because the ``\\r`` is already gone. ``newline=""`` is what makes that
    machinery real rather than decorative.
    """
    with Path(path).open("r", encoding="utf-8", newline="") as handle:
        return handle.read()


def spec_hosts_from_text(text: str) -> "set[str] | None":
    """Every host the spec TEXT places itself on. ``None`` when unparsable.

    Takes text rather than a path so the caller that has already read the
    file judges the SAME bytes it will edit. Re-reading to answer a second
    question is how one file yields two answers.
    """
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError:  # stx-allow: fallback (reason: an unparsable spec must reach the PLAN as "host unknown", not vanish from the selection filter)
        return None
    spec = (doc or {}).get("spec") or {}
    hosts = {str(spec.get("host"))} if spec.get("host") else set()
    declared = spec.get("hosts")
    if isinstance(declared, list):
        hosts |= {str(h) for h in declared if h}
    return hosts


def spec_hosts(path: Path) -> "set[str] | None":
    """Every host a spec places itself on.

    ``set()`` means the spec was read and places itself nowhere. ``None``
    means it COULD NOT BE READ — a different answer, and the one the host
    filter must not confuse with "no match": an unreadable spec that vanishes
    from the selection is the "118 done over a fleet of 119" failure, and
    ``--host`` would then be the flag that disables the guard blocking an
    unsafe apply.
    """
    try:
        text = read_spec_text(path)
    except (
        OSError,
        UnicodeDecodeError,
    ):  # stx-allow: fallback (reason: an unreadable spec must reach the PLAN as unreadable, not vanish from the selection filter)
        return None
    return spec_hosts_from_text(text)


@dataclass(frozen=True)
class ShadowedSpec:
    """A spec.yaml an earlier root's copy of the same AGENT NAME displaced.

    Both paths are carried because the agent name alone does not let anyone
    act on it: resolving the collision means looking at two files.
    """

    agent: str
    kept: Path
    dropped: Path


@dataclass(frozen=True)
class SpecSelection:
    """What this run looks at, and everything it deliberately did not.

    A plain list of paths cannot express the second half, which is why this
    exists: the count must add up to the spec.yaml files on disk, or say
    exactly where the difference went.
    """

    paths: "tuple[Path, ...]" = ()
    skipped_templates: "tuple[str, ...]" = field(default=())
    shadowed: "tuple[ShadowedSpec, ...]" = field(default=())
    unmatched_agents: "tuple[str, ...]" = field(default=())
    unmatched_hosts: "tuple[str, ...]" = field(default=())


def _candidates(roots, *, templates: bool) -> "tuple[list[Path], list[str]]":
    """Every spec.yaml under every root, sorted per root, plus the templates."""
    picked: "list[Path]" = []
    skipped: "list[str]" = []
    for root in roots:
        for path in sorted(Path(root).glob("*/spec.yaml")):
            if path.parent.name.startswith("_") and not templates:
                if path.parent.name not in skipped:
                    skipped.append(path.parent.name)
                continue
            picked.append(path)
    return picked, skipped


def _by_host(
    paths: "list[Path]", wanted: "set[str]"
) -> "tuple[list[Path], set[str], bool]":
    """Filter by host; report which wanted hosts matched, and any blind spot.

    The third value is True when at least one candidate's hosts could not be
    read. It suppresses the unmatched-host report, because "nothing declares
    that host" is a claim and a spec nobody could read is not evidence for it.
    """
    kept: "list[Path]" = []
    matched: "set[str]" = set()
    blind = False
    for path in paths:
        declared = spec_hosts(path)
        if declared is None:
            blind = True
            kept.append(path)
            continue
        hit = declared & wanted
        if hit:
            matched |= hit
            kept.append(path)
    return kept, matched, blind


def select_spec_paths_over_roots(
    roots: "tuple[Path, ...]",
    *,
    hosts: "tuple[str, ...]" = (),
    agents: "tuple[str, ...]" = (),
    templates: bool = False,
) -> SpecSelection:
    """The selection over SEVERAL roots, de-duped by AGENT NAME.

    Batching is the operator's own condition for trusting a 119-file rewrite:
    ``agents`` names an explicit set and ``hosts`` takes one machine at a
    time. The order is sorted so a dry-run and the apply that follows it
    agree on what they are looking at.

    THE BATCH SIZE IS NOT HERE. ``--limit`` caps what gets WRITTEN, and which
    specs those are is only knowable after planning — see
    :func:`.._engines_migration.plan_engines_migration`. Capping the glob
    instead re-selected the same first N on every run, so the second batch
    wrote nothing and reported the sweep complete.

    De-duplication is by NAME rather than by path, because the same agent
    under two roots is two paths and writing both migrates one agent twice —
    once into the copy that loads and once into a stale one. The loser is
    returned in :attr:`SpecSelection.shadowed` rather than dropped.
    """
    candidates, skipped = _candidates(roots, templates=templates)

    wanted_agents = {a for a in agents if a}
    matched_agents: "set[str]" = set()
    if wanted_agents:
        matched_agents = {p.parent.name for p in candidates} & wanted_agents
        candidates = [p for p in candidates if p.parent.name in wanted_agents]

    wanted_hosts = {h for h in hosts if h}
    matched_hosts: "set[str]" = set()
    blind = False
    if wanted_hosts:
        candidates, matched_hosts, blind = _by_host(candidates, wanted_hosts)

    picked: "list[Path]" = []
    shadowed: "list[ShadowedSpec]" = []
    first: "dict[str, Path]" = {}
    for path in candidates:
        name = path.parent.name
        if name in first:
            shadowed.append(ShadowedSpec(name, first[name], path))
            continue
        first[name] = path
        picked.append(path)

    return SpecSelection(
        paths=tuple(picked),
        skipped_templates=() if templates else tuple(skipped),
        shadowed=tuple(shadowed),
        unmatched_agents=tuple(sorted(wanted_agents - matched_agents)),
        unmatched_hosts=(
            () if blind else tuple(sorted(wanted_hosts - matched_hosts))
        ),
    )


def select_spec_paths(
    root: Path,
    *,
    hosts: "tuple[str, ...]" = (),
    agents: "tuple[str, ...]" = (),
    templates: bool = False,
) -> "tuple[list[Path], list[str]]":
    """One root's ``(paths, skipped template names)``.

    The single-root shorthand, expressed through the several-roots form so
    there is exactly one implementation of the filters. A caller that needs
    the shadowed copies or the unmatched selectors calls
    :func:`select_spec_paths_over_roots` and reads the full selection —
    a single root cannot shadow anything, and this shape is what the
    bucket-level unit tests want.
    """
    selection = select_spec_paths_over_roots(
        (root,), hosts=hosts, agents=agents, templates=templates
    )
    return list(selection.paths), list(selection.skipped_templates)
