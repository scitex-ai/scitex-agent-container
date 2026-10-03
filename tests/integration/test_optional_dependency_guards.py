"""Lean imports fail clearly; installed peers keep their actual typed contracts."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _probe(body: str, blocked: tuple[str, ...] = ()) -> subprocess.CompletedProcess:
    # CI installs peers into a target directory added to the parent sys.path.
    # The child uses the same interpreter, whose own site-packages may be bare.
    # Preserve actual import paths without inheriting auth/store environment.
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "LANG": "C.UTF-8",
        "TZ": "UTC",
        "PYTHONPATH": os.pathsep.join([str(ROOT / "src"), *filter(None, sys.path)]),
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
    }
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
    with tempfile.TemporaryDirectory(prefix="sac-optional-probe-") as home:
        env["HOME"] = home
        env["TMPDIR"] = home
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


def test_probe_preserves_dependencies_in_the_active_ci_target_path(tmp_path):
    # Arrange -- CI layers peers outside the interpreter's own site-packages.
    target = tmp_path / "ci-target-site"
    target.mkdir()
    (target / "synthetic_layered_peer.py").write_text("IDENTITY = 'ci-target-peer'\n")
    original = sys.path[:]
    sys.path.insert(0, str(target))
    # Act
    try:
        result = _probe(
            "import synthetic_layered_peer\nprint(synthetic_layered_peer.IDENTITY)\n"
        )
    finally:
        sys.path[:] = original
    # Assert
    assert (result.returncode, result.stdout) == (0, "ci-target-peer\n"), result.stderr


def test_probe_does_not_inherit_auth_or_store_pins(tmp_path):
    # Arrange
    pins = {
        "CODEX_HOME": str(tmp_path / "synthetic-profile"),
        "SCITEX_STORE_DSN": "synthetic-invalid-store",
    }
    previous = {key: os.environ.get(key) for key in pins}
    os.environ.update(pins)
    # Act
    try:
        result = _probe(
            "import os\nprint(any(key in os.environ for key in ('CODEX_HOME', 'SCITEX_STORE_DSN')))\n"
        )
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    # Assert
    assert (result.returncode, result.stdout) == (0, "False\n"), result.stderr
