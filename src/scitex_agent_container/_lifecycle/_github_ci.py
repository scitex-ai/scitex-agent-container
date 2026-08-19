"""GitHub-CI poll reads for the CI-feedback ring (sac #404).

feedback.pdf §3: ``sac listen`` polls GitHub CI on its OWN schedule and
delivers each verdict to the pusher. This module wraps the two ``gh``
reads the poll loop needs:

  * :func:`pr_ci_conclusion` — a PR's overall CI outcome
    (``success`` / ``failure`` / ``pending`` / ``none``), derived from
    ``gh pr checks <pr> -R <repo> --json bucket``.
  * :func:`pr_head_sha` — the PR's current head sha, from
    ``gh api repos/<repo>/pulls/<pr> --jq .head.sha`` (so the dedup key
    ``(repo, pr, head_sha, conclusion)`` tracks the exact commit).

Both take a ``run`` injection seam (``run(args) -> stdout``) defaulting
to a thin ``gh`` subprocess wrapper — tests pass canned output, no
network. The wrapper is deliberately fail-soft: any ``gh`` error maps to
empty output → ``"none"`` / ``""`` so a transient GitHub blip degrades to
"deliver nothing this tick" rather than crashing the listen loop.

``gh`` is the chosen client because it is already installed + token-
authenticated on the fleet hosts (repo + workflow scopes); no extra dep.
"""

from __future__ import annotations

import json
import logging
from typing import Callable

logger = logging.getLogger(__name__)

# A ``run`` seam returns the subprocess stdout for ``gh <args>``.
GhRunner = Callable[[list], str]

CONCLUSION_SUCCESS = "success"
CONCLUSION_FAILURE = "failure"
CONCLUSION_PENDING = "pending"
CONCLUSION_NONE = "none"

# ``gh pr checks --json bucket`` bucket vocabulary.
_FAILING_BUCKETS = frozenset({"fail", "cancel"})


def _run_gh(args: list) -> str:
    """Run ``gh <args>`` and return stdout (empty on any failure).

    Fail-soft: ``gh pr checks`` exits non-zero for pending/failing states
    yet still prints the ``--json`` payload, so we return stdout
    regardless of return code. A missing binary / OSError maps to ``""``.
    """
    import subprocess

    try:
        proc = subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            check=False,
        )
    except (
        Exception
    ) as exc:  # stx-allow: fallback (gh missing / spawn error → no verdict this tick)
        logger.warning("_github_ci: gh invocation failed: %s", exc)
        return ""
    return proc.stdout or ""


#: REST check-run ``conclusion`` -> the bucket vocabulary `gh pr checks`
#: uses, so the mapping below this stays untouched. Anything unrecognised is
#: PENDING, never SUCCESS: an unknown state must not read as green.
_BUCKET_FOR_CONCLUSION = {
    "success": "pass",
    "skipped": "skipping",
    "neutral": "skipping",
    "failure": "fail",
    "timed_out": "fail",
    "action_required": "fail",
    "startup_failure": "fail",
    "stale": "fail",
    "cancelled": "cancel",
}

#: REST commit-status ``state`` -> the same vocabulary. Same rule: unknown
#: is pending, not pass.
_BUCKET_FOR_STATE = {
    "success": "pass",
    "failure": "fail",
    "error": "fail",
    "pending": "pending",
}


def pr_ci_conclusion(
    repo: str,
    pr: int,
    *,
    head_sha: str = "",
    run: GhRunner = _run_gh,
) -> str:
    """Return the PR's overall CI conclusion.

    One of :data:`CONCLUSION_SUCCESS`, :data:`CONCLUSION_FAILURE`,
    :data:`CONCLUSION_PENDING`, :data:`CONCLUSION_NONE`. Mapping over the
    ``gh pr checks --json bucket`` buckets:

      * any ``fail``/``cancel`` bucket → ``failure``
      * else any ``pending`` bucket   → ``pending``
      * else (all ``pass``/``skipping``, non-empty) → ``success``
      * empty list or unparseable output → ``none`` (deliver nothing)
    """
    # REST, not `gh pr checks --json bucket` (a GraphQL POST). See
    # `list_open_prs` for the quota measurement that forced this.
    #
    # PARITY MATTERS HERE AND IS EASY TO GET WRONG. `gh pr checks` merges TWO
    # GitHub concepts: check-runs (the Actions/App API) AND commit statuses
    # (the older Status API that external CI still uses). Substituting only
    # `/check-runs` would silently DROP the statuses — a green verdict that is
    # green because it stopped looking. MEASURED 2026-08-19 on one open PR per
    # repo: scitex-agent-container check_runs=8 statuses=0, but scitex-dev
    # check_runs=16 statuses=1. The status arm is NOT hypothetical today, and
    # even where it is empty the code must ask, because "no external CI right
    # now" is a fleet fact and not a guarantee.
    #
    # The sha is fetched via REST (`pr_head_sha`, already REST in this
    # module) when the caller does not supply one; the poll loop already
    # holds it from `list_open_prs` and should pass it to save the call.
    # `pr_head_sha` is defined BELOW this function, so it is resolved at
    # call time rather than at import — Python looks the name up in the
    # module globals when the call executes, by which point it exists.
    sha = (head_sha or "").strip()
    if not sha:
        sha = pr_head_sha(repo, pr, run=run)
    if not sha:
        return CONCLUSION_NONE
    buckets: set[str] = set()

    raw = run(["api", f"repos/{repo}/commits/{sha}/check-runs?per_page=100"])
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except (ValueError, TypeError):
        payload = {}
    for cr in (payload or {}).get("check_runs", []) or []:
        if not isinstance(cr, dict):
            continue
        if str(cr.get("status", "")) != "completed":
            buckets.add("pending")
            continue
        buckets.add(_BUCKET_FOR_CONCLUSION.get(str(cr.get("conclusion", "")), "pending"))

    raw_st = run(["api", f"repos/{repo}/commits/{sha}/status"])
    try:
        st = json.loads(raw_st) if raw_st.strip() else {}
    except (ValueError, TypeError):
        st = {}
    for one in (st or {}).get("statuses", []) or []:
        if not isinstance(one, dict):
            continue
        buckets.add(_BUCKET_FOR_STATE.get(str(one.get("state", "")), "pending"))

    if not buckets:
        return CONCLUSION_NONE
    if buckets & _FAILING_BUCKETS:
        return CONCLUSION_FAILURE
    if "pending" in buckets:
        return CONCLUSION_PENDING
    if buckets:
        return CONCLUSION_SUCCESS
    return CONCLUSION_NONE


def failing_check_names(repo: str, pr: int, *, run: GhRunner = _run_gh) -> list:
    """Return the NAMES of the PR's checks currently in a failing bucket.

    :func:`pr_ci_conclusion` asks only for ``bucket``, which is all a
    pass/fail verdict needs — but it means the ring cannot tell "a
    different check failed this time" from "the same check has been red
    across every head sha", and those warrant different advice. Deliberately
    a SEPARATE call rather than widening the hot path: this is only needed
    on the rare escalation tick, not on every poll.

    Sorted and de-duplicated. Unparseable / empty output → ``[]``, so a
    caller can always render the escalation without a name.
    """
    raw = run(["pr", "checks", str(pr), "-R", repo, "--json", "name,bucket"])
    try:
        rows = json.loads(raw) if raw.strip() else []
    except (ValueError, TypeError):
        return []
    if not isinstance(rows, list):
        return []
    names = {
        str(r.get("name", "")).strip()
        for r in rows
        if isinstance(r, dict) and str(r.get("bucket", "")) in _FAILING_BUCKETS
    }
    return sorted(n for n in names if n)


def pr_head_sha(repo: str, pr: int, *, run: GhRunner = _run_gh) -> str:
    """Return the PR's current head sha (empty string if unresolvable)."""
    raw = run(["api", f"repos/{repo}/pulls/{pr}", "--jq", ".head.sha"])
    return raw.strip()


def list_open_prs(repo: str, *, run: GhRunner = _run_gh) -> list:
    """Return open PRs for ``repo`` as dicts ``{number, head_sha, body}``.

    One ``gh pr list -R <repo> --state open --json number,headRefOid,body``
    call gives the poll loop everything it needs per PR (number + head sha
    for the dedup key, body for the ``Owner:`` fallback). Unparseable /
    empty output → ``[]`` (the tick polls nothing for this repo).
    """
    # REST, not `gh pr list --json`. MEASURED 2026-08-19: this poller ran on
    # five hosts against one account and burned 10,634 GraphQL points/hour
    # against a 5,000/hour pool — the account was dead for 32 minutes of every
    # hour and every agent's PR operations failed during that window. `gh pr
    # list --json` is a GraphQL POST; `gh api repos/<r>/pulls` is REST, and
    # REST sat at 4,300/5,000 unused throughout. Same data, the other pool.
    #
    # `head_sha_for` in this same module already uses REST for exactly this
    # reason; this call site simply had not followed it.
    raw = run(
        [
            "api",
            f"repos/{repo}/pulls?state=open&per_page=100",
            "--jq",
            "[.[] | {number: .number, headRefOid: .head.sha, body: .body}]",
        ]
    )
    try:
        rows = json.loads(raw) if raw.strip() else []
    except (ValueError, TypeError):
        return []
    out: list = []
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict):
            continue
        num = r.get("number")
        if not isinstance(num, int):
            continue
        out.append(
            {
                "number": num,
                "head_sha": str(r.get("headRefOid", "")),
                "body": str(r.get("body") or ""),
            }
        )
    return out


def gh_ready(*, probe=None) -> bool:
    """True iff ``gh`` is installed AND authenticated.

    The poll loop calls this once at startup and FAILS LOUD (logs an
    error + disables itself) when it returns False — a missing /
    unauthenticated ``gh`` is a deploy error that must be visible, not a
    silent stream of ``none`` verdicts (operator: fail-loud, fail-fast,
    no silent fallbacks). ``probe`` is a test seam returning the bool
    directly; production runs ``gh auth status``.
    """
    if probe is not None:
        return bool(probe())
    import subprocess

    try:
        r = subprocess.run(
            ["gh", "auth", "status"],
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception:  # stx-allow: fallback (gh missing → not ready; loop fails loud)
        return False
    return r.returncode == 0


__all__ = [
    "CONCLUSION_FAILURE",
    "CONCLUSION_NONE",
    "CONCLUSION_PENDING",
    "CONCLUSION_SUCCESS",
    "failing_check_names",
    "gh_ready",
    "list_open_prs",
    "pr_ci_conclusion",
    "pr_head_sha",
]
