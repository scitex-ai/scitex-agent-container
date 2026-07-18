"""Read the open-PR list from GitHub — and NEVER lie about having read it.

THE ONE RULE THIS MODULE EXISTS TO ENFORCE
------------------------------------------
An EMPTY set of open PRs and an UNREADABLE set of open PRs are DIFFERENT
FACTS, and this module refuses to render the second as the first.

That collapse is not hypothetical — it is the shape of the fleet incident of
2026-07-18. :meth:`.._authheal._pass.PassOutcome.exit_code` ends in a bare
``return 0`` fallthrough, so "nothing was observed" and "everything is clean"
became the same exit code; five systemd timers then reported SUCCESS every ten
minutes while an agent sat login-expired for hours. The sibling
:func:`.._lifecycle._github_ci.list_open_prs` has the same defect one level
lower: every failure path (``gh`` missing, unauthenticated, rate-limited,
unparseable) returns ``[]``, which its callers cannot tell from "this repo has
no open PRs".

Applied to PR bookkeeping the consequence is concrete and bad: a 35-PR backlog
would render as a CLEAN BOARD, the card sweep would resolve every card it had
(because none of the PRs it knows about are "still open"), and the 3-day expiry
would close nothing while reporting success.

So the seam here is not ``run(args) -> str``. A string cannot carry "I failed",
which is exactly how the collapse gets built. It is
``run(args) -> GhInvocation``, a record that keeps the return code, both
streams and any spawn error — and :func:`fetch_open_prs` returns a
:class:`PRFetch` whose ``prs`` are meaningful ONLY when
``state is FetchState.OK``.

:attr:`FetchState.OK` is a WHITELIST OF ONE (:meth:`PRFetch.readable`). Every
other member — present and future — is UNKNOWN. A state nobody thought about
therefore defaults to "we could not determine", never to "clean". That is the
inverse of the fallthrough that caused the incident, and it is deliberate.

Note the ``gh`` contract this leans on: a repo with genuinely zero open PRs
prints ``[]``, not nothing. BLANK stdout on a zero exit is therefore
:attr:`FetchState.UNPARSEABLE` (something ate the payload), NOT an empty list.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Callable

__all__ = [
    "CI_FAILURE",
    "CI_NONE",
    "CI_PENDING",
    "CI_SUCCESS",
    "FetchState",
    "GhInvocation",
    "GhRunner",
    "JSON_FIELDS",
    "PRFetch",
    "PullRequest",
    "ci_status",
    "fetch_open_prs",
    "parse_pr_rows",
    "run_gh",
]

#: Exactly the fields the two jobs need: identity + the card's facts (title,
#: author, age, draft, CI) + ``updatedAt``, which is what "untouched for N
#: days" is measured from.
JSON_FIELDS = "number,title,author,createdAt,updatedAt,isDraft,url,statusCheckRollup"

CI_SUCCESS = "success"
CI_FAILURE = "failure"
CI_PENDING = "pending"
CI_NONE = "none"

#: ``statusCheckRollup`` conclusions that mean RED. ``gh`` reports CheckRun
#: rows (``status`` + ``conclusion``) and legacy StatusContext rows (``state``);
#: both vocabularies are folded in below.
_FAILING = frozenset(
    {"FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE", "ERROR"}
)

#: CheckRun ``status`` values meaning "still running" — note that an
#: in-progress row carries an EMPTY ``conclusion``, so conclusion alone cannot
#: distinguish pending from success (verified against a live response,
#: 2026-07-18).
_RUNNING = frozenset({"QUEUED", "IN_PROGRESS", "WAITING", "PENDING", "REQUESTED"})


class FetchState(str, Enum):
    """Did we actually READ the open-PR list — and if not, why not?

    Six members, one of which means success. The asymmetry is the design:
    :meth:`PRFetch.readable` tests ``is FetchState.OK`` and nothing else, so
    adding a member here can only ever add a way to say UNKNOWN. It can never
    accidentally add a way to say "clean".
    """

    #: Read AND parsed. ``PRFetch.prs`` is the truth, and an EMPTY tuple here
    #: genuinely means this repo has zero open PRs.
    OK = "ok"
    #: ``gh`` could not be spawned at all (not installed, PATH broken).
    NO_CLIENT = "no-client"
    #: ``gh`` ran but has no usable credential (HTTP 401 / "gh auth login").
    UNAUTHENTICATED = "unauthenticated"
    #: GitHub told us to slow down. The backlog is unchanged and unread.
    RATE_LIMITED = "rate-limited"
    #: Network / API failure — DNS, TLS, 5xx, timeout.
    UNREACHABLE = "unreachable"
    #: ``gh`` exited 0 but the payload is not the shape we require (including
    #: BLANK stdout, which is not the same as the ``[]`` a genuinely empty
    #: repo prints).
    UNPARSEABLE = "unparseable"


@dataclass(frozen=True)
class GhInvocation:
    """Everything ONE ``gh`` call told us, including how it failed.

    The whole point of this type is that it can express failure. A
    ``str``-returning runner cannot, so every caller of one is forced to invent
    an empty success — which is the bug this module exists to prevent.
    """

    returncode: int = 0
    stdout: str = ""
    stderr: str = ""
    #: Set when the process could not be started at all (``gh`` missing).
    spawn_error: str = ""


#: The injection seam. Tests pass a recorded :class:`GhInvocation`; production
#: gets :func:`run_gh`. Note the return type — this is the seam's contract.
GhRunner = Callable[[list], GhInvocation]


def run_gh(args: list) -> GhInvocation:
    """Run ``gh <args>``, reporting the outcome HONESTLY.

    Never swallows: a spawn failure becomes ``spawn_error`` and a non-zero exit
    keeps its code and stderr, so the classifier below can tell the caller
    WHICH way we are blind.
    """
    import subprocess

    # stx-allow: fallback (reason: this IS the honest-failure path — a missing/unspawnable gh becomes a GhInvocation the classifier turns into an UNKNOWN verdict, never into an empty success)
    try:
        proc = subprocess.run(
            ["gh", *args], capture_output=True, text=True, check=False, timeout=120
        )
    except Exception as exc:
        return GhInvocation(returncode=-1, spawn_error=f"{type(exc).__name__}: {exc}")
    return GhInvocation(
        returncode=proc.returncode, stdout=proc.stdout or "", stderr=proc.stderr or ""
    )


def _parse_ts(stamp: str) -> "datetime | None":
    """Parse ``gh``'s RFC-3339 ``...Z`` timestamps. Unparseable → ``None``."""
    text = (stamp or "").strip()
    if not text:
        return None
    # stx-allow: fallback (reason: a malformed timestamp must not crash a sweep; the caller renders age 0.0, the CONSERVATIVE direction — a PR never looks staler than it is, so a bad stamp can never trigger a close)
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(
            timezone.utc
        )
    except ValueError:
        return None


@dataclass(frozen=True)
class PullRequest:
    """One open PR, reduced to the facts the two jobs act on."""

    repo: str
    number: int
    title: str
    author: str
    created_at: str
    updated_at: str
    draft: bool
    url: str
    ci: str

    def _age(self, stamp: str, now: datetime) -> float:
        parsed = _parse_ts(stamp)
        if parsed is None:
            return 0.0
        return max(0.0, (now - parsed).total_seconds() / 86_400.0)

    def age_days(self, now: datetime) -> float:
        """Days since the PR was OPENED — the card's headline number."""
        return self._age(self.created_at, now)

    def idle_days(self, now: datetime) -> float:
        """Days since the PR was last TOUCHED — what the 3-day policy measures.

        The operator's rule is about a PR going stale, not about a long-running
        PR someone is actively pushing to, so this reads ``updatedAt``.
        """
        return self._age(self.updated_at, now)


def ci_status(rollup: object) -> str:
    """Fold ``statusCheckRollup`` into one word.

    Order matters: RED beats pending beats green. A missing/empty rollup is
    :data:`CI_NONE` — "no checks reported", which is NOT "checks passed".
    """
    if not isinstance(rollup, list) or not rollup:
        return CI_NONE
    states: set = set()
    running = False
    for row in rollup:
        if not isinstance(row, dict):
            continue
        if str(row.get("status", "")).upper() in _RUNNING:
            running = True
        for key in ("conclusion", "state"):
            value = str(row.get(key, "")).upper()
            if value:
                states.add(value)
    if states & _FAILING:
        return CI_FAILURE
    if running or "PENDING" in states:
        return CI_PENDING
    if states:
        return CI_SUCCESS
    return CI_NONE


def parse_pr_rows(rows: object, repo: str) -> "tuple[PullRequest, ...] | None":
    """Turn ``gh``'s JSON rows into :class:`PullRequest`s.

    Returns ``None`` when the payload is not the shape we require — the caller
    turns that into :attr:`FetchState.UNPARSEABLE`. It does NOT return an empty
    tuple for a bad payload: that is the very conflation this module forbids.
    """
    if not isinstance(rows, list):
        return None
    out: list = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        number = row.get("number")
        if not isinstance(number, int):
            continue
        author = row.get("author")
        login = (
            str(author.get("login", ""))
            if isinstance(author, dict)
            else str(author or "")
        )
        out.append(
            PullRequest(
                repo=repo,
                number=number,
                title=str(row.get("title") or ""),
                author=login or "unknown",
                created_at=str(row.get("createdAt") or ""),
                updated_at=str(row.get("updatedAt") or ""),
                draft=bool(row.get("isDraft")),
                url=str(row.get("url") or ""),
                ci=ci_status(row.get("statusCheckRollup")),
            )
        )
    return tuple(out)


@dataclass(frozen=True)
class PRFetch:
    """The open-PR list, PLUS whether we may believe it.

    ``prs`` is meaningful ONLY when :attr:`readable` — read it without checking
    and an unauthenticated ``gh`` silently becomes "the board is clean".
    """

    state: FetchState
    prs: tuple = ()
    detail: str = ""

    @property
    def readable(self) -> bool:
        """Did we PROVE we read the list? A whitelist of exactly one state."""
        return self.state is FetchState.OK

    @property
    def unknown(self) -> bool:
        return not self.readable

    def numbers(self) -> set:
        """Open PR numbers.

        Empty for an UNREADABLE fetch just as for a genuinely empty repo, so
        ALWAYS gate on :attr:`readable` before treating this as "these are the
        only open PRs" — that inference is what closes every card on a blind
        pass.
        """
        return {pr.number for pr in self.prs}


def _classify_failure(res: GhInvocation) -> tuple:
    """Name HOW we are blind, so the report is actionable rather than vague."""
    if res.spawn_error:
        return (
            FetchState.NO_CLIENT,
            f"the 'gh' CLI could not be run ({res.spawn_error}) — the open-PR "
            f"list was never fetched",
        )
    blob = f"{res.stderr}\n{res.stdout}".lower()
    if "rate limit" in blob or "429" in blob:
        return (
            FetchState.RATE_LIMITED,
            f"GitHub rate-limited this read (gh exit {res.returncode}): "
            f"{res.stderr.strip()[:400]}",
        )
    if (
        "gh auth login" in blob
        or "authentication" in blob
        or "http 401" in blob
        or "bad credentials" in blob
        or "not logged" in blob
    ):
        return (
            FetchState.UNAUTHENTICATED,
            f"'gh' has no usable credential (gh exit {res.returncode}): "
            f"{res.stderr.strip()[:400]} — run: gh auth login",
        )
    return (
        FetchState.UNREACHABLE,
        f"'gh' failed to read the PR list (exit {res.returncode}): "
        f"{res.stderr.strip()[:400]}",
    )


def fetch_open_prs(repo: str, *, limit: int = 200, run=None) -> PRFetch:
    """Fetch every OPEN PR for ``repo`` — or say, loudly, that we could not.

    The four returns below are the whole contract:

    * spawn/exit failure → the matching UNKNOWN state, ``prs=()``
    * blank stdout on exit 0 → ``UNPARSEABLE`` (``gh`` prints ``[]`` for a
      genuinely empty repo, so BLANK means something ate the payload)
    * a payload we cannot shape → ``UNPARSEABLE``
    * a real list (possibly empty) → ``OK``, and ONLY here does an empty
      ``prs`` mean "this repo has no open PRs"
    """
    runner = run if run is not None else run_gh
    res = runner(
        [
            "pr",
            "list",
            "-R",
            repo,
            "--state",
            "open",
            "--limit",
            str(limit),
            "--json",
            JSON_FIELDS,
        ]
    )
    if res.spawn_error or res.returncode != 0:
        state, detail = _classify_failure(res)
        return PRFetch(state, (), detail)

    raw = (res.stdout or "").strip()
    if not raw:
        return PRFetch(
            FetchState.UNPARSEABLE,
            (),
            "'gh' exited 0 but printed NOTHING. A repo with no open PRs prints "
            "'[]', so blank output means the payload was lost — this is an "
            "UNREAD list, not an empty one, and treating it as empty would "
            "render a full backlog as a clean board",
        )
    # stx-allow: fallback (reason: a payload we cannot parse is UNPARSEABLE — an explicit UNKNOWN state the caller must alarm on — never an empty list of PRs)
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        return PRFetch(
            FetchState.UNPARSEABLE,
            (),
            f"'gh' exited 0 but its output is not JSON ({exc}) — the open-PR "
            f"list is UNREAD, not empty",
        )
    parsed = parse_pr_rows(payload, repo)
    if parsed is None:
        return PRFetch(
            FetchState.UNPARSEABLE,
            (),
            "'gh' returned JSON that is not a list of PRs — refusing to guess "
            "that the backlog is empty",
        )
    return PRFetch(FetchState.OK, parsed, f"read {len(parsed)} open PR(s) from {repo}")
