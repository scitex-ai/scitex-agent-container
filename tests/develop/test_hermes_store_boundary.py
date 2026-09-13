"""Permanent ownership guard for Hermes-private versus SciTeX shared state."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / "src" / "scitex_agent_container" / "runtimes"
SHARED_STATE_MODULES = (
    "_cards_ingress.py",
    "_channel_inbox_dispatcher_lifecycle.py",
    "_hermes_profile.py",
    "_tui_turn_bridge_lifecycle.py",
)


def test_hermes_shared_state_never_reads_cards_specific_store_alias():
    # Arrange
    outcomes = []
    # Act
    for filename in SHARED_STATE_MODULES:
        source = (RUNTIME / filename).read_text(encoding="utf-8")
        outcomes.append(
            ('get("SCITEX_CARDS_DB")' not in source)
            and ('["SCITEX_CARDS_DB"]' not in source)
        )
    # Assert
    assert outcomes == [True] * len(SHARED_STATE_MODULES)


def test_hermes_lifecycle_does_not_propagate_cards_specific_store_alias():
    # Arrange
    outcomes = []
    # Act
    for filename in (
        "_channel_inbox_dispatcher_lifecycle.py",
        "_tui_turn_bridge_lifecycle.py",
    ):
        source = (RUNTIME / filename).read_text(encoding="utf-8")
        # An explicit removal from the inherited process environment is the
        # only permitted appearance; no tuple/list may forward the alias.
        outcomes.append(
            (
                source.count('env.pop("SCITEX_CARDS_DB", None)'),
                source.count('env["SCITEX_CARDS_DB"] ='),
            )
        )
    # Assert
    assert outcomes == [(1, 0), (1, 0)]


def test_accepted_protocol_adr_names_the_private_state_exception():
    # Arrange
    path = (
        ROOT
        / "docs"
        / "adr"
        / "0031-canonical-exchanges-and-bounded-hermes-visibility.md"
    )
    # Act
    adr = path.read_text(encoding="utf-8")
    normalized = " ".join(adr.split())
    # Assert
    assert (
        "SCITEX_STORE_DSN" in adr,
        "`.hermes/state.db`" in adr,
        "private session/context state" in normalized,
    ) == (True, True, True)
