#!/usr/bin/env python3
"""``spec_uuid`` on every agent-list row.

Card ``sac-identity-spec-uuid-20261026`` remainder item (a): ``sac agents
list --json`` exposed 0/46 uuid keys, so name was still the only lookup.
The row builder now always carries ``spec_uuid`` — the stamped identity
when the spec (or instances row) has one, ``""`` for dormant/unstamped
specs and heartbeat-only rows. Consumers never key-check.
"""

from __future__ import annotations

from scitex_agent_container.cli_pkg._helpers._agent_list_row import build_agent_row

_ROW_KWARGS = dict(
    status_val="running",
    screen_name="tui-x",
    multiplexer="tmux",
    started="2026-11-01T00:00:00Z",
    host_label="scitex-compute-04",
    host_display="scitex-compute-04",
    spec_path="/spec.yaml",
    a2a_port=19003,
    account_label="acct",
    deferred=True,
    errors=None,
    liveness_unknown=False,
    labels=None,
)

_STAMPED = "9743cb98-0000-4000-8000-000000000000"


def test_stamped_uuid_carried() -> None:
    # Arrange — a row for a spec carrying its minted uuid.
    # Act
    row = build_agent_row(name="a", spec_uuid=_STAMPED, **_ROW_KWARGS)
    # Assert — the identity survives verbatim.
    assert row["spec_uuid"] == _STAMPED


def test_unstamped_defaults_empty() -> None:
    # Arrange — a dormant spec row built without the new kwarg.
    # Act
    row = build_agent_row(name="a", **_ROW_KWARGS)
    # Assert — the key is still present, explicitly empty.
    assert row["spec_uuid"] == ""


def test_empty_input_normalizes() -> None:
    # Arrange — a caller passing through a missing/None-ish identity.
    # Act
    row = build_agent_row(name="a", spec_uuid="", **_ROW_KWARGS)
    # Assert — never None, never absent: "" is the honest empty.
    assert row["spec_uuid"] == ""
