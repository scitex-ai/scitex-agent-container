"""Shared constants for the auth-event-log suites — no mocks.

Mirrors ``tests/scitex_agent_container/_authheal/_helpers.py`` so the two
packages share one clock: an event written by a pass suite and an event written
by a log suite carry the same timestamp, which keeps a cross-suite comparison
honest instead of accidentally time-dependent.
"""

from __future__ import annotations

#: A fixed clock. Every suite injects it, so no test can be flaky on time.
NOW = 1_800_000_000.0

#: What :data:`NOW` renders as on a record. Written out literally rather than
#: recomputed from :data:`NOW` with the same call the production code makes —
#: a test that derives its expectation the way the code derives its answer
#: agrees with the code by construction and can never catch it being wrong.
NOW_ISO = "2027-01-15T08:00:00+00:00"
