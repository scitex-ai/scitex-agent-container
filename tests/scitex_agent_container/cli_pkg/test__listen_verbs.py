"""Tests for ``cli_pkg/_listen_verbs.py`` — ``sac listen start`` / ``stop``.

``listen`` is a NOUN (a command group), exactly like ``agents`` / ``db``
/ ``host``. Booting a daemon off the bare noun is the anti-pattern the
scitex CLI convention names outright ("trailing noun, no action:
never") — a typo or a stray tab-complete would start a server on 7878.
These tests pin the explicit verbs that fix it, and the coherence of the
full set (``start`` / ``stop`` / ``restart`` / ``status``).

Bind resolution is tested from BOTH sides — on the verb
(``sac listen start --bind …``) and on the group
(``sac listen --bind … start``) — because ``restart``/``status`` read
the group's stash while an operator's fingers reach for the verb.

No-mocks discipline (PA-306): ``_swap_attr`` / ``_swap_module`` are
hand-rolled save-and-restore context managers — no ``unittest.mock``, no
``MagicMock``, no ``monkeypatch``. ``uvicorn`` is replaced with a real
callable-bearing object so the production call site executes real
attribute lookups.

AAA + >=3-word names + one assert per test (STX-TQ002 / PA-307).
"""

from __future__ import annotations

import json as _json
import sys as _sys
import types as _types
from contextlib import contextmanager
from typing import Iterator

import pytest
from click.testing import CliRunner

from scitex_agent_container.cli_pkg.listen_cmds import listen

# ---------------------------------------------------------------------------
# Hand-rolled save/restore seams (PA-306 — no monkeypatch, no mock).
# ---------------------------------------------------------------------------


@contextmanager
def _swap_attr(obj, name: str, value) -> Iterator[None]:
    """Replace ``obj.<name>`` with ``value`` for the block; restore after."""
    saved = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, saved)


@contextmanager
def _swap_module(name: str, fake) -> Iterator[None]:
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

    Records the ``host``/``port`` the production code binds to. Mirrors
    the explicit ``Config`` + ``Server`` + ``server.run()`` shape the
    production boot path uses.
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
                self.should_exit = False
                self.started = False

            def run(self) -> None:
                _calls.append({"host": self.config.host, "port": self.config.port})

        self.Config = Config
        self.Server = Server


def _fake_app():
    """Stand-in for the Starlette app ``create_app`` returns."""
    return _types.SimpleNamespace(state=_types.SimpleNamespace())


@contextmanager
def _boot_harness(tmp_path) -> Iterator[_FakeUvicorn]:
    """Hermetic ``start``-path harness: no real flock, port, or bind.

    ``_do_start_listen`` routes the lock through ``_standby.resolve_startup``
    (hot-standby + failover), whose real path takes the flock at the
    operator's ``default_lock_dir()`` AND socket-probes the real port — so a
    CLI start test on a host with a live ``sac listen`` would otherwise stand
    by forever or probe the LIVE 7878 control plane. Swapping it out keeps
    these tests hermetic, fast, and incapable of touching the real daemon.
    """
    from pathlib import Path as _Path

    from scitex_agent_container._listen import _single_instance, _standby
    from scitex_agent_container._listen import server as _server
    from scitex_agent_container._listen import tokens as _tokens
    from scitex_agent_container._listen._single_instance import LockHandle

    fake_handle = LockHandle(fd=-1, pid_file=_Path("/nonexistent/listen-7878.pid"))
    fake_uvicorn = _FakeUvicorn()

    @contextmanager
    def _noop_guard() -> Iterator[None]:
        yield

    with (
        _swap_attr(_standby, "resolve_startup", lambda **_kw: fake_handle),
        _swap_attr(_standby, "standby_signal_guard", _noop_guard),
        # The throwaway fd=-1 handle must never reach the real releaser
        # (fcntl rejects a negative fd with ValueError).
        _swap_attr(_single_instance, "release_listen_lock", lambda _h: None),
        _swap_attr(_tokens, "ensure_token", lambda _p: "tok"),
        _swap_attr(_tokens, "default_token_path", lambda: tmp_path / "default.tok"),
        _swap_attr(_server, "create_app", lambda token, **_kw: _fake_app()),
        _swap_module("uvicorn", fake_uvicorn),
    ):
        yield fake_uvicorn


class _StopRecorder:
    """Hand-rolled stand-in for ``_stop.stop_listen``.

    Records the kwargs the CLI passes and returns a REAL ``StopResult``
    so the production render path executes real attribute lookups.
    """

    def __init__(self, **result_fields) -> None:
        from scitex_agent_container._listen._stop import StopResult

        self.calls: list[dict] = []
        defaults = {
            "ok": True,
            "escalated_to_sigkill": False,
            "had_prior_pidfile": True,
            "prior_pid_alive": True,
            "prior_pid": 4242,
        }
        defaults.update(result_fields)
        self._result = StopResult(**defaults)

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return self._result


@contextmanager
def _stop_harness(tmp_path, recorder: _StopRecorder) -> Iterator[None]:
    """Point ``sac listen stop`` at ``recorder`` and a throwaway lock dir."""
    from scitex_agent_container._listen import _single_instance, _stop

    with (
        _swap_attr(_stop, "stop_listen", recorder),
        _swap_attr(_single_instance, "default_lock_dir", lambda: tmp_path / "locks"),
    ):
        yield


# ---------------------------------------------------------------------------
# The verb set — `listen` is a noun; these four are its whole lifecycle.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "verb",
    [
        pytest.param("start", id="start-boots-the-daemon"),
        pytest.param("stop", id="stop-halts-the-daemon"),
        pytest.param("restart", id="restart-stop-clean-relaunch"),
        pytest.param("status", id="status-health-report"),
    ],
)
def test_listen_group_exposes_lifecycle_verb(verb: str) -> None:
    # Arrange — verb provided by parametrize.
    group = listen
    # Act
    registered = group.commands
    # Assert
    assert verb in registered


# ---------------------------------------------------------------------------
# `sac listen start` — the explicit boot verb.
# ---------------------------------------------------------------------------


def test_listen_start_verb_boots_uvicorn_on_default_bind(tmp_path) -> None:
    # Arrange
    runner = CliRunner()
    # Act
    with _boot_harness(tmp_path) as fake_uvicorn:
        runner.invoke(listen, ["start"])
    # Assert
    assert fake_uvicorn.calls == [{"host": "127.0.0.1", "port": 7878}]


def test_listen_start_verb_exits_zero_on_clean_shutdown(tmp_path) -> None:
    # Arrange
    runner = CliRunner()
    # Act
    with _boot_harness(tmp_path):
        result = runner.invoke(listen, ["start"])
    # Assert
    assert result.exit_code == 0, result.output


def test_listen_start_accepts_bind_option_on_the_verb(tmp_path) -> None:
    """`sac listen start --bind …` — where an operator's fingers go."""
    # Arrange
    runner = CliRunner()
    # Act
    with _boot_harness(tmp_path) as fake_uvicorn:
        runner.invoke(listen, ["start", "--bind", "127.0.0.1:7999"])
    # Assert
    assert fake_uvicorn.calls == [{"host": "127.0.0.1", "port": 7999}]


def test_listen_start_accepts_bind_option_on_the_group(tmp_path) -> None:
    """`sac listen --bind … start` — the form `restart`/`status` already use."""
    # Arrange
    runner = CliRunner()
    # Act
    with _boot_harness(tmp_path) as fake_uvicorn:
        runner.invoke(listen, ["--bind", "127.0.0.1:7999", "start"])
    # Assert
    assert fake_uvicorn.calls == [{"host": "127.0.0.1", "port": 7999}]


def test_listen_start_verb_emits_no_deprecation_warning(tmp_path) -> None:
    """The whole point of the verb: it is the NON-deprecated form."""
    # Arrange
    runner = CliRunner()
    # Act
    with _boot_harness(tmp_path):
        result = runner.invoke(listen, ["start"])
    # Assert
    assert "DEPRECATED" not in result.output.upper()


def test_listen_start_print_token_writes_token_to_stdout(tmp_path) -> None:
    # Arrange
    runner = CliRunner()
    # Act
    with _boot_harness(tmp_path):
        result = runner.invoke(listen, ["start", "--print-token"])
    # Assert
    assert "tok" in result.output


def test_listen_start_print_token_does_not_boot_uvicorn(tmp_path) -> None:
    # Arrange
    runner = CliRunner()
    # Act
    with _boot_harness(tmp_path) as fake_uvicorn:
        runner.invoke(listen, ["start", "--print-token"])
    # Assert
    assert fake_uvicorn.calls == []


def test_listen_start_non_loopback_bind_without_flag_exits_non_zero(
    tmp_path,
) -> None:
    # Arrange
    runner = CliRunner()
    # Act
    with _boot_harness(tmp_path):
        result = runner.invoke(listen, ["start", "--bind", "8.8.8.8:7878"])
    # Assert
    assert result.exit_code != 0


def test_listen_start_non_loopback_bind_with_flag_passes_host_to_uvicorn(
    tmp_path,
) -> None:
    # Arrange
    runner = CliRunner()
    # Act
    with _boot_harness(tmp_path) as fake_uvicorn:
        runner.invoke(
            listen, ["start", "--bind", "8.8.8.8:7878", "--allow-non-loopback"]
        )
    # Assert
    assert fake_uvicorn.calls == [{"host": "8.8.8.8", "port": 7878}]


# ---------------------------------------------------------------------------
# `sac listen stop` — the verb that did not exist.
# ---------------------------------------------------------------------------


def test_listen_stop_verb_exits_zero_after_stopping_daemon(tmp_path) -> None:
    # Arrange
    recorder = _StopRecorder()
    # Act
    with _stop_harness(tmp_path, recorder):
        result = CliRunner().invoke(listen, ["stop"])
    # Assert
    assert result.exit_code == 0, result.output


def test_listen_stop_verb_reports_the_stopped_pid(tmp_path) -> None:
    # Arrange
    recorder = _StopRecorder(prior_pid=4242)
    # Act
    with _stop_harness(tmp_path, recorder):
        result = CliRunner().invoke(listen, ["stop"])
    # Assert
    assert "4242" in result.output


def test_listen_stop_on_dead_daemon_exits_zero_idempotently(tmp_path) -> None:
    """`systemctl stop` contract: stopping a down daemon is SUCCESS."""
    # Arrange
    recorder = _StopRecorder(
        had_prior_pidfile=False, prior_pid_alive=False, prior_pid=None
    )
    # Act
    with _stop_harness(tmp_path, recorder):
        result = CliRunner().invoke(listen, ["stop"])
    # Assert
    assert result.exit_code == 0, result.output


def test_listen_stop_on_dead_daemon_says_it_was_not_running(tmp_path) -> None:
    # Arrange
    recorder = _StopRecorder(
        had_prior_pidfile=False, prior_pid_alive=False, prior_pid=None
    )
    # Act
    with _stop_harness(tmp_path, recorder):
        result = CliRunner().invoke(listen, ["stop"])
    # Assert
    assert "not running" in result.output.lower()


def test_listen_stop_exits_non_zero_when_stop_fails(tmp_path) -> None:
    # Arrange
    recorder = _StopRecorder(ok=False, error="ERROR: PID 4242 survived SIGKILL")
    # Act
    with _stop_harness(tmp_path, recorder):
        result = CliRunner().invoke(listen, ["stop"])
    # Assert
    assert result.exit_code != 0


def test_listen_stop_failure_surfaces_the_real_cause(tmp_path) -> None:
    """FAIL LOUD: the error names the REAL cause, not 'did not respond'."""
    # Arrange
    recorder = _StopRecorder(ok=False, error="ERROR: PID 4242 survived SIGKILL")
    # Act
    with _stop_harness(tmp_path, recorder):
        result = CliRunner().invoke(listen, ["stop"])
    # Assert
    assert "survived SIGKILL" in result.output


def test_listen_stop_forwards_force_flag_to_stop_listen(tmp_path) -> None:
    # Arrange
    recorder = _StopRecorder()
    # Act
    with _stop_harness(tmp_path, recorder):
        CliRunner().invoke(listen, ["stop", "--force"])
    # Assert
    assert recorder.calls[0]["force"] is True


def test_listen_stop_forwards_grace_secs_to_stop_listen(tmp_path) -> None:
    # Arrange
    recorder = _StopRecorder()
    # Act
    with _stop_harness(tmp_path, recorder):
        CliRunner().invoke(listen, ["stop", "--grace-secs", "30"])
    # Assert
    assert recorder.calls[0]["grace_secs"] == 30.0


def test_listen_stop_resolves_port_from_the_group_bind(tmp_path) -> None:
    """`sac listen stop` stops the daemon `sac listen start` would start."""
    # Arrange
    recorder = _StopRecorder()
    # Act
    with _stop_harness(tmp_path, recorder):
        CliRunner().invoke(listen, ["--bind", "127.0.0.1:7999", "stop"])
    # Assert
    assert recorder.calls[0]["port"] == 7999


def test_listen_stop_json_emits_parseable_envelope_on_stdout(tmp_path) -> None:
    """§8: `cmd --json | jq` must work with zero log contamination."""
    # Arrange
    recorder = _StopRecorder(prior_pid=4242)
    runner = CliRunner()
    # Act
    with _stop_harness(tmp_path, recorder):
        result = runner.invoke(listen, ["stop", "--json"])
    # Assert
    assert _json.loads(result.stdout)["prior_pid"] == 4242


def test_listen_stop_json_envelope_reports_ok_flag(tmp_path) -> None:
    # Arrange
    recorder = _StopRecorder()
    runner = CliRunner()
    # Act
    with _stop_harness(tmp_path, recorder):
        result = runner.invoke(listen, ["stop", "--json"])
    # Assert
    assert _json.loads(result.stdout)["ok"] is True


def test_listen_stop_warns_loudly_on_sigkill_escalation(tmp_path) -> None:
    # Arrange
    recorder = _StopRecorder(escalated_to_sigkill=True)
    # Act
    with _stop_harness(tmp_path, recorder):
        result = CliRunner().invoke(listen, ["stop"])
    # Assert
    assert "SIGKILL" in result.output


def test_listen_stop_surfaces_force_killed_wedged_port_holder(tmp_path) -> None:
    """Paper trail for the codified pkill (the 'curl hangs forever' case)."""
    # Arrange
    recorder = _StopRecorder(port_holders_killed=(9191,))
    # Act
    with _stop_harness(tmp_path, recorder):
        result = CliRunner().invoke(listen, ["stop"])
    # Assert
    assert "9191" in result.output
