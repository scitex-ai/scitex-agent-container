"""Perf-behaviour tests for `sac agents list` (`get_agent_list_data`).

Three profiler-driven fixes, each asserted by real behaviour (no mocks — the
seams are save/restore swaps of real module attributes with real callables):

1. Ports come from ONE `port_allocator.list_claims()` call, not a per-agent
   `get_port()`.
2. `validate_config` is NOT re-called when `load_config` already succeeded
   (a loaded config is valid by construction) — only when the load failed.
3. `running_only=True` DEFERS account + movement enrichment for rows the
   default view will hide (non-running), while keeping them for running rows;
   `running_only=False` enriches every row (the `--json` / `-v` contract).
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

import scitex_agent_container.cli_pkg._helpers._agent_list as _al
from scitex_agent_container.cli_pkg._helpers._agent_list import get_agent_list_data


class _FakeRegistry:
    def __init__(self, entries: list[dict]) -> None:
        self._entries = entries

    def list_all(self) -> list[dict]:
        return list(self._entries)


@contextmanager
def _swap_probe(impl: Callable[[Any], bool | None]) -> Iterator[None]:
    import scitex_agent_container.cli_pkg._helpers as _pkg

    saved_al = _al._probe_local
    saved_pkg = getattr(_pkg, "_probe_local", None)
    _al._probe_local = impl  # type: ignore[assignment]
    _pkg._probe_local = impl  # type: ignore[assignment]
    try:
        yield
    finally:
        _al._probe_local = saved_al  # type: ignore[assignment]
        if saved_pkg is None:
            delattr(_pkg, "_probe_local")
        else:
            _pkg._probe_local = saved_pkg  # type: ignore[assignment]


@contextmanager
def _swap_attr(obj: object, name: str, impl: object) -> Iterator[None]:
    saved = getattr(obj, name)
    setattr(obj, name, impl)
    try:
        yield
    finally:
        setattr(obj, name, saved)


def _no_discover() -> list[tuple[str, Path]]:
    return []


def _write_valid_spec(dir_: Path) -> Path:
    dir_.mkdir(parents=True, exist_ok=True)
    spec = dir_ / "spec.yaml"
    spec.write_text(
        "\n".join(
            [
                "apiVersion: scitex-agent-container/v3",
                "kind: Agent",
                "metadata: {}",
                "spec:",
                "  runtime: apptainer",
                "  host: ${HOSTNAME}",
                "  workdir: /home/agent/work",
                "  apptainer:",
                "    image: /x.sif",
                "    binds: []",
                "  claude:",
                "    model: sonnet",
                "  health:",
                "    enabled: true",
                "    interval: 60",
                "  restart:",
                "    policy: on-failure",
                "    max_retries: 3",
            ]
        )
        + "\n"
    )
    return spec


# ---------------------------------------------------------------------------
# FIX 1 — one list_claims() call, not per-agent get_port().
# ---------------------------------------------------------------------------


def test_ports_come_from_a_single_list_claims_call(tmp_path):
    # Arrange — two registered agents; count list_claims + get_port calls.
    from scitex_agent_container._state import port_allocator

    calls = {"list_claims": 0, "get_port": 0}

    def _fake_list_claims(**_kw):
        calls["list_claims"] += 1
        return [{"name": "a", "port": 19001, "claimed_at": "t"}]

    def _fake_get_port(_name, **_kw):
        calls["get_port"] += 1
        return 42

    spec_a = _write_valid_spec(tmp_path / "a")
    spec_b = _write_valid_spec(tmp_path / "b")
    registry = _FakeRegistry(
        [{"name": "a", "config": str(spec_a)}, {"name": "b", "config": str(spec_b)}]
    )
    # Act
    with _swap_attr(port_allocator, "list_claims", _fake_list_claims), _swap_attr(
        port_allocator, "get_port", _fake_get_port
    ), _swap_probe(lambda cfg: True):
        with _swap_attr(_al, "_discover_defined_agents", _no_discover):
            out = get_agent_list_data(registry)
    # Assert — exactly one bulk query for two agents; no per-agent get_port.
    assert calls == {"list_claims": 1, "get_port": 0}


def test_port_from_claims_map_lands_on_the_row(tmp_path):
    # Arrange
    from scitex_agent_container._state import port_allocator

    def _fake_list_claims(**_kw):
        return [{"name": "a", "port": 19001, "claimed_at": "t"}]

    spec_a = _write_valid_spec(tmp_path / "a")
    registry = _FakeRegistry([{"name": "a", "config": str(spec_a)}])
    # Act
    with _swap_attr(port_allocator, "list_claims", _fake_list_claims), _swap_probe(
        lambda cfg: True
    ), _swap_attr(_al, "_discover_defined_agents", _no_discover):
        out = get_agent_list_data(registry)
    # Assert
    assert out[0]["a2a_port"] == 19001


def test_port_none_when_agent_has_no_claim(tmp_path):
    # Arrange — claims map has no entry for this agent.
    from scitex_agent_container._state import port_allocator

    spec_a = _write_valid_spec(tmp_path / "a")
    registry = _FakeRegistry([{"name": "a", "config": str(spec_a)}])
    # Act
    with _swap_attr(port_allocator, "list_claims", lambda **_k: []), _swap_probe(
        lambda cfg: True
    ), _swap_attr(_al, "_discover_defined_agents", _no_discover):
        out = get_agent_list_data(registry)
    # Assert
    assert out[0]["a2a_port"] is None


# ---------------------------------------------------------------------------
# FIX 2 — no redundant re-validate when load_config already succeeded.
# ---------------------------------------------------------------------------


def test_validate_config_skipped_when_load_succeeds(tmp_path):
    # Arrange — count explicit validate_config calls from the list builder.
    from scitex_agent_container.config import _validation

    calls = {"n": 0}

    def _counting_validate(path):
        calls["n"] += 1
        return []

    spec = _write_valid_spec(tmp_path / "a")
    registry = _FakeRegistry([{"name": "a", "config": str(spec)}])
    # Act
    with _swap_attr(_validation, "validate_config", _counting_validate), _swap_probe(
        lambda cfg: True
    ), _swap_attr(_al, "_discover_defined_agents", _no_discover):
        out = get_agent_list_data(registry)
    # Assert — a valid, loaded config is not re-parsed/re-validated.
    assert calls["n"] == 0 and out[0]["status"] == "running"


def test_validate_config_called_when_load_fails(tmp_path):
    # Arrange — a missing config path: load_config raises, so the error list
    # must be recovered via validate_config.
    from scitex_agent_container.config import _validation

    calls = {"n": 0}

    def _counting_validate(path):
        calls["n"] += 1
        return ["stub-error"]

    missing = tmp_path / "gone" / "spec.yaml"
    registry = _FakeRegistry([{"name": "a", "config": str(missing)}])
    # Act
    with _swap_attr(_validation, "validate_config", _counting_validate), _swap_attr(
        _al, "_discover_defined_agents", _no_discover
    ):
        out = get_agent_list_data(registry)
    # Assert — load failed → validate_config consulted exactly once.
    assert calls["n"] == 1 and out[0]["validation_errors"] == ["stub-error"]


# ---------------------------------------------------------------------------
# FIX 3 — running_only defers account + movement for hidden (non-running) rows.
# ---------------------------------------------------------------------------


def test_running_only_enriches_the_running_row(tmp_path):
    # Arrange
    spec = _write_valid_spec(tmp_path / "a")
    registry = _FakeRegistry([{"name": "a", "config": str(spec)}])
    # Act
    with _swap_probe(lambda cfg: True), _swap_attr(
        _al, "_safe_account_for", lambda cfg: "ACCT"
    ), _swap_attr(_al, "_runtime_account_for", lambda name: None), _swap_attr(
        _al, "_discover_defined_agents", _no_discover
    ):
        out = get_agent_list_data(registry, running_only=True)
    # Assert — a shown (running) row keeps its account.
    assert out[0]["account"] == "ACCT"


def test_running_only_defers_account_for_stopped_row(tmp_path):
    # Arrange
    spec = _write_valid_spec(tmp_path / "a")
    registry = _FakeRegistry([{"name": "a", "config": str(spec)}])
    # Act
    with _swap_probe(lambda cfg: False), _swap_attr(
        _al, "_safe_account_for", lambda cfg: "ACCT"
    ), _swap_attr(_al, "_discover_defined_agents", _no_discover):
        out = get_agent_list_data(registry, running_only=True)
    # Assert — a hidden (stopped) row skips account resolution.
    assert out[0]["account"] == ""


def test_running_only_false_still_enriches_stopped_row(tmp_path):
    # Arrange — the --json / -v contract: every row stays enriched.
    spec = _write_valid_spec(tmp_path / "a")
    registry = _FakeRegistry([{"name": "a", "config": str(spec)}])
    # Act
    with _swap_probe(lambda cfg: False), _swap_attr(
        _al, "_safe_account_for", lambda cfg: "ACCT"
    ), _swap_attr(_al, "_discover_defined_agents", _no_discover):
        out = get_agent_list_data(registry, running_only=False)
    # Assert
    assert out[0]["account"] == "ACCT"


def test_deferred_row_keeps_the_movement_key_contract(tmp_path):
    # Arrange — deferred rows must still carry the always-present movement trio.
    spec = _write_valid_spec(tmp_path / "a")
    registry = _FakeRegistry([{"name": "a", "config": str(spec)}])
    # Act
    with _swap_probe(lambda cfg: False), _swap_attr(
        _al, "_safe_account_for", lambda cfg: "ACCT"
    ), _swap_attr(_al, "_discover_defined_agents", _no_discover):
        out = get_agent_list_data(registry, running_only=True)
    # Assert
    row = out[0]
    assert (
        row["session_jsonl_bytes"] == 0
        and row["session_jsonl_last_write"] == ""
        and row["heartbeat_at"] == ""
    )


def test_running_only_defers_defined_agent_account(tmp_path):
    # Arrange — a defined-on-disk agent is never running → hidden by default.
    spec = _write_valid_spec(tmp_path / "ondisk")

    def _discover() -> list[tuple[str, Path]]:
        return [("ondisk", spec)]

    registry = _FakeRegistry([])
    # Act
    with _swap_attr(_al, "_safe_account_for", lambda cfg: "ACCT"), _swap_attr(
        _al, "_discover_defined_agents", _discover
    ):
        out = get_agent_list_data(registry, running_only=True)
    # Assert
    row = next(r for r in out if r["name"] == "ondisk")
    assert row["account"] == "" and row["status"] == "defined"
