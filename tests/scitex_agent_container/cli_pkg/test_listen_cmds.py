"""Tests for cli_pkg.listen_cmds (``sac listen``).

TQ cleanup: every test carries AAA markers (TQ002) and exactly one
assertion (TQ007). Same-shape invariants over small input sets collapse
into ``pytest.parametrize``. Test names spell out the behaviour being
verified (TQ003-compatible). Module docstring summarises intent (TQ001).

No-mocks discipline: ``_swap_attr`` / ``_swap_module`` are hand-rolled
save-and-restore context managers (PA-306 pattern) — no ``unittest.mock``,
no ``MagicMock``, no ``monkeypatch`` / ``mocker``. ``uvicorn`` is replaced
with a real callable-bearing object so the production call site executes
real attribute lookups.
"""

from __future__ import annotations

import sys as _sys
from contextlib import contextmanager

import click as _click
import pytest
from click.testing import CliRunner

from scitex_agent_container.cli_pkg.listen_cmds import (
    _is_loopback,
    _split_bind,
    listen,
)

# ---------------------------------------------------------------------------
# Hand-rolled save/restore seams (PA-306 — no monkeypatch, no mock).
# ---------------------------------------------------------------------------


@contextmanager
def _swap_attr(obj, name, value):
    """Replace ``obj.<name>`` with ``value`` for the block; restore after."""
    saved = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, saved)


@contextmanager
def _swap_module(name, fake):
    """Inject ``fake`` at ``sys.modules[name]``; restore the prior entry."""
    saved = _sys.modules.get(name)
    _sys.modules[name] = fake
    try:
        yield
    finally:
        if saved is None:
            _sys.modules.pop(name, None)
        else:
            _sys.modules[name] = saved


class _FakeUvicorn:
    """Real callable-bearing stand-in for the ``uvicorn`` module.

    Records the ``host``/``port`` of any ``run()`` call so tests can
    assert on the production-side bind values without any mocking lib.
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def run(self, app, host, port, log_level, **_kw) -> None:
        self.calls.append({"host": host, "port": port})


# ---------------------------------------------------------------------------
# _split_bind — pure parsing, no I/O.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "spec,expected",
    [
        pytest.param("127.0.0.1:8080", ("127.0.0.1", 8080), id="ipv4-host-port"),
        pytest.param("localhost:9000", ("localhost", 9000), id="hostname-host-port"),
        pytest.param("[::1]:7777", ("::1", 7777), id="ipv6-bracketed-host-port"),
    ],
)
def test_split_bind_parses_valid_specs_into_host_port_tuple(spec, expected):
    # Arrange — spec/expected provided by parametrize.
    # Act
    result = _split_bind(spec)
    # Assert
    assert result == expected


@pytest.mark.parametrize(
    "spec",
    [
        pytest.param("nohostport", id="missing-port-separator"),
        pytest.param(":1234", id="empty-host"),
    ],
)
def test_split_bind_raises_usage_error_for_malformed_spec(spec):
    # Arrange
    parse = _split_bind
    # Act
    action = lambda: parse(spec)  # noqa: E731
    # Assert
    with pytest.raises(_click.UsageError):
        action()


# ---------------------------------------------------------------------------
# _is_loopback — pure classification, no DNS.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "host",
    [
        pytest.param("localhost", id="hostname-localhost"),
        pytest.param("127.0.0.1", id="ipv4-127-0-0-1"),
        # Per IPv4 spec, 127.0.0.0/8 is loopback.
        pytest.param("127.0.0.2", id="ipv4-127-0-0-2-in-loopback-block"),
        pytest.param("::1", id="ipv6-loopback"),
    ],
)
def test_is_loopback_returns_true_for_loopback_hosts(host):
    # Arrange — host provided by parametrize.
    # Act
    result = _is_loopback(host)
    # Assert
    assert result is True


@pytest.mark.parametrize(
    "host",
    [
        pytest.param("8.8.8.8", id="external-ipv4"),
        # Hostname strings other than 'localhost' aren't loopback (no DNS).
        pytest.param("example.com", id="non-localhost-hostname"),
    ],
)
def test_is_loopback_returns_false_for_non_loopback_hosts(host):
    # Arrange — host provided by parametrize.
    # Act
    result = _is_loopback(host)
    # Assert
    assert result is False


# ---------------------------------------------------------------------------
# listen command — CLI behaviour, with uvicorn swapped out at sys.modules.
# ---------------------------------------------------------------------------


def test_listen_print_token_exits_with_zero_status(tmp_path):
    # Arrange
    from scitex_agent_container._listen import tokens as _tokens

    tok = tmp_path / "tok.txt"
    fake_uvicorn = _FakeUvicorn()
    # Act
    with (
        _swap_attr(_tokens, "ensure_token", lambda p: "secret-token-abc"),
        _swap_module("uvicorn", fake_uvicorn),
    ):
        result = CliRunner().invoke(listen, ["--print-token", "--token-file", str(tok)])
    # Assert
    assert result.exit_code == 0, result.output


def test_listen_print_token_writes_token_to_stdout(tmp_path):
    # Arrange
    from scitex_agent_container._listen import tokens as _tokens

    tok = tmp_path / "tok.txt"
    fake_uvicorn = _FakeUvicorn()
    # Act
    with (
        _swap_attr(_tokens, "ensure_token", lambda p: "secret-token-abc"),
        _swap_module("uvicorn", fake_uvicorn),
    ):
        result = CliRunner().invoke(listen, ["--print-token", "--token-file", str(tok)])
    # Assert
    assert "secret-token-abc" in result.output


def test_listen_print_token_does_not_invoke_uvicorn_run(tmp_path):
    # Arrange
    from scitex_agent_container._listen import tokens as _tokens

    tok = tmp_path / "tok.txt"
    fake_uvicorn = _FakeUvicorn()
    # Act
    with (
        _swap_attr(_tokens, "ensure_token", lambda p: "secret-token-abc"),
        _swap_module("uvicorn", fake_uvicorn),
    ):
        CliRunner().invoke(listen, ["--print-token", "--token-file", str(tok)])
    # Assert
    assert fake_uvicorn.calls == []


def test_listen_non_loopback_bind_without_flag_exits_non_zero():
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(listen, ["--bind", "8.8.8.8:7878"])
    # Assert
    assert result.exit_code != 0


def test_listen_non_loopback_bind_without_flag_mentions_loopback_in_output():
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(listen, ["--bind", "8.8.8.8:7878"])
    # Assert
    assert "loopback" in result.output.lower()


def test_listen_default_loopback_bind_starts_uvicorn_with_zero_exit(tmp_path):
    # Arrange
    from scitex_agent_container._listen import server as _server
    from scitex_agent_container._listen import tokens as _tokens

    fake_uvicorn = _FakeUvicorn()
    # Act
    with (
        _swap_attr(_tokens, "ensure_token", lambda p: "tok"),
        _swap_attr(_tokens, "default_token_path", lambda: tmp_path / "default.tok"),
        _swap_attr(_server, "create_app", lambda token: object()),
        _swap_module("uvicorn", fake_uvicorn),
    ):
        result = CliRunner().invoke(listen, [])
    # Assert
    assert result.exit_code == 0, result.output


def test_listen_default_loopback_bind_passes_default_host_port_to_uvicorn(tmp_path):
    # Arrange
    from scitex_agent_container._listen import server as _server
    from scitex_agent_container._listen import tokens as _tokens

    fake_uvicorn = _FakeUvicorn()
    # Act
    with (
        _swap_attr(_tokens, "ensure_token", lambda p: "tok"),
        _swap_attr(_tokens, "default_token_path", lambda: tmp_path / "default.tok"),
        _swap_attr(_server, "create_app", lambda token: object()),
        _swap_module("uvicorn", fake_uvicorn),
    ):
        CliRunner().invoke(listen, [])
    # Assert
    assert fake_uvicorn.calls == [{"host": "127.0.0.1", "port": 7878}]


def test_listen_non_loopback_bind_with_allow_flag_passes_host_to_uvicorn(tmp_path):
    # Arrange
    from scitex_agent_container._listen import server as _server
    from scitex_agent_container._listen import tokens as _tokens

    fake_uvicorn = _FakeUvicorn()
    # Act
    with (
        _swap_attr(_tokens, "ensure_token", lambda p: "tok"),
        _swap_attr(_tokens, "default_token_path", lambda: tmp_path / "d.tok"),
        _swap_attr(_server, "create_app", lambda token: object()),
        _swap_module("uvicorn", fake_uvicorn),
    ):
        CliRunner().invoke(listen, ["--bind", "8.8.8.8:7878", "--allow-non-loopback"])
    # Assert
    assert fake_uvicorn.calls == [{"host": "8.8.8.8", "port": 7878}]
