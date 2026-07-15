"""``spec.startup_commands`` destructive-command guard.

Extracted from ``_validation.py`` to keep that orchestrator under the
512-line cap (sibling to ``_claude_validation`` / ``_placement_validation``
/ ``_shape_validation``). Called from ``validate_raw`` via
``errors.extend(validate_startup_commands(spec))``.

WHY THIS EXISTS — 2026-07-16 P0 (highest-blast-radius incident to date).
Every one of the fleet's 96 generated agent specs shipped an UNGUARDED::

    rm -rf $HOME/proj 2>/dev/null; ln -sfn <src> $HOME/proj

in ``startup_commands``. ``$HOME/proj`` is normally a SYMLINK, so the
author's intent was "atomically re-point the symlink". But ``rm -rf`` on a
*variable* target is a landmine: if ``$HOME/proj`` ever resolved to a real
directory (a stale checkout, a failed earlier symlink, a race) the
recursive force-delete would descend into it and wipe the ~195 real repos
underneath. One bad symlink from deleting the fleet's entire working tree.

The fixed form we shipped is symlink-checked + NON-recursive::

    [ -L "$HOME/proj" ] && rm -f "$HOME/proj"; ln -sfn <src> "$HOME/proj"

``rm -f`` (no ``-r``) CANNOT descend into a directory, so even if the guard
were wrong the blast radius is a single inode. This validator makes the
landmine IMPOSSIBLE TO REINTRODUCE: a spec whose ``startup_commands`` carry
a recursive-force ``rm`` on a variable target is REJECTED at validate time
(constitution: "prefer hooks to prompts; make the destructive path
impossible; fail fast and loud").

THE MATCHER (deliberately conservative — optimised for ZERO false
positives, because over-rejecting a valid spec blocks an agent boot):

  * The command string is split into simple-command SEGMENTS on shell
    control operators (``;`` ``&&`` ``||`` ``|`` ``&`` newline ``(`` ``)``)
    so ``rm`` is only inspected when it is in COMMAND POSITION — ``echo rm
    -rf $X`` (rm is an argument to echo) is NOT flagged.
  * Within a segment, leading ``KEY=VAL`` env-assignments are stripped and
    the command word must be ``rm`` (or ``/bin/rm`` / ``/usr/bin/rm``).
    Wrapper-prefixed forms (``sudo rm``, ``xargs rm``) are intentionally
    OUT OF SCOPE — they were not the incident and parsing their own option
    grammar would add false-positive risk; this guard stays narrow.
  * The rm's flags are parsed: short clusters (``-rf`` / ``-fr`` / ``-Rf``
    / ``-r`` / ``-f``), separated short flags (``-r -f``), long flags
    (``--recursive`` / ``--force``), and the ``--`` end-of-options marker.
    BOTH recursive (``r`` / ``R`` / ``--recursive``) AND force (``f`` /
    ``--force``) must be present — a plain ``rm -f $VAR`` (the FIXED form)
    is ALLOWED.
  * At least one operand (target) must contain a shell variable: a ``$``
    (``$HOME``, ``${SCRATCH}``, ``$VAR``) or a leading ``~`` (tilde home
    expansion). A purely literal target (``rm -rf /opt/build``) is the
    author's explicit choice and is NOT flagged — only a VARIABLE target
    can silently resolve to the wrong tree.

Known, accepted limitations (documented so review is honest): a shell
metacharacter INSIDE a quoted operand (``rm -rf "$HOME/a;b"``) may
mis-segment, and a single-quoted variable (``rm -rf '$HOME/proj'`` —
literal text in real shell) over-matches because ``shlex`` in posix mode
discards the quote style. Both are pathological, weighed against the
incident, and err toward the recurrence guard's purpose rather than
crashing realistic specs.
"""

from __future__ import annotations

import re
import shlex

# Shell control operators that terminate one simple command and begin the
# next. Splitting on these puts the command word at segment position 0, so
# we only inspect ``rm`` when it is actually being invoked (not passed as an
# argument to ``echo`` / ``printf`` / a subshell). ``||`` and ``&&`` are
# listed before the single-char class so a two-char operator is one split
# point rather than two.
_SEGMENT_SPLIT = re.compile(r"\|\||&&|[;\n\r|&()]")

# Command words that ARE a recursive-capable ``rm`` invocation. Absolute
# paths are included because ``/bin/rm -rf $X`` is exactly as dangerous as
# the bare ``rm``.
_RM_WORDS = frozenset({"rm", "/bin/rm", "/usr/bin/rm"})


def _strip_env_assignments(tokens: list[str]) -> list[str]:
    """Drop leading ``KEY=VAL`` shell env-var assignments.

    ``FOO=bar rm -rf $X`` → ``["rm", "-rf", "$X"]``. Same rule as the helper
    in ``_listen._inline_spec_startup_lint`` (kept local to avoid importing
    the _listen layer from config/).
    """
    out = list(tokens)
    while out:
        head = out[0]
        eq = head.find("=")
        if eq <= 0:
            break
        ident = head[:eq]
        if not (ident[0].isalpha() or ident[0] == "_"):
            break
        if not all(c.isalnum() or c == "_" for c in ident):
            break
        out.pop(0)
    return out


def _is_variable_target(operand: str) -> bool:
    """True when ``operand`` can expand to a path the author did not spell.

    A ``$`` anywhere (``$HOME``, ``${SCRATCH}/x``, ``a$b``) is a parameter
    expansion; a leading ``~`` is tilde home-expansion. Either means the
    real delete target is not its literal text — the landmine condition.
    """
    return "$" in operand or operand.startswith("~")


def _rm_is_recursive_force_on_variable(tokens: list[str]) -> bool:
    """True when ``tokens`` (one simple command) is the ``rm -rf $VAR`` landmine.

    Requires: command word is ``rm``; BOTH recursive and force flags are
    present; and at least one operand is a variable target. See the module
    docstring for the full flag grammar covered.
    """
    tokens = _strip_env_assignments(tokens)
    if not tokens or tokens[0] not in _RM_WORDS:
        return False
    recursive = False
    force = False
    has_variable_target = False
    end_of_options = False
    for tok in tokens[1:]:
        if not end_of_options and tok == "--":
            # POSIX end-of-options: everything after is an operand.
            end_of_options = True
            continue
        if not end_of_options and tok.startswith("--"):
            name = tok[2:].split("=", 1)[0]
            if name == "recursive":
                recursive = True
            elif name == "force":
                force = True
            # Any other long option (``--verbose``, ``--one-file-system``)
            # is irrelevant to the recursive+force+variable signature.
            continue
        if not end_of_options and tok.startswith("-") and len(tok) > 1:
            # Short flag cluster: ``-rf`` / ``-fr`` / ``-Rf`` / ``-v`` ...
            letters = tok[1:]
            if "r" in letters or "R" in letters:
                recursive = True
            if "f" in letters:
                force = True
            continue
        # Operand (delete target).
        if _is_variable_target(tok):
            has_variable_target = True
    return recursive and force and has_variable_target


def _command_is_unguarded_recursive_var_delete(command: str) -> bool:
    """True when any simple-command segment of ``command`` is the landmine."""
    for segment in _SEGMENT_SPLIT.split(command):
        segment = segment.strip()
        if not segment:
            continue
        try:
            tokens = shlex.split(segment, comments=False, posix=True)
        except ValueError:
            # Un-tokenisable segment (unbalanced quote / stray backslash).
            # The recursive+force+variable signature cannot be CONFIRMED
            # here, so we do not reject — shell-syntax errors are owned by
            # the _listen startup lint. Prefer not to false-positive.
            continue
        if _rm_is_recursive_force_on_variable(tokens):
            return True
    return False


def validate_startup_commands(spec: dict) -> list[str]:
    """Reject any ``startup_commands`` entry with an unguarded ``rm -rf $VAR``.

    Returns a list of error strings (empty = valid), matching the
    ``list[str]`` contract of the other ``config._*_validation`` siblings
    called from ``validate_raw``. Defensive: any unexpected shape collapses
    to "nothing to check".
    """
    errors: list[str] = []
    if not isinstance(spec, dict):
        return errors
    cmds = spec.get("startup_commands")
    if not isinstance(cmds, list):
        return errors
    for index, entry in enumerate(cmds):
        if not isinstance(entry, dict):
            continue
        command = entry.get("command")
        if not isinstance(command, str) or not command.strip():
            continue
        if _command_is_unguarded_recursive_var_delete(command):
            errors.append(
                f"spec.startup_commands[{index}].command runs an UNGUARDED "
                f"recursive delete of a variable path:\n    {command}\n"
                "`rm -rf $VAR` recurses, and a variable target can resolve to "
                "a real directory — this is the 2026-07-16 P0 where an "
                "unguarded `rm -rf $HOME/proj` was one bad symlink from "
                "deleting ~195 fleet repos. Use a symlink-checked, "
                "NON-recursive delete instead:\n"
                '    [ -L "$HOME/proj" ] && rm -f "$HOME/proj"\n'
                "(-L checks it is a symlink; -f without -r cannot descend "
                "into a directory). Never `rm -rf $VAR` in startup_commands "
                "(recursive + variable target = landmine)."
            )
    return errors


__all__ = ["validate_startup_commands"]
