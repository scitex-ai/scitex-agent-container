"""Tests for ``_listen._config`` (sac listen base-URL resolver).

Covers the precedence chain in :func:`listen_base_url` — config.yaml
``listen.port`` / ``listen.host`` first, built-in defaults
(``http://127.0.0.1:7878``) as the safety net. Malformed config must
fall back silently so a typo in operator-edited yaml can never block
agent startup.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from scitex_agent_container._listen import _config as listen_cfg


@pytest.fixture
def cfg_path(tmp_path: Path):
    """Redirect the ``_default_config_path`` lookup at a tmp dir.

    Uses explicit save/restore of ``$SCITEX_AGENT_CONTAINER_CONFIG``
    rather than ``monkeypatch.setenv`` per PA-306 (no mocks).
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


class TestListenBaseURL:
    # ------------------------------------------------------------------
    # Defaults when no config file exists.
    # ------------------------------------------------------------------
    def test_default_host_when_no_config(self, cfg_path: Path) -> None:
        # Arrange: cfg_path file is not created → no config on disk.
        # Act
        host = listen_cfg.listen_host()
        # Assert
        assert host == "127.0.0.1"

    def test_default_port_when_no_config(self, cfg_path: Path) -> None:
        # Arrange: cfg_path file is not created → no config on disk.
        # Act
        port = listen_cfg.listen_port()
        # Assert
        assert port == 7878

    def test_default_base_url_when_no_config(self, cfg_path: Path) -> None:
        # Arrange: cfg_path file is not created → no config on disk.
        # Act
        base_url = listen_cfg.listen_base_url()
        # Assert
        assert base_url == "http://127.0.0.1:7878"

    # ------------------------------------------------------------------
    # ``listen.port`` from YAML.
    # ------------------------------------------------------------------
    def test_reads_listen_port_from_config(self, cfg_path: Path) -> None:
        # Arrange
        cfg_path.write_text("listen:\n  port: 9090\n")
        # Act
        port = listen_cfg.listen_port()
        # Assert
        assert port == 9090

    def test_base_url_uses_port_from_config(self, cfg_path: Path) -> None:
        # Arrange
        cfg_path.write_text("listen:\n  port: 9090\n")
        # Act
        base_url = listen_cfg.listen_base_url()
        # Assert
        assert base_url == "http://127.0.0.1:9090"

    # ------------------------------------------------------------------
    # ``listen.host`` from YAML.
    # ------------------------------------------------------------------
    def test_reads_listen_host_from_config(self, cfg_path: Path) -> None:
        # Arrange
        cfg_path.write_text("listen:\n  host: 100.64.1.2\n  port: 7878\n")
        # Act
        host = listen_cfg.listen_host()
        # Assert
        assert host == "100.64.1.2"

    def test_base_url_uses_host_from_config(self, cfg_path: Path) -> None:
        # Arrange
        cfg_path.write_text("listen:\n  host: 100.64.1.2\n  port: 7878\n")
        # Act
        base_url = listen_cfg.listen_base_url()
        # Assert
        assert base_url == "http://100.64.1.2:7878"

    # ------------------------------------------------------------------
    # String → int coercion for YAML-quoted ports.
    # ------------------------------------------------------------------
    def test_string_port_coerced_to_int(self, cfg_path: Path) -> None:
        # Arrange: YAML quirk — quoted port arrives as a str.
        cfg_path.write_text('listen:\n  port: "7901"\n')
        # Act
        port = listen_cfg.listen_port()
        # Assert
        assert port == 7901

    # ------------------------------------------------------------------
    # Malformed YAML / out-of-range ports fall back to defaults.
    # ------------------------------------------------------------------
    def test_malformed_listen_block_port_falls_back(self, cfg_path: Path) -> None:
        # Arrange: non-mapping ``listen`` block.
        cfg_path.write_text("listen: not_a_mapping\n")
        # Act
        port = listen_cfg.listen_port()
        # Assert
        assert port == 7878

    def test_malformed_listen_block_base_url_falls_back(self, cfg_path: Path) -> None:
        # Arrange: non-mapping ``listen`` block.
        cfg_path.write_text("listen: not_a_mapping\n")
        # Act
        base_url = listen_cfg.listen_base_url()
        # Assert
        assert base_url == "http://127.0.0.1:7878"

    def test_negative_port_falls_back_to_default(self, cfg_path: Path) -> None:
        # Arrange
        cfg_path.write_text("listen:\n  port: -1\n")
        # Act
        port = listen_cfg.listen_port()
        # Assert
        assert port == 7878
