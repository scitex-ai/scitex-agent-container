"""Tests for the §5 renamed-command redirect helper.

Convention §5 (scitex/general/03_interface_02_cli/11_deprecation.md):
- Renamed CLI commands MUST exit non-zero (code 2) with a redirect.
- Soft warnings are forbidden — they let stale scripts persist.
- The wrapped command must NOT execute the original callback.

These tests pin that contract for ``cli_pkg._helpers.renamed_redirect``
and verify representative top-level aliases registered in ``_main``.

TQ cleanup: each test isolates one observable consequence of the §5
contract; shared setup is lifted into fixtures and parametrized cases
cover the top-level alias matrix.
"""

from __future__ import annotations

import click
import pytest
from click.testing import CliRunner

from scitex_agent_container.cli_pkg._helpers import renamed_redirect
from scitex_agent_container.cli_pkg._main import main as sac_main

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def redirect_invocation(runner):
    """Invoke a freshly-built renamed_redirect of an ``old-name`` command.

    Returns ``(result, side_effects)`` where ``side_effects`` is the list
    the original callback would append to if it (incorrectly) executed.
    """
    side_effects: list[str] = []

    @click.command(name="old-name")
    def original():
        side_effects.append("ran-original")

    aliased = renamed_redirect(original, new_path="sac new noun verb")
    # Act
    result = runner.invoke(aliased, [], standalone_mode=True)
    return result, side_effects


@pytest.fixture
def help_aliased():
    """Build a renamed_redirect alias and return its click.Command."""

    @click.command(name="old-name", help="Old help text.")
    def original():
        pass

    # Act
    return renamed_redirect(original, new_path="sac new noun verb")


@pytest.fixture
def old_path_override_result(runner):
    """Invoke a renamed_redirect that overrides the rendered ``old_path``."""

    @click.command(name="clean")
    def _clean():
        pass

    aliased = renamed_redirect(
        _clean, new_path="sac db clean", old_path="sac registry clean"
    )
    # Act
    return runner.invoke(aliased, [], standalone_mode=True)


# ---------------------------------------------------------------------------
# renamed_redirect — core contract (one assert per behaviour)
# ---------------------------------------------------------------------------


def test_renamed_redirect_exits_with_code_2(redirect_invocation):
    # Arrange
    result, _side_effects = redirect_invocation
    # Act
    exit_code = result.exit_code
    # Assert
    assert exit_code == 2, (
        f"renamed_redirect must exit 2 per §5, got {exit_code}. "
        f"stderr={result.stderr!r}"
    )


def test_renamed_redirect_does_not_execute_original_callback(redirect_invocation):
    # Arrange
    _result, side_effects = redirect_invocation
    # Act
    observed = list(side_effects)
    # Assert
    assert observed == [], (
        "renamed_redirect must NOT execute the original callback — "
        f"got side_effects={observed!r}"
    )


def test_renamed_redirect_stderr_names_new_path(redirect_invocation):
    # Arrange
    result, _ = redirect_invocation
    # Act
    stderr = result.stderr
    # Assert
    assert "renamed to 'sac new noun verb'" in stderr


def test_renamed_redirect_stderr_includes_rerun_instruction(redirect_invocation):
    # Arrange
    result, _ = redirect_invocation
    # Act
    stderr = result.stderr
    # Assert
    assert "Re-run with: sac new noun verb" in stderr


# ---------------------------------------------------------------------------
# renamed_redirect — --help surface
# ---------------------------------------------------------------------------


def test_renamed_redirect_help_includes_renamed_tag(help_aliased):
    # Arrange
    aliased = help_aliased
    # Act
    help_text = aliased.help
    # Assert
    assert help_text is not None and "[RENAMED]" in help_text


def test_renamed_redirect_help_mentions_new_path(help_aliased):
    # Arrange
    aliased = help_aliased
    # Act
    help_text = aliased.help or ""
    # Assert
    assert "sac new noun verb" in help_text


# ---------------------------------------------------------------------------
# Top-level registered aliases — parametrized over (argv, new_path_substring)
#
# Each row pins ONE alias that's wired into ``sac_main``. The exit-code
# contract (§5: exit 2) and the stderr-redirect contract are checked by
# two separate parametrized tests so a single CI failure names the
# specific row + behaviour.
# ---------------------------------------------------------------------------


TOP_LEVEL_REDIRECTS = [
    # (argv, new_path_substring_in_stderr, test_id)
    pytest.param(["clean-registry"], "sac db clean", id="clean-registry"),
    pytest.param(["probe-network"], "sac host probe-hub", id="probe-network"),
    pytest.param(["start", "any-name"], "sac agent start", id="start-alias"),
    pytest.param(["registry", "clean"], "sac db clean", id="registry-clean-subcommand"),
]


@pytest.mark.parametrize("argv, new_path_substring", TOP_LEVEL_REDIRECTS)
def test_top_level_alias_exits_with_code_2(runner, argv, new_path_substring):
    # Arrange
    cli = sac_main
    # Act
    result = runner.invoke(cli, argv, standalone_mode=True)
    # Assert
    assert result.exit_code == 2, (
        f"sac {' '.join(argv)} must redirect (exit 2), "
        f"got exit={result.exit_code} stderr={result.stderr!r}"
    )


@pytest.mark.parametrize("argv, new_path_substring", TOP_LEVEL_REDIRECTS)
def test_top_level_alias_stderr_names_new_path(runner, argv, new_path_substring):
    # Arrange
    cli = sac_main
    # Act
    result = runner.invoke(cli, argv, standalone_mode=True)
    # Assert
    assert new_path_substring in result.stderr


def test_registry_clean_stderr_quotes_old_path(runner):
    """F-CS11 phase 5: the redirect message echoes the user's full command."""
    # Arrange
    cli = sac_main
    # Act
    result = runner.invoke(cli, ["registry", "clean"], standalone_mode=True)
    # Assert
    assert "'sac registry clean'" in result.stderr


# ---------------------------------------------------------------------------
# renamed_redirect — explicit old_path override
# ---------------------------------------------------------------------------


def test_renamed_redirect_old_path_override_exits_with_code_2(
    old_path_override_result,
):
    # Arrange
    result = old_path_override_result
    # Act
    exit_code = result.exit_code
    # Assert
    assert exit_code == 2


def test_renamed_redirect_old_path_override_quotes_supplied_path(
    old_path_override_result,
):
    # Arrange
    result = old_path_override_result
    # Act
    stderr = result.stderr
    # Assert
    assert "'sac registry clean'" in stderr
