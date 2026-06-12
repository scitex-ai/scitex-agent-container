"""Tests for the pull-side self-peer discovery (TG 12706 / TG 12633 follow-up).

The ``sac mcp channel`` subcommand should be invokable WITHOUT
``--name`` from any directory containing (or nested under) a
``.scitex/agent-container/agents/self/spec.yaml``. The discovery
walks the cwd upward for the first hit, gates the YAML through
:func:`_listen._self_peers.is_self_peer_spec` (predicate parity
with the listen-side discovery), then resolves the name via the
listen-side ``_resolve_runtime_self_identity`` so the channel and
the listen agree on "who am I".

ONE generic shape — cwd-walk for ``.scitex/agent-container/agents/
self/spec.yaml``. No per-node home-scope fallback (operator
rejected node-specific exceptions per a2a a8580f78125f44b1ad89442794ad3dce).

Test style (STX-TQ002 / TQ007): explicit ``# Arrange`` / ``# Act`` /
``# Assert`` markers in order; one assertion per test.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from scitex_agent_container._mcp._channel_self_peer_discovery import (
    DiscoveredSelfIdentity,
    discover_self_identity,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_SPEC_REL = Path(".scitex/agent-container/agents/self/spec.yaml")


def _write_self_spec(root: Path, body: str) -> Path:
    """Helper — drop a self/spec.yaml under ``root`` and return the path."""
    target = root / _SPEC_REL
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body)
    return target


# ---------------------------------------------------------------------------
# discover_self_identity — happy path
# ---------------------------------------------------------------------------


def test_discover_self_identity_returns_none_when_no_spec_found(tmp_path: Path):
    # Arrange: empty tree, no spec anywhere upward.
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    # Act
    result = discover_self_identity(start=nested)
    # Assert
    assert result is None


def test_discover_self_identity_finds_spec_in_start_directory(tmp_path: Path):
    # Arrange
    spec_path = _write_self_spec(tmp_path, "listen_url: http://127.0.0.1:7878\n")
    # Act
    result = discover_self_identity(start=tmp_path, self_identity="capsule-7")
    # Assert
    assert result is not None and result.spec_path == spec_path


def test_discover_self_identity_walks_upward_from_nested_start(tmp_path: Path):
    # Arrange: spec at root, start deep below.
    spec_path = _write_self_spec(tmp_path, "listen_url: http://127.0.0.1:7878\n")
    nested = tmp_path / "deep" / "nested" / "dir"
    nested.mkdir(parents=True)
    # Act
    result = discover_self_identity(start=nested, self_identity="lead")
    # Assert
    assert result is not None and result.spec_path == spec_path


def test_discover_self_identity_uses_explicit_self_identity_verbatim(
    tmp_path: Path,
):
    # Arrange
    _write_self_spec(tmp_path, "listen_url: http://127.0.0.1:7878\n")
    # Act
    result = discover_self_identity(start=tmp_path, self_identity="my-runtime-name")
    # Assert
    assert result is not None and result.name == "my-runtime-name"


def test_discover_self_identity_returns_listen_url_from_spec(tmp_path: Path):
    # Arrange
    _write_self_spec(tmp_path, "listen_url: http://10.0.0.1:9999\n")
    # Act
    result = discover_self_identity(start=tmp_path, self_identity="x")
    # Assert
    assert result is not None and result.listen_url == "http://10.0.0.1:9999"


def test_discover_self_identity_returns_description_when_present(tmp_path: Path):
    # Arrange
    body = "listen_url: http://127.0.0.1:7878\ndescription: my runtime self\n"
    _write_self_spec(tmp_path, body)
    # Act
    result = discover_self_identity(start=tmp_path, self_identity="x")
    # Assert
    assert result is not None and result.description == "my runtime self"


def test_discover_self_identity_description_is_none_when_absent(tmp_path: Path):
    # Arrange
    _write_self_spec(tmp_path, "listen_url: http://127.0.0.1:7878\n")
    # Act
    result = discover_self_identity(start=tmp_path, self_identity="x")
    # Assert
    assert result is not None and result.description is None


def test_discover_self_identity_returns_dataclass_instance(tmp_path: Path):
    # Arrange
    _write_self_spec(tmp_path, "listen_url: http://127.0.0.1:7878\n")
    # Act
    result = discover_self_identity(start=tmp_path, self_identity="x")
    # Assert
    assert isinstance(result, DiscoveredSelfIdentity)


# ---------------------------------------------------------------------------
# discover_self_identity — predicate parity gate
# ---------------------------------------------------------------------------


def test_discover_self_identity_rejects_container_agent_shape(tmp_path: Path):
    # Arrange: spec carries ``apiVersion`` → predicate rejects.
    body = "apiVersion: scitex-agent-container/v3\nlisten_url: http://127.0.0.1:7878\n"
    _write_self_spec(tmp_path, body)
    # Act
    result = discover_self_identity(start=tmp_path, self_identity="x")
    # Assert
    assert result is None


def test_discover_self_identity_rejects_spec_key_container_shape(tmp_path: Path):
    # Arrange
    body = "spec:\n  runtime: apptainer\nlisten_url: http://127.0.0.1:7878\n"
    _write_self_spec(tmp_path, body)
    # Act
    result = discover_self_identity(start=tmp_path, self_identity="x")
    # Assert
    assert result is None


def test_discover_self_identity_rejects_blob_without_listen_url(tmp_path: Path):
    # Arrange
    _write_self_spec(tmp_path, "description: missing listen_url\n")
    # Act
    result = discover_self_identity(start=tmp_path, self_identity="x")
    # Assert
    assert result is None


# ---------------------------------------------------------------------------
# discover_self_identity — failure modes (never raise)
# ---------------------------------------------------------------------------


def test_discover_self_identity_returns_none_on_yaml_parse_error(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    # Arrange
    _write_self_spec(tmp_path, "not: [valid: yaml:\n")
    caplog.set_level(logging.WARNING)
    # Act
    result = discover_self_identity(start=tmp_path, self_identity="x")
    # Assert
    assert result is None


# ---------------------------------------------------------------------------
# discover_self_identity — name resolution fallback chain
# ---------------------------------------------------------------------------


def test_discover_self_identity_resolves_runtime_identity_when_no_explicit_name(
    tmp_path: Path,
):
    # Arrange: no explicit self_identity → injected resolver wins.
    _write_self_spec(tmp_path, "listen_url: http://127.0.0.1:7878\n")
    # Act
    result = discover_self_identity(
        start=tmp_path,
        runtime_resolver=lambda: "resolved-runtime",
    )
    # Assert
    assert result is not None and result.name == "resolved-runtime"


def test_discover_self_identity_falls_back_to_literal_self_when_resolver_returns_none(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
):
    # Arrange: no explicit name, injected resolver returns None.
    _write_self_spec(tmp_path, "listen_url: http://127.0.0.1:7878\n")
    caplog.set_level(logging.WARNING)
    # Act
    result = discover_self_identity(
        start=tmp_path,
        runtime_resolver=lambda: None,
    )
    # Assert
    assert result is not None and result.name == "self"


def test_discover_self_identity_literal_self_fallback_logs_warning(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
):
    # Arrange
    _write_self_spec(tmp_path, "listen_url: http://127.0.0.1:7878\n")
    caplog.set_level(logging.WARNING)
    # Act
    discover_self_identity(start=tmp_path, runtime_resolver=lambda: None)
    # Assert: at least one WARNING was logged.
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) >= 1


# ---------------------------------------------------------------------------
# DiscoveredSelfIdentity dataclass surface
# ---------------------------------------------------------------------------


def test_discovered_self_identity_has_required_fields():
    # Arrange
    name = "x"
    listen_url = "http://h:1"
    spec_path = Path("/tmp/spec.yaml")
    description = "d"
    # Act
    obj = DiscoveredSelfIdentity(
        name=name,
        listen_url=listen_url,
        spec_path=spec_path,
        description=description,
    )
    # Assert
    assert (
        obj.name == name
        and obj.listen_url == listen_url
        and obj.spec_path == spec_path
        and obj.description == description
    )


def test_discovered_self_identity_description_allows_none():
    # Arrange
    kwargs = dict(
        name="x",
        listen_url="http://h:1",
        spec_path=Path("/tmp/spec.yaml"),
        description=None,
    )
    # Act
    obj = DiscoveredSelfIdentity(**kwargs)
    # Assert
    assert obj.description is None


def test_discovered_self_identity_is_frozen():
    # Arrange
    obj = DiscoveredSelfIdentity(
        name="x",
        listen_url="http://h:1",
        spec_path=Path("/tmp/spec.yaml"),
        description="d",
    )
    raised: Exception | None = None
    # Act: attempt to mutate a frozen-dataclass field.
    try:
        obj.name = "mutated"  # type: ignore[misc]
    except Exception as exc:  # stx-allow: test-capture (reason: STX-TQ002 splits Act from Assert; frozen mutation is the Act, capturing the exc lets the Assert check it.)
        raised = exc
    # Assert
    assert raised is not None
