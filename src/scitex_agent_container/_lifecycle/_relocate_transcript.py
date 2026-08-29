"""Carry an agent's transcript to the host it is relocating to, and PROVE it landed.

`_session_carry.plan_session_carry` decides WHETHER the transcript follows the
agent. This executes a ``carry=True`` plan across two hosts — and then reads the
transcript BACK FROM THE TARGET and compares digests before saying it worked.

WHY READ-BACK RATHER THAN A SUCCESSFUL COPY. Measured 2026-08-08: inside a
container, ``~/.scitex`` turned out to be a symlink into a dotfiles git worktree,
created mid-session. Every write to it SUCCEEDED. The bytes went somewhere a
``git clean -xdf`` can erase, and nothing in any exit code said so. A transcript
copy is exactly that shape of operation — a write to a path inside a container
overlay — so "the copy returned 0" is not evidence that the agent will find its
memory when it boots. Confirm arrival, not dispatch.

A DIGEST, NOT A SIZE. A size check passes on a file truncated and re-padded, on
a partially-flushed write that happens to land on the same length, and on a
target path that already held a DIFFERENT transcript of the same size — which is
not far-fetched here, because a relocation target may have been this agent's home
before. Comparing content is barely more work and answers the question actually
being asked.

UNVERIFIABLE IS UNKNOWN, NEVER SUCCESS. If the read-back cannot run, the outcome
is ``carried=None``. That is the difference between "the transcript is there" and
"I could not check", and collapsing them is how the 2026-08-07 relocation
reported healthy while the agent had no memory at all. The caller must treat an
unknown as a reason to stop, not as a soft yes.

NO I/O HERE. The copy and the read-back are callables the caller supplies, so
this module never learns ssh, rsync, or the listen daemon — the same
ports-and-adapters split as `_relocate_preflight` and `_relocate_probe`. It also
means the whole verification contract is testable without a second machine.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Callable, Final

__all__ = [
    "CODE_CARRIED",
    "CODE_MISMATCH",
    "CODE_NOTHING_TO_CARRY",
    "CODE_SOURCE_UNREADABLE",
    "CODE_UNVERIFIABLE",
    "CarryOutcome",
    "carry_transcript",
    "digest",
]

#: Copied AND read back from the target with a matching digest.
CODE_CARRIED: Final = 200
#: The plan said not to carry (opted out, already diverged, nothing to copy).
CODE_NOTHING_TO_CARRY: Final = 204
#: The target's copy does not match the source. The relocation must not proceed.
CODE_MISMATCH: Final = 409
#: The source transcript could not be read; there is nothing to send.
CODE_SOURCE_UNREADABLE: Final = 410
#: The copy may or may not have landed — the read-back could not answer.
CODE_UNVERIFIABLE: Final = 503


def digest(payload: bytes) -> str:
    """Content fingerprint used on both sides of the comparison."""
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class CarryOutcome:
    """What happened, in one shape, with the evidence attached.

    ``carried`` is three-valued: True the transcript is on the target and was
    verified there, False it was deliberately not carried, None UNKNOWN. There is
    deliberately no ``__bool__`` — ``if outcome:`` must not quietly read an
    unknown as a yes, which is the exact mistake this module exists to prevent.
    """

    carried: bool | None
    code: int
    reason: str
    source_digest: str | None = None
    target_digest: str | None = None
    byte_count: int | None = None

    def __post_init__(self) -> None:
        if self.carried not in (True, False, None):
            raise ValueError(
                f"CarryOutcome.carried must be True/False/None, got {self.carried!r}"
            )
        if not self.reason:
            raise ValueError("CarryOutcome.reason must be non-empty")
        if self.carried is True and self.code != CODE_CARRIED:
            raise ValueError(
                f"CarryOutcome: carried=True must carry CODE_CARRIED, got {self.code}"
            )
        if self.carried is True and not self.target_digest:
            raise ValueError(
                "CarryOutcome: a success must carry the digest READ BACK from the "
                "target — without it nothing was verified"
            )
        if self.carried is True and self.source_digest != self.target_digest:
            raise ValueError(
                "CarryOutcome: carried=True with mismatched digests is unrepresentable"
            )


def carry_transcript(
    *,
    carry: bool | None,
    read_source: Callable[[], bytes],
    send: Callable[[bytes], None],
    read_back: Callable[[], bytes],
) -> CarryOutcome:
    """Execute a carry plan and verify it ON THE TARGET.

    ``carry`` is `SeedPlan.carry` — passed in rather than the whole plan so this
    stays independent of that module's shape.

    ``read_source`` returns the source transcript's bytes. ``send`` puts them on
    the target. ``read_back`` returns what the TARGET now holds; it must read
    through the same path the agent will use at boot, or it proves nothing about
    the case that matters.

    Every callable may raise — a network is entitled to fail in ways nobody
    enumerated. Each failure maps to its own outcome rather than to a generic
    error, because "there was nothing to send", "it did not arrive intact", and
    "I could not check" call for three different next actions.
    """
    if carry is None:
        return CarryOutcome(
            carried=None,
            code=CODE_UNVERIFIABLE,
            reason=(
                "the carry decision itself was unknown; resolve it before "
                "moving anything — proceeding would guess about the agent's memory"
            ),
        )
    if carry is False:
        return CarryOutcome(
            carried=False,
            code=CODE_NOTHING_TO_CARRY,
            reason="the plan declined to carry the transcript",
        )

    try:
        payload = read_source()
    except Exception as exc:  # stx-allow: fallback (reason: an unreadable source must become a NAMED outcome, not a crash mid-relocation; the exception text is preserved in the reason)
        return CarryOutcome(
            carried=False,
            code=CODE_SOURCE_UNREADABLE,
            reason=f"the source transcript could not be read ({type(exc).__name__}: {exc})",
        )

    src = digest(payload)

    try:
        send(payload)
    except Exception as exc:  # stx-allow: fallback (reason: a failed send is UNKNOWN, not a clean no — bytes may have partially landed, and only the read-back can say)
        return CarryOutcome(
            carried=None,
            code=CODE_UNVERIFIABLE,
            reason=(
                f"the copy failed ({type(exc).__name__}: {exc}); the target may hold "
                "a partial transcript — check it before retrying"
            ),
            source_digest=src,
            byte_count=len(payload),
        )

    try:
        landed = read_back()
    except Exception as exc:  # stx-allow: fallback (reason: THE central rule — an unverifiable copy is UNKNOWN and must never be reported as carried)
        return CarryOutcome(
            carried=None,
            code=CODE_UNVERIFIABLE,
            reason=(
                f"the copy returned success but could not be read back from the "
                f"target ({type(exc).__name__}: {exc}) — a successful write to a "
                "path inside a container overlay is not evidence the agent will "
                "find it at boot"
            ),
            source_digest=src,
            byte_count=len(payload),
        )

    tgt = digest(landed)
    if tgt != src:
        return CarryOutcome(
            carried=False,
            code=CODE_MISMATCH,
            reason=(
                "the target's copy does not match the source "
                f"({len(landed)} bytes there vs {len(payload)} sent) — do NOT hand "
                "over the lease; the agent would resume from a corrupted transcript"
            ),
            source_digest=src,
            target_digest=tgt,
            byte_count=len(payload),
        )

    return CarryOutcome(
        carried=True,
        code=CODE_CARRIED,
        reason=f"transcript verified on the target ({len(payload)} bytes)",
        source_digest=src,
        target_digest=tgt,
        byte_count=len(payload),
    )
