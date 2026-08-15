"""CLI-level tests for the fleet-wide default of ``sac agents list``.

No mocks and no ``monkeypatch``: the peer topology is a REAL ``config.yaml``
written into ``tmp_path`` and pointed at by the documented
``SCITEX_AGENT_CONTAINER_CONFIG`` env var, and the local hostname is pinned
through ``SCITEX_AGENT_CONTAINER_HOSTNAME`` — the same two knobs production
reads.

These tests never open a network connection. ``tests/conftest.py`` force-sets
``SAC_AGENTS_LIST_NO_FANOUT=1`` precisely so that no test ssh's into the
operator's real fleet, and the behaviour under test here — ``--host``
resolution, the loud unknown-host failure, the ``hosts`` block in ``--json`` —
is all decided before any transport would run. The fan-out itself is covered by
``_helpers/test__agent_list_fleet.py`` through explicit probe seams.
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from scitex_agent_container.cli_pkg.status_cmds import status

LOCAL = "fleet-test-local"
PEER = "fleet-test-peer"


@pytest.fixture
def fleet_config(tmp_path, env_save_restore):
    """A real config.yaml with one peer, plus a pinned local hostname."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "host:\n"
        f"  canonical: {LOCAL}\n"
        "peers:\n"
        f"  {PEER}:\n"
        f"    ssh: {PEER}.invalid\n"
    )
    env_save_restore.set("SCITEX_AGENT_CONTAINER_CONFIG", str(cfg))
    env_save_restore.set("SCITEX_AGENT_CONTAINER_HOSTNAME", LOCAL)
    return cfg


def _hosts(result) -> dict:
    # `result.output` folds stderr in (click 8.4 dropped mix_stderr), so an
    # ambient WARN logged during config load would land ahead of the payload.
    return json.loads(result.stdout)["hosts"]


# ===========================================================================
# The envelope stays what every consumer already parses
# ===========================================================================


def test_fleet_json_still_carries_the_agents_key(fleet_config):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(status, ["--json"])
    # Assert
    assert isinstance(json.loads(result.stdout)["agents"], list)


def test_fleet_json_exits_zero_even_with_peers_unqueried(fleet_config):
    # Arrange -- a listing that exited non-zero on an unreachable peer would
    # break every caller that parses it.
    runner = CliRunner()
    # Act
    result = runner.invoke(status, ["--json"])
    # Assert
    assert result.exit_code == 0, result.output


def test_fleet_json_carries_a_hosts_block(fleet_config):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(status, ["--json"])
    # Assert
    assert "reports" in _hosts(result)


def test_the_local_host_is_reported_by_its_resolved_name(fleet_config):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(status, ["--json"])
    # Assert
    assert any(r["host"] == LOCAL for r in _hosts(result)["reports"])


def test_the_local_host_names_the_instrument_that_answered(fleet_config):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(status, ["--json"])
    local = next(r for r in _hosts(result)["reports"] if r["host"] == LOCAL)
    # Assert
    assert local["instrument"] == "local_registry"


# ===========================================================================
# --host localhost resolves at parse time, and the header says so
# ===========================================================================


def test_host_localhost_is_accepted(fleet_config):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(status, ["--json", "--host", "localhost"])
    # Assert
    assert result.exit_code == 0, result.output


def test_host_localhost_is_resolved_to_the_real_hostname(fleet_config):
    # Arrange -- `localhost` names a different machine depending on where it
    # is typed, so the OUTPUT must record the resolved name.
    runner = CliRunner()
    # Act
    result = runner.invoke(status, ["--json", "--host", "localhost"])
    # Assert
    assert _hosts(result)["filter"]["resolutions"] == [
        {"requested": "localhost", "resolved": LOCAL}
    ]


def test_the_json_header_echoes_the_localhost_resolution(fleet_config):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(status, ["--json", "--host", "localhost"])
    # Assert
    assert f"--host localhost → {LOCAL}" in _hosts(result)["header"]


def test_the_table_header_echoes_the_localhost_resolution(fleet_config):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(status, ["--host", "localhost"])
    # Assert
    assert f"--host localhost → {LOCAL}" in result.output


def test_the_table_header_reports_the_responded_split(fleet_config):
    # Arrange -- mandatory, and it renders above the table every time.
    runner = CliRunner()
    # Act
    result = runner.invoke(status, ["--host", "localhost"])
    # Assert
    assert "1/1 host responded" in result.output


# ===========================================================================
# --host is repeatable, exact-match, and fails loud on an unknown name
# ===========================================================================


def test_host_selects_only_the_named_hosts(fleet_config):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(status, ["--json", "--host", "localhost"])
    # Assert
    assert [r["host"] for r in _hosts(result)["reports"]] == [LOCAL]


def test_host_is_repeatable(fleet_config):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(
        status, ["--json", "--host", "localhost", "--host", PEER]
    )
    # Assert
    assert {r["host"] for r in _hosts(result)["reports"]} == {LOCAL, PEER}


def test_a_named_peer_that_was_not_queried_is_reported_not_dropped(fleet_config):
    # Arrange -- the operator ASKED for it; silence would read "it is empty".
    runner = CliRunner()
    # Act
    result = runner.invoke(
        status, ["--json", "--host", "localhost", "--host", PEER]
    )
    peer = next(r for r in _hosts(result)["reports"] if r["host"] == PEER)
    # Assert
    assert peer["status"] == "not_queried"


def test_a_host_that_was_not_queried_has_an_unknown_agent_count(fleet_config):
    # Arrange -- None, NEVER 0.
    runner = CliRunner()
    # Act
    result = runner.invoke(
        status, ["--json", "--host", "localhost", "--host", PEER]
    )
    peer = next(r for r in _hosts(result)["reports"] if r["host"] == PEER)
    # Assert
    assert peer["agents"] is None


def test_an_unknown_host_fails_rather_than_listing_nothing(fleet_config):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(status, ["--json", "--host", "no-such-machine"])
    # Assert
    assert result.exit_code == 2


def test_the_unknown_host_error_names_the_known_peers(fleet_config):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(status, ["--json", "--host", "no-such-machine"])
    # Assert
    assert PEER in result.output


def test_the_unknown_host_error_names_the_local_host_too(fleet_config):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(status, ["--json", "--host", "no-such-machine"])
    # Assert
    assert LOCAL in result.output


# ===========================================================================
# Suppression is announced; the per-agent view is untouched
# ===========================================================================


def test_the_header_names_what_switched_the_fan_out_off(fleet_config):
    # Arrange -- a quietly-local listing is the exact ambiguity this removes.
    runner = CliRunner()
    # Act
    result = runner.invoke(status, ["--json"])
    # Assert
    assert _hosts(result)["fanout_suppressed_by"] == "SAC_AGENTS_LIST_NO_FANOUT"


def test_the_peer_count_is_reported_even_when_peers_were_not_queried(fleet_config):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(status, ["--json"])
    # Assert
    assert _hosts(result)["peers_known"] >= 1


def test_the_per_agent_view_takes_no_hosts_block(fleet_config):
    # Arrange -- `list <NAME>` is unchanged; only the fleet view gained one.
    runner = CliRunner()
    # Act
    result = runner.invoke(status, ["no-such-agent", "--json"])
    # Assert
    assert "hosts" not in json.loads(result.stdout)
