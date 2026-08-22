"""Tests for ``sac agent check-priority`` / ``sac registry reconcile-singletons``.

No-mocks rewrite (PA-306). The previous version monkeypatched
``subprocess.run``, the module-level ``load_config`` / ``_probe_ssh`` /
``Registry`` / ``agent_stop`` callables, and stubbed configs with
``SimpleNamespace``. This version exercises real production paths:

* ``_probe_ssh`` / ``_ssh_start_agent`` are tested against a real ``ssh``
  binary planted on ``PATH`` by the shared ``subprocess_shim`` fixture
  (argv + exit code controlled by the shim, not by attribute patching).
* ``_priority_report`` reads real on-disk ``spec.yaml`` files via the
  real ``load_config`` and validates against ``scitex-agent-container/v3``.
* CLI tests redirect the registry to ``tmp_path`` via the documented
  ``SCITEX_AGENT_CONTAINER_REGISTRY_DIR`` env var and pin the hostname
  via ``SCITEX_AGENT_CONTAINER_HOSTNAME``.
* ``agent_stop`` is now imported at module scope in production so it can
  be swapped via the same module-attribute save/restore pattern as
  ``image_group._load_apptainer`` -- a real callable seam, not
  ``MagicMock``. The real ``agent_stop`` would touch tmux/screen/hub,
  which is outside the scope of these CLI-shape tests.
"""

from __future__ import annotations

import importlib
import json
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest
from click.testing import CliRunner

import scitex_agent_container.cli_pkg.priority_cmds as pc
from scitex_agent_container.cli_pkg.priority_cmds import (
    _priority_report,
    _probe_ssh,
    _ssh_start_agent,
    priority_check,
    singleton_reconcile,
)

# ---------------------------------------------------------------------------
# Helpers -- real YAML specs + real registry JSON entries
# ---------------------------------------------------------------------------


def _write_spec(
    parent: Path,
    name: str,
    *,
    host: str | list[str] | None = None,
    hosts: str | list[str] | None = None,
) -> Path:
    """Write a minimal but valid v3 spec.yaml and return its path."""
    agent_dir = parent / name
    agent_dir.mkdir(parents=True, exist_ok=True)
    spec = agent_dir / "spec.yaml"
    lines = [
        "apiVersion: scitex-agent-container/v3",
        "kind: Agent",
        "metadata: {}",
        "spec:",
        "  runtime: apptainer",
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
    if hosts is not None:
        # multi-instance → workdir: null keeps the per-instance derivation
        # (the KEY is still required; red-start ruling 2026-07-21).
        lines.append(f"  hosts: {json.dumps(hosts)}")
        lines.append("  workdir:")
    else:
        # singleton (explicit host, or the local default) → workdir required.
        # 'local' is banned; an EMPTY host: is the caller's-host spelling
        # for the no-host-declared case.
        lines.append(f"  host: {json.dumps(host)}" if host is not None else "  host:")
        lines.append("  workdir: /home/agent/work")
    from tests.scitex_agent_container._helpers.explicit_spec import (
        explicitize_yaml,
    )

    spec.write_text(explicitize_yaml("\n".join(lines) + "\n"))
    return spec


@contextmanager
def _swap_agent_stop(impl) -> Iterator[list[str]]:
    """Swap ``pc.agent_stop`` for a real recording callable. Restores on exit."""
    calls: list[str] = []

    def _recording(name: str, *a, **kw) -> bool:
        calls.append(name)
        return impl(name)

    saved = pc.agent_stop
    pc.agent_stop = _recording  # type: ignore[assignment]
    try:
        yield calls
    finally:
        pc.agent_stop = saved  # type: ignore[assignment]


@pytest.fixture
def pin_hostname(env_save_restore):
    """Pin ``resolve_hostname()`` via the documented env override."""

    def _pin(value: str) -> None:
        env_save_restore.set("SCITEX_AGENT_CONTAINER_HOSTNAME", value)

    return _pin


@pytest.fixture
def tmp_registry(tmp_path, env_save_restore) -> Path:
    """Redirect the file-backed registry to a fresh dir under tmp_path."""
    reg = tmp_path / "registry"
    reg.mkdir()
    env_save_restore.set("SCITEX_AGENT_CONTAINER_REGISTRY_DIR", str(reg))
    # Reimport _state.registry so the module-level REGISTRY_DIR constant
    # picks up the new env var. Real reload, no monkeypatch.
    import scitex_agent_container._state.registry as _reg

    importlib.reload(_reg)
    # Undo the reload once the env is back — otherwise ``REGISTRY_DIR`` stays
    # pinned at this test's (soon-deleted) tmp dir for the rest of the worker.
    env_save_restore.reload_after_restore(_reg)
    saved_registry_cls = pc.Registry
    pc.Registry = _reg.Registry  # type: ignore[assignment]
    try:
        yield reg
    finally:
        pc.Registry = saved_registry_cls  # type: ignore[assignment]


def _register(reg_dir: Path, name: str, config_path: Path) -> None:
    """Write a real registry JSON entry."""
    (reg_dir / f"{name}.json").write_text(
        json.dumps(
            {
                "name": name,
                "config": str(config_path),
                "pid": 1,
                "started_at": "2026-01-01T00:00:00Z",
                "screen": name,
            }
        )
    )


# ---------------------------------------------------------------------------
# _probe_ssh -- real subprocess.run against shim ssh binary on PATH
# ---------------------------------------------------------------------------


def test_probe_ssh_returns_true_when_shim_ssh_exits_zero(subprocess_shim):
    # Arrange
    subprocess_shim.install("ssh", exit=0)
    # Act
    result = _probe_ssh("any-host")
    # Assert
    assert result is True


def test_probe_ssh_returns_false_when_shim_ssh_exits_nonzero(subprocess_shim):
    # Arrange
    subprocess_shim.install("ssh", exit=1)
    # Act
    result = _probe_ssh("any-host")
    # Assert
    assert result is False


def test_probe_ssh_passes_host_argument_to_ssh_binary(subprocess_shim):
    # Arrange
    subprocess_shim.install("ssh", exit=0)
    # Act
    _probe_ssh("specific-host")
    # Assert
    argv = subprocess_shim.argv_for("ssh")
    assert "specific-host" in argv


def test_probe_ssh_returns_false_when_ssh_binary_missing(env_save_restore, tmp_path):
    # Arrange -- empty PATH so subprocess.run raises FileNotFoundError.
    env_save_restore.set("PATH", str(tmp_path / "empty"))
    # Act
    result = _probe_ssh("any-host")
    # Assert
    assert result is False


# ---------------------------------------------------------------------------
# _ssh_start_agent -- real subprocess.run against shim ssh binary
# ---------------------------------------------------------------------------


def test_ssh_start_agent_returns_true_when_remote_exits_zero(subprocess_shim):
    # Arrange
    subprocess_shim.install("ssh", exit=0)
    # Act
    result = _ssh_start_agent("h", "agent-x")
    # Assert
    assert result is True


def test_ssh_start_agent_returns_false_when_remote_exits_nonzero(subprocess_shim):
    # Arrange
    subprocess_shim.install("ssh", exit=2)
    # Act
    result = _ssh_start_agent("h", "agent-x")
    # Assert
    assert result is False


def test_ssh_start_agent_targets_the_named_host(subprocess_shim):
    # Arrange
    subprocess_shim.install("ssh", exit=0)
    # Act
    _ssh_start_agent("target-host", "my-agent")
    # Assert
    assert "target-host" in subprocess_shim.argv_for("ssh")


def test_ssh_start_agent_runs_sac_start_command_on_target_host(subprocess_shim):
    """The remote command line is `sac agent start <name>`.

    ASSERTED ON THE JOINED COMMAND LINE, not on tokenisation. This used to
    pass the command as ONE argv element (``f"sac agent start {name}"``) and
    now passes four. Both reach the remote identically — ssh joins every token
    after the host with spaces and hands the result to the login shell (see
    ``build_ssh_argv``) — so tokenisation was never the property worth pinning.

    The split form is REQUIRED, not cosmetic: ``_is_sac_invocation`` decides
    whether to inject the registry's ``SCITEX_DIR=<root>`` pin by testing
    ``basename(command[0]) == "sac"``. Against the joined string that basename
    is the whole sentence, so the pin would silently never apply on this path.
    """
    # Arrange
    subprocess_shim.install("ssh", exit=0)
    # Act
    _ssh_start_agent("target-host", "my-agent")
    # Assert
    assert "sac agent start my-agent" in " ".join(subprocess_shim.argv_for("ssh"))


def test_ssh_start_agent_returns_false_when_ssh_binary_missing(
    env_save_restore, tmp_path
):
    # Arrange
    env_save_restore.set("PATH", str(tmp_path / "empty"))
    # Act
    result = _ssh_start_agent("h", "agent-x")
    # Assert
    assert result is False


# ---------------------------------------------------------------------------
# _priority_report -- real YAML on disk, real load_config, real _probe_ssh via shim
# ---------------------------------------------------------------------------


def test_priority_report_marks_multi_instance_agents_as_no_yield(tmp_path):
    # Arrange
    spec = _write_spec(tmp_path, "multi", hosts="all")
    # Act
    out = _priority_report(str(spec), "any-host")
    # Assert
    assert out["mode"] == "multi-instance" and out["should_yield"] is False


def test_priority_report_returns_local_singleton_when_no_host_declared(tmp_path):
    # Arrange
    spec = _write_spec(tmp_path, "anywhere")
    # Act
    out = _priority_report(str(spec), "x")
    # Assert
    assert out["mode"] == "local-singleton" and "no host preference" in out["reason"]


def test_priority_report_returns_local_singleton_when_host_chain_is_empty(tmp_path):
    # Arrange -- empty list goes through the `not host_val` branch.
    spec = _write_spec(tmp_path, "anywhere2", host=[])
    # Act
    out = _priority_report(str(spec), "x")
    # Assert
    assert out["mode"] == "local-singleton"


def test_priority_report_does_not_yield_when_already_on_top_priority(tmp_path):
    # Arrange
    spec = _write_spec(tmp_path, "rank1", host=["a", "b"])
    # Act
    out = _priority_report(str(spec), "a")
    # Assert
    assert out["should_yield"] is False and out["current_rank"] == 1


def test_priority_report_flags_current_host_not_in_chain(tmp_path):
    # Arrange
    spec = _write_spec(tmp_path, "off-chain", host=["a", "b"])
    # Act
    out = _priority_report(str(spec), "z")
    # Assert
    assert "not in the priority chain" in out["reason"]


def test_priority_report_does_not_yield_when_off_chain(tmp_path):
    # Arrange
    spec = _write_spec(tmp_path, "off-chain2", host=["a", "b"])
    # Act
    out = _priority_report(str(spec), "z")
    # Assert
    assert out["should_yield"] is False


def test_priority_report_yields_when_higher_priority_host_is_reachable(
    tmp_path, subprocess_shim
):
    # Arrange -- shim ssh exits 0, so every higher host appears reachable.
    spec = _write_spec(tmp_path, "yield-up", host=["a", "b", "c"])
    subprocess_shim.install("ssh", exit=0)
    # Act
    out = _priority_report(str(spec), "b")
    # Assert
    assert out["should_yield"] is True


def test_priority_report_lists_reachable_higher_hosts_when_yielding(
    tmp_path, subprocess_shim
):
    # Arrange
    spec = _write_spec(tmp_path, "yield-up2", host=["a", "b", "c"])
    subprocess_shim.install("ssh", exit=0)
    # Act
    out = _priority_report(str(spec), "b")
    # Assert
    assert out["reachable_higher_hosts"] == ["a"]


def test_priority_report_stays_when_higher_priority_hosts_unreachable(
    tmp_path, subprocess_shim
):
    # Arrange -- shim exits 1: every higher host appears unreachable.
    spec = _write_spec(tmp_path, "stay", host=["a", "b"])
    subprocess_shim.install("ssh", exit=1)
    # Act
    out = _priority_report(str(spec), "b")
    # Assert
    assert out["should_yield"] is False


def test_priority_report_records_unreachable_higher_hosts(tmp_path, subprocess_shim):
    # Arrange
    spec = _write_spec(tmp_path, "stay2", host=["a", "b"])
    subprocess_shim.install("ssh", exit=1)
    # Act
    out = _priority_report(str(spec), "b")
    # Assert
    assert out["unreachable_higher_hosts"] == ["a"]


def test_priority_report_normalises_string_host_into_chain_of_one(tmp_path):
    # Arrange
    spec = _write_spec(tmp_path, "solo", host="solo")
    # Act
    out = _priority_report(str(spec), "solo")
    # Assert
    assert out["current_rank"] == 1


# ---------------------------------------------------------------------------
# check-priority CLI -- real YAML on disk, real hostname env override
# ---------------------------------------------------------------------------


def test_check_priority_exits_2_when_config_path_does_not_exist(tmp_path):
    # Arrange
    missing = tmp_path / "does-not-exist.yaml"
    runner = CliRunner()
    # Act
    result = runner.invoke(priority_check, [str(missing), "--json"])
    # Assert
    assert result.exit_code == 2


def test_check_priority_exits_2_on_malformed_yaml(tmp_path):
    # Arrange -- write a real spec.yaml that fails v3 validation.
    bad_dir = tmp_path / "bad"
    bad_dir.mkdir()
    bad_spec = bad_dir / "spec.yaml"
    bad_spec.write_text("apiVersion: WRONG/v0\nspec: {}\n")
    runner = CliRunner()
    # Act
    result = runner.invoke(priority_check, [str(bad_spec), "--json"])
    # Assert
    assert result.exit_code == 2


def test_check_priority_emits_error_payload_in_json_on_failure(tmp_path):
    # Arrange
    bad_dir = tmp_path / "bad2"
    bad_dir.mkdir()
    bad_spec = bad_dir / "spec.yaml"
    bad_spec.write_text("apiVersion: WRONG/v0\nspec: {}\n")
    runner = CliRunner()
    # Act
    result = runner.invoke(priority_check, [str(bad_spec), "--json"])
    payload = json.loads(result.stdout)
    # Assert
    assert "error" in payload


def test_check_priority_exits_1_when_higher_priority_host_reachable(
    tmp_path, subprocess_shim, pin_hostname
):
    # Arrange
    spec = _write_spec(tmp_path, "yield-cli", host=["a", "b"])
    subprocess_shim.install("ssh", exit=0)
    pin_hostname("b")
    runner = CliRunner()
    # Act
    result = runner.invoke(priority_check, [str(spec)])
    # Assert
    assert result.exit_code == 1


def test_check_priority_prints_yield_marker_when_yielding(
    tmp_path, subprocess_shim, pin_hostname
):
    # Arrange
    spec = _write_spec(tmp_path, "yield-cli2", host=["a", "b"])
    subprocess_shim.install("ssh", exit=0)
    pin_hostname("b")
    runner = CliRunner()
    # Act
    result = runner.invoke(priority_check, [str(spec)])
    # Assert
    assert "YIELD" in result.output


def test_check_priority_exits_0_when_already_on_top_host(tmp_path, pin_hostname):
    # Arrange
    spec = _write_spec(tmp_path, "stay-cli", host=["a", "b"])
    pin_hostname("a")
    runner = CliRunner()
    # Act
    result = runner.invoke(priority_check, [str(spec)])
    # Assert
    assert result.exit_code == 0


def test_check_priority_prints_stay_marker_when_not_yielding(tmp_path, pin_hostname):
    # Arrange
    spec = _write_spec(tmp_path, "stay-cli2", host=["a", "b"])
    pin_hostname("a")
    runner = CliRunner()
    # Act
    result = runner.invoke(priority_check, [str(spec)])
    # Assert
    assert "STAY" in result.output


def test_check_priority_json_mode_emits_agent_name(tmp_path, pin_hostname):
    # Arrange
    spec = _write_spec(tmp_path, "json-agent", host=["a"])
    pin_hostname("a")
    runner = CliRunner()
    # Act
    result = runner.invoke(priority_check, [str(spec), "--json"])
    payload = json.loads(result.stdout)
    # Assert
    assert payload["agent"] == "json-agent"


def test_check_priority_honours_explicit_current_host_flag(
    tmp_path, subprocess_shim, pin_hostname
):
    # Arrange -- env says "a" (would stay), but --current-host says "b" (yield).
    spec = _write_spec(tmp_path, "explicit-host", host=["a", "b"])
    subprocess_shim.install("ssh", exit=0)
    pin_hostname("a")
    runner = CliRunner()
    # Act
    result = runner.invoke(priority_check, [str(spec), "--current-host", "b", "--json"])
    payload = json.loads(result.stdout)
    # Assert
    assert payload["current_host"] == "b"


# ---------------------------------------------------------------------------
# singleton-reconcile -- real registry on disk, real hostname env
# ---------------------------------------------------------------------------


def test_reconcile_exits_0_when_no_agents_registered(tmp_registry, pin_hostname):
    # Arrange
    pin_hostname("h")
    runner = CliRunner()
    # Act
    result = runner.invoke(singleton_reconcile, [])
    # Assert
    assert result.exit_code == 0


def test_reconcile_exits_1_when_yield_recommended_dry_run(
    tmp_path, tmp_registry, subprocess_shim, pin_hostname
):
    # Arrange -- singleton on rank-2 host with rank-1 reachable.
    spec = _write_spec(tmp_path, "ag", host=["a", "b"])
    _register(tmp_registry, "ag", spec)
    subprocess_shim.install("ssh", exit=0)
    pin_hostname("b")
    runner = CliRunner()
    # Act
    result = runner.invoke(singleton_reconcile, [])
    # Assert
    assert result.exit_code == 1


def test_reconcile_prints_yield_marker_in_dry_run(
    tmp_path, tmp_registry, subprocess_shim, pin_hostname
):
    # Arrange
    spec = _write_spec(tmp_path, "ag2", host=["a", "b"])
    _register(tmp_registry, "ag2", spec)
    subprocess_shim.install("ssh", exit=0)
    pin_hostname("b")
    runner = CliRunner()
    # Act
    result = runner.invoke(singleton_reconcile, [])
    # Assert
    assert "YIELD" in result.output


def test_reconcile_execute_invokes_local_agent_stop(
    tmp_path, tmp_registry, subprocess_shim, pin_hostname
):
    # Arrange -- execute mode: ssh shim succeeds, agent_stop seam records call.
    spec = _write_spec(tmp_path, "ag-exec", host=["a", "b"])
    _register(tmp_registry, "ag-exec", spec)
    subprocess_shim.install("ssh", exit=0)
    pin_hostname("b")
    runner = CliRunner()
    # Act
    with _swap_agent_stop(lambda n: True) as stopped:
        runner.invoke(singleton_reconcile, ["--execute"])
    # Assert
    assert stopped == ["ag-exec"]


def test_reconcile_execute_exits_0_on_successful_handover(
    tmp_path, tmp_registry, subprocess_shim, pin_hostname
):
    # Arrange
    spec = _write_spec(tmp_path, "ag-exec2", host=["a", "b"])
    _register(tmp_registry, "ag-exec2", spec)
    subprocess_shim.install("ssh", exit=0)
    pin_hostname("b")
    runner = CliRunner()
    # Act
    with _swap_agent_stop(lambda n: True):
        result = runner.invoke(singleton_reconcile, ["--execute"])
    # Assert
    assert result.exit_code == 0


def test_reconcile_execute_prints_yielded_marker_on_success(
    tmp_path, tmp_registry, subprocess_shim, pin_hostname
):
    # Arrange
    spec = _write_spec(tmp_path, "ag-exec3", host=["a", "b"])
    _register(tmp_registry, "ag-exec3", spec)
    subprocess_shim.install("ssh", exit=0)
    pin_hostname("b")
    runner = CliRunner()
    # Act
    with _swap_agent_stop(lambda n: True):
        result = runner.invoke(singleton_reconcile, ["--execute"])
    # Assert
    assert "yielded" in result.output


def test_reconcile_exits_2_when_yaml_invalid_and_no_yield(
    tmp_path, tmp_registry, pin_hostname
):
    # Arrange -- registered agent points at a config that fails validation.
    bad_dir = tmp_path / "bad-ag"
    bad_dir.mkdir()
    bad_spec = bad_dir / "spec.yaml"
    bad_spec.write_text("apiVersion: WRONG/v0\nspec: {}\n")
    _register(tmp_registry, "bad-ag", bad_spec)
    pin_hostname("a")
    runner = CliRunner()
    # Act
    result = runner.invoke(singleton_reconcile, [])
    # Assert
    assert result.exit_code == 2


def test_reconcile_surfaces_yaml_error_in_output(tmp_path, tmp_registry, pin_hostname):
    # Arrange
    bad_dir = tmp_path / "bad-ag2"
    bad_dir.mkdir()
    bad_spec = bad_dir / "spec.yaml"
    bad_spec.write_text("apiVersion: WRONG/v0\nspec: {}\n")
    _register(tmp_registry, "bad-ag2", bad_spec)
    pin_hostname("a")
    runner = CliRunner()
    # Act
    result = runner.invoke(singleton_reconcile, [])
    # Assert
    assert "validation failed" in result.output or "error" in result.output.lower()


def test_reconcile_exits_0_when_all_agents_are_on_correct_host(
    tmp_path, tmp_registry, pin_hostname
):
    # Arrange
    spec = _write_spec(tmp_path, "x", host=["a", "b"])
    _register(tmp_registry, "x", spec)
    pin_hostname("a")
    runner = CliRunner()
    # Act
    result = runner.invoke(singleton_reconcile, [])
    # Assert
    assert result.exit_code == 0


def test_reconcile_json_mode_emits_array_with_agent_entries(
    tmp_path, tmp_registry, pin_hostname
):
    # Arrange
    spec = _write_spec(tmp_path, "x2", host=["a", "b"])
    _register(tmp_registry, "x2", spec)
    pin_hostname("a")
    runner = CliRunner()
    # Act
    result = runner.invoke(singleton_reconcile, ["--json"])
    payload = json.loads(result.stdout)
    # Assert
    assert payload[0]["agent"] == "x2"
