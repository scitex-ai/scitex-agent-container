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


def pr_ci_conclusion(repo: str, pr: int, *, run: GhRunner = _run_gh) -> str:
    """Return the PR's overall CI conclusion.

    One of :data:`CONCLUSION_SUCCESS`, :data:`CONCLUSION_FAILURE`,
    :data:`CONCLUSION_PENDING`, :data:`CONCLUSION_NONE`. Mapping over the
    ``gh pr checks --json bucket`` buckets:

      * any ``fail``/``cancel`` bucket → ``failure``
      * else any ``pending`` bucket   → ``pending``
      * else (all ``pass``/``skipping``, non-empty) → ``success``
      * empty list or unparseable output → ``none`` (deliver nothing)
    """
    raw = run(["pr", "checks", str(pr), "-R", repo, "--json", "bucket"])
    try:
        rows = json.loads(raw) if raw.strip() else []
    except (ValueError, TypeError):
        return CONCLUSION_NONE
    if not isinstance(rows, list) or not rows:
        return CONCLUSION_NONE
    buckets = {str(r.get("bucket", "")) for r in rows if isinstance(r, dict)}
    if buckets & _FAILING_BUCKETS:
        return CONCLUSION_FAILURE
    if "pending" in buckets:
        return CONCLUSION_PENDING
    if buckets:
        return CONCLUSION_SUCCESS
    return CONCLUSION_NONE


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
    raw = run(
        [
            "pr",
            "list",
            "-R",
            repo,
            "--state",
            "open",
            "--json",
            "number,headRefOid,body",
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
    "gh_ready",
    "list_open_prs",
    "pr_ci_conclusion",
    "pr_head_sha",
]
