"""Listen issues, observes and fences a real native computation exchange."""

import json
from datetime import datetime, timezone
from functools import partial
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from scitex_dev.store import NEW_RECORD
from starlette.applications import Starlette

from scitex_agent_container._listen import _agent_handshake as routes
from scitex_agent_container._state import _agentic_handshake as state
from scitex_agent_container._state import dispatch_feedback
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
def rig(tmp_path):
    # Existing protocol adapters and existing dispatch_feedback write logic run;
    # only their public store boundaries and kernel capture are test doubles.
    memory = _MemoryLedger()
    dispatches, feedbacks, events = {}, {}, []
    clock = SimpleNamespace(now=100.0)
    source = SimpleNamespace(available=True, target=dict(TARGET), replaced=False)
    record_ack = partial(
        dispatch_feedback.record_agentic_ack,
        dispatch_lookup=lambda did, **kw: dispatches.get(did),
        feedback_lookup=lambda did, agent: feedbacks.get(did),
        feedback_write=lambda values, **kw: feedbacks.setdefault(
            values["dispatch_id"], dict(values)
        ),
        dispatch_update=lambda did, status, **kw: dispatches[did].update(status=status),
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
        if not source.available:
            raise CodexActivityError("private process path and error")
        assert (name, host) == (TARGET["agent"], TARGET["host"])
        observed = reduce_codex_activity(
            rollout.read_text().splitlines(keepends=True),
            expected_thread_id=THREAD,
            observed_at=clock.now,
        )
        current = SimpleNamespace(**vars(binding))
        if source.replaced:
            current.rollout_identity = (
                binding.rollout_identity[0],
                binding.rollout_identity[1] + 1,
            )
        return dict(source.target), observed.event_seq, current

    dependencies = routes.HandshakeDependencies(
        store_factory=lambda: memory,
        clock=lambda: clock.now,
        capture=capture,
        dispatch_recorder=lambda **kw: dispatches.setdefault(
            kw["dispatch_id"], {**kw, "status": "sent"}
        ),
        event_persister=lambda **kw: events.append(kw["event"]) or len(events),
        ack_recorder=lambda event, **kw: state.record_handshake_ack(
            event, now=clock.now, store_factory=lambda: memory, record_ack=record_ack
        ),
    )
    app = Starlette(
        routes=routes.handshake_routes("/agents", dependencies=dependencies)
    )
    app.state.local_host, app.state.inbox = TARGET["host"], Broker()
    return SimpleNamespace(
        app=app,
        memory=memory,
        rollout=rollout,
        feedbacks=feedbacks,
        events=events,
        clock=clock,
        source=source,
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


@pytest_asyncio.fixture
async def round_trip(rig):
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
        rig.clock.now = contract["deadline"] + 1
        expired_success = await client.get(uri)
    # Assert: same server nonce, authored fields, actual output, privacy-safe result.
    return {
        "statuses": (
            started.status_code,
            acked.status_code,
            pending.status_code,
            proven.status_code,
        ),
        "persisted_daemon_challenge": (
            event["from_agent"] == "daemon"
            and event["kind"] == "agentic_challenge"
            and event["_row_id"] == 1
        ),
        "acceptance_is_unproven": (
            started.json()["transport"]["agentic_proof"] is False
            and pending.json()["proven"] is None
        ),
        "source_proof": (
            proven.json()["proven"] is True
            and proven.json()["proof"]["call_id"] == "call-compute"
        ),
        "privacy": "private-command" not in proven.text
        and "private-result" not in proven.text,
        "expired_success": (
            expired_success.status_code == 410
            and expired_success.json()["proven"] is None
        ),
        "final_ledger_code": rig.memory.values[exchange_id]["code"],
        "authored_feedback": (
            rig.feedbacks[contract["nonce"]]["understood"]
            == "Compute and verify session ownership."
        ),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "key,expected",
    [
        ("statuses", (202, 202, 202, 200)),
        ("persisted_daemon_challenge", True),
        ("acceptance_is_unproven", True),
        ("source_proof", True),
        ("privacy", True),
        ("expired_success", True),
        ("final_ledger_code", 200),
        ("authored_feedback", True),
    ],
)
async def test_round_trip_requires_authorship_source_proof_and_current_lease(
    round_trip, key, expected
):
    # Arrange
    observations = round_trip
    # Act
    value = observations[key]
    # Assert
    assert value == expected


@pytest_asyncio.fixture
async def mechanical_round_trip(rig):
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
        rig.clock.now = 102.0
        expired = await client.get(uri)
    # Assert
    return {
        "subscribers": started.json()["transport"]["subscriber_count"],
        "refused_without_feedback": refused.status_code == 422 and rig.feedbacks == {},
        "expired_unproven": (
            expired.json()["proven"] is False
            and expired.json()["status_code"]["code"] == 504
        ),
        "expiry_reason": json.loads(expired.json()["status_code"]["message"])[
            "reason"
        ],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "key,expected",
    [
        ("subscribers", 0),
        ("refused_without_feedback", True),
        ("expired_unproven", True),
    ],
)
async def test_zero_subscribers_and_mechanical_ack_never_prove_agentic_work(
    mechanical_round_trip, key, expected
):
    # Arrange
    observations = mechanical_round_trip
    # Act
    value = observations[key]
    # Assert
    assert value == expected


@pytest.mark.asyncio
async def test_expired_undelivered_challenge_reports_broker_publication(
    mechanical_round_trip,
):
    # Arrange: no subscriber ever admitted the persisted challenge.
    result = mechanical_round_trip
    # Act
    reason = result["expiry_reason"]
    # Assert
    assert "persisted and published to the durable inbox" in reason


@pytest.mark.asyncio
async def test_expired_undelivered_challenge_limits_missing_proof_to_lease(
    mechanical_round_trip,
):
    # Arrange: expiry is an observation boundary, not a dead-target claim.
    result = mechanical_round_trip
    # Act
    reason = result["expiry_reason"]
    # Assert
    assert "within the issued handshake lease" in reason


@pytest.mark.asyncio
async def test_expired_undelivered_challenge_keeps_target_admission_unproven(
    mechanical_round_trip,
):
    # Arrange: broker acceptance alone cannot prove target admission.
    result = mechanical_round_trip
    # Act
    reason = result["expiry_reason"]
    # Assert
    assert "Target admission was not proven." in reason


@pytest.mark.asyncio
async def test_echoing_correct_hash_without_a_new_tool_output_expires_unproven(rig):
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
        rig.clock.now = 102.0
        result = await client.get(uri)
    # Assert: semantic ACK proves receipt; it still cannot prove native work.
    assert (
        result.json()["proven"] is False and result.json()["status_code"]["code"] == 422
    )


@pytest_asyncio.fixture
async def late_reply(rig):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=rig.app), base_url="http://test"
    ) as client:
        started = await client.post(
            "/agents/scitex-hub/handshakes", json={"timeout_s": 1}
        )
        exchange_id = started.json()["exchange_id"]
        contract = json.loads(rig.memory.values[exchange_id]["operation"])
        uri = f"/agents/scitex-hub/handshakes/{exchange_id}"
        rig.clock.now = contract["deadline"] + 1
        await client.get(uri)
        final_revision = rig.memory.revisions[exchange_id]
        for payload in (
            {"type": "function_call", "call_id": "late-call", "arguments": "private"},
            {
                "type": "function_call_output",
                "call_id": "late-call",
                "output": state.expected_answer(contract),
            },
        ):
            with rig.rollout.open("a") as output:
                output.write(_row("response_item", payload, rig.clock.now))
        acked = await client.post(
            uri + "/ack", json={"feedback": _feedback(contract, exchange_id)}
        )
        status = await client.get(uri)
    return {
        "late_ack_code": acked.status_code,
        "timeout_code": status.json()["status_code"]["code"],
        "ledger_unchanged": rig.memory.revisions[exchange_id] == final_revision,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "key,expected",
    [("late_ack_code", 422), ("timeout_code", 504), ("ledger_unchanged", True)],
)
async def test_late_real_tool_output_and_authored_ack_preserve_final_timeout(
    late_reply, key, expected
):
    # Arrange: real-format correlated tool output and authored ACK arrive late.
    observations = late_reply
    # Act
    value = observations[key]
    # Assert
    assert value == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["unavailable", "foreign_session", "replaced_source"])
async def test_owner_disappearing_after_authored_ack_stays_unknown(rig, case):
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

        mutations = {
            "unavailable": lambda: setattr(rig.source, "available", False),
            "foreign_session": lambda: rig.source.target.update(
                session_id="foreign-thread"
            ),
            "replaced_source": lambda: setattr(rig.source, "replaced", True),
        }
        mutations[case]()
        result = await client.get(uri)
    # Assert: no terminal success or private failure strings.
    assert (
        result.status_code == 503
        and result.json()["proven"] is None
        and "private process" not in result.text
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
