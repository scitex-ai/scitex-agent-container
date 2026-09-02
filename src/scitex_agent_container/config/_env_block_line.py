"""Set and unset one key in a spec's ``spec.apptainer.env`` block, as TEXT.

This is the engine behind ``sac agents env set`` / ``sac agents env unset``.
It replaces a hand-written host script (``~/.local/bin/sac-agent-env.sh``)
that did the same job with ``sed`` over the whole file — see
``docs/hand-written-script-retirement-20260902.md`` for the full retirement
record and for what that script got wrong. Two of its faults are the reason
this module is shaped the way it is:

* it compared against the FIRST ``^\\s+K:`` line anywhere in the document, so
  ``env set NAME model=x`` read ``spec.claude.model``; and
* its ``sed`` rewrote EVERY indented ``K:`` line in the file, in every block.

Both are answered by anchoring on the key PATH — :data:`ENV_PATH` — through
:mod:`._yaml_line_edit`, which is the convention :mod:`._a2a_host_line` and
:mod:`._to_home_layers_line` already established for editing a spec.

WHY A TEXT EDIT AND NOT ``yaml.safe_load`` + ``yaml.safe_dump``
    A spec is an agent's identity and these files are hand-maintained: the
    fleet's specs carry paragraphs of comments recording WHY a field holds
    the value it does (one live spec spends 25 lines explaining a single
    ``runtime: tui``). A load/dump round-trip loses every one of them, plus
    quote style, blank lines and key order, and it does so even when nothing
    was changed. The sibling script this replaces used exactly that round-trip
    and is criticised for it in the retirement record; repeating it here would
    port the defect along with the feature.

``spec.apptainer.env`` — NOT ``spec.env``
    Top-level ``spec.env`` was relocated in the v3 realign and is now REFUSED
    by the validator with a hint (``config._validation._V3_RELOCATED_FIELDS``).
    ``spec.apptainer.env`` is the live home: :func:`.._parsers._apptainer
    .parse_apptainer` reads it and ``config._loaders`` merges it over sac's
    auto-derived namespace, so a value written here is what the agent's
    process actually gets.

WHAT IT REFUSES RATHER THAN GUESSES AT
    A value that is a block scalar (``|``/``>``), an anchor/alias/tag, or a
    collection is refused by name and NOTHING is written — for ``unset`` that
    is not fastidiousness but correctness, since deleting the single line of a
    multi-line value leaves its continuation dangling. A refusal aborts the
    whole edit, so a two-key ``set`` never lands half of itself.

IDEMPOTENCE IS MEASURED AGAINST THE LOADER, NOT THE TEXT
    ``KEY: 1`` and ``KEY: "1"`` are different bytes and the same environment
    variable, because ``parse_apptainer`` stringifies every value. So the
    existing value is read with the YAML parser and compared as ``str(...)``
    — the loader's own expression — and an edit that would change only the
    quoting reports ``unchanged`` and writes nothing.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Iterable, Sequence

import yaml

from ._yaml_line_edit import (
    find_block,
    find_key,
    inline_value,
    split_ending,
    split_inline_comment,
)

#: Where an agent's environment lives in a v3 spec document.
ENV_PATH = ("spec", "apptainer", "env")

#: The block :data:`ENV_PATH` hangs off, and the one this editor will create
#: ``env:`` inside. It will NOT create the apptainer block itself: a spec with
#: no engine block is a shape whose author meant something we cannot infer.
APPTAINER_PATH = ENV_PATH[:-1]

# POSIX-portable environment variable names. Deliberately narrower than
# `_yaml_line_edit._KEY_RE` (which also allows `.` and `-`): a key with a dot
# in it is a legal YAML key and not a variable any shell can export, so it is
# rejected at the door rather than written and then silently ignored.
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

REFUSED_NO_APPTAINER = "spec.apptainer block not found"
REFUSED_INLINE_APPTAINER = "spec.apptainer carries an inline value, not a block"
REFUSED_EMPTY_APPTAINER = "spec.apptainer has no child keys to anchor to"
REFUSED_INLINE_ENV = (
    "spec.apptainer.env carries an inline value other than {} — refusing to "
    "rewrite a shape this editor did not author"
)

#: Actions a single key can undergo. ``unchanged`` and ``absent`` are the two
#: NO-OP outcomes and are reported rather than swallowed: "already at target"
#: is the answer an operator re-running a command needs to see.
ADDED = "added"
UPDATED = "updated"
UNCHANGED = "unchanged"
REMOVED = "removed"
ABSENT = "absent"


class MalformedAssignment(ValueError):
    """A ``KEY=VALUE`` argument (or a bare KEY) that is not usable.

    Raised by :func:`parse_assignment` / :func:`validate_key` and mapped by
    the CLI onto exit code 2, so a typo is distinguishable from a spec that
    could not be edited (exit 1).
    """


@dataclass(frozen=True)
class EnvChange:
    """What happened to ONE key. ``old``/``new`` are None where inapplicable."""

    key: str
    action: str
    old: "str | None" = None
    new: "str | None" = None

    @property
    def is_noop(self) -> bool:
        return self.action in (UNCHANGED, ABSENT)


@dataclass(frozen=True)
class EnvEdit:
    """The outcome of an edit: the text, whether it moved, and why not.

    ``changed`` is False both when every key was already at its target and
    when the edit was refused; ``refusal`` separates those. On a refusal
    ``text`` is the ORIGINAL, byte-identical — the edit is all-or-nothing.
    """

    text: str
    changed: bool
    changes: "tuple[EnvChange, ...]" = ()
    refusal: "str | None" = None


def parse_assignment(arg: str) -> "tuple[str, str]":
    """Split ``KEY=VALUE`` into its halves, or raise :class:`MalformedAssignment`.

    Splits on the FIRST ``=`` so a value may contain more of them
    (``GIT_SSH_COMMAND=ssh -o ControlMaster=no`` is a real fleet value). An
    EMPTY value is legal and means the empty string, which is a different
    thing from unsetting the key.
    """
    key, sep, value = arg.partition("=")
    if not sep:
        raise MalformedAssignment(
            f"{arg!r} is not KEY=VALUE — no '='. To remove a key use "
            "`sac agents env unset`."
        )
    return validate_key(key), value


def validate_key(key: str) -> str:
    """Return ``key`` if it is a usable variable name, else raise."""
    if not _ENV_KEY_RE.match(key):
        raise MalformedAssignment(
            f"{key!r} is not a usable environment variable name — letters, "
            "digits and underscore only, and not starting with a digit."
        )
    return key


def render_scalar(value: str) -> str:
    """Render ``value`` as a double-quoted YAML scalar.

    ALWAYS quoted, even for a value that would survive unquoted. An unquoted
    value is where the surprises live: a bare ``:`` splits the line into two
    keys, a leading ``*`` is an alias, ``no`` is a boolean under YAML 1.1, and
    a trailing ``#`` starts a comment. The retired shell script quoted for the
    same reason and was right to.

    ``json.dumps`` is the renderer because YAML's double-quoted style accepts
    JSON's escape set exactly; ``ensure_ascii=False`` keeps non-ASCII readable
    rather than turning it into ``\\uXXXX``.
    """
    return json.dumps(value, ensure_ascii=False)


def _last_structural(bodies: "list[str]", start: int, stop: int) -> "int | None":
    """Index of the last line carrying structure in ``[start, stop)``.

    Insertions anchor here rather than at ``stop - 1`` so a comment block
    introducing the NEXT sibling key stays attached to that key instead of
    being pushed below the line we add.
    """
    for i in range(stop - 1, start - 1, -1):
        stripped = bodies[i].strip()
        if stripped and not stripped.startswith("#"):
            return i
    return None


def _insert_lines(lines: "list[str]", index: int, new_bodies: "Sequence[str]") -> None:
    """Insert ``new_bodies`` immediately after ``lines[index]``.

    Reuses the anchor's own line ending, and terminates the anchor first when
    it is the file's last line and has none — otherwise the anchor and the
    first inserted line fuse into one, which is valid YAML for neither. Twin
    of :func:`._yaml_line_edit.insert_after`, which inserts exactly one
    ``key: value`` line; this one also has to open a block.
    """
    body, ending = split_ending(lines[index])
    terminator = ending or "\n"
    if not ending:
        lines[index] = body + terminator
    for offset, new_body in enumerate(new_bodies):
        lines.insert(index + 1 + offset, new_body + terminator)


def _read_existing(scalar: str, key: str) -> "tuple[str | None, str | None]":
    """Read an existing inline value as the LOADER would, or refuse.

    Returns ``(value, refusal)``. ``value`` is None both for a bare ``KEY:``
    (no value at all) and for an explicit null — neither is a string, so any
    requested value differs from it and the line gets rewritten.
    """
    if scalar == "":
        return None, None
    if scalar[0] in "|>&*!":
        return None, (
            f"spec.apptainer.env.{key} holds a {scalar[0]!r}-style value "
            "(block scalar, anchor, alias or tag) that spans or references "
            "more than this line; edit it by hand."
        )
    try:
        loaded = yaml.safe_load(scalar)
    except yaml.YAMLError as exc:
        return None, f"spec.apptainer.env.{key} does not parse as a scalar: {exc}"
    if isinstance(loaded, (dict, list)):
        return None, (
            f"spec.apptainer.env.{key} holds a collection, not a scalar; "
            "edit it by hand."
        )
    if loaded is None:
        return None, None
    # `str(...)` is `_parsers._apptainer.parse_apptainer`'s own expression for
    # turning a spec value into an environment value. Comparing any other way
    # would let this editor and the loader disagree about what is already set.
    return str(loaded), None


def _child_indent_step(indent: str, child_indent: str) -> str:
    """The document's own indent step, derived rather than assumed.

    ``max(..., 2)`` guards a pathological spec whose nesting does not deepen;
    two spaces is what every fleet spec uses and what a fresh block gets.
    """
    return " " * max(len(child_indent) - len(indent), 2)


def _set_one(
    text: str, key: str, value: str
) -> "tuple[str, EnvChange | None, str | None]":
    """Set one key. Returns ``(text, change, refusal)``; on refusal ``text`` is
    unchanged and ``change`` is None."""
    lines = text.splitlines(keepends=True)
    bodies = [split_ending(raw)[0] for raw in lines]

    apptainer = find_block(bodies, APPTAINER_PATH)
    if apptainer is None:
        return text, None, REFUSED_NO_APPTAINER
    if apptainer.inline_value is not None:
        return text, None, REFUSED_INLINE_APPTAINER
    if apptainer.child_indent is None:
        return text, None, REFUSED_EMPTY_APPTAINER

    step = _child_indent_step(apptainer.indent, apptainer.child_indent)
    env = find_block(bodies, ENV_PATH)

    # No env block at all — open one at the END of the apptainer block, which
    # is where every fleet spec that has one keeps it.
    if env is None:
        anchor = _last_structural(bodies, apptainer.start, apptainer.stop)
        if anchor is None:  # pragma: no cover - child_indent implies a child
            return text, None, REFUSED_EMPTY_APPTAINER
        child_indent = apptainer.child_indent + step
        _insert_lines(
            lines,
            anchor,
            [
                f"{apptainer.child_indent}env:",
                f"{child_indent}{key}: {render_scalar(value)}",
            ],
        )
        return "".join(lines), EnvChange(key, ADDED, None, value), None

    # `env: {}` — the shape `yaml.safe_dump` writes for an empty mapping.
    # Reopening it as a block is a defined transformation, so it is done
    # rather than refused; any OTHER inline value is refused.
    if env.inline_value is not None:
        if env.inline_value.strip() != "{}":
            return text, None, REFUSED_INLINE_ENV
        comment = split_inline_comment(inline_value(bodies[env.key_line]) or "")[1]
        suffix = f"  {comment}" if comment else ""
        ending = split_ending(lines[env.key_line])[1]
        lines[env.key_line] = f"{env.indent}env:{suffix}{ending}"
        child_indent = env.indent + step
        _insert_lines(
            lines, env.key_line, [f"{child_indent}{key}: {render_scalar(value)}"]
        )
        return "".join(lines), EnvChange(key, ADDED, None, value), None

    # A bare `env:` with no children yet.
    if env.child_indent is None:
        child_indent = env.indent + step
        _insert_lines(
            lines, env.key_line, [f"{child_indent}{key}: {render_scalar(value)}"]
        )
        return "".join(lines), EnvChange(key, ADDED, None, value), None

    existing = find_key(bodies, env.start, env.stop, env.child_indent, key)
    if existing is None:
        anchor = _last_structural(bodies, env.start, env.stop)
        if anchor is None:  # pragma: no cover - child_indent implies a child
            anchor = env.key_line
        _insert_lines(
            lines, anchor, [f"{env.child_indent}{key}: {render_scalar(value)}"]
        )
        return "".join(lines), EnvChange(key, ADDED, None, value), None

    scalar, comment = split_inline_comment(inline_value(bodies[existing]) or "")
    old, refusal = _read_existing(scalar, key)
    if refusal is not None:
        return text, None, refusal
    if old == value:
        # Byte-identical return: an idempotent re-run must not touch the file,
        # so the caller can compare and skip the write (and the restart).
        return text, EnvChange(key, UNCHANGED, old, value), None

    ending = split_ending(lines[existing])[1]
    suffix = f"  {comment}" if comment else ""
    lines[existing] = f"{env.child_indent}{key}: {render_scalar(value)}{suffix}{ending}"
    return "".join(lines), EnvChange(key, UPDATED, old, value), None


def _collapse_empty_env(text: str) -> str:
    """Rewrite a childless ``env:`` to ``env: {}`` after the last key is removed.

    A bare ``env:`` parses as null, which ``parse_apptainer`` already coerces
    to ``{}``, so this is cosmetic for the loader and load-bearing for the
    reader: ``env:`` alone looks like a block whose contents went missing.
    Comment lines left behind by the removal are kept — an operator's note is
    not ours to delete — and YAML ignores their indentation, so the document
    still parses.
    """
    lines = text.splitlines(keepends=True)
    bodies = [split_ending(raw)[0] for raw in lines]
    env = find_block(bodies, ENV_PATH)
    if env is None or env.inline_value is not None or env.child_indent is not None:
        return text
    comment = split_inline_comment(inline_value(bodies[env.key_line]) or "")[1]
    suffix = f"  {comment}" if comment else ""
    ending = split_ending(lines[env.key_line])[1]
    lines[env.key_line] = f"{env.indent}env: {{}}{suffix}{ending}"
    return "".join(lines)


def _unset_one(text: str, key: str) -> "tuple[str, EnvChange | None, str | None]":
    """Remove one key. A key that is not there is ``absent``, never an error."""
    lines = text.splitlines(keepends=True)
    bodies = [split_ending(raw)[0] for raw in lines]

    env = find_block(bodies, ENV_PATH)
    if env is None or env.inline_value is not None or env.child_indent is None:
        return text, EnvChange(key, ABSENT), None

    existing = find_key(bodies, env.start, env.stop, env.child_indent, key)
    if existing is None:
        return text, EnvChange(key, ABSENT), None

    scalar = split_inline_comment(inline_value(bodies[existing]) or "")[0]
    old, refusal = _read_existing(scalar, key)
    if refusal is not None:
        # Refusing to READ it is refusing to DELETE it: a multi-line value
        # whose first line is removed leaves its continuation dangling.
        return text, None, refusal

    del lines[existing]
    return _collapse_empty_env("".join(lines)), EnvChange(key, REMOVED, old, None), None


def set_env(text: str, pairs: "Iterable[tuple[str, str]]") -> EnvEdit:
    """Apply every ``(key, value)`` to ``spec.apptainer.env``.

    All-or-nothing: the first refusal returns the ORIGINAL text with nothing
    applied, so a spec never ends up carrying half of a multi-key request.
    """
    current = text
    changes: "list[EnvChange]" = []
    for key, value in pairs:
        current, change, refusal = _set_one(current, key, value)
        if refusal is not None:
            return EnvEdit(text, False, (), refusal)
        assert change is not None  # no refusal => a change was reported
        changes.append(change)
    return EnvEdit(current, current != text, tuple(changes))


def unset_env(text: str, keys: "Iterable[str]") -> EnvEdit:
    """Remove every key in ``keys``. Same all-or-nothing rule as :func:`set_env`."""
    current = text
    changes: "list[EnvChange]" = []
    for key in keys:
        current, change, refusal = _unset_one(current, key)
        if refusal is not None:
            return EnvEdit(text, False, (), refusal)
        assert change is not None  # no refusal => a change was reported
        changes.append(change)
    return EnvEdit(current, current != text, tuple(changes))


def should_restart(edit: EnvEdit, requested: bool) -> bool:
    """Whether ``--restart`` should actually fire.

    A separate, pure predicate so the rule — *restart only when the spec moved*
    — can be exercised without a container, a tmux session or a live agent.
    Restarting after a no-op is not merely wasteful: a restart costs an agent
    its turn and, on a fleet sweep, does it once per invocation forever.
    """
    return requested and edit.changed


__all__ = [
    "ABSENT",
    "ADDED",
    "APPTAINER_PATH",
    "ENV_PATH",
    "REFUSED_EMPTY_APPTAINER",
    "REFUSED_INLINE_APPTAINER",
    "REFUSED_INLINE_ENV",
    "REFUSED_NO_APPTAINER",
    "REMOVED",
    "UNCHANGED",
    "UPDATED",
    "EnvChange",
    "EnvEdit",
    "MalformedAssignment",
    "parse_assignment",
    "render_scalar",
    "set_env",
    "should_restart",
    "unset_env",
    "validate_key",
]
