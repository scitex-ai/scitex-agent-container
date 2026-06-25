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


def validate_flag_argv(argv: list[str]) -> None:
    """Fail loud if a value-taking apptainer flag is missing its value.

    Inspects only the flag region between ``apptainer exec`` and the SIF
    path (first positional that is not consumed as a flag value). Raises
    :class:`ApptainerArgvError` describing the offending flag + the token
    that would be wrongly swallowed.

    A no-op for a well-formed argv.
    """
    flag_region = _flag_region(argv)
    i = 0
    n = len(flag_region)
    while i < n:
        token = flag_region[i]
        if token in VALUE_TAKING_FLAGS:
            nxt = flag_region[i + 1] if i + 1 < n else None
            if nxt is None or nxt.startswith("--"):
                swallowed = nxt if nxt is not None else "<end-of-flags / the SIF>"
                raise ApptainerArgvError(
                    f"apptainer flag {token!r} is missing its required value: "
                    f"the next token is {swallowed!r}. apptainer would swallow "
                    f"that token as {token}'s value and, if it is a flag like "
                    "'--fakeroot', create a stray relative file (e.g. "
                    "'--fakeroot') in the launch cwd / project root. Fix the "
                    "spec.apptainer.raw_args ordering so every value-taking "
                    "flag is immediately followed by its value."
                )
            # Skip the value so a value that legitimately starts with
            # '--' (rare, but possible) isn't itself re-checked.
            i += 2
            continue
        i += 1


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


__all__ = ["ApptainerArgvError", "VALUE_TAKING_FLAGS", "validate_flag_argv"]
