"""Named launch-profile validation and materialization tests."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from scitex_agent_container.config import load_config
from scitex_agent_container.config._validation import validate_raw
from tests.scitex_agent_container._helpers.explicit_spec import explicit_doc


def _profiled_doc() -> dict:
    doc = explicit_doc()
    claude = doc["spec"].pop("claude")
    anthropic = copy.deepcopy(claude)
    anthropic["model"] = "opus[1m]"
    codex = copy.deepcopy(claude)
    codex["model"] = "gpt-5.6-sol"
    codex["provider"] = "codex"
    doc["spec"]["default_profile"] = "claude-code"
    doc["spec"]["profiles"] = {
        "claude-code": {
            "harness": "claude-code",
            "claude": anthropic,
        },
        "codex": {
            "harness": "claude-code",
            "claude": codex,
        },
    }
    return doc


def _write_doc(tmp_path: Path, doc: dict) -> Path:
    agent_dir = tmp_path / "sales"
    agent_dir.mkdir()
    path = agent_dir / "spec.yaml"
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    return path


def test_load_config_selects_requested_profile(tmp_path: Path) -> None:
    # Arrange
    path = _write_doc(tmp_path, _profiled_doc())
    # Act
    config = load_config(path, profile="codex")
    # Assert
    assert config.profile == "codex"


def test_load_config_materializes_selected_profile_model(tmp_path: Path) -> None:
    # Arrange
    path = _write_doc(tmp_path, _profiled_doc())
    # Act
    config = load_config(path, profile="codex")
    # Assert
    assert config.claude.model == "gpt-5.6-sol"


def test_load_config_reports_selected_backend(tmp_path: Path) -> None:
    # Arrange
    path = _write_doc(tmp_path, _profiled_doc())
    # Act
    config = load_config(path, profile="codex")
    # Assert
    assert config.backend == "codex"


def test_load_config_uses_declared_default_profile(tmp_path: Path) -> None:
    # Arrange
    path = _write_doc(tmp_path, _profiled_doc())
    # Act
    config = load_config(path)
    # Assert
    assert config.profile == "claude-code"


def test_load_config_rejects_unknown_requested_profile(tmp_path: Path) -> None:
    # Arrange
    path = _write_doc(tmp_path, _profiled_doc())
    # Act
    ctx = pytest.raises(ValueError, match="Available profiles: claude-code, codex")
    # Assert
    with ctx:
        load_config(path, profile="missing")


def test_load_config_rejects_profile_request_for_legacy_spec(tmp_path: Path) -> None:
    # Arrange
    path = _write_doc(tmp_path, explicit_doc())
    # Act
    ctx = pytest.raises(ValueError, match="does not define spec.profiles")
    # Assert
    with ctx:
        load_config(path, profile="claude-code")


def test_load_config_keeps_legacy_spec_backward_compatible(tmp_path: Path) -> None:
    # Arrange
    path = _write_doc(tmp_path, explicit_doc())
    # Act
    config = load_config(path)
    # Assert
    assert (config.profile, config.profiled) == ("default", False)


def test_validate_raw_rejects_root_claude_with_profiles() -> None:
    # Arrange
    doc = _profiled_doc()
    doc["spec"]["claude"] = copy.deepcopy(
        doc["spec"]["profiles"]["claude-code"]["claude"]
    )
    # Act
    errors = validate_raw(doc, "/tmp/spec.yaml")
    # Assert
    assert any("cannot be combined" in error for error in errors)


def test_validate_raw_requires_default_profile() -> None:
    # Arrange
    doc = _profiled_doc()
    doc["spec"].pop("default_profile")
    # Act
    errors = validate_raw(doc, "/tmp/spec.yaml")
    # Assert
    assert any("default_profile is required" in error for error in errors)


def test_validate_raw_rejects_unsupported_native_codex_harness() -> None:
    # Arrange
    doc = _profiled_doc()
    doc["spec"]["profiles"]["codex"]["harness"] = "codex"
    # Act
    errors = validate_raw(doc, "/tmp/spec.yaml")
    # Assert
    assert any("Native Codex is not a supported harness yet" in error for error in errors)


def test_validate_raw_checks_non_default_profile() -> None:
    # Arrange
    doc = _profiled_doc()
    doc["spec"]["profiles"]["codex"]["claude"].pop("model")
    # Act
    errors = validate_raw(doc, "/tmp/spec.yaml")
    # Assert
    assert any("Profile(s) 'codex'" in error and "claude.model" in error for error in errors)
