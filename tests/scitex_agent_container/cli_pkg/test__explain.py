"""Tests for ``sac agents explain`` — the effective-launch-plan renderer.

Covers the two safety-critical behaviours (secret redaction, the
workdir-backing check) plus the unknown-agent error path.
"""

from __future__ import annotations

from contextlib import contextmanager

from scitex_agent_container.cli_pkg import _explain as explain_module
from scitex_agent_container.cli_pkg._explain import (
    _argv_for,
    _channel_lines,
    _delegation_line,
    _pwd_is_backed,
    _redact,
    explain,
)
from scitex_agent_container.cli_pkg._explain_engine import engine_lines
from scitex_agent_container.config import AgentConfig


@contextmanager
def _replace_attribute(target, name, value):
    original = getattr(target, name)
    setattr(target, name, value)
    try:
        yield
    finally:
        setattr(target, name, original)


def test_redact_masks_a_secret_named_value() -> None:
    # Arrange — an env entry whose KEY looks like a secret.
    entry = "SAC_ANTHROPIC_API_KEY=sk-ant-oat01-supersecret"
    # Act
    out = _redact(entry)
    # Assert — the value never appears verbatim.
    assert "sk-ant-oat01-supersecret" not in out


def test_redact_reports_the_secret_length_not_value() -> None:
    # Arrange
    entry = "SAC_LISTEN_BEARER=abcdef"
    # Act
    out = _redact(entry)
    # Assert
    assert out == "SAC_LISTEN_BEARER=<redacted: 6 chars>"


def test_redact_leaves_non_secret_env_untouched() -> None:
    # Arrange — an ordinary, non-secret env entry.
    entry = "CLAUDE_AGENT_ID=proj-scitex-dev"
    # Act
    out = _redact(entry)
    # Assert
    assert out == "CLAUDE_AGENT_ID=proj-scitex-dev"


def test_pwd_is_backed_true_when_under_a_bind_target() -> None:
    # Arrange — workdir nested inside a bound directory.
    binds = [("/home/u", "/home/u", "rw")]
    # Act
    backed = _pwd_is_backed("/home/u/proj/x", binds)
    # Assert
    assert backed is True


def test_pwd_is_backed_false_when_no_bind_covers_it() -> None:
    # Arrange — workdir not under any bind target (no cwd in container).
    binds = [("/data", "/capsule", "ro")]
    # Act
    backed = _pwd_is_backed("/work", binds)
    # Assert
    assert backed is False


def test_delegation_line_exposes_effective_deny_and_bound() -> None:
    # Arrange
    config = AgentConfig(name="worker", harness="hermes", runtime="headless")
    config.lineage.may_spawn = False
    config.delegation.max_concurrent_children = 1
    config.delegation.worktree_isolation = False
    # Act
    line = _delegation_line(config)
    # Assert
    assert all(
        fragment in line
        for fragment in (
            "disabled (delegate_task removed)",
            "max children: 1",
            "Git worktree isolation: off",
        )
    )


def test_a2a_line_separates_configured_auto_from_durable_resolved_port(
) -> None:
    # Arrange
    config = AgentConfig(name="worker", harness="hermes", runtime="tui")
    config.a2a.port = "auto"

    def port_reader(name: str) -> int | None:
        return 19_556 if name == "worker" else None

    # Act
    line = explain_module._a2a_line(config, port_reader=port_reader)

    # Assert
    assert line == (
        "A2A port: configured=auto; resolved=19556; "
        "source=durable_port_claim"
    )


def test_engine_explain_exposes_the_effective_timeout_contract() -> None:
    # Arrange
    config = AgentConfig(name="worker", harness="hermes", runtime="headless")
    config.upstream_deadline_seconds = 1800
    config.client_abandonment_seconds = 1860

    # Act
    rendered = "\n".join(engine_lines(config))

    # Assert
    assert (
        "upstream_deadline_seconds: 1800" in rendered
        and "client_abandonment_seconds: 1860" in rendered
    )


def test_channel_explain_separates_cards_ingress_from_tools() -> None:
    # Arrange: Cards delivery is a declared rail, but no Cards MCP tool is
    # exposed to the model. These are intentionally independent facts.
    config = AgentConfig(name="worker", harness="hermes", runtime="tui")

    # Act
    rendered = "\n".join(_channel_lines(config, ["server:sac", "server:scitex-cards"]))

    # Assert
    assert (
        "cards_inbound_delivery: resolved" in rendered,
        "cards_tools: unavailable" in rendered,
        "CCT is optional" in rendered,
    ) == (True, True, True)


def test_channel_explain_reports_cards_tools_independently() -> None:
    # Arrange
    config = AgentConfig(name="worker", harness="hermes", runtime="tui")
    config.mcp_servers["scitex-cards"] = {"command": "scitex-cards-mcp"}

    # Act
    rendered = "\n".join(_channel_lines(config, ["server:sac", "server:scitex-cards"]))

    # Assert
    assert (
        "cards_inbound_delivery: resolved" in rendered,
        "cards_tools: resolved" in rendered,
    ) == (True, True)


def test_explain_unknown_agent_raises_click_exception() -> None:
    # Arrange
    from click.testing import CliRunner

    runner = CliRunner()
    # Act
    result = runner.invoke(explain, ["definitely-no-such-agent-xyz"])
    # Assert — fail-loud with a hint, not a stack trace.
    assert "no agent named" in result.output


def test_argv_for_uses_the_selected_hermes_runtime(tmp_path) -> None:
    # Arrange
    from scitex_agent_container._lifecycle import _runtime_select

    config = AgentConfig(name="worker", harness="hermes", runtime="headless")
    calls = []

    class SelectedRuntime:
        def resolve_sif(self, value):
            calls.append(("resolve", value))
            return tmp_path / "hermes.sif"

        def _state_dir(self, value):
            calls.append(("state", value))
            return tmp_path / "state"

        def build_run_argv(self, value, *, state_dir, sif_path):
            calls.append(("build", value, state_dir, sif_path))
            return ["apptainer", "exec", str(sif_path), "hermes", "gateway", "run"]

    # Act
    with _replace_attribute(
        _runtime_select, "_get_runtime", lambda value: SelectedRuntime()
    ):
        argv = _argv_for(config)
    # Assert
    assert (
        argv[-3:],
        [call[0] for call in calls],
    ) == (
        ["hermes", "gateway", "run"],
        ["resolve", "state", "build"],
    )
