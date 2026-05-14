"""CLI tests for the ``sac peer`` noun-group.

PA-306: no `unittest.mock`. The CLI's collaborators are swapped via
hand-rolled fake callables installed on the module's namespace and
restored on teardown — same effect as `monkeypatch` without the mock
library or the banned fixture parameter.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Callable, Iterator

from click.testing import CliRunner

import scitex_agent_container._network.peer as peer_mod
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


class TestPeerGroup:
    def test_group_lists_two_verbs(self) -> None:
        """`sac peer --help` shows post-turn + resolve-url under the group."""
        runner = CliRunner()
        result = runner.invoke(peer_group, ["--help"])
        assert result.exit_code == 0
        assert "post-turn" in result.output
        assert "resolve-url" in result.output

    def test_post_turn_invokes_post_turn(self) -> None:
        """`sac peer post-turn AGENT TEXT` calls peer.post_turn and echoes
        the reply."""
        captured: dict = {}

        def fake_post_turn(name: str, text: str, **_kw) -> str:
            captured["call"] = (name, text)
            return "echo:hi"

        runner = CliRunner()
        with _swap("post_turn", fake_post_turn):
            result = runner.invoke(peer_group, ["post-turn", "alpha", "hi"])
        assert result.exit_code == 0
        assert result.output.strip() == "echo:hi"
        assert captured["call"] == ("alpha", "hi")

    def test_post_turn_json_envelope(self) -> None:
        """`--json` emits the full envelope, not just the reply."""
        import json

        def fake_post_turn(*_a, **_kw) -> str:
            return "ok"

        runner = CliRunner()
        with _swap("post_turn", fake_post_turn):
            result = runner.invoke(peer_group, ["post-turn", "alpha", "hi", "--json"])
        assert result.exit_code == 0
        body = json.loads(result.output)
        assert body == {"reply": "ok", "exit_after": False}

    def test_post_turn_peer_error_exits_2(self) -> None:
        """PeerError surfaces as exit code 2 + error message."""
        from scitex_agent_container._network.peer import PeerError

        def fake_post_turn(*_a, **_kw) -> str:
            raise PeerError("boom")

        runner = CliRunner()
        with _swap("post_turn", fake_post_turn):
            result = runner.invoke(peer_group, ["post-turn", "alpha", "hi"])
        assert result.exit_code == 2
        # Click 8.2+ merges stderr into output when mix_stderr unavailable.
        assert "boom" in result.output

    def test_resolve_url_prints_url(self) -> None:
        def fake_resolve(_name: str) -> str:
            return "ssh://mba:18888/v1/turn"

        runner = CliRunner()
        with _swap("resolve_peer_url", fake_resolve):
            result = runner.invoke(peer_group, ["resolve-url", "head-mba"])
        assert result.exit_code == 0
        assert result.output.strip() == "ssh://mba:18888/v1/turn"
