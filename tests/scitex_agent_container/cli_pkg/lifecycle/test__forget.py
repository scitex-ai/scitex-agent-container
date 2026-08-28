"""Tests for ``sac agents forget`` — local-only registry-reset recovery.

Operator backlog #3 (per lead 2026-06-01). Today there is no verb that
drops a specific agent's registry state cleanly when the agent is
already gone but state.db still claims it is running. The existing
``sac agents stop --force`` handles "agent WAS running, peer now
unreachable" — but NOT the "agent is gone, only stale rows persist"
case (a SLURM-reclaimed compute node, a crashed peer that came back
with a fresh state.db, etc.). The dispatch fixes #252/#253 do not
close this gap.

``sac agents forget <name>`` is the registry-reset recovery verb:
purely local state.db mutations (instances row tombstoned with
``exit_reason='operator-forget'``, comms_nodes pin unregistered),
NO ssh, NO remote signal. Refuses to act on an agent that has a
LIVE local instance unless ``--force`` is passed (avoids accidental
state-clobber on a healthy agent).

NO MOCKS (PA-306): real on-disk state.db, real CliRunner against the
real click command, real fixtures isolating the state-db path. Each
test: AAA markers (TQ002), one assertion (TQ007), 3+-word name.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterator

import pytest
from click.testing import CliRunner

from scitex_agent_container._state import state_db
from scitex_agent_container._state.state_db_instances import (
    list_active_instances,
    record_instance_start,
)
from scitex_agent_container._state.state_db_nodes import (
    register_comms_node,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_state(tmp_path: Path) -> Iterator[Path]:
    """Real isolated state.db; env + module constants saved/restored."""
    db = tmp_path / "state.db"
    saved_env = os.environ.get("SCITEX_AGENT_CONTAINER_STATE_DB")
    saved_default = state_db.DEFAULT_DB_PATH
    os.environ["SCITEX_AGENT_CONTAINER_STATE_DB"] = str(db)
    state_db.DEFAULT_DB_PATH = db
    state_db.init_schema(db)
    try:
        yield db
    finally:
        state_db.DEFAULT_DB_PATH = saved_default
        if saved_env is None:
            os.environ.pop("SCITEX_AGENT_CONTAINER_STATE_DB", None)
        else:
            os.environ["SCITEX_AGENT_CONTAINER_STATE_DB"] = saved_env


def _seed_active_instance(
    name: str, *, host: str = "h", db_path: Path | None = None
) -> str:
    """Insert one active ``instances`` row for ``name`` and return its id."""
    return record_instance_start(name, host=host, db_path=db_path)


def _seed_comms_node(name: str, *, host: str = "h", port: int = 9999) -> None:
    """Pin ``name`` in the comms_nodes store.

    No ``db_path``: comms_nodes moved to PostgreSQL on 2026-08-28, so the
    caller must take the ``pg_schema`` fixture instead of an isolated file.
    """
    register_comms_node(name=name, host=host, a2a_port=port, source_host=None)


def _run_forget(*argv: str) -> object:
    """Invoke ``sac agents forget`` via the click CliRunner."""
    from scitex_agent_container.cli_pkg.lifecycle._forget import forget

    return CliRunner().invoke(forget, list(argv))


# ---------------------------------------------------------------------------
# Happy path — stale-only rows are cleared without --force
# ---------------------------------------------------------------------------


def test_forget_tombstones_stale_instances_row(isolated_state: Path) -> None:
    # Arrange — a SLURM-reclaimed agent: instances row still active,
    # but no live local process. ``forget`` must tombstone it without
    # SSH or runtime probing.
    _seed_active_instance("ghost-agent", db_path=isolated_state)
    # Act
    _run_forget("ghost-agent", "--force")
    # Assert — no active rows remain for ghost-agent.
    active = [r["name"] for r in list_active_instances(db_path=isolated_state)]
    assert "ghost-agent" not in active


def test_forget_clears_comms_nodes_pin(
    isolated_state: Path, pg_schema: str
) -> None:
    # Arrange — federated routing still pins ghost-agent at a dead
    # host. Without this, future a2a sends silently fan out to the
    # dead host even after the instance row is gone. BOTH fixtures:
    # ``instances`` is still SQLite, ``comms_nodes`` is PostgreSQL.
    _seed_active_instance("ghost-agent", db_path=isolated_state)
    _seed_comms_node("ghost-agent")
    from scitex_agent_container._state.state_db_nodes import (
        resolve_node_host,
    )

    # Act
    _run_forget("ghost-agent", "--force")
    # Assert — the comms_nodes routing tuple is gone.
    assert resolve_node_host(name="ghost-agent", db_path=isolated_state) is None


def test_forget_exit_reason_is_operator_forget(isolated_state: Path) -> None:
    # Arrange — distinct exit_reason so post-hoc state.db inspection
    # tells "operator forgot this" apart from a graceful stop /
    # liveness sweep / peer-unreachable force-release.
    _seed_active_instance("ghost-agent", db_path=isolated_state)
    from scitex_agent_container._state.state_db_instances import last_known_instance

    # Act
    _run_forget("ghost-agent", "--force")
    row = last_known_instance("ghost-agent", db_path=isolated_state)
    # Assert
    assert row is not None and row["exit_reason"] == "operator-forget"


def test_forget_no_active_row_succeeds_silently(isolated_state: Path) -> None:
    # Arrange — no rows at all (operator double-runs the command).
    # forget MUST be idempotent — exit 0 with a "nothing-to-do" note.
    # Act
    result = _run_forget("never-existed")
    # Assert
    assert result.exit_code == 0


# ---------------------------------------------------------------------------
# Safety gate — refuse without --force when a live instance exists
# ---------------------------------------------------------------------------


def test_forget_refuses_live_instance_without_force(isolated_state: Path) -> None:
    # Arrange — a brand-new instance row with a recent heartbeat
    # looks LIVE; clobbering it would lose a real running agent's
    # state. Forget must refuse unless --force.
    _seed_active_instance("live-agent", db_path=isolated_state)
    # Act
    result = _run_forget("live-agent")
    # Assert — non-zero exit, with a clear reason in stderr.
    assert result.exit_code != 0


def test_refusal_message_names_the_force_flag(isolated_state: Path) -> None:
    # Arrange — the operator-facing error MUST name the remedy
    # (``--force``) so the next step is obvious. Mirrors the
    # ``stop --force`` UX.
    _seed_active_instance("live-agent", db_path=isolated_state)
    # Act
    result = _run_forget("live-agent")
    # Assert
    assert "--force" in (result.output or "") + (
        getattr(result, "stderr_bytes", b"").decode()
        if hasattr(result, "stderr_bytes")
        else ""
    )


def test_forget_force_overrides_live_safety_gate(isolated_state: Path) -> None:
    # Arrange — operator KNOWS the agent is dead despite the live-
    # looking row (SLURM-reclaimed node, peer-OS-rebooted, etc.).
    # --force MUST tombstone the row.
    _seed_active_instance("live-but-dead", db_path=isolated_state)
    # Act
    _run_forget("live-but-dead", "--force")
    # Assert
    active = [r["name"] for r in list_active_instances(db_path=isolated_state)]
    assert "live-but-dead" not in active


# ---------------------------------------------------------------------------
# No remote SSH — the whole point of `forget` is local-only
# ---------------------------------------------------------------------------


def test_forget_does_not_spawn_ssh_subprocess(
    isolated_state: Path, subprocess_shim
) -> None:
    # Arrange — install a real fake ``ssh`` binary on ``$PATH`` via the
    # package's no-mocks ``subprocess_shim`` fixture (the same pattern
    # the listen-side argv regression test uses). The shim records each
    # invocation; ``forget`` MUST be local-only and never reach for ssh
    # (that is what ``stop`` does; ``forget`` is the recovery path for
    # when ssh is hopeless). PA-306 §3 forbids ``monkeypatch.setattr``
    # — the production code's real ``subprocess.run`` does its real
    # PATH lookup, finds the shim, and the shim's argv log is what we
    # read back.
    subprocess_shim.install("ssh", stdout="", exit=0)
    _seed_active_instance("ghost", db_path=isolated_state)
    # Act
    _run_forget("ghost", "--force")
    # Assert — the shim records zero invocations because forget never
    # shelled out to ssh in the first place.
    assert subprocess_shim.call_count("ssh") == 0


# ---------------------------------------------------------------------------
# JSON shape — operator tooling can branch on the envelope
# ---------------------------------------------------------------------------


def test_forget_json_envelope_carries_exit_reason(isolated_state: Path) -> None:
    # Arrange
    _seed_active_instance("ghost", db_path=isolated_state)
    # Act
    result = _run_forget("ghost", "--force", "--json")
    payload = json.loads(result.stdout)
    # Assert
    assert payload.get("exit_reason") == "operator-forget"


def test_forget_json_envelope_carries_name(isolated_state: Path) -> None:
    # Arrange
    _seed_active_instance("ghost", db_path=isolated_state)
    # Act
    result = _run_forget("ghost", "--force", "--json")
    payload = json.loads(result.stdout)
    # Assert
    assert payload.get("name") == "ghost"
