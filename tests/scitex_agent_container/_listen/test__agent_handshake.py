"""Listen issues, observes and fences a real native computation exchange."""

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest
from scitex_dev.store import NEW_RECORD
from starlette.applications import Starlette
from starlette.routing import Route

from scitex_agent_container._listen import _agent_handshake as routes
from scitex_agent_container._state import _agentic_handshake as state
from scitex_agent_container._state import dispatch_feedback, dispatch_ledger
from scitex_agent_container.a2a._inbox_bus import Broker
from scitex_agent_container.runtimes._codex_activity import (
    CodexActivityError,
    reduce_codex_activity,
)

THREAD = "01a0fdd8-24b2-7b23-a264-4ae60f30245b"
TARGET = {
    "agent": "scitex-hub",
    "host": "compute-04",
    "instance_id": "canonical-instance",
    "boot_id": "canonical-instance:123:456",
    "session_id": THREAD,
}


class _MemoryLedger:
    def __init__(self):
        self.values, self.revisions = {}, {}

    def get(self, key):
        value = self.values.get(key["exchange_id"])
        return None if value is None else SimpleNamespace(values=dict(value))

    def revision(self, key):
        return self.revisions.get(key["exchange_id"])

    def put(self, values, *, expected_revision):
        key = values["exchange_id"]
        assert (
            expected_revision is NEW_RECORD or expected_revision == self.revisions[key]
        )
        self.values[key] = dict(values)
        self.revisions[key] = self.revisions.get(key, 0) + 1

    def close(self):
        pass


def _row(record_type, payload, at=90):
    return (
        json.dumps(
            {
                "type": record_type,
                "payload": payload,
                "timestamp": datetime.fromtimestamp(at, timezone.utc).isoformat(),
            }
        )
        + "\n"
    )


@pytest.fixture
def rig(tmp_path, monkeypatch):
    # Existing protocol adapters and existing dispatch_feedback write logic run;
    # only their public store boundaries and kernel capture are test doubles.
    memory = _MemoryLedger()
    dispatches, feedbacks, events = {}, {}, []
    monkeypatch.setattr(state, "_store", lambda: memory)
    monkeypatch.setattr(routes.time, "time", lambda: 100.0)
    monkeypatch.setattr(
        dispatch_ledger,
        "record_dispatch",
        lambda **kw: dispatches.setdefault(kw["dispatch_id"], {**kw, "status": "sent"}),
    )
    monkeypatch.setattr(
        dispatch_feedback, "get_dispatch", lambda did, **kw: dispatches.get(did)
    )
    monkeypatch.setattr(
        dispatch_feedback, "_feedback", lambda did, agent: feedbacks.get(did)
    )
    monkeypatch.setattr(
        dispatch_feedback,
        "_put",
        lambda values, **kw: feedbacks.setdefault(values["dispatch_id"], dict(values)),
    )
    monkeypatch.setattr(
        dispatch_feedback,
        "update_dispatch_status",
        lambda did, status, **kw: dispatches[did].update(status=status),
    )
    from scitex_agent_container._state import state_store_channel

    monkeypatch.setattr(
        state_store_channel,
        "persist_event",
        lambda **kw: events.append(kw["event"]) or len(events),
    )
    rollout = tmp_path / "rollout-test.jsonl"
    rollout.write_text(_row("session_meta", {"id": THREAD, "source": "cli"}))
    stat = rollout.stat()
    binding = SimpleNamespace(
        rollout_path=rollout,
        rollout_identity=(stat.st_dev, stat.st_ino),
        thread_id=THREAD,
    )

    def capture(name, host):
        assert (name, host) == (TARGET["agent"], TARGET["host"])
        observed = reduce_codex_activity(
            rollout.read_text().splitlines(keepends=True),
            expected_thread_id=THREAD,
            observed_at=100,
        )
        return dict(TARGET), observed.event_seq, binding

    monkeypatch.setattr(routes, "_capture_target", capture)
    app = Starlette(
        routes=[
            Route(
                "/agents/{name}/handshakes",
                routes.agent_handshake_start,
                methods=["POST"],
            ),
            Route(
                "/agents/{name}/handshakes/{exchange_id}", routes.agent_handshake_status
            ),
            Route(
                "/agents/{name}/handshakes/{exchange_id}/ack",
                routes.agent_handshake_ack,
                methods=["POST"],
            ),
        ]
    )
    app.state.local_host, app.state.inbox = TARGET["host"], Broker()
    return SimpleNamespace(
        app=app, memory=memory, rollout=rollout, feedbacks=feedbacks, events=events
    )


def _feedback(contract, exchange_id):
    return {
        "dispatch_id": contract["nonce"],
        "understood": "Compute and verify session ownership.",
        "owner": TARGET["agent"],
        "next_checkpoint": "Report the current native activity.",
        "handshake_proof": {
            "exchange_id": exchange_id,
            "nonce": contract["nonce"],
            **{key: TARGET[key] for key in ("instance_id", "boot_id", "session_id")},
            "answer": state.expected_answer(contract),
        },
    }


@pytest.mark.asyncio
async def test_round_trip_remains_202_until_new_correlated_native_output_is_source_observed(
    rig,
    monkeypatch,
):
    # Arrange: subscribe only to observe actual persisted daemon challenge.
    queue = await rig.app.state.inbox.subscribe(TARGET["agent"])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=rig.app), base_url="http://test"
    ) as client:
        # Act: issue, deliver, then intentionally ACK before tool output exists.
        started = await client.post("/agents/scitex-hub/handshakes", json={})
        exchange_id = started.json()["exchange_id"]
        event = queue.get_nowait()
        contract = json.loads(rig.memory.values[exchange_id]["operation"])
        uri = f"/agents/scitex-hub/handshakes/{exchange_id}"
        acked = await client.post(
            uri + "/ack", json={"feedback": _feedback(contract, exchange_id)}
        )
        pending = await client.get(uri)
        # Real-format fixture now contains the independently computed digest.
        rig.rollout.write_text(
            rig.rollout.read_text()
            + _row(
                "response_item",
                {
                    "type": "function_call",
                    "call_id": "call-compute",
                    "arguments": "private-command",
                },
                100,
            )
            + _row(
                "response_item",
                {
                    "type": "function_call_output",
                    "call_id": "call-compute",
                    "output": f"private-result {state.expected_answer(contract)}",
                },
                100,
            )
        )
        proven = await client.get(uri)
        (rig.rollout.parent / "handshake-schema-examples.json").write_text(
            json.dumps(
                {
                    "origin": "synthetic filesystem fixture; not live fleet acceptance",
                    "source": "test_round_trip_remains_202_until_new_correlated_native_output_is_source_observed",
                    "issued": started.json(),
                    "authored_ack": acked.json(),
                    "pending": pending.json(),
                    "proven": proven.json(),
                },
                indent=2,
            )
            + "\n"
        )
        monkeypatch.setattr(routes.time, "time", lambda: contract["deadline"] + 1)
        expired_success = await client.get(uri)
    # Assert: same server nonce, authored fields, actual output, privacy-safe result.
    assert (
        started.status_code,
        acked.status_code,
        pending.status_code,
        proven.status_code,
    ) == (202, 202, 202, 200)
    assert (
        event["from_agent"] == "daemon"
        and event["kind"] == "agentic_challenge"
        and event["_row_id"] == 1
    )
    assert (
        started.json()["transport"]["agentic_proof"] is False
        and pending.json()["proven"] is None
    )
    assert (
        proven.json()["proven"] is True
        and proven.json()["proof"]["call_id"] == "call-compute"
    )
    assert "private-command" not in proven.text and "private-result" not in proven.text
    assert (
        expired_success.status_code == 410 and expired_success.json()["proven"] is None
    )
    assert rig.memory.values[exchange_id]["code"] == 200
    assert (
        rig.feedbacks[contract["nonce"]]["understood"]
        == "Compute and verify session ownership."
    )


@pytest.mark.asyncio
async def test_zero_subscribers_and_mechanical_ack_never_prove_agentic_work(
    rig, monkeypatch
):
    # Arrange
    # Act
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=rig.app), base_url="http://test"
    ) as client:
        started = await client.post(
            "/agents/scitex-hub/handshakes", json={"timeout_s": 1}
        )
        exchange_id = started.json()["exchange_id"]
        contract = json.loads(rig.memory.values[exchange_id]["operation"])
        feedback = _feedback(contract, exchange_id)
        feedback["understood"] = contract["nonce"]
        uri = f"/agents/scitex-hub/handshakes/{exchange_id}"
        refused = await client.post(uri + "/ack", json={"feedback": feedback})
        monkeypatch.setattr(routes.time, "time", lambda: 102.0)
        expired = await client.get(uri)
    # Assert
    assert started.json()["transport"]["subscriber_count"] == 0
    assert refused.status_code == 422 and rig.feedbacks == {}
    assert (
        expired.json()["proven"] is False
        and expired.json()["status_code"]["code"] == 504
    )


@pytest.mark.asyncio
async def test_echoing_correct_hash_without_a_new_tool_output_expires_unproven(
    rig, monkeypatch
):
    # Arrange
    # Act
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=rig.app), base_url="http://test"
    ) as client:
        started = await client.post(
            "/agents/scitex-hub/handshakes", json={"timeout_s": 1}
        )
        exchange_id = started.json()["exchange_id"]
        contract = json.loads(rig.memory.values[exchange_id]["operation"])
        uri = f"/agents/scitex-hub/handshakes/{exchange_id}"
        await client.post(
            uri + "/ack", json={"feedback": _feedback(contract, exchange_id)}
        )
        monkeypatch.setattr(routes.time, "time", lambda: 102.0)
        result = await client.get(uri)
    # Assert: semantic ACK proves receipt; it still cannot prove native work.
    assert (
        result.json()["proven"] is False and result.json()["status_code"]["code"] == 422
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["unavailable", "foreign_session", "replaced_source"])
async def test_owner_disappearing_after_authored_ack_stays_unknown(
    rig, monkeypatch, case
):
    # Arrange
    # Act
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=rig.app), base_url="http://test"
    ) as client:
        started = await client.post("/agents/scitex-hub/handshakes", json={})
        exchange_id = started.json()["exchange_id"]
        contract = json.loads(rig.memory.values[exchange_id]["operation"])
        uri = f"/agents/scitex-hub/handshakes/{exchange_id}"
        await client.post(
            uri + "/ack", json={"feedback": _feedback(contract, exchange_id)}
        )

        original = routes._capture_target

        def unavailable(*args):
            if case == "unavailable":
                raise CodexActivityError("private process path and error")
            target, cursor, binding = original(*args)
            if case == "foreign_session":
                target["session_id"] = "foreign-thread"
            else:
                binding = SimpleNamespace(**vars(binding))
                binding.rollout_identity = (
                    binding.rollout_identity[0],
                    binding.rollout_identity[1] + 1,
                )
            return target, cursor, binding

        monkeypatch.setattr(routes, "_capture_target", unavailable)
        result = await client.get(uri)
    # Assert: no terminal success or private failure strings.
    assert result.status_code == 503 and result.json()["proven"] is None
    assert (
        "private process" not in result.text
        and rig.memory.values[exchange_id]["final"] is False
    )


@pytest.mark.asyncio
async def test_client_cannot_select_nonce_or_runtime_identity(rig):
    # Arrange
    # Act
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=rig.app), base_url="http://test"
    ) as client:
        result = await client.post(
            "/agents/scitex-hub/handshakes", json={"nonce": "client-selected"}
        )
    # Assert: rejected before persistence or delivery.
    assert result.status_code == 400 and rig.memory.values == {} and rig.events == []
