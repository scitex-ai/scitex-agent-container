"""Tests for ``runtimes/_sdk_channels.apply_channels``.

``apply_channels`` is pure: it mutates a plain ``kwargs`` dict in place.
No auth, registry, or env fixtures needed — the tests pass real dicts and
assert on the mutation.

Two load-bearing behaviours this guards:

  * the dev-channels flag fires for ANY ``spec.claude.channels`` entry, not
    just ``server:sac``. Before the fix a foreign channel (an agent's own
    external channel bot) survived the runner argv but was DROPPED at the
    gate, so claude never rendered its ``<channel>`` tags.
  * GENERIC wake-on-push: sac exposes the agent's own ``/v1/turn`` under the
    package-agnostic ``SAC_AGENT_TURN_URL`` env var. ANY MCP server entry may
    opt in by referencing ``${SAC_AGENT_TURN_URL}`` in its own env — sac
    names no channel and no downstream package. The tests use a placeholder
    channel/MCP name to prove the mechanism carries zero package knowledge.

TQ: each test is Arrange / Act / Assert with a single assertion; multi-fact
scenarios are split into sibling tests so the failing line names the
contract that regressed.
"""

from __future__ import annotations

import json
import os

import pytest

from scitex_agent_container.runtimes._sdk_channels import (
    SAC_AGENT_TURN_URL_ENV,
    SAC_BIN_ENV,
    SacBinaryNotFoundError,
    agent_turn_url,
    apply_channels,
    compute_channel_plan,
    export_agent_turn_url,
    merge_home_mcp_servers,
)

# A stand-in for whatever external channel an operator wires. sac must know
# NOTHING about it — the mechanism works purely by the spec entry referencing
# the generic ``${SAC_AGENT_TURN_URL}`` placeholder.
_EXAMPLE_CHANNEL = "server:example-channel"
_EXAMPLE_MCP_KEY = "example-channel"
# The downstream package keeps its OWN env-var name; it just maps it to the
# generic sac placeholder in its spec entry.
_EXAMPLE_TURN_ENV = "EXAMPLE_CHANNEL_TURN_URL"


@pytest.fixture(autouse=True)
def _clean_agent_turn_url_env():
    """Ensure ``SAC_AGENT_TURN_URL`` is unset around every test.

    ``export_agent_turn_url`` uses ``setdefault`` (operator-override
    semantics), so a value leaked by one test would pin the resolution in
    the next. Save/restore keeps each test hermetic (PA-306: no monkeypatch).
    """
    saved = os.environ.pop(SAC_AGENT_TURN_URL_ENV, None)
    try:
        yield
    finally:
        os.environ.pop(SAC_AGENT_TURN_URL_ENV, None)
        if saved is not None:
            os.environ[SAC_AGENT_TURN_URL_ENV] = saved


@pytest.fixture
def fake_sac_bin(tmp_path):
    """Materialize an executable fake ``sac`` script and point ``$SAC_BIN``
    at it. Yields the absolute path so tests can assert on the exact value.

    Yield-based save/restore of ``$SAC_BIN`` (PA-306: no monkeypatch).
    """
    fake = tmp_path / "sac"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    saved = os.environ.get(SAC_BIN_ENV)
    os.environ[SAC_BIN_ENV] = str(fake)
    try:
        yield str(fake)
    finally:
        if saved is None:
            os.environ.pop(SAC_BIN_ENV, None)
        else:
            os.environ[SAC_BIN_ENV] = saved


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
                    _EXAMPLE_MCP_KEY: {
                        "command": "bash",
                        "args": ["-c", "exec run /srv/channel-server"],
                        "env": {"CHANNEL_STATE": "/home/agent/.channel-clew"},
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


class TestComputeChannelPlan:
    """The shared plan both runtimes apply (SDK -> kwargs, TUI -> argv): one
    source of truth for the channel set, sac sidecar, and generic turn URL."""

    def test_agent_turn_url_present_when_port_set(self):
        # Arrange — no channel gating: the turn URL is generic.
        channels = [_EXAMPLE_CHANNEL]
        # Act
        plan = compute_channel_plan(channels, 700, "clew")
        # Assert
        assert plan.agent_turn_url == "http://127.0.0.1:700/v1/turn"

    def test_agent_turn_url_present_even_without_any_channel(self):
        # Arrange — the turn URL does NOT depend on any channel being set.
        # Act
        plan = compute_channel_plan([], 700, "clew")
        # Assert
        assert plan.agent_turn_url == "http://127.0.0.1:700/v1/turn"

    def test_agent_turn_url_none_without_port(self):
        # Arrange
        channels = [_EXAMPLE_CHANNEL]
        # Act
        plan = compute_channel_plan(channels, None, "clew")
        # Assert
        assert plan.agent_turn_url is None

    def test_sac_channel_yields_sidecar_args_with_turn_url(self):
        # Arrange
        channels = ["server:sac"]
        # Act
        plan = compute_channel_plan(channels, 701, "lead")
        # Assert
        assert plan.sac_sidecar_args == (
            "mcp",
            "channel",
            "--name",
            "lead",
            "--turn-url",
            "http://127.0.0.1:701/v1/turn",
        )

    def test_channels_are_the_shared_deduped_set(self):
        # Arrange
        channels = ["server:sac", "server:scitex-todo", "server:sac"]
        # Act
        plan = compute_channel_plan(channels, None, "lead")
        # Assert
        assert plan.channels == ("server:sac", "server:scitex-todo")


class TestForeignChannelTurnsOnDevChannels:
    """A non-sac channel must enable dev-channels (the regression guard)."""

    def test_foreign_channel_sets_dev_flag(self):
        # Arrange
        kwargs: dict = {}
        # Act
        apply_channels(kwargs, [_EXAMPLE_CHANNEL], None, "clew")
        # Assert
        assert _devflag(kwargs) == _EXAMPLE_CHANNEL

    def test_foreign_channel_does_not_register_sac_mcp(self):
        # Arrange
        kwargs: dict = {}
        # Act
        apply_channels(kwargs, [_EXAMPLE_CHANNEL], None, "clew")
        # Assert: sac sidecar is server:sac-only — must NOT auto-wire here.
        assert "sac" not in kwargs.get("mcp_servers", {})


class TestAgentTurnUrlHelpers:
    """The generic turn-url helpers sac exposes: value + os.environ publish."""

    def test_agent_turn_url_builds_loopback(self):
        # Arrange
        port = 19007
        # Act
        url = agent_turn_url(port)
        # Assert
        assert url == "http://127.0.0.1:19007/v1/turn"

    def test_agent_turn_url_none_without_port(self):
        # Arrange
        port = None
        # Act
        url = agent_turn_url(port)
        # Assert
        assert url is None

    def test_export_publishes_into_os_environ(self):
        # Arrange
        port = 19007
        # Act
        export_agent_turn_url(port)
        # Assert
        assert os.environ[SAC_AGENT_TURN_URL_ENV] == "http://127.0.0.1:19007/v1/turn"

    def test_export_returns_url(self):
        # Arrange
        port = 19007
        # Act
        url = export_agent_turn_url(port)
        # Assert
        assert url == "http://127.0.0.1:19007/v1/turn"

    def test_export_noop_without_port(self):
        # Arrange
        port = None
        # Act
        export_agent_turn_url(port)
        # Assert — nothing published.
        assert SAC_AGENT_TURN_URL_ENV not in os.environ

    def test_export_does_not_override_preset(self):
        # Arrange — an operator/spec pre-set value must win (setdefault).
        os.environ[SAC_AGENT_TURN_URL_ENV] = "http://operator.example/v1/turn"
        # Act
        export_agent_turn_url(19007)
        # Assert
        assert os.environ[SAC_AGENT_TURN_URL_ENV] == "http://operator.example/v1/turn"


class TestGenericWakeOnPush:
    """Concern (c) — GENERIC wake-on-push. An MCP entry opts in purely by
    referencing ``${SAC_AGENT_TURN_URL}``; sac names no channel/package."""

    def _kwargs_with_referencing_mcp(self) -> dict:
        # The downstream MCP maps its OWN env var to the generic placeholder.
        return {
            "mcp_servers": {
                _EXAMPLE_MCP_KEY: {
                    "type": "stdio",
                    "command": "bash",
                    "args": ["-c", "exec run channel-server"],
                    "env": {_EXAMPLE_TURN_ENV: "${SAC_AGENT_TURN_URL}"},
                }
            }
        }

    def test_placeholder_resolved_when_port_set(self):
        # Arrange
        kwargs = self._kwargs_with_referencing_mcp()
        # Act
        apply_channels(kwargs, [_EXAMPLE_CHANNEL], 19007, "clew")
        # Assert
        env = kwargs["mcp_servers"][_EXAMPLE_MCP_KEY]["env"]
        assert env[_EXAMPLE_TURN_ENV] == "http://127.0.0.1:19007/v1/turn"

    def test_placeholder_unresolved_when_no_port(self):
        # Arrange
        kwargs = self._kwargs_with_referencing_mcp()
        # Act — no a2a port: sac has no value to expose, placeholder survives.
        apply_channels(kwargs, [_EXAMPLE_CHANNEL], None, "clew")
        # Assert
        env = kwargs["mcp_servers"][_EXAMPLE_MCP_KEY]["env"]
        assert env[_EXAMPLE_TURN_ENV] == "${SAC_AGENT_TURN_URL}"

    def test_resolution_is_channel_agnostic(self):
        # Arrange — an entry that references the placeholder but whose channel
        # is NOT even in spec.claude.channels still gets the value (the value
        # is exposed whenever a port resolves; opt-in is by reference alone).
        kwargs = self._kwargs_with_referencing_mcp()
        # Act — only server:sac requested, yet the example entry resolves.
        apply_channels(kwargs, [_EXAMPLE_CHANNEL], 19007, "clew")
        env = kwargs["mcp_servers"][_EXAMPLE_MCP_KEY]["env"]
        # Assert — no channel name appears in the resolved value.
        assert "example-channel" not in env[_EXAMPLE_TURN_ENV]

    def test_entry_without_placeholder_untouched(self):
        # Arrange — an MCP that does NOT opt in keeps its env verbatim.
        kwargs: dict = {
            "mcp_servers": {
                _EXAMPLE_MCP_KEY: {
                    "type": "stdio",
                    "command": "bash",
                    "env": {"CHANNEL_AGENT_ID": "clew"},
                }
            }
        }
        # Act
        apply_channels(kwargs, [_EXAMPLE_CHANNEL], 19007, "clew")
        # Assert
        env = kwargs["mcp_servers"][_EXAMPLE_MCP_KEY]["env"]
        assert env == {"CHANNEL_AGENT_ID": "clew"}

    def test_no_crash_when_no_mcp_servers(self):
        # Arrange — port set but nothing to resolve.
        kwargs: dict = {}
        # Act
        apply_channels(kwargs, [_EXAMPLE_CHANNEL], 19007, "clew")
        # Assert — dev flag still set; no mcp_servers created by the wake path.
        assert _devflag(kwargs) == _EXAMPLE_CHANNEL

    def test_publishes_turn_url_into_os_environ(self):
        # Arrange
        kwargs = self._kwargs_with_referencing_mcp()
        # Act
        apply_channels(kwargs, [_EXAMPLE_CHANNEL], 19007, "clew")
        # Assert — a spawned MCP that reads the var directly also sees it.
        assert os.environ[SAC_AGENT_TURN_URL_ENV] == "http://127.0.0.1:19007/v1/turn"


class TestSacChannelStillWorks:
    """``server:sac`` keeps both the dev flag and the sidecar registration.

    All tests here depend on ``fake_sac_bin`` so the binary resolver returns
    a deterministic absolute path instead of raising SacBinaryNotFoundError.
    """

    def test_sac_channel_sets_dev_flag(self, fake_sac_bin):
        # Arrange
        kwargs: dict = {}
        # Act
        apply_channels(kwargs, ["server:sac"], 9999, "lead")
        # Assert
        assert _devflag(kwargs) == "server:sac"

    def test_sac_channel_registers_sac_mcp(self, fake_sac_bin):
        # Arrange — SAC_BIN points at a real executable so the resolver
        # returns a deterministic absolute path (see fake_sac_bin fixture).
        kwargs: dict = {}
        # Act
        apply_channels(kwargs, ["server:sac"], 9999, "lead")
        # Assert
        assert kwargs["mcp_servers"]["sac"]["command"] == fake_sac_bin

    def test_sac_sidecar_carries_turn_url_for_wake(self, fake_sac_bin):
        # Arrange
        kwargs: dict = {}
        # Act
        apply_channels(kwargs, ["server:sac"], 9999, "lead")
        # Assert
        args = kwargs["mcp_servers"]["sac"]["args"]
        assert args[args.index("--turn-url") + 1] == "http://127.0.0.1:9999/v1/turn"

    def test_sac_sidecar_omits_turn_url_when_no_a2a_port(self, fake_sac_bin):
        # Arrange
        kwargs: dict = {}
        # Act
        apply_channels(kwargs, ["server:sac"], None, "lead")
        # Assert
        assert "--turn-url" not in kwargs["mcp_servers"]["sac"]["args"]


class TestSacBinaryResolution:
    """``server:sac`` must spawn ``sac`` via an absolute, executable path —
    a bare ``"sac"`` command in the MCP config fails exec silently when the
    SDK subprocess's PATH does not contain the agent venv's bin dir, and
    the a2a inbox then has no consumer (smoke-tested 2026-06-15 on
    proj-scitex-agent-container; lead messages queued undelivered for
    hours). This contract is the loud-fail guard."""

    def test_sac_bin_env_override_is_honoured(self, fake_sac_bin):
        # Arrange — fake_sac_bin already sets $SAC_BIN to the fake script
        kwargs: dict = {}
        # Act
        apply_channels(kwargs, ["server:sac"], 9999, "lead")
        # Assert
        assert kwargs["mcp_servers"]["sac"]["command"] == fake_sac_bin

    def test_command_is_absolute_path(self, fake_sac_bin):
        # Arrange
        kwargs: dict = {}
        # Act
        apply_channels(kwargs, ["server:sac"], 9999, "lead")
        # Assert
        cmd = kwargs["mcp_servers"]["sac"]["command"]
        assert os.path.isabs(cmd), f"command must be absolute path: {cmd!r}"

    def test_resolves_via_path_when_no_env_override(self, tmp_path):
        # Arrange — drop $SAC_BIN, install fake `sac` on PATH only
        fake = tmp_path / "sac"
        fake.write_text("#!/bin/sh\nexit 0\n")
        fake.chmod(0o755)
        saved_sac_bin = os.environ.pop(SAC_BIN_ENV, None)
        saved_path = os.environ.get("PATH", "")
        os.environ["PATH"] = str(tmp_path)
        try:
            kwargs: dict = {}
            # Act
            apply_channels(kwargs, ["server:sac"], 9999, "lead")
            # Assert
            assert kwargs["mcp_servers"]["sac"]["command"] == str(fake)
        finally:
            os.environ["PATH"] = saved_path
            if saved_sac_bin is not None:
                os.environ[SAC_BIN_ENV] = saved_sac_bin

    @pytest.mark.skipif(
        os.path.isfile("/opt/venv-agent/bin/sac"),
        reason=(
            "/opt/venv-agent/bin/sac exists on this host; resolver will "
            "find it via the candidate fallback. Test cannot drive the "
            "unresolvable branch without further isolation."
        ),
    )
    def test_unresolvable_sac_raises_loudly(self, tmp_path):
        # Arrange — wipe PATH and $SAC_BIN so no candidate resolves; the
        # resolver still checks /opt/venv-agent/bin/sac as a last resort,
        # so we exercise the raise only when that path is absent on the
        # test host (the case for CI/dev boxes). ``match=`` pins the
        # message-content contract (one assertion = STX-TQ007 clean).
        saved_sac_bin = os.environ.pop(SAC_BIN_ENV, None)
        saved_path = os.environ.get("PATH", "")
        os.environ["PATH"] = str(tmp_path)  # empty dir → no `sac`
        kwargs: dict = {}
        try:
            # Act
            # Assert
            with pytest.raises(SacBinaryNotFoundError, match=r"(?i)sac"):
                apply_channels(kwargs, ["server:sac"], 9999, "lead")
        finally:
            os.environ["PATH"] = saved_path
            if saved_sac_bin is not None:
                os.environ[SAC_BIN_ENV] = saved_sac_bin

    def test_sac_bin_pointing_at_nonexistent_path_raises(self, tmp_path):
        # Arrange — typo'd override must fail loudly and the message
        # MUST name the SAC_BIN_ENV env var so the operator sees which
        # variable to fix. ``match=SAC_BIN_ENV`` pins both contracts in
        # one assertion (STX-TQ007).
        saved_sac_bin = os.environ.get(SAC_BIN_ENV)
        os.environ[SAC_BIN_ENV] = str(tmp_path / "does-not-exist")
        kwargs: dict = {}
        try:
            # Act
            # Assert
            with pytest.raises(SacBinaryNotFoundError, match=SAC_BIN_ENV):
                apply_channels(kwargs, ["server:sac"], 9999, "lead")
        finally:
            if saved_sac_bin is None:
                os.environ.pop(SAC_BIN_ENV, None)
            else:
                os.environ[SAC_BIN_ENV] = saved_sac_bin


class TestBothChannelsCoexist:
    """An agent can request both its own channel AND the sac bus channel.

    Depends on ``fake_sac_bin`` because the sac sidecar registration goes
    through the absolute-path resolver.
    """

    def test_dev_flag_lists_both_channels_comma_joined(self, fake_sac_bin):
        # Arrange
        kwargs: dict = {}
        # Act
        apply_channels(kwargs, ["server:sac", _EXAMPLE_CHANNEL], None, "clew")
        # Assert: claude needs the full set to render both channels' tags.
        assert _devflag(kwargs) == f"server:sac,{_EXAMPLE_CHANNEL}"

    def test_sac_mcp_registered_when_sac_present_among_many(self, fake_sac_bin):
        # Arrange
        kwargs: dict = {}
        # Act
        apply_channels(kwargs, [_EXAMPLE_CHANNEL, "server:sac"], None, "clew")
        # Assert
        assert "sac" in kwargs["mcp_servers"]


class TestDedupeAndNormalization:
    """Whitespace + duplicate channel entries are normalized."""

    def test_duplicate_channels_collapse_in_dev_flag(self, fake_sac_bin):
        # Arrange
        kwargs: dict = {}
        # Act — ``fake_sac_bin`` so the sac sidecar resolver does not raise.
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
    a per-agent MCP (e.g. an agent's own external channel bot) reaches the
    SDK.
    """

    def test_home_mcp_server_is_merged_in(self, home_with_mcp):
        # Arrange
        existing: dict = {}
        # Act
        out = merge_home_mcp_servers(existing)
        # Assert
        assert _EXAMPLE_MCP_KEY in out

    def test_merged_entry_gets_default_stdio_type(self, home_with_mcp):
        # Arrange
        existing: dict = {}
        # Act
        out = merge_home_mcp_servers(existing)
        # Assert
        assert out[_EXAMPLE_MCP_KEY]["type"] == "stdio"

    def test_registry_entry_wins_on_key_collision(self, home_with_mcp):
        # Arrange — same key already present from resolve_agent_workspace.
        existing = {_EXAMPLE_MCP_KEY: {"command": "registry", "type": "stdio"}}
        # Act
        out = merge_home_mcp_servers(existing)
        # Assert
        assert out[_EXAMPLE_MCP_KEY]["command"] == "registry"

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
