"""Pre-flight lint of ``spec.startup_commands[*].command`` first-tokens.

The follow-up to the SAC-from-SAC PR-1/2/3 series. On 2026-06-03 the
clew launcher (PR clew#70) materialised a child Agent spec where the
agent's CLAUDE-mission prompt text was accidentally placed in
``spec.startup_commands[*].command`` instead of ``spec.startup_prompts``.
The first line of the prompt was ``"You: ..."``, which the SAC
runtime faithfully executed as a shell command:

    /bin/bash: line 1: You: command not found

The agent SIF spawned + restart-looped, the cohort burnt ~30 min of
operator triage, and the wire-shape contract from PR-1
(``kind="bind_unresolvable"``) did not catch it because the bind
preflight only sees ``apptainer.binds``, not ``startup_commands``.

This module adds a sibling pre-flight that runs AFTER bind-translate +
bind-preflight (PR-1+2) and BEFORE the spec is materialised. It
inspects every ``startup_commands[*].command``, tokenises via
:func:`shlex.split`, drops leading ``KEY=VAL`` env assignments, and
applies a deliberately-conservative set of checks to the first
remaining bareword:

  * **Shell-syntax**: :func:`shlex.split` exception → fail-loud with
    ``reason="shell_syntax_malformed"``. Unbalanced quotes / stray
    backslashes would have failed at container exec time anyway —
    we just surface the error at HTTP 400 instead.
  * **Prompt-text smoking gun**: if the first bareword contains a
    colon (``:``), it is almost certainly leaked prompt content.
    The clew incident's ``"You:"`` is the canonical example. Fail
    with ``reason="first_token_looks_like_prompt_text"`` and a
    suggestion that points at ``spec.startup_prompts``.
  * **Host PATH resolution**: :func:`shutil.which` on the bareword.
    Misses on the SAC host PATH → ``reason="first_token_not_on_path"``.
    Allowlisted shell built-ins (``cd``, ``echo``, ``:``, …) pass
    through.

Pass-through (never rejected — out of scope for this preflight):

  * Empty / missing ``command`` (already dropped by
    ``parse_startup_commands``).
  * Absolute paths (``/opt/x``) or relative paths
    (``./x``, ``../x``) — we can't probe SIF-internal paths from
    the host; trust the operator.
  * Variable-prefixed commands (``$HOME/bin/x``) — same reason.

The validator is read-only. Any unexpected spec shape collapses to
"nothing to check", same defensive pattern as the bind preflight.

Wire shape (kept in lockstep with PR-1's ``bind_unresolvable``):

.. code-block:: json

   {
     "error": "startup_commands[*].command first-token validation failed",
     "kind": "spec_invalid",
     "details": {
       "startup_commands": [
         {
           "index": 0,
           "command": "You: please do X",
           "first_token": "You:",
           "reason": "first_token_looks_like_prompt_text"
         }
       ],
       "suggestion": "..."
     }
   }

Callers MUST branch on ``kind`` + ``details.startup_commands[].reason``
(stable enum), not on the prose ``error`` or ``suggestion``.
"""

from __future__ import annotations

import shlex
import shutil
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# Allowlist — POSIX-ish shell built-ins that are not on $PATH but always
# available inside any bash invocation. Kept as a frozenset constant so
# tests can introspect it without importing internals.
# ---------------------------------------------------------------------------

_SHELL_BUILTINS: frozenset[str] = frozenset(
    {
        # control flow & no-op
        ":",
        ".",
        "true",
        "false",
        "exit",
        "return",
        "break",
        "continue",
        "shift",
        # I/O
        "echo",
        "printf",
        "read",
        # vars
        "export",
        "unset",
        "set",
        "declare",
        "local",
        "typeset",
        "readonly",
        # eval & source
        "eval",
        "exec",
        "source",
        # navigation
        "cd",
        "pwd",
        "pushd",
        "popd",
        "dirs",
        # test
        "test",
        "[",
        "[[",
        # job control
        "jobs",
        "bg",
        "fg",
        "wait",
        "kill",
        "trap",
        # misc
        "umask",
        "ulimit",
        "times",
        "type",
        "hash",
        "alias",
        "unalias",
        "getopts",
        "let",
        "command",
        "help",
        "history",
        "fc",
        "logout",
    }
)


# ---------------------------------------------------------------------------
# Result shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StartupCommandIssue:
    """Per-entry preflight result for a single ``startup_commands`` item.

    * ``index`` — position in the original ``startup_commands`` list.
      Stable across reorderings done by upstream parsers so the caller
      can point operators at the exact line.
    * ``command`` — verbatim ``command`` string from the spec entry.
      Echoed so the caller does not have to cross-reference the spec
      it just POST'd.
    * ``first_token`` — the bareword we evaluated (post ``KEY=VAL``
      stripping). Empty string if the spec entry was malformed.
    * ``reason`` — stable enum the caller branches on. See the module
      docstring for the value taxonomy.
    """

    index: int
    command: str
    first_token: str
    reason: str


@dataclass(frozen=True)
class StartupLintResult:
    """Aggregated startup_commands lint outcome.

    * ``ok`` — ``True`` iff every entry passed (or was pass-through:
      empty command, absolute/relative path, variable-prefixed).
    * ``issues`` — every failing entry in input order. The caller can
      render all misses in one shot instead of stop-on-first.
    """

    ok: bool
    issues: tuple[StartupCommandIssue, ...]


# ---------------------------------------------------------------------------
# Spec extraction
# ---------------------------------------------------------------------------


def _iter_startup_commands(spec: dict) -> list[Any]:
    """Best-effort extraction of ``spec.startup_commands`` from a v3 spec.

    Defensive: any unexpected shape collapses to an empty list. Same
    pattern as :func:`_inline_spec_preflight._iter_binds`.
    """
    if not isinstance(spec, dict):
        return []
    spec_body = spec.get("spec")
    if not isinstance(spec_body, dict):
        return []
    cmds = spec_body.get("startup_commands")
    if not isinstance(cmds, list):
        return []
    return cmds


def _extract_command(entry: Any) -> str | None:
    """Pull the ``command`` string out of a single startup_commands entry.

    Accepts the canonical dict form ``{delay: int, command: str}``.
    Returns ``None`` for any unexpected shape; the lint then skips
    the entry (the downstream parser will drop it anyway, so this is
    not a host-visible signal we need to enforce).
    """
    if not isinstance(entry, dict):
        return None
    cmd = entry.get("command")
    if not isinstance(cmd, str):
        return None
    return cmd


def _strip_env_assignments(tokens: list[str]) -> list[str]:
    """Drop leading ``KEY=VAL`` shell env-var assignments.

    ``FOO=bar BAZ=qux mycmd arg`` → ``["mycmd", "arg"]``.

    A token counts as an env-var assignment when it matches
    ``[A-Za-z_][A-Za-z0-9_]*=...`` (POSIX identifier prefix +
    literal ``=`` + anything). Stops at the first non-matching
    token; any later ``=`` characters in arguments are NOT stripped.
    """
    stripped: list[str] = list(tokens)
    while stripped:
        head = stripped[0]
        eq = head.find("=")
        if eq <= 0:
            break
        ident = head[:eq]
        if not ident[0].isalpha() and ident[0] != "_":
            break
        if not all(c.isalnum() or c == "_" for c in ident):
            break
        stripped.pop(0)
    return stripped


# ---------------------------------------------------------------------------
# Public preflight
# ---------------------------------------------------------------------------


def preflight_startup_commands(spec: dict) -> StartupLintResult:
    """Validate every ``spec.startup_commands[*].command`` first token.

    Returns a :class:`StartupLintResult` with ``ok=False`` and an
    ``issues`` list when one or more entries fail. See the module
    docstring for the per-issue ``reason`` taxonomy.

    Specs with no startup_commands (or unparseable shape) collapse to
    ``ok=True`` with empty ``issues`` — there is nothing to reject.
    """
    issues: list[StartupCommandIssue] = []
    for index, entry in enumerate(_iter_startup_commands(spec)):
        cmd = _extract_command(entry)
        # Empty / missing command: pass-through. The downstream
        # ``parse_startup_commands`` drops these, and we do not want
        # to false-positive on a spec that includes an obviously-empty
        # entry the operator will discover at parse time.
        if cmd is None or cmd.strip() == "":
            continue

        try:
            raw_tokens = shlex.split(cmd, comments=False, posix=True)
        except ValueError as exc:
            issues.append(
                StartupCommandIssue(
                    index=index,
                    command=cmd,
                    first_token="",
                    reason="shell_syntax_malformed",
                )
            )
            # Don't try to keep parsing — record + move on. The shlex
            # message itself is not surfaced (callers don't branch on
            # prose); the bare ``shell_syntax_malformed`` enum is the
            # contract. ``exc`` retained for trace context only.
            del exc
            continue

        tokens = _strip_env_assignments(raw_tokens)
        if not tokens:
            # All-env-assignments command (``FOO=bar BAZ=qux``).
            # Technically legal but a no-op; not our preflight's job
            # to flag style. Pass-through.
            continue

        first = tokens[0]

        # Pass-through cases — we cannot resolve these from the host
        # side without container introspection, so trust the operator.
        if first.startswith("/"):
            continue  # absolute path
        if first.startswith("./") or first.startswith("../"):
            continue  # relative path
        if first.startswith("$"):
            continue  # shell variable prefix
        if first.startswith("~"):
            continue  # home expansion (apptainer keeps $HOME in-SIF)

        # High-confidence prompt-text signal. The clew incident's
        # ``"You:"`` is the canonical case: a colon-suffixed bareword
        # at the start of what should be a shell command is almost
        # never a real executable. We surface this BEFORE the PATH
        # check so the suggestion message can be specific.
        if ":" in first:
            issues.append(
                StartupCommandIssue(
                    index=index,
                    command=cmd,
                    first_token=first,
                    reason="first_token_looks_like_prompt_text",
                )
            )
            continue

        # Shell built-ins are not on $PATH but always work. Allowlist.
        if first in _SHELL_BUILTINS:
            continue

        # Final check: host PATH resolution. ``shutil.which`` honours
        # ``$PATH`` from the SAC host process, which is the same
        # environment that runs ``apptainer exec``, so a miss here
        # reliably predicts a miss at container start. False positives
        # are possible (command exists only in the SIF, not on host),
        # which is why the suggestion message includes the
        # absolute-path escape hatch.
        if shutil.which(first) is None:
            issues.append(
                StartupCommandIssue(
                    index=index,
                    command=cmd,
                    first_token=first,
                    reason="first_token_not_on_path",
                )
            )

    return StartupLintResult(ok=not issues, issues=tuple(issues))


# ---------------------------------------------------------------------------
# HTTP failure body
# ---------------------------------------------------------------------------


# Suggestion strings keyed by ``reason``. Centralised so any future
# tweak goes in one place (e.g. when we add a docs URL the operator can
# follow). The first-token field is interpolated at body-build time;
# the ``{first_token}`` placeholder MUST stay verbatim in the table.
_REASON_SUGGESTIONS: dict[str, str] = {
    "first_token_looks_like_prompt_text": (
        "First token '{first_token}' contains ':' and looks like "
        "leaked prompt text. spec.startup_commands runs as shell "
        "commands BEFORE the agent starts; the agent's mission goes "
        "in spec.startup_prompts (which is fed to the SDK as the "
        "first user message). Move this content there."
    ),
    "first_token_not_on_path": (
        "First token '{first_token}' was not found on the SAC host "
        "PATH and is not a recognised shell built-in. If this "
        "command exists only inside the SIF, use an absolute "
        "container path (e.g. /opt/scitex/bin/{first_token}). If it "
        "is prompt text, move it to spec.startup_prompts."
    ),
    "shell_syntax_malformed": (
        "Command failed shell tokenisation (shlex). Check for "
        "unbalanced quotes or stray backslashes; the runtime would "
        "have hit the same error at container start."
    ),
}


def preflight_failure_response_body(result: StartupLintResult) -> dict:
    """Build the HTTP 400 JSON body for a failed startup_commands lint.

    Stable wire shape:

    .. code-block:: json

       {
         "error": "startup_commands[*].command first-token validation failed",
         "kind": "spec_invalid",
         "details": {
           "startup_commands": [
             {
               "index": 0,
               "command": "You: please do X",
               "first_token": "You:",
               "reason": "first_token_looks_like_prompt_text",
               "suggestion": "..."
             }
           ]
         }
       }

    Callers branch on ``kind`` + ``details.startup_commands[].reason``;
    the prose ``error`` + per-entry ``suggestion`` are for humans.

    Note: ``kind`` is the existing ``"spec_invalid"`` enum already in
    use by :mod:`_inline_spec` for malformed-spec rejection (apiVersion
    mismatch, kind mismatch, etc.). Re-using the enum keeps the
    consumer-side branch table small; the per-entry ``reason`` carries
    the sub-shade.
    """
    entries: list[dict] = []
    for issue in result.issues:
        entry: dict = {
            "index": issue.index,
            "command": issue.command,
            "first_token": issue.first_token,
            "reason": issue.reason,
        }
        template = _REASON_SUGGESTIONS.get(issue.reason)
        if template is not None:
            entry["suggestion"] = template.format(first_token=issue.first_token)
        entries.append(entry)
    return {
        "error": "startup_commands[*].command first-token validation failed",
        "kind": "spec_invalid",
        "details": {"startup_commands": entries},
    }


__all__ = [
    "StartupCommandIssue",
    "StartupLintResult",
    "preflight_startup_commands",
    "preflight_failure_response_body",
]
