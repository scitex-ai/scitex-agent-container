"""CLI vocabulary remains available when rebuilding from an installed package."""

from pathlib import Path

import pytest
import tomllib

import scitex_agent_container
from scitex_agent_container.cli_pkg._image_source_inputs import (
    locate_cli_dictionary,
    stage_cli_dictionary,
)

_REPO = Path(__file__).resolve().parents[3]
_PACKAGE = Path(scitex_agent_container.__file__).resolve().parent
_DICTIONARY = _REPO / ".scitex/dev/cli-audit-dict.yaml"


@pytest.fixture
def project_data():
    return tomllib.loads((_REPO / "pyproject.toml").read_text())


def test_checkout_resolves_canonical_vocabulary(project_data):
    # Act
    source = locate_cli_dictionary(_PACKAGE, _REPO / "pyproject.toml", project_data)
    # Assert
    assert source == _DICTIONARY


def test_installed_vocabulary_reconstructs_canonical_build_input(
    tmp_path, project_data
):
    # Arrange
    package = tmp_path / "installed/scitex_agent_container"
    package.mkdir(parents=True)
    payload = package / "_cli_audit_dict.yaml"
    payload.write_bytes(_DICTIONARY.read_bytes())
    staged = tmp_path / "staged"
    # Act
    source = locate_cli_dictionary(
        package, package / "_bundled/pyproject.toml", project_data
    )
    stage_cli_dictionary(source, staged)
    # Assert
    assert (
        staged / ".scitex/dev/cli-audit-dict.yaml"
    ).read_bytes() == _DICTIONARY.read_bytes()


def test_missing_declared_dictionary_is_refused(tmp_path, project_data):
    # Assert
    with pytest.raises(FileNotFoundError, match="dictionary is missing"):
        # Act
        locate_cli_dictionary(
            tmp_path / "package", tmp_path / "pyproject.toml", project_data
        )


def test_dictionary_symlink_cannot_escape_source_root(tmp_path, project_data):
    # Arrange
    project = tmp_path / "project"
    dictionary = project / ".scitex/dev/cli-audit-dict.yaml"
    dictionary.parent.mkdir(parents=True)
    dictionary.symlink_to(_DICTIONARY)
    # Assert
    with pytest.raises(ValueError, match="escapes its source root"):
        # Act
        locate_cli_dictionary(
            project / "src/scitex_agent_container",
            project / "pyproject.toml",
            project_data,
        )


def test_legacy_metadata_without_dictionary_needs_no_input(tmp_path, project_data):
    # Arrange
    del project_data["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"][
        ".scitex/dev/cli-audit-dict.yaml"
    ]
    # Act
    source = locate_cli_dictionary(
        tmp_path / "package", tmp_path / "pyproject.toml", project_data
    )
    # Assert
    assert source is None
