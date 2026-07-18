"""Real temp state + a RECORDED gh response for the PR-lifecycle suites.

No mocks (repo rule). Every fixture hands the pass something REAL:

* ``store`` — an on-disk scitex-todo store the pass genuinely writes to and the
  test reads back with the real ``scitex_todo`` API.
* ``recorded_gh`` — an actual ``gh pr list --json ...`` payload captured from
  ``scitex-ai/scitex-agent-container`` on 2026-07-18 and committed under
  ``fixtures/``. Not a hand-built dict: a hand-built fixture encodes what we
  BELIEVE gh returns, and the belief is the thing most likely to be wrong.
  (This one immediately corrected one: an IN_PROGRESS CheckRun carries an
  EMPTY ``conclusion``, so conclusion alone cannot tell pending from success.)

The ``gh`` seam is a plain callable returning a real
:class:`.._prlifecycle._gh.GhInvocation`, so tests drive the production
classifier and parser end to end — only the subprocess is replaced, and it is
replaced by a RECORDING of one, not by a stand-in for one.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scitex_agent_container._prlifecycle._gh import GhInvocation

FIXTURES = Path(__file__).parent / "fixtures"

#: The repo the recorded response was captured from.
RECORDED_REPO = "scitex-ai/scitex-agent-container"


@pytest.fixture
def store(tmp_path: Path) -> str:
    """A real (initially absent) scitex-todo store path — no mocks."""
    return str(tmp_path / "tasks.yaml")


@pytest.fixture
def recorded_payload() -> str:
    """The raw stdout of a real ``gh pr list --json ...`` call."""
    return (FIXTURES / "gh_pr_list_open.json").read_text()


@pytest.fixture
def recorded_rows(recorded_payload: str) -> list:
    return json.loads(recorded_payload)


@pytest.fixture
def recorded_gh(recorded_payload: str):
    """A runner replaying the recorded response — exit 0, real payload."""

    def _run(args: list) -> GhInvocation:
        return GhInvocation(returncode=0, stdout=recorded_payload)

    return _run


def gh_failing(
    *, returncode: int = 1, stderr: str = "", stdout: str = "", spawn_error: str = ""
):
    """A runner reproducing a REAL ``gh`` failure mode (verbatim stderr)."""

    def _run(args: list) -> GhInvocation:
        return GhInvocation(
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
            spawn_error=spawn_error,
        )

    return _run


def gh_returning(payload: str, *, returncode: int = 0):
    """A runner returning an exact stdout string on the given exit code."""

    def _run(args: list) -> GhInvocation:
        return GhInvocation(returncode=returncode, stdout=payload)

    return _run
