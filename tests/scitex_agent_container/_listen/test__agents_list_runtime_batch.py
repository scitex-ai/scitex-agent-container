"""GET /agents runtime enrichment is one-snapshot/one-birth-query."""

from __future__ import annotations

import json

from scitex_agent_container._listen._agents_list import annotate_runtime_rows
from scitex_agent_container._listen._registry_endpoints import enrich_row_with_endpoint


def test_runtime_rows_batch_active_and_birth_reads_with_duplicate_rows() -> None:
    # Arrange
    calls = {"active": 0, "birth": 0}
    active = [
        {"id": "newest-wrong", "name": "alpha", "host": "node-a", "remote": False},
        {"id": "alpha-real", "name": "alpha", "host": "node-a", "remote": False},
        {"id": "beta-real", "name": "beta", "host": "node-a", "remote": False},
    ]

    def active_reader(host=None) -> list[dict]:
        calls["active"] += 1
        return active

    def birth_reader(ids: tuple[str, ...]) -> dict[str, dict]:
        calls["birth"] += 1
        return {
            value: {
                "compiled_spec_json": json.dumps(
                    {
                        "runtime": "tui",
                        "harness": "hermes",
                        "engine_key": f"selected-{value}",
                        "model": "actual",
                    }
                )
            }
            for value in ids
        }

    # Act
    rows = annotate_runtime_rows(
        [
            {"name": "alpha", "config": "/alpha/spec.yaml"},
            {"name": "beta", "config": "/beta/spec.yaml"},
        ],
        active_reader=active_reader,
        birth_reader=birth_reader,
        config_loader=lambda path: {
            "runtime": "tui",
            "harness": "hermes",
            "engine_key": "stale-spec",
            "model": "stale",
        },
        runtime_probe=lambda config: True,
        evidence_reader=lambda name, row: {
            "marker_id": f"{name}-real",
            "pid": None,
            "session": None,
            "heartbeat": None,
        },
        local_host="node-a",
    )

    # Assert
    assert (
        calls,
        [row["engine"] for row in rows],
        [row["runtime_identity_source"] for row in rows],
    ) == (
        {"active": 1, "birth": 1},
        ["selected-alpha-real", "selected-beta-real"],
        ["birth_certificate", "birth_certificate"],
    )


def test_stopped_runtime_does_not_publish_bound_birth() -> None:
    # Arrange
    # Act
    row = annotate_runtime_rows(
        [{"name": "alpha", "config": "/alpha/spec.yaml"}],
        active_reader=lambda host=None: [
            {"id": "stale", "name": "alpha", "host": "node-a", "remote": False}
        ],
        birth_reader=lambda ids: {
            "stale": {
                "compiled_spec_json": json.dumps(
                    {"engine_key": "stale-selected", "harness": "hermes"}
                )
            }
        },
        config_loader=lambda path: {
            "runtime": "tui",
            "harness": "hermes",
            "engine_key": "current-spec",
            "model": "declared",
        },
        runtime_probe=lambda config: False,
        evidence_reader=lambda name, row: {
            "marker_id": "stale",
            "pid": None,
            "session": None,
            "heartbeat": None,
        },
        local_host="node-a",
    )[0]

    # Assert
    assert (row["status"], row["engine"], row["runtime_identity_source"]) == (
        "stopped",
        "current-spec",
        "spec",
    )


def test_endpoint_enrichment_uses_supplied_snapshot_without_per_row_store_reads() -> None:
    # Arrange
    # Act
    row = enrich_row_with_endpoint(
        {"name": "remote-a"},
        ports={},
        instance_endpoints={"remote-a": (19123, "remote-node")},
        local_host="local-node",
    )

    # Assert
    assert (row["a2a_port"], row["turn_url"]) == (
        19123,
        "http://remote-node:19123/v1/turn",
    )
