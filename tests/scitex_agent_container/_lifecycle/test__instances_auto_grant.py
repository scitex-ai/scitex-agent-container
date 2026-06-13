"""Auto-grant ``<self> → lead`` on every ``agent_start`` (OP-PRIO-1).

Bug the change fixes: a previous container died WITHOUT going through
``agent_stop`` (kernel OOM, host reboot, ``kill -9``). The ACL grant
``<self> → lead`` had been added manually by the operator
(``sac a2a grant <agent> lead``) earlier in the same db, but the
restart pathway never refreshed it. When state.db was rebuilt from a
fresh snapshot or the original grant row was lost, ``lead`` could no
longer drive the agent until the operator re-ran ``sac a2a grant`` by
hand. Pinning the grant write inside ``record_local_instance`` means
EVERY successful start refreshes it — and because :func:`grant_send`
is idempotent, repeat starts do not duplicate the row.

Tests use a real on-disk SQLite state.db (isolated per test via the
``SCITEX_AGENT_CONTAINER_STATE_DB`` env override) and a real runtime
stub exposing ``_state_dir`` — no mocks, no monkeypatch.
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path
from typing import Iterator

import pytest

from scitex_agent_container.config import AgentConfig


@pytest.fixture
def db_path(tmp_path: Path) -> Iterator[Path]:
    """Per-test on-disk state.db, exported via env (save/restore).

    ``state_db`` reads ``SCITEX_AGENT_CONTAINER_STATE_DB`` at import into
    a module-level ``DEFAULT_DB_PATH``; reload after setting the env so
    every helper (including ``has_grant`` / ``open_db``) lands in the
    temp DB.
    """
    p = tmp_path / "state.db"
    key = "SCITEX_AGENT_CONTAINER_STATE_DB"
    saved = os.environ.get(key)
    os.environ[key] = str(p)
    import scitex_agent_container._state.state_db as mod

    importlib.reload(mod)
    try:
        yield p
    finally:
        if saved is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = saved
        importlib.reload(mod)


class _RuntimeStub:
    """Honest runtime collaborator — only the ``_state_dir`` resolver
    that ``_instances`` calls. Mirrors ApptainerContainerRuntime's API."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def _state_dir(self, config: AgentConfig) -> Path:
        return self._root / config.name


# ---------------------------------------------------------------------------
# Happy path — first start grants self → lead
# ---------------------------------------------------------------------------


def test_record_local_instance_grants_self_to_lead(
    db_path: Path, tmp_path: Path
) -> None:
    # Arrange
    from scitex_agent_container._lifecycle._instances import record_local_instance
    from scitex_agent_container._state.state_db_nodes import has_grant

    cfg = AgentConfig(name="grant-1", runtime="apptainer")
    # Act
    record_local_instance(cfg, _RuntimeStub(tmp_path))
    # Assert
    assert has_grant(sender="grant-1", target="lead") is True


# ---------------------------------------------------------------------------
# Idempotency — repeat starts do not duplicate the comms_grants row
# ---------------------------------------------------------------------------


def test_record_local_instance_grant_to_lead_is_idempotent(
    db_path: Path, tmp_path: Path
) -> None:
    # Arrange — two successive starts simulate a crash-recover loop.
    from scitex_agent_container._lifecycle._instances import record_local_instance
    from scitex_agent_container._state.state_db import open_db

    cfg = AgentConfig(name="grant-2", runtime="apptainer")
    rt = _RuntimeStub(tmp_path)
    record_local_instance(cfg, rt)
    # Act
    record_local_instance(cfg, rt)
    # Assert — exactly one comms_grants row for (grant-2, lead).
    with open_db() as conn:
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM comms_grants "
            "WHERE sender_name = ? AND target_name = ?",
            ("grant-2", "lead"),
        ).fetchone()["c"]
    assert count == 1


# ---------------------------------------------------------------------------
# Happy path sanity — grant write does not break record_local_instance's
# documented return contract (instance id string).
# ---------------------------------------------------------------------------


def test_record_local_instance_returns_instance_id_when_grant_write_succeeds(
    db_path: Path, tmp_path: Path
) -> None:
    # Arrange
    from scitex_agent_container._lifecycle._instances import record_local_instance

    cfg = AgentConfig(name="grant-3", runtime="apptainer")
    # Act
    instance_id = record_local_instance(cfg, _RuntimeStub(tmp_path))
    # Assert — record_local_instance documents ``str | None``; on the
    # happy path with a writeable state.db it MUST return the id string.
    assert isinstance(instance_id, str)
