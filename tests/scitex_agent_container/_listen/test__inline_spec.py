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
import subprocess
from pathlib import Path

import pytest
import yaml

from scitex_agent_container._lifecycle._twin import CARDS_AGENT_ENV, TWIN_PARENT_ENV
from scitex_agent_container._listen._inline_spec import materialize_inline_spec
from tests.scitex_agent_container._helpers.explicit_spec import explicit_doc


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


def _twin_spec(*, workdir: Path, overlay: Path) -> dict:
    doc = explicit_doc(
        {
            "runtime": "tui",
            "harness": "hermes",
            "workdir": str(workdir),
            "apptainer": {
                "image": "sac-base",
                "overlay": str(overlay),
                "env": {
                    CARDS_AGENT_ENV: "parent-twin",
                    TWIN_PARENT_ENV: "parent",
                },
            },
            "available_harnesses": {
                "hermes": {"session": {"mode": "continue", "max_age_minutes": None}}
            },
        },
        metadata={"labels": {"role": "worker"}},
    )
    for legacy in ("claude", "container", "watchdog", "context_management"):
        doc["spec"].pop(legacy, None)
    doc["spec"]["comms"]["channels"] = ["server:sac", "server:scitex-cards"]
    return doc


def test_materialized_twin_creates_worktree_on_host(home_root: Path) -> None:
    # Arrange
    parent_repo = home_root / "parent-repo"
    twin_workdir = home_root / ".sac-twins" / "parent-twin" / "workdir"
    parent_repo.mkdir()
    subprocess.run(["git", "init", "-q", str(parent_repo)], check=True)
    subprocess.run(
        ["git", "-C", str(parent_repo), "config", "user.email", "test@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(parent_repo), "config", "user.name", "Twin Test"],
        check=True,
    )
    (parent_repo / "tracked.txt").write_text("parent\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(parent_repo), "add", "tracked.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(parent_repo), "commit", "-qm", "seed"], check=True
    )
    parent_spec = _twin_spec(
        workdir=parent_repo,
        overlay=home_root / "runtime" / "parent" / "overlay",
    )
    parent_spec["spec"]["apptainer"]["env"] = {CARDS_AGENT_ENV: "parent"}
    parent_dir = (
        home_root / ".scitex" / "agent-container" / "agents" / "parent"
    )
    parent_dir.mkdir(parents=True)
    (parent_dir / "spec.yaml").write_text(
        yaml.safe_dump(parent_spec, sort_keys=False), encoding="utf-8"
    )
    child_spec = _twin_spec(
        workdir=twin_workdir,
        overlay=home_root / "runtime" / "parent" / ".sac-twins" / "parent-twin" / "overlay",
    )

    # Act
    result = materialize_inline_spec(
        "parent-twin", child_spec, overwrite=False, caller="parent"
    )

    # Assert
    assert (
        result,
        (twin_workdir / "tracked.txt").read_text(encoding="utf-8"),
    ) == (None, "parent\n")


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

    def test_overwrite_refuses_authority_managed_agent_link(self, home_root: Path):
        # Arrange — link-specs installs the whole per-agent directory as a
        # symlink into a clean git-backed authority tree.
        authority = home_root / "authority" / "alpha"
        authority.mkdir(parents=True)
        spec_path = authority / "spec.yaml"
        original = _valid_spec()
        spec_path.write_text(yaml.safe_dump(original), encoding="utf-8")
        agents = home_root / ".scitex" / "agent-container" / "agents"
        agents.mkdir(parents=True)
        (agents / "alpha").symlink_to(authority, target_is_directory=True)
        replacement = _valid_spec()
        replacement["spec"] = {"role": "worker"}
        # Act — even the legacy overwrite escape hatch must not write through
        # the authority link.
        result = materialize_inline_spec("alpha", replacement, overwrite=True)
        # Assert
        assert result.status_code == 409

    def test_authority_refusal_uses_existing_collision_kind(self, home_root: Path):
        # Arrange
        authority = home_root / "authority" / "alpha"
        authority.mkdir(parents=True)
        (authority / "spec.yaml").write_text(
            yaml.safe_dump(_valid_spec()), encoding="utf-8"
        )
        agents = home_root / ".scitex" / "agent-container" / "agents"
        agents.mkdir(parents=True)
        (agents / "alpha").symlink_to(authority, target_is_directory=True)
        # Act
        result = materialize_inline_spec("alpha", _valid_spec(), overwrite=True)
        # Assert — existing clients already branch on this stable 409 kind.
        assert _body(result)["kind"] == "already_exists"

    def test_authority_refusal_preserves_source_bytes(self, home_root: Path):
        # Arrange
        authority = home_root / "authority" / "alpha"
        authority.mkdir(parents=True)
        spec_path = authority / "spec.yaml"
        original_bytes = (
            b"apiVersion: scitex-agent-container/v3\nkind: Agent\nspec: {}\n"
        )
        spec_path.write_bytes(original_bytes)
        agents = home_root / ".scitex" / "agent-container" / "agents"
        agents.mkdir(parents=True)
        (agents / "alpha").symlink_to(authority, target_is_directory=True)
        # Act
        materialize_inline_spec("alpha", _valid_spec(), overwrite=True)
        # Assert — the incident was mutation of a supposedly immutable git
        # snapshot, so byte preservation is the primary regression contract.
        assert spec_path.read_bytes() == original_bytes

    def test_overwrite_refuses_direct_spec_symlink(self, home_root: Path):
        # Arrange — protect the narrower layout too, even though link-specs
        # currently links the whole agent directory.
        authority = home_root / "authority"
        authority.mkdir()
        source = authority / "alpha.yaml"
        source.write_text(yaml.safe_dump(_valid_spec()), encoding="utf-8")
        primary = home_root / ".scitex" / "agent-container" / "agents" / "alpha"
        primary.mkdir(parents=True)
        (primary / "spec.yaml").symlink_to(source)
        # Act
        result = materialize_inline_spec("alpha", _valid_spec(), overwrite=True)
        # Assert
        assert result.status_code == 409


class TestMaterializeStartupCommandsLintWireIn:
    """Integration of the startup_commands first-token lint into the
    inline-spec materialise pipeline. Unit-level branch coverage of the
    lint itself lives in :mod:`test__inline_spec_startup_lint`; this
    class only confirms the wire-in at the materialise entrypoint.
    """

    def test_prompt_text_command_returns_400(self, home_root: Path):
        # Arrange — the clew launcher #70 canonical bug: agent's CLAUDE
        # mission prompt was placed in startup_commands by mistake.
        spec = _valid_spec()
        spec["spec"]["startup_commands"] = [
            {"command": "You: run the experiment with seed=42"}
        ]
        # Act
        result = materialize_inline_spec("alpha", spec, overwrite=False)
        # Assert
        assert result.status_code == 400

    def test_prompt_text_command_kind_is_spec_invalid(self, home_root: Path):
        # Arrange
        spec = _valid_spec()
        spec["spec"]["startup_commands"] = [{"command": "You: do thing"}]
        # Act
        result = materialize_inline_spec("alpha", spec, overwrite=False)
        # Assert
        assert _body(result)["kind"] == "spec_invalid"

    def test_prompt_text_command_does_not_write_spec_file(self, home_root: Path):
        # Arrange — fail-loud rejection must leave zero artifacts (no
        # spec dir, no spec.yaml). Same fail-loud contract PR-1 holds
        # for bind_unresolvable.
        spec = _valid_spec()
        spec["spec"]["startup_commands"] = [{"command": "You: leaked"}]
        spec_path = (
            home_root / ".scitex" / "agent-container" / "agents" / "alpha" / "spec.yaml"
        )
        # Act
        materialize_inline_spec("alpha", spec, overwrite=False)
        # Assert
        assert not spec_path.exists()

    def test_well_formed_startup_commands_pass_through(self, home_root: Path):
        # Arrange — ``echo`` + ``ls`` (both available as builtin /
        # PATH executable) should not block the materialise.
        spec = _valid_spec()
        spec["spec"]["startup_commands"] = [
            {"command": "echo starting"},
            {"command": "ls /tmp"},
        ]
        # Act
        result = materialize_inline_spec("alpha", spec, overwrite=False)
        # Assert
        assert result is None
