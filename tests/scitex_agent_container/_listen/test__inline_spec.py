"""Tests for ``_listen._inline_spec`` (POST /agents inline spec writer).

Covers :func:`materialize_inline_spec`: writes a valid v3 Agent spec
under ``$HOME/.scitex/agent-container/agents/<name>/spec.yaml`` (None
return on success) and emits 400/409 ``JSONResponse`` for malformed or
already-existing payloads. Real-fixture only (PA-306 no-mocks): we
redirect ``$HOME`` to ``tmp_path`` and round-trip YAML on disk.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml

from scitex_agent_container._listen._inline_spec import materialize_inline_spec


@pytest.fixture
def home_root(tmp_path: Path):
    """Redirect ``$HOME`` to ``tmp_path`` for the duration of one test.

    Explicit save/restore (no monkeypatch) matches the PA-306 pattern
    in sibling tests.
    """
    key = "HOME"
    saved = os.environ.get(key)
    os.environ[key] = str(tmp_path)
    try:
        yield tmp_path
    finally:
        if saved is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = saved


def _valid_spec() -> dict:
    """Minimal v3 Agent spec accepted by the validator."""
    return {
        "apiVersion": "scitex-agent-container/v3",
        "kind": "Agent",
        "metadata": {"name": "alpha"},
        "spec": {"role": "head"},
    }


def _body(resp) -> dict:
    """Extract the JSON payload from a Starlette ``JSONResponse``."""
    return json.loads(bytes(resp.body).decode("utf-8"))


class TestMaterializeValidSpec:
    def test_valid_spec_returns_none(self, home_root: Path):
        # Arrange
        spec = _valid_spec()
        # Act
        result = materialize_inline_spec("alpha", spec, overwrite=False)
        # Assert
        assert result is None

    def test_valid_spec_writes_yaml_file(self, home_root: Path):
        # Arrange
        spec = _valid_spec()
        expected_path = (
            home_root / ".scitex" / "agent-container" / "agents" / "alpha" / "spec.yaml"
        )
        # Act
        materialize_inline_spec("alpha", spec, overwrite=False)
        # Assert
        assert expected_path.is_file()

    def test_written_yaml_roundtrips_back(self, home_root: Path):
        # Arrange
        spec = _valid_spec()
        spec_path = (
            home_root / ".scitex" / "agent-container" / "agents" / "alpha" / "spec.yaml"
        )
        # Act
        materialize_inline_spec("alpha", spec, overwrite=False)
        loaded = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
        # Assert
        assert loaded == spec


class TestMaterializeRejectsNonDict:
    def test_list_spec_returns_400(self, home_root: Path):
        # Arrange
        bad_spec = ["not", "a", "dict"]
        # Act
        result = materialize_inline_spec("alpha", bad_spec, overwrite=False)
        # Assert
        assert result.status_code == 400

    def test_string_spec_error_message(self, home_root: Path):
        # Arrange
        bad_spec = "yaml-as-string"
        # Act
        result = materialize_inline_spec("alpha", bad_spec, overwrite=False)
        # Assert
        assert "JSON object" in _body(result)["error"]


class TestMaterializeRejectsBadApiVersion:
    def test_wrong_api_version_returns_400(self, home_root: Path):
        # Arrange
        spec = _valid_spec()
        spec["apiVersion"] = "scitex-agent-container/v2"
        # Act
        result = materialize_inline_spec("alpha", spec, overwrite=False)
        # Assert
        assert result.status_code == 400

    def test_missing_api_version_returns_400(self, home_root: Path):
        # Arrange
        spec = _valid_spec()
        del spec["apiVersion"]
        # Act
        result = materialize_inline_spec("alpha", spec, overwrite=False)
        # Assert
        assert result.status_code == 400

    def test_bad_api_version_error_message(self, home_root: Path):
        # Arrange
        spec = _valid_spec()
        spec["apiVersion"] = "wrong/v1"
        # Act
        result = materialize_inline_spec("alpha", spec, overwrite=False)
        # Assert
        assert "v3" in _body(result)["error"]


class TestMaterializeRejectsBadKind:
    def test_wrong_kind_returns_400(self, home_root: Path):
        # Arrange
        spec = _valid_spec()
        spec["kind"] = "Pod"
        # Act
        result = materialize_inline_spec("alpha", spec, overwrite=False)
        # Assert
        assert result.status_code == 400

    def test_missing_kind_returns_400(self, home_root: Path):
        # Arrange
        spec = _valid_spec()
        del spec["kind"]
        # Act
        result = materialize_inline_spec("alpha", spec, overwrite=False)
        # Assert
        assert result.status_code == 400

    def test_bad_kind_error_message(self, home_root: Path):
        # Arrange
        spec = _valid_spec()
        spec["kind"] = "Service"
        # Act
        result = materialize_inline_spec("alpha", spec, overwrite=False)
        # Assert
        assert "Agent" in _body(result)["error"]


class TestMaterializeOverwriteSemantics:
    def test_existing_spec_without_overwrite_409(self, home_root: Path):
        # Arrange
        spec = _valid_spec()
        materialize_inline_spec("alpha", spec, overwrite=False)
        # Act
        result = materialize_inline_spec("alpha", spec, overwrite=False)
        # Assert
        assert result.status_code == 409

    def test_existing_spec_with_overwrite_succeeds(self, home_root: Path):
        # Arrange
        materialize_inline_spec("alpha", _valid_spec(), overwrite=False)
        replacement = _valid_spec()
        replacement["metadata"]["name"] = "alpha-v2"
        # Act
        result = materialize_inline_spec("alpha", replacement, overwrite=True)
        # Assert
        assert result is None

    def test_overwrite_replaces_file_contents(self, home_root: Path):
        # Arrange
        materialize_inline_spec("alpha", _valid_spec(), overwrite=False)
        replacement = _valid_spec()
        replacement["spec"] = {"role": "worker"}
        spec_path = (
            home_root / ".scitex" / "agent-container" / "agents" / "alpha" / "spec.yaml"
        )
        # Act
        materialize_inline_spec("alpha", replacement, overwrite=True)
        loaded = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
        # Assert
        assert loaded["spec"]["role"] == "worker"
