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


def test_parse_proxy_returns_none_for_kind_agent() -> None:
    """spec.proxy is ignored when kind is not AgentProxy."""
    assert parse_proxy({"proxy": {"upstream": "https://x"}}, kind="Agent") is None


def test_parse_proxy_defaults_when_only_upstream() -> None:
    out = parse_proxy(
        {"proxy": {"upstream": "https://peer.example.com"}}, kind="AgentProxy"
    )
    assert isinstance(out, ProxySpec)
    assert out.upstream == "https://peer.example.com"
    assert out.trust == "untrusted"
    assert out.redact == []
    assert out.timeout_s == 30.0


def test_parse_proxy_full_block() -> None:
    out = parse_proxy(
        {
            "proxy": {
                "upstream": "http://peer.local:8080",
                "trust": "local-mesh",
                "redact": ["SECRET_TOKEN", "internal-only"],
                "timeout_s": 12.5,
            }
        },
        kind="AgentProxy",
    )
    assert out.upstream == "http://peer.local:8080"
    assert out.trust == "local-mesh"
    assert out.redact == ["SECRET_TOKEN", "internal-only"]
    assert out.timeout_s == 12.5


# ---------------------------------------------------------------------------
# parse_proxy — failure modes
# ---------------------------------------------------------------------------


def test_parse_proxy_missing_block_raises() -> None:
    with pytest.raises(ValueError, match="spec.proxy is required"):
        parse_proxy({}, kind="AgentProxy")


def test_parse_proxy_non_mapping_block_raises() -> None:
    with pytest.raises(ValueError, match="must be a mapping"):
        parse_proxy({"proxy": "https://x"}, kind="AgentProxy")


def test_parse_proxy_missing_upstream_raises() -> None:
    with pytest.raises(ValueError, match="upstream is required"):
        parse_proxy({"proxy": {"trust": "trusted"}}, kind="AgentProxy")


def test_parse_proxy_empty_upstream_raises() -> None:
    with pytest.raises(ValueError, match="upstream is required"):
        parse_proxy({"proxy": {"upstream": ""}}, kind="AgentProxy")


def test_parse_proxy_non_url_upstream_raises() -> None:
    with pytest.raises(ValueError, match="http://"):
        parse_proxy({"proxy": {"upstream": "peer.local:8080"}}, kind="AgentProxy")


def test_parse_proxy_bad_trust_raises() -> None:
    with pytest.raises(ValueError, match="trust"):
        parse_proxy(
            {"proxy": {"upstream": "https://x", "trust": "totally-trusted"}},
            kind="AgentProxy",
        )


def test_parse_proxy_redact_must_be_list_of_strings() -> None:
    with pytest.raises(ValueError, match="redact"):
        parse_proxy(
            {"proxy": {"upstream": "https://x", "redact": "secret"}},
            kind="AgentProxy",
        )
    with pytest.raises(ValueError, match="redact"):
        parse_proxy(
            {"proxy": {"upstream": "https://x", "redact": [1, 2]}},
            kind="AgentProxy",
        )


def test_parse_proxy_bad_timeout_raises() -> None:
    with pytest.raises(ValueError, match="timeout"):
        parse_proxy(
            {"proxy": {"upstream": "https://x", "timeout_s": "abc"}},
            kind="AgentProxy",
        )
    with pytest.raises(ValueError, match="timeout"):
        parse_proxy(
            {"proxy": {"upstream": "https://x", "timeout_s": 0}},
            kind="AgentProxy",
        )


# ---------------------------------------------------------------------------
# validator-level coupling: kind: AgentProxy implies proxy required +
# claude/startup_* forbidden; kind: Agent forbids spec.proxy.
# ---------------------------------------------------------------------------


def _base_proxy_raw(**overrides) -> dict:
    raw = {
        "apiVersion": "scitex-agent-container/v3",
        "kind": "AgentProxy",
        "spec": {
            "proxy": {"upstream": "https://peer.example.com"},
        },
    }
    raw["spec"].update(overrides)
    return raw


def test_validator_accepts_minimal_agent_proxy() -> None:
    errors = validate_raw(_base_proxy_raw(), "spec.yaml")
    assert errors == [], errors


def test_validator_rejects_agent_proxy_missing_proxy_block() -> None:
    raw = {
        "apiVersion": "scitex-agent-container/v3",
        "kind": "AgentProxy",
        "spec": {},
    }
    errors = validate_raw(raw, "spec.yaml")
    assert any("spec.proxy is required" in e for e in errors), errors


def test_validator_rejects_claude_block_on_agent_proxy() -> None:
    raw = _base_proxy_raw(claude={"model": "sonnet"})
    errors = validate_raw(raw, "spec.yaml")
    assert any(
        "spec.claude is not allowed when kind: AgentProxy" in e for e in errors
    ), errors


def test_validator_rejects_startup_prompts_on_agent_proxy() -> None:
    raw = _base_proxy_raw(startup_prompts=["hi"])
    errors = validate_raw(raw, "spec.yaml")
    assert any(
        "spec.startup_prompts is not allowed when kind: AgentProxy" in e for e in errors
    ), errors


def test_validator_rejects_startup_commands_on_agent_proxy() -> None:
    raw = _base_proxy_raw(startup_commands=[{"command": "echo hi"}])
    errors = validate_raw(raw, "spec.yaml")
    assert any(
        "spec.startup_commands is not allowed when kind: AgentProxy" in e
        for e in errors
    ), errors


def test_validator_rejects_proxy_block_on_kind_agent() -> None:
    raw = {
        "apiVersion": "scitex-agent-container/v3",
        "kind": "Agent",
        "spec": {"proxy": {"upstream": "https://x"}},
    }
    errors = validate_raw(raw, "spec.yaml")
    assert any(
        "spec.proxy is only meaningful when kind: AgentProxy" in e for e in errors
    ), errors


def test_validator_rejects_unknown_kind() -> None:
    raw = {
        "apiVersion": "scitex-agent-container/v3",
        "kind": "SomethingElse",
        "spec": {},
    }
    errors = validate_raw(raw, "spec.yaml")
    assert any("kind must be one of" in e for e in errors), errors
