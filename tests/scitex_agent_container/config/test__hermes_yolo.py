"""Tests for the Hermes yolo spec flag (operator order 2026-09-29)."""

from __future__ import annotations

import pytest

from scitex_agent_container.config import AgentConfig
from scitex_agent_container.config._harness_callables import (
    _hermes_tui_inner_argv,
)
from scitex_agent_container.config._hermes_yolo import parse_selected_hermes_yolo


def _spec(harness: str = "hermes", **harness_fields: object) -> dict:
    return {
        "harness": harness,
        "available_harnesses": {"hermes": {"yolo": True, **harness_fields}},
    }


@pytest.mark.parametrize(
    "spec",
    [{"harness": "hermes"}, {"harness": "hermes", "available_harnesses": {"hermes": {}}}],
)
def test_yolo_defaults_off(spec) -> None:
    # Arrange
    # Act
    enabled = parse_selected_hermes_yolo(spec)
    # Assert
    assert enabled is False


def test_yolo_reads_hermes_harness_block() -> None:
    # Arrange
    # Act
    # Assert
    assert parse_selected_hermes_yolo(_spec()) is True


def test_yolo_ignored_off_hermes() -> None:
    # Arrange
    # Act
    # Assert
    assert parse_selected_hermes_yolo(_spec(harness="openai")) is False


def _argv_config() -> AgentConfig:
    cfg = AgentConfig(name="probe")
    cfg.model = "muse-spark-1.3-contributor"
    cfg.engine_key = "probe-engine"
    return cfg


def test_argv_carries_yolo_flag_when_set() -> None:
    # Arrange
    cfg = _argv_config()
    # Act
    cfg.hermes_yolo = True
    # Assert
    assert "--yolo" in _hermes_tui_inner_argv(cfg)


def test_argv_omits_yolo_flag_by_default() -> None:
    # Arrange
    # Act
    # Assert
    assert "--yolo" not in _hermes_tui_inner_argv(_argv_config())
