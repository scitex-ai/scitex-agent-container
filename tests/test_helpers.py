"""Regression tests for cli_pkg/_helpers.py — todo#454 fix.

Verifies that get_agent_list_data() reports local agents as 'running' when
they are alive in tmux, even when screen has no sessions (the original bug
caused false-positive all-dead alerts for MBA fleet which runs tmux).
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from scitex_agent_container.registry import Registry
from scitex_agent_container.cli_pkg._helpers import get_agent_list_data


MINIMAL_CONFIG = {
    "apiVersion": "cld-agent/v1",
    "kind": "Agent",
    "metadata": {"name": "test-agent"},
    "spec": {"runtime": "claude-code", "model": "sonnet"},
}


@pytest.fixture
def tmp_config(tmp_path: Path) -> str:
    cfg_file = tmp_path / "test-agent.yaml"
    cfg_file.write_text(yaml.dump(MINIMAL_CONFIG))
    return str(cfg_file)


@pytest.fixture
def registry(tmp_path: Path) -> Registry:
    return Registry(registry_dir=tmp_path / "registry")


class TestGetAgentListDataTmuxFix:
    """todo#454: local agents alive in tmux must show as 'running'."""

    def test_tmux_alive_reports_running(self, registry, tmp_config):
        """Agent in tmux but NOT in screen must show status='running'."""
        registry.add("test-agent", tmp_config, "cld-test-agent", pid=99999)

        with (
            patch(
                "scitex_agent_container.runtimes.tmux.TmuxManager.exists",
                return_value=True,
            ),
            patch(
                "scitex_agent_container.runtimes.screen.ScreenManager.exists",
                return_value=False,
            ),
        ):
            data = get_agent_list_data(registry)

        assert len(data) == 1
        assert data[0]["name"] == "test-agent"
        assert data[0]["status"] == "running", (
            "Agent alive in tmux must be reported as running (todo#454 regression)"
        )

    def test_screen_only_still_reports_running(self, registry, tmp_config):
        """Agents in screen (not tmux) must still work correctly."""
        registry.add("test-agent", tmp_config, "cld-test-agent", pid=99999)

        with (
            patch(
                "scitex_agent_container.runtimes.tmux.TmuxManager.exists",
                return_value=False,
            ),
            patch(
                "scitex_agent_container.runtimes.screen.ScreenManager.exists",
                return_value=True,
            ),
        ):
            data = get_agent_list_data(registry)

        assert len(data) == 1
        assert data[0]["status"] == "running"

    def test_neither_tmux_nor_screen_reports_stopped(self, registry, tmp_config):
        """Agent with no live session in either multiplexer must show stopped."""
        registry.add("test-agent", tmp_config, "cld-test-agent", pid=99999)

        with (
            patch(
                "scitex_agent_container.runtimes.tmux.TmuxManager.exists",
                return_value=False,
            ),
            patch(
                "scitex_agent_container.runtimes.screen.ScreenManager.exists",
                return_value=False,
            ),
        ):
            data = get_agent_list_data(registry)

        assert len(data) == 1
        assert data[0]["status"] == "stopped"
