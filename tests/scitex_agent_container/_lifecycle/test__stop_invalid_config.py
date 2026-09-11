from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from scitex_agent_container._lifecycle._stop import _load_config_for_teardown
from scitex_agent_container.config import load_config
from tests.scitex_agent_container._lifecycle.test_lifecycle import _write_spec


def _add_launch_field_unknown_to_current_schema(spec: Path) -> None:
    raw = yaml.safe_load(spec.read_text())
    raw["spec"]["future_launch_capability"] = {"enabled": True}
    spec.write_text(yaml.safe_dump(raw, sort_keys=False))


def test_current_launch_loader_rejects_unknown_field(
    tmp_path: Path,
) -> None:
    # Arrange
    spec = _write_spec(tmp_path)
    _add_launch_field_unknown_to_current_schema(spec)

    # Act
    # Assert
    with pytest.raises(ValueError, match="Unknown spec field"):
        load_config(spec)


def test_teardown_loader_accepts_spec_rejected_by_current_launch_rules(
    tmp_path: Path,
) -> None:
    # Arrange
    spec = _write_spec(tmp_path)
    _add_launch_field_unknown_to_current_schema(spec)

    # Act
    config = _load_config_for_teardown(spec, "alpha")

    # Assert
    assert (config.name, config.runtime) == ("alpha", "apptainer")


def test_teardown_loader_rejects_registry_name_mismatch(tmp_path: Path) -> None:
    # Arrange
    spec = _write_spec(tmp_path)
    _add_launch_field_unknown_to_current_schema(spec)

    # Act
    # Assert
    with pytest.raises(ValueError, match="resolves to spec for 'alpha'"):
        _load_config_for_teardown(spec, "different-agent")
