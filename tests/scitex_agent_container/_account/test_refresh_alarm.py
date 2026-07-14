"""Tests for ``_account.refresh_alarm`` — loud refresh-failure alerting.

INCIDENT 2026-07-10: the refresh timer collected failing results for
hours and told nobody. These tests lock the alerting contract: first
failure alerts once; repeats stay quiet; recovery re-arms; a failed
delivery is retried because nothing was recorded.

No-mocks (PA-306): real JSON state files on tmp_path; the delivery seam
is a plain recording closure (the module's documented ``notify``
injection point). AAA marker comments; one assertion per test.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

from scitex_agent_container._account.refresh_alarm import alert_failed_refreshes

_ERROR_TEXT = (
    "token endpoint https://platform.claude.com/v1/oauth/token did not "
    "evaluate the grant (HTTP 404) — NOT a token problem"
)


def _failure(name: str) -> dict:
    return {
        "name": name,
        "success": False,
        "skipped": False,
        "error": _ERROR_TEXT,
        "failure_kind": "transport",
        "credentials_path": f"/store/{name}/.credentials.json",
    }


def _success(name: str) -> dict:
    return {
        "name": name,
        "success": True,
        "skipped": False,
        "error": None,
        "failure_kind": None,
        "credentials_path": f"/store/{name}/.credentials.json",
    }


def _recorder():
    sent: list[tuple[str, str, str]] = []

    def notify(account: str, summary: str, detail: str) -> None:
        sent.append((account, summary, detail))

    return sent, notify


def test_first_failure_sends_exactly_one_alert(tmp_path: Path) -> None:
    # Arrange
    sent, notify = _recorder()
    state = tmp_path / "state.json"
    # Act
    alert_failed_refreshes([_failure("acct-a")], state_path=state, notify=notify)
    # Assert
    assert len(sent) == 1


def test_alert_summary_names_the_account(tmp_path: Path) -> None:
    # Arrange
    sent, notify = _recorder()
    state = tmp_path / "state.json"
    # Act
    alert_failed_refreshes([_failure("acct-a")], state_path=state, notify=notify)
    # Assert
    assert "acct-a" in sent[0][1]


def test_alert_summary_carries_the_error_text(tmp_path: Path) -> None:
    # Arrange
    sent, notify = _recorder()
    state = tmp_path / "state.json"
    # Act
    alert_failed_refreshes([_failure("acct-a")], state_path=state, notify=notify)
    # Assert
    assert "NOT a token problem" in sent[0][1]


def test_alert_summary_includes_login_recovery_line(tmp_path: Path) -> None:
    # Arrange
    sent, notify = _recorder()
    state = tmp_path / "state.json"
    # Act
    alert_failed_refreshes([_failure("acct-a")], state_path=state, notify=notify)
    # Assert
    assert "`claude /login` as acct-a" in sent[0][1]


def test_alert_summary_includes_accounts_save_recovery_line(
    tmp_path: Path,
) -> None:
    # Arrange
    sent, notify = _recorder()
    state = tmp_path / "state.json"
    # Act
    alert_failed_refreshes([_failure("acct-a")], state_path=state, notify=notify)
    # Assert
    assert "`sac accounts save acct-a`" in sent[0][1]


def test_alert_detail_carries_the_failure_kind(tmp_path: Path) -> None:
    # Arrange — the error CLASS must ride in the alert so the operator
    # knows whether re-login can even help (INCIDENT 2026-07-10).
    sent, notify = _recorder()
    state = tmp_path / "state.json"
    # Act
    alert_failed_refreshes([_failure("acct-a")], state_path=state, notify=notify)
    # Assert
    assert "failure_kind: transport" in sent[0][2]


def test_repeat_failure_does_not_realert(tmp_path: Path) -> None:
    # Arrange — the ~2h timer must not page the operator 12x/day for the
    # same already-alerted dead account.
    sent, notify = _recorder()
    state = tmp_path / "state.json"
    alert_failed_refreshes([_failure("acct-a")], state_path=state, notify=notify)
    # Act
    alert_failed_refreshes([_failure("acct-a")], state_path=state, notify=notify)
    # Assert
    assert len(sent) == 1


def test_recovery_then_failure_alerts_again(tmp_path: Path) -> None:
    # Arrange — an account that recovers and later dies again is a NEW
    # outage and must page again.
    sent, notify = _recorder()
    state = tmp_path / "state.json"
    alert_failed_refreshes([_failure("acct-a")], state_path=state, notify=notify)
    alert_failed_refreshes([_success("acct-a")], state_path=state, notify=notify)
    # Act
    alert_failed_refreshes([_failure("acct-a")], state_path=state, notify=notify)
    # Assert
    assert len(sent) == 2


def test_skipped_fresh_result_clears_the_dedupe_entry(tmp_path: Path) -> None:
    # Arrange — a skipped-still-fresh result proves the account is alive
    # again (the gate only skips fresh tokens), so the alarm re-arms.
    sent, notify = _recorder()
    state = tmp_path / "state.json"
    alert_failed_refreshes([_failure("acct-a")], state_path=state, notify=notify)
    skipped = {"name": "acct-a", "success": None, "skipped": True, "error": None}
    # Act
    alert_failed_refreshes([skipped], state_path=state, notify=notify)
    # Assert
    assert "acct-a" not in json.loads(state.read_text())


def test_failed_delivery_is_not_recorded_so_next_run_retries(
    tmp_path: Path,
) -> None:
    # Arrange — run 1's delivery rail is down (raises); nothing must be
    # recorded so run 2 retries and succeeds.
    state = tmp_path / "state.json"

    def broken(account: str, summary: str, detail: str) -> None:
        raise RuntimeError("rail down")

    alert_failed_refreshes(
        [_failure("acct-a")],
        state_path=state,
        notify=broken,
        err_stream=io.StringIO(),
    )
    sent, notify = _recorder()
    # Act
    alert_failed_refreshes([_failure("acct-a")], state_path=state, notify=notify)
    # Assert
    assert len(sent) == 1


def test_failed_delivery_prints_loud_stderr_line(tmp_path: Path) -> None:
    # Arrange
    state = tmp_path / "state.json"
    stream = io.StringIO()

    def broken(account: str, summary: str, detail: str) -> None:
        raise RuntimeError("rail down")

    # Act
    alert_failed_refreshes(
        [_failure("acct-a")], state_path=state, notify=broken, err_stream=stream
    )
    # Assert
    assert "ALERT DELIVERY FAILED" in stream.getvalue()


def test_alerted_account_names_are_returned(tmp_path: Path) -> None:
    # Arrange
    _, notify = _recorder()
    state = tmp_path / "state.json"
    # Act
    alerted = alert_failed_refreshes(
        [_failure("acct-a"), _success("acct-b")], state_path=state, notify=notify
    )
    # Assert
    assert alerted == ["acct-a"]
