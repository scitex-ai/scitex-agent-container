"""WHERE the transcript must land on the target — derived from the TARGET's workdir.

Claude Code does not store a conversation under the agent's name. It stores it
under an encoding of the RESOLVED WORKING DIRECTORY it was launched in::

    $HOME/.claude/projects/<encoded-resolved-cwd>/<session-uuid>.jsonl

So a transcript is found at boot only if it sits under the directory name the
TARGET's runner will compute. Copy it under the SOURCE's directory name and the
file is present, readable, byte-identical — and invisible. The agent starts,
reports healthy, and has no memory of the conversation that moved it. That is the
2026-08-07 failure shape exactly, reproduced by a copy that "worked".

WHY THE TARGET'S WORKDIR MUST BE OBSERVED AND NOT ASSUMED. The two hosts run the
same spec, so the workdir STRING is usually identical — which is precisely what
makes the mistake cheap to commit and expensive to find. It is not reliably the
same path after resolution: Claude Code encodes the RESOLVED cwd, and a workdir
that is a symlink on one host and a real directory on the other resolves to two
different strings from one spec. A relocation onto a host where the repo lives
behind a different mount is the normal case in this fleet, not a corner one.

Resolution therefore cannot be done here. ``Path.resolve()`` in this process
resolves against the SOURCE's filesystem, and the answer would be confidently
wrong in exactly the case that matters. The resolved workdir is an OBSERVATION
the target-side probe supplies, and an unobserved one is UNKNOWN — never a
locally-guessed substitute.

THE ENCODING IS NOT REIMPLEMENTED HERE. ``_runners._session_candidates.
encode_claude_project`` already replicates it (``/`` and ``.`` to ``-``, then
triple-or-more dashes collapsed to ``--``) and the runner reads the store through
that function. A second copy of the rule would be a second thing to keep correct,
and the two would disagree on the first path nobody tested.

WHAT THIS TELLS YOU THAT A STRING COMPARE DOES NOT. :func:`derive_target_dir`
reports whether the target's directory name matches the source's. A MISMATCH IS
NOT AN ERROR — it is the normal, correct outcome when the two hosts resolve the
workdir differently, and it is the whole reason the name is recomputed rather
than copied. It is surfaced because a relocation that silently changes where the
transcript lives should say so out loud, and because a mismatch nobody expected
is the first sign the target's workdir is not what the spec claims.

Pure: no filesystem, no network, no clock. Strings in, a derivation out.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

__all__ = [
    "CODE_DERIVED",
    "CODE_UNKNOWN",
    "TargetTranscriptDir",
    "derive_target_dir",
    "encode_workdir",
]

#: The target-side directory was derived from an observed resolved workdir.
CODE_DERIVED: Final = 200
#: The target's resolved workdir was not observed; nothing can be derived.
CODE_UNKNOWN: Final = 503


def encode_workdir(resolved_workdir: str) -> str:
    """Claude Code's resolved-cwd -> ``projects/`` directory-name encoding.

    Delegates to :func:`.._runners._session_candidates.encode_claude_project`,
    which is the copy the runner itself reads the store through. Re-exported
    under a name that says what it takes (a RESOLVED workdir), because the whole
    class of bug this module guards against starts with someone passing the
    unresolved one.
    """
    from .._runners._session_candidates import encode_claude_project

    return encode_claude_project(resolved_workdir)


@dataclass(frozen=True)
class TargetTranscriptDir:
    """The directory the target's runner will look in, and how it was reached.

    ``path`` is ``None`` when the derivation could not be made, which is the
    same discipline the rest of the relocate machinery uses: a value that was
    not established is absent rather than guessed. There is deliberately no
    ``__bool__`` — ``if target_dir:`` on an underived one would read as "we know
    where to put it".

    ``matches_source`` is three-valued on purpose. ``None`` means the source's
    directory name was not supplied, so the question was never asked; that is
    distinct from an observed ``False``, which is a real difference between the
    two hosts and worth printing.
    """

    path: str | None
    code: int
    reason: str
    encoded: str | None = None
    matches_source: bool | None = None
    hint: str = ""

    def __post_init__(self) -> None:
        if not self.reason:
            raise ValueError("TargetTranscriptDir.reason must be non-empty")
        if self.path is not None and self.code != CODE_DERIVED:
            raise ValueError(
                f"TargetTranscriptDir: a derived path must carry CODE_DERIVED, got {self.code}"
            )
        if self.path is None and not self.hint:
            raise ValueError(
                "TargetTranscriptDir: a failed derivation must say what to measure next"
            )


def derive_target_dir(
    *,
    target_home: str | None,
    target_resolved_workdir: str | None,
    source_dir_name: str | None = None,
) -> TargetTranscriptDir:
    """Compute ``<target_home>/.claude/projects/<encoded>`` from OBSERVED facts.

    ``target_home`` and ``target_resolved_workdir`` are what the target itself
    reported — ``$HOME`` and the workdir after symlink resolution, both read on
    the target. Either being ``None`` (not observed) yields an UNKNOWN, because
    the alternative is to substitute this host's answer for the other host's, and
    a plausible wrong path here produces a healthy agent with no memory.

    ``source_dir_name`` is the bare directory name the transcript currently lives
    under on the source (e.g. ``-home-ywatanabe-proj-lead``). Supplying it turns
    on the comparison; omitting it leaves ``matches_source`` at ``None`` rather
    than defaulting the answer to "same", which would be a claim nobody made.
    """
    if not target_home:
        return TargetTranscriptDir(
            path=None,
            code=CODE_UNKNOWN,
            reason="the target's $HOME was not observed",
            hint=(
                "probe the target for its $HOME before transporting; the transcript "
                "store hangs off it, and this host's $HOME is not evidence about "
                "the other host's"
            ),
        )
    if not target_resolved_workdir:
        return TargetTranscriptDir(
            path=None,
            code=CODE_UNKNOWN,
            reason="the target's RESOLVED workdir was not observed",
            hint=(
                "probe the target for the workdir after symlink resolution "
                "(readlink -f / realpath). Resolving it here would resolve against "
                "the SOURCE's filesystem and name a directory the target's runner "
                "will never read"
            ),
        )

    encoded = encode_workdir(target_resolved_workdir)
    path = f"{target_home.rstrip('/')}/.claude/projects/{encoded}"
    matches = None if source_dir_name is None else (encoded == source_dir_name)
    if matches is False:
        reason = (
            f"the target encodes its workdir as {encoded!r}, the source stores the "
            f"transcript under {source_dir_name!r} — the two hosts resolve this "
            "workdir differently, so the name is recomputed rather than copied"
        )
    else:
        reason = f"the target's runner will read {path}"
    return TargetTranscriptDir(
        path=path,
        code=CODE_DERIVED,
        reason=reason,
        encoded=encoded,
        matches_source=matches,
    )
