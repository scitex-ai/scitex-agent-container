"""Insert an explicit ``spec.a2a.host`` declaration into a spec, as a TEXT edit.

Every agent spec should state its own a2a bind address rather than inheriting
one from a default buried in four separate call sites. 101 of the fleet's 102
specs already do; one omits the key and is bound by the code default instead.
This module writes that key.

**The value written is the code default itself**,
:data:`._a2a_defaults.DEFAULT_A2A_HOST` — the value every reader already falls
back to (:func:`._parsers._a2a.parse_a2a`, the a2a sidecar, ``sac a2a doctor``,
and the health probe). Making the spec explicit is therefore a change to what
the file SAYS, never to what the process BINDS. That equivalence is the entire
point of the migration and is pinned by test rather than assumed — see
:mod:`._a2a_defaults` for why the five spellings of that default are not yet
collapsed onto one.

Anchoring is by key PATH (``spec`` → ``a2a``), not by regex over the whole
file. See :mod:`._yaml_line_edit` for why: ``spec.host`` and ``spec.comms.a2a``
are both real keys in these files, both would satisfy a bare anchor, and
neither is the bind address.

The new key goes after ``port:``, matching the ``(port, host)`` order the
overwhelming majority of already-declaring specs use, so a migrated file still
reads like the files around it.
"""

from __future__ import annotations

from dataclasses import dataclass

from ._a2a_defaults import DEFAULT_A2A_HOST
from ._yaml_line_edit import find_block, find_key, insert_after, split_ending

#: The path of the bind address within a v3 spec document.
A2A_PATH = ("spec", "a2a")

# Refusal reasons. Deliberately CONSTANT strings with nothing interpolated:
# the sweep groups refusals by reason to stay readable across 102 specs, and a
# reason carrying a filename would put every spec in its own group.
REFUSED_ALREADY_DECLARED = "already declares spec.a2a.host"
REFUSED_NO_A2A_BLOCK = "no spec.a2a block to anchor to"
REFUSED_INLINE_A2A = "spec.a2a has an inline value, not a block"
REFUSED_EMPTY_A2A = "spec.a2a has no child keys"
REFUSED_NO_PORT = "spec.a2a has no port: line to anchor to"


@dataclass(frozen=True)
class LineEdit:
    """The outcome of one text edit: the text, whether it changed, and why not.

    The sibling editors return a bare ``(text, changed)`` pair. That is not
    enough here. 101 of 102 specs already declare the key, so a sweep reporting
    ``False`` for each of them cannot distinguish the 101 benign no-ops from a
    spec whose shape the editor genuinely did not recognise — which is the one
    thing an operator reading the dry-run needs to see. ``reason`` carries that
    distinction; it is None exactly when ``changed`` is True.
    """

    text: str
    changed: bool
    reason: "str | None" = None


def insert_a2a_host(text: str, host: str = DEFAULT_A2A_HOST) -> LineEdit:
    """Add ``host: <host>`` to the spec's ``a2a`` block, after ``port:``.

    Returns a :class:`LineEdit`. When ``changed`` is False the text comes back
    BYTE-IDENTICAL and ``reason`` names which of these applies:

      * the spec already declares ``spec.a2a.host`` (idempotent re-run);
      * there is no ``spec.a2a`` block, it carries an inline value, it has no
        children, or it has no ``port:`` line to anchor to.

    Only the last group is a request for human attention — but none of them is
    a silent success, and none of them writes a guess. A spec is an agent's
    identity; "needs manual attention" beats a plausible insertion into the
    wrong block.
    """
    lines = text.splitlines(keepends=True)
    bodies = [split_ending(raw)[0] for raw in lines]

    block = find_block(bodies, A2A_PATH)
    if block is None:
        return LineEdit(text, False, REFUSED_NO_A2A_BLOCK)
    if block.inline_value is not None:
        return LineEdit(text, False, REFUSED_INLINE_A2A)
    if block.child_indent is None:
        return LineEdit(text, False, REFUSED_EMPTY_A2A)

    # Idempotence is checked INSIDE the located block, not over the file. A
    # file-wide `host:` scan would match `spec.host` — which every spec has —
    # and report all 102 as already declared, migrating nothing while looking
    # like a clean run.
    if (
        find_key(bodies, block.start, block.stop, block.child_indent, "host")
        is not None
    ):
        return LineEdit(text, False, REFUSED_ALREADY_DECLARED)

    port_line = find_key(bodies, block.start, block.stop, block.child_indent, "port")
    if port_line is None:
        return LineEdit(text, False, REFUSED_NO_PORT)

    insert_after(lines, port_line, block.child_indent, "host", host)
    return LineEdit("".join(lines), True)


__all__ = [
    "A2A_PATH",
    "REFUSED_ALREADY_DECLARED",
    "REFUSED_EMPTY_A2A",
    "REFUSED_INLINE_A2A",
    "REFUSED_NO_A2A_BLOCK",
    "REFUSED_NO_PORT",
    "LineEdit",
    "insert_a2a_host",
]
