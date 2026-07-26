"""Tests for the per-agent token and cost counter."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from click.testing import CliRunner

from scitex_agent_container._account.openai_usage import record_usage
from scitex_agent_container._runners._session_quota import accumulate_quota
from scitex_agent_container.cli_pkg._agents_usage import (
    _fmt_jpy,
    agents_usage,
    build_usage_payload,
)


def _seed_usage(state_dir: Path, home: Path) -> None:
    state_dir.mkdir(parents=True)
    accumulate_quota(
        state_dir,
        {
            "input_tokens": 100,
            "output_tokens": 30,
            "cache_creation_input_tokens": 10,
            "cache_read_input_tokens": 5,
        },
        cost_usd=0.012345,
    )
    record_usage(
        {"requests": 1, "input_tokens": 200, "output_tokens": 50},
        model="gpt-4o-mini",
        agent="sales",
        home=home,
    )


def _seed_tui_usage(agent_home: Path) -> None:
    transcript = agent_home / ".claude" / "projects" / "-work" / "session.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        json.dumps(
            {
                "type": "assistant",
                "uuid": "assistant-1",
                "timestamp": "2026-07-26T12:48:50.048Z",
                "message": {
                    "model": "claude-opus-4-8",
                    "usage": {
                        "input_tokens": 20,
                        "output_tokens": 5,
                        "cache_read_input_tokens": 3,
                    },
                },
            }
        )
    )


def test_payload_totals_all_token_classes(tmp_path: Path) -> None:
    # Arrange
    state_dir = tmp_path / "runtime" / "sales"
    _seed_usage(state_dir, tmp_path)
    # Act
    payload = build_usage_payload("sales", state_dir=state_dir, home=tmp_path)
    # Assert
    assert payload["tokens"]["total"] == 395


def test_payload_combines_sdk_and_tui_tokens(tmp_path: Path) -> None:
    # Arrange
    state_dir = tmp_path / "runtime" / "sales"
    agent_home = tmp_path / "agent-home"
    _seed_usage(state_dir, tmp_path)
    _seed_tui_usage(agent_home)
    # Act
    payload = build_usage_payload(
        "sales",
        state_dir=state_dir,
        home=tmp_path,
        agent_home=agent_home,
    )
    # Assert
    assert payload["tokens"]["total"] == 423


def test_payload_reports_provider_cost(tmp_path: Path) -> None:
    # Arrange
    state_dir = tmp_path / "runtime" / "sales"
    _seed_usage(state_dir, tmp_path)
    # Act
    payload = build_usage_payload("sales", state_dir=state_dir, home=tmp_path)
    # Assert
    assert payload["cost"]["sdk_provider_reported_usd"] == 0.012345


def test_payload_reports_openai_estimate(tmp_path: Path) -> None:
    # Arrange
    state_dir = tmp_path / "runtime" / "sales"
    _seed_usage(state_dir, tmp_path)
    # Act
    payload = build_usage_payload("sales", state_dir=state_dir, home=tmp_path)
    # Assert
    assert payload["cost"]["openai_estimated_usd"] > 0.0


def test_payload_labels_cost_as_not_an_invoice(tmp_path: Path) -> None:
    # Arrange
    state_dir = tmp_path / "runtime" / "sales"
    _seed_usage(state_dir, tmp_path)
    # Act
    payload = build_usage_payload("sales", state_dir=state_dir, home=tmp_path)
    # Assert
    assert "not Claude Pro/Max subscription fees" in payload["note"]


def test_payload_reports_claude_estimate_in_usd(tmp_path: Path) -> None:
    # Arrange
    state_dir = tmp_path / "runtime" / "sales"
    agent_home = tmp_path / "agent-home"
    _seed_tui_usage(agent_home)
    # Act
    payload = build_usage_payload(
        "sales",
        state_dir=state_dir,
        home=tmp_path,
        agent_home=agent_home,
    )
    # Assert
    assert payload["cost"]["claude_api_estimated_usd"] == 0.0002265


def test_payload_converts_claude_estimate_to_jpy(tmp_path: Path) -> None:
    # Arrange
    state_dir = tmp_path / "runtime" / "sales"
    agent_home = tmp_path / "agent-home"
    _seed_tui_usage(agent_home)
    # Act
    payload = build_usage_payload(
        "sales",
        state_dir=state_dir,
        home=tmp_path,
        agent_home=agent_home,
        usd_jpy_rate=160.0,
    )
    # Assert
    assert payload["cost"]["claude_api_estimated_jpy"] == 0.04


def test_payload_reports_transcript_coverage_window(tmp_path: Path) -> None:
    # Arrange
    state_dir = tmp_path / "runtime" / "sales"
    agent_home = tmp_path / "agent-home"
    _seed_tui_usage(agent_home)
    # Act
    payload = build_usage_payload(
        "sales",
        state_dir=state_dir,
        home=tmp_path,
        agent_home=agent_home,
    )
    # Assert
    assert payload["coverage"]["first_observed_at"] == "2026-07-26T12:48:50.048Z"


def test_payload_filters_claude_tokens_by_period(tmp_path: Path) -> None:
    # Arrange
    state_dir = tmp_path / "runtime" / "sales"
    agent_home = tmp_path / "agent-home"
    _seed_tui_usage(agent_home)
    # Act
    payload = build_usage_payload(
        "sales",
        state_dir=state_dir,
        home=tmp_path,
        agent_home=agent_home,
        since="2026-07-26T12:00:00Z",
        until="2026-07-26T13:00:00Z",
    )
    # Assert
    assert payload["tokens"]["total"] == 28


def test_payload_period_is_half_open(tmp_path: Path) -> None:
    # Arrange
    state_dir = tmp_path / "runtime" / "sales"
    agent_home = tmp_path / "agent-home"
    _seed_tui_usage(agent_home)
    # Act
    payload = build_usage_payload(
        "sales",
        state_dir=state_dir,
        home=tmp_path,
        agent_home=agent_home,
        until="2026-07-26T12:48:50.048Z",
    )
    # Assert
    assert payload["tokens"]["total"] == 0


def test_payload_filters_openai_tokens_by_period(tmp_path: Path) -> None:
    # Arrange
    state_dir = tmp_path / "runtime" / "sales"
    record_usage(
        {"requests": 1, "input_tokens": 200, "output_tokens": 50},
        model="gpt-4o-mini",
        agent="sales",
        home=tmp_path,
        now=datetime(2026, 7, 26, 12, tzinfo=timezone.utc),
    )
    # Act
    payload = build_usage_payload(
        "sales",
        state_dir=state_dir,
        home=tmp_path,
        since="2026-07-26T00:00:00Z",
        until="2026-07-27T00:00:00Z",
    )
    # Assert
    assert payload["tokens"]["total"] == 250


def test_payload_rejects_reversed_period(tmp_path: Path) -> None:
    # Arrange
    state_dir = tmp_path / "runtime" / "sales"
    # Act / Assert
    try:
        build_usage_payload(
            "sales",
            state_dir=state_dir,
            home=tmp_path,
            since="2026-07-27T00:00:00Z",
            until="2026-07-26T00:00:00Z",
        )
    except ValueError as exc:
        assert "--since must be earlier" in str(exc)
    else:
        raise AssertionError("reversed usage period was accepted")


def test_payload_missing_cost_is_not_silent_zero(tmp_path: Path) -> None:
    # Arrange
    state_dir = tmp_path / "runtime" / "ghost"
    # Act
    payload = build_usage_payload("ghost", state_dir=state_dir, home=tmp_path)
    # Assert
    assert payload["cost"]["sdk_provider_reported_usd"] is None


def test_payload_missing_openai_estimate_is_not_silent_zero(
    tmp_path: Path,
) -> None:
    # Arrange
    state_dir = tmp_path / "runtime" / "ghost"
    # Act
    payload = build_usage_payload("ghost", state_dir=state_dir, home=tmp_path)
    # Assert
    assert payload["cost"]["openai_estimated_usd"] is None


def test_usage_json_has_stable_agent_name() -> None:
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(agents_usage, ["ghost", "--json"])
    payload = json.loads(result.output)
    # Assert
    assert payload["agent"] == "ghost"


def test_usage_human_calls_it_cost_not_fee() -> None:
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(agents_usage, ["ghost"])
    # Assert
    assert "provider-reported cost" in result.output


def test_usage_human_marks_missing_cost_unavailable() -> None:
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(agents_usage, ["ghost"])
    # Assert
    assert "unavailable" in result.output


def test_usage_human_labels_unknown_coverage() -> None:
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(agents_usage, ["ghost"])
    # Assert
    assert "First observed (UTC)" in result.output


def test_usage_human_displays_requested_period() -> None:
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(
        agents_usage,
        ["ghost", "--since", "2026-07-26T00:00:00Z"],
    )
    # Assert
    assert "Period start (UTC, inclusive)" in result.output


def test_usage_rejects_last_with_explicit_period() -> None:
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(
        agents_usage,
        ["ghost", "--last", "1h", "--since", "2026-07-26"],
    )
    # Assert
    assert result.exit_code == 2


def test_usage_rejects_invalid_period_timestamp() -> None:
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(agents_usage, ["ghost", "--since", "yesterdayish"])
    # Assert
    assert result.exit_code == 2


def test_jpy_display_uses_conventional_half_up_rounding() -> None:
    # Arrange
    value = 644_108.5
    # Act
    rendered = _fmt_jpy(value)
    # Assert
    assert rendered == "¥644,109"
