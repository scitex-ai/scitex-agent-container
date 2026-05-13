"""Tests for ``_listen._config`` (sac listen base-URL resolver).

Covers the precedence chain in :func:`listen_base_url` — config.yaml
``listen.port`` / ``listen.host`` first, built-in defaults
(``http://127.0.0.1:7878``) as the safety net. Malformed config must
fall back silently so a typo in operator-edited yaml can never block
agent startup.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scitex_agent_container._listen import _config as listen_cfg


@pytest.fixture
def cfg_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the ``_default_config_path`` lookup at a tmp dir."""
    p = tmp_path / "config.yaml"
    monkeypatch.setenv("SCITEX_AGENT_CONTAINER_CONFIG", str(p))
    return p


class TestListenBaseURL:
    def test_defaults_when_no_config(self, cfg_path: Path) -> None:
        # File doesn't exist → built-in defaults.
        assert listen_cfg.listen_host() == "127.0.0.1"
        assert listen_cfg.listen_port() == 7878
        assert listen_cfg.listen_base_url() == "http://127.0.0.1:7878"

    def test_reads_listen_port_from_config(self, cfg_path: Path) -> None:
        cfg_path.write_text("listen:\n  port: 9090\n")
        assert listen_cfg.listen_port() == 9090
        assert listen_cfg.listen_base_url() == "http://127.0.0.1:9090"

    def test_reads_listen_host_from_config(self, cfg_path: Path) -> None:
        cfg_path.write_text("listen:\n  host: 100.64.1.2\n  port: 7878\n")
        assert listen_cfg.listen_host() == "100.64.1.2"
        assert listen_cfg.listen_base_url() == "http://100.64.1.2:7878"

    def test_string_port_coerced(self, cfg_path: Path) -> None:
        # YAML quirk: an unquoted operator-typed port may end up a str.
        cfg_path.write_text('listen:\n  port: "7901"\n')
        assert listen_cfg.listen_port() == 7901

    def test_malformed_yaml_falls_back(self, cfg_path: Path) -> None:
        cfg_path.write_text("listen: not_a_mapping\n")
        # Non-mapping listen block → defaults.
        assert listen_cfg.listen_port() == 7878
        assert listen_cfg.listen_base_url() == "http://127.0.0.1:7878"

    def test_negative_port_ignored(self, cfg_path: Path) -> None:
        cfg_path.write_text("listen:\n  port: -1\n")
        assert listen_cfg.listen_port() == 7878
