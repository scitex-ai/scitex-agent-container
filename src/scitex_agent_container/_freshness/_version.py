"""Version ordering — deliberately strict, and honest about giving up.

Zero dependencies on purpose. This code runs on every ``sac`` invocation
(via the cached-warning path) and inside a cron alarm that must keep
working on a host whose sac is *too old to fix itself*. A hard import of
``packaging`` is a way for the staleness alarm to be taken out by the
very staleness it exists to report.

The contract every function here keeps: **an input it does not fully
understand returns ``None``, never a guess.** ``None`` propagates to
:attr:`Freshness.UNKNOWN`, which is silent. A comparator that guesses
wrong in the "fresh" direction hides a real outage; one that guesses
wrong in the "stale" direction cries wolf until it is disabled. Both
end with nobody trusting the alarm, so neither is permitted.
"""

from __future__ import annotations

import re

__all__ = ["compare", "is_behind", "latest", "parse"]

# X.Y.Z[.N...] with an optional PEP440 pre-release suffix (a/b/rc).
# Anything else -- dev builds, local versions, epochs, post-releases,
# "dev", "0.0.0+unknown" -- is intentionally NOT matched, and therefore
# reported UNKNOWN rather than mis-ordered.
_RE = re.compile(r"^v?(\d+(?:\.\d+)*)(?:(a|b|rc)(\d+))?$")

# Final releases sort AFTER every pre-release of the same number:
# 0.21.17rc1 < 0.21.17. Ranks are compared as the 2nd tuple element.
_PRE_RANK = {"a": 0, "b": 1, "rc": 2}
_FINAL_RANK = 3


def parse(raw: str | None) -> tuple | None:
    """Parse a version into a sortable key, or ``None`` if not understood.

    ``None`` is a first-class answer, not a failure to handle: it is how
    "I cannot order this" reaches the caller as UNKNOWN instead of as a
    silently wrong comparison.

    >>> parse("0.21.17") < parse("v0.21.17") is False
    True
    >>> parse("0.21.9") < parse("0.21.17")   # not string order
    True
    >>> parse("0.21.17rc1") < parse("0.21.17")
    True
    >>> parse("dev") is None
    True
    """
    if not raw or not isinstance(raw, str):
        return None
    m = _RE.match(raw.strip())
    if m is None:
        return None
    release = tuple(int(p) for p in m.group(1).split("."))
    if m.group(2) is None:
        return (release, _FINAL_RANK, 0)
    return (release, _PRE_RANK[m.group(2)], int(m.group(3)))


def compare(a: str | None, b: str | None) -> int | None:
    """``-1`` if a<b, ``0`` if equal, ``1`` if a>b; ``None`` if either is
    unparseable.

    Note ``0.21.17`` and ``v0.21.17`` compare EQUAL — the ``v`` prefix is
    a git-tag spelling of the same release, and the whole point of the
    ghost-tag check is to line tags up against PyPI versions.
    """
    pa, pb = parse(a), parse(b)
    if pa is None or pb is None:
        return None
    return (pa > pb) - (pa < pb)


def is_behind(installed: str | None, latest_: str | None) -> bool | None:
    """Is ``installed`` strictly older than ``latest_``?

    ``None`` when either side is unparseable — the caller MUST map that
    to UNKNOWN and stay quiet. An installed version *newer* than PyPI
    (a dev build, a worktree, an unreleased bump) is ``False``: ahead is
    not behind, and warning a developer that their own unpublished build
    is "stale" is exactly the noise that gets an alarm switched off.
    """
    cmp = compare(installed, latest_)
    if cmp is None:
        return None
    return cmp < 0


def latest(versions) -> str | None:
    """The greatest parseable version in ``versions``, or ``None``.

    Unparseable entries are skipped rather than poisoning the result:
    one weird string on PyPI must not blind the whole check. ``None``
    only when *nothing* was parseable.
    """
    best_key = None
    best_raw = None
    for v in versions or ():
        key = parse(v)
        if key is None:
            continue
        if best_key is None or key > best_key:
            best_key, best_raw = key, v
    return best_raw


# EOF
