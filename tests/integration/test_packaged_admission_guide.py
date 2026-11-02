"""Exercise real shipped guide resources, including stale-artifact controls."""
import importlib.util
import io
from pathlib import Path
from zipfile import ZipFile

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "admission_guide_check", _ROOT / ".github/ci/check_admission_guide.py"
)
_CHECK = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_CHECK)


def _wheel(resources):
    buffer = io.BytesIO()
    with ZipFile(buffer, "w") as archive:
        for name, body in resources.items():
            archive.writestr(name, body)
    return buffer.getvalue()


def _refusal(wheel, source_root):
    try:
        _CHECK.verify_admission_guide(wheel, source_root)
    except ValueError as error:
        return str(error)
    return ""


@pytest.fixture()
def resources():
    return {
        name: (_ROOT / "src" / name).read_bytes()
        for name in (_CHECK._SKILL, _CHECK._HTML, _CHECK._SOURCE)
    }


def test_shipped_guide_reference_and_source_fingerprint_are_current(resources):
    # Arrange
    wheel = _wheel(resources)
    # Act
    proof = _CHECK.verify_admission_guide(wheel, _ROOT)
    # Assert
    assert proof["anchor"] == "fleet-dashboard-admission-receipts"


def test_canonical_edit_without_rebuild_refuses_stale_packaged_source(resources, tmp_path):
    # Arrange
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "talking-to-agents.md").write_bytes(resources[_CHECK._SOURCE] + b"\nNew guidance.\n")
    wheel = _wheel(resources)
    # Act
    refused = _refusal(wheel, tmp_path)
    # Assert
    assert "source differs" in refused


def test_updating_only_packaged_source_cannot_disguise_stale_html(resources, tmp_path):
    # Arrange
    docs = tmp_path / "docs"
    docs.mkdir()
    updated = resources[_CHECK._SOURCE] + b"\nNew guidance.\n"
    (docs / "talking-to-agents.md").write_bytes(updated)
    resources[_CHECK._SOURCE] = updated
    wheel = _wheel(resources)
    # Act
    refused = _refusal(wheel, tmp_path)
    # Assert
    assert "HTML source fingerprint" in refused



def test_reference_outside_package_refuses(resources):
    # Arrange
    resources[_CHECK._SKILL] = resources[_CHECK._SKILL].replace(
        b"../../_sphinx_html/", b"../../../../docs/"
    )
    wheel = _wheel(resources)
    # Act
    refused = _refusal(wheel, _ROOT)
    # Assert
    assert "escapes" in refused


def test_missing_packaged_guide_refuses(resources):
    # Arrange
    resources.pop(_CHECK._HTML)
    wheel = _wheel(resources)
    # Act
    refused = _refusal(wheel, _ROOT)
    # Assert
    assert "resource missing" in refused


def test_missing_rendered_section_refuses(resources):
    # Arrange
    resources[_CHECK._SKILL] = resources[_CHECK._SKILL].replace(
        b"#fleet-dashboard-admission-receipts", b"#absent-section"
    )
    wheel = _wheel(resources)
    # Act
    refused = _refusal(wheel, _ROOT)
    # Assert
    assert "no rendered section" in refused


def test_declared_docs_floors_cover_the_include_read_event():
    # Arrange
    import tomllib

    project = tomllib.loads((_ROOT / "pyproject.toml").read_text())
    requirements = (_ROOT / "docs/sphinx/requirements.txt").read_text().splitlines()
    # Act
    declarations = (
        "sphinx>=7.2.5" in project["project"]["optional-dependencies"]["docs"],
        "sphinx>=7.2.5" in requirements,
        "myst-parser>=3.0" in project["project"]["optional-dependencies"]["docs"],
        "myst-parser>=3.0" in requirements,
    )
    # Assert
    assert declarations == (True, True, True, True)
