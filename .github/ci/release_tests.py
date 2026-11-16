"""Release test witness: the shared pytest-matrix legs of THIS release run.

The release workflow calls the org-wide shared matrix (same callee and pin
as ci.yml). This witness proves two things before anything is built:

(a) the run resolved exactly that callee, at the revision pinned in the
    release workflow AT THE RELEASED COMMIT (read through the API, never
    from the runner's checkout), and
(b) its three legs, on this commit, run and attempt, completed success.

Loaded by path from release-identity.py, which runs under ``python -I``.
"""

import base64
import re

TEST_CALLEE = "scitex-ai/.github/.github/workflows/pytest-matrix.yml"
TEST_LEG_PREFIX = "test / pytest-matrix-on-ubuntu-py"
TEST_VERSIONS = ("3.11", "3.12", "3.13")
RELEASE_WORKFLOW = ".github/workflows/pypi-publish-and-github-release-on-tag.yml"
JOB_PAGE_LIMIT = 100


def pinned_callee(repository, commit, query):
    """Return the single shared-matrix pin the release workflow declares."""
    content = query(
        "repos/" + repository + "/contents/" + RELEASE_WORKFLOW + "?ref=" + commit
    )
    if content.get("encoding") != "base64":
        raise ValueError("unsupported release workflow encoding")
    raw = base64.b64decode(content["content"].replace("\n", ""), validate=True)
    pins = set(
        re.findall(
            r"^\s*uses:\s*" + re.escape(TEST_CALLEE) + r"@([0-9a-f]{40})\s*$",
            raw.decode(),
            re.MULTILINE,
        )
    )
    if len(pins) != 1:
        raise ValueError("release workflow does not pin exactly one test callee")
    return pins.pop()


def used_callee(current, pin):
    """Check the run resolved the pinned callee, and only that revision."""
    used = [
        row
        for row in current.get("referenced_workflows") or []
        if row.get("path", "").split("@", 1)[0] == TEST_CALLEE
    ]
    if (
        len(used) != 1
        or used[0].get("path") != TEST_CALLEE + "@" + pin
        or used[0].get("sha") != pin
    ):
        raise ValueError("release run did not use the pinned test callee")
    return {"path": TEST_CALLEE, "sha": pin}


def succeeded_legs(jobs_document, commit, run, attempt):
    """Return the three matrix legs, each completed success on this run."""
    jobs = jobs_document.get("jobs", [])
    if (
        type(jobs_document.get("total_count")) is not int
        or jobs_document["total_count"] != len(jobs)
        or len(jobs) > JOB_PAGE_LIMIT
    ):
        raise ValueError("release job inventory is incomplete")
    completed = []
    for version in TEST_VERSIONS:
        name = TEST_LEG_PREFIX + version
        rows = [row for row in jobs if row.get("name") == name]
        if (
            len(rows) != 1
            or rows[0].get("status") != "completed"
            or rows[0].get("conclusion") != "success"
            or rows[0].get("head_sha") != commit
            or rows[0].get("run_id") != int(run)
            or rows[0].get("run_attempt") != int(attempt)
            or type(rows[0].get("id")) is not int
        ):
            raise ValueError("all three genuine release test legs have not succeeded")
        completed.append({"version": version, "job": rows[0]["id"], "name": name})
    return completed


def witness(repository, commit, run, attempt, current, prefix, query):
    """Return ``{"test_callee": ..., "tests": [...]}`` for the release proof."""
    callee = used_callee(current, pinned_callee(repository, commit, query))
    document = query(prefix + "/jobs?per_page=" + str(JOB_PAGE_LIMIT))
    return {
        "test_callee": callee,
        "tests": succeeded_legs(document, commit, run, attempt),
    }
