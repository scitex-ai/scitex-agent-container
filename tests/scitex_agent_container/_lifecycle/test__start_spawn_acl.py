"""Spawn ACL is enforced on the CORE start path AND the MCP tool path
(ADR-0010 Rule B / Phase 2 — 起動経路 = 記録経路 = ACL経路).

The acceptance for "sac from sac" Step 1: an agent-from-agent spawn,
whether driven through the MCP ``agent_start`` tool (clew's surface) or
the plain CLI, now (a) passes through ``check_spawn`` and (b) writes a
``lineage`` table row — not just the ``instances.spawned_by`` string.

PA-306: NO mocks. The two surfaces are exercised for real:

  * **core path** — :func:`agent_start` is driven with ``dry_run=True``
    and a real hand-rolled ``_FakeRuntime`` collaborator (the runtime is
    NOT a mock; it records whether ``start`` was reached, exactly as the
    existing drift-guard tests do). ``dry_run`` returns right after the
    gate, so the gate's lineage write is exercised without launching a
    container. The lineage row is then read back from a REAL on-disk
    state.db.
  * **MCP-tool path** — the real ``agent_start`` MCP tool function is
    called; it drives the real Click CLI (``invoke_cli_text``) through to
    core ``agent_start``. A child-caller spawn is rejected by the gate
    BEFORE any runtime work, proving the MCP surface is ACL-gated.

Each test: AAA markers (TQ002), one assertion (TQ007), 3+-word name.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterator

import pytest

from scitex_agent_container._lifecycle._start import agent_start
from scitex_agent_container._state import state_db
from scitex_agent_container._state.registry import Registry
from scitex_agent_container._state.state_db_nodes import derive_group, record_lineage
from scitex_agent_container.config import AgentConfig

# ---------------------------------------------------------------------------
# Real (non-mock) collaborators + isolation fixtures
# ---------------------------------------------------------------------------


class _FakeRuntime:
    """Honest runtime surface; records whether start() was reached.

    Not a mock — a hand-rolled fake exposing only the methods core
    ``agent_start`` calls (``is_running`` / ``start``). ``start`` honours
    the ``dry_run`` kwarg so the dry-run early-return path works.
    """

    def __init__(self) -> None:
        self.started: list[AgentConfig] = []

    def is_running(self, config: AgentConfig) -> bool:
        return False

    def start(self, config: AgentConfig, **kwargs: Any) -> bool:
        self.started.append(config)
        return True


class _FakeHandover:
    """Honest handover surface; no-op for the module callables core start
    invokes. Not a mock."""

    def ensure_instance_uuid(self, config: AgentConfig) -> str:
        return "uuid"

    def hydrate_from_hub(self, config: AgentConfig) -> bool:
        return True

    def start_failback_poller(self, config: AgentConfig) -> None:
        pass


@pytest.fixture
def isolated_state(tmp_path: Path) -> Iterator[Path]:
    """Isolated state.db + runtime dir + HOME, all under tmp_path.

    No mocks — real sqlite, real dirs; env + module constants saved and
    restored on teardown.
    """
    db = tmp_path / "state.db"
    runtime_dir = tmp_path / "runtime"
    home = tmp_path / "home"
    home.mkdir()
    keys = {
        "SCITEX_AGENT_CONTAINER_STATE_DB": str(db),
        "SCITEX_AGENT_CONTAINER_RUNTIME_DIR": str(runtime_dir),
        "HOME": str(home),
        "SCITEX_DIR": str(home / ".scitex"),
    }
    saved = {k: os.environ.get(k) for k in keys}
    saved_default = state_db.DEFAULT_DB_PATH
    os.environ.update(keys)
    state_db.DEFAULT_DB_PATH = db
    state_db.init_schema(db)
    try:
        yield db
    finally:
        state_db.DEFAULT_DB_PATH = saved_default
        for k, prev in saved.items():
            if prev is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = prev


@pytest.fixture
def sac_name() -> Iterator[Any]:
    """Yield a setter for the caller identity (SAC_NAME, both prefixes)."""
    keys = ("SAC_NAME", "SCITEX_AGENT_CONTAINER_NAME")
    saved = {k: os.environ.get(k) for k in keys}

    def _set(value: str | None) -> None:
        for k in keys:
            if value is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = value

    try:
        yield _set
    finally:
        for k, prev in saved.items():
            if prev is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = prev


def _write_spec(yaml_root: Path, name: str) -> Path:
    """Write a minimal, real v3 spec under the dir-as-SSoT layout."""
    agent_dir = yaml_root / name
    agent_dir.mkdir(parents=True)
    spec = agent_dir / "spec.yaml"
    spec.write_text(
        "apiVersion: scitex-agent-container/v3\n"
        "kind: Agent\n"
        "spec:\n"
        "  runtime: apptainer\n"
        f"  workdir: {yaml_root / (name + '-work')}\n"
        "  claude:\n"
        "    model: sonnet\n"
        "  health:\n"
        "    enabled: false\n"
    )
    return spec


# ---------------------------------------------------------------------------
# (b) Core spawn path writes a lineage row (not just spawned_by string)
# ---------------------------------------------------------------------------


def test_core_start_writes_lineage_row_for_parent_caller(
    isolated_state, sac_name, tmp_path
) -> None:
    # Arrange — a root parent agent spawns a child via core agent_start.
    sac_name("parent-root")
    spec = _write_spec(tmp_path / "yaml", "capsule-child")
    registry = Registry(registry_dir=tmp_path / "reg")
    # Act — dry_run returns right after the gate; no container launched.
    agent_start(
        str(spec),
        registry=registry,
        dry_run=True,
        runtime_factory=lambda _c: _FakeRuntime(),
        handover_mod=_FakeHandover(),
        sleep_fn=lambda _s: None,
    )
    # Assert — the lineage edge is in state.db (child's group has parent).
    assert "parent-root" in derive_group(name="capsule-child")


def test_admin_start_records_no_lineage_edge(
    isolated_state, sac_name, tmp_path
) -> None:
    # Arrange — no SAC_NAME → admin / operator / lead launch.
    sac_name(None)
    spec = _write_spec(tmp_path / "yaml", "admin-agent")
    registry = Registry(registry_dir=tmp_path / "reg")
    # Act
    agent_start(
        str(spec),
        registry=registry,
        dry_run=True,
        runtime_factory=lambda _c: _FakeRuntime(),
        handover_mod=_FakeHandover(),
        sleep_fn=lambda _s: None,
    )
    # Assert — admin-launched agent starts unattached (singleton group).
    assert derive_group(name="admin-agent") == {"admin-agent"}


# ---------------------------------------------------------------------------
# (a) MCP-tool path passes through check_spawn (deny gated before runtime)
# ---------------------------------------------------------------------------


def test_mcp_tool_spawn_is_denied_for_child_caller(
    isolated_state, sac_name, tmp_path
) -> None:
    # Arrange — the MCP tool runs through the real CLI; a child caller
    # ("worker-a", parented to "root") must be rejected by check_spawn.
    record_lineage(child="worker-a", parent="root", db_path=isolated_state)
    sac_name("worker-a")
    yaml_root = tmp_path / "yaml"
    _write_spec(yaml_root, "denied-child")
    saved_yaml = os.environ.get("SCITEX_AGENT_CONTAINER_YAML_DIRS")
    saved_key = os.environ.get("SAC_ANTHROPIC_API_KEY")
    # SAC_ANTHROPIC_API_KEY satisfies the OAuth preflight so the ONLY
    # thing that can deny is the spawn gate (not a missing-creds exit).
    os.environ["SCITEX_AGENT_CONTAINER_YAML_DIRS"] = str(yaml_root)
    os.environ["SAC_ANTHROPIC_API_KEY"] = "sk-ant-api-test-dummy"
    from scitex_agent_container._mcp._tools._agent import agent_start as mcp_start

    try:
        # Act — drive the real MCP tool → real Click CLI → core start.
        result = mcp_start("denied-child")
    finally:
        for k, prev in (
            ("SCITEX_AGENT_CONTAINER_YAML_DIRS", saved_yaml),
            ("SAC_ANTHROPIC_API_KEY", saved_key),
        ):
            if prev is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = prev
    # Assert — the CLI exited non-zero because the gate denied the spawn.
    assert result["exit_code"] != 0


def test_mcp_tool_spawn_deny_does_not_launch_child(
    isolated_state, sac_name, tmp_path
) -> None:
    # Arrange — same denied child; assert no live instance row was created
    # (the gate fired BEFORE any runtime/instance bookkeeping).
    record_lineage(child="worker-b", parent="root", db_path=isolated_state)
    sac_name("worker-b")
    yaml_root = tmp_path / "yaml"
    _write_spec(yaml_root, "never-launched")
    saved_yaml = os.environ.get("SCITEX_AGENT_CONTAINER_YAML_DIRS")
    saved_key = os.environ.get("SAC_ANTHROPIC_API_KEY")
    os.environ["SCITEX_AGENT_CONTAINER_YAML_DIRS"] = str(yaml_root)
    os.environ["SAC_ANTHROPIC_API_KEY"] = "sk-ant-api-test-dummy"
    from scitex_agent_container._mcp._tools._agent import agent_start as mcp_start
    from scitex_agent_container._state.state_db import list_active_instances

    try:
        # Act
        mcp_start("never-launched")
        active = [r["name"] for r in list_active_instances()]
    finally:
        for k, prev in (
            ("SCITEX_AGENT_CONTAINER_YAML_DIRS", saved_yaml),
            ("SAC_ANTHROPIC_API_KEY", saved_key),
        ):
            if prev is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = prev
    # Assert — the denied child never reached instance bookkeeping.
    assert "never-launched" not in active
