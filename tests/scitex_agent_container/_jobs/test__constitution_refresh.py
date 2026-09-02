"""``_constitution_refresh`` — the source-side port of the hand-written
``sac-constitution-refresh.sh``, exercised against REAL temp overlays.

No mocks. Every test builds a real overlays root under ``tmp_path`` with the
exact layout a running agent reads (``<overlay>/upper/home/agent/.claude/
commands/constitution.md``) and asserts on what the pass left on disk as
well as on what it reported — a count that disagrees with the disk is the
very bug the read-back exists to catch.

Also pins the two declarations that make the engine a JOB rather than a
script: the JobSpec ``provide_jobs()`` yields for it, and the migration row
that retires the hand-written unit pair it replaces.

AAA markers; one assertion per test.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from scitex_agent_container._jobs import _constitution_refresh as cr
from scitex_agent_container._jobs._migrate import _renames
from scitex_agent_container.cli_pkg._agents_refresh_constitution import (
    refresh_constitution,
)

JOB_NAME = "scitex-agent-container-constitution-refresh"

#: Comfortably over the truncation floor, deterministic, and different from
#: the stale copy below.
CURRENT = ("# constitution\n" + "- a rule that must reach every agent\n" * 400).encode()
STALE = b"# constitution\nan older snapshot\n"
#: A fixed past mtime; an unnecessary rewrite would move it.
PAST_NS = 1_600_000_000 * 10**9


def _agent_home(root: Path, name: str) -> Path:
    home = root / name / "upper" / "home" / "agent"
    home.mkdir(parents=True)
    return home


def _copy_in(home: Path, data: bytes) -> Path:
    target = home / ".claude" / "commands" / "constitution.md"
    target.parent.mkdir(parents=True)
    target.write_bytes(data)
    return target


@pytest.fixture
def source(tmp_path: Path) -> Path:
    path = tmp_path / "constitution.md"
    path.write_bytes(CURRENT)
    return path


@pytest.fixture
def overlays(tmp_path: Path) -> Path:
    root = tmp_path / "overlays"
    root.mkdir()
    return root


# ---------------------------------------------------------------------------
# The source gate: exit 2, nothing attempted.
# ---------------------------------------------------------------------------


def test_a_source_under_the_floor_is_refused_with_exit_2(
    tmp_path: Path, overlays: Path
) -> None:
    # Arrange — a 7-byte constitution is a truncated write, not an edit.
    small = tmp_path / "constitution.md"
    small.write_bytes(b"# stub\n")
    # Act
    result = cr.refresh(small, overlays, dry_run=False)
    # Assert
    assert result.exit_code == cr.EXIT_SOURCE


def test_a_refused_source_is_not_fanned_out(tmp_path: Path, overlays: Path) -> None:
    # Arrange — refusing the source must also mean writing nothing.
    small = tmp_path / "constitution.md"
    small.write_bytes(b"# stub\n")
    home = _agent_home(overlays, "alpha")
    # Act
    cr.refresh(small, overlays, dry_run=False)
    # Assert
    assert not (home / ".claude").exists()


def test_a_missing_source_is_refused_with_exit_2(
    tmp_path: Path, overlays: Path
) -> None:
    # Arrange
    missing = tmp_path / "absent.md"
    # Act
    result = cr.refresh(missing, overlays, dry_run=False)
    # Assert
    assert result.exit_code == cr.EXIT_SOURCE


def test_a_refused_source_renders_as_fatal(tmp_path: Path, overlays: Path) -> None:
    # Arrange
    missing = tmp_path / "absent.md"
    # Act
    report = cr.render(cr.refresh(missing, overlays, dry_run=False))
    # Assert
    assert report.splitlines()[-1].startswith("FATAL: ")


# ---------------------------------------------------------------------------
# One overlay, each state it can be in.
# ---------------------------------------------------------------------------


def test_a_stale_overlay_is_rewritten_and_read_back(source: Path, overlays: Path) -> None:
    # Arrange — a copy that differs from the source.
    target = _copy_in(_agent_home(overlays, "alpha"), STALE)
    # Act
    result = cr.refresh(source, overlays, dry_run=False)
    # Assert — counted as updated only because the read-back matched.
    assert (result.exit_code, result.updated, target.read_bytes()) == (0, 1, CURRENT)


def test_a_current_overlay_is_left_alone(source: Path, overlays: Path) -> None:
    # Arrange — same bytes already there, mtime pinned in the past.
    target = _copy_in(_agent_home(overlays, "alpha"), CURRENT)
    os.utime(target, ns=(PAST_NS, PAST_NS))
    # Act
    result = cr.refresh(source, overlays, dry_run=False)
    # Assert — no write happened, so the mtime did not move.
    assert (result.ok, target.stat().st_mtime_ns) == (1, PAST_NS)


def test_a_new_overlay_gets_a_created_copy(source: Path, overlays: Path) -> None:
    # Arrange — an agent home with no .claude/commands yet.
    _agent_home(overlays, "alpha")
    # Act
    result = cr.refresh(source, overlays, dry_run=False)
    # Assert
    assert (result.new, cr.target_in(overlays / "alpha").read_bytes()) == (1, CURRENT)


def test_an_overlay_without_an_agent_home_is_skipped(
    source: Path, overlays: Path
) -> None:
    # Arrange — a directory under the root that is not an agent overlay.
    (overlays / "scratch").mkdir()
    # Act
    result = cr.refresh(source, overlays, dry_run=False)
    # Assert
    assert (result.skipped, result.exit_code) == (1, 0)


def test_a_skipped_overlay_is_not_written_into(source: Path, overlays: Path) -> None:
    # Arrange
    (overlays / "scratch").mkdir()
    # Act
    cr.refresh(source, overlays, dry_run=False)
    # Assert
    assert list((overlays / "scratch").iterdir()) == []


def test_an_uncreatable_commands_dir_counts_as_failed(
    source: Path, overlays: Path
) -> None:
    # Arrange — `.claude` is a FILE, so `.claude/commands` cannot be made.
    home = _agent_home(overlays, "alpha")
    (home / ".claude").write_bytes(b"not a directory\n")
    # Act
    result = cr.refresh(source, overlays, dry_run=False)
    # Assert
    assert (result.exit_code, result.failed) == (cr.EXIT_FAILED, 1)


def test_a_failure_names_the_overlay(source: Path, overlays: Path) -> None:
    # Arrange
    home = _agent_home(overlays, "alpha")
    (home / ".claude").write_bytes(b"not a directory\n")
    # Act
    result = cr.refresh(source, overlays, dry_run=False)
    # Assert
    assert result.failures[0].startswith("alpha: ")


def test_a_failed_overlay_does_not_stop_the_others(
    source: Path, overlays: Path
) -> None:
    # Arrange — alpha cannot be refreshed; beta must still be.
    (_agent_home(overlays, "alpha") / ".claude").write_bytes(b"x\n")
    _agent_home(overlays, "beta")
    # Act
    result = cr.refresh(source, overlays, dry_run=False)
    # Assert
    assert (result.failed, result.new) == (1, 1)


# ---------------------------------------------------------------------------
# Dry run: counts, no writes.
# ---------------------------------------------------------------------------


def test_dry_run_counts_what_would_change(source: Path, overlays: Path) -> None:
    # Arrange — one stale copy, one missing copy, one current copy.
    _copy_in(_agent_home(overlays, "alpha"), STALE)
    _agent_home(overlays, "beta")
    _copy_in(_agent_home(overlays, "gamma"), CURRENT)
    # Act
    result = cr.refresh(source, overlays, dry_run=True)
    # Assert
    assert (result.updated, result.new, result.ok, result.exit_code) == (1, 1, 1, 0)


def test_dry_run_writes_nothing(source: Path, overlays: Path) -> None:
    # Arrange
    target = _copy_in(_agent_home(overlays, "alpha"), STALE)
    _agent_home(overlays, "beta")
    # Act
    cr.refresh(source, overlays, dry_run=True)
    # Assert
    assert (target.read_bytes(), cr.target_in(overlays / "beta").exists()) == (
        STALE,
        False,
    )


# ---------------------------------------------------------------------------
# The zero-overlay pass and the report.
# ---------------------------------------------------------------------------


def test_an_absent_overlays_root_is_a_zero_overlay_pass(
    source: Path, tmp_path: Path
) -> None:
    # Arrange — a fleet-wide timer on a host with no agents must not fail
    # its unit; the script's glob iterated nothing, and so does this.
    nowhere = tmp_path / "nowhere"
    # Act
    result = cr.refresh(source, nowhere, dry_run=False)
    # Assert
    assert (result.exit_code, result.summary) == (
        0,
        "  already current=0  updated=0  created=0  failed=0  skipped=0",
    )


def test_the_report_says_when_no_overlays_were_found(
    source: Path, tmp_path: Path
) -> None:
    # Arrange
    nowhere = tmp_path / "nowhere"
    # Act
    report = cr.render(cr.refresh(source, nowhere, dry_run=False))
    # Assert
    assert "no overlays found" in report


def test_the_summary_line_carries_the_scripts_counts(
    source: Path, overlays: Path
) -> None:
    # Arrange
    _copy_in(_agent_home(overlays, "alpha"), STALE)
    # Act
    result = cr.refresh(source, overlays, dry_run=False)
    # Assert
    assert result.summary == "  already current=0  updated=1  created=0  failed=0  skipped=0"


def test_the_report_names_the_source_digest(source: Path, overlays: Path) -> None:
    # Arrange
    # Act
    result = cr.refresh(source, overlays, dry_run=False)
    # Assert
    assert f"md5={result.source_md5[:8]}" in cr.render(result)


def test_the_report_lists_each_failure(source: Path, overlays: Path) -> None:
    # Arrange
    (_agent_home(overlays, "alpha") / ".claude").write_bytes(b"x\n")
    # Act
    report = cr.render(cr.refresh(source, overlays, dry_run=False))
    # Assert
    assert "  FAILED: alpha: " in report


# ---------------------------------------------------------------------------
# main(argv) and the defaults.
# ---------------------------------------------------------------------------


def test_main_returns_the_exit_code_and_prints_the_summary(
    source: Path, overlays: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Arrange
    _copy_in(_agent_home(overlays, "alpha"), STALE)
    # Act
    code = cr.main(["--source", str(source), "--overlays-root", str(overlays)])
    # Assert
    assert (code, "updated=1" in capsys.readouterr().out) == (0, True)


def test_main_dry_run_flag_writes_nothing(source: Path, overlays: Path) -> None:
    # Arrange
    target = _copy_in(_agent_home(overlays, "alpha"), STALE)
    # Act
    cr.main(["--dry-run", "--source", str(source), "--overlays-root", str(overlays)])
    # Assert
    assert target.read_bytes() == STALE


def test_main_reports_a_refused_source_as_exit_2(
    tmp_path: Path, overlays: Path
) -> None:
    # Arrange
    missing = tmp_path / "absent.md"
    # Act
    code = cr.main(["--source", str(missing), "--overlays-root", str(overlays)])
    # Assert
    assert code == cr.EXIT_SOURCE


def test_default_source_honours_the_env_override() -> None:
    # Arrange
    env = {cr.SOURCE_ENV: "/somewhere/constitution.md"}
    # Act
    got = cr.default_source(env)
    # Assert
    assert got == Path("/somewhere/constitution.md")


def test_default_source_is_the_host_users_commands_dir() -> None:
    # Arrange — derived from the running user's home, never a spelled login.
    env: dict[str, str] = {}
    # Act
    got = cr.default_source(env)
    # Assert
    assert got == Path.home() / ".claude" / "commands" / "constitution.md"


def test_default_overlays_root_honours_the_env_override() -> None:
    # Arrange
    env = {cr.OVERLAY_ROOT_ENV: "/elsewhere/overlays"}
    # Act
    got = cr.default_overlays_root(env)
    # Assert
    assert got == Path("/elsewhere/overlays")


def test_default_overlays_root_is_sacs_containers_dir() -> None:
    # Arrange
    env: dict[str, str] = {}
    # Act
    got = cr.default_overlays_root(env)
    # Assert
    assert got == Path.home() / ".scitex" / "agent-container" / "containers" / "overlays"


def test_the_target_is_the_overlay_upper_not_the_runtime_home() -> None:
    # Arrange — the trap the script header records: runtime/<agent>/home/
    # looks the same and is read by nobody.
    overlay = Path("/ov/alpha")
    # Act
    got = cr.target_in(overlay)
    # Assert
    assert got == Path("/ov/alpha/upper/home/agent/.claude/commands/constitution.md")


# ---------------------------------------------------------------------------
# The CLI verb the JobSpec command runs.
# ---------------------------------------------------------------------------


def test_the_cli_verb_distributes_and_exits_0(source: Path, overlays: Path) -> None:
    # Arrange
    target = _copy_in(_agent_home(overlays, "alpha"), STALE)
    # Act
    result = CliRunner().invoke(
        refresh_constitution,
        ["--source", str(source), "--overlays-root", str(overlays)],
    )
    # Assert
    assert (result.exit_code, target.read_bytes()) == (0, CURRENT)


def test_the_cli_verb_dry_run_writes_nothing(source: Path, overlays: Path) -> None:
    # Arrange
    target = _copy_in(_agent_home(overlays, "alpha"), STALE)
    # Act
    CliRunner().invoke(
        refresh_constitution,
        ["--dry-run", "--source", str(source), "--overlays-root", str(overlays)],
    )
    # Assert
    assert target.read_bytes() == STALE


def test_the_cli_verb_prints_the_summary_line(source: Path, overlays: Path) -> None:
    # Arrange
    _copy_in(_agent_home(overlays, "alpha"), STALE)
    # Act
    result = CliRunner().invoke(
        refresh_constitution,
        ["--source", str(source), "--overlays-root", str(overlays)],
    )
    # Assert
    assert "already current=0  updated=1" in result.stdout


def test_the_cli_verb_exits_2_on_a_refused_source(
    tmp_path: Path, overlays: Path
) -> None:
    # Arrange
    missing = tmp_path / "absent.md"
    # Act
    result = CliRunner().invoke(
        refresh_constitution,
        ["--source", str(missing), "--overlays-root", str(overlays)],
    )
    # Assert
    assert result.exit_code == cr.EXIT_SOURCE


# ---------------------------------------------------------------------------
# The declaration: a JobSpec, through the real provider and validator.
# ---------------------------------------------------------------------------


@pytest.fixture
def job():
    pytest.importorskip(
        "scitex_dev.jobs",
        reason="installed scitex-dev predates the scitex_dev.jobs contract",
    )
    from ._jobspec_helpers import _job

    return _job(JOB_NAME)


def test_provide_jobs_declares_the_constitution_refresh(job) -> None:
    # Arrange — fetched by name through the real provider.
    # Act
    name = job.name
    # Assert
    assert name == JOB_NAME


def test_the_job_kind_is_timer(job) -> None:
    # Arrange — a periodic systemd --user timer; a bad kind makes
    # `ecosystem up` silently drop sac's WHOLE provider.
    # Act
    kind = job.kind
    # Assert
    assert kind == "timer"


def test_the_job_command_is_the_bounded_verb(job) -> None:
    # Arrange — the bound lives in the COMMAND (the only part a cron line
    # carries); the payload is the absolute `sac`, checked by shape.
    from ._jobspec_helpers import _split_command

    # Act
    bound, _payload, rest = _split_command(job.command)
    # Assert
    assert (bound, rest) == ("/usr/bin/timeout 120", "agents refresh-constitution")


def test_the_job_cadence_is_the_hand_written_units(job) -> None:
    # Arrange — OnBootSec=3min / OnUnitActiveSec=10min, kept from the unit
    # pair this declaration retires rather than re-decided.
    # Act
    cadence = (job.on_boot_sec, job.on_unit_active_sec)
    # Assert
    assert cadence == ("3min", "10min")


def test_the_job_description_names_the_ruling_and_the_incident(job) -> None:
    # Arrange — the two dates a reader needs to find the why.
    # Act
    dated = ("2026-09-02" in job.description, "2026-08-15" in job.description)
    # Assert
    assert dated == (True, True)


# ---------------------------------------------------------------------------
# The migration row that retires the hand-written unit pair.
# ---------------------------------------------------------------------------


def test_the_hand_written_unit_has_a_rename_row() -> None:
    # Arrange
    # Act
    row = _renames.by_local("constitution-refresh")
    # Assert
    assert (row.old, row.new, row.kind) == (
        "sac-constitution-refresh",
        JOB_NAME,
        "timer",
    )


def test_the_rename_retires_both_hand_written_units() -> None:
    # Arrange — displacing only the .timer would leave a runnable .service.
    row = _renames.by_local("constitution-refresh")
    # Act
    units = row.old_units()
    # Assert
    assert units == (
        "sac-constitution-refresh.service",
        "sac-constitution-refresh.timer",
    )


def test_the_rename_is_not_held() -> None:
    # Arrange — two writers of identical bytes are harmless duplication, so
    # there is no supervised window to wait for.
    row = _renames.by_local("constitution-refresh")
    # Act
    held = row.held
    # Assert
    assert held is False
