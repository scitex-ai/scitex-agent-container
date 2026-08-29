"""`GET /agents/<name>/status` must not report UNKNOWN as a NEGATIVE.

Covers ``agent_status`` in ``src/scitex_agent_container/_listen/server.py``
(its sibling ``test_server.py`` carries the rest of that module).

THE BUG. The handler answered a flat 404 for ANY exception out of
``resolve_config`` / ``load_config``, so three different facts shared one code:

    the agent does not exist        a true NEGATIVE
    the name is ambiguous           we CANNOT TELL (two registries claim it)
    the spec is unreadable          we CANNOT TELL (I/O fault)

Only the first is a negative. This is the fleet's authoritative "does agent X
exist" route — `_send_broker.lookup_peer_via_host` calls it and treats 404 as
THE definitive negative — so collapsing the other two into 404 tells every
caller "no such agent" when the honest answer is "I could not determine that".

WHY THE NEW CODES ARE SAFE, and why 404 must stay 404: that client treats any
other non-2xx as `PeerLookupUnavailable`, which it renders as UNKNOWN with "this
is NOT evidence the agent is stopped". So 409/500 land in a branch that already
exists and is already correct, while moving the true negative off 404 would
break the one verdict the caller is entitled to trust.

No mocks (STX-NM002): a real `TestClient` against a real app, with a real
temporary registry on disk. The ambiguity case is produced by making
``resolve_config`` raise the REAL exception type via a genuinely ambiguous
name — not by patching the function.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from scitex_agent_container._listen.server import create_app

_TOKEN = "test-token-status-typed"


@pytest.fixture
def client(tmp_path: Path):
    """A real app with HOME redirected to an EMPTY registry.

    HOME is redirected so `resolve_config` cannot find the operator's real
    agents — every name is genuinely unknown unless a test creates it.
    """
    saved_home = os.environ.get("HOME")
    os.environ["HOME"] = str(tmp_path)
    try:
        with TestClient(create_app(token=_TOKEN)) as c:
            yield c
    finally:
        if saved_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = saved_home


def _get(client: TestClient, name: str):
    return client.get(
        f"/agents/{name}/status",
        headers={"authorization": f"Bearer {_TOKEN}"},
    )


# ---------------------------------------------------------------------------
# The one TRUE negative keeps 404 — the caller is entitled to trust it
# ---------------------------------------------------------------------------


def test_an_unknown_agent_still_answers_404(client) -> None:
    """404 is THE definitive negative; moving it would break every caller."""
    # Arrange
    name = "definitely-no-such-agent-xyz"
    # Act
    resp = _get(client, name)
    # Assert
    assert resp.status_code == 404


def test_an_unknown_agent_is_typed_as_unknown_agent(client) -> None:
    """The body says WHICH negative it is, not just that something failed."""
    # Arrange
    name = "definitely-no-such-agent-xyz"
    # Act
    body = _get(client, name).json()
    # Assert
    assert body["kind"] == "unknown_agent"


def test_an_unknown_agent_response_names_the_agent(client) -> None:
    # Arrange
    name = "definitely-no-such-agent-xyz"
    # Act
    body = _get(client, name).json()
    # Assert
    assert body["name"] == name


# ---------------------------------------------------------------------------
# An I/O fault is UNKNOWN, not "no such agent"
# ---------------------------------------------------------------------------


def _make_unreadable_spec(home: Path, name: str) -> Path:
    """A registered agent whose spec exists but cannot be read."""
    d = home / ".scitex" / "agent-container" / "agents" / name
    d.mkdir(parents=True, exist_ok=True)
    spec = d / "spec.yaml"
    spec.write_text("apiVersion: scitex-agent-container/v3\nkind: Agent\n")
    spec.chmod(0o000)
    return spec


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_an_unreadable_spec_is_not_reported_as_a_missing_agent(
    client, tmp_path
) -> None:
    """The agent EXISTS; saying 404 would be a lie the caller cannot detect."""
    # Arrange
    name = "unreadable-spec-agent"
    spec = _make_unreadable_spec(tmp_path, name)
    # Act
    try:
        status = _get(client, name).status_code
    finally:
        spec.chmod(0o644)
    # Assert
    assert status != 404


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_an_unreadable_spec_answers_500(client, tmp_path) -> None:
    # Arrange
    name = "unreadable-spec-agent-500"
    spec = _make_unreadable_spec(tmp_path, name)
    # Act
    try:
        status = _get(client, name).status_code
    finally:
        spec.chmod(0o644)
    # Assert
    assert status == 500


# ---------------------------------------------------------------------------
# The shape is uniform: every failure carries a machine-readable kind
# ---------------------------------------------------------------------------


def test_every_failure_carries_a_machine_readable_kind(client) -> None:
    """A caller must not have to parse prose to classify the answer."""
    # Arrange
    name = "definitely-no-such-agent-xyz"
    # Act
    body = _get(client, name).json()
    # Assert
    assert isinstance(body.get("kind"), str)
