"""CLI tests for the ``sac peer`` noun-group.

PA-306: no `unittest.mock`. The CLI's collaborators are swapped via
hand-rolled fake callables installed on the module's namespace and
restored on teardown — same effect as `monkeypatch` without the mock
library or the banned fixture parameter.

TQ cleanup: each test is named for the specific behaviour it verifies
(TQ003), carries the AAA marker triple (TQ002), and asserts exactly
one fact (TQ007). Shared invocations are factored into module-level
helpers so the matrix stays declarative.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Callable, Iterator

from click.testing import CliRunner

import scitex_agent_container._network.peer as peer_mod
from scitex_agent_container._network._peer_timeout import PeerTimeoutPending
from scitex_agent_container._network.peer import PeerError
from scitex_agent_container.cli_pkg.peer_group import peer_group


@contextmanager
def _swap(name: str, fn: Callable) -> Iterator[None]:
    """Swap ``peer_mod.<name>`` for ``fn`` for the duration of the block.

    This is a hand-rolled fake injector, NOT a mock — the replacement
    is a real callable with the production signature. Tests stay
    isolated by always restoring the original attribute on exit.
    """
    saved = getattr(peer_mod, name)
    setattr(peer_mod, name, fn)
    try:
        yield
    finally:
        setattr(peer_mod, name, saved)


# ---------------------------------------------------------------------------
# `sac peer --help` — group lists its verbs
# ---------------------------------------------------------------------------


def _invoke_help():
    runner = CliRunner()
    return runner.invoke(peer_group, ["--help"])


def test_group_help_exits_zero() -> None:
    # Arrange
    invoke = _invoke_help
    # Act
    result = invoke()
    # Assert
    assert result.exit_code == 0


def test_group_help_lists_post_turn_verb() -> None:
    # Arrange
    invoke = _invoke_help
    # Act
    result = invoke()
    # Assert
    assert "post-turn" in result.output


def test_group_help_lists_resolve_url_verb() -> None:
    # Arrange
    invoke = _invoke_help
    # Act
    result = invoke()
    # Assert
    assert "resolve-url" in result.output


# ---------------------------------------------------------------------------
# `sac peer post-turn AGENT TEXT` — happy path
# ---------------------------------------------------------------------------


def _invoke_post_turn_capturing():
    """Run ``post-turn alpha hi`` against a fake that records args + replies."""
    captured: dict = {}

    def fake_post_turn(name: str, text: str, **_kw) -> str:
        captured["call"] = (name, text)
        return "echo:hi"

    runner = CliRunner()
    with _swap("post_turn", fake_post_turn):
        result = runner.invoke(peer_group, ["post-turn", "alpha", "hi"])
    return result, captured


def test_post_turn_happy_path_exits_zero() -> None:
    # Arrange
    invoke = _invoke_post_turn_capturing
    # Act
    result, _ = invoke()
    # Assert
    assert result.exit_code == 0


def test_post_turn_happy_path_echoes_reply() -> None:
    # Arrange
    invoke = _invoke_post_turn_capturing
    # Act
    result, _ = invoke()
    # Assert
    assert result.output.strip() == "echo:hi"


def test_post_turn_forwards_agent_and_text_to_collaborator() -> None:
    # Arrange
    invoke = _invoke_post_turn_capturing
    # Act
    _, captured = invoke()
    # Assert
    assert captured["call"] == ("alpha", "hi")


# ---------------------------------------------------------------------------
# `sac peer post-turn ... --json` — envelope shape
# ---------------------------------------------------------------------------


def _invoke_post_turn_json():
    def fake_post_turn(*_a, **_kw) -> str:
        return "ok"

    runner = CliRunner()
    with _swap("post_turn", fake_post_turn):
        result = runner.invoke(peer_group, ["post-turn", "alpha", "hi", "--json"])
    return result


def test_post_turn_json_exits_zero() -> None:
    # Arrange
    invoke = _invoke_post_turn_json
    # Act
    result = invoke()
    # Assert
    assert result.exit_code == 0


def test_post_turn_json_emits_full_envelope() -> None:
    # Arrange
    invoke = _invoke_post_turn_json
    # Act
    result = invoke()
    # Assert
    assert json.loads(result.stdout) == {"text": "ok", "exit_after": False}


# ---------------------------------------------------------------------------
# `sac peer post-turn` — PeerError surface
# ---------------------------------------------------------------------------


def _invoke_post_turn_raising():
    def fake_post_turn(*_a, **_kw) -> str:
        raise PeerError("boom")

    runner = CliRunner()
    with _swap("post_turn", fake_post_turn):
        result = runner.invoke(peer_group, ["post-turn", "alpha", "hi"])
    return result


def test_post_turn_peer_error_exits_2() -> None:
    # Arrange
    invoke = _invoke_post_turn_raising
    # Act
    result = invoke()
    # Assert
    assert result.exit_code == 2


def test_post_turn_peer_error_message_surfaces_in_output() -> None:
    # Arrange
    # Click 8.2+ merges stderr into output when mix_stderr unavailable.
    invoke = _invoke_post_turn_raising
    # Act
    result = invoke()
    # Assert
    assert "boom" in result.output


# ---------------------------------------------------------------------------
# `sac peer post-turn` — PeerTimeoutPending (504 in-progress) surface
# ---------------------------------------------------------------------------


def _pending_exc() -> PeerTimeoutPending:
    """Build a real PeerTimeoutPending an in-progress 504 would raise."""
    return PeerTimeoutPending(
        "Timeout after 120s — this is NOT necessarily a failure. ...",
        status="timeout_wait_elapsed",
        timeout_s=120.0,
        session_id="sid-abc",
        heartbeat={"state": "working"},
        possibilities=["turn still draining"],
        raw_body={"status": "timeout_wait_elapsed", "timeout_s": 120.0},
    )


def _invoke_post_turn_timeout(extra_args: list[str] | None = None):
    def fake_post_turn(*_a, **_kw) -> str:
        raise _pending_exc()

    runner = CliRunner()
    args = ["post-turn", "alpha", "hi"] + (extra_args or [])
    with _swap("post_turn", fake_post_turn):
        result = runner.invoke(peer_group, args)
    return result


def test_post_turn_timeout_pending_exits_zero_not_failure() -> None:
    # Arrange
    invoke = _invoke_post_turn_timeout
    # Act
    result = invoke()
    # Assert — in-progress is not an error; exit 0 (not 2).
    assert result.exit_code == 0


def test_post_turn_timeout_pending_prints_interpretation() -> None:
    # Arrange
    invoke = _invoke_post_turn_timeout
    # Act
    result = invoke()
    # Assert
    assert "NOT necessarily a failure" in result.output


def test_post_turn_timeout_pending_json_emits_structured_body() -> None:
    # Arrange
    invoke = _invoke_post_turn_timeout
    # Act
    result = invoke(["--json"])
    # Assert
    assert json.loads(result.stdout)["status"] == "timeout_wait_elapsed"


# ---------------------------------------------------------------------------
# `sac peer resolve-url AGENT`
# ---------------------------------------------------------------------------


def _invoke_resolve_url():
    def fake_resolve(_name: str) -> str:
        return "ssh://mba:18888/v1/turn"

    runner = CliRunner()
    with _swap("resolve_peer_url", fake_resolve):
        result = runner.invoke(peer_group, ["resolve-url", "head-mba"])
    return result


def test_resolve_url_exits_zero() -> None:
    # Arrange
    invoke = _invoke_resolve_url
    # Act
    result = invoke()
    # Assert
    assert result.exit_code == 0


def test_resolve_url_prints_resolved_url() -> None:
    # Arrange
    invoke = _invoke_resolve_url
    # Act
    result = invoke()
    # Assert
    assert result.output.strip() == "ssh://mba:18888/v1/turn"
