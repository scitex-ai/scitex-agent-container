"""Tests for ``sac dev {cron,systemd}`` federated-job commands.

WHY THIS FILE WAS REWRITTEN — it is the fixture that hid the bug.

It used to install a hand-rolled fake ``scitex_dev.jobs`` module into
``sys.modules``, whose ``_Job`` dataclass defaulted to ``kind="systemd"``.
No real JobSpec can have that kind: ``ALLOWED_KINDS`` is
``{service,timer,cron}`` and ``JobSpec.validate()`` raises on anything
else at construction. So the suite asserted, in green, that ``sac dev
systemd list`` shows ``sac.accounts-refresh`` — while in production that
command printed "No sac systemd-kind jobs." and exited 0, because the
group name was being passed straight through as the kind filter and all
four of sac's real timers are ``kind="timer"``.

A fake whose shape no real object can have does not test the production
path; it tests the fake. That is the same failure as the twin-spawning
suite (29 green tests over a ``spec.env`` shape v3 validation rejects) and
it is why these tests now drive the REAL ``scitex_dev.jobs`` with REAL
``JobSpec`` objects. If the contract is not installed, the file skips —
it does not invent a stand-in.

No mocks (PA-306). The one seam still injected is ``_ecosystem_delegate``,
which would otherwise shell out to a real ``scitex-dev`` subprocess and
mutate the host's crontab/units; the delegation ARGUMENTS are the thing
under test there, and they are captured, not faked.

AAA marker comments; one assertion per test.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from typing import Iterator

import pytest
from click.testing import CliRunner

jobs_mod = pytest.importorskip(
    "scitex_dev.jobs",
    reason="installed scitex-dev predates the scitex_dev.jobs contract",
)

from scitex_agent_container.cli_pkg._dev_jobs import GROUP_KINDS  # noqa: E402
from scitex_agent_container.cli_pkg.dev_group import dev_group  # noqa: E402


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
# list — against the REAL provider and the REAL taxonomy
# ---------------------------------------------------------------------------


def test_systemd_list_shows_sacs_real_timers() -> None:
    # Arrange — the regression that matters: sac's four jobs are all
    # kind="timer", and `ecosystem systemd` selects timer+service. This
    # command listed NOTHING for weeks while the old fake made it green.
    runner = CliRunner()
    # Act
    result = runner.invoke(dev_group, ["systemd", "list"])
    # Assert
    assert "sac.accounts-refresh" in result.output


def test_systemd_list_shows_every_declared_sac_job() -> None:
    # Arrange — all four, not just the one a pinning test happens to name.
    from scitex_agent_container._jobs_plugin import provide_jobs

    expected = [j.name for j in provide_jobs() if j.kind in GROUP_KINDS["systemd"]]
    runner = CliRunner()
    # Act
    result = runner.invoke(dev_group, ["systemd", "list"])
    # Assert
    assert all(name in result.output for name in expected)


def test_systemd_list_exit_zero() -> None:
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(dev_group, ["systemd", "list"])
    # Assert
    assert result.exit_code == 0


def test_systemd_list_filters_out_foreign_jobs() -> None:
    # Arrange — scitex-dev's own built-in jobs are discoverable too; only
    # sac.* may show here.
    runner = CliRunner()
    # Act
    result = runner.invoke(dev_group, ["systemd", "list", "--json"])
    # Assert
    import json as _json

    assert all(j["name"].startswith("sac.") for j in _json.loads(result.output))


def test_systemd_list_json_reports_the_real_kind() -> None:
    # Arrange — the JSON surfaces `kind` precisely so a future taxonomy
    # drift is visible rather than silently filtered to nothing.
    runner = CliRunner()
    # Act
    result = runner.invoke(dev_group, ["systemd", "list", "--json"])
    # Assert
    import json as _json

    assert {j["kind"] for j in _json.loads(result.output)} == {"timer"}


def test_cron_list_is_empty_because_sac_declares_no_cron_jobs() -> None:
    # Arrange — a TRUE empty (sac has no kind="cron" job), unlike the
    # systemd group's old empty, which was a bug wearing the same output.
    runner = CliRunner()
    # Act
    result = runner.invoke(dev_group, ["cron", "list"])
    # Assert
    assert "No sac cron jobs." in result.output


# ---------------------------------------------------------------------------
# the dead group stays dead
# ---------------------------------------------------------------------------


def test_there_is_no_daemon_group() -> None:
    # Arrange — `sac dev daemon` was dead in BOTH halves: it filtered
    # kind="daemon" (never legal, so always zero jobs) AND delegated to
    # `scitex-dev ecosystem daemon`, which is not an ecosystem subcommand
    # at all. A long-running job is kind="service", via the systemd group.
    groups = dev_group.commands
    # Act
    present = "daemon" in groups
    # Assert
    assert present is False


def test_job_groups_are_exactly_the_group_kinds_ssot() -> None:
    # Arrange — the CLI surface is derived from GROUP_KINDS, so a group can
    # never again exist without a declared kind filter behind it.
    expected = set(GROUP_KINDS)
    # Act
    present = {g for g in expected if g in dev_group.commands}
    # Assert
    assert present == expected


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
# verb consistency with the scitex-dev ecosystem aggregator
# ---------------------------------------------------------------------------


def _verbs_of(group: str) -> set[str]:
    """The leaf subcommand names exposed under ``sac dev <group>``."""
    grp = dev_group.commands[group]
    return set(grp.commands)  # type: ignore[attr-defined]


def test_cron_verbs_are_list_install_uninstall() -> None:
    # Arrange
    group = "cron"
    # Act
    verbs = _verbs_of(group)
    # Assert
    assert verbs == {"list", "install", "uninstall"}


def test_systemd_verbs_are_list_install_uninstall() -> None:
    # Arrange
    group = "systemd"
    # Act
    verbs = _verbs_of(group)
    # Assert
    assert verbs == {"list", "install", "uninstall"}


def test_install_delegates_to_the_matching_ecosystem_group() -> None:
    # Arrange — capture the (group, verb, name) tuples install delegates
    # with, rather than shelling out to a real scitex-dev that would
    # rewrite the host's systemd units.
    import scitex_agent_container.cli_pkg._dev_jobs as dj

    captured: list[tuple] = []
    original = dj._ecosystem_delegate
    dj._ecosystem_delegate = lambda *a, **k: captured.append(a) or 0  # type: ignore[assignment]
    try:
        runner = CliRunner()
        # Act
        runner.invoke(dev_group, ["systemd", "install", "-y"])
    finally:
        dj._ecosystem_delegate = original  # type: ignore[assignment]
    # Assert — every delegation targets `ecosystem systemd install`.
    assert captured and all(a[:2] == ("systemd", "install") for a in captured)


def test_install_delegates_once_per_declared_timer() -> None:
    # Arrange — the count is the point: a group that silently matched zero
    # jobs delegated zero times and still exited 0.
    import scitex_agent_container.cli_pkg._dev_jobs as dj
    from scitex_agent_container._jobs_plugin import provide_jobs

    expected = len([j for j in provide_jobs() if j.kind in GROUP_KINDS["systemd"]])
    captured: list[tuple] = []
    original = dj._ecosystem_delegate
    dj._ecosystem_delegate = lambda *a, **k: captured.append(a) or 0  # type: ignore[assignment]
    try:
        runner = CliRunner()
        # Act
        runner.invoke(dev_group, ["systemd", "install", "-y"])
    finally:
        dj._ecosystem_delegate = original  # type: ignore[assignment]
    # Assert
    assert len(captured) == expected
