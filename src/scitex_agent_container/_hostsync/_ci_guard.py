"""Refuse to mutate a peer whose checkout CI is currently using.

This guard is not hypothetical. On Spartan the sac checkout DOUBLES AS
THE SELF-HOSTED RUNNER'S AUDIT WORKSPACE. On 2026-07-14 an agent left a
branch checked out there; a ``develop`` CI run then audited THAT BRANCH
while reporting itself as a test of develop. It broke the release for
~40 minutes and nothing announced it. A ``git merge`` landing under a
runner mid-job is the same class of corruption, arriving faster.

Two signals, because one is not enough
--------------------------------------
* ``busy`` on each runner — is a job executing RIGHT NOW.
* queued / in-progress RUNS — is a job about to be handed to an idle
  runner. A runner that is idle at the instant we look can be running a
  job three seconds later, which is exactly long enough for a
  fast-forward to land underneath it. "Not busy now" is not "will not be
  busy"; checking only the flag would be a race with a corrupt CI run as
  the prize.

UNKNOWN is not IDLE
-------------------
If ``gh`` is missing, unauthenticated, offline, or answers with
something we cannot parse, the verdict is ``UNKNOWN`` and the sync
REFUSES. sac has repeatedly shipped the bug where a failed probe
collapses into a reassuring pole — a timeout read as DOWN, a missing
pidfile read as "preempted" — and each time the false verdict was more
destructive than the fault it hid. Absence of evidence is not evidence.
"""

from __future__ import annotations

import enum
import json
import subprocess
from dataclasses import dataclass

__all__ = ["CiState", "CiVerdict", "DEFAULT_REPO", "check_ci_idle"]

# sac's own repo. The verb reconciles sac's own checkout, so the slug is
# a property of THIS package rather than of any host's config — there is
# no second source of truth to keep in step. ``--repo`` overrides it for
# a fork or a rename.
DEFAULT_REPO = "scitex-ai/scitex-agent-container"

_GH_TIMEOUT_S = 30


class CiState(enum.Enum):
    """Three states, never two. Only ``IDLE`` and ``NOT_APPLICABLE`` pass."""

    IDLE = "idle"
    BUSY = "busy"
    #: No runner on this peer serves this repo — the guard does not apply.
    #: Reported explicitly rather than silently skipped.
    NOT_APPLICABLE = "not-applicable"
    #: We could not find out. Refuses, and says how to find out.
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class CiVerdict:
    """Whether the peer's CI is quiet enough to touch its checkout."""

    state: CiState
    detail: str = ""
    busy_runners: tuple[str, ...] = ()
    active_runs: int = 0

    @property
    def may_mutate(self) -> bool:
        """Only a POSITIVE observation of quiet authorises a mutation."""
        return self.state in (CiState.IDLE, CiState.NOT_APPLICABLE)

    def to_dict(self) -> dict:
        return {
            "state": self.state.value,
            "detail": self.detail,
            "busy_runners": list(self.busy_runners),
            "active_runs": self.active_runs,
        }


def _gh_json(args: list[str], *, runner) -> object | None:
    """Run ``gh <args>`` and parse stdout as JSON; ``None`` on ANY problem.

    ``None`` means UNKNOWN — deliberately indistinguishable from every
    other way of not knowing, and never conflated with an empty result.
    """
    try:
        proc = runner(
            ["gh", *args],
            capture_output=True,
            text=True,
            timeout=_GH_TIMEOUT_S,
            check=False,
        )
    except (
        FileNotFoundError,
        OSError,
        subprocess.SubprocessError,
    ):  # stx-allow: fallback (reason: gh missing / spawn error / timeout → UNKNOWN, which REFUSES; never silently "idle")
        return None
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout or "")
    except (
        ValueError
    ):  # stx-allow: fallback (reason: unparseable gh output → UNKNOWN, which REFUSES)
        return None


def _runner_serves_peer(runner_row: dict, peer: str) -> bool:
    """True when ``runner_row`` plausibly lives on ``peer``.

    Matched on the runner's NAME and its LABELS, tokenised on ``-``, so
    peer ``spartan`` matches both ``spartan-cpu-scitex-agent-container-01``
    (by name) and ``scitex-agent-container-02`` carrying the
    ``spartan-cpu`` label (by label). Peers with no matching runner get
    ``NOT_APPLICABLE`` — the guard is reported as inapplicable, not
    silently skipped.
    """
    needle = peer.strip().lower()
    if not needle:
        return False
    tokens: set[str] = set()
    name = str(runner_row.get("name") or "").lower()
    tokens.update(name.split("-"))
    for label in runner_row.get("labels") or []:
        if isinstance(label, dict):
            tokens.update(str(label.get("name") or "").lower().split("-"))
    return needle in tokens


def check_ci_idle(
    peer: str,
    *,
    repo: str = DEFAULT_REPO,
    runner=subprocess.run,
) -> CiVerdict:
    """Is it safe to mutate ``peer``'s checkout without corrupting a CI run?

    Args:
        peer: the peer whose checkout we intend to fast-forward.
        repo: ``owner/name`` whose self-hosted runners to inspect.
        runner: injectable ``subprocess.run``-shaped callable (real; the
            tests install a real ``gh`` shim on PATH rather than mocking).

    Returns:
        A :class:`CiVerdict`. Never raises. Every non-answer is
        ``UNKNOWN``, which refuses — see the module docstring.
    """
    runners = _gh_json(["api", f"/repos/{repo}/actions/runners"], runner=runner)
    if not isinstance(runners, dict) or not isinstance(runners.get("runners"), list):
        return CiVerdict(
            state=CiState.UNKNOWN,
            detail=(
                f"could not read {repo}'s self-hosted runners via gh. That is "
                "UNKNOWN, not idle, so the sync refuses rather than risk landing "
                "a merge under a live CI job. Check:  gh auth status  and  "
                f"gh api /repos/{repo}/actions/runners"
            ),
        )

    mine = [
        r
        for r in runners["runners"]
        if isinstance(r, dict) and _runner_serves_peer(r, peer)
    ]
    if not mine:
        return CiVerdict(
            state=CiState.NOT_APPLICABLE,
            detail=f"no {repo} self-hosted runner is registered on '{peer}'",
        )

    busy = [str(r.get("name") or "?") for r in mine if r.get("busy") is True]
    if busy:
        return CiVerdict(
            state=CiState.BUSY,
            detail=(
                f"{len(busy)} runner(s) on '{peer}' are executing a job right now. "
                "Syncing would move the checkout under a running audit — exactly "
                "the corruption that broke develop's CI on 2026-07-14."
            ),
            busy_runners=tuple(busy),
        )

    # An idle runner is one queued job away from being busy. Check for
    # work that is already scheduled, not just work already started.
    active = 0
    for status in ("in_progress", "queued"):
        payload = _gh_json(
            ["api", f"/repos/{repo}/actions/runs?status={status}&per_page=1"],
            runner=runner,
        )
        if not isinstance(payload, dict) or not isinstance(
            payload.get("total_count"), int
        ):
            return CiVerdict(
                state=CiState.UNKNOWN,
                detail=(
                    f"could not read {repo}'s {status} workflow runs via gh. "
                    "UNKNOWN is not idle — refusing."
                ),
            )
        active += payload["total_count"]

    if active:
        return CiVerdict(
            state=CiState.BUSY,
            detail=(
                f"{active} workflow run(s) are queued or in progress for {repo}. "
                f"'{peer}'s runners are idle only for the moment — a queued job "
                "can start mid-sync. Waiting is the whole point of this guard."
            ),
            active_runs=active,
        )

    return CiVerdict(
        state=CiState.IDLE,
        detail=(
            f"{len(mine)} runner(s) on '{peer}' idle; no queued or in-progress "
            f"runs for {repo}"
        ),
    )
