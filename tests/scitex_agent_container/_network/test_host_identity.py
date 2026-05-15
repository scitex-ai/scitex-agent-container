"""Tests for host_identity (local-vs-remote resolver).

Uses the pure ``compute_identities()`` surface for hostname/alias
logic — no module-attribute swapping, no socket / os.uname patching.
For the end-to-end ``get_local_identities()`` wrapper, file aliases are
controlled via the real env override ``SAC_HOST_IDENTITY_PATH`` and a
real YAML written to ``tmp_path``.

TQ cleanup: every test carries AAA markers (TQ002) and exactly one
assertion (TQ007). Same-shape invariants over small input sets collapse
into ``pytest.parametrize``. Test names spell out the behaviour being
verified (TQ003-compatible).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from scitex_agent_container._network import host_identity as hi

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_identity_file(tmp_path: Path):
    """Real YAML at a tmp_path, surfaced via the env override.

    Pure env save/restore — no production-internals patching.
    """
    path = tmp_path / "host-identity.yaml"
    saved = os.environ.get("SAC_HOST_IDENTITY_PATH")
    os.environ["SAC_HOST_IDENTITY_PATH"] = str(path)
    hi._reset_cache_for_tests()
    try:
        yield path
    finally:
        if saved is None:
            os.environ.pop("SAC_HOST_IDENTITY_PATH", None)
        else:
            os.environ["SAC_HOST_IDENTITY_PATH"] = saved
        hi._reset_cache_for_tests()


# ---------------------------------------------------------------------------
# compute_identities() — pure logic, no I/O, no patching
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("loopback_name", ["localhost", "127.0.0.1", "::1"])
def test_compute_identities_includes_loopback_name_unconditionally(loopback_name):
    # Arrange
    hostname = "anyhost"
    nodename = "anyhost"
    # Act
    ids = hi.compute_identities(hostname=hostname, nodename=nodename)
    # Assert
    assert loopback_name in ids


def test_compute_identities_lowercases_full_hostname_string():
    # Arrange
    hostname = "MyHost.Example"
    nodename = "MyHost"
    # Act
    ids = hi.compute_identities(hostname=hostname, nodename=nodename)
    # Assert
    assert "myhost.example" in ids


def test_compute_identities_adds_short_form_of_mixed_case_hostname():
    # Arrange
    hostname = "MyHost.Example"
    nodename = "MyHost"
    # Act
    ids = hi.compute_identities(hostname=hostname, nodename=nodename)
    # Assert
    assert "myhost" in ids


def test_compute_identities_preserves_lowercased_fqdn_string():
    # Arrange
    hostname = "hostA.example.com"
    nodename = "hostA"
    # Act
    ids = hi.compute_identities(hostname=hostname, nodename=nodename)
    # Assert
    assert "hosta.example.com" in ids


def test_compute_identities_extracts_short_form_from_fqdn():
    # Arrange
    hostname = "hostA.example.com"
    nodename = "hostA"
    # Act
    ids = hi.compute_identities(hostname=hostname, nodename=nodename)
    # Assert
    assert "hosta" in ids


@pytest.mark.parametrize("expected_name", ["real", "nas", "ugreen"])
def test_compute_identities_unions_each_file_alias_into_result(expected_name):
    # Arrange
    file_aliases = {"nas", "ugreen"}
    # Act
    ids = hi.compute_identities(
        hostname="real", nodename="real", file_aliases=file_aliases
    )
    # Assert
    assert expected_name in ids


@pytest.mark.parametrize("expected_name", ["real", "mba", "spartan"])
def test_compute_identities_unions_each_resource_alias_into_result(expected_name):
    # Arrange
    resource_aliases = {"mba", "spartan"}
    # Act
    ids = hi.compute_identities(
        hostname="real", nodename="real", resource_aliases=resource_aliases
    )
    # Assert
    assert expected_name in ids


def test_compute_identities_returns_only_auto_names_when_aliases_absent():
    # Arrange
    hostname = "solo"
    nodename = "solo"
    # Act
    ids = hi.compute_identities(hostname=hostname, nodename=nodename)
    # Assert
    assert "solo" in ids


@pytest.mark.parametrize("foreign_name", ["nas", "mba"])
def test_compute_identities_does_not_leak_alias_when_sources_absent(foreign_name):
    # Arrange
    hostname = "solo"
    nodename = "solo"
    # Act
    ids = hi.compute_identities(hostname=hostname, nodename=nodename)
    # Assert
    assert foreign_name not in ids


# ---------------------------------------------------------------------------
# is_local_host() — public-API behavior
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("blank_value", [None, "", "   "])
def test_is_local_host_treats_blank_input_as_local(
    isolated_identity_file: Path, blank_value
):
    # Arrange — isolated env, no YAML file written.
    # Act
    result = hi.is_local_host(blank_value)
    # Assert
    assert result is True


@pytest.mark.parametrize("alias", ["nas", "ugreen"])
def test_is_local_host_matches_alias_listed_in_yaml_file(
    isolated_identity_file: Path, alias
):
    # Arrange
    isolated_identity_file.write_text(yaml.safe_dump({"aliases": ["nas", "ugreen"]}))
    hi._reset_cache_for_tests()
    # Act
    result = hi.is_local_host(alias)
    # Assert
    assert result is True


@pytest.mark.parametrize("queried_name", ["NAS", "Nas"])
def test_is_local_host_compares_yaml_alias_case_insensitively(
    isolated_identity_file: Path, queried_name
):
    # Arrange
    isolated_identity_file.write_text(yaml.safe_dump({"aliases": ["nas"]}))
    hi._reset_cache_for_tests()
    # Act
    result = hi.is_local_host(queried_name)
    # Assert
    assert result is True


def test_is_local_host_rejects_name_absent_from_yaml_aliases(
    isolated_identity_file: Path,
):
    # Arrange
    isolated_identity_file.write_text(yaml.safe_dump({"aliases": ["nas"]}))
    hi._reset_cache_for_tests()
    # Act
    result = hi.is_local_host("totally-not-this-host-12345")
    # Assert
    assert result is False


# ---------------------------------------------------------------------------
# YAML loader — file errors via real malformed files
# ---------------------------------------------------------------------------


def test_yaml_loader_raises_runtime_error_on_malformed_yaml(
    isolated_identity_file: Path,
):
    # Arrange
    isolated_identity_file.write_text("aliases: [unterminated")
    hi._reset_cache_for_tests()
    loader = hi.get_local_identities
    # Act
    raises_ctx = pytest.raises(RuntimeError, match="Invalid YAML")
    # Assert
    with raises_ctx:
        loader()


def test_yaml_loader_raises_runtime_error_when_root_is_not_mapping(
    isolated_identity_file: Path,
):
    # Arrange
    isolated_identity_file.write_text("- just\n- a\n- list\n")
    hi._reset_cache_for_tests()
    loader = hi.get_local_identities
    # Act
    raises_ctx = pytest.raises(RuntimeError, match="must be a YAML mapping")
    # Assert
    with raises_ctx:
        loader()


def test_yaml_loader_raises_runtime_error_when_aliases_is_not_list(
    isolated_identity_file: Path,
):
    # Arrange
    isolated_identity_file.write_text(yaml.safe_dump({"aliases": "not-a-list"}))
    hi._reset_cache_for_tests()
    loader = hi.get_local_identities
    # Act
    raises_ctx = pytest.raises(RuntimeError, match="must be a list")
    # Assert
    with raises_ctx:
        loader()


def test_load_file_aliases_returns_empty_set_when_file_is_absent(tmp_path: Path):
    # Arrange — explicit missing path, no env juggling needed.
    missing_path = tmp_path / "does-not-exist.yaml"
    # Act
    result = hi._load_file_aliases(missing_path)
    # Assert
    assert result == set()


# ---------------------------------------------------------------------------
# Cache behavior
# ---------------------------------------------------------------------------


def test_cache_serves_first_alias_after_initial_reset(
    isolated_identity_file: Path,
):
    # Arrange
    isolated_identity_file.write_text(yaml.safe_dump({"aliases": ["first-name"]}))
    hi._reset_cache_for_tests()
    # Act
    result = hi.is_local_host("first-name")
    # Assert
    assert result is True


def test_cache_hides_new_alias_until_reset_is_called(
    isolated_identity_file: Path,
):
    # Arrange — prime the cache with the first alias set, then rewrite YAML.
    isolated_identity_file.write_text(yaml.safe_dump({"aliases": ["first-name"]}))
    hi._reset_cache_for_tests()
    hi.is_local_host("first-name")
    isolated_identity_file.write_text(yaml.safe_dump({"aliases": ["second-name"]}))
    # Act — no cache reset; stale identities should still be served.
    result = hi.is_local_host("second-name")
    # Assert
    assert result is False


def test_cache_reset_for_tests_picks_up_rewritten_yaml_aliases(
    isolated_identity_file: Path,
):
    # Arrange — prime cache, rewrite YAML, then reset the cache.
    isolated_identity_file.write_text(yaml.safe_dump({"aliases": ["first-name"]}))
    hi._reset_cache_for_tests()
    hi.is_local_host("first-name")
    isolated_identity_file.write_text(yaml.safe_dump({"aliases": ["second-name"]}))
    hi._reset_cache_for_tests()
    # Act
    result = hi.is_local_host("second-name")
    # Assert
    assert result is True
