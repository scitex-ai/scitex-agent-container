"""What the verdict actually SAYS, and the limits it admits to.

Companion to :mod:`ci_card_rail` (orchestration), :mod:`ci_rail_cards`
(the card contract) and :mod:`ci_rail_listen` (delivery). This module
owns one thing: turning a conclusion into the sentence a woken human
reads.

That deserves its own module because the message is where this rail's
sharpest defect lived. It once ended a green verdict with "Self-merge if
you own it" -- advice drawn from ONE workflow's result, because `needs:`
cannot reach across workflow files and lint, quality, import-smoke and
the runner guard are all invisible from here. That is a verdict computed
over the checks in view, presented as a verdict over the gate: the exact
shape of queued-reads-as-green, reproduced inside the fix for it.

The rule this module exists to hold: **say what was observed, say what
was not, and decline to draw the conclusion the reader wants.**
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["sibling_workflow_names", "verdict_text"]


def sibling_workflow_names(this_workflow: str, workflows_dir: str | None = None) -> list[str]:
    """PR-triggering workflows in the repo EXCEPT the one this rail sees.

    DERIVED, NOT HARDCODED, and the difference has a failure mode. A list
    written by hand is a snapshot of today's workflow set: add a workflow
    next month and it silently drops out of the disclaimer, so the
    message quietly resumes overclaiming while still LOOKING careful. A
    disclaimer that decays is worse than none, because it keeps buying
    trust it no longer earns.

    Filtered to workflows a PULL REQUEST can trigger. Listing every file
    would name the release, nightly and autobump workflows -- accurate
    about the repo, misleading about this commit, since none of them will
    ever report on a PR. A disclaimer that overstates what is pending is
    its own small dishonesty.

    Scans for a top-level ``name:`` instead of parsing YAML: this runs
    under ``uv run --with scitex-cards`` and must not assume a YAML
    library is present. Returns ``[]`` when the directory is unreadable,
    which the caller renders as an UNNAMED disclaimer -- never a stale
    list, and never silence.
    """
    directory = Path(workflows_dir) if workflows_dir else Path(".github/workflows")
    if not directory.is_dir():
        return []
    names: list[str] = []
    for path in sorted(directory.glob("*.y*ml")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        # Split at `jobs:` so a `pull_request` appearing in a step or a
        # comment cannot be mistaken for a trigger declaration.
        head = text.split("\njobs:", 1)[0]
        if "pull_request" not in head:
            continue
        for line in text.splitlines():
            if line.startswith("name:"):
                name = line.split(":", 1)[1].strip().strip("\"'")
                if name and name != this_workflow:
                    names.append(name)
                break
    return sorted(set(names))


def _repo_basename(repo: str) -> str:
    return repo.rstrip("/").split("/")[-1].strip()


def verdict_text(
    *,
    repo: str,
    branch: str,
    sha: str,
    conclusion: str,
    leg: str,
    run_url: str,
    card_id: str,
    detail: str = "",
    unobserved: list[str] | None = None,
    routing: str = "",
) -> str:
    """The message a woken human reads.

    ``detail`` names what broke, and is not decoration: a notification
    that arrives saying only "Red" has solved half the problem, and half
    is measurable here -- seven consecutive red nights on this fleet
    reached nobody. Arriving is not the fix; arriving with the failing
    test named is.

    ``routing`` says WHY THIS REACHED YOU, and is empty exactly when the
    answer is "because you pushed". It is non-empty when the rail could
    not identify the pusher and addressed the verdict by repo-owner
    fallback instead -- a routing guess, not an attribution. Saying so IN
    THE MESSAGE is the half of the fix the card cannot do: a reader is
    told the verdict and its provenance in the same breath, rather than
    receiving an unqualified "your CI failed" for somebody else's push.
    Same rule as ``unobserved`` below -- state what was observed, state
    what was not, and decline to draw the conclusion the reader wants.

    Deliberately NO attribution of CAUSE. The rail reports the verdict and
    quotes the log; it never says WHY it broke, because it cannot know,
    and a rail that guesses causes teaches people to distrust the ones it
    gets right.
    """
    head = f"CI {conclusion.upper()} — {_repo_basename(repo)} `{branch}` ({sha[:8]})"
    if leg:
        head += f" [{leg}]"

    if conclusion == "success":
        tail = "The pytest gate is green for this commit. It is NOT a merge signal: "
        if unobserved:
            tail += (
                f"this rail cannot see {len(unobserved)} other PR workflows — "
                f"{', '.join(unobserved)}."
            )
        else:
            tail += "other workflows report separately and this rail cannot see them."
    else:
        tail = "Red. Fix and push; this rail re-fires on your next push."

    parts = [f"{head}.", tail]
    if detail.strip():
        parts.append(detail.strip())
    # ABOVE the run link, not appended after it. A reader who stops at the
    # first link must already have been told this verdict may not be theirs.
    if routing.strip():
        parts.append(routing.strip())
    parts.append(f"Run: {run_url}")
    parts.append(f"Card: {card_id}")
    return "\n".join(parts)


# EOF
