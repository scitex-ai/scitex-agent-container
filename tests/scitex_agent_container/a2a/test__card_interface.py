"""Agent discovery advertises a usable SDK transport, without invented skills."""

from __future__ import annotations

import pytest
import yaml
from starlette.testclient import TestClient

from scitex_agent_container.a2a import build_app
from scitex_agent_container.a2a._card_interface import agent_interface


@pytest.fixture()
def card_client(tmp_path, pg_schema):
    path = tmp_path / "echo.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "apiVersion": "scitex-agent-container/v3",
                "metadata": {"name": "echo"},
                "spec": {"a2a": {"handler": "echo", "port": 8888}},
            }
        )
    )
    with TestClient(build_app([path])) as client:
        yield client


def test_interface_strips_base_trailing_slash() -> None:
    # Arrange
    base = "http://testserver/"
    # Act
    interface = agent_interface(base, "echo", protocol_binding="JSONRPC")
    # Assert
    assert interface["url"] == "http://testserver/agents/echo/message:send"


def test_advertised_url_accepts_jsonrpc_message(card_client) -> None:
    # Arrange
    card = card_client.get("/agents/echo/.well-known/agent-card.json").json()
    payload = {
        "jsonrpc": "2.0",
        "id": "card-route",
        "method": "SendMessage",
        "params": {
            "message": {
                "message_id": "card-message",
                "role": "ROLE_USER",
                "parts": [{"text": "hello"}],
            }
        },
    }
    # Act
    response = card_client.post(
        card["supportedInterfaces"][0]["url"],
        json=payload,
        headers={"A2A-Version": "1.0"},
    )
    # Assert
    assert (
        response.json()["result"]["task"]["status"]["state"] == "TASK_STATE_COMPLETED"
    )


@pytest.mark.parametrize("discovery", ["/agents/", "/.well-known/agent-card.json"])
def test_discovery_members_match_public_agent_interface(card_client, discovery) -> None:
    # Arrange
    card = card_client.get("/agents/echo/.well-known/agent-card.json").json()
    # Act
    body = card_client.get(discovery).json()
    members = body.get("agents", body.get("x-scitex-agent-container", {}).get("agents"))
    # Assert
    assert members[0]["supportedInterfaces"] == card["supportedInterfaces"]
