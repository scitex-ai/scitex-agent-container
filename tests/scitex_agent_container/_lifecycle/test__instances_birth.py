"""``record_local_instance`` writes the birth certificate (v4 step 5).

The launch path is the ONE place the compiled config and the freshly
minted incarnation id are both in hand; the certificate must land there
as an intrinsic side-effect, keyed by the same id the ``instances`` row,
the beats and the ExitRecord carry. Same no-mocks arrangement as
``test__instances.py`` — a real on-disk state.db via explicit
``db_path`` and the honest runtime stub.
"""

from __future__ import annotations

import json
from pathlib import Path

from scitex_agent_container._lifecycle._instances import record_local_instance
from scitex_agent_container._state.state_db_incarnations import get_incarnation
from scitex_agent_container.config import AgentConfig


class _RuntimeStub:
    """Honest runtime collaborator — only the ``_state_dir`` resolver
    that ``_instances`` calls. Mirrors ApptainerContainerRuntime's API."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def _state_dir(self, config: AgentConfig) -> Path:
        return self._root / config.name


def test_local_start_records_a_birth_certificate(tmp_path: Path) -> None:
    # Arrange
    db = tmp_path / "state.db"
    cfg = AgentConfig(name="born-1", runtime="apptainer")
    # Act
    incarnation = record_local_instance(cfg, _RuntimeStub(tmp_path), db_path=db)
    # Assert: the row exists under the SAME id the instances row minted.
    assert get_incarnation(incarnation, db_path=db) is not None


def test_birth_certificate_names_the_agent(tmp_path: Path) -> None:
    # Arrange
    db = tmp_path / "state.db"
    cfg = AgentConfig(name="born-2", runtime="apptainer")
    # Act
    incarnation = record_local_instance(cfg, _RuntimeStub(tmp_path), db_path=db)
    # Assert
    assert get_incarnation(incarnation, db_path=db)["agent_id"] == "born-2"


def test_birth_certificate_carries_the_compiled_spec(tmp_path: Path) -> None:
    # Arrange: a resolved value that only exists post-compile (the model
    # default) must be readable straight off the record.
    db = tmp_path / "state.db"
    cfg = AgentConfig(name="born-3", runtime="apptainer")
    # Act
    incarnation = record_local_instance(cfg, _RuntimeStub(tmp_path), db_path=db)
    stored = json.loads(get_incarnation(incarnation, db_path=db)["compiled_spec_json"])
    # Assert
    assert stored["model"] == cfg.model


def test_birth_certificate_redacts_env_secrets(tmp_path: Path) -> None:
    # Arrange
    db = tmp_path / "state.db"
    cfg = AgentConfig(
        name="born-4", runtime="apptainer", env={"MY_API_KEY": "sk-live-abc"}
    )
    # Act
    incarnation = record_local_instance(cfg, _RuntimeStub(tmp_path), db_path=db)
    stored = json.loads(get_incarnation(incarnation, db_path=db)["compiled_spec_json"])
    # Assert: slot name kept, value never recorded.
    assert stored["env"]["MY_API_KEY"] == "<redacted:MY_API_KEY>"
