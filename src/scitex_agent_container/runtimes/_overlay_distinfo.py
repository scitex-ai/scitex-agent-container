"""Predict the dist-info collision a RESTART would create in an overlay agent.

The bake-time guards in ``containers/apptainer-*.def`` already assert that a
freshly built image carries exactly one ``.dist-info`` per distribution. They
are correct and they are not enough, because they can only see the IMAGE. An
agent's runtime ``site-packages`` is the MERGED view of the base image and its
per-agent overlay, and the merge is where this breaks.

Why a healthy agent becomes broken without anyone touching it
-------------------------------------------------------------
An overlayfs whiteout masks exactly ONE NAME. When an agent runs
``pip install`` INSIDE its container, pip first uninstalls the copy it is
replacing — the copy in whatever base was mounted AT THAT MOMENT — and that
uninstall lands in the overlay as a whiteout for that one directory name.

Nothing re-evaluates that whiteout later. Swap the base image underneath and
the whiteout still masks the OLD name, which is no longer there, while the NEW
base's ``.dist-info`` is masked by nothing at all. The merged view then shows
TWO, and ``importlib.metadata`` refuses the distribution — so the agent dies at
BOOT, on an image that is itself clean, having changed nothing.

The consequence that makes this worth predicting rather than detecting: a
rolling restart to pick up a newer base does not merely fail to repair such an
agent, it CREATES the failure in agents that were working. "Keep everything
current" is the trigger, so the check has to run BEFORE the restart, not after.

Measured 2026-07-28: two agents held the SAME package version over the SAME
base and were both healthy, yet one predicted 1 and the other 2 at next boot.
The only difference was WHICH names their whiteouts covered — i.e. which base
happened to be mounted when each ran its install. That asymmetry is invisible
from inside either container (the process sees only the merged view, never the
whiteout names), which is precisely why this lives host-side.

Why these functions take NAMES and not paths
--------------------------------------------
Classifying an overlay entry is a filesystem question — a whiteout is a
character-special file, and :func:`os.stat` answers it — but PREDICTING the
merge is pure set algebra over directory names. Splitting them keeps the rule
that actually encodes the bug testable without root, without apptainer, and
without fabricating device nodes: the caller collects three name collections,
these functions decide. Nothing here touches a disk.
"""

from __future__ import annotations

import re
from collections import defaultdict

#: ``scitex_cards-0.17.9.dist-info`` -> distribution ``scitex_cards``, version
#: ``0.17.9``. Anchored, and the version group is deliberately permissive: a
#: local or pre-release segment (``1.2.3+local``, ``2.0.0rc1``) is still one
#: distribution, and treating it as a different one would MISS a collision.
_DIST_INFO = re.compile(r"^(?P<dist>.+?)-(?P<version>[^-]+)\.dist-info$")


def parse_dist_info(name: str) -> tuple[str, str] | None:
    """Split a ``.dist-info`` directory name into ``(distribution, version)``.

    Returns ``None`` for anything that is not a ``.dist-info`` name, so callers
    can hand over a raw directory listing without pre-filtering.
    """
    match = _DIST_INFO.match(name)
    if match is None:
        return None
    return match.group("dist"), match.group("version")


def predict_merged_names(
    base_names: object,
    overlay_real: object,
    overlay_whiteouts: object,
) -> set[str]:
    """Return the ``.dist-info`` names visible after the overlay is merged.

    This is the whole semantics of the bug in one expression: a whiteout
    subtracts the ONE name it spells, and the overlay's own directories are
    added. A whiteout naming a version the new base does not contain therefore
    subtracts NOTHING, which is exactly how the collision appears.
    """
    return (set(base_names) - set(overlay_whiteouts)) | set(overlay_real)


def find_collisions(names: object) -> dict[str, list[str]]:
    """Map each distribution seen more than once to its sorted versions.

    A distribution appearing once is omitted: the result is empty exactly when
    the merged view is safe, so it doubles as the boolean the caller wants.
    """
    versions: dict[str, list[str]] = defaultdict(list)
    for name in names:
        parsed = parse_dist_info(name)
        if parsed is not None:
            versions[parsed[0]].append(parsed[1])
    return {dist: sorted(found) for dist, found in versions.items() if len(found) > 1}


def predict_restart_collisions(
    base_names: object,
    overlay_real: object,
    overlay_whiteouts: object,
) -> dict[str, list[str]]:
    """Collisions a restart onto ``base_names`` would produce for this overlay.

    Empty means the restart is safe for metadata resolution. A non-empty result
    names every distribution that would refuse, and with which versions — the
    form an operator needs to decide whether to reconcile the overlay first.
    """
    merged = predict_merged_names(base_names, overlay_real, overlay_whiteouts)
    return find_collisions(merged)
