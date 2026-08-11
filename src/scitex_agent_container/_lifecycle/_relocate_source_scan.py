"""Count what is un-saved in the repos on the host being LEFT.

Supplies :class:`.._relocate_preflight.SourceFacts` — the one preflight check
that is about the source rather than the target. A relocation carries the spec
and (when the transport runs) the transcript; a half-finished branch, a stash and
an unpushed commit stay exactly where they are, on a machine the agent will no
longer be looking at.

WHY THE COUNTS ARE TAKEN LOCALLY AND NOT OVER ssh. The source is where the
coordinator runs. Routing this through the same batched remote probe would mean
measuring the machine the command is on by going out to the network and back —
more ways to fail for a question that needs none of them.

A FAILED SCAN IS ``None``, NEVER ZERO. This is the same rule as
:mod:`_relocate_probe_adapter` and it is even easier to break here, because
``git status --porcelain`` failing and a clean tree BOTH produce no output
lines. Counting lines on a failed run yields 0, "clean", and a relocation that
strands the work it was checking for. So every command's exit code is inspected
and anything other than a clean success leaves the count ``None``, which the
check reports as UNKNOWN and refuses on.

UNPUSHED IS COUNTED AGAINST THE UPSTREAM, AND NO UPSTREAM IS NOT ZERO. A branch
with no upstream has nowhere to have been pushed TO, so every commit on it is
unreachable from any other machine. Reporting 0 there would be the most
expensive kind of wrong — it is precisely the branch a relocation strands. It is
reported as un-measured, with the reason, rather than as clean.

The runner is injected, so the decision logic is testable against captured git
output with no repo and no subprocess.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Callable, Sequence

from ._relocate_origin import RepoWork
from ._relocate_preflight import SourceFacts

__all__ = [
    "CommandResult",
    "run_git",
    "scan_repo",
    "scan_source",
]


@dataclass(frozen=True)
class CommandResult:
    """One command's output and how it exited. ``exit_code`` is never assumed."""

    stdout: str
    stderr: str
    exit_code: int

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


def run_git(argv: Sequence[str], *, timeout_s: float = 20.0) -> CommandResult:
    """Run a git command, returning its result rather than raising on failure.

    A non-zero exit is DATA here — it is what tells :func:`scan_repo` to leave a
    count unmeasured instead of reading an empty stdout as "clean". Only a
    failure to run git at all becomes an exceptional exit code (127), so the
    caller still sees "this was not measured" rather than a crash mid-preflight.
    """
    try:
        proc = subprocess.run(  # noqa: S603
            list(argv),
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except Exception as exc:  # stx-allow: fallback (reason: a git that could not run must leave the count UNMEASURED, never 0; the reason is preserved on stderr)
        return CommandResult(
            stdout="", stderr=f"{type(exc).__name__}: {exc}", exit_code=127
        )
    return CommandResult(
        stdout=proc.stdout or "", stderr=proc.stderr or "", exit_code=proc.returncode
    )


def scan_repo(
    path: str,
    *,
    runner: Callable[[Sequence[str]], CommandResult] = run_git,
) -> RepoWork:
    """Measure one repo. Any command that did not succeed leaves its count ``None``.

    Three questions, each independent so a failure costs only its own answer:
    the branch name, the working-tree status, and the commits ahead of the
    upstream. A repo whose branch could not be read is still worth counting
    files in.
    """
    if not path:
        raise ValueError("scan_repo needs a repo path")

    branch_run = runner(["git", "-C", path, "rev-parse", "--abbrev-ref", "HEAD"])
    branch = branch_run.stdout.strip() if branch_run.ok else ""

    status_run = runner(["git", "-C", path, "status", "--porcelain"])
    uncommitted: int | None
    if status_run.ok:
        uncommitted = len([ln for ln in status_run.stdout.splitlines() if ln.strip()])
    else:
        # An empty stdout from a FAILED status is indistinguishable from a clean
        # tree. Refusing to count it is the whole reason this branch exists.
        uncommitted = None

    ahead_run = runner(["git", "-C", path, "rev-list", "--count", "@{u}..HEAD"])
    unpushed: int | None
    if ahead_run.ok:
        raw = ahead_run.stdout.strip()
        unpushed = int(raw) if raw.isdigit() else None
    else:
        # Usually "no upstream configured". That is NOT zero unpushed commits —
        # it is a branch that exists on this machine only, which is exactly the
        # work a relocation would strand.
        unpushed = None

    return RepoWork(
        path=path, branch=branch, uncommitted=uncommitted, unpushed=unpushed
    )


def scan_source(
    repo_paths: Sequence[str],
    *,
    runner: Callable[[Sequence[str]], CommandResult] = run_git,
) -> SourceFacts:
    """Scan every repo the agent works in, into the facts preflight consumes.

    An empty ``repo_paths`` yields an empty tuple, which is an OBSERVED "no
    repos to strand" and passes the check. That is different from passing no
    facts at all (``SourceFacts()``), which is "nobody looked" and refuses —
    and the difference is the caller's to state deliberately, which is why this
    function does not try to discover repos on its own.
    """
    return SourceFacts(repos=tuple(scan_repo(p, runner=runner) for p in repo_paths))
