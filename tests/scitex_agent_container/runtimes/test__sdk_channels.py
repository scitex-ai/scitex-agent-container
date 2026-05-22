"""Tests for ``runtimes/_sdk_channels.apply_channels``.

``apply_channels`` is pure: it mutates a plain ``kwargs`` dict in place.
No auth, registry, or env fixtures needed — the tests pass real dicts and
assert on the mutation.

The load-bearing behaviour this guards (the Task A generalization): the
``--dangerously-load-development-channels`` flag fires for ANY
``spec.claude.channels`` entry, not just ``server:sac``. Before the fix a
foreign channel (e.g. ``server:claude-code-telegrammer`` for an agent's own
telegrammer bot) survived the runner argv but was DROPPED at the gate, so
claude never rendered its ``<channel>`` tags and the notifications were
silently ignored.

TQ: each test is Arrange / Act / Assert with a single assertion; multi-fact
scenarios are split into sibling tests so the failing line names the
contract that regressed.
"""

from __future__ import annotations

from scitex_agent_container.runtimes._sdk_channels import apply_channels


def _devflag(kwargs: dict) -> str | None:
    """Return the dev-channels flag value, or None if unset."""
    return kwargs.get("extra_args", {}).get("dangerously-load-development-channels")


class TestForeignChannelTurnsOnDevChannels:
    """A non-sac channel must enable dev-channels (the regression guard)."""

    def test_telegrammer_channel_sets_dev_flag(self):
        # Arrange
        kwargs: dict = {}
        # Act
        apply_channels(kwargs, ["server:claude-code-telegrammer"], None, "clew")
        # Assert
        assert _devflag(kwargs) == "server:claude-code-telegrammer"

    def test_telegrammer_channel_does_not_register_sac_mcp(self):
        # Arrange
        kwargs: dict = {}
        # Act
        apply_channels(kwargs, ["server:claude-code-telegrammer"], None, "clew")
        # Assert: sac sidecar is server:sac-only — must NOT auto-wire here.
        assert "sac" not in kwargs.get("mcp_servers", {})


class TestSacChannelStillWorks:
    """``server:sac`` keeps both the dev flag and the sidecar registration."""

    def test_sac_channel_sets_dev_flag(self):
        # Arrange
        kwargs: dict = {}
        # Act
        apply_channels(kwargs, ["server:sac"], 9999, "lead")
        # Assert
        assert _devflag(kwargs) == "server:sac"

    def test_sac_channel_registers_sac_mcp(self):
        # Arrange
        kwargs: dict = {}
        # Act
        apply_channels(kwargs, ["server:sac"], 9999, "lead")
        # Assert
        assert kwargs["mcp_servers"]["sac"]["command"] == "sac"

    def test_sac_sidecar_carries_turn_url_for_wake(self):
        # Arrange
        kwargs: dict = {}
        # Act
        apply_channels(kwargs, ["server:sac"], 9999, "lead")
        # Assert
        args = kwargs["mcp_servers"]["sac"]["args"]
        assert args[args.index("--turn-url") + 1] == "http://127.0.0.1:9999/v1/turn"

    def test_sac_sidecar_omits_turn_url_when_no_a2a_port(self):
        # Arrange
        kwargs: dict = {}
        # Act
        apply_channels(kwargs, ["server:sac"], None, "lead")
        # Assert
        assert "--turn-url" not in kwargs["mcp_servers"]["sac"]["args"]


class TestBothChannelsCoexist:
    """An agent can request both its own channel AND the sac bus channel."""

    def test_dev_flag_lists_both_channels_comma_joined(self):
        # Arrange
        kwargs: dict = {}
        # Act
        apply_channels(
            kwargs, ["server:sac", "server:claude-code-telegrammer"], None, "clew"
        )
        # Assert: claude needs the full set to render both channels' tags.
        assert _devflag(kwargs) == "server:sac,server:claude-code-telegrammer"

    def test_sac_mcp_registered_when_sac_present_among_many(self):
        # Arrange
        kwargs: dict = {}
        # Act
        apply_channels(
            kwargs, ["server:claude-code-telegrammer", "server:sac"], None, "clew"
        )
        # Assert
        assert "sac" in kwargs["mcp_servers"]


class TestDedupeAndNormalization:
    """Whitespace + duplicate channel entries are normalized."""

    def test_duplicate_channels_collapse_in_dev_flag(self):
        # Arrange
        kwargs: dict = {}
        # Act
        apply_channels(kwargs, ["server:sac", " server:sac "], None, "lead")
        # Assert
        assert _devflag(kwargs) == "server:sac"


class TestNoChannels:
    """No channels → no dev flag, no sidecar (the common case)."""

    def test_empty_list_leaves_dev_flag_unset(self):
        # Arrange
        kwargs: dict = {}
        # Act
        apply_channels(kwargs, [], None, "lead")
        # Assert
        assert _devflag(kwargs) is None

    def test_none_leaves_mcp_servers_untouched(self):
        # Arrange
        kwargs: dict = {}
        # Act
        apply_channels(kwargs, None, None, "lead")
        # Assert
        assert "mcp_servers" not in kwargs
