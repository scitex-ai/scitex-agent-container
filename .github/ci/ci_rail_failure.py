"""What actually broke: pulling the first useful failure lines out of a
run's logs so a red verdict names something to look at.

WHY THIS EXISTS. A notification that reaches a human and says only "Red.
Fix and push" has solved half the problem. Seven consecutive red nights
on this fleet reached nobody; the cure for that is not merely arriving,
it is arriving with the failing test named. Tonight's own red would have
read as "Red." when the useful content was five tests asserting a
capability that a dependency release had just added.

WHY IT READS THE **JOB** LOG, NOT THE RUN LOG. The verdict job runs while
its own run is still in progress, and ``/runs/{id}/logs`` returns an
EMPTY archive for an in-progress run. A completed job's log is available
immediately, so this walks the run's jobs and reads the logs of the ones
that already failed.

THE 0-BYTE TRAP, WHICH IS THE REASON THIS MODULE IS CAREFUL. An empty log
greps exactly like a clean one: both yield no matches and exit 0. So does
a log that could not be fetched at all. Every one of those is reported
here as an explicit statement about the LOG rather than as silence about
the CODE — see :func:`summarize_failures`. A "no failures found" that is
indistinguishable from "never looked" is the bug this rail exists to
delete, and it would be especially perverse to reintroduce it in the code
whose job is to say what went wrong.
"""

from __future__ import annotations

import gzip
import io
import json
import os
import re
import urllib.error
import urllib.request
import zipfile

GITHUB_API = "https://api.github.com"

# Ordered by how much a reader learns from a hit. pytest's own summary
# line names the test; the `E ` lines carry the assertion; a traceback's
# last line names the exception. Collection errors and import failures
# come last because they are usually consequences of the first three.
FAILURE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^FAILED\s+\S+.*$"),
    re.compile(r"^ERROR\s+\S+.*$"),
    re.compile(r"^E\s{3}\s*\S.*$"),
    re.compile(r"^\s*(?:[A-Za-z_.]+)?(?:Error|Exception|AssertionError):.*$"),
)

# GitHub prefixes every log line with an ISO timestamp and a space.
_TS = re.compile(r"^\d{4}-\d{2}-\d{2}T[\d:.]+Z\s")

MAX_LINES = 4
MAX_LINE_CHARS = 200

__all__ = ["extract_failure_lines", "summarize_failures"]


def _token() -> str | None:
    for var in ("GITHUB_TOKEN", "GH_TOKEN"):
        value = (os.environ.get(var) or "").strip()
        if value:
            return value
    return None


def _api(path: str, token: str, *, raw: bool = False, timeout: float = 20.0):
    req = urllib.request.Request(f"{GITHUB_API}{path}")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        payload = resp.read()
        if resp.info().get("Content-Encoding") == "gzip":
            payload = gzip.decompress(payload)
        if raw:
            return payload
        return json.loads(payload.decode("utf-8") or "{}")


def _decode_log(payload: bytes) -> str:
    """Job logs arrive as plain text, or occasionally as a zip archive."""
    if payload[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            names = archive.namelist()
            if not names:
                return ""
            return "\n".join(
                archive.read(name).decode("utf-8", "replace") for name in names[:4]
            )
    return payload.decode("utf-8", "replace")


def extract_failure_lines(log_text: str, *, limit: int = MAX_LINES) -> list[str]:
    """The most informative distinct failure lines, in priority order.

    Deduplicated because a parametrized test failing forty times produces
    forty near-identical lines, and a notification that is forty copies
    of one assertion is no more useful than one copy.
    """
    seen: set[str] = set()
    picked: list[str] = []
    for pattern in FAILURE_PATTERNS:
        for raw_line in log_text.splitlines():
            line = _TS.sub("", raw_line).strip()
            if not line or not pattern.match(line):
                continue
            line = line[:MAX_LINE_CHARS]
            if line in seen:
                continue
            seen.add(line)
            picked.append(line)
            if len(picked) >= limit:
                return picked
    return picked


def summarize_failures(repo: str, run_id: str) -> str:
    """One short block naming what failed, or WHY nothing could be named.

    Never returns an empty string. Every path -- no token, API refusal,
    a zero-byte log, a log with no recognisable failure line -- produces a
    sentence about the LOG. Silence here would read as "nothing was
    wrong", which is exactly the confusion that lets a red night pass
    unnoticed.
    """
    if not run_id:
        return "(no run id; cannot name the failure)"
    token = _token()
    if not token:
        return "(no GITHUB_TOKEN in the verdict job; cannot read the run's logs)"

    try:
        jobs = _api(f"/repos/{repo}/actions/runs/{run_id}/jobs?per_page=100", token)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return f"(could not list the run's jobs: {exc})"

    failed = [
        job
        for job in (jobs.get("jobs") or [])
        if job.get("conclusion") in ("failure", "timed_out")
    ]
    if not failed:
        return "(no failed job in this run reported a log to read)"

    names = ", ".join(str(job.get("name")) for job in failed[:3])
    for job in failed:
        try:
            payload = _api(
                f"/repos/{repo}/actions/jobs/{job.get('id')}/logs", token, raw=True
            )
        except (urllib.error.URLError, OSError) as exc:
            return f"failed: {names} (log fetch failed: {exc})"
        # THE BYTE COUNT IS CHECKED BEFORE THE GREP, not after. An empty
        # log and a clean log are indistinguishable downstream.
        if not payload:
            continue
        lines = extract_failure_lines(_decode_log(payload))
        if lines:
            body = "\n".join(f"  {line}" for line in lines)
            return f"failed: {names}\n{body}"
    return (
        f"failed: {names} (logs were empty or held no recognisable failure "
        "line — open the run to see why)"
    )


# EOF
