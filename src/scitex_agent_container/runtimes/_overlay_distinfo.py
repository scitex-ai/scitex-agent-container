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
TWO.

What happens next is SILENT, not fatal — measured
--------------------------------------------------
An earlier version of this docstring claimed ``importlib.metadata`` refuses a
duplicated distribution, so the agent dies at BOOT. That is WRONG, and wrong in
the direction that matters: it invites the inference "no agent died, so the
fleet is clean". Measured 2026-08-05 on a live masked agent (scitex-hub,
CPython 3.12.3, twelve shadowed packages, which booted normally):

* ``importlib.metadata`` SEES both dist-infos (``count == 2``) and silently
  picks one. No exception, no warning.
* The pick is READDIR ORDER — raw filesystem order, no arbitration. Confirmed
  against the discriminator: the base entry preceded the overlay in
  ``os.listdir`` while *sorted* order would have put the overlay first.
* So the winner is PER-PACKAGE UNPREDICTABLE. Of the six packages with two
  visible dist-infos, metadata chose the OVERLAY for three and the BASE for
  three. There is no "metadata reads the base" rule — that guess was made and
  this measurement refuted it.
* METADATA RESOLUTION AND CODE RESOLUTION ARE INDEPENDENT. The code comes from
  whichever package directory the merge exposes; the metadata from whichever
  ``.dist-info`` readdir yields first. Different mechanisms over different
  directory entries, so they can split — 12 masked, 6 with duplicate metadata,
  1 where metadata and code actually disagreed (``openai``: metadata 2.53.0,
  running code 2.44.0).

The honest statement of the harm is neither "12 are broken" nor "only 1
disagrees": ANY OF THE SIX COULD DISAGREE, AND ONE DOES. ``__file__`` cannot
tell you which — the merged view presents a single path either way — so only
``__version__`` reveals which code actually loaded.

Consequence for anything reading a version through ``importlib.metadata``,
version-drift detection included: it is not merely blind to masking, it is wrong
in an UNPREDICTABLE DIRECTION. Metadata may report newer than the running code
(reads as current — the dangerous way) or older (reads as stale). A consistent
bias could be corrected for; this cannot.

THREE HARM CHANNELS, AND WHY A VERSION COMPARISON IS THE WRONG DETECTOR
-----------------------------------------------------------------------
The obvious remedy — "cross-check ``__version__`` against metadata" — is a
WEAKER instrument than the duplicate count this module already computes, and
the third channel is what proves it:

1. STALE CODE. The overlay's package directory wins the merge, so the agent
   runs an old version. Visible as a version difference host-side.
2. WRONG VERSION REPORTED. Metadata resolves to the other dist-info than the
   one whose code loaded (``openai`` above). Visible to a metadata-vs-code
   comparison.
3. ENTRY POINTS FROM THE SHADOWED DIST-INFO. Tools that iterate ALL
   distributions — pytest's ``pytest11``, and anything calling
   ``entry_points()`` — read the duplicate's declarations too. Measured on the
   same agent: ``scitex_dev-0.42.0.dist-info`` declares
   ``pytest11 = scitex_dev._core._test_execution_plugin``; the code on disk is
   0.21.0 and that module does not exist; pytest raises ``ModuleNotFoundError``
   before collecting a single test.

Channel 3 is INVISIBLE to channels 1 and 2's detectors. On that agent,
``scitex_dev`` read as "agree" — metadata 0.21.0, code 0.21.0, both correct and
consistent — while being the most broken package in the container. A version
comparison cannot see it, because nothing about the VERSION is wrong; the harm
is in the other dist-info's metadata being consumed by a third party.

So the detector stays a DUPLICATE COUNT. Two dist-infos for one distribution is
the hazard, whatever the versions do. A version comparison would have called
this container healthier than it is.

The 3.12.3 result is not silently claimed for other interpreters —
``importlib.metadata``'s duplicate handling has changed across releases.

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

An inside-out scan does not merely miss some — its blind spot has a SHAPE. It
can only ever see shadows where the two dist-info names DIFFER, because a
same-version shadow and a live whiteout each leave exactly ONE directory in the
merged view, and one directory is indistinguishable from not being shadowed at
all. Measured on the same agent: 12 found host-side, 6 found from inside. The
six misses were the four same-version shadows plus ``pydantic`` /
``pydantic-core``, whose whiteouts were working. That is a systematic bias, not
a sampling gap, so "I checked from inside and it looked fine" is guaranteed to
under-report and must not be treated as a negative result.

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
