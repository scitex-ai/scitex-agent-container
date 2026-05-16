"""Tests for the pure ``_resolve_dispatch_peer`` helper.

Carved into its own file (rather than appended to ``test__common.py``)
because this helper belongs to the upcoming cross-host dispatch path
and stays easier to evolve in isolation. Covers the documented
behaviour table plus case-sensitivity and whitespace edge cases.

The helper is a pure resolver — it never logs, never reads files,
never raises. These tests assert exactly that surface.
"""

from __future__ import annotations

from scitex_agent_container._state.host_config import PeerSpec
from scitex_agent_container.cli_pkg.lifecycle._common import (
    _resolve_dispatch_peer,
)


def _peers_with_spartan() -> dict[str, PeerSpec]:
    """Build a peer registry containing only ``spartan-bm152``.

    The single-peer shape is deliberate — the resolver doesn't care
    about siblings, and keeping the fixture minimal makes the
    behaviour-table assertions read straight off the test name.
    """
    return {
        "spartan-bm152": PeerSpec(name="spartan-bm152", ssh="spartan-bm152"),
    }


# ---------------------------------------------------------------------------
# Behaviour table — five canonical rows from the helper's docstring
# ---------------------------------------------------------------------------


def test_target_none_returns_none_for_local_execution():
    # Arrange
    peers = _peers_with_spartan()
    # Act
    out = _resolve_dispatch_peer(None, "ywata-note-win", peers)
    # Assert
    assert out is None


def test_target_matches_current_host_returns_none_when_peer_exists():
    # Arrange — peer registry knows about the current host too.
    peers = {
        "ywata-note-win": PeerSpec(name="ywata-note-win", ssh="ywata-note-win"),
    }
    # Act
    out = _resolve_dispatch_peer("ywata-note-win", "ywata-note-win", peers)
    # Assert
    assert out is None


def test_target_matches_current_host_returns_none_when_peer_missing():
    # Arrange — peer registry does NOT list the current host.
    peers = _peers_with_spartan()
    # Act
    out = _resolve_dispatch_peer("ywata-note-win", "ywata-note-win", peers)
    # Assert
    assert out is None


def test_unknown_target_returns_none_for_caller_to_decide():
    # Arrange
    peers = _peers_with_spartan()
    # Act
    out = _resolve_dispatch_peer("unknown-host", "ywata-note-win", peers)
    # Assert
    assert out is None


def test_known_peer_distinct_from_current_returns_peer_name():
    # Arrange
    peers = _peers_with_spartan()
    # Act
    out = _resolve_dispatch_peer("spartan-bm152", "ywata-note-win", peers)
    # Assert
    assert out == "spartan-bm152"


# ---------------------------------------------------------------------------
# Edge cases — case sensitivity + whitespace
# ---------------------------------------------------------------------------


def test_target_host_lookup_is_case_sensitive():
    # Arrange — uppercase target with lowercase peer key must NOT match.
    # Matches the underlying dict-lookup semantics in
    # ``host_config.load().peers`` (peer keys are taken verbatim from
    # YAML, no folding).
    peers = _peers_with_spartan()
    # Act
    out = _resolve_dispatch_peer("SPARTAN-BM152", "ywata-note-win", peers)
    # Assert
    assert out is None


def test_target_host_whitespace_padding_is_not_stripped():
    # Arrange — leading/trailing whitespace must NOT be normalised; the
    # resolver compares strings literally so config drift surfaces as a
    # missed match rather than a silent recovery.
    peers = _peers_with_spartan()
    # Act
    out = _resolve_dispatch_peer(" spartan-bm152 ", "ywata-note-win", peers)
    # Assert
    assert out is None
