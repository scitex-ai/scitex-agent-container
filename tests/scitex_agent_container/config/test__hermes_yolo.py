"""Tests for the Hermes yolo spec flag (operator order 2026-09-29)."""

from __future__ import annotations

import pytest
import yaml

from scitex_agent_container.config import AgentConfig, load_config
from scitex_agent_container.config._harness_callables import (
    _hermes_tui_inner_argv,
)
from scitex_agent_container.config._hermes_yolo import parse_selected_hermes_yolo
from scitex_agent_container.config._schema_compat import canonical_surface_errors
from tests.scitex_agent_container._helpers.explicit_spec import explicit_doc


def _spec(harness: str = "hermes", **harness_fields: object) -> dict:
    return {
        "harness": harness,
        "available_harnesses": {"hermes": {"yolo": True, **harness_fields}},
    }


@pytest.mark.parametrize(
    "spec",
    [
        {"harness": "hermes"},
        {"harness": "hermes", "available_harnesses": {"hermes": {}}},
    ],
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


def _authored_spec(yolo: object) -> dict:
    raw = explicit_doc(
        {
            "runtime": "tui",
            "harness": "hermes",
            "engine": "muse",
            "comms": {"channels": []},
            "available_harnesses": {
                "hermes": {
                    "session": {"mode": "continue", "max_age_minutes": None},
                    "yolo": yolo,
                }
            },
            "available_engines": {
                "muse": {
                    "model": "muse-spark-1.3-contributor",
                    "provider": {
                        "base_url": "http://synthetic.invalid/v1",
                        "auth_token_env": "SYNTHETIC_KEY",
                    },
                }
            },
        }
    )
    for legacy in ("engines", "claude", "watchdog", "container"):
        raw["spec"].pop(legacy, None)
    return raw


def test_authored_yolo_true_validates_and_sets_hermes_yolo(
    tmp_path, env_save_restore
) -> None:
    # Arrange -- schema and parser must agree: the validator used to reject
    # the very field parse_selected_hermes_yolo reads.
    env_save_restore.set("SAC_ENGINES_FILE", str(tmp_path / "no-fleet-engines.yaml"))
    path = tmp_path / "lead" / "spec.yaml"
    path.parent.mkdir()
    path.write_text(yaml.safe_dump(_authored_spec(True)))
    # Act
    config = load_config(path)
    # Assert
    assert config.hermes_yolo is True


@pytest.mark.parametrize("value", ["true", 1, None])
def test_authored_non_boolean_yolo_is_refused(value) -> None:
    # Arrange
    raw = _authored_spec(value)
    # Act
    errors = canonical_surface_errors(raw)
    # Assert
    assert "spec.available_harnesses.hermes.yolo must be a boolean" in errors


def test_yolo_on_a_non_hermes_harness_is_refused() -> None:
    # Arrange
    raw = {
        "spec": {
            "harness": "opencode",
            "runtime": "tui",
            "comms": {"channels": []},
            "available_harnesses": {
                "opencode": {
                    "session": {"mode": "continue", "max_age_minutes": None},
                    "yolo": True,
                }
            },
        }
    }
    # Act
    errors = canonical_surface_errors(raw)
    # Assert
    assert errors == [
        "spec.available_harnesses.opencode.yolo is only valid for the Hermes harness"
    ]
