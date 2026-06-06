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

import json
import os

import pytest

from scitex_agent_container.runtimes._sdk_channels import (
    TelegrammerWakeWiringError,
    apply_channels,
    merge_home_mcp_servers,
    validate_telegrammer_wake_wiring,
)


@pytest.fixture
def home_with_mcp(tmp_path):
    """Point $HOME at a tmp dir and write a real ``.mcp.json`` there.

    Yield-based save/restore of $HOME (PA-306: no monkeypatch). The file
    is real on disk so ``merge_home_mcp_servers`` exercises its actual
    read path, not a stubbed one.
    """
    saved = os.environ.get("HOME")
    os.environ["HOME"] = str(tmp_path)
    (tmp_path / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "claude-code-telegrammer": {
                        "command": "bash",
                        "args": ["-c", "exec bun run /tg/telegram-server.ts"],
                        "env": {"TG_STATE": "/home/agent/.tg-clew"},
                    }
                }
            }
        )
    )
    try:
        yield tmp_path
    finally:
        if saved is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = saved


@pytest.fixture
def home_without_mcp(tmp_path):
    """Point $HOME at a tmp dir with NO ``.mcp.json``."""
    saved = os.environ.get("HOME")
    os.environ["HOME"] = str(tmp_path)
    try:
        yield tmp_path
    finally:
        if saved is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = saved


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


class TestTelegrammerWakeWiring:
    """Concern (c): inject CLAUDE_CODE_TELEGRAMMER_TURN_URL so the agent's own
    telegrammer bot WAKES an idle SDK session (symmetric with server:sac)."""

    def _kwargs_with_telegrammer_mcp(self) -> dict:
        return {
            "mcp_servers": {
                "claude-code-telegrammer": {
                    "type": "stdio",
                    "command": "bash",
                    "args": ["-c", "exec bun run telegram-server.ts"],
                    "env": {"CLAUDE_CODE_TELEGRAMMER_TELEGRAM_AGENT_ID": "clew"},
                }
            }
        }

    def test_turn_url_injected_into_telegrammer_env(self):
        # Arrange
        kwargs = self._kwargs_with_telegrammer_mcp()
        # Act
        apply_channels(kwargs, ["server:claude-code-telegrammer"], 19007, "clew")
        # Assert
        env = kwargs["mcp_servers"]["claude-code-telegrammer"]["env"]
        assert env["CLAUDE_CODE_TELEGRAMMER_TURN_URL"] == (
            "http://127.0.0.1:19007/v1/turn"
        )

    def test_no_turn_url_when_a2a_port_missing(self):
        # Arrange
        kwargs = self._kwargs_with_telegrammer_mcp()
        # Act
        apply_channels(kwargs, ["server:claude-code-telegrammer"], None, "clew")
        # Assert
        env = kwargs["mcp_servers"]["claude-code-telegrammer"]["env"]
        assert "CLAUDE_CODE_TELEGRAMMER_TURN_URL" not in env

    def test_operator_set_turn_url_is_preserved(self):
        # Arrange
        kwargs = self._kwargs_with_telegrammer_mcp()
        kwargs["mcp_servers"]["claude-code-telegrammer"]["env"][
            "CLAUDE_CODE_TELEGRAMMER_TURN_URL"
        ] = "http://operator.example/v1/turn"
        # Act
        apply_channels(kwargs, ["server:claude-code-telegrammer"], 19007, "clew")
        # Assert
        env = kwargs["mcp_servers"]["claude-code-telegrammer"]["env"]
        assert env["CLAUDE_CODE_TELEGRAMMER_TURN_URL"] == (
            "http://operator.example/v1/turn"
        )

    def test_no_injection_without_telegrammer_channel(self):
        # Arrange
        kwargs = self._kwargs_with_telegrammer_mcp()
        # Act — channel not requested, only the MCP entry present
        apply_channels(kwargs, ["server:sac"], 19007, "clew")
        # Assert
        env = kwargs["mcp_servers"]["claude-code-telegrammer"]["env"]
        assert "CLAUDE_CODE_TELEGRAMMER_TURN_URL" not in env

    def test_no_crash_when_telegrammer_mcp_absent(self):
        # Arrange — channel requested but no backing MCP entry merged
        kwargs: dict = {"mcp_servers": {}}
        # Act
        apply_channels(kwargs, ["server:claude-code-telegrammer"], 19007, "clew")
        # Assert
        assert "claude-code-telegrammer" not in kwargs["mcp_servers"]


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


class TestMergeHomeMcpServers:
    """``$HOME/.mcp.json`` is the per-agent MCP delivery path for the SDK.

    The apptainer SDK runner's ``resolve_agent_workspace`` returns ``{}``
    inside the container, and ``setting_sources=[]`` kills the SDK's own
    project-scope ``.mcp.json`` discovery — so this merge is the ONLY way
    a per-agent MCP (e.g. an agent's own telegrammer) reaches the SDK.
    """

    def test_home_mcp_server_is_merged_in(self, home_with_mcp):
        # Arrange
        existing: dict = {}
        # Act
        out = merge_home_mcp_servers(existing)
        # Assert
        assert "claude-code-telegrammer" in out

    def test_merged_entry_gets_default_stdio_type(self, home_with_mcp):
        # Arrange
        existing: dict = {}
        # Act
        out = merge_home_mcp_servers(existing)
        # Assert
        assert out["claude-code-telegrammer"]["type"] == "stdio"

    def test_registry_entry_wins_on_key_collision(self, home_with_mcp):
        # Arrange — same key already present from resolve_agent_workspace.
        existing = {"claude-code-telegrammer": {"command": "registry", "type": "stdio"}}
        # Act
        out = merge_home_mcp_servers(existing)
        # Assert
        assert out["claude-code-telegrammer"]["command"] == "registry"

    def test_missing_home_mcp_file_is_noop(self, home_without_mcp):
        # Arrange
        existing = {"sac": {"command": "sac", "type": "stdio"}}
        # Act
        out = merge_home_mcp_servers(existing)
        # Assert
        assert out == existing

    def test_env_refs_resolved_from_environ(self, home_with_mcp):
        # Arrange — write a ${VAR} ref into the home .mcp.json + set the var.
        (home_with_mcp / ".mcp.json").write_text(
            json.dumps(
                {"mcpServers": {"x": {"command": "c", "env": {"K": "${MY_REF}"}}}}
            )
        )
        saved = os.environ.get("MY_REF")
        os.environ["MY_REF"] = "resolved-value"
        # Act
        try:
            out = merge_home_mcp_servers({})
        finally:
            if saved is None:
                os.environ.pop("MY_REF", None)
            else:
                os.environ["MY_REF"] = saved
        # Assert
        assert out["x"]["env"]["K"] == "resolved-value"


# ---------------------------------------------------------------------------
# Bug #41 hardening — diagnostics for the telegrammer-wake silent-skip paths.
#
# The runner-side ``_wire_telegrammer_wake`` (called from ``apply_channels``)
# now LOGs every silent-skip case so the operator can see exactly which gate
# failed when an idle agent doesn't wake on Telegram. The host-side
# ``validate_telegrammer_wake_wiring`` HARD-FAILS the start when the channel
# is requested but the a2a port is unset — catching the misconfig at
# ``sac agents start`` time instead of after the operator's third
# un-replied Telegram message.
# ---------------------------------------------------------------------------


class TestTelegrammerWakeDiagnostics:
    """``_wire_telegrammer_wake`` must LOG (not silently no-op) on each
    skip path so the operator can see the precise misconfig."""

    def test_a2a_port_missing_logs_warning(self, caplog):
        # Arrange
        kwargs: dict = {"mcp_servers": {}}
        # Act
        with caplog.at_level(
            "WARNING", logger="scitex_agent_container.runtimes._sdk_channels"
        ):
            apply_channels(kwargs, ["server:claude-code-telegrammer"], None, "clew")
        # Assert
        assert any(
            "spec.a2a.port is unset" in rec.getMessage() for rec in caplog.records
        )

    def test_mcp_entry_missing_logs_error(self, caplog):
        # Arrange — channel + a2a port present, but no claude-code-telegrammer
        # entry in mcp_servers.
        kwargs: dict = {"mcp_servers": {"some-other-mcp": {"type": "stdio"}}}
        # Act
        with caplog.at_level(
            "ERROR", logger="scitex_agent_container.runtimes._sdk_channels"
        ):
            apply_channels(kwargs, ["server:claude-code-telegrammer"], 19007, "clew")
        # Assert
        assert any(
            "claude-code-telegrammer" in rec.getMessage()
            and "no MCP entry keyed" in rec.getMessage()
            for rec in caplog.records
        )

    def test_successful_wiring_logs_info(self, caplog):
        # Arrange
        kwargs: dict = {
            "mcp_servers": {
                "claude-code-telegrammer": {
                    "type": "stdio",
                    "command": "bash",
                    "args": ["-c", "exec bun run telegram-server.ts"],
                    "env": {},
                }
            }
        }
        # Act
        with caplog.at_level(
            "INFO", logger="scitex_agent_container.runtimes._sdk_channels"
        ):
            apply_channels(kwargs, ["server:claude-code-telegrammer"], 19007, "clew")
        # Assert
        assert any(
            "telegrammer wake wired" in rec.getMessage() for rec in caplog.records
        )

    def test_no_log_when_channel_not_requested(self, caplog):
        # Arrange — channel set has NO telegrammer entry, so the wake helper
        # must produce NO log lines (a benign no-op, not a misconfig).
        kwargs: dict = {"mcp_servers": {}}
        # Act
        with caplog.at_level(
            "WARNING", logger="scitex_agent_container.runtimes._sdk_channels"
        ):
            apply_channels(kwargs, ["server:sac"], 19007, "clew")
        # Assert
        assert all(
            "telegrammer" not in rec.getMessage().lower() for rec in caplog.records
        )

    def test_operator_set_url_preserved_and_logged(self, caplog):
        # Arrange
        kwargs: dict = {
            "mcp_servers": {
                "claude-code-telegrammer": {
                    "type": "stdio",
                    "command": "bash",
                    "args": ["-c", "exec bun run telegram-server.ts"],
                    "env": {
                        "CLAUDE_CODE_TELEGRAMMER_TURN_URL": "http://operator.example/v1/turn"
                    },
                }
            }
        }
        # Act
        with caplog.at_level(
            "INFO", logger="scitex_agent_container.runtimes._sdk_channels"
        ):
            apply_channels(kwargs, ["server:claude-code-telegrammer"], 19007, "clew")
        # Assert
        assert any("pre-set by operator" in rec.getMessage() for rec in caplog.records)


class TestValidateTelegrammerWakeWiring:
    """Host-side preflight in ``_lifecycle/_start.agent_start``: hard-fail
    the start when the wake wiring provably won't succeed."""

    def test_no_channels_returns_none(self):
        # Arrange — no channels requested at all.
        channels = None
        # Act
        result = validate_telegrammer_wake_wiring(channels, None, agent_name="clew")
        # Assert — returns None (no validation needed).
        assert result is None

    def test_empty_channels_returns_none(self):
        # Arrange — empty channel list.
        channels: list[str] = []
        # Act
        result = validate_telegrammer_wake_wiring(channels, None, agent_name="clew")
        # Assert
        assert result is None

    def test_other_channel_only_returns_none(self):
        # Arrange — server:sac is fine without a2a port for this check.
        channels = ["server:sac"]
        # Act
        result = validate_telegrammer_wake_wiring(channels, None, agent_name="clew")
        # Assert
        assert result is None

    def test_telegrammer_channel_with_port_returns_none(self):
        # Arrange — channel requested and a2a port set: the runtime can wire.
        channels = ["server:claude-code-telegrammer"]
        # Act
        result = validate_telegrammer_wake_wiring(channels, 19007, agent_name="clew")
        # Assert
        assert result is None

    def test_telegrammer_channel_without_port_raises(self):
        # Arrange
        channels = ["server:claude-code-telegrammer"]
        # Act
        # Assert — pytest.raises is the assertion (TQ007: one per test).
        with pytest.raises(TelegrammerWakeWiringError):
            validate_telegrammer_wake_wiring(channels, None, agent_name="clew")

    def test_telegrammer_channel_without_port_message_names_agent(self):
        # Arrange
        channels = ["server:claude-code-telegrammer"]
        # Act
        # Assert — `match` folds the message content into the raises block
        # so the operator-naming contract is checked as part of the same
        # assertion (TQ007 compliant: one assertion per test).
        with pytest.raises(TelegrammerWakeWiringError, match="clew"):
            validate_telegrammer_wake_wiring(channels, None, agent_name="clew")

    def test_telegrammer_channel_without_port_message_names_channel(self):
        # Arrange
        channels = ["server:claude-code-telegrammer"]
        # Act
        # Assert — the raised message must name the offending channel so the
        # operator can spot the mis-spec in their YAML.
        with pytest.raises(
            TelegrammerWakeWiringError, match="server:claude-code-telegrammer"
        ):
            validate_telegrammer_wake_wiring(channels, None, agent_name="clew")

    def test_telegrammer_channel_without_port_message_names_missing_field(self):
        # Arrange
        channels = ["server:claude-code-telegrammer"]
        # Act
        # Assert — the raised message must name the missing field so the
        # operator knows exactly what to set in spec.yaml.
        with pytest.raises(TelegrammerWakeWiringError, match=r"spec\.a2a\.port"):
            validate_telegrammer_wake_wiring(channels, None, agent_name="clew")

    def test_telegrammer_channel_without_port_no_agent_name(self):
        # Arrange — caller may not have an agent name (host-side preflight is
        # called from agent_start which has it; future callers may not).
        channels = ["server:claude-code-telegrammer"]
        # Act
        # Assert — raise still happens without agent_name.
        with pytest.raises(TelegrammerWakeWiringError):
            validate_telegrammer_wake_wiring(channels, None)
