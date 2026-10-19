"""Tests for the shared Agents app header (SDK contract, Writer pattern).

Every Agents page renders exactly ONE header: the shared
``stx-app-header`` partial (SDK chrome), never the legacy hand-rolled
``agents-head`` block. The header states the LEAF package version serving
the page. Driven through Django's test client against the REAL loopback
listener; each test has AAA markers and a single assertion (STX-TQ002/TQ007).
"""

from __future__ import annotations

from importlib.metadata import version
from pathlib import Path

from scitex_agent_container._django._constants import IDENTITY_ENV

_DJANGO_DIR = Path(__file__).resolve().parents[3] / "src" / "scitex_agent_container" / "_django"
_PROJECT_ROOT = _DJANGO_DIR.parents[2]
_PYPROJECT = _PROJECT_ROOT / "pyproject.toml"

_SHARED_HEADER_CLASS = "stx-app-header"
_LEGACY_HEADER_CLASS = "agents-head"


def _installed_version() -> str:
    return version("scitex-agent-container")


def _fleet_html(client, env_save_restore) -> str:
    # Arrange
    env_save_restore.set(IDENTITY_ENV, "alice")
    # Act
    return client.get("/").content.decode()


def _header_block(body: str) -> str:
    start = body.index("<header")
    end = body.index("</header>", start)
    return body[start:end]


def test_fleet_page_has_exactly_one_header(client, loopback, env_save_restore):
    # Arrange
    body = _fleet_html(client, env_save_restore)
    # Act
    count = body.count("<header")
    # Assert
    assert count == 1


def test_fleet_header_uses_shared_contract_class(client, loopback, env_save_restore):
    # Arrange
    header = _header_block(_fleet_html(client, env_save_restore))
    # Act
    shared = _SHARED_HEADER_CLASS in header
    # Assert
    assert shared


def test_fleet_header_shows_leaf_version(client, loopback, env_save_restore):
    # Arrange
    header = _header_block(_fleet_html(client, env_save_restore))
    # Act
    marked = f"v{_installed_version()}" in header
    # Assert
    assert marked


def test_fleet_page_has_no_legacy_header(client, loopback, env_save_restore):
    # Arrange
    body = _fleet_html(client, env_save_restore)
    # Act
    legacy = _LEGACY_HEADER_CLASS in body
    # Assert
    assert not legacy


def test_detail_page_has_exactly_one_header(client, loopback, env_save_restore):
    # Arrange
    env_save_restore.set(IDENTITY_ENV, "alice")
    # Act
    count = client.get("/alpha/").content.decode().count("<header")
    # Assert
    assert count == 1


def test_installed_version_matches_pyproject():
    # Arrange
    declared = next(
        line.split("=")[1].strip().strip('"')
        for line in _PYPROJECT.read_text(encoding="utf-8").splitlines()
        if line.startswith("version")
    )
    # Act
    installed = _installed_version()
    # Assert
    assert installed == declared
