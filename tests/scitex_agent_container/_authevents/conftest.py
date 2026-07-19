"""Real temp state for the auth-event-log suites — no mocks.

Every fixture is a real path on ``tmp_path`` that the production writer really
appends to and the test really reads back. The one "failure injection" is a
genuinely read-only directory: the operating system refuses the write, so the
fail-open contract is proved against a real refusal rather than a patched one
that could only ever agree with us.
"""

from __future__ import annotations

from pathlib import Path

import pytest

#: A fixed clock, matching the ``_authheal`` suites, so no test is time-flaky.
NOW = 1_800_000_000.0


@pytest.fixture
def event_log(tmp_path: Path) -> Path:
    """The auth-event log path. Absent = nothing has ever been recorded."""
    return tmp_path / "auth-events.jsonl"


@pytest.fixture
def denied_log(tmp_path: Path):
    """A log path the REAL writer genuinely cannot create. No mocks.

    The parent dir is read-only, so the append fails the way it would on a
    full disk or a revoked mount — the world says no; nothing is injected.
    """
    readonly = tmp_path / "readonly"
    readonly.mkdir()
    readonly.chmod(0o555)
    try:
        yield readonly / "auth-events.jsonl"
    finally:
        readonly.chmod(0o755)
