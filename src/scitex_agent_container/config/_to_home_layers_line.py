"""Insert a ``to_home_layers:`` declaration into a spec, as a TEXT edit.

Migrating the fleet to declare its ``to_home`` cascade means touching 102
hand-maintained ``spec.yaml`` files. A full ruamel/pyyaml load+dump cycle would
do it in three lines and reformat unrelated content along the way — quote
styles, flow-vs-block, blank lines, comment placement. :mod:`._group_sync`
already rejected that trade for exactly this reason, and this module follows
the same convention rather than inventing a second one: a targeted line edit
that leaves every byte it did not intend to touch.

The anchor is the spec's own ``to_home:`` line. That is the natural sibling of
the new key, and it is present in 106 of the 107 spec files on this host, so
the insertion point is well-defined almost everywhere. Where it is NOT — one
spec today — the edit is REFUSED rather than guessed at. A spec is an agent's
identity; "needs manual attention" is a far better outcome than a plausible
insertion into the wrong block.

Idempotent: a spec that already declares ``to_home_layers`` is left untouched,
so the migration can be re-run without accumulating duplicates.
"""

from __future__ import annotations

import re

_TO_HOME_RE = re.compile(r"^(?P<indent>[ \t]*)to_home:[ \t]*(?P<value>\S.*)?$")
_ALREADY_RE = re.compile(r"^[ \t]*to_home_layers:")


def _split_ending(raw: str) -> "tuple[str, str]":
    if raw.endswith("\r\n"):
        return raw[:-2], "\r\n"
    if raw.endswith("\n"):
        return raw[:-1], "\n"
    return raw, ""


def render_layers_value(layers: "list[str]") -> str:
    """Render ``layers`` as the flow list the fleet's specs author.

    Flow style (``[a, b]``) matches how ``groups:`` is written in these specs,
    so a migrated file still reads like the files around it. An empty list
    renders as ``[]`` — a spec inheriting nothing, which is a real declaration
    and not the same as omitting the key.
    """
    return "[" + ", ".join(layers) + "]"


def insert_to_home_layers(text: str, layers: "list[str]") -> "tuple[str, bool]":
    """Add ``to_home_layers: [...]`` after the spec's ``to_home:`` line.

    Returns ``(new_text, changed)``. ``changed`` is False — and ``text`` comes
    back byte-identical — when:

      * the spec already declares ``to_home_layers`` (idempotent re-run), or
      * no ``to_home:`` line exists to anchor to (refused, not guessed).

    A False return is a request for human attention, never a silent success.
    Only the FIRST ``to_home:`` line is used; these specs author exactly one,
    and touching more would be guessing about a shape we have not seen.
    """
    lines = text.splitlines(keepends=True)
    bodies = [_split_ending(raw)[0] for raw in lines]

    # Idempotence, checked PER LINE. A bare `search()` would need re.MULTILINE
    # to see anything past the first line, and silently matching only at
    # position 0 is the kind of near-miss that makes a re-run duplicate a key.
    if any(_ALREADY_RE.match(body) for body in bodies):
        return text, False

    for i, raw in enumerate(lines):
        body, ending = _split_ending(raw)
        m = _TO_HOME_RE.match(body)
        if not m:
            continue
        # Reuse the anchor's own indent and line ending so the inserted line is
        # indistinguishable from a hand-authored neighbour.
        #
        # When the anchor is the file's last line and carries NO terminator,
        # terminating only the NEW line is not enough — the two keys fuse onto
        # one line ("to_home: ./x  to_home_layers: [...]"), which is valid YAML
        # for neither. The anchor itself has to gain the terminator. Caught by
        # test, after a comment here claimed it was already handled.
        terminator = ending or "\n"
        if not ending:
            lines[i] = body + terminator
        new_line = (
            f"{m.group('indent')}to_home_layers: "
            f"{render_layers_value(layers)}{terminator}"
        )
        lines.insert(i + 1, new_line)
        return "".join(lines), True
    return text, False


__all__ = ["insert_to_home_layers", "render_layers_value"]
