"""Fail-loud guard against malformed ``apptainer exec`` flag argv.

Root-cause fix for the stray ``--fakeroot`` file (1 NULL byte) that
kept appearing in the PROJECT ROOT.

The bug
-------
``build_run_argv`` assembles the ``apptainer exec`` flag list from a mix
of sac-curated flags (``--containall``, ``--home /home/agent``,
``--fakeroot``, ``--bind …``, ``--overlay …``) and operator-supplied
``spec.apptainer.raw_args``. apptainer's ``--overlay`` / ``--bind`` /
``--env-file`` / ``--home`` / ``--workdir`` each take a **required
value**. When such a flag is emitted with NO value — e.g. an operator
``raw_args: ["--overlay"]`` followed by sac's own ``--fakeroot``, or a
``raw_args: ["--bind"]`` — apptainer's CLI parser swallows the NEXT
token as the missing value. The next token is frequently sac's
``--fakeroot`` (a no-value flag).

apptainer then treats that swallowed ``--fakeroot`` as a **relative
overlay/image path** and creates a stub file at it. Because the TUI
runtime shell-runs the argv with ``cwd = expanded_workdir`` (the PROJECT
ROOT for the maintainer agent whose workdir *is* the repo), the stub
lands as ``<repo>/--fakeroot`` — exactly 1 byte, the placeholder header
apptainer writes for a fresh overlay/output target.

The fix
-------
Validate the flag argv (the slice between ``apptainer exec`` and the
SIF) BEFORE it is ever launched / shell-joined: if a known
value-taking apptainer flag is immediately followed by another
``--option`` (so its value is missing), raise :class:`ApptainerArgvError`
loudly instead of letting apptainer silently create a stray file in the
project root. No band-aid (we don't delete the file after the fact); we
refuse to emit an argv that could create it.

Only the curated + raw flag region is checked. The inner command
(``bash -c "<preflight>\nexec …"`` or the flat relaxed inner argv) sits
AFTER the SIF and is the container's business, not apptainer's flag
parser — so it is excluded.

Naming the culprit
------------------
The first version of this message ended with a single unconditional
sentence: "Fix the spec.apptainer.raw_args ordering." That is the right
remedy for the common case and the WRONG one for two others — sac's own
assembly can in principle emit the bad pair, and the message never said
WHICH agent's spec to open. On 2026-07-23 an operator hit the ``--env
--env`` form on ``scitex-cards-{chat,gui,mobile}`` (an orphan ``--env``
left behind when a ``KEY=VALUE`` line was deleted), read the message,
opened the similarly-named ``scitex-cards`` spec — which was clean — and
concluded the guard was lying. It was not; it just never named the file.

So the message now reports PROVENANCE as a tri-state, computed from the
``raw_args`` the caller passes in:

* ``raw_args`` malformed          → the SPEC is at fault; the offending
  ``raw_args`` index is named.
* ``raw_args`` present and clean  → the spec is EXONERATED in the text
  and the reader is told sac's assembly introduced it.
* ``raw_args`` not supplied       → provenance is stated as UNKNOWN
  rather than guessed.

The offending flag-region index and a window of neighbouring tokens are
always printed, so the reader sees the actual argv instead of a rule.
"""

from __future__ import annotations

# apptainer ``exec`` options that REQUIRE a following value. A subset
# focused on the ones sac itself emits or operators commonly pass via
# raw_args; an unknown value-flag not listed here simply isn't guarded
# (we never want a false positive that blocks a legitimate launch).
VALUE_TAKING_FLAGS = frozenset(
    {
        "--bind",
        "--overlay",
        "--env-file",
        "--home",
        "--workdir",
        "--pwd",
        "--env",
        "--scratch",
        "--mount",
        "--fusemount",
    }
)


class ApptainerArgvError(ValueError):
    """Raised when the apptainer flag argv is malformed.

    Specifically: a value-taking flag is missing its value (the next
    token is another ``--option`` or the flag is last in the region),
    which would make apptainer swallow the following flag as a relative
    path and create a stray file in the launch cwd.
    """


def find_missing_value(tokens: list[str]) -> tuple[int, str, str | None] | None:
    """First ``(index, flag, swallowed)`` where a value-taking flag has no value.

    ``swallowed`` is the token apptainer would consume as the value, or
    ``None`` when the flag is last. Returns ``None`` for a well-formed
    list. Pairs are skipped whole so a value that legitimately starts
    with ``--`` is not itself re-checked.
    """
    i = 0
    n = len(tokens)
    while i < n:
        token = tokens[i]
        if token in VALUE_TAKING_FLAGS:
            nxt = tokens[i + 1] if i + 1 < n else None
            if nxt is None or nxt.startswith("--"):
                return (i, token, nxt)
            i += 2
            continue
        i += 1
    return None


def validate_flag_argv(
    argv: list[str],
    *,
    raw_args: list[str] | None = None,
    agent: str | None = None,
) -> None:
    """Fail loud if a value-taking apptainer flag is missing its value.

    Inspects only the flag region between ``apptainer exec`` and the SIF
    path (first positional that is not consumed as a flag value). Raises
    :class:`ApptainerArgvError` describing the offending flag, the token
    that would be wrongly swallowed, its index + neighbours, and where the
    malformation came from.

    ``raw_args`` is ``spec.apptainer.raw_args``. Supplying it is what lets
    the message ATTRIBUTE the fault instead of always blaming the spec;
    omitting it yields an explicit "provenance unknown" rather than a
    guess. ``agent`` names the agent in the message so the reader opens
    the right spec.

    A no-op for a well-formed argv.
    """
    found = find_missing_value(_flag_region(argv))
    if found is None:
        return
    raise ApptainerArgvError(
        _render_error(_flag_region(argv), found, raw_args=raw_args, agent=agent)
    )


def _render_error(
    region: list[str],
    found: tuple[int, str, str | None],
    *,
    raw_args: list[str] | None,
    agent: str | None,
) -> str:
    index, flag, nxt = found
    swallowed = nxt if nxt is not None else "<end-of-flags / the SIF>"
    window = region[max(0, index - 3) : index + 4]
    who = f" for agent {agent!r}" if agent else ""
    return (
        f"apptainer flag {flag!r} is missing its required value: the next "
        f"token is {swallowed!r}. apptainer would swallow that token as "
        f"{flag}'s value and, if it is a flag like '--fakeroot', create a "
        "stray relative file (e.g. '--fakeroot') in the launch cwd / project "
        f"root.\n  at flag-region index {index}; surrounding tokens: {window}\n"
        f"  {_provenance(raw_args, who)}"
    )


def _provenance(raw_args: list[str] | None, who: str) -> str:
    if raw_args is None:
        return (
            "CAUSE UNKNOWN: spec.apptainer.raw_args was not supplied to this "
            "check, so it cannot say whether the spec or sac's own argv "
            "assembly introduced the pair. Check the spec's raw_args first "
            "for a value-taking flag with no value, then sac's assembly."
        )
    tokens = [str(a) for a in raw_args]
    raw_found = find_missing_value(tokens)
    if raw_found is None:
        return (
            f"CAUSE: NOT the spec — spec.apptainer.raw_args{who} is "
            f"well-formed ({len(tokens)} tokens checked), so editing it "
            "cannot fix this. The pair was introduced while sac ASSEMBLED "
            "the argv; this is a sac bug. Report it with the token window "
            "above."
        )
    raw_index, raw_flag, raw_next = raw_found
    return (
        f"CAUSE: spec.apptainer.raw_args{who} is itself malformed at index "
        f"{raw_index}: {raw_flag!r} is followed by {raw_next!r}. Give that "
        "flag its value, or delete the orphan flag — an orphan is exactly "
        "what deleting the VALUE line of an `--env KEY=VALUE` pair leaves "
        "behind. Run `sac agents find <agent>` for the spec path."
    )


def _flag_region(argv: list[str]) -> list[str]:
    """Return the apptainer flag tokens between ``exec`` and the SIF.

    The SIF is the first positional NOT consumed as a value-taking
    flag's argument. Everything from ``apptainer exec`` up to (excluding)
    that positional is the flag region; the inner command after the SIF
    is excluded.

    Robust to a leading ``["apptainer", "exec", ...]`` prefix; if the
    expected prefix is absent we scan the whole list (the guard then
    simply checks the entire argv, which is still safe).
    """
    start = 0
    if len(argv) >= 2 and argv[0] == "apptainer" and argv[1] == "exec":
        start = 2

    region: list[str] = []
    i = start
    n = len(argv)
    while i < n:
        token = argv[i]
        if token.startswith("--"):
            region.append(token)
            if (
                token in VALUE_TAKING_FLAGS
                and i + 1 < n
                and not argv[i + 1].startswith("--")
            ):
                # Real (non-flag) value — keep it in-region as the pair so
                # the validator sees a satisfied flag. A ``--``-prefixed
                # next token is a MISSING value: leave it for the validator
                # to flag (do NOT consume it here).
                region.append(argv[i + 1])
                i += 2
                continue
            i += 1
            continue
        # First non-flag, non-value token == the SIF path → flags end.
        break
    return region


__all__ = [
    "ApptainerArgvError",
    "VALUE_TAKING_FLAGS",
    "find_missing_value",
    "validate_flag_argv",
]
