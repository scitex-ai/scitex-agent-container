"""Which RUNNER executed each job — the fact a status word hides.

A green check does not tell you where the work ran. Measured 2026-08-11:
scitex-dev PR #572 showed ``import-smoke  pass  1m9s`` while the jobs API
reported ``runner_name=spartan-cpu-org-01``. The run had been queued BEFORE the
repo's ``CI_RUNS_ON`` was repointed, so it executed on a host nobody expected —
and every human-visible signal said green. Re-reading ``CI_RUNS_ON`` would also
have said green, because the variable HAD been changed. Only the job's actual
``runner_name`` disagreed.

So this module reads ``runner_name`` rather than status, for EVERY job of a run
(not just failing ones — the failing ones were never the risk).

WHY THERE IS NO HARDCODED HOST POLICY HERE, and why there was:
the first version of this file shipped ``BANNED_RUNNER_SUBSTRINGS = ("spartan",)``
because a host had been banned from CI outright. That ban was repealed the very
next day, which would have left a tool exiting non-zero on sanctioned runs — a
known-false gate, the exact failure mode it exists to catch. A rule that can be
repealed overnight does not belong baked into a library.

What survives a policy reversal is the QUESTION, not the answer: *where did this
work actually run?* So the default is to report, and any gate is stated by the
caller at the call site:

  * ``--deny SUBSTR``   fail if any job ran on a matching runner
  * ``--expect SUBSTR`` fail if any job ran on a runner that does NOT match

``--expect`` is usually the better one: it asserts what you intended rather than
enumerating what you fear, so it still catches a host nobody thought to ban.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from typing import Optional, Sequence

from ._ci_why import _RUN_ID_IN_LINK, CIWhyError, GhRunner, _repo_args, run_gh

__all__ = [
    "JobRunner",
    "RunRunners",
    "audit",
    "matches_any",
    "render_text",
    "resolve_all_run_ids",
    "run_job_runners",
]


def matches_any(runner_name: Optional[str], substrings: Sequence[str]) -> bool:
    """True when ``runner_name`` contains any of ``substrings`` (case-insensitive).

    An UNASSIGNED runner (a queued job) matches nothing — absence of a host is
    not evidence about which host, in either direction.
    """
    if not runner_name:
        return False
    name = runner_name.lower()
    return any(s.lower() in name for s in substrings if s)


@dataclass
class JobRunner:
    """One job, and the machine that actually ran it."""

    job: str
    status: str
    conclusion: Optional[str]
    runner_name: Optional[str]
    runner_group: Optional[str]
    labels: list[str] = field(default_factory=list)
    url: Optional[str] = None

    @property
    def where(self) -> str:
        """Human-readable host, honest when the job never got one."""
        return self.runner_name or "<unassigned>"

    def to_dict(self) -> dict:
        return {
            "job": self.job,
            "status": self.status,
            "conclusion": self.conclusion,
            "runner_name": self.runner_name,
            "runner_group": self.runner_group,
            "labels": self.labels,
            "url": self.url,
        }


@dataclass
class RunRunners:
    """Every job of one run, with the runner each landed on."""

    run_id: str
    jobs: list[JobRunner] = field(default_factory=list)

    def denied(self, substrings: Sequence[str]) -> list[JobRunner]:
        """Jobs that ran somewhere the caller said they must not."""
        return [j for j in self.jobs if matches_any(j.runner_name, substrings)]

    def unexpected(self, substrings: Sequence[str]) -> list[JobRunner]:
        """Jobs that ran somewhere OTHER than where the caller expected.

        A job with no runner yet is not a violation — it has not run anywhere.
        """
        if not substrings:
            return []
        return [
            j
            for j in self.jobs
            if j.runner_name and not matches_any(j.runner_name, substrings)
        ]

    @property
    def tally(self) -> dict[str, int]:
        """How many jobs landed on each runner — the distribution, not a verdict."""
        return dict(Counter(j.where for j in self.jobs))

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "jobs": [j.to_dict() for j in self.jobs],
            "tally": self.tally,
        }


def _jobs_path(run_id: str, repo: Optional[str]) -> str:
    # gh substitutes {owner}/{repo} from the cwd's repo when none is given.
    owner_repo = repo if repo else "{owner}/{repo}"
    return f"repos/{owner_repo}/actions/runs/{run_id}/jobs"


def run_job_runners(
    run_id: str, *, run_gh: GhRunner = run_gh, repo: Optional[str] = None
) -> RunRunners:
    """Read every job of ``run_id`` and the runner it executed on."""
    raw = run_gh(["api", "--paginate", _jobs_path(run_id, repo)])
    try:
        payload = json.loads(raw or "{}")
    except ValueError as exc:
        raise CIWhyError(f"could not parse jobs JSON for run {run_id}") from exc

    jobs_raw = payload.get("jobs") if isinstance(payload, dict) else None
    if jobs_raw is None:
        raise CIWhyError(f"no jobs found for run {run_id}")

    jobs = [
        JobRunner(
            job=str(j.get("name", "?")),
            status=str(j.get("status", "?")),
            conclusion=j.get("conclusion"),
            runner_name=j.get("runner_name"),
            runner_group=j.get("runner_group_name"),
            labels=list(j.get("labels") or []),
            url=j.get("html_url"),
        )
        for j in jobs_raw
    ]
    return RunRunners(run_id=str(run_id), jobs=jobs)


def _pr_all_run_ids(pr: str, *, run_gh: GhRunner, repo: Optional[str]) -> list[str]:
    """EVERY run behind a PR's checks — passing ones included.

    Deliberately not ``_ci_why._pr_failing_run_ids``: a job that ran somewhere
    unexpected is most likely to be hiding behind a check that PASSED.
    """
    raw = run_gh(["pr", "checks", pr, *_repo_args(repo), "--json", "name,state,link"])
    try:
        checks = json.loads(raw or "[]")
    except ValueError as exc:
        raise CIWhyError(f"could not parse gh pr checks JSON for PR {pr}") from exc
    run_ids: list[str] = []
    for c in checks or []:
        m = _RUN_ID_IN_LINK.search(str(c.get("link", "")))
        if m and m.group(1) not in run_ids:
            run_ids.append(m.group(1))
    if not run_ids:
        raise CIWhyError(f"no workflow runs found behind PR {pr}")
    return run_ids


def resolve_all_run_ids(
    target: Optional[str], *, run_gh: GhRunner = run_gh, repo: Optional[str] = None
) -> list[str]:
    """PR number / run id / branch -> ALL relevant run ids (not just red)."""
    from ._ci_why import _RUN_ID_MIN, _branch_latest_run_id, _current_branch

    target = (target or "").strip()
    if not target:
        return _branch_latest_run_id(_current_branch(), run_gh=run_gh, repo=repo)
    if target.isdigit():
        if int(target) >= _RUN_ID_MIN:
            return [target]
        return _pr_all_run_ids(target, run_gh=run_gh, repo=repo)
    return _branch_latest_run_id(target, run_gh=run_gh, repo=repo)


def audit(
    target: Optional[str], *, run_gh: GhRunner = run_gh, repo: Optional[str] = None
) -> list[RunRunners]:
    """Resolve ``target`` and report the runner behind each of its jobs."""
    ids = resolve_all_run_ids(target, run_gh=run_gh, repo=repo)
    return [run_job_runners(rid, run_gh=run_gh, repo=repo) for rid in ids]


def render_text(
    run: RunRunners,
    *,
    deny: Sequence[str] = (),
    expect: Sequence[str] = (),
) -> str:
    """One line per job, then the per-runner tally.

    Marks are applied only where the CALLER stated a policy; with no policy this
    is a plain report of where the work ran.
    """
    flagged = {id(j) for j in run.denied(deny)} | {id(j) for j in run.unexpected(expect)}
    lines = []
    for j in run.jobs:
        mark = "FLAG " if id(j) in flagged else "     "
        grp = f" [{j.runner_group}]" if j.runner_group else ""
        concl = j.conclusion or j.status
        lines.append(f"  {mark}{concl:<12} {j.where}{grp}  {j.job}")
    if run.jobs:
        lines.append("  ---")
        for host, n in sorted(run.tally.items(), key=lambda kv: (-kv[1], kv[0])):
            lines.append(f"  {n:>3} job(s) on {host}")
    return "\n".join(lines)
