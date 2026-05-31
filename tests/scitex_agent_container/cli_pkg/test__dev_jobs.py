"""Tests for ``sac dev {cron,daemon,systemd}`` federated-job commands.

No-mocks (PA-306): the production seam is the lazy ``from scitex_dev.jobs
import jobs_of_kind`` inside ``_dev_jobs``. We drive that seam by
installing a real, hand-rolled ``scitex_dev.jobs`` module into
``sys.modules`` (present-case) or by forcing its import to fail
(degrade-case) — exercising the exact code path production hits when the
installed scitex-dev does / does not ship the jobs contract.

AAA marker comments; one assertion per test.
"""

from __future__ import annotations

import sys
import types
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

from click.testing import CliRunner

from scitex_agent_container.cli_pkg.dev_group import dev_group


@dataclass
class _Job:
    name: str
    schedule: str
    command: str
    description: str
    kind: str = "systemd"
    on_boot_sec: str | None = None
    on_unit_active_sec: str | None = None
    timeout_sec: int | None = None


def _sac_systemd_job() -> _Job:
    return _Job(
        name="sac.accounts-refresh",
        schedule="0 */2 * * *",
        command="sac accounts refresh --all --skip-active",
        description="Headless OAuth refresh.",
        kind="systemd",
        on_unit_active_sec="2h",
    )


@contextmanager
def _jobs_present(jobs: list[_Job]) -> Iterator[None]:
    """Install a real fake ``scitex_dev.jobs`` exposing ``jobs_of_kind``."""
    mod = types.ModuleType("scitex_dev.jobs")

    def jobs_of_kind(kind: str) -> list[_Job]:
        return [j for j in jobs if j.kind == kind]

    mod.jobs_of_kind = jobs_of_kind  # type: ignore[attr-defined]
    saved = sys.modules.get("scitex_dev.jobs")
    sys.modules["scitex_dev.jobs"] = mod
    try:
        yield
    finally:
        if saved is None:
            del sys.modules["scitex_dev.jobs"]
        else:
            sys.modules["scitex_dev.jobs"] = saved


@contextmanager
def _jobs_absent() -> Iterator[None]:
    """Force ``import scitex_dev.jobs`` to raise ImportError."""
    saved = sys.modules.get("scitex_dev.jobs")
    sys.modules["scitex_dev.jobs"] = None  # type: ignore[assignment]
    try:
        yield
    finally:
        if saved is None:
            del sys.modules["scitex_dev.jobs"]
        else:
            sys.modules["scitex_dev.jobs"] = saved


# ---------------------------------------------------------------------------
# list — present case
# ---------------------------------------------------------------------------


def test_systemd_list_shows_only_sac_jobs() -> None:
    # Arrange — one sac systemd job + a foreign systemd job that must NOT show.
    foreign = _Job("other.thing", "0 * * * *", "do", "x", kind="systemd")
    with _jobs_present([_sac_systemd_job(), foreign]):
        runner = CliRunner()
        # Act
        result = runner.invoke(dev_group, ["systemd", "list"])
    # Assert — sac's job is listed, the foreign one filtered out.
    assert (
        "sac.accounts-refresh" in result.output and "other.thing" not in result.output
    )


def test_systemd_list_exit_zero_when_present() -> None:
    # Arrange
    with _jobs_present([_sac_systemd_job()]):
        runner = CliRunner()
        # Act
        result = runner.invoke(dev_group, ["systemd", "list"])
    # Assert
    assert result.exit_code == 0


def test_cron_list_empty_when_sac_has_no_cron_jobs() -> None:
    # Arrange — sac only has a systemd job.
    with _jobs_present([_sac_systemd_job()]):
        runner = CliRunner()
        # Act
        result = runner.invoke(dev_group, ["cron", "list"])
    # Assert
    assert "No sac cron-kind jobs." in result.output


# ---------------------------------------------------------------------------
# degrade — absent case
# ---------------------------------------------------------------------------


def test_list_degrades_with_upgrade_hint_when_jobs_absent() -> None:
    # Arrange
    with _jobs_absent():
        runner = CliRunner()
        # Act
        result = runner.invoke(dev_group, ["systemd", "list"])
    # Assert — upgrade hint surfaces (no stack trace).
    text = (result.output or "") + (getattr(result, "stderr", "") or "")
    assert "requires scitex-dev>=" in text


def test_list_degrade_exit_code_nonzero() -> None:
    # Arrange
    with _jobs_absent():
        runner = CliRunner()
        # Act
        result = runner.invoke(dev_group, ["systemd", "list"])
    # Assert — clean non-zero exit, not an unhandled exception.
    assert result.exit_code == 3 and result.exception.__class__ is SystemExit


def test_install_degrades_when_jobs_absent() -> None:
    # Arrange
    with _jobs_absent():
        runner = CliRunner()
        # Act
        result = runner.invoke(dev_group, ["systemd", "install", "-y"])
    # Assert
    text = (result.output or "") + (getattr(result, "stderr", "") or "")
    assert "requires scitex-dev>=" in text


# ---------------------------------------------------------------------------
# verb consistency with scitex-dev ecosystem aggregator
#
# Canonical verbs per job-kind (scitex-dev ecosystem):
#   cron    → list / install / uninstall
#   systemd → list / install / uninstall
#   daemon  → list / exec   (a daemon is *run*, not "installed")
# ---------------------------------------------------------------------------


def _verbs_of(kind: str) -> set[str]:
    """The leaf subcommand names exposed under ``sac dev <kind>``."""
    grp = dev_group.commands[kind]
    return set(grp.commands)  # type: ignore[attr-defined]


def test_cron_verbs_are_list_install_uninstall() -> None:
    # Arrange
    kind = "cron"
    # Act
    verbs = _verbs_of(kind)
    # Assert
    assert verbs == {"list", "install", "uninstall"}


def test_systemd_verbs_are_list_install_uninstall() -> None:
    # Arrange
    kind = "systemd"
    # Act
    verbs = _verbs_of(kind)
    # Assert
    assert verbs == {"list", "install", "uninstall"}


def test_daemon_verbs_are_list_exec() -> None:
    # Arrange — daemon is run, not installed: list + exec, no
    # install/uninstall (matches scitex-dev ecosystem daemon).
    kind = "daemon"
    # Act
    verbs = _verbs_of(kind)
    # Assert
    assert verbs == {"list", "exec"}


def test_daemon_has_no_install_verb() -> None:
    # Arrange
    kind = "daemon"
    # Act
    verbs = _verbs_of(kind)
    # Assert — explicit: the wrong install/uninstall verbs are gone.
    assert "install" not in verbs and "uninstall" not in verbs


def test_daemon_exec_takes_positional_name_argument() -> None:
    # Arrange — exec mirrors `scitex-dev ecosystem daemon exec <name>`.
    exec_cmd = dev_group.commands["daemon"].commands["exec"]  # type: ignore[attr-defined]
    # Act
    arg_names = [p.name for p in exec_cmd.params if p.param_type_name == "argument"]
    # Assert
    assert arg_names == ["name"]


def _sac_daemon_job() -> _Job:
    return _Job(
        name="sac.watcher",
        schedule="-",
        command="sac listen --forever",
        description="Long-running sac watcher.",
        kind="daemon",
    )


def test_daemon_exec_delegates_to_scitex_dev_with_positional_name() -> None:
    # Arrange — capture the (kind, verb, name) the exec verb delegates with.
    import scitex_agent_container.cli_pkg._dev_jobs as dj

    captured: list[tuple] = []
    original = dj._ecosystem_delegate
    dj._ecosystem_delegate = lambda *a, **k: (captured.append(a) or 0)  # type: ignore[assignment]
    try:
        with _jobs_present([_sac_daemon_job()]):
            runner = CliRunner()
            # Act
            runner.invoke(dev_group, ["daemon", "exec", "sac.watcher"])
    finally:
        dj._ecosystem_delegate = original  # type: ignore[assignment]
    # Assert — delegated as (kind="daemon", verb="exec", name="sac.watcher").
    assert captured == [("daemon", "exec", "sac.watcher")]


def test_daemon_exec_rejects_unknown_job() -> None:
    # Arrange — only sac.watcher exists.
    with _jobs_present([_sac_daemon_job()]):
        runner = CliRunner()
        # Act
        result = runner.invoke(dev_group, ["daemon", "exec", "does.not.exist"])
    # Assert — clean ClickException, not a delegated run.
    assert result.exit_code != 0 and "unknown sac daemon job" in result.output


def test_daemon_exec_degrades_when_jobs_absent() -> None:
    # Arrange
    with _jobs_absent():
        runner = CliRunner()
        # Act
        result = runner.invoke(dev_group, ["daemon", "exec", "sac.watcher"])
    # Assert
    text = (result.output or "") + (getattr(result, "stderr", "") or "")
    assert "requires scitex-dev>=" in text
