"""The PR that is BLOCKED with zero failures — because a required check never ran.

THE STATE THIS EXISTS TO CATCH, and why nothing else catches it:

A branch's required status checks are matched BY NAME. A check that never
STARTED is not "pending" in the API — it is ABSENT from ``statusCheckRollup``
entirely. So a PR whose required ``pytest-matrix-on-ubuntu-py3.13`` leg never
got a runner reports:

    mergeable=MERGEABLE  mergeStateStatus=BLOCKED  pass=7  fail=0

Seven green, nothing red, and unmergeable — indefinitely, and silently. Every
human-visible summary agrees it is fine. Counting the rollup says fine. Only
comparing the REQUIRED-CONTEXTS LIST BY NAME against the checks that actually
reported disagrees, because the evidence is a name that is *missing*.

MEASURED on this repo, 2026-08-12 08:49Z: #1014, #1017 and #1005 sat in exactly
this state at once while all four ``scitex-org-cpu`` runners were saturated —
14 matrix legs queued behind them. #1014 had been waiting 14 minutes. Nothing
alarmed, because there was nothing red to alarm on. The auto-merge monitor was
correct and useless here: it waits for CLEAN, and CLEAN is precisely what never
arrives.

WHY A COUNT CANNOT SUBSTITUTE FOR THE NAME LIST. "Zero pending" and "never
enqueued" produce the identical rollup. The absent context is the whole signal,
so the required list must come from BRANCH PROTECTION and be diffed by name —
deriving "what should have run" from "what did run" can only ever return green.

THE TWO SHAPES ARE DELIBERATELY DISTINCT:

  * ``never_started``  the name is absent from the rollup. INVISIBLE — this is
                       the pathology. It can persist forever with no signal.
  * ``pending``        the name is present as queued/in_progress. Visible, and
                       usually benign: the work is on its way. Reported, but it
                       is not on its own an alarm.

A required context that FAILED is not this bug at all — that PR is red and
already loud, so a run with failures is never flagged silent.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional

from ._ci_why import CIWhyError, GhRunner, _repo_args, run_gh

__all__ = [
    "PRGate",
    "RequiredContext",
    "audit_blocked",
    "render_text",
    "required_contexts",
]

# Conclusions GitHub itself treats as satisfying a required check.
_PASSING = {"SUCCESS", "NEUTRAL", "SKIPPED"}
# Statuses meaning "created, but no verdict yet".
_UNFINISHED = {"QUEUED", "IN_PROGRESS", "WAITING", "PENDING", "REQUESTED"}

NEVER_STARTED = "never_started"
PENDING = "pending"
PASSED = "passed"
FAILED = "failed"


@dataclass
class RequiredContext:
    """One required status check, and whether it ever reported at all."""

    name: str
    state: str
    detail: str = ""

    @property
    def is_silent(self) -> bool:
        """True when this context never even enqueued — the invisible case."""
        return self.state == NEVER_STARTED

    def to_dict(self) -> dict:
        return {"name": self.name, "state": self.state, "detail": self.detail}


@dataclass
class PRGate:
    """An open PR judged against its base branch's REQUIRED contexts, by name."""

    number: int
    title: str
    base: str
    merge_state_status: str
    mergeable: str
    url: str = ""
    is_draft: bool = False
    required: list[RequiredContext] = field(default_factory=list)
    reported: int = 0

    def _in(self, state: str) -> list[RequiredContext]:
        return [c for c in self.required if c.state == state]

    @property
    def never_started(self) -> list[RequiredContext]:
        return self._in(NEVER_STARTED)

    @property
    def pending(self) -> list[RequiredContext]:
        return self._in(PENDING)

    @property
    def failed(self) -> list[RequiredContext]:
        return self._in(FAILED)

    @property
    def silently_blocked(self) -> bool:
        """Nothing red, yet a required context has not even been created.

        TWO EXCLUSIONS, BOTH MEASURED AGAINST THE LIVE BOARD 2026-08-12, because
        an alarm that cries wolf is worse than no alarm at all:

        * A DRAFT is *meant* to be unmergeable. Flagging one is noise.
        * A CONFLICTING PR's checks are absent BECAUSE it cannot be merged — the
          first live run flagged #883 and #942, both ``CONFLICTING/DIRTY``, whose
          matrix legs had never been enqueued for exactly that reason. That
          blocker is already named in ``mergeable`` and needs no second alarm;
          the state worth catching is the one nothing else reports.

        So the shape is narrow on purpose: MERGEABLE, nothing red, and a
        required context that was never created.

        A CONSEQUENCE WORTH KNOWING: GitHub computes ``mergeable`` lazily and
        serves ``UNKNOWN`` while it does, so a PR is not judged on that poll.
        That is the right direction to be wrong in for an ALARM — it delays a
        true positive by one poll rather than inventing one — but it does mean
        a single clean run is not proof, and this is a thing to POLL, not to
        read once. (The opposite convention holds in ``_ci_why``, where UNKNOWN
        must never read as green: there the question is whether something
        FAILED, and here it is whether to wake someone.)
        """
        if self.is_draft:
            return False
        if self.mergeable.upper() != "MERGEABLE":
            return False
        return bool(self.never_started) and not self.failed

    def to_dict(self) -> dict:
        return {
            "number": self.number,
            "title": self.title,
            "base": self.base,
            "mergeStateStatus": self.merge_state_status,
            "mergeable": self.mergeable,
            "url": self.url,
            "isDraft": self.is_draft,
            "required": [c.to_dict() for c in self.required],
            "reported_checks": self.reported,
            "never_started": [c.name for c in self.never_started],
            "pending": [c.name for c in self.pending],
            "failed": [c.name for c in self.failed],
            "silently_blocked": self.silently_blocked,
        }


def required_contexts(
    branch: str, *, run_gh: GhRunner = run_gh, repo: Optional[str] = None
) -> list[str]:
    """The required status-check NAMES for ``branch``, from branch protection.

    This is the half of the comparison that cannot be derived from the PR: it
    says what SHOULD report. An unprotected branch requires nothing, which is a
    real answer (no possible silent block), not an error.
    """
    owner_repo = repo if repo else "{owner}/{repo}"
    path = f"repos/{owner_repo}/branches/{branch}/protection/required_status_checks"
    try:
        raw = run_gh(["api", path])
    except CIWhyError:
        # No protection, or no admin rights to read it. Either way this tool
        # cannot judge the branch — and must not claim it is clean.
        return []
    try:
        payload = json.loads(raw or "{}")
    except ValueError as exc:
        raise CIWhyError(
            f"could not parse branch-protection JSON for {branch}"
        ) from exc

    names: list[str] = []
    for c in payload.get("contexts") or []:
        if c and c not in names:
            names.append(str(c))
    # The newer `checks[]` form carries the same names plus an app_id.
    for c in payload.get("checks") or []:
        ctx = (c or {}).get("context")
        if ctx and ctx not in names:
            names.append(str(ctx))
    return names


def _rollup_states(rollup: list) -> dict[str, tuple[str, str]]:
    """Map reported check NAME -> (state, detail).

    Both rollup shapes are flattened: CheckRun carries ``name``/``status``/
    ``conclusion``; the legacy StatusContext carries ``context``/``state``.
    """
    out: dict[str, tuple[str, str]] = {}
    for entry in rollup or []:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name") or entry.get("context")
        if not name:
            continue
        status = str(entry.get("status") or "").upper()
        concl = str(entry.get("conclusion") or entry.get("state") or "").upper()

        if status and status in _UNFINISHED and not concl:
            state, detail = PENDING, status.lower()
        elif concl in _PASSING:
            state, detail = PASSED, concl.lower()
        elif concl:
            state, detail = FAILED, concl.lower()
        else:
            state, detail = PENDING, (status or "unknown").lower()

        # A name can appear more than once (a re-run). The worst wins, so a
        # green re-run never hides a leg that is still missing or red.
        rank = {PASSED: 0, PENDING: 1, FAILED: 2}
        if name not in out or rank[state] > rank[out[name][0]]:
            out[str(name)] = (state, detail)
    return out


def _open_prs(*, run_gh: GhRunner, repo: Optional[str], limit: int) -> list[dict]:
    raw = run_gh(
        [
            "pr",
            "list",
            *_repo_args(repo),
            "--state",
            "open",
            "--limit",
            str(limit),
            "--json",
            "number,title,baseRefName,mergeable,mergeStateStatus,"
            "statusCheckRollup,isDraft,url",
        ]
    )
    try:
        return json.loads(raw or "[]") or []
    except ValueError as exc:
        raise CIWhyError("could not parse gh pr list JSON") from exc


def audit_blocked(
    *,
    run_gh: GhRunner = run_gh,
    repo: Optional[str] = None,
    base: Optional[str] = None,
    limit: int = 100,
) -> list[PRGate]:
    """Diff every open PR's REQUIRED contexts, by name, against what reported."""
    prs = _open_prs(run_gh=run_gh, repo=repo, limit=limit)
    if base:
        prs = [p for p in prs if str(p.get("baseRefName", "")) == base]

    # One protection read per distinct base branch, not one per PR.
    cache: dict[str, list[str]] = {}
    gates: list[PRGate] = []
    for p in prs:
        branch = str(p.get("baseRefName", ""))
        if branch not in cache:
            cache[branch] = required_contexts(branch, run_gh=run_gh, repo=repo)

        rollup = _rollup_states(p.get("statusCheckRollup") or [])
        required = [
            RequiredContext(
                name=name,
                state=rollup.get(name, (NEVER_STARTED, "absent from rollup"))[0],
                detail=rollup.get(name, (NEVER_STARTED, "absent from rollup"))[1],
            )
            for name in cache[branch]
        ]
        gates.append(
            PRGate(
                number=int(p.get("number", 0)),
                title=str(p.get("title", "")),
                base=branch,
                merge_state_status=str(p.get("mergeStateStatus", "")),
                mergeable=str(p.get("mergeable", "")),
                url=str(p.get("url", "")),
                is_draft=bool(p.get("isDraft", False)),
                required=required,
                reported=len(rollup),
            )
        )
    return gates


def render_text(gates: list[PRGate]) -> str:
    """One block per PR, silent ones first — they are the reason to look."""
    if not gates:
        return "no open PRs"

    ordered = sorted(gates, key=lambda g: (not g.silently_blocked, g.number))
    lines: list[str] = []
    for g in ordered:
        mark = "SILENT-BLOCK" if g.silently_blocked else "            "
        draft = " [draft]" if g.is_draft else ""
        lines.append(
            f"{mark} #{g.number}  {g.mergeable}/{g.merge_state_status}"
            f"  -> {g.base}{draft}  {g.title[:60]}"
        )
        if not g.required:
            lines.append("      (no required contexts on this base — nothing to gate)")
        for c in g.required:
            lines.append(f"      {c.state:<13} {c.name}  ({c.detail})")
        lines.append(
            f"      {g.reported} check(s) reported; a naive pending-count "
            f"reads {len(g.pending)}"
        )
    silent = [g for g in ordered if g.silently_blocked]
    lines.append("  ---")
    if silent:
        nums = ", ".join(f"#{g.number}" for g in silent)
        lines.append(
            f"  {len(silent)} PR(s) BLOCKED with no failure — required check "
            f"never started: {nums}"
        )
    else:
        lines.append("  no PR is blocked by a never-started required check")
    return "\n".join(lines)
