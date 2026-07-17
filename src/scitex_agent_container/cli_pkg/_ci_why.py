"""Extract the REAL reason a CI run is red — as cheaply as its status.

Reading CI *status* is one word (``failure``); reading *why* has been
tens of thousands of lines, so a bounded-context agent is steered to the
cheap word and the word replaces the reason instead of summarising it.
This module inverts that price: it fetches a failing run's log ONCE and
parses it to a few hundred bytes — failing test IDs, assertion lines, or
a setup failure's ``##[error]``. Everything except the injectable
:func:`run_gh` gh-seam is pure/string-based (unit-tested, no network).
"""

from __future__ import annotations

import json
import re
import subprocess
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Callable, Optional

__all__ = [
    "CIWhyError",
    "GhRunner",
    "JobFailure",
    "RunFailures",
    "run_gh",
    "clean_log_line",
    "parse_job_context",
    "split_log_by_job",
    "parse_failed_log",
    "resolve_run_ids",
    "explain_run",
    "explain",
    "render_text",
]

# --log-failed can be a few MB; give the fetch room but stay bounded.
_GH_TIMEOUT_S = 90

# A PR number is small; a run id is a 10+ digit database id. Anything at
# or above this magnitude is treated as a run id, below it as a PR number.
_RUN_ID_MIN = 10_000_000

GhRunner = Callable[[list[str]], str]


class CIWhyError(RuntimeError):
    """gh is missing/unauthenticated/errored, or the target won't resolve.

    Raised, never swallowed into a reassuring "no failures": not knowing
    WHY a run is red is UNKNOWN, and UNKNOWN must not read as green. The
    click layer turns this into a loud stderr error + non-zero exit.
    """


def run_gh(args: list[str], *, _run=subprocess.run) -> str:
    """Default ``gh`` seam: run ``gh <args>`` and return stdout text.

    The one place the network is touched. Returns stdout even on a
    non-zero exit *when stdout is non-empty* — ``gh pr checks`` exits
    non-zero precisely when checks fail, yet still prints the JSON we
    want. A non-zero exit with no output (bad run id, auth failure, gh
    missing) raises :class:`CIWhyError`.
    """
    try:
        proc = _run(
            ["gh", *args],
            capture_output=True,
            text=True,
            timeout=_GH_TIMEOUT_S,
            check=False,
        )
    except FileNotFoundError as exc:  # stx-allow: fallback (reason: gh not installed → loud CIWhyError, never a silent green)
        raise CIWhyError(
            "gh CLI not found on PATH — install GitHub CLI: https://cli.github.com"
        ) from exc
    except (
        OSError,
        subprocess.SubprocessError,
    ) as exc:  # stx-allow: fallback (reason: spawn error / timeout → loud CIWhyError, never a silent green)
        raise CIWhyError(f"gh {' '.join(args)} failed to run: {exc}") from exc
    out = proc.stdout or ""
    if proc.returncode != 0 and not out.strip():
        err = (proc.stderr or "").strip() or f"exited {proc.returncode}"
        raise CIWhyError(f"gh {' '.join(args)}: {err}")
    return out


# ---------------------------------------------------------------------------
# Log cleaning — strip GitHub-Actions scaffolding.
# ---------------------------------------------------------------------------

# The ISO-8601 runner timestamp that prefixes every raw actions log line
# (after gh's optional "<job>\t<step>\t" prefix). Everything up to and
# including it is scaffolding.
_TS_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z ?")
_BOM = "﻿"
_ERROR_ANNOT = "##[error]"


def clean_log_line(raw: str) -> Optional[str]:
    r"""Strip GitHub-Actions scaffolding from one raw log line.

    Removes the optional ``<job>\t<step>\t`` prefix that
    ``gh run view --log-failed`` prepends, the ISO-8601 runner timestamp,
    and the UTF-8 BOM. ``##[group]`` / ``##[endgroup]`` fold markers are
    dropped entirely (return ``None``). Everything else — including
    ``##[error]`` — is returned as bare content.
    """
    line = raw.rstrip("\n").replace(_BOM, "")
    m = _TS_RE.search(line)
    content = line[m.end() :] if m else line
    stripped = content.strip()
    if stripped.startswith("##[group]") or stripped.startswith("##[endgroup]"):
        return None
    return content


def split_log_by_job(log_text: str) -> "OrderedDict[str, str]":
    r"""Group ``gh run view --log-failed`` lines by job name (first column).

    ``--log-failed`` prefixes each line ``<job>\t<step>\t<ts> <content>``.
    Lines with no such prefix (a plain single-job log, as in a fixture)
    group under the empty-string key.
    """
    groups: "OrderedDict[str, list[str]]" = OrderedDict()
    for raw in log_text.splitlines():
        line = raw.replace(_BOM, "")
        parts = line.split("\t", 2)
        if len(parts) == 3 and _TS_RE.match(parts[2]):
            job = parts[0]
        else:
            job = ""
        groups.setdefault(job, []).append(raw)
    return OrderedDict((job, "\n".join(lines)) for job, lines in groups.items())


# ---------------------------------------------------------------------------
# Job-name context — python version + runner OS from the job name.
# ---------------------------------------------------------------------------

_PY_RE = re.compile(r"(?:py[ -]?)?3[.\-](\d{1,2})\b", re.IGNORECASE)
_OS_RE = re.compile(
    r"(ubuntu-latest|ubuntu-\d[\w.]*|ubuntu|macos-[\w.]+|macos|"
    r"windows-[\w.]+|windows|self-hosted)",
    re.IGNORECASE,
)


def parse_job_context(job_name: str) -> tuple[Optional[str], Optional[str]]:
    """Best-effort ``(python_version, runner_os)`` from a job name.

    Matrix legs encode context in the name, e.g.
    ``pytest-matrix-on-ubuntu-py3.11``  -> ('3.11', 'ubuntu'),
    ``import-smoke-on-ubuntu-py3-12``   -> ('3.12', 'ubuntu'),
    ``...guard-on-self-hosted``         -> (None, 'self-hosted').
    """
    py = None
    m = _PY_RE.search(job_name)
    if m:
        py = f"3.{m.group(1)}"
    os_ = None
    mo = _OS_RE.search(job_name)
    if mo:
        os_ = mo.group(1).lower()
    return py, os_


# ---------------------------------------------------------------------------
# The parser — the testable core.
# ---------------------------------------------------------------------------

_SUMMARY_RE = re.compile(r"={3,}\s*short test summary info\s*={3,}", re.IGNORECASE)
_FAILURES_HDR_RE = re.compile(r"={3,}\s*(FAILURES|ERRORS)\s*={3,}")
_SUMMARY_END_RE = re.compile(
    r"={3,}.*\b(passed|failed|error|errors|skipped|deselected|"
    r"xfailed|xpassed|warning|warnings|no tests ran)\b.*={3,}",
    re.IGNORECASE,
)
_FAILED_LINE_RE = re.compile(r"^(FAILED|ERROR)\s+\S")
_E_LINE_RE = re.compile(r"^E(?:\s|$)")


@dataclass
class JobFailure:
    """The distilled failure of ONE job — a few lines, not a whole log."""

    job: str
    py: Optional[str] = None
    os: Optional[str] = None
    failed_tests: list[str] = field(default_factory=list)
    assertions: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)  # ##[error] annotations
    tail: list[str] = field(default_factory=list)
    url: str = ""

    @property
    def signal(self) -> str:
        """Which tier produced the primary evidence (priority order)."""
        if self.failed_tests:
            return "pytest-summary"
        if self.assertions:
            return "pytest-assertion"
        if self.errors:
            return "annotation"
        if self.tail:
            return "tail"
        return "none"

    def context(self) -> str:
        """`` (py3.11, ubuntu)`` — the matrix context, or empty."""
        bits = [b for b in (self.py and f"py{self.py}", self.os) if b]
        return f" ({', '.join(bits)})" if bits else ""

    def primary_lines(
        self, *, max_assertions: int = 8, max_errors: int = 5, max_tests: int = 20
    ) -> list[str]:
        """The compact evidence to show under this job's header."""
        lines: list[str] = []
        if self.failed_tests:
            lines.extend(self.failed_tests[:max_tests])
            extra = len(self.failed_tests) - max_tests
            if extra > 0:
                lines.append(f"... and {extra} more failing test(s)")
        if self.assertions:
            lines.extend(self.assertions[:max_assertions])
        if not self.failed_tests and not self.assertions:
            if self.errors:
                lines.extend(f"##[error] {e}" for e in self.errors[:max_errors])
            elif self.tail:
                lines.append("(no pytest/annotation signal — last log lines:)")
                lines.extend(self.tail)
            else:
                lines.append("(failed, but gh returned no log content)")
        return lines

    def to_dict(self) -> dict:
        return {
            "job": self.job,
            "python": self.py,
            "os": self.os,
            "signal": self.signal,
            "failed_tests": self.failed_tests,
            "assertions": self.assertions,
            "errors": self.errors,
            "tail": self.tail,
            "url": self.url,
        }


def parse_failed_log(
    log_text: str,
    *,
    job_name: str = "",
    url: str = "",
    tail_lines: int = 8,
) -> JobFailure:
    """Parse ONE job's ``--log-failed`` text into a :class:`JobFailure`.

    Priority of signals: (1) the ``short test summary info`` ``FAILED``
    lines; (2) the ``FAILURES`` block ``E`` assertion lines; (3)
    ``##[error]`` annotations (setup/infra failures); (4) fallback to the
    last ``tail_lines`` non-blank cleaned lines.
    """
    py, os_ = parse_job_context(job_name)
    fail = JobFailure(job=job_name, py=py, os=os_, url=url)

    clean: list[str] = []
    for raw in log_text.splitlines():
        c = clean_log_line(raw)
        if c is None:
            continue
        clean.append(c)
        if c.lstrip().startswith(_ERROR_ANNOT):
            annot = c.lstrip()[len(_ERROR_ANNOT) :].strip()
            if annot:
                fail.errors.append(annot)

    # (1) pytest short test summary — the cheapest, richest signal.
    in_summary = False
    for c in clean:
        s = c.strip()
        if _SUMMARY_RE.search(s):
            in_summary = True
            continue
        if in_summary:
            if _SUMMARY_END_RE.search(s):
                break
            if _FAILED_LINE_RE.match(s):
                fail.failed_tests.append(s)

    # (2) assertion detail from the FAILURES / ERRORS block.
    in_failures = False
    for c in clean:
        st = c.strip()
        if _FAILURES_HDR_RE.search(st):
            in_failures = True
            continue
        if in_failures:
            if _SUMMARY_RE.search(st):
                break
            if _E_LINE_RE.match(c):
                fail.assertions.append(st)

    # (4) fallback tail when nothing structured was found.
    if not (fail.failed_tests or fail.assertions or fail.errors):
        nonblank = [c for c in clean if c.strip()]
        fail.tail = nonblank[-tail_lines:]
    return fail


# ---------------------------------------------------------------------------
# Run-level orchestration (uses the run_gh seam).
# ---------------------------------------------------------------------------

_FAILED_JOB_CONCLUSIONS = {"failure", "cancelled", "timed_out", "startup_failure"}
_RUN_ID_IN_LINK = re.compile(r"/actions/runs/(\d+)")


@dataclass
class RunFailures:
    """Every distilled job failure for ONE run."""

    run_id: str
    workflow: str = ""
    title: str = ""
    branch: str = ""
    url: str = ""
    failures: list[JobFailure] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "workflow": self.workflow,
            "title": self.title,
            "branch": self.branch,
            "url": self.url,
            "failures": [f.to_dict() for f in self.failures],
        }


def _repo_args(repo: Optional[str]) -> list[str]:
    return ["-R", repo] if repo else []


def _current_branch(_run=subprocess.run) -> str:
    try:
        proc = _run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
    except (
        OSError
    ) as exc:  # stx-allow: fallback (reason: git missing → loud CIWhyError)
        raise CIWhyError(f"could not determine current branch: {exc}") from exc
    branch = (proc.stdout or "").strip()
    if proc.returncode != 0 or not branch:
        raise CIWhyError(
            "not in a git checkout (or detached HEAD) — pass a PR number, "
            "run id, or branch explicitly"
        )
    return branch


def _pr_failing_run_ids(pr: str, *, run_gh: GhRunner, repo: Optional[str]) -> list[str]:
    raw = run_gh(
        ["pr", "checks", pr, *_repo_args(repo), "--json", "name,state,link,bucket"]
    )
    try:
        checks = json.loads(raw or "[]")
    except ValueError as exc:
        raise CIWhyError(f"could not parse gh pr checks JSON for PR {pr}") from exc
    run_ids: list[str] = []
    for c in checks or []:
        failed = c.get("bucket") == "fail" or str(c.get("state", "")).upper() in {
            "FAILURE",
            "ERROR",
            "CANCELLED",
            "TIMED_OUT",
        }
        if not failed:
            continue
        m = _RUN_ID_IN_LINK.search(str(c.get("link", "")))
        if m and m.group(1) not in run_ids:
            run_ids.append(m.group(1))
    return run_ids


def _branch_latest_run_id(
    branch: str, *, run_gh: GhRunner, repo: Optional[str]
) -> list[str]:
    raw = run_gh(
        [
            "run",
            "list",
            *_repo_args(repo),
            "-b",
            branch,
            "-L",
            "1",
            "--json",
            "databaseId",
        ]
    )
    try:
        runs = json.loads(raw or "[]")
    except ValueError as exc:
        raise CIWhyError(
            f"could not parse gh run list JSON for branch {branch}"
        ) from exc
    if not runs:
        raise CIWhyError(f"no workflow runs found for branch '{branch}'")
    return [str(runs[0]["databaseId"])]


def resolve_run_ids(
    target: Optional[str], *, run_gh: GhRunner = run_gh, repo: Optional[str] = None
) -> list[str]:
    """Resolve a PR number / run id / branch / nothing to failing run id(s).

    * empty  -> the latest run for the current git branch;
    * a run id (>= 8 digits) -> itself;
    * a PR number -> the run id(s) behind its failing checks;
    * anything else -> the latest run for that branch name.
    """
    target = (target or "").strip()
    if not target:
        return _branch_latest_run_id(_current_branch(), run_gh=run_gh, repo=repo)
    if target.isdigit():
        if int(target) >= _RUN_ID_MIN:
            return [target]
        return _pr_failing_run_ids(target, run_gh=run_gh, repo=repo)
    return _branch_latest_run_id(target, run_gh=run_gh, repo=repo)


def explain_run(
    run_id: str, *, run_gh: GhRunner = run_gh, repo: Optional[str] = None
) -> RunFailures:
    """Fetch + distil every failing job of a single run."""
    meta_raw = run_gh(
        [
            "run",
            "view",
            str(run_id),
            *_repo_args(repo),
            "--json",
            "jobs,displayTitle,headBranch,conclusion,url,workflowName",
        ]
    )
    try:
        meta = json.loads(meta_raw or "{}")
    except ValueError as exc:
        raise CIWhyError(f"could not parse gh run view JSON for run {run_id}") from exc
    run = RunFailures(
        run_id=str(run_id),
        workflow=meta.get("workflowName", ""),
        title=meta.get("displayTitle", ""),
        branch=meta.get("headBranch", ""),
        url=meta.get("url", ""),
    )
    failing = [
        j
        for j in (meta.get("jobs") or [])
        if str(j.get("conclusion", "")).lower() in _FAILED_JOB_CONCLUSIONS
    ]
    if not failing:
        return run

    by_job = split_log_by_job(
        run_gh(["run", "view", str(run_id), *_repo_args(repo), "--log-failed"])
    )
    for j in failing:
        name = j.get("name", "")
        jf = parse_failed_log(by_job.get(name, ""), job_name=name, url=j.get("url", ""))
        if jf.signal == "none":
            steps = [
                s.get("name", "?")
                for s in (j.get("steps") or [])
                if str(s.get("conclusion", "")).lower() == "failure"
            ]
            if steps:
                jf.errors.append("failed step: " + ", ".join(steps))
        run.failures.append(jf)
    return run


def explain(
    target: Optional[str], *, run_gh: GhRunner = run_gh, repo: Optional[str] = None
) -> list[RunFailures]:
    """Resolve ``target`` and distil the failing run(s) behind it."""
    return [
        explain_run(rid, run_gh=run_gh, repo=repo)
        for rid in resolve_run_ids(target, run_gh=run_gh, repo=repo)
    ]


def render_text(run: RunFailures) -> str:
    """Render one run's failures as compact human text (per-job blocks)."""
    if not run.failures:
        return "no failures"
    out: list[str] = []
    for jf in run.failures:
        out.append(f"{jf.job}{jf.context()}")
        out.extend(f"  {line}" for line in jf.primary_lines())
        if jf.url:
            out.append(f"  -> {jf.url}")
    return "\n".join(out)
