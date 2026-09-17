"""Remote coordinator rows never claim owning-host runtime selection."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import scitex_agent_container.cli_pkg._helpers._agent_list as _al
from scitex_agent_container.cli_pkg._helpers._agent_list_fleet_model import HostTarget
from scitex_agent_container.cli_pkg._helpers._agent_list_fleet_probe import _stamp_host
from scitex_agent_container.cli_pkg._helpers._agent_list_remote_rows import (
    remote_instance_rows,
)
from tests.scitex_agent_container._helpers.explicit_spec import explicitize_yaml


@contextmanager
def _discovering(path: Path):
    saved = _al._discover_defined_agents
    _al._discover_defined_agents = lambda: [("remote-a", path)]
    try:
        yield
    finally:
        _al._discover_defined_agents = saved


def test_remote_liveness_without_owning_host_identity_keeps_selection_unknown(tmp_path) -> None:
    # Arrange
    spec = tmp_path / "spec.yaml"
    spec.write_text(
        explicitize_yaml(
            "apiVersion: scitex-agent-container/v3\n"
            "kind: Agent\n"
            "metadata: {}\n"
            "spec:\n"
            "  runtime: tui\n"
            "  harness: hermes\n"
            "  engine: stale-coordinator-engine\n"
            "  host: remote-node\n"
            "  workdir: /tmp\n"
        ),
        encoding="utf-8",
    )
    active = [
        {
            "id": "remote-inc",
            "name": "remote-a",
            "host": "remote-node",
            "remote": True,
            "started_at": "2026-01-01T00:00:00Z",
        }
    ]

    # Act
    with _discovering(spec):
        rows = remote_instance_rows(
            registered=set(),
            display_host="coordinator",
            port_claims={},
            instances_oracle=lambda: active,
            status_probe=lambda name, host: "running",
        )

    # Assert
    assert (
        rows[0]["status"],
        rows[0]["engine"],
        rows[0]["model"],
        rows[0]["runtime_identity_source"],
    ) == ("running", "unknown", "unknown", "owning_host_status_unavailable")


def test_fleet_remote_spec_fallback_is_not_dressed_as_selected() -> None:
    # Arrange
    target = HostTarget(name="remote-node", ssh="remote-node")

    # Act
    row = _stamp_host(
        [
            {
                "name": "remote-a",
                "engine": "stale-spec-engine",
                "model": "stale-spec-model",
                "runtime_identity_source": "spec",
            }
        ],
        target,
    )[0]

    # Assert
    assert (row["engine"], row["model"], row["runtime_identity_source"]) == (
        "unknown",
        "unknown",
        "owning_host_spec_only",
    )


def test_fleet_remote_birth_bound_selection_is_preserved() -> None:
    # Arrange
    target = HostTarget(name="remote-node", ssh="remote-node")

    # Act
    row = _stamp_host(
        [
            {
                "name": "remote-a",
                "engine": "actual-engine",
                "model": "actual-model",
                "runtime_identity_source": "birth_certificate",
            }
        ],
        target,
    )[0]

    # Assert
    assert (row["engine"], row["model"], row["runtime_identity_source"]) == (
        "actual-engine",
        "actual-model",
        "birth_certificate",
    )
