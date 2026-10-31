"""Tests for the shared Agents app header (SDK contract, Writer pattern).

Every Agents page mounts exactly ONE shared AppHeader primitive
(``data-stx-app-header``, client-rendered by the SDK): the full product
title, the leaf version, the project picker. No leaf-local <header>
app row. The header states the LEAF package version serving the page.
Driven through Django's test client against the REAL loopback
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


_MOUNT_DIV = '<div data-stx-app-header'


def _header_block(body: str) -> str:
    start = body.index(_MOUNT_DIV)
    end = body.index("header-picker.js", start)
    return body[start:end]


def test_fleet_page_mounts_exactly_one_shared_header(client, loopback, env_save_restore):
    # Arrange — one AppHeader mount; toolbars are functional bars, not rows.
    body = _fleet_html(client, env_save_restore)
    # Act
    count = body.count(_MOUNT_DIV)
    # Assert
    assert count == 1


def test_fleet_header_mounts_the_shared_primitive(client, loopback, env_save_restore):
    # Arrange — the leaf emits the mount point, not a hand-rolled row.
    # (The mount attribute itself trivially contains "stx-app-header".)
    header = _header_block(_fleet_html(client, env_save_restore))
    # Act
    mounted = _MOUNT_DIV in header
    # Assert
    assert mounted


def test_fleet_header_mounts_no_leaf_elements(client, loopback, env_save_restore):
    # Arrange — same shared mount (helper); the leaf must not hand-roll
    # header element classes of its own.
    header = _header_block(_fleet_html(client, env_save_restore))
    # Act
    leaf_elements = [m for m in ("stx-app-header__title", "stx-app-header__version", "stx-app-header__actions", "stx-app-header__slot") if m in header]
    # Assert
    assert leaf_elements == []


def test_fleet_header_shows_leaf_version(client, loopback, env_save_restore):
    # Arrange — the badge renders client-side; the server contract is the
    # data attribute carrying the installed leaf version.
    header = _header_block(_fleet_html(client, env_save_restore))
    # Act
    marked = f'data-app-version="{_installed_version()}"' in header
    # Assert
    assert marked


def test_fleet_page_has_no_legacy_header(client, loopback, env_save_restore):
    # Arrange
    body = _fleet_html(client, env_save_restore)
    # Act
    legacy = _LEGACY_HEADER_CLASS in body
    # Assert
    assert not legacy


def test_detail_page_mounts_exactly_one_shared_header(client, loopback, env_save_restore):
    # Arrange
    env_save_restore.set(IDENTITY_ENV, "alice")
    # Act
    count = client.get("/alpha/").content.decode().count(_MOUNT_DIV)
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
