"""Tests for the install audit — duplicate/fossil ``.dist-info`` detection.

Real ``.dist-info`` directories written to ``tmp_path`` and handed to the
code under test directly, so the assertions do not depend on whatever
happens to be installed in the interpreter running the suite.

A fossil ``.dist-info`` advertising a version whose code is gone is a known
trap; ``importlib.metadata.distributions()`` dedupes by name and so HIDES
it, which is why the code globs the path entries itself.
"""

from __future__ import annotations

from pathlib import Path

from scitex_agent_container._provenance._audit import find_dist_infos


def _write_dist_info(site: Path, version: str) -> Path:
    """Create a real .dist-info directory, laid out as pip would leave it."""
    dist_info = site / f"scitex_agent_container-{version}.dist-info"
    dist_info.mkdir(parents=True)
    (dist_info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: scitex-agent-container\nVersion: {version}\n"
    )
    (site / "scitex_agent_container").mkdir(exist_ok=True)
    return dist_info


class TestFindDistInfos:
    def test_finds_a_single_installed_distribution(self, tmp_path: Path):
        # Arrange
        site = tmp_path / "site-packages"
        _write_dist_info(site, "0.21.13")

        # Act
        found = find_dist_infos([str(site)])

        # Assert
        assert len(found) == 1

    def test_finds_both_halves_of_a_duplicate_install(self, tmp_path: Path):
        # Arrange — the fossil case: two .dist-info dirs for one package,
        # one advertising a version whose code is long gone.
        first = tmp_path / "site-a"
        second = tmp_path / "site-b"
        _write_dist_info(first, "0.21.13")
        _write_dist_info(second, "0.9.4")

        # Act
        found = find_dist_infos([str(first), str(second)])

        # Assert
        assert len(found) == 2

    def test_ignores_unrelated_distributions(self, tmp_path: Path):
        # Arrange
        site = tmp_path / "site-packages"
        (site / "requests-2.31.0.dist-info").mkdir(parents=True)

        # Act
        found = find_dist_infos([str(site)])

        # Assert
        assert found == []

    def test_the_same_directory_twice_counts_once(self, tmp_path: Path):
        # Arrange — a duplicated path entry is not a duplicate INSTALL, and
        # must not be reported as one.
        site = tmp_path / "site-packages"
        _write_dist_info(site, "0.21.13")

        # Act
        found = find_dist_infos([str(site), str(site)])

        # Assert
        assert len(found) == 1

    def test_a_legacy_egg_info_is_still_found(self, tmp_path: Path):
        # Arrange — setuptools-era installs leave .egg-info, and one of
        # those alongside a .dist-info is a classic fossil pair.
        site = tmp_path / "site-packages"
        (site / "scitex_agent_container.egg-info").mkdir(parents=True)

        # Act
        found = find_dist_infos([str(site)])

        # Assert
        assert len(found) == 1

    def test_an_empty_path_finds_nothing(self, tmp_path: Path):
        # Arrange
        empty = tmp_path / "empty"
        empty.mkdir()

        # Act
        found = find_dist_infos([str(empty)])

        # Assert
        assert found == []

# EOF
