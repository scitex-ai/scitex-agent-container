"""Tests for the §5 renamed-command redirect helper.

Convention §5 (scitex/general/03_interface_02_cli/11_deprecation.md):
- Renamed CLI commands MUST exit non-zero (code 2) with a redirect.
- Soft warnings are forbidden — they let stale scripts persist.
- The wrapped command must NOT execute the original callback.

These tests pin that contract for ``cli_pkg._helpers.renamed_redirect``
and verify representative top-level aliases registered in ``_main``.
"""

from __future__ import annotations

import click
from click.testing import CliRunner

from scitex_agent_container.cli_pkg._helpers import renamed_redirect


def test_renamed_redirect_exits_2_with_message():
    """The wrapped callback exits 2 and prints a redirect to stderr."""
    side_effects: list[str] = []

    @click.command(name="old-name")
    def original():
        side_effects.append("ran-original")

    aliased = renamed_redirect(original, new_path="sac new noun verb")

    runner = CliRunner()
    result = runner.invoke(aliased, [], standalone_mode=True)

    assert result.exit_code == 2, (
        f"renamed_redirect must exit 2 per §5, got {result.exit_code}. "
        f"stderr={result.stderr!r}"
    )
    assert side_effects == [], (
        "renamed_redirect must NOT execute the original callback — "
        f"got side_effects={side_effects!r}"
    )
    assert "renamed to 'sac new noun verb'" in result.stderr
    assert "Re-run with: sac new noun verb" in result.stderr


def test_renamed_redirect_help_documents_new_path():
    """``--help`` on the redirect surfaces the [RENAMED] notice."""

    @click.command(name="old-name", help="Old help text.")
    def original():
        pass

    aliased = renamed_redirect(original, new_path="sac new noun verb")
    assert aliased.help is not None
    assert "[RENAMED]" in aliased.help
    assert "sac new noun verb" in aliased.help


def test_top_level_clean_registry_redirects():
    """Sanity: a real registered alias hard-errors with the right path."""
    from scitex_agent_container.cli_pkg._main import main as sac_main

    runner = CliRunner()
    result = runner.invoke(sac_main, ["clean-registry"], standalone_mode=True)
    assert result.exit_code == 2, (
        f"sac clean-registry must redirect (exit 2), "
        f"got exit={result.exit_code} stderr={result.stderr!r}"
    )
    assert "sac registry clean" in result.stderr


def test_top_level_probe_network_redirects():
    """Sanity: another real registered alias hard-errors."""
    from scitex_agent_container.cli_pkg._main import main as sac_main

    runner = CliRunner()
    result = runner.invoke(sac_main, ["probe-network"], standalone_mode=True)
    assert result.exit_code == 2
    assert "sac network probe" in result.stderr


def test_top_level_start_alias_redirects():
    """Lifecycle alias: ``sac start <agent>`` must redirect to ``sac agent start``."""
    from scitex_agent_container.cli_pkg._main import main as sac_main

    runner = CliRunner()
    # No agent name needed — redirect fires before arg validation.
    result = runner.invoke(sac_main, ["start", "any-name"], standalone_mode=True)
    assert result.exit_code == 2
    assert "sac agent start" in result.stderr
