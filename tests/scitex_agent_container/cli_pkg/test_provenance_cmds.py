"""Tests for ``sac --version`` and ``sac provenance``.

Drives the real click commands through ``CliRunner`` against the real
loaded package — no mocks. What is being pinned down is that ``--version``
reports the identity of the code that is actually imported, and keeps the
shape scripts already parse.
"""

from __future__ import annotations

from click.testing import CliRunner

from scitex_agent_container._provenance import identity, package_dir
from scitex_agent_container.cli_pkg._main import main
from scitex_agent_container.cli_pkg.provenance_cmds import provenance


class TestVersionFlag:
    def test_prints_the_declared_version(self):
        # Arrange
        runner = CliRunner()

        # Act
        result = runner.invoke(main, ["--version"])

        # Assert
        assert identity()["version"] in result.output

    def test_prints_where_the_module_was_loaded_from(self):
        # Arrange — the site-packages-vs-worktree distinction that a bare
        # version string could never show.
        runner = CliRunner()

        # Act
        result = runner.invoke(main, ["--version"])

        # Assert
        assert str(package_dir()) in result.output

    def test_prints_an_identity_that_moves_with_the_code(self):
        # Arrange
        runner = CliRunner()

        # Act
        result = runner.invoke(main, ["--version"])

        # Assert
        assert identity()["commit"][:8] in result.output

    def test_the_version_flag_exits_zero(self):
        # Arrange
        runner = CliRunner()

        # Act
        result = runner.invoke(main, ["--version"])

        # Assert
        assert result.exit_code == 0

    def test_short_flag_matches_the_long_flag(self):
        # Arrange
        runner = CliRunner()

        # Act
        short = runner.invoke(main, ["-V"]).output

        # Assert
        assert short == runner.invoke(main, ["--version"]).output


class TestProvenanceCommand:
    def test_reports_the_loaded_origin(self):
        # Arrange
        runner = CliRunner()

        # Act
        result = runner.invoke(provenance, [])

        # Assert
        assert str(package_dir()) in result.output

    def test_json_output_carries_the_anomaly_list(self):
        # Arrange
        runner = CliRunner()

        # Act
        result = runner.invoke(provenance, ["--json"])

        # Assert
        assert '"anomalies"' in result.output

    def test_json_output_carries_the_on_disk_code_hash(self):
        # Arrange — the digest of the bytes actually on disk: the one
        # signal that still tells the truth when someone hand-patches
        # site-packages.
        runner = CliRunner()

        # Act
        result = runner.invoke(provenance, ["--json"])

        # Assert
        assert '"live_code_hash"' in result.output

# EOF
