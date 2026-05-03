"""CLI tests for the ``sac peer`` noun-group."""

from __future__ import annotations

from unittest.mock import patch

from click.testing import CliRunner

from scitex_agent_container.cli_pkg.peer_cmds import peer_group


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
        runner = CliRunner()
        with patch(
            "scitex_agent_container.peer.post_turn", return_value="echo:hi"
        ) as fake:
            result = runner.invoke(peer_group, ["post-turn", "alpha", "hi"])
        assert result.exit_code == 0
        assert result.output.strip() == "echo:hi"
        assert fake.call_args.args == ("alpha", "hi")

    def test_post_turn_json_envelope(self) -> None:
        """`--json` emits the full envelope, not just the reply."""
        import json

        runner = CliRunner()
        with patch("scitex_agent_container.peer.post_turn", return_value="ok"):
            result = runner.invoke(peer_group, ["post-turn", "alpha", "hi", "--json"])
        assert result.exit_code == 0
        body = json.loads(result.output)
        assert body == {"reply": "ok", "exit_after": False}

    def test_post_turn_peer_error_exits_2(self) -> None:
        """PeerError surfaces as exit code 2 + error message."""
        from scitex_agent_container.peer import PeerError

        runner = CliRunner()
        with patch(
            "scitex_agent_container.peer.post_turn",
            side_effect=PeerError("boom"),
        ):
            result = runner.invoke(peer_group, ["post-turn", "alpha", "hi"])
        assert result.exit_code == 2
        # Click 8.2+ merges stderr into output when mix_stderr unavailable.
        assert "boom" in result.output

    def test_resolve_url_prints_url(self) -> None:
        runner = CliRunner()
        with patch(
            "scitex_agent_container.peer.resolve_peer_url",
            return_value="ssh://mba:18888/v1/turn",
        ):
            result = runner.invoke(peer_group, ["resolve-url", "head-mba"])
        assert result.exit_code == 0
        assert result.output.strip() == "ssh://mba:18888/v1/turn"
