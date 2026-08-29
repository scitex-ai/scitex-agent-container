"""``--exit-zero`` separates the VERDICT from the PROCESS STATUS.

Why this flag exists, measured 2026-08-17: `sac host sync --check --all
--alarm` runs hourly as scitex-agent-container-host-sync-check.service.
Finding drift exits 1 and "could not determine" exits 2, so systemd recorded
the unit as `failed` for doing its job correctly. That put compute-04 into
`degraded`; the dotfiles sync installer then tested
`systemctl --user is-system-running`, read `degraded` as "systemd is
absent", and PERMANENTLY and SILENTLY refused to install its timer — so that
host stopped receiving dotfiles sync entirely. A finding became a health
signal became a missing service, across two packages, every step reporting
success.

THE FIXTURE USES A PEER THAT CANNOT BE REACHED, ON PURPOSE. An empty peer
list is rejected by the verb ("no syncable peers"), and a reachable peer
would make the verdict 0 — either way the tests could not tell "the status
was neutralised" apart from "there was nothing to neutralise". An
unreachable peer yields UNDETERMINED, a genuinely non-zero verdict, which
is the only case the flag changes. That is the positive control.

No mocks: a real Click invocation against a real config file, asserting the
process status and the JSON a caller actually receives.
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from scitex_agent_container.cli_pkg._host_sync import host_sync

# `.invalid` is reserved by RFC 2606 and can never resolve, so ssh fails
# immediately rather than waiting on a network timeout.
_UNREACHABLE = "host:\n  aliases: {}\npeers:\n  nowhere:\n    ssh: nowhere.invalid\n"


@pytest.fixture
def unreachable_peer_config(tmp_path, env_save_restore):
    """One peer that cannot be reached -> UNDETERMINED -> non-zero verdict.

    The env var is ``SCITEX_AGENT_CONTAINER_CONFIG``, read from
    ``_state/host_config.py:85`` rather than guessed. Getting this name
    wrong does not fail a test — it silently loads the OPERATOR'S REAL
    config and ssh-es every peer in the fleet.
    """
    cfg = tmp_path / "config.yaml"
    cfg.write_text(_UNREACHABLE)
    env_save_restore.set("SCITEX_AGENT_CONTAINER_CONFIG", str(cfg))
    return cfg


def _run(args):
    return CliRunner().invoke(host_sync, args, catch_exceptions=False)


_BASE = ["--check", "--all", "--json", "--timeout", "1"]


def test_without_the_flag_an_unreachable_peer_exits_non_zero(unreachable_peer_config):
    """The positive control: this case really does exit non-zero by default.

    Without this, every assertion below could pass on a verb that never
    returns non-zero at all.
    """
    # Arrange
    args = list(_BASE)
    # Act
    result = _run(args)
    # Assert
    assert result.exit_code != 0


def test_with_the_flag_the_process_exits_zero(unreachable_peer_config):
    """The whole point: systemd must not read a finding as ill health."""
    # Arrange
    args = [*_BASE, "--exit-zero"]
    # Act
    result = _run(args)
    # Assert
    assert result.exit_code == 0


def test_with_the_flag_the_verdict_is_still_non_zero_in_json(unreachable_peer_config):
    """The half that stops this becoming a gate that cannot fail.

    Status neutral, verdict intact. A fix that suppressed both would pass
    the test above and fail this one.
    """
    # Arrange
    args = [*_BASE, "--exit-zero"]
    # Act
    result = _run(args)
    # Assert
    assert json.loads(result.output)["exit_code"] != 0


def test_the_flag_does_not_change_the_verdict_value(unreachable_peer_config):
    """The reported verdict is identical with and without the flag."""
    # Arrange
    plain = _run(list(_BASE))
    # Act
    zeroed = _run([*_BASE, "--exit-zero"])
    # Assert
    assert json.loads(zeroed.output)["exit_code"] == json.loads(plain.output)["exit_code"]


def test_default_still_routes_verdict_to_status(unreachable_peer_config):
    """The contract for interactive and script callers is UNCHANGED."""
    # Arrange
    result = _run(list(_BASE))
    # Act
    verdict = json.loads(result.output)["exit_code"]
    # Assert
    assert result.exit_code == verdict


def test_help_documents_exit_zero():
    """A job depends on this flag; an undocumented flag is a trap."""
    # Arrange
    args = ["--help"]
    # Act
    result = _run(args)
    # Assert
    assert "--exit-zero" in result.output


def test_the_job_command_passes_exit_zero():
    """The flag is worthless if the JobSpec does not use it.

    Asserted against the real registered JobSpec, so editing the job's
    command cannot leave this green while the timer keeps exiting non-zero.
    """
    # Arrange
    from scitex_agent_container._jobs._jobs_plugin import provide_jobs

    jobs = {j.name: j for j in provide_jobs()}
    # Act
    command = jobs["scitex-agent-container-host-sync-check"].command
    # Assert
    assert "--exit-zero" in command
