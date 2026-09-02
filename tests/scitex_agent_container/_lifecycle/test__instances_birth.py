"""``record_local_instance`` writes the birth certificate (v4 step 5).

The launch path is the ONE place the compiled config and the freshly
minted incarnation id are both in hand; the certificate must land there
as an intrinsic side-effect, keyed by the same id the ``instances`` row,
the beats and the ExitRecord carry. Same no-mocks arrangement as
``test__instances.py`` — the honest runtime stub and a REAL PostgreSQL via
``pg_schema``.

THIS FILE USED TO STRADDLE TWO DATABASES, and that was the migration in
miniature: the ``instances`` row went to a per-agent file threaded in as an
explicit ``db_path``, the birth certificate had already moved to per-host
PostgreSQL, and these tests asserted the two still agreed on the incarnation
id that joins them. Both records now live in the same store, so there is one
database and no path to thread; what the tests assert is unchanged.
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


def test_local_start_records_a_birth_certificate(pg_schema: str, tmp_path: Path) -> None:
    # Arrange
    cfg = AgentConfig(name="born-1", runtime="apptainer")
    # Act
    incarnation = record_local_instance(cfg, _RuntimeStub(tmp_path))
    # Assert: the row exists under the SAME id the instances row minted.
    assert get_incarnation(incarnation) is not None


def test_birth_certificate_names_the_agent(pg_schema: str, tmp_path: Path) -> None:
    # Arrange
    cfg = AgentConfig(name="born-2", runtime="apptainer")
    # Act
    incarnation = record_local_instance(cfg, _RuntimeStub(tmp_path))
    # Assert
    assert get_incarnation(incarnation)["agent_id"] == "born-2"


def test_birth_certificate_carries_the_compiled_spec(pg_schema: str, tmp_path: Path) -> None:
    # Arrange: a resolved value that only exists post-compile (the model
    # default) must be readable straight off the record.
    cfg = AgentConfig(name="born-3", runtime="apptainer")
    # Act
    incarnation = record_local_instance(cfg, _RuntimeStub(tmp_path))
    stored = json.loads(get_incarnation(incarnation)["compiled_spec_json"])
    # Assert
    assert stored["model"] == cfg.model


def test_birth_certificate_redacts_env_secrets(pg_schema: str, tmp_path: Path) -> None:
    # Arrange
    cfg = AgentConfig(
        name="born-4", runtime="apptainer", env={"MY_API_KEY": "sk-live-abc"}
    )
    # Act
    incarnation = record_local_instance(cfg, _RuntimeStub(tmp_path))
    stored = json.loads(get_incarnation(incarnation)["compiled_spec_json"])
    # Assert: slot name kept, value never recorded.
    assert stored["env"]["MY_API_KEY"] == "<redacted:MY_API_KEY>"
