"""Regression tests for ``runtimes._apptainer_listen_env.listen_env_flags``.

Root-cause coverage for card
``sac-agent-cannot-spawn-agents-listen-7878-unreachable``: an agent
INSIDE a sac container could not spawn/start peers because

* ``SAC_LISTEN_BASE_URL`` was injected as the loopback
  ``http://127.0.0.1:7878`` the listen server BINDS — a container that
  dials its own ``127.0.0.1`` reaches nothing and the spawn POST times
  out. a2a already reaches the host via the canonical ``hostname:port``,
  so the injected URL must use that same host-reachable form.
* ``$SCITEX_AGENT_CONTAINER_YAML_DIRS`` (the agent spec-dir search path)
  was never forwarded into the container, so an in-container
  ``agent_start`` found no specs.

These tests assert ``listen_env_flags`` now injects a host-reachable
``SAC_LISTEN_BASE_URL`` (NOT 127.0.0.1) and forwards the spec-dir env
when the host has it set.

Conventions: STX-TQ002 AAA-marker, STX-TQ007 one-assert, PA-306
no-mock-fixtures (explicit env save/restore, real config objects).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

import pytest

from scitex_agent_container.runtimes._apptainer_listen_env import listen_env_flags


class _NoChannelsConfig:
    """Minimal real config object with no ``server:sac`` channel.

    ``listen_env_flags`` reads only ``getattr(config, "claude", None)``;
    a ``claude`` of ``None`` means no bus channel is requested, so the
    bearer is non-fatal and the function returns the env flags without
    raising. This is a real object, not a mock.
    """

    claude = None


@pytest.fixture
def _isolate_home(tmp_path: Path) -> Iterator[Path]:
    """Redirect ``HOME`` to a tmp dir so no real bearer token is read.

    The bearer resolver anchors on ``Path.home()``; an empty tmp HOME
    yields no token, which is harmless for a no-``server:sac`` spec.
    """
    saved = os.environ.get("HOME")
    os.environ["HOME"] = str(tmp_path)
    try:
        yield tmp_path
    finally:
        if saved is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = saved


@pytest.fixture
def _loopback_config_yaml(tmp_path: Path) -> Iterator[Path]:
    """Point config.yaml at a tmp file with NO ``listen.host`` override.

    With ``listen.host`` absent, ``listen_host()`` returns the loopback
    default — exactly the regression scenario where the old code would
    have injected ``127.0.0.1``.
    """
    p = tmp_path / "config.yaml"
    key = "SCITEX_AGENT_CONTAINER_CONFIG"
    saved = os.environ.get(key)
    os.environ[key] = str(p)
    try:
        yield p
    finally:
        if saved is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = saved


@pytest.fixture
def _canonical_host(tmp_path: Path) -> Iterator[str]:
    """Pin ``host_config.canonical_host()`` via ``$SAC_HOST``.

    The canonical hostname is the host-reachable address the a2a
    turn_url path uses; pinning it makes the injected URL deterministic.
    """
    key = "SAC_HOST"
    saved = os.environ.get(key)
    os.environ[key] = "ywata-note-win"
    try:
        yield "ywata-note-win"
    finally:
        if saved is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = saved


@pytest.fixture
def _spec_dirs_env() -> Iterator[str]:
    """Set ``$SCITEX_AGENT_CONTAINER_YAML_DIRS`` on the host."""
    key = "SCITEX_AGENT_CONTAINER_YAML_DIRS"
    saved = os.environ.get(key)
    os.environ[key] = "/host/specs:/host/shared"
    try:
        yield "/host/specs:/host/shared"
    finally:
        if saved is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = saved


@pytest.fixture
def _no_spec_dirs_env() -> Iterator[None]:
    """Ensure ``$SCITEX_AGENT_CONTAINER_YAML_DIRS`` is unset."""
    key = "SCITEX_AGENT_CONTAINER_YAML_DIRS"
    saved = os.environ.get(key)
    os.environ.pop(key, None)
    try:
        yield None
    finally:
        if saved is not None:
            os.environ[key] = saved


def _env_value(flags: list[str], name: str) -> str | None:
    """Return the VALUE injected for ``--env <name>=<value>``, or None."""
    for i, tok in enumerate(flags):
        if tok == "--env" and i + 1 < len(flags):
            kv = flags[i + 1]
            if kv.startswith(f"{name}="):
                return kv.split("=", 1)[1]
    return None


class TestListenEnvFlags:
    def test_injects_listen_base_url_env(
        self,
        _isolate_home: Path,
        _loopback_config_yaml: Path,
        _canonical_host: str,
        _no_spec_dirs_env: None,
    ) -> None:
        # Arrange
        config = _NoChannelsConfig()
        # Act
        flags = listen_env_flags(config)
        # Assert
        assert _env_value(flags, "SAC_LISTEN_BASE_URL") is not None

    def test_listen_base_url_is_host_reachable_not_loopback(
        self,
        _isolate_home: Path,
        _loopback_config_yaml: Path,
        _canonical_host: str,
        _no_spec_dirs_env: None,
    ) -> None:
        # Arrange
        config = _NoChannelsConfig()
        # Act
        url = _env_value(flags=listen_env_flags(config), name="SAC_LISTEN_BASE_URL")
        # Assert
        assert "127.0.0.1" not in (url or "")

    def test_listen_base_url_uses_canonical_hostname(
        self,
        _isolate_home: Path,
        _loopback_config_yaml: Path,
        _canonical_host: str,
        _no_spec_dirs_env: None,
    ) -> None:
        # Arrange
        config = _NoChannelsConfig()
        # Act
        url = _env_value(flags=listen_env_flags(config), name="SAC_LISTEN_BASE_URL")
        # Assert
        assert url == "http://ywata-note-win:7878"

    def test_forwards_spec_dirs_env_when_set(
        self,
        _isolate_home: Path,
        _loopback_config_yaml: Path,
        _canonical_host: str,
        _spec_dirs_env: str,
    ) -> None:
        # Arrange
        config = _NoChannelsConfig()
        # Act
        value = _env_value(
            flags=listen_env_flags(config),
            name="SCITEX_AGENT_CONTAINER_YAML_DIRS",
        )
        # Assert
        assert value == "/host/specs:/host/shared"

    def test_omits_spec_dirs_env_when_unset(
        self,
        _isolate_home: Path,
        _loopback_config_yaml: Path,
        _canonical_host: str,
        _no_spec_dirs_env: None,
    ) -> None:
        # Arrange
        config = _NoChannelsConfig()
        # Act
        value = _env_value(
            flags=listen_env_flags(config),
            name="SCITEX_AGENT_CONTAINER_YAML_DIRS",
        )
        # Assert
        assert value is None
