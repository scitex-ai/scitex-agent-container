"""Where a directory goes when it must be preserved: BESIDE itself, never inside itself.

One rule, one function, because it is needed in two places — the target's prior
transcript directory before a copy, and the source's own after a completed move —
and the two must not drift.

    <parent>/.old/<stamp>/<name>        correct
    <directory>/.old/<stamp>            a path that cannot exist

MEASURED 2026-08-11, on the canary run's SECOND pass. The first relocation
copied 3.6 MB across two hosts and verified it by byte and line count. The
second — the idempotency retry, run against a target that now held the previous
copy — computed the move-aside destination inside the directory being moved.
``mv`` refused with "cannot move a directory into itself", the transport stopped,
and what should have been a clean retry was a dead end.

A PURE MODULE COULD NOT HAVE CAUGHT THIS. The string was well-formed, the
function returned, every unit test passed, and the test that pinned the value
pinned the wrong value with full confidence. It took a real ``mv`` on a real
host. That is the whole argument for not claiming an adapter works until it has
been exercised against two machines — and it is the reason the second pass was
run at all, rather than declaring victory after the first.

``.old/`` sits beside the displaced directory rather than beside everything else
in the parent, and each displacement gets its own stamp, so two runs do not merge
their contents and a restore is unambiguous about which run it is undoing.
"""

from __future__ import annotations

__all__ = ["move_aside_destination"]


def move_aside_destination(directory: str, stamp: str) -> str:
    """``<parent>/.old/<stamp>/<name>`` for ``directory``.

    Raises on a path with no parent (``"/"``, ``""``): there is nowhere beside it
    to move it to, and inventing somewhere would put the only copy of a
    conversation in a directory nobody would think to look in.
    """
    trimmed = directory.rstrip("/")
    parent, _, name = trimmed.rpartition("/")
    if not name or not parent:
        raise ValueError(
            f"move_aside_destination cannot displace {directory!r} — it has no parent "
            "to be moved aside within"
        )
    return f"{parent}/.old/{stamp}/{name}"
