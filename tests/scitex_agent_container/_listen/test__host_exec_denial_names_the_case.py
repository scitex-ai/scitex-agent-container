"""A host_exec denial must say WHICH empty-group case it hit, and where it looked.

INCIDENT 2026-08-09. Three agents (scitex-db, scitex-cards,
scitex-agent-container) were refused ``host_exec`` inside fifteen minutes
with::

    "host_exec is restricted to groups ['developer', 'privileged',
     'researcher']; caller '<name>' resolves to group ''"

That message asserts ONE cause — you are registered here and ungrouped —
while the truth was the other: the caller had no policy row in the store
being consulted. Both produce the empty string, and BOTH layers collapse
them deliberately:

  * ``resolve_group_name``  -- "no policy row, or a row with an empty
    group_name, is 'ungrouped'"
  * ``read_comms_policy``   -- "a missing row yields DEFAULT_COMMS_POLICY
    so the no-row vs row-with-default-values distinction is invisible"

So the message sent three readers after their group labels when the real
question was WHICH DATABASE was consulted. About fifteen minutes.

THE DECISION IS UNCHANGED. Both cases still deny, both still fail CLOSED.
This is observability, not a permissions change — which is why
:func:`test_unregistered_caller_is_still_denied` and
:func:`test_registered_but_ungrouped_caller_is_still_denied` are here:
they are the guard against "improving" this into a permissions bug.

No mocks (PA-306): the registration probe is injected at the same
production seam ``group_resolver`` already uses. AAA markers, one
assertion per test.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from scitex_agent_container._listen._host_exec import host_exec


class _FakeRequest:
    """Minimal Request stand-in: a JSON body and an authenticated node."""

    def __init__(self, body: dict[str, Any], authenticated_node: str) -> None:
        self._body = body

        class _State:
            pass

        self.state = _State()
        self.state.authenticated_node = authenticated_node

    async def json(self) -> dict[str, Any]:
        return self._body


def _noop_audit(*_args, **_kwargs) -> None:
    return None


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _deny(caller: str, *, group: str, registered: bool):
    req = _FakeRequest({"argv": ["true"]}, authenticated_node=caller)
    return _run(
        host_exec(
            req,
            group_resolver=lambda name: group,
            registration_probe=lambda name: registered,
            audit_writer=_noop_audit,
        )
    )


# ---------------------------------------------------------------------------
# The decision must not move
# ---------------------------------------------------------------------------


def test_registered_but_ungrouped_caller_is_still_denied():
    # Arrange
    caller, group, registered = "known-agent", "", True
    # Act
    response = _deny(caller, group=group, registered=registered)
    # Assert
    assert response.status_code == 403


def test_unregistered_caller_is_still_denied():
    # Arrange
    caller, group, registered = "stranger", "", False
    # Act
    response = _deny(caller, group=group, registered=registered)
    # Assert
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# ...but the message must distinguish them
# ---------------------------------------------------------------------------


def test_registered_caller_is_told_it_is_registered():
    # Arrange
    caller, registered = "known-agent", True
    # Act
    response = _deny(caller, group="", registered=registered)
    # Assert
    assert "IS registered here" in json.loads(response.body)["reason"]


def test_unregistered_caller_is_told_it_has_no_row():
    # Arrange
    caller, registered = "stranger", False
    # Act
    response = _deny(caller, group="", registered=registered)
    # Assert
    assert "NO policy row" in json.loads(response.body)["reason"]


def test_unregistered_message_says_it_is_not_the_same_as_a_denial():
    # Arrange: the reader must not go looking at group labels, which is
    # exactly what the old message caused three agents to do.
    # Act
    response = _deny("stranger", group="", registered=False)
    # Assert
    assert "NOT the same as being denied" in json.loads(response.body)["reason"]


def test_unregistered_message_points_at_the_restart_and_wrong_store_causes():
    # Arrange
    caller, registered = "stranger", False
    # Act
    response = _deny(caller, group="", registered=registered)
    # Assert
    assert "DIFFERENT store" in json.loads(response.body)["reason"]


# ---------------------------------------------------------------------------
# ...and must name where it looked
# ---------------------------------------------------------------------------


def test_denial_names_the_store_it_consulted():
    # Arrange: the single fact that was missing on 2026-08-09. Without it
    # the reader cannot tell "I lack a group" from "you asked the wrong
    # database".
    # Act
    response = _deny("stranger", group="", registered=False)
    # Assert
    assert "Store consulted:" in json.loads(response.body)["reason"]


def test_ineligible_named_group_still_reports_the_group():
    # Arrange: a caller with a REAL but ineligible group must still see it
    # named — that case was never ambiguous and must not regress.
    # Act
    response = _deny("guest-agent", group="guest", registered=True)
    # Assert
    assert "'guest'" in json.loads(response.body)["reason"]
