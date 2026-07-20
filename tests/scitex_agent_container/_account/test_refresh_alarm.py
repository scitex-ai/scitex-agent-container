"""Tests for ``_account.refresh_alarm`` — loud refresh-failure alerting.

INCIDENT 2026-07-10: the refresh timer collected failing results for
hours and told nobody. These tests lock the alerting contract: first
failure alerts once; repeats stay quiet; recovery re-arms; a failed
delivery is retried because nothing was recorded.

The DEFAULT delivery (:func:`_default_notify`) has two legs with
deliberately different failure semantics, and the last block here pins
that split. The RECORD in sac's own event log comes first and is what
makes the rail trustworthy — it depends on no lead, no network and no
other software — so only its failure raises, leaving the dedupe unmarked
for the next run. The lead ``blocker`` push on top is best-effort: on a
fleet with no ``lead:`` block there is nowhere to push, and that must not
re-page the operator on every one of the timer's ~12 daily runs.

No-mocks (PA-306), no monkeypatching: real JSON state files on tmp_path,
a real temp JSONL event log redirected through the documented
``SAC_EVENT_LOG`` env var, and a real ``config.yaml`` with no ``lead:``
block for the unconfigured-push leg. The injected delivery seam is a
plain recording closure (the module's documented ``notify`` injection
point). AAA marker comments; one assertion per test.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from scitex_agent_container._account.refresh_alarm import (
    SUBSYSTEM,
    _default_notify,
    alert_failed_refreshes,
)
from scitex_agent_container._events import EVENT_LOG_ENV, SUBJECT_DEGRADED, read_events

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


# --- the DEFAULT delivery: record first, then push best-effort --------------


@pytest.fixture
def no_lead_configured(tmp_path: Path, env_save_restore) -> Path:
    """A REAL config.yaml with no ``lead:`` block. No mocks.

    This is the fleet this host actually is (2026-07-11): the push leg has
    nowhere to go and fails for real, which is exactly the condition that
    must NOT re-page the operator on every one of the timer's ~12 daily runs.
    """
    cfg = tmp_path / "config.yaml"
    cfg.write_text("host:\n  aliases: {}\npeers: {}\n")
    env_save_restore.set("SCITEX_AGENT_CONTAINER_CONFIG", str(cfg))
    return cfg


def test_the_default_delivery_records_the_failure(
    tmp_path: Path, env_save_restore, no_lead_configured
) -> None:
    # Arrange — the record is what makes this rail trustworthy: it depends on
    # no lead, no network and no software sac does not own.
    log = tmp_path / "sac-events.jsonl"
    env_save_restore.set(EVENT_LOG_ENV, str(log))
    # Act
    _default_notify("acct-a", "summary line", "detail block")
    # Assert
    assert [e.event for e in read_events(log, subsystem=SUBSYSTEM)] == [
        SUBJECT_DEGRADED
    ]


def test_the_recorded_failure_names_the_account(
    tmp_path: Path, env_save_restore, no_lead_configured
) -> None:
    # Arrange
    log = tmp_path / "sac-events.jsonl"
    env_save_restore.set(EVENT_LOG_ENV, str(log))
    # Act
    _default_notify("acct-a", "summary line", "detail block")
    # Assert
    assert read_events(log)[0].subject == "acct-a"


def test_the_recorded_failure_is_labelled_an_account(
    tmp_path: Path, env_save_restore, no_lead_configured
) -> None:
    # Arrange — an account is not an agent; the subject_kind keeps the
    # populations apart in one shared log.
    log = tmp_path / "sac-events.jsonl"
    env_save_restore.set(EVENT_LOG_ENV, str(log))
    # Act
    _default_notify("acct-a", "summary line", "detail block")
    # Assert
    assert read_events(log)[0].subject_kind == "account"


def test_the_recorded_failure_carries_the_verdict(
    tmp_path: Path, env_save_restore, no_lead_configured
) -> None:
    # Arrange
    log = tmp_path / "sac-events.jsonl"
    env_save_restore.set(EVENT_LOG_ENV, str(log))
    # Act
    _default_notify("acct-a", "summary line", "detail block")
    # Assert
    assert read_events(log)[0].verdict == "refresh_failed"


def test_an_unconfigured_lead_push_does_not_raise(
    tmp_path: Path, env_save_restore, no_lead_configured
) -> None:
    # Arrange — THE dedupe guard. The durable record already succeeded, so a
    # push failure loses attention-now, not the fact. Raising here would leave
    # the dedupe unmarked and re-page on every run of a ~2h timer.
    log = tmp_path / "sac-events.jsonl"
    env_save_restore.set(EVENT_LOG_ENV, str(log))
    # Act
    _default_notify("acct-a", "summary line", "detail block")
    # Assert — reaching this line at all is the proof; the record still stands.
    assert read_events(log) != []


def test_an_unconfigured_lead_push_is_loud(
    tmp_path: Path, env_save_restore, no_lead_configured, capsys
) -> None:
    # Arrange — best-effort is not silent: nobody has been paged, and that
    # fact must reach stderr rather than being swallowed.
    env_save_restore.set(EVENT_LOG_ENV, str(tmp_path / "sac-events.jsonl"))
    # Act
    _default_notify("acct-a", "summary line", "detail block")
    # Assert
    assert "lead blocker push FAILED" in capsys.readouterr().err


def test_a_failed_record_raises_so_the_run_retries(
    tmp_path: Path, env_save_restore, no_lead_configured
) -> None:
    # Arrange — a REALLY read-only dir, so the append fails the way it would
    # on a broken host. Losing the record is the ONE failure that leaves sac
    # with no account of the outage at all, so it must reach the caller and
    # leave the dedupe unmarked.
    denied = tmp_path / "denied"
    denied.mkdir()
    denied.chmod(0o555)
    env_save_restore.set(EVENT_LOG_ENV, str(denied / "sac-events.jsonl"))
    raised: list[BaseException] = []
    try:
        # Act
        try:
            _default_notify("acct-a", "summary line", "detail block")
        except RuntimeError as exc:
            raised.append(exc)
        # Assert
        assert [type(e).__name__ for e in raised] == ["RuntimeError"]
    finally:
        denied.chmod(0o755)
