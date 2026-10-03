"""Real ASGI/ledger/native-file boundaries for historical handshake observation."""

import json
import math
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from scitex_dev.status import StatusCode

from scitex_agent_container._listen._activity_projection import activity_projection
from scitex_agent_container._listen._agent_handshake import HandshakeDependencies
from scitex_agent_container._runners._session_state import write_heartbeat
from scitex_agent_container._state._agentic_handshake import expected_answer
from scitex_agent_container._state.authoritative_heartbeat import (
    classify_resident_state,
)
from scitex_agent_container.runtimes._codex_activity_projection import (
    promote_codex_activity,
)

from ..runtimes.test__codex_activity import _meta, _tool, _turn
from ..runtimes.test__codex_activity_binding import _layout
from ..runtimes.test__codex_activity_projection import _Diary
from .test__agent_handshake import TARGET, _feedback, _row
from .test__agent_handshake import rig as rig


def _append_tool(rig, contract, *, completed_at=102):
    with rig.rollout.open("a") as stream:
        stream.write(
            _row(
                "response_item",
                {
                    "type": "function_call",
                    "call_id": "fresh-compute",
                    "arguments": "private-command",
                },
                101,
            )
        )
        stream.write(
            _row(
                "response_item",
                {
                    "type": "function_call_output",
                    "call_id": "fresh-compute",
                    "output": "private-result " + expected_answer(contract),
                },
                completed_at,
            )
        )


@pytest_asyncio.fixture
async def proven_receipt(rig):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=rig.app), base_url="http://test"
    ) as client:
        issued = await client.post(
            "/agents/scitex-hub/handshakes", json={"timeout_s": 10}
        )
        exchange = issued.json()["exchange_id"]
        contract = json.loads(rig.memory.values[exchange]["operation"])
        uri = f"/agents/scitex-hub/handshakes/{exchange}"
        feedback = _feedback(contract, exchange)
        feedback["verified_at"] = (
            1  # Authored extra fields never stamp server evidence.
        )
        await client.post(uri + "/ack", json={"feedback": feedback})
        pending = await client.get(uri)
        _append_tool(rig, contract)
        rig.clock.now = 104
        proven = await client.get(uri)
        yield SimpleNamespace(
            rig=rig,
            client=client,
            exchange=exchange,
            contract=contract,
            uri=uri,
            pending=pending,
            proven=proven,
        )


@pytest.mark.asyncio
async def test_native_completion_server_verification_and_observation_are_distinct(
    proven_receipt,
):
    # Arrange
    receipt = proven_receipt
    # Act
    data = receipt.proven.json()
    # Assert
    assert (
        data["issued_at"],
        data["deadline"],
        data["tool_completed_at"],
        data["verified_at"],
        data["observed_at"],
        data["handshake_lease_s"],
        data["verified_age_s"],
    ) == (100, 110, 102, 104, 104, 10, 0)


@pytest.mark.asyncio
async def test_authored_ack_cannot_supply_server_verification_timestamp(proven_receipt):
    # Arrange
    receipt = proven_receipt
    # Act
    data = receipt.pending.json()
    # Assert
    assert (data["verified_at"], data["verified_age_s"], data["proven"]) == (
        None,
        None,
        None,
    )


@pytest.mark.asyncio
async def test_repeated_poll_ages_without_rewriting_immutable_success(proven_receipt):
    # Arrange
    receipt = proven_receipt
    revision = receipt.rig.memory.revisions[receipt.exchange]
    receipt.rig.clock.now = 106
    # Act
    result = await receipt.client.get(receipt.uri)
    # Assert
    assert (
        result.json()["verified_at"],
        result.json()["observed_at"],
        result.json()["verified_age_s"],
        receipt.rig.memory.revisions[receipt.exchange],
    ) == (104, 106, 2, revision)


@pytest.mark.asyncio
async def test_stale_finalizer_retains_the_first_committed_server_clock(proven_receipt):
    # Arrange: a second observer prepared its successful result before reading finality.
    receipt = proven_receipt
    row = receipt.rig.memory.values[receipt.exchange]
    original = dict(row)
    revision = receipt.rig.memory.revisions[receipt.exchange]
    stale = json.loads(row["message"])
    stale["verified_at"] = 106
    dependencies = HandshakeDependencies(store_factory=lambda: receipt.rig.memory)
    # Act: the server adapter uses the real ledger's status/CAS boundary.
    dependencies.advance(
        receipt.exchange,
        StatusCode(kind=row["kind"], code=200, message=json.dumps(stale)),
    )
    # Assert
    assert (
        receipt.rig.memory.values[receipt.exchange],
        receipt.rig.memory.revisions[receipt.exchange],
    ) == (original, revision)


@pytest.mark.asyncio
async def test_later_pending_exchange_preserves_separate_successful_receipt(
    proven_receipt,
):
    # Arrange
    receipt = proven_receipt
    receipt.rig.clock.now = 106
    # Act
    pending = await receipt.client.post("/agents/scitex-hub/handshakes", json={})
    historical = await receipt.client.get(receipt.uri)
    # Assert
    assert (
        pending.status_code,
        pending.json()["verified_at"],
        historical.json()["verified_at"],
        historical.json()["verified_age_s"],
        pending.json()["exchange_id"] != receipt.exchange,
    ) == (202, None, 104, 2, True)


@pytest.mark.asyncio
async def test_expired_success_retains_same_identity_history_without_current_admission(
    proven_receipt,
):
    # Arrange
    receipt = proven_receipt
    receipt.rig.clock.now = 111
    # Act
    result = await receipt.client.get(receipt.uri)
    # Assert
    assert (
        result.status_code,
        result.json()["proven"],
        result.json()["phase"],
        result.json()["verified_at"],
        result.json()["verified_age_s"],
        result.json()["lease_remaining_s"],
        receipt.rig.memory.values[receipt.exchange]["code"],
    ) == (410, None, "expired", 104, 7, -1, 200)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(
            lambda rig: rig.source.target.update(instance_id="successor"),
            id="instance_id",
        ),
        pytest.param(
            lambda rig: rig.source.target.update(boot_id="successor"), id="boot_id"
        ),
        pytest.param(
            lambda rig: rig.source.target.update(session_id="successor"),
            id="session_id",
        ),
        pytest.param(lambda rig: setattr(rig.source, "replaced", True), id="source"),
        pytest.param(
            lambda rig: setattr(rig.source, "available", False), id="unavailable"
        ),
    ],
)
async def test_successor_or_missing_source_cannot_borrow_expired_success(
    proven_receipt, mutate
):
    # Arrange
    receipt = proven_receipt
    receipt.rig.clock.now = 111
    mutate(receipt.rig)
    # Act
    result = await receipt.client.get(receipt.uri)
    # Assert
    assert (
        result.status_code,
        result.json()["proven"],
        result.json().get("verified_at"),
        receipt.rig.memory.values[receipt.exchange]["code"],
    ) == (503, None, None, 200)


@pytest.mark.asyncio
@pytest.mark.parametrize("now", [math.nan, math.inf, -1, 99])
async def test_bad_or_regressed_observation_clock_does_not_fabricate_zero_age(
    proven_receipt, now
):
    # Arrange
    receipt = proven_receipt
    receipt.rig.clock.now = now
    # Act
    result = await receipt.client.get(receipt.uri)
    # Assert
    assert (
        result.status_code,
        result.json()["proven"],
        result.json().get("verified_age_s"),
    ) == (503, None, None)


@pytest.mark.asyncio
async def test_legacy_success_does_not_backfill_a_server_verification_clock(
    proven_receipt,
):
    # Arrange: simulate an existing immutable receipt from the earlier source version.
    receipt = proven_receipt
    row = receipt.rig.memory.values[receipt.exchange]
    state = json.loads(row["message"])
    del state["verified_at"]
    row["message"] = json.dumps(state)
    receipt.rig.clock.now = 106
    revision = receipt.rig.memory.revisions[receipt.exchange]
    # Act
    result = await receipt.client.get(receipt.uri)
    # Assert
    assert (
        result.json()["verified_at"],
        result.json()["verified_age_s"],
        receipt.rig.memory.revisions[receipt.exchange],
    ) == (None, None, revision)


@pytest.mark.asyncio
async def test_first_late_poll_records_timely_proof_but_never_readmits_expired_lease(
    rig,
):
    # Arrange
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=rig.app), base_url="http://test"
    ) as client:
        issued = await client.post(
            "/agents/scitex-hub/handshakes", json={"timeout_s": 10}
        )
        exchange = issued.json()["exchange_id"]
        contract = json.loads(rig.memory.values[exchange]["operation"])
        uri = f"/agents/scitex-hub/handshakes/{exchange}"
        await client.post(
            uri + "/ack", json={"feedback": _feedback(contract, exchange)}
        )
        _append_tool(rig, contract)
        rig.clock.now = 112
        # Act
        result = await client.get(uri)
    # Assert
    assert (
        result.status_code,
        result.json()["proven"],
        result.json()["tool_completed_at"],
        result.json()["verified_at"],
        rig.memory.values[exchange]["code"],
    ) == (410, None, 102, 112, 200)


def _native_work(tmp_path, rig):
    root = tmp_path / "native"
    root.mkdir()
    layout = _layout(root)
    layout["record"].update(
        name=TARGET["agent"], host=TARGET["host"], screen="tui-" + TARGET["agent"]
    )
    birth = layout["birth"]
    birth.update(agent_id=TARGET["agent"], host=TARGET["host"])
    compiled = json.loads(birth["compiled_spec_json"])
    compiled["name"] = TARGET["agent"]
    birth["compiled_spec_json"] = json.dumps(compiled)
    layout["rollout"].write_text(
        _meta() + _turn("task_started") + _tool("function_call", timestamp=14)
    )
    diary = _Diary()
    promote_codex_activity(
        layout["state"],
        TARGET["agent"],
        host=TARGET["host"],
        write_fn=lambda directory, **fields: write_heartbeat(
            directory, db_writer=diary, **fields
        ),
        instance_reader=lambda _: dict(layout["record"]),
        birth_reader=lambda _: dict(birth),
        proc_root=layout["proc"],
        now_fn=lambda: 100,
    )
    heartbeat = json.loads((layout["state"] / "heartbeat.json").read_text())
    rig.source.target.update(
        instance_id=layout["record"]["id"],
        boot_id=heartbeat["boot_id"],
        session_id=heartbeat["session_id"],
    )
    return layout, heartbeat


@pytest.mark.asyncio
async def test_missing_ack_while_native_work_is_busy_never_marks_process_stopped(
    rig, tmp_path
):
    # Arrange: the real native heartbeat writer publishes ongoing work independently.
    layout, heartbeat = _native_work(tmp_path, rig)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=rig.app), base_url="http://test"
    ) as client:
        issued = await client.post(
            "/agents/scitex-hub/handshakes", json={"timeout_s": 1}
        )
        rig.clock.now = 102
        # Act: deadline miss does not enter any work counter or process classifier.
        result = await client.get(
            f"/agents/scitex-hub/handshakes/{issued.json()['exchange_id']}"
        )
    activity = activity_projection(layout["state"], now=102)
    resident = classify_resident_state(
        heartbeat["authoritative_heartbeat"],
        now=102,
        process_alive=True,
        federation_connected=True,
        progress_stale_s=90,
    )
    # Assert
    assert (
        result.json()["verified_at"],
        activity["operation"]["value"],
        activity["tools_started"]["value"],
        activity["tools_inflight"]["value"],
        resident,
    ) == (None, "busy", 1, 1, "active")
