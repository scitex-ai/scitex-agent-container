"""Line-level primitives for editing a spec WITHOUT reformatting it.

:mod:`._to_home_layers_line` and :mod:`._group_sync` both refuse a
ruamel/pyyaml load+dump round-trip, for the reason stated in their docstrings:
it would reformat unrelated content along the way — quote styles,
flow-vs-block, blank lines, comment placement. A spec is an agent's identity
and a bulk rewrite of 102 hand-maintained files is the worst possible place to
discover that. This module holds the pieces those editors need in common so a
third editor does not become a third convention.

The substantive piece is :func:`find_block`, which locates a key by its FULL
PATH rather than by a bare regex. That distinction is not academic here. The
fleet's specs contain three different things a naive ``^\\s*a2a:`` or
``^\\s*host:`` anchor will happily match:

  * ``spec.host`` — the machine placement (``ywata-note-win``), present in
    102 of 102 specs and appearing EARLIER in the file than the a2a block;
  * ``spec.comms.a2a`` — a different ``a2a`` key at a deeper indent, present
    in 101 of 102;
  * ``spec.a2a.host`` — the bind address, the only one an a2a edit means.

Anchoring on line order instead of structure happens to work on today's files
(measured: ``spec.a2a`` precedes ``spec.comms.a2a`` in all 102). Working by
luck is not the same as working, and the luck is invisible in the diff.

Indentation is compared by LENGTH. YAML forbids tabs for indentation, so on a
well-formed spec this is exact; a file that mixes them fails to match and is
refused, which is the correct outcome for a shape we have not seen.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# A plain mapping key at the start of a line. Deliberately strict: an
# identifier-shaped key only. A quoted key, a merge key, or a sequence item
# (``- command: …``) does NOT match, so an unfamiliar shape becomes a refusal
# instead of a guess.
_KEY_RE = re.compile(
    r"^(?P<indent>[ \t]*)(?P<key>[A-Za-z_][A-Za-z0-9_.-]*):[ \t]*(?P<value>\S.*)?$"
)


def split_ending(raw: str) -> "tuple[str, str]":
    """Split a raw line into ``(body, line_ending)``.

    Hand-split rather than via ``splitlines()`` so CRLF files and a final line
    with no terminator at all both survive an edit unchanged. Twin of
    ``_to_home_layers_line._split_ending``; that one predates this module.
    """
    if raw.endswith("\r\n"):
        return raw[:-2], "\r\n"
    if raw.endswith("\n"):
        return raw[:-1], "\n"
    return raw, ""


def _indent_of(body: str) -> str:
    return body[: len(body) - len(body.lstrip(" \t"))]


def is_skippable(body: str) -> bool:
    """True for blank and comment-only lines — they carry no structure.

    Public because two things outside this module need the SAME answer: an
    editor walking back over the comment block that belongs to the key it is
    inserting above, and one locating where a block's real content ends. A
    second spelling of "is this line structure?" is exactly the drift this
    module was created to prevent.
    """
    stripped = body.strip()
    return not stripped or stripped.startswith("#")


@dataclass(frozen=True)
class KeyLine:
    """A parsed ``<indent><key>: <value>`` line. ``value`` is "" for a block."""

    indent: str
    key: str
    value: str


def parse_key_line(body: str) -> "KeyLine | None":
    """Split one line into indent / key / value, or None if it is not a key.

    The same strict :data:`_KEY_RE` :func:`find_key` matches on, exposed so a
    caller reading a VALUE (rather than locating a key) does not reach for a
    regex of its own and quietly accept a shape this module refuses.
    """
    m = _KEY_RE.match(body)
    if m is None:
        return None
    return KeyLine(m.group("indent"), m.group("key"), m.group("value") or "")


def last_content_line(bodies: "list[str]", start: int, stop: int) -> "int | None":
    """Index of the last line carrying structure in ``[start, stop)``.

    A block's ``stop`` is the next SIBLING key, so the lines between a block's
    final child and its ``stop`` are the blank and comment lines that visually
    introduce that sibling. Replacing through ``stop`` would eat them; this is
    where a replacement must end instead.
    """
    for i in range(stop - 1, start - 1, -1):
        if not is_skippable(bodies[i]):
            return i
    return None


@dataclass(frozen=True)
class Block:
    """Where one mapping key and its children live, as indices into ``bodies``.

    ``child_indent`` is None when the key has no child lines at all — either
    it carries an inline value (``inline_value`` is then set) or the block is
    empty. Both are shapes the callers refuse rather than edit.
    """

    key_line: int
    indent: str
    child_indent: "str | None"
    #: First child line index, and one past the last.
    start: int
    stop: int
    inline_value: "str | None" = None


def _block_extent(bodies: "list[str]", key_line: int, indent: str) -> int:
    """Index one past the last line belonging to the block opened at ``key_line``.

    The block ends at the first line carrying structure (not blank, not a
    comment) whose indent is no deeper than the key's own. Blank and comment
    lines are NOT terminators: a comment sitting between two sibling keys is
    extremely common in these specs, and treating it as the end of the block
    would truncate the search right where the interesting keys are.
    """
    for i in range(key_line + 1, len(bodies)):
        body = bodies[i]
        if is_skippable(body):
            continue
        if len(_indent_of(body)) <= len(indent):
            return i
    return len(bodies)


def _first_child_indent(
    bodies: "list[str]", start: int, stop: int, indent: str
) -> "str | None":
    for i in range(start, stop):
        body = bodies[i]
        if is_skippable(body):
            continue
        child = _indent_of(body)
        return child if len(child) > len(indent) else None
    return None


def find_key(
    bodies: "list[str]", start: int, stop: int, indent: str, key: str
) -> "int | None":
    """Index of the line declaring ``key`` at exactly ``indent`` within a range.

    Exact-indent matching is what keeps a nested namesake out of the result —
    the whole reason this module exists.
    """
    for i in range(start, stop):
        body = bodies[i]
        if is_skippable(body):
            continue
        m = _KEY_RE.match(body)
        if m is None or m.group("indent") != indent or m.group("key") != key:
            continue
        return i
    return None


def find_block(bodies: "list[str]", path: "tuple[str, ...]") -> "Block | None":
    """Locate the block at the dotted ``path``, or None if it is not there.

    Each segment is matched as a direct child of the previous one, at exactly
    the enclosing block's child indent. ``find_block(bodies, ("spec", "a2a"))``
    therefore cannot return ``spec.comms.a2a`` (deeper) and cannot be confused
    by ``spec.host`` (a different key), regardless of what order they appear in.
    """
    search_start, search_stop = 0, len(bodies)
    indent = ""
    block: "Block | None" = None
    for depth, segment in enumerate(path):
        i = find_key(bodies, search_start, search_stop, indent, segment)
        if i is None:
            return None
        m = _KEY_RE.match(bodies[i])
        assert m is not None  # find_key only returns lines that matched
        inline = m.group("value")
        if inline:
            # An inline value ends the descent. Returning it (rather than None)
            # lets the caller distinguish "no such key" from "the key is there
            # but its shape is one I do not edit" — two different refusals.
            block = Block(
                key_line=i,
                indent=indent,
                child_indent=None,
                start=i + 1,
                stop=i + 1,
                inline_value=inline,
            )
            return block if depth == len(path) - 1 else None
        stop = _block_extent(bodies, i, indent)
        child_indent = _first_child_indent(bodies, i + 1, stop, indent)
        block = Block(
            key_line=i,
            indent=indent,
            child_indent=child_indent,
            start=i + 1,
            stop=stop,
        )
        if depth == len(path) - 1:
            return block
        if child_indent is None:
            return None
        search_start, search_stop, indent = i + 1, stop, child_indent
    return block


def insert_after(
    lines: "list[str]", index: int, indent: str, key: str, value: str
) -> None:
    """Insert ``<indent><key>: <value>`` immediately after ``lines[index]``.

    Reuses the anchor's own line ending so the new line is indistinguishable
    from a hand-authored neighbour. When the anchor is the file's LAST line and
    carries no terminator, terminating only the new line is not enough — the
    two keys fuse onto one line, which is valid YAML for neither — so the
    anchor itself gains the terminator. That case is the one
    ``_to_home_layers_line`` records as caught by a test after a comment
    claimed it was already handled; it is reproduced here on purpose.
    """
    body, ending = split_ending(lines[index])
    terminator = ending or "\n"
    if not ending:
        lines[index] = body + terminator
    lines.insert(index + 1, f"{indent}{key}: {value}{terminator}")


__all__ = [
    "Block",
    "KeyLine",
    "find_block",
    "find_key",
    "insert_after",
    "is_skippable",
    "last_content_line",
    "parse_key_line",
    "split_ending",
]
