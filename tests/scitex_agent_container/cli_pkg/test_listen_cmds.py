"""Tests for cli_pkg.listen_cmds (sac listen)."""

from __future__ import annotations

import pytest
from click.testing import CliRunner

import scitex_agent_container.cli_pkg.listen_cmds as lc
from scitex_agent_container.cli_pkg.listen_cmds import (
    _is_loopback,
    _split_bind,
    listen,
)

# ---------------------------------------------------------------------------
# _split_bind
# ---------------------------------------------------------------------------


def test_split_bind_ipv4_form():
    assert _split_bind("127.0.0.1:8080") == ("127.0.0.1", 8080)


def test_split_bind_hostname_form():
    assert _split_bind("localhost:9000") == ("localhost", 9000)


def test_split_bind_ipv6_form():
    host, port = _split_bind("[::1]:7777")
    assert host == "::1"
    assert port == 7777


def test_split_bind_invalid_no_port_raises():
    import click as _click

    with pytest.raises(_click.UsageError):
        _split_bind("nohostport")


def test_split_bind_invalid_no_host_raises():
    import click as _click

    with pytest.raises(_click.UsageError):
        _split_bind(":1234")


# ---------------------------------------------------------------------------
# _is_loopback
# ---------------------------------------------------------------------------


def test_is_loopback_localhost():
    assert _is_loopback("localhost") is True


def test_is_loopback_127_0_0_1():
    assert _is_loopback("127.0.0.1") is True


def test_is_loopback_127_0_0_2():
    # Per IPv4 spec, 127.0.0.0/8 is loopback.
    assert _is_loopback("127.0.0.2") is True


def test_is_loopback_external_ipv4():
    assert _is_loopback("8.8.8.8") is False


def test_is_loopback_ipv6_loopback():
    assert _is_loopback("::1") is True


def test_is_loopback_nonparsable_host_is_false():
    # Hostname strings other than 'localhost' aren't loopback (no DNS resolution here).
    assert _is_loopback("example.com") is False


# ---------------------------------------------------------------------------
# listen command
# ---------------------------------------------------------------------------


def test_listen_print_token_short_circuits(monkeypatch, tmp_path):
    """--print-token should print and return without starting uvicorn."""
    tok = tmp_path / "tok.txt"

    monkeypatch.setattr(lc, "__name__", lc.__name__)  # no-op anchor
    from scitex_agent_container._listen import tokens as _tokens

    monkeypatch.setattr(_tokens, "ensure_token", lambda p: "secret-token-abc")

    called = {"uvicorn": False}

    def boom(*a, **kw):
        called["uvicorn"] = True

    # Lazy uvicorn import — we must monkey-patch the global name once imported.
    import sys

    fake_uvicorn = type("U", (), {"run": staticmethod(boom)})()
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)

    runner = CliRunner()
    result = runner.invoke(listen, ["--print-token", "--token-file", str(tok)])
    assert result.exit_code == 0, result.output
    assert "secret-token-abc" in result.output
    assert called["uvicorn"] is False


def test_listen_non_loopback_without_flag_fails(monkeypatch):
    runner = CliRunner()
    result = runner.invoke(listen, ["--bind", "8.8.8.8:7878"])
    assert result.exit_code != 0
    assert "not loopback" in result.output.lower() or "loopback" in result.output


def test_listen_starts_uvicorn_when_allowed(monkeypatch, tmp_path):
    """The happy path on loopback ends with a uvicorn.run() call."""
    from scitex_agent_container._listen import server as _server
    from scitex_agent_container._listen import tokens as _tokens

    monkeypatch.setattr(_tokens, "ensure_token", lambda p: "tok")
    monkeypatch.setattr(_tokens, "default_token_path", lambda: tmp_path / "default.tok")
    monkeypatch.setattr(_server, "create_app", lambda token: object())

    seen = {}

    def fake_run(app, host, port, log_level):
        seen["host"] = host
        seen["port"] = port

    import sys

    fake_uvicorn = type("U", (), {"run": staticmethod(fake_run)})()
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)

    runner = CliRunner()
    result = runner.invoke(listen, [])
    assert result.exit_code == 0, result.output
    assert seen == {"host": "127.0.0.1", "port": 7878}


def test_listen_non_loopback_with_flag_starts(monkeypatch, tmp_path):
    from scitex_agent_container._listen import server as _server
    from scitex_agent_container._listen import tokens as _tokens

    monkeypatch.setattr(_tokens, "ensure_token", lambda p: "tok")
    monkeypatch.setattr(_tokens, "default_token_path", lambda: tmp_path / "d.tok")
    monkeypatch.setattr(_server, "create_app", lambda token: object())

    seen = {}

    def fake_run(app, host, port, log_level):
        seen["host"] = host

    import sys

    fake_uvicorn = type("U", (), {"run": staticmethod(fake_run)})()
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)

    runner = CliRunner()
    result = runner.invoke(listen, ["--bind", "8.8.8.8:7878", "--allow-non-loopback"])
    assert result.exit_code == 0, result.output
    assert seen["host"] == "8.8.8.8"
