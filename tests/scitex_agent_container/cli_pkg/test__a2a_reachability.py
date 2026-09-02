"""``sac a2a reachability`` end to end — real Click, real registry, real ssh leg.

The verb is the scheduled job's entry point, so what is pinned here is the
CONTRACT the supervisor's execution log reads: the exit codes (0 / 1 / 3),
the ``--json`` shape, the ``--record`` file and its ``--last`` reader, and
the event-log records every run leaves behind.

No mocks and no monkeypatching. The registry is a real ``hosts.yaml`` under
a real ``$SCITEX_DIR``; the config is a real file under
``SCITEX_AGENT_CONTAINER_CONFIG`` (read from ``_state/host_config.py``, not
guessed — the wrong name would silently load the OPERATOR'S config and ssh
every fleet peer); peer tokens are real files under a temp ``HOME``; the
ssh leg is a fake ``ssh`` on PATH so production calls the real
``subprocess.run``; the event log is the per-test file the suite's autouse
fixture already points ``SAC_EVENT_LOG`` at.
"""

from __future__ import annotations

import json
import os
import textwrap
from pathlib import Path

import pytest
from click.testing import CliRunner

from scitex_agent_container._events import EVENT_LOG_ENV, SUBJECT_DEGRADED, read_events
from scitex_agent_container._listen.peer_tokens import write_peer_token
from scitex_agent_container._network._ssh_curl import STATUS_MARKER
from scitex_agent_container.cli_pkg.a2a_group import a2a

_HOSTS_YAML = textwrap.dedent(
    """\
    hosts:
      probe-box:
        kind: workstation
        ssh_alias: probe-box
        scitex_root: "~/.scitex"
      peer-with-alias:
        kind: workstation
        ssh_alias: peer-with-alias-ssh
        scitex_root: "~/.scitex"
      peer-without-alias:
        kind: workstation
        ssh_alias: null
        scitex_root: "~/.scitex"
    """
)

_CONFIG_YAML = "host:\n  canonical: probe-box\n"

_HEALTHY = f'{{"ok": true, "service": "sac-listen", "v": 1}}\n{STATUS_MARKER}200\n'


@pytest.fixture
def fleet(tmp_path: Path, env_save_restore) -> Path:
    """A three-host fleet seen from ``probe-box``, with NO peer tokens yet."""
    home = tmp_path / "home"
    home.mkdir()
    scitex_dir = tmp_path / "scitex"
    (scitex_dir / "dev").mkdir(parents=True)
    (scitex_dir / "dev" / "hosts.yaml").write_text(_HOSTS_YAML)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(_CONFIG_YAML)
    env_save_restore.set("HOME", str(home))
    env_save_restore.set("SCITEX_DIR", str(scitex_dir))
    env_save_restore.set("SCITEX_AGENT_CONTAINER_CONFIG", str(cfg))
    env_save_restore.set(
        "SCITEX_AGENT_CONTAINER_RUNTIME_DIR", str(tmp_path / "runtime")
    )
    env_save_restore.set("SAC_SSH_CONTROL_DIR", str(tmp_path / "cm"))
    for key in (
        "SAC_HOST",
        "SCITEX_AGENT_CONTAINER_HOST",
        "SCITEX_AGENT_CONTAINER_HOSTNAME",
        "SAC_SSH_CONTROL_MASTER",
    ):
        env_save_restore.delete(key)
    return home


def _seed_token(home: Path) -> None:
    """Give ``peer-with-alias`` a peer token, so its leg gets DISPATCHED."""
    write_peer_token(
        peer_host="peer-with-alias",
        token="t0k3n",
        tokens_dir=home / ".scitex" / "agent-container" / "peer-tokens",
    )


def _run(args):
    return CliRunner().invoke(a2a, ["reachability", *args], catch_exceptions=False)


def _rows(result) -> dict[str, dict]:
    return {row["host"]: row for row in json.loads(result.output)["hosts"]}


# ---------------------------------------------------------------------------
# exit 3 — nothing measurable
# ---------------------------------------------------------------------------


def test_verb_exits_three_when_every_host_is_unknown(fleet):
    # Arrange — aliases exist but no peer token does, and one row has no
    # alias, and one row is this host: nothing can be dispatched.
    args = ["--all", "--json"]
    # Act
    result = _run(args)
    # Assert
    assert result.exit_code == 3


def test_all_unknown_report_lists_every_host_as_unknown(fleet):
    # Arrange
    args = ["--all", "--json"]
    # Act
    result = _run(args)
    # Assert
    assert {r["reachable"] for r in _rows(result).values()} == {None}


def test_this_host_is_reported_unknown_over_no_transport(fleet):
    # Arrange
    args = ["--all", "--json"]
    # Act
    result = _run(args)
    # Assert
    assert _rows(result)["probe-box"]["transport"] == "none"


def test_a_registry_row_without_alias_is_unknown_and_names_the_registry(fleet):
    # Arrange
    args = ["--all", "--json"]
    # Act
    result = _run(args)
    # Assert
    assert "hosts.yaml" in _rows(result)["peer-without-alias"]["error"]


def test_a_missing_peer_token_is_unknown_and_names_the_add_peer_fix(fleet):
    # Arrange
    args = ["--host", "peer-with-alias", "--json"]
    # Act
    result = _run(args)
    # Assert
    assert "sac host add-peer" in _rows(result)["peer-with-alias"]["error"]


def test_each_json_row_carries_exactly_the_six_declared_fields(fleet):
    # Arrange
    args = ["--all", "--json"]
    # Act
    result = _run(args)
    shapes = {frozenset(row) for row in _rows(result).values()}
    # Assert
    assert shapes == {
        frozenset(
            {"host", "ssh_alias", "transport", "reachable", "elapsed_ms", "error"}
        )
    }


# ---------------------------------------------------------------------------
# exit 1 — a dispatched leg that failed
# ---------------------------------------------------------------------------


def test_verb_exits_one_when_a_dispatched_leg_fails(fleet, subprocess_shim):
    # Arrange — a peer token exists, so the leg is dispatched; ssh refuses.
    _seed_token(fleet)
    subprocess_shim.install("ssh", exit=255, stderr="ssh: connect: refused")
    # Act
    result = _run(["--all", "--json", "--timeout", "1"])
    # Assert
    assert result.exit_code == 1


def test_failed_leg_is_recorded_degraded_in_the_event_log(fleet, subprocess_shim):
    # Arrange
    _seed_token(fleet)
    subprocess_shim.install("ssh", exit=255, stderr="ssh: connect: refused")
    # Act
    _run(["--all", "--json", "--timeout", "1"])
    degraded = [
        e.subject
        for e in read_events(path=Path(os.environ[EVENT_LOG_ENV]))
        if e.event == SUBJECT_DEGRADED
    ]
    # Assert
    assert degraded == ["peer-with-alias"]


def test_ssh_leg_dials_the_registry_alias_not_the_host_name(fleet, subprocess_shim):
    # Arrange — the forwarder routes by alias; so must the probe.
    _seed_token(fleet)
    subprocess_shim.install("ssh", exit=255, stderr="refused")
    # Act
    _run(["--host", "peer-with-alias", "--json", "--timeout", "1"])
    # Assert
    assert "peer-with-alias-ssh" in subprocess_shim.argv_for("ssh")


# ---------------------------------------------------------------------------
# exit 0 — a dispatched leg that answered as a listen
# ---------------------------------------------------------------------------


def test_verb_exits_zero_when_every_dispatched_leg_reaches_a_listen(
    fleet, subprocess_shim
):
    # Arrange — unknown rows (this host, the alias-less row) sit beside the
    # one measured host; the measured host is a real answer.
    _seed_token(fleet)
    subprocess_shim.install("ssh", exit=0, stdout=_HEALTHY)
    # Act
    result = _run(["--all", "--json"])
    # Assert
    assert result.exit_code == 0


# ---------------------------------------------------------------------------
# --record / --last — the report file and its reader
# ---------------------------------------------------------------------------


def test_record_writes_the_report_into_the_runtime_dir(fleet, tmp_path):
    # Arrange
    args = ["--all", "--json", "--record"]
    # Act
    _run(args)
    # Assert
    assert (tmp_path / "runtime" / "a2a-reachability.json").is_file()


def test_last_replays_the_recorded_report_with_its_exit_code(fleet, subprocess_shim):
    # Arrange — record a pass that measured an unreachable host ...
    _seed_token(fleet)
    subprocess_shim.install("ssh", exit=255, stderr="refused")
    _run(["--all", "--json", "--record", "--timeout", "1"])
    # Act — ... then read it back without probing.
    result = _run(["--last", "--json"])
    # Assert
    assert result.exit_code == 1


def test_last_without_a_recorded_report_fails_loudly(fleet):
    # Arrange
    args = ["--last"]
    # Act
    result = _run(args)
    # Assert
    assert result.exit_code != 0 and "no recorded report" in result.output


# ---------------------------------------------------------------------------
# usage errors stay Click's exit 2 — never a fleet verdict
# ---------------------------------------------------------------------------


def test_host_and_all_together_is_a_usage_error(fleet):
    # Arrange
    args = ["--host", "peer-with-alias", "--all"]
    # Act
    result = _run(args)
    # Assert
    assert result.exit_code == 2


def test_an_unknown_host_name_is_a_usage_error_naming_the_known_hosts(fleet):
    # Arrange — a typo must not become "exit 3, the fleet is unknown".
    args = ["--host", "peer-typo", "--json"]
    # Act
    result = _run(args)
    # Assert
    assert result.exit_code == 2 and "peer-with-alias" in result.output
