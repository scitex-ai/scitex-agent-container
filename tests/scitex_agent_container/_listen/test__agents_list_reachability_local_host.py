"""The LIST endpoint must resolve its own host, exactly as the single-row one does.

WHY THIS FILE EXISTS, and why the sibling test was not enough. #1174 added the
env fallback to ``server._annotate_status_reachability`` — the SINGLE-row path
— and its test (``test_server_reachability_local_host.py``) proved that path
correct. It shipped, and the reported symptom did not move: ``a2a_peers`` still
returned ``inbox_reachable: "unknown"`` for all eleven peers.

Because ``a2a_peers`` reads ``GET /agents``, and that route annotates through
``_agents_list._annotate_reachability``, which kept the bare
``getattr(request.app.state, "local_host", None)``. ``create_app`` defaults
that to ``None`` and ``cli_pkg/listen_cmds.py`` never passes it, so
``_is_locally_observable`` hit ``if not local_host: return False`` for every
row carrying host evidence and annotated the whole fleet ``unknown``.

MEASURED on scitex-compute-04 2026-08-20T21:35Z, with the daemon restarted and
verifiably running the #1174 code: 11 of 11 rows ``unknown``, every one of them
with a ``turn_url`` naming this very host.

So the lesson this file encodes is not "add a fallback". It is that a fix
verified on the call site it PATCHED says nothing about the call site the
SYMPTOM arrives through, and both existed here.

PA-306: no mocks. The stand-ins below are hand-written objects holding exactly
the attributes the function reads — the same "configuration, not mocking"
pattern the sibling file documents.
"""

from __future__ import annotations

import asyncio

from scitex_agent_container._listen._agents_list import _annotate_reachability
from scitex_agent_container._listen._reachability import (
    REACHABLE,
    UNKNOWN,
    UNREACHABLE,
)
from scitex_agent_container._state.state_db import _resolve_host


class _Inbox:
    """The broker seam: returns a real subscriber-count mapping."""

    def __init__(self, counts: dict[str, int]) -> None:
        self._counts = counts

    async def subscriber_counts(self) -> dict[str, int]:
        return dict(self._counts)


class _BrokenInbox:
    """A broker that cannot be read — the degrade-safely control."""

    async def subscriber_counts(self) -> dict[str, int]:
        raise RuntimeError("broker unreadable")


class _State:
    def __init__(self, inbox: object, local_host: str | None) -> None:
        self.inbox = inbox
        self.local_host = local_host


class _App:
    def __init__(self, state: _State) -> None:
        self.state = state


class _Req:
    def __init__(self, app: _App) -> None:
        self.app = app


def _annotate(
    rows: list[dict],
    *,
    counts: dict[str, int] | None = None,
    local_host: str | None = None,
    inbox: object | None = None,
) -> list[dict]:
    box = inbox if inbox is not None else _Inbox(counts or {})
    req = _Req(_App(_State(box, local_host)))
    return asyncio.run(_annotate_reachability(req, rows))


def _local_row() -> dict:
    """A row shaped like production: no ``host`` key, host named by turn_url."""
    return {"name": "peer", "turn_url": f"http://{_resolve_host(None)}:19001/v1/turn"}


# ---------------------------------------------------------------------------
# The defect: production has local_host=None, and the list path read it bare
# ---------------------------------------------------------------------------


def test_a_local_row_is_reachable_when_app_state_has_no_host() -> None:
    # Arrange
    rows = [_local_row()]
    # Act
    out = _annotate(rows, counts={"peer": 1}, local_host=None)
    # Assert — was UNKNOWN for all 11 peers on the live fleet
    assert out[0]["inbox_reachable"] == REACHABLE


def test_a_local_row_reports_its_subscriber_count() -> None:
    # Arrange
    rows = [_local_row()]
    # Act
    out = _annotate(rows, counts={"peer": 1}, local_host=None)
    # Assert — the count was None alongside the unknown verdict
    assert out[0]["inbox_subscribers"] == 1


def test_a_local_row_with_no_subscribers_is_unreachable() -> None:
    # Arrange — an OBSERVED zero on a bus we can see is the one case
    # that earns UNREACHABLE rather than UNKNOWN.
    rows = [_local_row()]
    # Act
    out = _annotate(rows, counts={}, local_host=None)
    # Assert — the field must be able to reach its third value too
    assert out[0]["inbox_reachable"] == UNREACHABLE


def test_a_local_row_with_no_subscribers_reports_zero() -> None:
    # Arrange
    rows = [_local_row()]
    # Act
    out = _annotate(rows, counts={}, local_host=None)
    # Assert
    assert out[0]["inbox_subscribers"] == 0


# ---------------------------------------------------------------------------
# Controls — the fix must not buy reachability by fabricating locality
# ---------------------------------------------------------------------------


def test_a_remote_row_stays_unknown() -> None:
    # Arrange — a row on a DIFFERENT host, served by a different broker whose
    # subscriber table we cannot see.
    rows = [{"name": "peer", "host": "some-other-host-entirely"}]
    # Act
    out = _annotate(rows, counts={}, local_host=None)
    # Assert — a fabricated zero here is a false accusation of deafness
    assert out[0]["inbox_reachable"] == UNKNOWN


def test_a_remote_row_reports_no_subscriber_count() -> None:
    # Arrange
    rows = [{"name": "peer", "host": "some-other-host-entirely"}]
    # Act
    out = _annotate(rows, counts={}, local_host=None)
    # Assert — None, not 0: we did not observe, we declined to guess
    assert out[0]["inbox_subscribers"] is None


def test_an_explicit_app_state_host_still_wins() -> None:
    # Arrange — when create_app IS given a host, that declaration is
    # authoritative and the env resolver must not override it.
    rows = [{"name": "peer", "host": "declared-host"}]
    # Act
    out = _annotate(rows, counts={"peer": 2}, local_host="declared-host")
    # Assert — the fallback is a fallback, not a replacement
    assert out[0]["inbox_reachable"] == REACHABLE


def test_an_unreadable_broker_degrades_to_unknown() -> None:
    # Arrange — the safe-direction guarantee this route is written around
    rows = [_local_row()]
    # Act
    out = _annotate(rows, inbox=_BrokenInbox(), local_host=None)
    # Assert — "could not check" must never render as death
    assert out[0]["inbox_reachable"] == UNKNOWN


def test_an_unreadable_broker_reports_no_count() -> None:
    # Arrange
    rows = [_local_row()]
    # Act
    out = _annotate(rows, inbox=_BrokenInbox(), local_host=None)
    # Assert
    assert out[0]["inbox_subscribers"] is None


def test_every_row_is_annotated_not_just_the_first() -> None:
    # Arrange — the list path's own responsibility: it maps over N rows
    here = _resolve_host(None)
    rows = [
        {"name": "a", "turn_url": f"http://{here}:19001/v1/turn"},
        {"name": "b", "turn_url": f"http://{here}:19002/v1/turn"},
        {"name": "c", "host": "elsewhere"},
    ]
    # Act
    out = _annotate(rows, counts={"a": 1}, local_host=None)
    # Assert
    assert [r["inbox_reachable"] for r in out] == [REACHABLE, UNREACHABLE, UNKNOWN]
