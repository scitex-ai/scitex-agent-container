"""Tests for ``cli_pkg._main`` -- top-level ``sac`` click entry point.

PA-306 no-mocks coverage closure. Collaborators are real:

* ``CliRunner`` invokes the real Click commands.
* A yield-based ``home_in_tmp`` fixture sets ``$HOME`` directly via
  ``os.environ`` (no ``monkeypatch``) so ``Path.home()`` lands in the
  test sandbox -- the cache-based install actually writes
  ``~/.scitex/agent-container/runtime/completion/<binary>``, symlinks
  ``~/.local/share/bash-completion/scitex/<binary>``, and appends to
  the real ``~/.bashrc`` / ``~/.zshrc`` under tmp_path.
* The ``scitex-agent-container`` and ``sac`` binaries shipped in the
  project venv generate real completion scripts via subprocess.
* The off-tree ``PackageNotFoundError`` branch is exercised by passing
  a real ``importlib.metadata.version`` call pinned to a dist name
  that genuinely doesn't exist -- no monkey-patching.
"""

from __future__ import annotations

import os
import sys
from importlib.metadata import version as real_version_lookup

import click
import pytest
from click.testing import CliRunner

from scitex_agent_container.cli_pkg._main import (
    _MainGroup,
    _pkg_version,
    cli_entry_point,
    main,
)

# ---------------------------------------------------------------------------
# Fixtures (yield-based, no monkeypatch)
# ---------------------------------------------------------------------------


@pytest.fixture
def home_in_tmp(tmp_path):
    """Point Path.home() at tmp_path by overriding HOME / USERPROFILE.

    Yield-based: restores prior env values on teardown so the global
    process env isn't permanently mutated. No ``monkeypatch``.
    """
    # Arrange
    prior = {k: os.environ.get(k) for k in ("HOME", "USERPROFILE")}
    os.environ["HOME"] = str(tmp_path)
    os.environ["USERPROFILE"] = str(tmp_path)
    try:
        yield tmp_path
    finally:
        for k, v in prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@pytest.fixture
def saved_argv():
    """Save and restore ``sys.argv`` without monkeypatch."""
    original = list(sys.argv)
    try:
        yield
    finally:
        sys.argv[:] = original


# ---------------------------------------------------------------------------
# _pkg_version: installed + fallback
# ---------------------------------------------------------------------------


def test_pkg_version_returns_installed_string():
    # Arrange
    # (package is installed in the venv; no setup needed)
    # Act
    result = _pkg_version()
    # Assert
    assert isinstance(result, str) and result != ""


def test_pkg_version_falls_back_to_dev_off_tree():
    # Arrange: a real importlib.metadata.version call pinned to a
    # genuinely uninstalled dist name -- this raises
    # PackageNotFoundError when invoked from inside _pkg_version.
    def real_lookup_for_missing_dist(_name):
        return real_version_lookup("no-such-dist-name-sac-test-only")

    # Act
    result = _pkg_version(lookup=real_lookup_for_missing_dist)
    # Assert
    assert result == "dev"


# ---------------------------------------------------------------------------
# Top-level group surface
# ---------------------------------------------------------------------------


def test_help_lists_completion_commands():
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(main, ["--help"])
    # Assert
    assert "install-shell-completion" in result.output


def test_no_subcommand_prints_help():
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(main, [])
    # Assert
    assert "SciTeX Agent Container" in result.output


def test_help_recursive_flag_runs():
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(main, ["--help-recursive"])
    # Assert
    assert result.exit_code == 0


def test_json_flag_falls_through_to_help():
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(main, ["--json"])
    # Assert
    assert result.exit_code == 0


def test_list_commands_includes_install_completion():
    # Arrange
    group = _MainGroup()
    ctx = click.Context(group)
    # Act
    names = group.list_commands(ctx)
    # Assert
    assert "install-shell-completion" in names


def test_list_commands_includes_print_completion():
    # Arrange
    group = _MainGroup()
    ctx = click.Context(group)
    # Act
    names = group.list_commands(ctx)
    # Assert
    assert "print-shell-completion" in names


# ---------------------------------------------------------------------------
# Completion cache install -- dry-run
# ---------------------------------------------------------------------------


def test_dry_run_announces_write(home_in_tmp):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(main, ["install-shell-completion", "--dry-run", "-y"])
    # Assert
    assert "Would write" in result.output


def test_dry_run_does_not_create_cache_dir(home_in_tmp):
    # Arrange
    cache_dir = home_in_tmp / ".scitex" / "agent-container" / "runtime" / "completion"
    runner = CliRunner()
    # Act
    runner.invoke(main, ["install-shell-completion", "--dry-run", "-y"])
    # Assert
    assert not cache_dir.exists()


def test_dry_run_does_not_create_rc_file(home_in_tmp):
    # Arrange
    rc_file = home_in_tmp / ".bashrc"
    runner = CliRunner()
    # Act
    runner.invoke(main, ["install-shell-completion", "--dry-run", "-y"])
    # Assert
    assert not rc_file.exists()


def test_dry_run_mentions_xdg_symlink(home_in_tmp):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(main, ["install-shell-completion", "--dry-run", "-y"])
    # Assert
    assert "Would symlink" in result.output


def test_dry_run_mentions_rc_append(home_in_tmp):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(main, ["install-shell-completion", "--dry-run", "-y"])
    # Assert
    assert "Would append to" in result.output


# ---------------------------------------------------------------------------
# Completion cache install -- real writes
# ---------------------------------------------------------------------------


def test_install_writes_bash_cache_file(home_in_tmp):
    # Arrange
    cache_path = (
        home_in_tmp / ".scitex" / "agent-container" / "runtime" / "completion" / "sac"
    )
    runner = CliRunner()
    # Act
    runner.invoke(main, ["install-shell-completion", "--shell", "bash", "-y"])
    # Assert
    assert cache_path.is_file()


def test_install_creates_xdg_symlink(home_in_tmp):
    # Arrange
    xdg_link = home_in_tmp / ".local" / "share" / "bash-completion" / "scitex" / "sac"
    runner = CliRunner()
    # Act
    runner.invoke(main, ["install-shell-completion", "--shell", "bash", "-y"])
    # Assert
    assert xdg_link.is_symlink()


def test_install_appends_source_line_to_bashrc(home_in_tmp):
    # Arrange
    rc_file = home_in_tmp / ".bashrc"
    runner = CliRunner()
    # Act
    runner.invoke(main, ["install-shell-completion", "--shell", "bash", "-y"])
    # Assert
    assert "sac-completion: sac" in rc_file.read_text()


def test_install_skips_duplicate_rc_append(home_in_tmp):
    # Arrange: pre-existing bashrc with the marker already present.
    rc_file = home_in_tmp / ".bashrc"
    rc_file.write_text(
        "# sac-completion: sac\n# sac-completion: scitex-agent-container\n"
    )
    runner = CliRunner()
    # Act
    runner.invoke(main, ["install-shell-completion", "--shell", "bash", "-y"])
    # Assert: marker count unchanged (no duplicate append).
    assert rc_file.read_text().count("sac-completion: sac\n") == 1


def test_install_zsh_writes_zshrc_marker(home_in_tmp):
    # Arrange
    zshrc = home_in_tmp / ".zshrc"
    runner = CliRunner()
    # Act
    runner.invoke(main, ["install-shell-completion", "--shell", "zsh", "-y"])
    # Assert
    assert "sac-completion" in zshrc.read_text()


def test_install_unsupported_shell_emits_error(home_in_tmp):
    # Arrange
    runner = CliRunner()
    # Act: ``fish`` is in click's choice list but unsupported by cache install.
    result = runner.invoke(main, ["install-shell-completion", "--shell", "fish", "-y"])
    # Assert
    assert "cache install supports bash/zsh" in result.output


def test_install_replaces_stale_xdg_symlink(home_in_tmp):
    # Arrange: a stale symlink pointing somewhere else.
    xdg_dir = home_in_tmp / ".local" / "share" / "bash-completion" / "scitex"
    xdg_dir.mkdir(parents=True)
    stale_target = home_in_tmp / "stale-target"
    stale_target.write_text("old")
    xdg_link = xdg_dir / "sac"
    xdg_link.symlink_to(stale_target)
    runner = CliRunner()
    # Act
    runner.invoke(main, ["install-shell-completion", "--shell", "bash", "-y"])
    # Assert: link now resolves into the sac cache dir.
    assert "runtime/completion/sac" in str(xdg_link.resolve())


# ---------------------------------------------------------------------------
# Lazy resolve + completion attach
# ---------------------------------------------------------------------------


def test_resolve_lazy_attach_is_idempotent():
    # Arrange
    group = _MainGroup()
    # Act
    first = group._resolve_lazy("install-shell-completion")
    second = group._resolve_lazy("install-shell-completion")
    # Assert
    assert first is second


def test_resolve_lazy_returns_unknown_as_none():
    # Arrange
    group = _MainGroup()
    # Act
    result = group._resolve_lazy("definitely-not-a-real-command")
    # Assert
    assert result is None


# ---------------------------------------------------------------------------
# cli_entry_point dispatch
# ---------------------------------------------------------------------------


def test_cli_entry_point_runs_main_without_on_flag(saved_argv):
    # Arrange
    sys.argv[:] = ["sac", "--help"]
    # Act
    run = cli_entry_point
    # Assert
    with pytest.raises(SystemExit):
        run()


def test_cli_entry_point_rejects_malformed_on_flag(saved_argv):
    # Arrange: bare ``--on`` with no value is a UsageError from split_on_flag.
    sys.argv[:] = ["sac", "--on"]
    # Act
    run = cli_entry_point
    # Assert
    with pytest.raises(SystemExit):
        run()


# ---------------------------------------------------------------------------
# WI-6 — ``sac channel`` tombstone regression guard (handoff §6)
#
# Per HANDOFF_AGENT_COMMS_2026-05-19.md §6 (WI-6 "Delete deprecated
# tombstones"): the ``sac channel send`` group was a ``[DEPRECATED]``
# back-compat surface duplicating ``sac peer post-turn`` (same outcome,
# different transport). No live in-repo caller. Deleted on this branch.
# These tests assert the tombstone stays gone — adding ``channel`` back
# would have to land with a new behaviour test, not as a back-compat
# re-introduction.
# ---------------------------------------------------------------------------


def test_channel_group_module_is_deleted() -> None:
    """The ``scitex_agent_container.cli_pkg.channel_group`` module is
    no longer importable. The tombstone is fully removed (handoff §6).
    """
    # Arrange
    import importlib

    target = "scitex_agent_container.cli_pkg.channel_group"
    # Act
    try:
        importlib.import_module(target)
    except ModuleNotFoundError:
        importable = False
    else:
        importable = True
    # Assert
    assert importable is False


def test_main_cli_does_not_advertise_channel_command() -> None:
    """``sac --help`` text must not mention ``channel`` — it would be a
    surfaced tombstone (handoff §0 Hard rules).
    """
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(main, ["--help"])
    # Assert
    assert "channel" not in result.output.lower(), result.output


def test_sac_channel_invocation_is_unknown_command() -> None:
    """``sac channel send`` must exit non-zero — the group is gone.
    Click reports 'No such command' for the unknown noun.
    """
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(main, ["channel", "send", "alpha", "hi"])
    # Assert
    assert result.exit_code != 0


def test_lazy_commands_does_not_register_channel() -> None:
    """The LazyGroup mapping must not list ``channel`` either."""
    # Arrange
    keys = set(_MainGroup.LAZY_COMMANDS.keys())
    # Act
    has_channel = "channel" in keys
    # Assert
    assert has_channel is False


def test_main_cli_module_imports_cleanly() -> None:
    """Guard: if someone removes ``channel_group`` but leaves a dangling
    reference in ``_main.py``, every CLI invocation breaks at import
    time. Catch it here rather than in every other test."""
    # Arrange
    import importlib

    target = "scitex_agent_container.cli_pkg._main"
    # Act
    try:
        mod = importlib.import_module(target)
    except Exception as exc:  # noqa: BLE001
        mod = None
        err: Exception | None = exc
    else:
        err = None
    # Assert
    assert mod is not None, f"_main import failed: {err!r}"
