"""Lean imports fail clearly; installed peers keep their actual typed contracts."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PREFIXES = (
    "SCITEX_",
    "SAC_",
    "PG",
    "CODEX_",
    "OPENAI_",
    "ANTHROPIC_",
    "CLAUDE_",
    "CCT_",
    "UV_INDEX",
    "UV_EXTRA_INDEX",
)


def _probe(body: str, blocked: tuple[str, ...] = ()) -> subprocess.CompletedProcess:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(PREFIXES)
        and not any(
            word in key.upper() for word in ("AUTH", "TOKEN", "SECRET", "API_KEY")
        )
    }
    env["PYTHONPATH"] = str(ROOT / "src")
    code = """
import asyncio
import importlib
import importlib.abc
import json
import sys

class MissingDependencyFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in BLOCKED:
            raise ModuleNotFoundError('synthetic missing dependency', name=fullname)
        return None

sys.meta_path.insert(0, MissingDependencyFinder())
"""
    code = "BLOCKED = " + repr(blocked) + "\n" + code + body
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=5,
    )


@pytest.mark.parametrize(
    "blocked, expression, extra",
    [
        (
            "mcp",
            "importlib.import_module('scitex_agent_container._mcp._channel_tool_defs')",
            "mcp",
        ),
        (
            "mcp",
            "importlib.import_module('scitex_agent_container._mcp._channel_feedback_tools').read_dispatch_status({}, agent='synthetic')",
            "mcp",
        ),
        (
            "mcp",
            "importlib.import_module('scitex_agent_container._mcp._channel_send_errors').error_result(None)",
            "mcp",
        ),
        (
            "mcp",
            "importlib.import_module('scitex_agent_container._mcp._channel_tools').register_tools(None, agent_name='synthetic', listen_url='synthetic', bearer=None)",
            "mcp",
        ),
        (
            "mcp",
            "asyncio.run(importlib.import_module('scitex_agent_container._mcp.channel')._run('synthetic', 'synthetic', None))",
            "mcp",
        ),
        (
            "mcp",
            "asyncio.run(importlib.import_module('scitex_agent_container._mcp.channel')._serve(None, None, name='synthetic', listen_url='synthetic', bearer=None))",
            "mcp",
        ),
        (
            "mcp",
            "asyncio.run(importlib.import_module('scitex_agent_container._mcp.channel')._push_channel_event(None, {}))",
            "mcp",
        ),
        (
            "fastmcp",
            "importlib.import_module('scitex_agent_container.cli_pkg.mcp_group')._default_load_fastmcp_version()",
            "mcp",
        ),
        (
            "scitex_app",
            "importlib.import_module('scitex_agent_container._django._server')._run_server()",
            "gui",
        ),
        (
            "scitex_app",
            "importlib.import_module('scitex_agent_container._django._server').serve(package='synthetic', project_dir='/synthetic', port=0, host='127.0.0.1')",
            "gui",
        ),
        (
            "django",
            "importlib.import_module('scitex_agent_container._django.apps')",
            "gui",
        ),
        (
            "django",
            "importlib.import_module('scitex_agent_container._django.urls')",
            "gui",
        ),
        (
            "django",
            "importlib.import_module('scitex_agent_container._django.views')",
            "gui",
        ),
        (
            "scitex_ui",
            "importlib.import_module('scitex_agent_container._django.views')._mount_base(None, '')",
            "gui",
        ),
        (
            "scitex_ui",
            "importlib.import_module('scitex_agent_container._django.views')._shell_context(None, 'synthetic', '')",
            "gui",
        ),
    ],
)
def test_missing_optional_peer_raises_its_declared_extra_before_side_effects(
    blocked, expression, extra
):
    # Arrange
    body = (
        "try:\n    "
        + expression
        + "\nexcept ImportError as error:\n    print(str(error))\n    print(type(error.__cause__).__name__)\nelse:\n    raise AssertionError('missing optional dependency admitted')\n"
    )
    # Act
    result = _probe(body, (blocked,))
    # Assert
    assert result.returncode == 0 and result.stdout.splitlines() == [
        f"SAC {'GUI' if extra == 'gui' else 'MCP'} dependencies are unavailable; install scitex-agent-container[{extra}].",
        "ModuleNotFoundError",
    ], result.stderr


def test_core_and_cli_groups_import_without_gui_or_mcp_peers():
    # Arrange
    blocked = ("django", "scitex_app", "scitex_ui", "mcp", "fastmcp")
    body = "import scitex_agent_container\nimport scitex_agent_container.cli_pkg.gui_group\nimport scitex_agent_container.cli_pkg.mcp_group\nprint('core imports passed')\n"
    # Act
    result = _probe(body, blocked)
    # Assert
    assert (result.returncode, result.stdout) == (0, "core imports passed\n"), (
        result.stderr
    )


def test_real_mcp_tool_classes_are_used_when_peer_is_installed():
    # Arrange
    pytest.importorskip("mcp")
    from mcp.types import Tool

    from scitex_agent_container._mcp._channel_tool_defs import build_tool_list

    # Act
    tools = build_tool_list()
    # Assert
    assert tools and all(type(tool) is Tool for tool in tools)


def test_real_django_app_config_survives_scitex_app_absence():
    # Arrange
    pytest.importorskip("django")
    body = "from django.apps import AppConfig\nfrom scitex_agent_container._django.apps import AgentContainerDashboardConfig\nprint(issubclass(AgentContainerDashboardConfig, AppConfig))\n"
    # Act
    result = _probe(body, ("scitex_app",))
    # Assert
    assert (result.returncode, result.stdout) == (0, "True\n"), result.stderr


def test_gui_views_use_real_django_request_type():
    # Arrange
    pytest.importorskip("django")
    from django.http import HttpRequest

    from scitex_agent_container._django import views

    # Act
    request_type = views.HttpRequest
    # Assert
    assert request_type is HttpRequest
