"""``_annotate_status_reachability`` must resolve its own host.

WHY THIS FILE EXISTS. ``create_app`` defaults ``local_host=None`` and the one
production caller (``cli_pkg/listen_cmds.py``) does not pass it. So
``app.state.local_host`` was None on every running daemon,
``_reachability._is_locally_observable`` hit its ``if not local_host: return
False``, and EVERY row carrying a host was annotated ``unknown`` — including
rows on the very host answering the request. Reported by scitex-dev as all 11
a2a peers reading ``unknown``.

That made ``inbox_reachable`` a three-valued field that could only ever return
one of its values, while its own tool description tells callers to check it
before sending. The sibling ``_node_channel`` path already carried the env
fallback for the identical question; this one did not.

A SEPARATE FILE, not an addition to ``test_server.py``: that mirror is already
1,647 lines against a 512-line cap, and the package convention is focused
``test_server_<topic>.py`` files (``test_server_lineage_acl.py``,
``test_server_agent_card_path.py``, …).

PA-306: no mocks. ``_Req`` is a hand-written stand-in holding exactly the two
attributes the function reads — the same "configuration, not mocking" pattern
``test_server.py`` documents for its module-attribute re-pointing.
"""

from __future__ import annotations

import asyncio

from scitex_agent_container._listen.server import _annotate_status_reachability
from scitex_agent_container._state.state_db import _resolve_host


class _Inbox:
    """The broker seam: returns a real subscriber-count mapping."""

    def __init__(self, counts: dict[str, int]) -> None:
        self._counts = counts

    async def subscriber_counts(self) -> dict[str, int]:
        return dict(self._counts)


class _State:
    def __init__(self, inbox: _Inbox, local_host: str | None) -> None:
        self.inbox = inbox
        self.local_host = local_host


class _App:
    def __init__(self, state: _State) -> None:
        self.state = state


class _Req:
    def __init__(self, app: _App) -> None:
        self.app = app


def _annotate(row: dict, *, counts: dict[str, int], local_host: str | None) -> dict:
    req = _Req(_App(_State(_Inbox(counts), local_host)))
    return asyncio.run(_annotate_status_reachability(req, row))


def test_local_row_is_reachable_when_app_state_local_host_is_unset():
    # Arrange — reproduces production exactly: create_app was called without
    # local_host, so app.state.local_host is None, and the row names THIS host.
    host = _resolve_host(None)
    row = {"name": "alpha", "host": host}
    # Act
    out = _annotate(row, counts={"alpha": 1}, local_host=None)
    # Assert — before the fix this was "unknown" for every host-carrying row.
    assert out["inbox_reachable"] == "reachable"


def test_local_row_reports_its_subscriber_count_when_local_host_is_unset():
    # Arrange — the count is the other half: an "unknown" verdict also nulls
    # inbox_subscribers, so asserting only the verdict leaves that unpinned.
    host = _resolve_host(None)
    row = {"name": "alpha", "host": host}
    # Act
    out = _annotate(row, counts={"alpha": 3}, local_host=None)
    # Assert
    assert out["inbox_subscribers"] == 3


def test_a_row_on_another_host_stays_unknown():
    # Arrange — POSITIVE CONTROL for the assertions above. They check that a
    # value is NOT "unknown", which a function that always said "reachable"
    # would also satisfy. A remote row must still be unknown: this daemon has
    # no window into another host's broker, and inventing a zero there would
    # be a false accusation of deafness.
    row = {"name": "beta", "host": "some-other-host-that-is-not-us"}
    # Act
    out = _annotate(row, counts={}, local_host=None)
    # Assert
    assert out["inbox_reachable"] == "unknown"


def test_an_explicit_local_host_still_wins():
    # Arrange — the fallback must not shadow a caller that DID pin the host,
    # which is what in-process multi-host tests rely on.
    row = {"name": "alpha", "host": "pinned-host"}
    # Act
    out = _annotate(row, counts={"alpha": 1}, local_host="pinned-host")
    # Assert
    assert out["inbox_reachable"] == "reachable"
