"""Tests for ``sac mcp`` group — start / doctor / list-tools / install."""

from __future__ import annotations

import json
import sys
import types
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

from scitex_agent_container.cli_pkg import mcp_group as mg
from scitex_agent_container.cli_pkg.mcp_group import mcp


@pytest.fixture(autouse=True)
def _no_real_mcp(monkeypatch):
    """Avoid loading the real _mcp package by default — tests inject stubs."""
    return monkeypatch


def _install_fake_mcp(
    monkeypatch, *, run_server=None, server=None, raise_import: bool = False
):
    """Install a fake ``scitex_agent_container._mcp`` module."""
    if raise_import:
        # Force ImportError when ``from .._mcp import run_server`` runs.
        for k in list(sys.modules):
            if k.startswith("scitex_agent_container._mcp"):
                monkeypatch.delitem(sys.modules, k, raising=False)
        # Stub out so the import fails:
        bad = types.ModuleType("scitex_agent_container._mcp")
        # Don't define run_server / server attrs → ImportError on `from .. import x`.
        monkeypatch.setitem(sys.modules, "scitex_agent_container._mcp", bad)
        return
    fake = types.ModuleType("scitex_agent_container._mcp")
    fake.run_server = run_server or MagicMock()
    monkeypatch.setitem(sys.modules, "scitex_agent_container._mcp", fake)
    fake_server_mod = types.ModuleType("scitex_agent_container._mcp.server")
    fake_server_mod.get_server = lambda: server
    monkeypatch.setitem(
        sys.modules, "scitex_agent_container._mcp.server", fake_server_mod
    )


def test_start_dry_run_stdio():
    runner = CliRunner()
    result = runner.invoke(mcp, ["start", "--dry-run"])
    assert result.exit_code == 0
    assert "transport=stdio" in result.output


def test_start_dry_run_http():
    runner = CliRunner()
    result = runner.invoke(
        mcp, ["start", "--dry-run", "--http", "--host", "0.0.0.0", "--port", "9999"]
    )
    assert result.exit_code == 0
    assert "transport=http" in result.output
    assert "9999" in result.output


def test_start_invokes_run_server(monkeypatch):
    called = {}

    def fake_run(transport, host, port):
        called.update(transport=transport, host=host, port=port)

    _install_fake_mcp(monkeypatch, run_server=fake_run)
    runner = CliRunner()
    result = runner.invoke(mcp, ["start"])
    assert result.exit_code == 0, result.output
    assert called == {"transport": "stdio", "host": "127.0.0.1", "port": 8970}


def test_start_http_prints_url(monkeypatch):
    fake_run = MagicMock()
    _install_fake_mcp(monkeypatch, run_server=fake_run)
    runner = CliRunner()
    result = runner.invoke(mcp, ["start", "--http", "--port", "1234"])
    assert result.exit_code == 0, result.output
    assert "http://127.0.0.1:1234" in result.output
    fake_run.assert_called_once()


def test_start_import_error_surfaces(monkeypatch):
    # Make `from .._mcp import run_server` blow up.
    def fail_import(*a, **k):
        raise ImportError("no fastmcp")

    monkeypatch.setattr(
        mg,
        "__getattr__",
        lambda name: (_ for _ in ()).throw(ImportError()) if False else None,
        raising=False,
    )
    # Simpler approach: shadow the package with a module missing run_server.
    bad = types.ModuleType("scitex_agent_container._mcp")
    monkeypatch.setitem(sys.modules, "scitex_agent_container._mcp", bad)
    runner = CliRunner()
    result = runner.invoke(mcp, ["start"])
    assert result.exit_code != 0
    assert "fastmcp" in result.output


def test_doctor_ok(monkeypatch):
    fake_fastmcp = types.ModuleType("fastmcp")
    fake_fastmcp.__version__ = "9.9.9"
    monkeypatch.setitem(sys.modules, "fastmcp", fake_fastmcp)

    class FakeTool:
        def __init__(self, name):
            self.name = name
            self.description = "desc"

    class FakeServer:
        async def list_tools(self):
            return [FakeTool("a"), FakeTool("b")]

    _install_fake_mcp(monkeypatch, server=FakeServer())
    runner = CliRunner()
    result = runner.invoke(mcp, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "fastmcp" in result.output
    assert "MCP server ready" in result.output


def test_doctor_missing_fastmcp(monkeypatch):
    monkeypatch.setitem(sys.modules, "fastmcp", None)
    runner = CliRunner()
    result = runner.invoke(mcp, ["doctor"])
    assert result.exit_code != 0
    assert "fastmcp not installed" in result.output


def test_doctor_server_error(monkeypatch):
    fake_fastmcp = types.ModuleType("fastmcp")
    fake_fastmcp.__version__ = "1.0"
    monkeypatch.setitem(sys.modules, "fastmcp", fake_fastmcp)

    class BoomServer:
        async def list_tools(self):
            raise RuntimeError("boom")

    _install_fake_mcp(monkeypatch, server=BoomServer())
    # _enumerate_tools swallows RuntimeError in async path → returns [] (no error).
    # Force get_server itself to raise to hit the error branch.
    fake_server_mod = types.ModuleType("scitex_agent_container._mcp.server")

    def _raise():
        raise RuntimeError("registration failed")

    fake_server_mod.get_server = _raise
    monkeypatch.setitem(
        sys.modules, "scitex_agent_container._mcp.server", fake_server_mod
    )
    runner = CliRunner()
    result = runner.invoke(mcp, ["doctor"])
    assert result.exit_code != 0
    assert "MCP server error" in result.output


def test_list_tools_human(monkeypatch):
    class T:
        def __init__(self, n, d):
            self.name = n
            self.description = d

    class FakeServer:
        async def list_tools(self):
            return [T("foo", "Foo tool\nlong"), T("bar", "")]

    _install_fake_mcp(monkeypatch, server=FakeServer())
    runner = CliRunner()
    result = runner.invoke(mcp, ["list-tools"])
    assert result.exit_code == 0, result.output
    assert "foo" in result.output
    assert "bar" in result.output
    assert "Foo tool" in result.output


def test_list_tools_json(monkeypatch):
    class T:
        def __init__(self, n):
            self.name = n
            self.description = ""

    class FakeServer:
        async def list_tools(self):
            return [T("z"), T("a")]

    _install_fake_mcp(monkeypatch, server=FakeServer())
    runner = CliRunner()
    result = runner.invoke(mcp, ["list-tools", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["count"] == 2
    assert [t["name"] for t in payload["tools"]] == ["a", "z"]


def test_list_tools_import_error_json(monkeypatch):
    # Cause `from .._mcp.server import get_server` to ImportError.
    monkeypatch.setitem(
        sys.modules,
        "scitex_agent_container._mcp.server",
        types.ModuleType("scitex_agent_container._mcp.server"),
    )
    # Module without get_server → AttributeError on `from .. import get_server`.
    # Click's @command exception handler will surface. Easier: shadow with an
    # object that raises ImportError on attribute access.

    class _Bad(types.ModuleType):
        def __getattr__(self, name):
            raise ImportError("fastmcp missing")

    monkeypatch.setitem(
        sys.modules,
        "scitex_agent_container._mcp.server",
        _Bad("scitex_agent_container._mcp.server"),
    )
    runner = CliRunner()
    result = runner.invoke(mcp, ["list-tools", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["count"] == 0
    assert "fastmcp" in payload["error"]


def test_list_tools_import_error_human(monkeypatch):
    class _Bad(types.ModuleType):
        def __getattr__(self, name):
            raise ImportError("fastmcp missing")

    monkeypatch.setitem(
        sys.modules,
        "scitex_agent_container._mcp.server",
        _Bad("scitex_agent_container._mcp.server"),
    )
    runner = CliRunner()
    result = runner.invoke(mcp, ["list-tools"])
    assert result.exit_code == 0
    assert "fastmcp not installed" in result.output


def test_install_default():
    runner = CliRunner()
    result = runner.invoke(mcp, ["install"])
    assert result.exit_code == 0
    assert "Installation" in result.output
    assert "pip install" in result.output


def test_install_claude_code():
    runner = CliRunner()
    result = runner.invoke(mcp, ["install", "--claude-code"])
    assert result.exit_code == 0
    assert '"scitex-agent-container"' in result.output
    assert '"sac"' in result.output


def test_enumerate_tools_dict_fallback():
    """FastMCP 2.x style: server.tools is a plain dict."""

    class T:
        def __init__(self, n):
            self.name = n
            self.description = ""

    class Srv:
        # No async list_tools — only the dict path.
        tools = {"x": T("x"), "y": T("y")}

    result = mg._enumerate_tools(Srv())
    names = sorted(t.name for t in result)
    assert names == ["x", "y"]


def test_enumerate_tools_tool_manager_inner_dict():
    class T:
        def __init__(self, n):
            self.name = n
            self.description = ""

    class Manager:
        _tools = {"q": T("q")}

    class Srv:
        _tool_manager = Manager()

    result = mg._enumerate_tools(Srv())
    assert [t.name for t in result] == ["q"]


def test_enumerate_tools_empty():
    class Srv:
        pass

    assert mg._enumerate_tools(Srv()) == []
