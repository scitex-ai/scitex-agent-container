#!/usr/bin/env python3
"""Refuse shell loops where ``pgrep -f`` provably matches the waiter itself.

Claude Code submits the Bash command on stdin in a PreToolUse JSON envelope.
The command later appears in the full command line of its ``bash -c`` process.
Consequently a regex that matches the submitted text also matches the waiter.

This detector is intentionally conservative. It does not ban ``pgrep`` or
``pgrep -f`` generally; it requires a while/until loop and a full-command-line
pattern that can actually be matched against the submitted command.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys

try:
    import scitex_logging as slogging

    log = slogging.getLogger(__name__)
except ImportError:  # standalone copy without sac installed
    class _StderrFallback:
        """Minimal log-surface writing verbatim lines to stderr.

        Used only when ``scitex_logging`` is not importable (standalone
        copy on agent $HOME / bare SIF). Diagnostics go to stderr —
        never stdout, which carries protocol frames.
        """

        @staticmethod
        def _write(message: str) -> None:
            sys.stderr.write(f"{message}\n")
            sys.stderr.flush()

        def error(self, message: str) -> None:
            self._write(message)

        warning = error
        info = error

    log = _StderrFallback()
from collections.abc import Iterator, Sequence

DENY = 2
ALLOW = 0

_LOOP_HEADS = frozenset({"while", "until"})
_COMMAND_BOUNDARIES = frozenset({";", "&", "&&", "|", "||", ")"})
_SHORT_OPTIONS_WITH_ARGUMENT = frozenset("dGgPstUu")
_LONG_OPTIONS_WITH_ARGUMENT = frozenset(
    {
        "--delimiter",
        "--group",
        "--pgroup",
        "--parent",
        "--session",
        "--terminal",
        "--euid",
        "--uid",
        "--ns",
        "--nslist",
        "--signal",
        "--cgroup",
        "--env",
    }
)


def _tokens(command: str) -> list[str]:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()<>")
    lexer.commenters = ""
    lexer.whitespace_split = True
    return list(lexer)


def _pgrep_patterns(tokens: Sequence[str]) -> Iterator[str]:
    """Yield pattern operands from invocations that enable full-line matching."""
    for start, executable in enumerate(tokens):
        if os.path.basename(executable) != "pgrep":
            continue

        full = False
        skip_next = False
        options_done = False
        for token in tokens[start + 1 :]:
            if token in _COMMAND_BOUNDARIES:
                break
            if skip_next:
                skip_next = False
                continue
            if not options_done and token == "--":
                options_done = True
                continue
            if not options_done and token.startswith("--"):
                name = token.split("=", 1)[0]
                if name == "--full":
                    full = True
                if name in _LONG_OPTIONS_WITH_ARGUMENT and "=" not in token:
                    skip_next = True
                continue
            if not options_done and token.startswith("-") and token != "-":
                option_chars = token[1:]
                for index, char in enumerate(option_chars):
                    if char == "f":
                        full = True
                    if char in _SHORT_OPTIONS_WITH_ARGUMENT:
                        if index == len(option_chars) - 1:
                            skip_next = True
                        break
                continue

            if full:
                yield token
            break


def self_matching_pattern(command: str) -> str | None:
    """Return the first provably self-matching looped pgrep pattern."""
    try:
        tokens = _tokens(command)
    except ValueError:
        return None

    if not _LOOP_HEADS.intersection(tokens) or "do" not in tokens:
        return None

    for pattern in _pgrep_patterns(tokens):
        try:
            if re.search(pattern, command):
                return pattern
        except re.error:
            # pgrep will diagnose its own invalid ERE. Do not turn a parser
            # disagreement into a denial of an otherwise legitimate command.
            continue
    return None


def _command_from_stdin() -> str:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError, TypeError):
        return ""
    if payload.get("tool_name") != "Bash":
        return ""
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return ""
    command = tool_input.get("command") or ""
    return command if isinstance(command, str) else ""


def main() -> int:
    command = _command_from_stdin()
    pattern = self_matching_pattern(command)
    if pattern is None:
        return ALLOW

    log.error(
        "BLOCKED by deny_self_matching_pgrep_wait.sh: this looping "
        f"`pgrep -f` pattern matches the waiter's own command line: {pattern!r}\n\n"
        "After the intended process exits, pgrep will continue finding the "
        "waiter's `bash -c` process, so this background task cannot finish and "
        "the agent turn cannot drain.\n\n"
        "Use the launched process PID and `wait \"$pid\"` when possible. If "
        "you must discover an unrelated process, use a self-excluding pattern "
        "such as `pgrep -f '[w]orker-name'` and verify it once with `pgrep -af` "
        "before starting the loop."
    )
    return DENY


if __name__ == "__main__":
    raise SystemExit(main())

# EOF
