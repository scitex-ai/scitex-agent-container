"""`accounts refresh-quota-cache` must be able to reach its console helper.

The command imported `from .._helpers import system_msg`. From a module that
already lives in `cli_pkg/`, `..` is the PACKAGE ROOT, where no `_helpers`
exists — the helper is at `cli_pkg/_helpers/`. So the command raised

    ModuleNotFoundError: No module named 'scitex_agent_container._helpers'

WHY IT SURVIVED, and why this test is shaped the way it is: the bad import
sits INSIDE a function body, on the branch taken only when accounts ARE
stored. Importing the module never executes it, so an import-only test passes
against the broken code — I wrote that test first and it did exactly that.
The only test that catches this must actually REACH the branch, which means a
HOME with at least one stored account.

Consequence, measured on compute-04 2026-08-14: the usage cache could never be
refreshed, so `sac accounts list` kept serving a stale figure —
`ywatanabe-scitex-ai` showed 35% used while the account was at 92%. The fleet
believed it had most of a week's capacity that did not exist.

No mocks: a real temporary HOME with real files on disk, and the real Click
command invoked end to end.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterator

import pytest
from click.testing import CliRunner

from scitex_agent_container.cli_pkg._account_refresh_quota_cache import (
    account_refresh_quota_cache,
)


@pytest.fixture
def home_with_one_account(tmp_path: Path) -> Iterator[Path]:
    """A real HOME holding one stored account, restored afterwards.

    `os.environ` is written and put back by hand — the no-mocks rule bans
    `monkeypatch`, and production reads the real environment.
    """
    saved = os.environ.get("HOME")
    account = tmp_path / ".scitex" / "agent-container" / "accounts" / "acct-under-test"
    account.mkdir(parents=True)
    # Shape-valid but non-live: enough for the walk to find an account, so the
    # command reaches the reporting branch. The fetch itself is expected to
    # fail against it, which is fine — this asserts the branch RUNS.
    (account / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "not-a-live-token"}}),
        encoding="utf-8",
    )
    os.environ["HOME"] = str(tmp_path)
    try:
        yield tmp_path
    finally:
        if saved is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = saved


def test_the_reporting_branch_runs_without_a_missing_module_error(
    home_with_one_account: Path,
) -> None:
    """With an account stored, the command takes the branch that imports the
    console helper. A wrong relative level raises ModuleNotFoundError here."""
    # Arrange
    runner = CliRunner()

    # Act
    result = runner.invoke(account_refresh_quota_cache, [])

    # Assert
    assert not isinstance(result.exception, ModuleNotFoundError), result.output
