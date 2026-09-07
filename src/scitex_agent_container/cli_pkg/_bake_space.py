"""Will this pull fit? — the free-space decision for ``bake-remote``.

MEASURED 2026-09-06/07 on scitex-compute-03: `bake-remote` started a
~7.6G transfer onto a volume with 4.0G free. It could not finish, and
what it left behind reduced the space available to the next attempt —
so every failure made the next one likelier. A pull that cannot fit is
not a pull; refusing is the correct answer and it must be said out loud.

PURE ON PURPOSE. This module does no I/O: the caller measures (remote
artifact size, existing partial, free bytes) and this decides. That
keeps the arithmetic — which is where the resume subtlety lives —
testable without a filesystem, a remote host, or a 7G file.

THE RESUME SUBTLETY, which a naive check gets wrong. rsync runs with
``--partial``, so an interrupted transfer leaves bytes that the next run
RESUMES from. The requirement is therefore the REMAINDER, not the whole
artifact: a 7.6G pull that already has 7.0G on disk needs 0.6G, and a
checker that demands 7.6G would refuse a transfer that fits comfortably.
Refusing work that would have succeeded is its own defect.

THREE-VALUED, because the size can be UNKNOWN. If the remote size cannot
be determined (ssh failed, stat unavailable, unparseable output), that is
not "it fits" and not "it does not fit" — it is "cannot tell". Collapsing
unknown into either pole is the constitution's most-shipped bug. Unknown
PROCEEDS, because refusing every pull whenever a probe is flaky would be
a worse failure than the one this prevents, and it SAYS SO, so a pull
that later dies of space is not a mystery.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "SpaceVerdict",
    "DEFAULT_MARGIN_BYTES",
    "check_space",
    "human_bytes",
]

#: Headroom demanded beyond the transfer itself. A pull that lands with
#: zero bytes to spare leaves a host that cannot write its own logs — the
#: 2026-09-06 incident put compute-03 at exactly that point twice.
DEFAULT_MARGIN_BYTES = 2 * 1024**3


@dataclass(frozen=True)
class SpaceVerdict:
    """Fixed shape, so a caller never has to guess which field exists.

    ``proceed`` is the decision. ``known`` says whether it rests on a
    measured size or on not being able to tell — the two are different
    and a caller that conflates them is repeating the bug this guards.
    """

    proceed: bool
    known: bool
    needed: int
    free: int
    reason: str

    def __post_init__(self) -> None:
        if not self.known and not self.proceed:
            raise ValueError(
                "an UNKNOWN size must PROCEED — refusing on 'cannot tell' "
                "would block every pull whenever the size probe is flaky"
            )


def human_bytes(count: int) -> str:
    """Render bytes for a message a tired operator reads at 03:00."""
    value = float(count)
    for unit in ("B", "K", "M", "G", "T"):
        if abs(value) < 1024.0 or unit == "T":
            return f"{value:.1f}{unit}"
        value /= 1024.0
    return f"{value:.1f}T"


def check_space(
    *,
    remote_size: int | None,
    existing_partial: int,
    free: int,
    margin: int = DEFAULT_MARGIN_BYTES,
) -> SpaceVerdict:
    """Decide whether the remaining transfer fits, with headroom.

    ``remote_size`` is ``None`` when it could not be determined.
    ``existing_partial`` is what ``--partial`` already left on disk (0
    when there is none), and is subtracted because the transfer resumes.
    """
    if remote_size is None:
        return SpaceVerdict(
            proceed=True,
            known=False,
            needed=0,
            free=free,
            reason=(
                "remote artifact size UNKNOWN (probe failed) — proceeding "
                "without a space check. If this pull dies partway, disk is "
                f"the first thing to check ({human_bytes(free)} free now)."
            ),
        )
    remaining = max(0, remote_size - existing_partial)
    needed = remaining + margin
    if free >= needed:
        return SpaceVerdict(
            proceed=True,
            known=True,
            needed=needed,
            free=free,
            reason=(
                f"{human_bytes(remaining)} to transfer "
                f"+ {human_bytes(margin)} margin fits in "
                f"{human_bytes(free)} free"
            ),
        )
    return SpaceVerdict(
        proceed=False,
        known=True,
        needed=needed,
        free=free,
        reason=(
            f"REFUSING: needs {human_bytes(remaining)} to transfer "
            f"+ {human_bytes(margin)} margin = {human_bytes(needed)}, but "
            f"only {human_bytes(free)} is free. A pull that cannot fit "
            f"leaves a partial that makes the NEXT attempt likelier to "
            f"fail. Free space on this volume, or bake to another host."
        ),
    )
