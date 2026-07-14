"""Real evidence, recorded from the real systems on 2026-07-14.

NOT fixtures invented to make tests pass. Every value here was captured
from production:

* ``PYPI_RELEASES`` — ``GET https://pypi.org/pypi/scitex-agent-container/json``
* ``GIT_TAGS``      — ``git tag -l 'v*'`` in the repo
* ``RELEASE_RUNS``  — ``gh run list --workflow pypi-publish-and-github-release-on-tag.yml --json ...``

Which is why the numbers are ugly: the real 0.21 line is missing SIX of
its eighteen tags on PyPI (v0.21.6, .8, .10, .12, .15, .16 never
published). The operator knew about two of them. Tests written against
tidy invented data would never have shown that, and the check would have
been tuned against a world that does not exist.

The ``AT_INCIDENT_*`` values are the same systems as they stood at
2026-07-13 ~23:30 UTC — the moment v0.21.16's release run failed and the
fleet went a full day believing a merged fix was live. They exist so the
regression test can ask the only question that matters:

    would this alarm have fired, then, on that data?
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# NOW (2026-07-14): v0.21.17 published, six older ghosts behind it.
# ---------------------------------------------------------------------------

# Every version PyPI has actually published, in full. Recorded rather
# than trimmed: an earlier draft of this file carried only the 0.21.x
# slice, and the ghost-tag check then (correctly) flagged all of v0.17-
# v0.20 as ghosts. The test failed, the data was wrong, and fixing it
# surfaced a SEVENTH ghost nobody had noticed — v0.17.1. Trimmed fixtures
# hide exactly the thing this check exists to find.
PYPI_RELEASES = {
    "0.1.0", "0.2.0",
    "0.3.0", "0.3.1", "0.3.2", "0.3.3",
    "0.4.0", "0.4.1", "0.4.2",
    "0.5.0", "0.7.0", "0.7.1", "0.9.0", "0.9.1",
    "0.10.0", "0.10.1", "0.10.2", "0.10.3", "0.10.4", "0.10.5", "0.10.6", "0.10.7",
    "0.12.0", "0.13.0", "0.14.0", "0.15.0", "0.16.0",
    # NOTE: 0.17.1 is absent. Tag v0.17.1 exists. Ghost #7.
    "0.17.0", "0.17.2", "0.17.3",
    "0.18.0", "0.19.0", "0.20.0",
    "0.21.0", "0.21.1", "0.21.2", "0.21.3", "0.21.4", "0.21.5",
    # 0.21.6 absent — ghost.   0.21.8 absent — ghost.
    "0.21.7",
    # 0.21.10 absent — ghost.  0.21.12 absent — ghost.
    "0.21.9", "0.21.11", "0.21.13", "0.21.14",
    # 0.21.15 absent — ghost (run CANCELLED).
    # 0.21.16 absent — ghost (run FAILED). This is the incident.
    "0.21.17",
}

PYPI_LATEST = "0.21.17"

# Every tag that never reached PyPI. EIGHT, not the two anyone remembered
# — confirmed against the live systems on 2026-07-14, and the live `sac
# freshness refresh` reports exactly this set.
ALL_GHOSTS = {
    "v0.6.0",
    "v0.17.1",
    "v0.21.6",
    "v0.21.8",
    "v0.21.10",
    "v0.21.12",
    "v0.21.15",
    "v0.21.16",
}

# The COMPLETE tag list — `git tag -l 'v*'`, all 44, nothing trimmed.
# An earlier draft kept only the tail of this list, which quietly hid two
# ghosts (v0.6.0 and v0.17.1). A fixture that is a convenient subset of
# reality is how a check gets tuned against a world that does not exist.
GIT_TAGS = [
    "v0.5.0", "v0.6.0", "v0.7.0", "v0.7.1", "v0.9.0", "v0.9.1",
    "v0.10.0", "v0.10.1", "v0.10.2", "v0.10.3",
    "v0.10.4", "v0.10.5", "v0.10.6", "v0.10.7",
    "v0.12.0", "v0.13.0", "v0.14.0", "v0.15.0", "v0.16.0",
    "v0.17.0", "v0.17.1", "v0.17.2", "v0.17.3",
    "v0.18.0", "v0.19.0", "v0.20.0",
    "v0.21.0", "v0.21.1", "v0.21.2", "v0.21.3", "v0.21.4",
    "v0.21.5", "v0.21.6", "v0.21.7", "v0.21.8", "v0.21.9",
    "v0.21.10", "v0.21.11", "v0.21.12", "v0.21.13", "v0.21.14",
    "v0.21.15", "v0.21.16", "v0.21.17",
]

# The two the operator named. Both real, both never published.
KNOWN_GHOSTS = ("v0.21.15", "v0.21.16")

RELEASE_RUNS = [
    {
        "conclusion": "success",
        "status": "completed",
        "headBranch": "v0.21.17",
        "createdAt": "2026-07-14T01:37:33Z",
        "url": "https://github.com/scitex-ai/scitex-agent-container/actions/runs/29299020678",
    },
    {
        "conclusion": "failure",
        "status": "completed",
        "headBranch": "v0.21.16",
        "createdAt": "2026-07-13T23:24:19Z",
        "url": "https://github.com/scitex-ai/scitex-agent-container/actions/runs/29292943090",
    },
    {
        "conclusion": "cancelled",
        "status": "completed",
        "headBranch": "v0.21.15",
        "createdAt": "2026-07-13T20:58:50Z",
        "url": "https://github.com/scitex-ai/scitex-agent-container/actions/runs/29284554656",
    },
    {
        "conclusion": "success",
        "status": "completed",
        "headBranch": "v0.21.14",
        "createdAt": "2026-07-13T15:18:40Z",
        "url": "https://github.com/scitex-ai/scitex-agent-container/actions/runs/29261686336",
    },
]

# What the host actually had installed while all of the above was true.
HOST_INSTALLED = "0.21.14"


# ---------------------------------------------------------------------------
# AT THE INCIDENT (2026-07-13 ~23:30 UTC): v0.21.16 tagged, run FAILED,
# PyPI still on 0.21.14, host on 0.21.14. Nothing alarmed.
# ---------------------------------------------------------------------------

AT_INCIDENT_PYPI_RELEASES = PYPI_RELEASES - {"0.21.17"}
AT_INCIDENT_PYPI_LATEST = "0.21.14"
AT_INCIDENT_GIT_TAGS = [t for t in GIT_TAGS if t != "v0.21.17"]
AT_INCIDENT_RELEASE_RUNS = [
    r for r in RELEASE_RUNS if r["headBranch"] != "v0.21.17"
]

# EOF
