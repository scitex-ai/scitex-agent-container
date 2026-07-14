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
import types as _types
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

    Records the ``host``/``port`` the production code binds to so tests
    can assert on it without any mocking lib. ``sac listen`` builds an
    explicit ``uvicorn.Config`` + ``uvicorn.Server`` and calls
    ``server.run()`` (so it can stash the server on ``app.state`` for the
    SIGTERM shutdown bridge — card sac-listen-sigterm-sse-shutdown-hang);
    the fake mirrors that shape and records the bind on ``Server.run()``.
    The legacy ``run()`` is kept for any caller that still uses
    ``uvicorn.run`` directly.
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []
        _calls = self.calls

        class Config:
            def __init__(self, app, host, port, **_kw) -> None:
                self.app = app
                self.host = host
                self.port = port

        class Server:
            def __init__(self, config) -> None:
                self.config = config
                # Real ``uvicorn.Server`` exposes these; the bridge reads
                # ``should_exit`` and the loopback harness reads ``started``.
                self.should_exit = False
                self.started = False

            def run(self) -> None:
                _calls.append(
                    {"host": self.config.host, "port": self.config.port}
                )

        self.Config = Config
        self.Server = Server

    def run(self, app, host, port, log_level, **_kw) -> None:
        self.calls.append({"host": host, "port": port})


def _fake_app():
    """Stand-in for the Starlette app ``create_app`` returns.

    Production stashes the uvicorn Server on ``app.state.uvicorn_server``
    (for the shutdown bridge), so the fake needs a settable ``.state``.
    """
    return _types.SimpleNamespace(state=_types.SimpleNamespace())


@contextmanager
def _swap_standby_serves():
    """Make the hot-standby startup a hermetic no-op that 'acquires' at once.

    ``_do_start_listen`` now routes the lock through
    ``_standby.resolve_startup`` (hot-standby + failover). Its real path
    takes the flock at the operator's ``default_lock_dir()`` AND socket-
    probes the real port — so a CLI start test running on a host with a
    live ``sac listen`` would otherwise stand by forever or probe 7878.
    Swapping ``resolve_startup`` to return a throwaway handle (and the
    signal guard to a no-op) keeps these tests hermetic + fast. The lazy
    ``from .._listen._standby import ...`` inside the CLI binds these
    swapped attributes at call time.
    """
    from pathlib import Path as _Path

    from scitex_agent_container._listen import _single_instance, _standby
    from scitex_agent_container._listen._single_instance import LockHandle

    fake_handle = LockHandle(fd=-1, pid_file=_Path("/nonexistent/listen-7878.pid"))

    @contextmanager
    def _noop_guard():
        yield

    with (
        _swap_attr(_standby, "resolve_startup", lambda **_kw: fake_handle),
        _swap_attr(_standby, "standby_signal_guard", _noop_guard),
        # The CLI releases the handle in its ``finally``; the throwaway
        # fd=-1 handle must never reach the real releaser (fcntl rejects a
        # negative fd with ValueError), so no-op the release too.
        _swap_attr(_single_instance, "release_listen_lock", lambda _h: None),
    ):
        yield


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
        _swap_standby_serves(),
        _swap_attr(_tokens, "ensure_token", lambda p: "tok"),
        _swap_attr(_tokens, "default_token_path", lambda: tmp_path / "default.tok"),
        _swap_attr(_server, "create_app", lambda token, **_kw: _fake_app()),
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
        _swap_standby_serves(),
        _swap_attr(_tokens, "ensure_token", lambda p: "tok"),
        _swap_attr(_tokens, "default_token_path", lambda: tmp_path / "default.tok"),
        _swap_attr(_server, "create_app", lambda token, **_kw: _fake_app()),
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
        _swap_standby_serves(),
        _swap_attr(_tokens, "ensure_token", lambda p: "tok"),
        _swap_attr(_tokens, "default_token_path", lambda: tmp_path / "d.tok"),
        _swap_attr(_server, "create_app", lambda token, **_kw: _fake_app()),
        _swap_module("uvicorn", fake_uvicorn),
    ):
        CliRunner().invoke(listen, ["--bind", "8.8.8.8:7878", "--allow-non-loopback"])
    # Assert
    assert fake_uvicorn.calls == [{"host": "8.8.8.8", "port": 7878}]


# ---------------------------------------------------------------------------
# Bare-boot deprecation (phase W: warn + FORWARD).
#
# `listen` is a NOUN, so booting a daemon off the bare noun is a footgun —
# but the bare form MUST keep working until every launcher has migrated to
# `sac listen start`. Three of them still invoke it bare TODAY:
#
#   1. scripts/systemd/sac-listen.service  -> ExecStart=/usr/bin/env sac listen
#   2. _listen/_restart.py                 -> [sac_binary(), "listen"] respawn
#   3. PR #543's systemd JobSpec           -> command="sac listen"
#
# `sac listen` IS the host control plane on 127.0.0.1:7878; the whole fleet
# loses host access when it is down. Flipping the bare form to show-help
# would take it offline. So: warn, never break. The first test below is the
# regression guard that keeps it that way.
# ---------------------------------------------------------------------------


def test_bare_listen_still_boots_the_daemon_for_launcher_compat(tmp_path):
    """HARD COUPLING: the systemd unit + `_restart` respawn invoke this bare."""
    # Arrange
    from scitex_agent_container._listen import server as _server
    from scitex_agent_container._listen import tokens as _tokens

    fake_uvicorn = _FakeUvicorn()
    # Act
    with (
        _swap_standby_serves(),
        _swap_attr(_tokens, "ensure_token", lambda p: "tok"),
        _swap_attr(_tokens, "default_token_path", lambda: tmp_path / "default.tok"),
        _swap_attr(_server, "create_app", lambda token, **_kw: _fake_app()),
        _swap_module("uvicorn", fake_uvicorn),
    ):
        CliRunner().invoke(listen, [])
    # Assert
    assert fake_uvicorn.calls == [{"host": "127.0.0.1", "port": 7878}]


def test_bare_listen_boot_warns_that_it_is_deprecated(tmp_path):
    # Arrange
    from scitex_agent_container._listen import server as _server
    from scitex_agent_container._listen import tokens as _tokens

    fake_uvicorn = _FakeUvicorn()
    # Act
    with (
        _swap_standby_serves(),
        _swap_attr(_tokens, "ensure_token", lambda p: "tok"),
        _swap_attr(_tokens, "default_token_path", lambda: tmp_path / "default.tok"),
        _swap_attr(_server, "create_app", lambda token, **_kw: _fake_app()),
        _swap_module("uvicorn", fake_uvicorn),
    ):
        result = CliRunner().invoke(listen, [])
    # Assert
    assert "DEPRECATED" in result.output.upper()


def test_bare_listen_boot_warning_names_the_start_verb(tmp_path):
    """A deprecation that doesn't name the replacement is just noise."""
    # Arrange
    from scitex_agent_container._listen import server as _server
    from scitex_agent_container._listen import tokens as _tokens

    fake_uvicorn = _FakeUvicorn()
    # Act
    with (
        _swap_standby_serves(),
        _swap_attr(_tokens, "ensure_token", lambda p: "tok"),
        _swap_attr(_tokens, "default_token_path", lambda: tmp_path / "default.tok"),
        _swap_attr(_server, "create_app", lambda token, **_kw: _fake_app()),
        _swap_module("uvicorn", fake_uvicorn),
    ):
        result = CliRunner().invoke(listen, [])
    # Assert
    assert "sac listen start" in result.output


def test_bare_listen_boot_warning_names_the_removal_version(tmp_path):
    """CLI convention §5: every phase names the removal version."""
    # Arrange
    from scitex_agent_container._listen import server as _server
    from scitex_agent_container._listen import tokens as _tokens
    from scitex_agent_container.cli_pkg.listen_cmds import (
        BARE_BOOT_REMOVAL_VERSION,
    )

    fake_uvicorn = _FakeUvicorn()
    # Act
    with (
        _swap_standby_serves(),
        _swap_attr(_tokens, "ensure_token", lambda p: "tok"),
        _swap_attr(_tokens, "default_token_path", lambda: tmp_path / "default.tok"),
        _swap_attr(_server, "create_app", lambda token, **_kw: _fake_app()),
        _swap_module("uvicorn", fake_uvicorn),
    ):
        result = CliRunner().invoke(listen, [])
    # Assert
    assert BARE_BOOT_REMOVAL_VERSION in result.output


def test_bare_listen_boot_warning_goes_to_stderr_not_stdout(tmp_path):
    """§8: warnings are stderr. `sac listen --print-token` must stay pipeable."""
    # Arrange
    from scitex_agent_container._listen import server as _server
    from scitex_agent_container._listen import tokens as _tokens

    fake_uvicorn = _FakeUvicorn()
    runner = CliRunner()
    # Act
    with (
        _swap_standby_serves(),
        _swap_attr(_tokens, "ensure_token", lambda p: "tok"),
        _swap_attr(_tokens, "default_token_path", lambda: tmp_path / "default.tok"),
        _swap_attr(_server, "create_app", lambda token, **_kw: _fake_app()),
        _swap_module("uvicorn", fake_uvicorn),
    ):
        result = runner.invoke(listen, [])
    # Assert
    assert "DEPRECATED" not in result.stdout.upper()


def test_listen_print_token_emits_no_boot_deprecation_warning(tmp_path):
    """`--print-token` never boots, so the boot-deprecation must not fire."""
    # Arrange
    from scitex_agent_container._listen import tokens as _tokens

    tok = tmp_path / "tok.txt"
    fake_uvicorn = _FakeUvicorn()
    # Act
    with (
        _swap_attr(_tokens, "ensure_token", lambda p: "secret-token-abc"),
        _swap_module("uvicorn", fake_uvicorn),
    ):
        result = CliRunner().invoke(
            listen, ["--print-token", "--token-file", str(tok)]
        )
    # Assert
    assert "DEPRECATED" not in result.output.upper()
