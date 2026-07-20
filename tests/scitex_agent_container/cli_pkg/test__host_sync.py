"""CLI tests for ``sac host sync``.

PA-306: no ``unittest.mock``. Real ``CliRunner``, a real ``config.yaml``
via the ``SCITEX_AGENT_CONTAINER_CONFIG`` env override, and a real
PATH-installed ``ssh`` shim for the remote round-trip.

The assertions that matter are the EXIT CODES: ``--check`` is meant to
be a cron alarm, and an alarm that exits 0 on drift is a report nobody
reads. That is exactly how a five-release-stale Spartan checkout went
unnoticed until someone looked by hand.

Each test: AAA markers (TQ002), one assertion (TQ007), 3+-word name (TQ003).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from scitex_agent_container._events import read_events
from scitex_agent_container.cli_pkg._host_sync import host_sync

_REPO = "/data/gpfs/projects/punim0264/ywatanabe/scitex-agent-container"
_MODULE = f"{_REPO}/src/scitex_agent_container/__init__.py"


@pytest.fixture
def cfg_path(tmp_path: Path, env_save_restore) -> Path:
    """Real config.yaml naming one peer, surfaced via the env override."""
    p = tmp_path / "config.yaml"
    p.write_text("host:\n  aliases: {}\npeers:\n  spartan:\n    ssh: spartan\n")
    env_save_restore.set("SCITEX_AGENT_CONTAINER_CONFIG", str(p))
    return p


def _marker_block(*, head: str, target_sha: str, behind: int = 0) -> str:
    return (
        f"SAC_SYNC module={_MODULE}\n"
        f"SAC_SYNC repo={_REPO}\n"
        "SAC_SYNC target=origin/develop\n"
        f"SAC_SYNC target_sha={target_sha}\n"
        f"SAC_SYNC head={head}\n"
        "SAC_SYNC ahead=0\n"
        f"SAC_SYNC behind={behind}\n"
        "SAC_SYNC symbol=['agent_name', 'range_']\n"
        "SAC_SYNC end\n"
    )


def test_check_on_stale_peer_exits_non_zero(cfg_path, subprocess_shim):
    # Arrange — the peer is 4 commits behind the centre.
    subprocess_shim.install(
        "ssh", stdout=_marker_block(head="aaa111", target_sha="bbb222", behind=4)
    )
    # Act
    result = CliRunner().invoke(host_sync, ["--check", "spartan"])
    # Assert — an alarm that exits 0 on drift is not an alarm.
    assert result.exit_code == 1


def test_check_on_current_peer_exits_zero(cfg_path, subprocess_shim):
    # Arrange
    subprocess_shim.install(
        "ssh", stdout=_marker_block(head="aaa111", target_sha="aaa111")
    )
    # Act
    result = CliRunner().invoke(host_sync, ["--check", "spartan"])
    # Assert
    assert result.exit_code == 0


def test_check_on_unreachable_peer_exits_two(cfg_path, subprocess_shim):
    # Arrange — UNKNOWN is neither clean nor drifted; it is its own code.
    subprocess_shim.install("ssh", exit=255, stderr="ssh: connect: refused\n")
    # Act
    result = CliRunner().invoke(host_sync, ["--check", "spartan"])
    # Assert
    assert result.exit_code == 2


def test_check_never_runs_a_merge(cfg_path, subprocess_shim):
    # Arrange
    subprocess_shim.install(
        "ssh", stdout=_marker_block(head="aaa111", target_sha="bbb222", behind=4)
    )
    # Act
    CliRunner().invoke(host_sync, ["--check", "spartan"])
    calls = subprocess_shim.invocations("ssh")
    # Assert — --check is read-only, structurally.
    assert not any("merge --ff-only" in " ".join(argv) for argv in calls)


def test_check_names_the_stale_peer_in_output(cfg_path, subprocess_shim):
    # Arrange — never silent: the operator must see WHICH peer drifted.
    subprocess_shim.install(
        "ssh", stdout=_marker_block(head="aaa111", target_sha="bbb222", behind=4)
    )
    # Act
    result = CliRunner().invoke(host_sync, ["--check", "spartan"])
    # Assert
    assert "spartan" in result.output


def test_check_reports_the_loaded_module_path(cfg_path, subprocess_shim):
    # Arrange — evidence, not a summary. A version string would lie.
    subprocess_shim.install(
        "ssh", stdout=_marker_block(head="aaa111", target_sha="aaa111")
    )
    # Act
    result = CliRunner().invoke(host_sync, ["--check", "spartan"])
    # Assert
    assert "scitex_agent_container/__init__.py" in result.output


def test_json_output_carries_the_exit_code(cfg_path, subprocess_shim):
    # Arrange
    subprocess_shim.install(
        "ssh", stdout=_marker_block(head="aaa111", target_sha="bbb222", behind=4)
    )
    # Act
    result = CliRunner().invoke(host_sync, ["--check", "spartan", "--json"])
    # Assert
    assert '"exit_code": 1' in result.output


def test_peer_and_all_together_is_a_usage_error(cfg_path):
    # Arrange
    # Act
    result = CliRunner().invoke(host_sync, ["--check", "--all", "spartan"])
    # Assert
    assert result.exit_code == 2


def test_neither_peer_nor_all_is_a_usage_error(cfg_path):
    # Arrange
    # Act
    result = CliRunner().invoke(host_sync, ["--check"])
    # Assert
    assert result.exit_code == 2


def test_alarm_without_check_is_a_usage_error(cfg_path):
    # Arrange — the alarm must ride ONLY the read-only --check form; a
    # scheduled alarm that could fast-forward a peer is Stage 1.
    # Act
    result = CliRunner().invoke(host_sync, ["--alarm", "--all"])
    # Assert
    assert result.exit_code == 2


def test_alarm_records_a_stale_peer_as_degraded(
    cfg_path, subprocess_shim, env_save_restore, tmp_path
):
    # Arrange — a real temp event log, redirected via the documented env
    # var, and a peer 4 commits behind the centre.
    log = tmp_path / "sac-events.jsonl"
    env_save_restore.set("SAC_EVENT_LOG", str(log))
    subprocess_shim.install(
        "ssh", stdout=_marker_block(head="aaa111", target_sha="bbb222", behind=4)
    )
    # Act — the exact read-only check+alarm form the timer runs.
    CliRunner().invoke(host_sync, ["--check", "spartan", "--alarm"])
    # Assert — the shout is DURABLE: spartan is recorded degraded.
    recorded = [(e.event, e.subject) for e in read_events(log)]
    assert recorded == [("subject-degraded", "spartan")]


def test_alarm_is_read_only_and_runs_no_merge(
    cfg_path, subprocess_shim, env_save_restore, tmp_path
):
    # Arrange — a drifted peer plus a redirected event log so nothing
    # touches the real one.
    env_save_restore.set("SAC_EVENT_LOG", str(tmp_path / "sac-events.jsonl"))
    subprocess_shim.install(
        "ssh", stdout=_marker_block(head="aaa111", target_sha="bbb222", behind=4)
    )
    # Act
    CliRunner().invoke(host_sync, ["--check", "spartan", "--alarm"])
    calls = subprocess_shim.invocations("ssh")
    # Assert — --alarm rides the read-only detector; it never mutates a peer.
    assert not any("merge --ff-only" in " ".join(argv) for argv in calls)
