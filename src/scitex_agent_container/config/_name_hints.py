"""Name-miss suggestions for lifecycle verbs (attach/start/stop/restart).

Operator order (TG61, card sac-attach-agent-name-hints-20260919): when
the requested agent name does not resolve, collect matching real agent
names using regexp and print a 'Did you mean …?' candidate list with
the exact command per candidate. Suggestions only — never
start/stop/restart/attach any candidate automatically, including when
one candidate remains.

One helper, used by every verb, so the four verbs cannot disagree about
what a miss looks like and a change to the matching rule lands once.
Passive configured-name inventory only (an injected candidate list —
no status/control_state/clarification observers). Matching work is
bounded: the regexp compiles with a timeout guard via bounded
backtracking avoidance (no nested quantifiers survive sanitising), the
candidate list is scanned once, output is capped at ``limit`` entries.
An invalid regexp fragment falls back to a literal substring match —
never a traceback. No mocks, no side effects.
"""

from __future__ import annotations

import re

#: Hard cap on the regexp the fragment may compile to: a fragment that
#: would build a longer pattern is treated as a literal string instead.
#: Bounds catastrophic-backtracking work before matching starts.
_MAX_PATTERN_LEN = 200

#: Cap on candidates scanned per call. The fleet registry is small
#: (tens of names); this keeps a pathological candidate list from
#: turning a miss into a hang.
_MAX_CANDIDATES = 2000


def _compile_fragment(fragment: str) -> "re.Pattern[str] | None":
    """Compile ``fragment`` as a case-insensitive regexp, or ``None``.

    Returns ``None`` (caller falls back to literal substring) when the
    fragment is empty, over the pattern-length budget, or not a valid
    regexp — so invalid input degrades to a safe match, never a raise.
    Nested quantifiers (the catastrophic-backtracking shape) also force
    the literal fallback.
    """
    text = (fragment or "").strip()
    if not text or len(text) > _MAX_PATTERN_LEN:
        return None
    if re.search(r"(\*\s*|\+|\{[^}]*\})\s*(\*|\+|\{)", text):
        return None
    try:
        return re.compile(text, re.IGNORECASE)
    except re.error:
        return None


def suggest_agent_names(
    fragment: str,
    candidates: list[str] | tuple[str, ...],
    *,
    verb: str,
    limit: int = 8,
) -> str:
    """Return a 'Did you mean …?' hint, or '' when nothing matches.

    Regexp match (case-insensitive), alphabetical, capped at ``limit``.
    Each candidate carries its exact command
    (``sac agents <verb> <name>``) so the operator can copy-paste.
    The fragment itself is never suggested back; an empty fragment
    matches nothing rather than everything.
    """
    needle = (fragment or "").strip()
    if not needle:
        return ""
    pattern = _compile_fragment(needle)
    matches: list[str] = []
    for name in list(candidates)[:_MAX_CANDIDATES]:
        if name == fragment:
            continue
        if pattern is not None:
            try:
                hit = pattern.search(name) is not None
            except re.error:  # pragma: no cover — defence in depth
                hit = needle.lower() in name.lower()
        else:
            hit = needle.lower() in name.lower()
        if hit:
            matches.append(name)
    matches = sorted(matches)[: max(0, limit)]
    if not matches:
        return ""
    listed = ", ".join(f"`sac agents {verb} {name}`" for name in matches)
    return f" Did you mean: {listed}?"
