"""Tests for ``sac agent send``.

Covers the three HTTP delivery paths (in-SIF host-listen proxy, cross-host
peer ``/v1/turn``, local loopback ``/v1/turn``) and the REFUSAL that
terminates the command when all of them decline. The host-side
``claude --resume`` fallback these tests were originally written around was
removed 2026-08-14 — see the block comment where its tests used to be.

PA-306: no ``unittest.mock``. Production collaborators are swapped at
the module namespace via ``_swap`` context managers, and env mutations
go through explicit save/restore.

TQ cleanup: each test is named for the specific behaviour it verifies
(TQ003), carries the AAA marker triple (TQ002), and asserts exactly one
fact (TQ007). Shared invariants over equivalent inputs are collapsed
into ``pytest.parametrize`` so the matrix stays declarative.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

import pytest
from click.testing import CliRunner

import scitex_agent_container.cli_pkg.send_cmds as send_mod
from scitex_agent_container.cli_pkg.send_cmds import send
from tests.scitex_agent_container._helpers.explicit_spec import explicitize_yaml


# The generic ``_swap(name, fn)`` module-namespace helper lived here and is
# gone with its last caller: every remaining swap targets a specific
# collaborator (``os.kill``, ``post_turn_to_url``) and says so in its name.


@contextmanager
def _swap_os_kill(fn: Callable) -> Iterator[None]:
    saved = send_mod.os.kill
    send_mod.os.kill = fn  # type: ignore[assignment]
    try:
        yield
    finally:
        send_mod.os.kill = saved  # type: ignore[assignment]


def _seed_agent(tmp_path: Path, name: str, session_id: str) -> Path:
    yaml_root = tmp_path / "agents"
    agent_dir = yaml_root / name
    agent_dir.mkdir(parents=True)
    (agent_dir / "spec.yaml").write_text(
        explicitize_yaml(f"""apiVersion: scitex-agent-container/v3
kind: Agent
spec:
  runtime: apptainer
  host: ${{HOSTNAME}}
  workdir: {tmp_path / "workdir"}
  apptainer:
    image: /x.sif
    binds: []
  claude:
    model: sonnet
  health:
    enabled: true
    interval: 60
  restart:
    policy: on-failure
    max_retries: 3
""")
    )
    (tmp_path / "workdir").mkdir()
    state_dir = tmp_path / "state" / name
    state_dir.mkdir(parents=True)
    (state_dir / "session_id").write_text(session_id, encoding="utf-8")
    return yaml_root


@contextmanager
def _empty_state_db(tmp_path: Path) -> Iterator[None]:
    """Point ``state.db`` at a fresh empty file for the duration.

    The local-send branch in ``send`` consults
    ``state_db.list_active_instances()`` to decide whether an agent is
    running locally with a bound a2a_port. Without isolation a row left
    by an earlier test in the shared default db (CI runs the whole
    suite) makes ``alpha`` look "running" and the send POSTs to a dead
    loopback port instead of falling through to ``claude --resume``.
    Redirecting to an empty db (and reloading the import-time
    ``DEFAULT_DB_PATH``) keeps these resume-path tests deterministic.
    """
    import importlib

    import scitex_agent_container._state.state_db as _state_db_mod

    key = "SCITEX_AGENT_CONTAINER_STATE_DB"
    saved = os.environ.get(key)
    os.environ[key] = str(tmp_path / "isolated-state.db")
    importlib.reload(_state_db_mod)
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = saved
        importlib.reload(_state_db_mod)


@pytest.fixture
def isolated_env(tmp_path):
    """PA-306: env + send_mod.state_dir_for save/restore in one fixture.

    Isolates ``state.db`` so the local-send branch sees no stray active
    row for the seeded agent — which is what drives the send to its
    terminal REFUSAL branch (2026-08-14: there is no longer a
    ``claude --resume`` fallback to fall into).

    The ``resolve_config`` pin this fixture used to carry is gone with
    the resume path: the command no longer loads the agent's spec at all
    on the way to refusing, so there is nothing for a stray
    ``~/.scitex/agent-container/agents/<name>/spec.yaml`` to shadow.
    """
    yaml_root = _seed_agent(tmp_path, "alpha", "abc-123-def")
    key = "SCITEX_AGENT_CONTAINER_YAML_DIRS"
    saved_env = os.environ.get(key)
    saved_state = send_mod.state_dir_for
    os.environ[key] = str(yaml_root)
    send_mod.state_dir_for = (  # type: ignore[assignment]
        lambda name, root=None: tmp_path / "state" / name
    )
    with _empty_state_db(tmp_path):
        try:
            yield tmp_path
        finally:
            send_mod.state_dir_for = saved_state  # type: ignore[assignment]
            if saved_env is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = saved_env


# ---------------------------------------------------------------------------
# CLI argument validation
# ---------------------------------------------------------------------------


def test_invocation_without_prompt_or_key_exits_nonzero(isolated_env):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(send, ["alpha"])
    # Assert
    assert result.exit_code != 0


def test_invocation_without_prompt_or_key_reports_requirement_in_output(
    isolated_env,
):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(send, ["alpha"])
    # Assert
    assert "Either PROMPT or --key is required" in result.output


def test_invocation_with_both_prompt_and_key_exits_nonzero(isolated_env):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(send, ["alpha", "hello", "--key", "ESC"])
    # Assert
    assert result.exit_code != 0


def test_invocation_with_both_prompt_and_key_reports_mutual_exclusion(
    isolated_env,
):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(send, ["alpha", "hello", "--key", "ESC"])
    # Assert
    assert "mutually exclusive" in result.output


# ---------------------------------------------------------------------------
# --key ESC: SIGINT delivery
# ---------------------------------------------------------------------------


@pytest.fixture
def alpha_with_pid(isolated_env):
    """isolated_env plus a recorded pid file for the alpha agent."""
    (isolated_env / "state" / "alpha" / "pid").write_text("4242")
    return isolated_env


def _invoke_key_esc_capturing_kill():
    """Run ``send alpha --key ESC`` and return (result, kill_call)."""
    kill_call: dict = {}
    with _swap_os_kill(lambda pid, sig: kill_call.update(pid=pid, sig=sig)):
        runner = CliRunner()
        result = runner.invoke(send, ["alpha", "--key", "ESC"])
    return result, kill_call


def test_key_esc_with_recorded_pid_exits_zero(alpha_with_pid):
    # Arrange
    invoke = _invoke_key_esc_capturing_kill
    # Act
    result, _ = invoke()
    # Assert
    assert result.exit_code == 0, result.output


@pytest.mark.parametrize(
    "field,expected",
    [
        ("pid", 4_242),
        ("sig", 2),  # signal.SIGINT
    ],
)
def test_key_esc_delivers_sigint_to_recorded_pid(alpha_with_pid, field, expected):
    # Arrange
    invoke = _invoke_key_esc_capturing_kill
    # Act
    _, kill_call = invoke()
    # Assert
    assert kill_call[field] == expected


def test_key_unsupported_exits_nonzero(isolated_env):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(send, ["alpha", "--key", "F12"])
    # Assert
    assert result.exit_code != 0


def test_key_unsupported_reports_not_supported(isolated_env):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(send, ["alpha", "--key", "F12"])
    # Assert
    assert "not supported" in result.output


def test_key_esc_without_pid_file_exits_nonzero(isolated_env):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(send, ["alpha", "--key", "ESC"])
    # Assert
    assert result.exit_code != 0


def test_key_esc_without_pid_file_reports_not_running(isolated_env):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(send, ["alpha", "--key", "ESC"])
    # Assert
    assert "not running" in result.output


# ---------------------------------------------------------------------------
# NO DELIVERY PATH LEFT -> REFUSE (2026-08-14 containment invariant)
#
# What USED to live here: "Missing session_id", "Happy path: resume
# invocation", "--no-stream", "--model / --max-turns forwarding" and
# "Trailing -- passthrough" — ~20 tests, all of them asserting the SHAPE of
# a `claude --resume` argv the command ran ON THE BARE HOST whenever no A2A
# port was recorded. That fallback was the one path that genuinely launched
# an agent turn outside apptainer during normal operation, and it could not
# have worked anyway: a contained agent's session lives in the CONTAINER's
# ~/.claude/projects/ store, so a host-side resume finds nothing or resumes
# an unrelated host session.
#
# Those tests are not ported, they are DELETED, because their subject is
# gone. What replaces them is the property that took its place: with every
# HTTP delivery path declined, the command refuses and says why.
# ---------------------------------------------------------------------------


def test_no_delivery_path_exits_nonzero(isolated_env):
    # Arrange — isolated_env leaves an empty state.db, so no a2a_port exists.
    runner = CliRunner()
    # Act
    result = runner.invoke(send, ["alpha", "follow up please"])
    # Assert
    assert result.exit_code != 0, result.output


def test_no_delivery_path_reports_the_missing_a2a_port(isolated_env):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(send, ["alpha", "follow up please"])
    # Assert
    assert "no A2A port is recorded" in result.output, result.output


def test_no_delivery_path_says_the_agent_is_contained(isolated_env):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(send, ["alpha", "follow up please"])
    # Assert
    assert "apptainer" in result.output, result.output


def test_no_delivery_path_names_the_recovery_command(isolated_env):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(send, ["alpha", "follow up please"])
    # Assert
    assert "sac agents start alpha" in result.output, result.output


def test_send_no_longer_exposes_a_no_stream_flag(isolated_env):
    """The flag only ever shaped the removed claude argv."""
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(send, ["alpha", "hi", "--no-stream"])
    # Assert
    assert "no such option" in result.output.lower(), result.output


# ---------------------------------------------------------------------------
# Cross-host send: state.db row on peer → ssh://peer:port/v1/turn POST.
# ---------------------------------------------------------------------------


@pytest.fixture
def remote_send_env(tmp_path):
    """State.db redirect + peer config so cross-host send fires."""
    import importlib

    saved_db = os.environ.get("SCITEX_AGENT_CONTAINER_STATE_DB")
    saved_host = os.environ.get("SAC_HOST")
    saved_cfg = os.environ.get("SCITEX_AGENT_CONTAINER_CONFIG")
    db = tmp_path / "state.db"
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "host:\n  fallback: hostname-short\npeers:\n  peer-x:\n    ssh: peer-x\n"
    )
    os.environ["SCITEX_AGENT_CONTAINER_STATE_DB"] = str(db)
    os.environ["SAC_HOST"] = "lead-host"
    os.environ["SCITEX_AGENT_CONTAINER_CONFIG"] = str(cfg)
    import scitex_agent_container._state.state_db as _state_db_mod

    importlib.reload(_state_db_mod)
    try:
        yield tmp_path
    finally:
        for k, v in (
            ("SCITEX_AGENT_CONTAINER_STATE_DB", saved_db),
            ("SAC_HOST", saved_host),
            ("SCITEX_AGENT_CONTAINER_CONFIG", saved_cfg),
        ):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        importlib.reload(_state_db_mod)


def test_remote_send_without_a2a_port_raises_typed_error(remote_send_env):
    # Arrange — seed a row with NO a2a_port (the proj-scitex-stats case).
    from scitex_agent_container._state.state_db import record_instance_start

    record_instance_start(name="zeta", host="peer-x", a2a_port=None)
    runner = CliRunner()
    # Act
    result = runner.invoke(send, ["zeta", "hi"])
    # Assert
    assert "did not register an A2A port" in result.output


def test_remote_send_with_a2a_port_dispatches_to_post_turn_to_url(remote_send_env):
    # Arrange — seed remote row + stub post_turn_to_url collaborator.
    from scitex_agent_container._state.state_db import record_instance_start

    record_instance_start(name="zeta", host="peer-x", a2a_port=18888)
    captured: dict = {}

    def fake_post(url, text, *, exit_after=False, timeout_s=600.0):
        captured["url"] = url
        captured["text"] = text
        return "REMOTE-REPLY"

    import scitex_agent_container._network.peer as _peer_mod

    saved = _peer_mod.post_turn_to_url
    _peer_mod.post_turn_to_url = fake_post  # type: ignore[assignment]
    try:
        runner = CliRunner()
        # Act
        result = runner.invoke(send, ["zeta", "hi"])
    finally:
        _peer_mod.post_turn_to_url = saved  # type: ignore[assignment]
    # Assert
    assert captured.get("url") == "ssh://peer-x:18888/v1/turn"


def test_remote_send_prints_reply_from_peer(remote_send_env):
    # Arrange
    from scitex_agent_container._state.state_db import record_instance_start

    record_instance_start(name="zeta", host="peer-x", a2a_port=18888)
    import scitex_agent_container._network.peer as _peer_mod

    saved = _peer_mod.post_turn_to_url
    _peer_mod.post_turn_to_url = lambda *a, **kw: "REMOTE-REPLY"  # type: ignore[assignment]
    try:
        runner = CliRunner()
        # Act
        result = runner.invoke(send, ["zeta", "hi"])
    finally:
        _peer_mod.post_turn_to_url = saved  # type: ignore[assignment]
    # Assert
    assert "REMOTE-REPLY" in result.output


# ---------------------------------------------------------------------------
# Local send: active state.db row on THIS host with a bound a2a_port →
# http://127.0.0.1:port/v1/turn POST (NOT host-side claude --resume).
# The fix for the 2026-05-22 apptainer mis-target diagnosis.
# ---------------------------------------------------------------------------


# Loopback A2A port for the local-send fixtures. A port reads as a
# whole identifier, so it stays bare (NL001 carve-out); the suppression
# keeps the file lint-clean since the rule still flags 4+ digit ints.
_LOCAL_PORT = 19005  # stx-allow: STX-NL001


@contextmanager
def _swap_peer_post_turn_to_url(fn: Callable) -> Iterator[None]:
    import scitex_agent_container._network.peer as _peer_mod

    saved = _peer_mod.post_turn_to_url
    _peer_mod.post_turn_to_url = fn  # type: ignore[assignment]
    try:
        yield
    finally:
        _peer_mod.post_turn_to_url = saved  # type: ignore[assignment]


def test_local_send_with_a2a_port_dispatches_to_loopback_v1turn(remote_send_env):
    # Arrange — seed a LOCAL row (host == current SAC_HOST) with a port.
    from scitex_agent_container._state.state_db import record_instance_start

    record_instance_start(name="local-a", host="lead-host", a2a_port=_LOCAL_PORT)
    captured: dict = {}

    def fake_post(url, text, *, exit_after=False, timeout_s=600.0):
        captured["url"] = url
        return "LOCAL-REPLY"

    # Act
    with _swap_peer_post_turn_to_url(fake_post):
        CliRunner().invoke(send, ["local-a", "hi"])
    # Assert
    assert captured.get("url") == "http://127.0.0.1:19005/v1/turn"


def test_local_send_forwards_the_prompt_text(remote_send_env):
    # Arrange
    from scitex_agent_container._state.state_db import record_instance_start

    record_instance_start(name="local-a", host="lead-host", a2a_port=_LOCAL_PORT)
    captured: dict = {}

    def fake_post(url, text, *, exit_after=False, timeout_s=600.0):
        captured["text"] = text
        return "LOCAL-REPLY"

    # Act
    with _swap_peer_post_turn_to_url(fake_post):
        CliRunner().invoke(send, ["local-a", "do the thing"])
    # Assert
    assert captured.get("text") == "do the thing"


def test_local_send_prints_reply_from_loopback(remote_send_env):
    # Arrange
    from scitex_agent_container._state.state_db import record_instance_start

    record_instance_start(name="local-a", host="lead-host", a2a_port=_LOCAL_PORT)
    # Act
    with _swap_peer_post_turn_to_url(lambda *a, **kw: "LOCAL-REPLY"):
        result = CliRunner().invoke(send, ["local-a", "hi"])
    # Assert
    assert "LOCAL-REPLY" in result.output


def test_local_send_exits_zero_on_loopback_reply(remote_send_env):
    # Arrange
    from scitex_agent_container._state.state_db import record_instance_start

    record_instance_start(name="local-a", host="lead-host", a2a_port=_LOCAL_PORT)
    # Act
    with _swap_peer_post_turn_to_url(lambda *a, **kw: "LOCAL-REPLY"):
        result = CliRunner().invoke(send, ["local-a", "hi"])
    # Assert
    assert result.exit_code == 0


def test_local_send_without_a2a_port_does_not_take_the_http_path(remote_send_env):
    # Arrange — a LOCAL row WITHOUT a bound port must not be POSTed to.
    from scitex_agent_container._state.state_db import record_instance_start

    record_instance_start(name="local-b", host="lead-host", a2a_port=None)
    posted: dict = {}

    def fake_post(url, text, *, exit_after=False, timeout_s=600.0):
        posted["url"] = url
        return "SHOULD-NOT-HAPPEN"

    # Act
    with _swap_peer_post_turn_to_url(fake_post):
        CliRunner().invoke(send, ["local-b", "hi"])
    # Assert
    assert "url" not in posted


def test_local_send_without_a2a_port_refuses_instead_of_going_bare(remote_send_env):
    """The old name for this was "falls through to resume" — it no longer does."""
    # Arrange
    from scitex_agent_container._state.state_db import record_instance_start

    record_instance_start(name="local-b", host="lead-host", a2a_port=None)

    def fake_post(url, text, *, exit_after=False, timeout_s=600.0):
        return "SHOULD-NOT-HAPPEN"

    # Act
    with _swap_peer_post_turn_to_url(fake_post):
        result = CliRunner().invoke(send, ["local-b", "hi"])
    # Assert
    assert "no A2A port is recorded" in result.output, result.output


def test_local_send_failure_wraps_peer_error(remote_send_env):
    # Arrange
    from scitex_agent_container._network.peer import PeerError
    from scitex_agent_container._state.state_db import record_instance_start

    record_instance_start(name="local-a", host="lead-host", a2a_port=_LOCAL_PORT)

    def fake_post(url, text, *, exit_after=False, timeout_s=600.0):
        raise PeerError("connection refused")

    # Act
    with _swap_peer_post_turn_to_url(fake_post):
        result = CliRunner().invoke(send, ["local-a", "hi"])
    # Assert
    assert "local send failed" in result.output
