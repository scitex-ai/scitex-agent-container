"""``spec.claude.flags`` one-argv-token-per-element guard.

Extracted from ``_claude_validation.py`` for the same reason
``_startup_command_validation`` was extracted from ``_validation``: a rule
with an incident behind it reads better as its own module than as a clause,
and it keeps the caller under the 512-line cap. Called from
``validate_claude`` via ``errors.extend(validate_claude_flags(claude_block))``.

WHY THIS EXISTS — measured 2026-08-06. The ``figrecipe`` agent was unbootable
for 15 days (dead since 2026-07-22, assumed to be a dead a2a sidecar). A
restart printed the real cause::

    error: unknown option '--effort ultracode'

Its spec listed ``--effort ultracode`` as ONE element of
``spec.claude.flags``. Each element becomes one argv token, so claude received
that whole string as a single option name and the inner process exited during
boot. Every restart in those 15 days failed identically, and nothing surfaced
it — the agent simply stayed unreachable.

Two properties made this expensive rather than merely wrong. The YAML looks
right: ``- --effort ultracode`` reads exactly like a command line, and the
list-of-argv-tokens contract is invisible at the point of authoring. And the
failure is observable only in boot stderr, which nobody reads until an agent
has been missing for weeks.

THE MATCHER (keyed to the LEADING DASH, deliberately not to whitespace):

  * An entry NOT starting with ``-`` is a VALUE, and its spaces are payload.
    Three live capsule specs pass ``{"mcpServers": {}}`` this way. A
    whitespace-keyed rule would reject all three and block their boots — the
    same harm as the bug, inverted, which is why the axis matters more than
    the symptom here.
  * ``--flag=value`` is one legitimate token even when the value contains
    spaces (``--mcp-config={"mcpServers": {}}``). So the glued case is the one
    whose FIRST whitespace comes BEFORE any ``=``: a flag, a separator, then
    something that should have been its own element.

The refusal REJECTS rather than auto-splits: an author who wrote
``--foo "a b"`` meaning a quoted value would be silently given different
semantics by a helpful splitter, and a spec that boots differently from what
it says is the class of problem this module exists to remove.
"""

from __future__ import annotations


def _is_glued_flag(entry: str) -> bool:
    """True when ``entry`` is a flag and its value crammed into ONE argv token.

    See the module docstring for the incident and the full matcher rationale.
    """
    if not entry.startswith("-"):
        return False  # a bare VALUE; its spaces are payload, not a separator
    first_space = min(
        (i for i, ch in enumerate(entry) if ch.isspace()),
        default=-1,
    )
    if first_space < 0:
        return False  # no whitespace at all — an ordinary flag
    equals = entry.find("=")
    # ``--flag=value with spaces`` is legitimate; ``--flag value`` is not.
    return equals < 0 or first_space < equals


def validate_claude_flags(claude_block: dict) -> list[str]:
    """Reject a ``spec.claude.flags`` element that glues a flag to its value.

    Returns a list of error strings (empty = valid), matching the
    ``list[str]`` contract of the other ``config._*_validation`` siblings.
    Defensive: any unexpected shape collapses to "nothing to check".
    """
    errors: list[str] = []
    if not isinstance(claude_block, dict):
        return errors
    flags = claude_block.get("flags")
    if flags is None:
        return errors
    if not isinstance(flags, list):
        errors.append(
            "spec.claude.flags must be a list of individual argv tokens, got "
            f"{type(flags).__name__}"
        )
        return errors
    for index, entry in enumerate(flags):
        if not isinstance(entry, str):
            errors.append(
                "spec.claude.flags[%d] must be a string argv token, got %r"
                % (index, entry)
            )
            continue
        if _is_glued_flag(entry):
            flag, _, value = entry.partition(" ")
            errors.append(
                f"spec.claude.flags[{index}] glues a flag to its value in one "
                f"argv token:\n    {entry!r}\n"
                "Every flags element is passed as ONE argv token, so claude "
                "receives this whole string as a single option name, fails "
                f"with \"unknown option '{entry}'\", and EXITS DURING BOOT. "
                "That is how figrecipe stayed dead for 15 days (2026-07-22 to "
                "2026-08-06): the restart failed the same way every time and "
                "nothing surfaced it. Split it into two elements:\n"
                f"    - {flag}\n    - {value.strip()}\n"
                "(Use the --flag=value spelling instead if the value itself "
                "contains spaces.)"
            )
    return errors


__all__ = ["validate_claude_flags"]
