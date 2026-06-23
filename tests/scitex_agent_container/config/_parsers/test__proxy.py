"""Tests for ``parse_proxy`` and the kind+proxy coupling.

Covers the spec.proxy block parser used by the v3 loader for
``kind: AgentProxy`` agents. The kind+block coupling (proxy required
when kind is AgentProxy, claude/startup_* forbidden) is enforced by
the validator and exercised via ``validate_raw`` here too.
"""

from __future__ import annotations

import pytest

from scitex_agent_container.config._parsers._proxy import parse_proxy
from scitex_agent_container.config._proxy_types import ProxySpec
from scitex_agent_container.config._validation import validate_raw

# ---------------------------------------------------------------------------
# parse_proxy — happy path + defaults
# ---------------------------------------------------------------------------


def test_parse_proxy_returns_none_when_kind_is_plain_agent() -> None:
    """spec.proxy is ignored when kind is not AgentProxy."""
    # Arrange
    raw = {"proxy": {"upstream": "https://x"}}
    # Act
    result = parse_proxy(raw, kind="Agent")
    # Assert
    assert result is None


def test_parse_proxy_defaults_returns_proxy_spec_instance() -> None:
    # Arrange
    raw = {"proxy": {"upstream": "https://peer.example.com"}}
    # Act
    out = parse_proxy(raw, kind="AgentProxy")
    # Assert
    assert isinstance(out, ProxySpec)


def test_parse_proxy_defaults_preserves_upstream_value() -> None:
    # Arrange
    raw = {"proxy": {"upstream": "https://peer.example.com"}}
    # Act
    out = parse_proxy(raw, kind="AgentProxy")
    # Assert
    assert out.upstream == "https://peer.example.com"


def test_parse_proxy_defaults_trust_to_untrusted() -> None:
    # Arrange
    raw = {"proxy": {"upstream": "https://peer.example.com"}}
    # Act
    out = parse_proxy(raw, kind="AgentProxy")
    # Assert
    assert out.trust == "untrusted"


def test_parse_proxy_defaults_redact_to_empty_list() -> None:
    # Arrange
    raw = {"proxy": {"upstream": "https://peer.example.com"}}
    # Act
    out = parse_proxy(raw, kind="AgentProxy")
    # Assert
    assert out.redact == []


def test_parse_proxy_defaults_timeout_to_30_seconds() -> None:
    # Arrange
    raw = {"proxy": {"upstream": "https://peer.example.com"}}
    # Act
    out = parse_proxy(raw, kind="AgentProxy")
    # Assert
    assert out.timeout_s == 30.0


_FULL_PROXY_RAW = {
    "proxy": {
        "upstream": "http://peer.local:8080",
        "trust": "local-mesh",
        "redact": ["SECRET_TOKEN", "internal-only"],
        "timeout_s": 12.5,
    }
}


def test_parse_proxy_full_block_preserves_upstream() -> None:
    # Arrange
    raw = _FULL_PROXY_RAW
    # Act
    out = parse_proxy(raw, kind="AgentProxy")
    # Assert
    assert out.upstream == "http://peer.local:8080"


def test_parse_proxy_full_block_preserves_trust() -> None:
    # Arrange
    raw = _FULL_PROXY_RAW
    # Act
    out = parse_proxy(raw, kind="AgentProxy")
    # Assert
    assert out.trust == "local-mesh"


def test_parse_proxy_full_block_preserves_redact_list() -> None:
    # Arrange
    raw = _FULL_PROXY_RAW
    # Act
    out = parse_proxy(raw, kind="AgentProxy")
    # Assert
    assert out.redact == ["SECRET_TOKEN", "internal-only"]


def test_parse_proxy_full_block_preserves_timeout() -> None:
    # Arrange
    raw = _FULL_PROXY_RAW
    # Act
    out = parse_proxy(raw, kind="AgentProxy")
    # Assert
    assert out.timeout_s == 12.5


# ---------------------------------------------------------------------------
# parse_proxy — failure modes
# ---------------------------------------------------------------------------


def test_parse_proxy_missing_block_raises_value_error() -> None:
    # Arrange
    raw: dict = {}
    # Act
    action = lambda: parse_proxy(raw, kind="AgentProxy")
    # Assert
    with pytest.raises(ValueError, match="spec.proxy is required"):
        action()


def test_parse_proxy_non_mapping_block_raises_value_error() -> None:
    # Arrange
    raw = {"proxy": "https://x"}
    # Act
    action = lambda: parse_proxy(raw, kind="AgentProxy")
    # Assert
    with pytest.raises(ValueError, match="must be a mapping"):
        action()


def test_parse_proxy_missing_upstream_raises_value_error() -> None:
    # Arrange
    raw = {"proxy": {"trust": "trusted"}}
    # Act
    action = lambda: parse_proxy(raw, kind="AgentProxy")
    # Assert
    with pytest.raises(ValueError, match="upstream is required"):
        action()


def test_parse_proxy_empty_upstream_raises_value_error() -> None:
    # Arrange
    raw = {"proxy": {"upstream": ""}}
    # Act
    action = lambda: parse_proxy(raw, kind="AgentProxy")
    # Assert
    with pytest.raises(ValueError, match="upstream is required"):
        action()


def test_parse_proxy_non_url_upstream_raises_value_error() -> None:
    # Arrange
    raw = {"proxy": {"upstream": "peer.local:8080"}}
    # Act
    action = lambda: parse_proxy(raw, kind="AgentProxy")
    # Assert
    with pytest.raises(ValueError, match="http://"):
        action()


def test_parse_proxy_bad_trust_value_raises_value_error() -> None:
    # Arrange
    raw = {"proxy": {"upstream": "https://x", "trust": "totally-trusted"}}
    # Act
    action = lambda: parse_proxy(raw, kind="AgentProxy")
    # Assert
    with pytest.raises(ValueError, match="trust"):
        action()


@pytest.mark.parametrize(
    "redact_value",
    [
        pytest.param("secret", id="string-instead-of-list"),
        pytest.param([1, 2], id="list-of-non-strings"),
    ],
)
def test_parse_proxy_invalid_redact_raises_value_error(redact_value) -> None:
    # Arrange
    raw = {"proxy": {"upstream": "https://x", "redact": redact_value}}
    # Act
    action = lambda: parse_proxy(raw, kind="AgentProxy")
    # Assert
    with pytest.raises(ValueError, match="redact"):
        action()


@pytest.mark.parametrize(
    "timeout_value",
    [
        pytest.param("abc", id="non-numeric-string"),
        pytest.param(0, id="zero-not-positive"),
    ],
)
def test_parse_proxy_invalid_timeout_raises_value_error(timeout_value) -> None:
    # Arrange
    raw = {"proxy": {"upstream": "https://x", "timeout_s": timeout_value}}
    # Act
    action = lambda: parse_proxy(raw, kind="AgentProxy")
    # Assert
    with pytest.raises(ValueError, match="timeout"):
        action()


# ---------------------------------------------------------------------------
# validator-level coupling: kind: AgentProxy implies proxy required +
# claude/startup_* forbidden; kind: Agent forbids spec.proxy.
# ---------------------------------------------------------------------------


def _base_proxy_raw(**overrides) -> dict:
    raw = {
        "apiVersion": "scitex-agent-container/v3",
        "kind": "AgentProxy",
        "spec": {
            # Required author fields (no hidden defaults). A proxy has NO
            # claude block (forbidden for kind: AgentProxy); proxy.upstream is
            # its kind-specific required field.
            "runtime": "tui",
            "host": "local",
            "workdir": "/home/agent/work",
            "apptainer": {"image": "/x.sif", "binds": []},
            "health": {"enabled": True, "interval": 60},
            "restart": {"policy": "always", "max_retries": 3},
            "proxy": {"upstream": "https://peer.example.com"},
        },
    }
    raw["spec"].update(overrides)
    return raw


def test_validator_accepts_minimal_agent_proxy_spec() -> None:
    # Arrange
    raw = _base_proxy_raw()
    # Act
    errors = validate_raw(raw, "spec.yaml")
    # Assert
    assert errors == []


def test_validator_rejects_agent_proxy_missing_proxy_block() -> None:
    # Arrange
    raw = {
        "apiVersion": "scitex-agent-container/v3",
        "kind": "AgentProxy",
        "spec": {},
    }
    # Act
    errors = validate_raw(raw, "spec.yaml")
    # Assert
    assert any("spec.proxy is required" in e for e in errors)


def test_validator_rejects_claude_block_on_agent_proxy() -> None:
    # Arrange
    raw = _base_proxy_raw(claude={"model": "sonnet"})
    # Act
    errors = validate_raw(raw, "spec.yaml")
    # Assert
    assert any("spec.claude is not allowed when kind: AgentProxy" in e for e in errors)


def test_validator_rejects_startup_prompts_on_agent_proxy() -> None:
    # Arrange
    raw = _base_proxy_raw(startup_prompts=["hi"])
    # Act
    errors = validate_raw(raw, "spec.yaml")
    # Assert
    assert any(
        "spec.startup_prompts is not allowed when kind: AgentProxy" in e for e in errors
    )


def test_validator_rejects_startup_commands_on_agent_proxy() -> None:
    # Arrange
    raw = _base_proxy_raw(startup_commands=[{"command": "echo hi"}])
    # Act
    errors = validate_raw(raw, "spec.yaml")
    # Assert
    assert any(
        "spec.startup_commands is not allowed when kind: AgentProxy" in e
        for e in errors
    )


def test_validator_rejects_proxy_block_on_kind_agent() -> None:
    # Arrange
    raw = {
        "apiVersion": "scitex-agent-container/v3",
        "kind": "Agent",
        "spec": {"proxy": {"upstream": "https://x"}},
    }
    # Act
    errors = validate_raw(raw, "spec.yaml")
    # Assert
    assert any(
        "spec.proxy is only meaningful when kind: AgentProxy" in e for e in errors
    )


def test_validator_rejects_unknown_kind_value() -> None:
    # Arrange
    raw = {
        "apiVersion": "scitex-agent-container/v3",
        "kind": "SomethingElse",
        "spec": {},
    }
    # Act
    errors = validate_raw(raw, "spec.yaml")
    # Assert
    assert any("kind must be one of" in e for e in errors)
