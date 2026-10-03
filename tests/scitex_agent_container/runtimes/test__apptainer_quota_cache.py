"""Automatic quota binds obey explicit denials without changing explicit mounts."""

from scitex_agent_container.config._types import AgentConfig
from scitex_agent_container.runtimes._apptainer_quota_cache import (
    quota_cache_bind_flags,
)
from scitex_agent_container.runtimes._p3a_default_binds import apply_default_binds


def test_present_cache_is_not_automatically_bound_to_a_denied_footprint(tmp_path):
    # Arrange
    config = AgentConfig(name="restricted-child")
    config.env = {"SCITEX_STORE_DSN": "", "PGUSER": "", "PGPASSFILE": ""}
    cache = tmp_path / "quota.json"
    cache.write_text("synthetic metadata")
    # Act
    flags = quota_cache_bind_flags(config, cache)
    # Assert
    assert flags == []


def test_undeclared_normal_agent_keeps_present_cache_bind(tmp_path):
    # Arrange
    config = AgentConfig(name="normal-agent")
    cache = tmp_path / "quota.json"
    cache.write_text("synthetic metadata")
    # Act
    flags = quota_cache_bind_flags(config, cache)
    # Assert
    assert flags == ["--bind", f"{cache}:/var/sac/quota-cache.json:ro",
                     "--env", "CCT_QUOTA_CACHE_PATH=/var/sac/quota-cache.json"]


def test_partial_denial_keeps_normal_quota_behavior(tmp_path):
    # Arrange
    config = AgentConfig(name="normal-agent")
    config.env = {"SCITEX_STORE_DSN": "", "PGUSER": ""}
    cache = tmp_path / "quota.json"
    cache.write_text("synthetic metadata")
    # Act
    flags = quota_cache_bind_flags(config, cache)
    # Assert
    assert f"{cache}:/var/sac/quota-cache.json:ro" in flags


def test_absent_cache_keeps_the_existing_no_bind_behavior(tmp_path):
    # Arrange
    config = AgentConfig(name="normal-agent")
    # Act
    flags = quota_cache_bind_flags(config, tmp_path / "absent")
    # Assert
    assert flags == []


def test_explicit_quota_mount_is_preserved_for_a_denied_footprint(tmp_path):
    # Arrange
    config = AgentConfig(name="restricted-child")
    config.env = {"SCITEX_STORE_DSN": "", "PGUSER": "", "PGPASSFILE": ""}
    declared = [f"{tmp_path}:/var/sac/quota-cache.json:ro"]
    # Act
    result = apply_default_binds(declared, config=config)
    # Assert
    assert result == declared
