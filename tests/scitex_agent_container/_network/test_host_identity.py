"""Tests for host_identity (local-vs-remote resolver).

Uses the pure ``compute_identities()`` surface for hostname/alias
logic — no module-attribute swapping, no socket / os.uname patching.
For the end-to-end ``get_local_identities()`` wrapper, file aliases are
controlled via the real env override ``SAC_HOST_IDENTITY_PATH`` and a
real YAML written to ``tmp_path``.
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


def test_compute_identities_includes_loopback_names_unconditionally():
    ids = hi.compute_identities(hostname="anyhost", nodename="anyhost")
    assert "localhost" in ids
    assert "127.0.0.1" in ids
    assert "::1" in ids


def test_compute_identities_lowercases_hostname():
    ids = hi.compute_identities(hostname="MyHost.Example", nodename="MyHost")
    assert "myhost.example" in ids
    assert "myhost" in ids  # short form added


def test_compute_identities_includes_short_form_of_fqdn():
    ids = hi.compute_identities(hostname="hostA.example.com", nodename="hostA")
    assert "hosta.example.com" in ids
    assert "hosta" in ids


def test_compute_identities_unions_file_aliases():
    ids = hi.compute_identities(
        hostname="real", nodename="real", file_aliases={"nas", "ugreen"}
    )
    assert {"real", "nas", "ugreen"}.issubset(ids)


def test_compute_identities_unions_resource_aliases():
    ids = hi.compute_identities(
        hostname="real", nodename="real", resource_aliases={"mba", "spartan"}
    )
    assert {"real", "mba", "spartan"}.issubset(ids)


def test_compute_identities_empty_aliases_returns_only_auto():
    ids = hi.compute_identities(hostname="solo", nodename="solo")
    assert "solo" in ids
    # Nothing leaked from absent file/resource sources.
    assert "nas" not in ids
    assert "mba" not in ids


# ---------------------------------------------------------------------------
# is_local_host() — public-API behavior
# ---------------------------------------------------------------------------


def test_is_local_host_accepts_none_and_empty(isolated_identity_file: Path):
    assert hi.is_local_host(None) is True
    assert hi.is_local_host("") is True
    assert hi.is_local_host("   ") is True


def test_is_local_host_matches_yaml_alias(isolated_identity_file: Path):
    isolated_identity_file.write_text(yaml.safe_dump({"aliases": ["nas", "ugreen"]}))
    hi._reset_cache_for_tests()
    assert hi.is_local_host("nas") is True
    assert hi.is_local_host("ugreen") is True


def test_is_local_host_case_insensitive_against_yaml(isolated_identity_file: Path):
    isolated_identity_file.write_text(yaml.safe_dump({"aliases": ["nas"]}))
    hi._reset_cache_for_tests()
    assert hi.is_local_host("NAS") is True
    assert hi.is_local_host("Nas") is True


def test_is_local_host_rejects_unknown_name(isolated_identity_file: Path):
    isolated_identity_file.write_text(yaml.safe_dump({"aliases": ["nas"]}))
    hi._reset_cache_for_tests()
    assert hi.is_local_host("totally-not-this-host-12345") is False


# ---------------------------------------------------------------------------
# YAML loader — file errors via real malformed files
# ---------------------------------------------------------------------------


def test_yaml_loader_raises_on_malformed_yaml(isolated_identity_file: Path):
    isolated_identity_file.write_text("aliases: [unterminated")
    hi._reset_cache_for_tests()
    with pytest.raises(RuntimeError, match="Invalid YAML"):
        hi.get_local_identities()


def test_yaml_loader_raises_on_non_mapping_root(isolated_identity_file: Path):
    isolated_identity_file.write_text("- just\n- a\n- list\n")
    hi._reset_cache_for_tests()
    with pytest.raises(RuntimeError, match="must be a YAML mapping"):
        hi.get_local_identities()


def test_yaml_loader_raises_on_non_list_aliases(isolated_identity_file: Path):
    isolated_identity_file.write_text(yaml.safe_dump({"aliases": "not-a-list"}))
    hi._reset_cache_for_tests()
    with pytest.raises(RuntimeError, match="must be a list"):
        hi.get_local_identities()


def test_load_file_aliases_returns_empty_when_file_absent(tmp_path: Path):
    # Pass an explicit missing path — no env juggling needed.
    result = hi._load_file_aliases(tmp_path / "does-not-exist.yaml")
    assert result == set()


# ---------------------------------------------------------------------------
# Cache behavior
# ---------------------------------------------------------------------------


def test_cache_reset_for_tests_drops_cached_identities(isolated_identity_file: Path):
    isolated_identity_file.write_text(yaml.safe_dump({"aliases": ["first-name"]}))
    hi._reset_cache_for_tests()
    assert hi.is_local_host("first-name") is True

    isolated_identity_file.write_text(yaml.safe_dump({"aliases": ["second-name"]}))
    # Without reset, the cache still serves the old set.
    assert hi.is_local_host("second-name") is False
    hi._reset_cache_for_tests()
    assert hi.is_local_host("second-name") is True
