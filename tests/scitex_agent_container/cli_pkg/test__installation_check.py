"""CLI tests for ``sac installation check``.

PA-306: no mocks. ``CliRunner`` drives the real click command against a
REAL venv layout built under ``tmp_path`` — real dist-info dirs, real
``.pth`` pointers, real package directories. The operator's
``/opt/venv-sac`` is never touched.

The exit-code contract is the load-bearing part and is asserted from both
ends: BROKEN must fail the command, and UNKNOWN alone must NOT — turning
"I could not look" into a red build is how a check gets ``|| true``'d into
uselessness. ``--strict`` is the opt-in for callers who need a complete
answer rather than merely no bad news.

Each test: AAA markers (TQ002), one assertion (TQ007), 3+-word name
(TQ003). No monkeypatch (NM002).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from scitex_agent_container.cli_pkg._installation_check import installation_check

_PY = "python3.12"


@pytest.fixture
def runner():
    """Real click runner. Yield-fixture: no monkeypatch (NM002)."""
    yield CliRunner()


@pytest.fixture
def clean_venv(tmp_path):
    """A real venv whose single distribution is coherent."""
    root = tmp_path / "clean"
    site = root / "lib" / _PY / "site-packages"
    site.mkdir(parents=True)
    _dist_info(site, "scitex_cards", "0.32.3")
    _package(site, "scitex_cards")
    yield root


@pytest.fixture
def broken_venv(tmp_path):
    """A real venv reproducing the 2026-08-09 shape: dead + shadowed."""
    root = tmp_path / "broken"
    site = root / "lib" / _PY / "site-packages"
    site.mkdir(parents=True)
    _dist_info(site, "scitex_agent_container", "0.24.25")
    _package(site, "scitex_agent_container")
    (site / "_editable_impl_scitex_agent_container.pth").write_text(
        str(tmp_path / "deleted-worktree" / "src")
    )
    yield root


def _dist_info(site: Path, name: str, version: str) -> Path:
    dist_info = site / (name + "-" + version + ".dist-info")
    dist_info.mkdir(parents=True)
    (dist_info / "METADATA").write_text("Name: " + name + "\n")
    (dist_info / "top_level.txt").write_text(name + "\n")
    (dist_info / "RECORD").write_text(name + "/__init__.py,,\n")
    return dist_info


def _package(site: Path, name: str) -> Path:
    pkg = site / name
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text("")
    return pkg


# ---------------------------------------------------------------------------
# Exit-code contract
# ---------------------------------------------------------------------------
def test_broken_venv_exits_non_zero(runner, broken_venv):
    # Arrange
    # Act
    result = runner.invoke(installation_check, [str(broken_venv)])
    # Assert
    assert result.exit_code == 1


def test_clean_venv_exits_zero(runner, clean_venv):
    # Arrange
    # Act
    result = runner.invoke(installation_check, [str(clean_venv)])
    # Assert
    assert result.exit_code == 0


def test_unknown_alone_exits_zero(runner, clean_venv):
    # Arrange — an absent distribution is UNKNOWN, and UNKNOWN is not a
    # failure: it is an absence of evidence.
    # Act
    result = runner.invoke(
        installation_check, [str(clean_venv), "--dist", "not-installed"]
    )
    # Assert
    assert result.exit_code == 0


def test_strict_makes_unknown_exit_two(runner, clean_venv):
    # Arrange
    # Act
    result = runner.invoke(
        installation_check, [str(clean_venv), "--dist", "not-installed", "--strict"]
    )
    # Assert
    assert result.exit_code == 2


def test_missing_venv_exits_zero_without_strict(runner, tmp_path):
    # Arrange — an unreadable site-packages is UNKNOWN, not BROKEN.
    # Act
    result = runner.invoke(installation_check, [str(tmp_path / "no-such-venv")])
    # Assert
    assert result.exit_code == 0


# ---------------------------------------------------------------------------
# Output — UNKNOWN must be VISIBLE even though it does not fail
# ---------------------------------------------------------------------------
def test_unknown_row_is_printed(runner, clean_venv):
    # Arrange
    # Act
    result = runner.invoke(
        installation_check, [str(clean_venv), "--dist", "not-installed"]
    )
    # Assert
    assert "UNKNOWN" in result.output


def test_missing_venv_says_why(runner, tmp_path):
    # Arrange
    # Act
    result = runner.invoke(installation_check, [str(tmp_path / "no-such-venv")])
    # Assert
    assert "UNKNOWN" in result.output


def test_broken_row_names_the_reason(runner, broken_venv):
    # Arrange
    # Act
    result = runner.invoke(installation_check, [str(broken_venv)])
    # Assert
    assert "shadowed-pointer" in result.output


def test_clean_venv_hides_ok_rows_by_default(runner, clean_venv):
    # Arrange
    # Act
    result = runner.invoke(installation_check, [str(clean_venv)])
    # Assert
    assert "scitex-cards" not in result.output


def test_all_flag_lists_ok_rows(runner, clean_venv):
    # Arrange
    # Act
    result = runner.invoke(installation_check, [str(clean_venv), "--all"])
    # Assert
    assert "scitex-cards" in result.output


def test_foreign_venv_declares_imports_unobservable(runner, clean_venv):
    # Arrange — never let a skipped leg read as a clean one.
    # Act
    result = runner.invoke(installation_check, [str(clean_venv)])
    # Assert
    assert "UNOBSERVABLE" in result.output


# ---------------------------------------------------------------------------
# --json contract
# ---------------------------------------------------------------------------
def test_json_output_parses(runner, broken_venv):
    # Arrange
    # Act
    result = runner.invoke(installation_check, [str(broken_venv), "--json"])
    # Assert
    assert json.loads(result.stdout)["counts"]["broken"] == 1


def test_json_carries_the_exit_code(runner, broken_venv):
    # Arrange
    # Act
    result = runner.invoke(installation_check, [str(broken_venv), "--json"])
    # Assert
    assert json.loads(result.stdout)["exit_code"] == 1


def test_json_reason_breakdown_names_shadowed(runner, broken_venv):
    # Arrange
    # Act
    result = runner.invoke(installation_check, [str(broken_venv), "--json"])
    # Assert
    assert "shadowed-pointer" in json.loads(result.stdout)["reason_breakdown"]


def test_json_carries_the_dead_pointer_path(runner, broken_venv):
    # Arrange — the pointer path IS the repair instruction.
    # Act
    result = runner.invoke(installation_check, [str(broken_venv), "--json"])
    payload = json.loads(result.stdout)
    row = next(d for d in payload["distributions"] if d["state"] == "broken")
    # Assert
    assert row["evidence"]["pointers"][0]["target_exists"] is False


# ---------------------------------------------------------------------------
# Wiring — the verb must actually be reachable as `sac installation check`
# ---------------------------------------------------------------------------
def test_check_is_registered_on_the_installation_group():
    # Arrange
    from scitex_agent_container.cli_pkg.installation_group import install_group

    # Act
    names = list(install_group.commands)
    # Assert
    assert "check" in names
