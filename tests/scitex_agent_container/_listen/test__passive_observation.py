"""Real proc/owned source, native writer and read-only batch boundaries."""

import json
from functools import partial

import pytest

from scitex_agent_container._listen._agents_list import annotate_runtime_rows
from scitex_agent_container._listen._handshake_snapshot import read_handshake_page
from scitex_agent_container._listen._passive_observation import (
    annotate_observation_rows,
    capture_observation_authority,
    runtime_observation,
)

from ..runtimes.test__codex_activity import _meta
from ..runtimes.test__codex_activity_binding import AGENT, HOST, _layout
from ..runtimes.test__codex_activity_projection import _cache, _promote
from .test__handshake_snapshot import _ReadOnlyLedger, _record


@pytest.fixture
def native(tmp_path):
    layout = _layout(tmp_path)
    _promote(layout)
    # The first beat only records size baselines (no work evidence yet).
    # Session growth before the second beat is the mechanically-measured
    # work the binary verdict reads — without it every verdict is DEAD.
    session_log = layout["state"] / "session.jsonl"
    session_log.write_text("native turn produced output\n")
    _promote(layout, now=101)
    authority = capture_observation_authority(
        AGENT,
        HOST,
        [layout["record"]],
        layout["birth"],
        layout["state"],
        proc_root=layout["proc"],
    )
    return layout, authority


def _batch(
    native, *, records=None, clock=None, capture=None, reader=None, threshold=None
):
    layout, authority = native
    store = _ReadOnlyLedger(records or [])
    kwargs = {
        "active": [layout["record"]],
        "births": {AGENT: layout["birth"]},
        "local_host": HOST,
        "capture_fn": capture
        or partial(capture_observation_authority, proc_root=layout["proc"]),
        "state_dir_fn": lambda name: layout["state"],
        "clock": clock or (lambda: 106),
        "ledger_reader": reader
        or partial(read_handshake_page, store_factory=lambda: store),
        "order_fn": lambda: 7,
        "producer_epoch": "a" * 32,
        "progress_stale_s": threshold,
    }
    return annotate_observation_rows(
        [{"name": AGENT, "host": HOST, "liveness": {"verdict": "alive"}}], **kwargs
    )[0], store


def test_real_writer_and_owned_metadata_publish_same_source(native):
    # Arrange
    layout, authority = native
    # Act
    row, store = _batch(native)
    evidence = row["observation"]
    # Assert: session growth across the two fixture beats reads WORKING
    # with positive byte-delta work evidence — never counters, never phase.
    assert (
        evidence["authority"]["source"]["identity"],
        _cache(layout)["activity_source_id"],
        evidence["runtime"]["resident_state"],
        evidence["runtime"]["session_jsonl_delta_bytes"] > 0,
        "tools_completed" in evidence["runtime"],
        len(store.calls),
    ) == (authority.source.identity, authority.source.identity, "working", True, False, 1)


def test_passive_replay_keeps_server_stamp_and_no_ledger_write(native):
    # Arrange
    layout, authority = native
    row = _record(authority)
    operation = json.loads(row["operation"])
    operation["source_identity"] = [
        layout["rollout"].stat().st_dev,
        layout["rollout"].stat().st_ino,
    ]
    row["operation"] = json.dumps(operation)
    original = dict(row)
    # Act
    earlier, _ = _batch(native, records=[row])
    later, store = _batch(native, records=[row], clock=lambda: 108)
    # Assert
    assert (
        earlier["observation"]["handshake"]["last_verified_reply"]["verified_at"],
        later["observation"]["handshake"]["last_verified_reply"]["verified_at"],
        later["observation"]["handshake"]["last_verified_reply"]["verified_age_s"],
        row,
        len(store.calls),
    ) == (104, 104, 4, original, 1)


def test_source_rollover_between_reads_refuses_mixed_frame(native):
    # Arrange
    layout, authority = native
    calls = []

    def capture(*args):
        calls.append(True)
        if len(calls) == 2:
            replacement = layout["rollout"].with_name("replacement.jsonl")
            replacement.write_text(_meta())
            replacement.replace(layout["rollout"])
        return capture_observation_authority(*args, proc_root=layout["proc"])

    # Act
    row, store = _batch(native, capture=capture)
    # Assert
    assert (
        row["observation"]["authority"],
        row["observation"]["runtime"]["state"],
        row["observation"]["handshake"]["reason"],
        row["liveness"],
        len(store.calls),
    ) == (None, "unknown", "authority_unknown", {"verdict": "alive"}, 1)


@pytest.mark.parametrize(
    "key", ["activity_instance_id", "boot_id", "session_id", "activity_source_id"]
)
def test_old_work_identity_cannot_be_attached_to_current_source(native, key):
    # Arrange
    layout, authority = native
    heartbeat = _cache(layout)
    heartbeat[key] = "old"
    # Act
    result = runtime_observation(heartbeat, authority, now=106)
    # Assert: fenced identity refuses the stale source; deltas stay None,
    # never a fabricated zero.
    assert (
        result.state,
        result.session_jsonl_delta_bytes,
        result.subagent_jsonl_delta_bytes,
    ) == (
        "unknown",
        None,
        None,
    )


@pytest.mark.parametrize("timestamp", [True, float("nan"), float("inf"), 107, -1])
def test_invalid_or_future_work_observation_does_not_fabricate_zero(native, timestamp):
    # Arrange
    layout, authority = native
    heartbeat = _cache(layout)
    heartbeat["authoritative_heartbeat"]["observed_at"] = timestamp
    heartbeat["ts"] = timestamp
    # Act
    result = runtime_observation(heartbeat, authority, now=106)
    # Assert
    assert (result.state, result.age_s, result.progress_age_s) == (
        "unknown",
        None,
        None,
    )


def test_expired_work_lease_keeps_facts_with_deltas_but_no_verdict(native):
    # Arrange
    layout, authority = native
    # Act: the fixture's second beat observed at t=101 with a 90s lease,
    # so t=192 is past the deadline.
    result = runtime_observation(_cache(layout), authority, now=192)
    # Assert: clocks stay factual (age 91, 90s lease, expired), the
    # measured deltas survive (progress needs no lease), but the binary
    # verdict is withheld — an expired lease reads no verdict at all.
    assert (
        result.state,
        result.age_s,
        result.heartbeat_lease_s,
        result.lease_expired,
        result.session_jsonl_delta_bytes,
        result.resident_state,
        result.lease_remaining_s,
    ) == ("unknown", 91, 90, True, 28, None, -1)


@pytest.mark.parametrize(
    "threshold", [None, 96, 95, 0], ids=["none", "equal", "below", "zero"]
)
def test_progress_ignores_supplied_threshold_and_reads_deltas(native, threshold):
    # Arrange: the threshold knob is accepted for call-site compatibility
    # only — progress is byte-delta work evidence, never staleness math.
    layout, authority = native
    # Act
    result = runtime_observation(
        _cache(layout), authority, now=110, progress_stale_s=threshold
    )
    # Assert: progress age comes from the rollout event clock (110-14)
    # regardless of threshold; no staleness fields exist on the contract.
    assert (
        result.progress_age_s,
        result.session_jsonl_delta_bytes,
        result.subagent_jsonl_delta_bytes,
        result.resident_state,
        hasattr(result, "progress_stale_s"),
        hasattr(result, "progress_is_stale"),
    ) == (96, 28, 0, "working", False, False)


def test_missing_native_source_stamp_keeps_work_unknown(native):
    # Arrange
    layout, authority = native
    heartbeat = _cache(layout)
    del heartbeat["activity_source_id"]
    # Act
    result = runtime_observation(heartbeat, authority, now=106)
    # Assert
    assert (
        result.state,
        result.authority,
        result.session_jsonl_delta_bytes,
    ) == (
        "unknown",
        None,
        None,
    )


def test_ledger_outage_does_not_erase_independent_work_or_liveness(native):
    # Arrange
    def unavailable(names):
        raise OSError("private database error")

    # Act
    row, _ = _batch(native, reader=unavailable)
    # Assert: the ledger outage blinds the handshake, but the fenced
    # byte-delta work evidence and the row's own liveness survive it.
    assert (
        row["observation"]["handshake"]["reason"],
        row["observation"]["runtime"]["resident_state"],
        row["observation"]["runtime"]["session_jsonl_delta_bytes"] > 0,
        row["liveness"],
    ) == ("ledger_unavailable", "working", True, {"verdict": "alive"})


def test_clock_regression_keeps_ages_unknown(native):
    # Arrange
    times = iter([106, 105])
    # Act
    row, _ = _batch(native, clock=lambda: next(times))
    # Assert
    assert (
        row["observation"]["frame"]["clock"],
        row["observation"]["frame"]["observed_at"],
        row["observation"]["runtime"]["age_s"],
    ) == ("uncertain", None, None)


def test_existing_runtime_batch_reuses_birth_and_active_snapshot(native):
    # Arrange
    layout, authority = native
    store = _ReadOnlyLedger([])
    observer = partial(
        annotate_observation_rows,
        capture_fn=partial(capture_observation_authority, proc_root=layout["proc"]),
        state_dir_fn=lambda name: layout["state"],
        ledger_reader=partial(read_handshake_page, store_factory=lambda: store),
        clock=lambda: 106,
    )
    # Act
    result = annotate_runtime_rows(
        [{"name": AGENT, "host": HOST, "config": "synthetic"}],
        active_reader=lambda host=None: [layout["record"]],
        birth_reader=lambda ids: {layout["record"]["id"]: layout["birth"]},
        evidence_reader=lambda name, row: {"marker_id": layout["record"]["id"]},
        config_loader=lambda path: {"harness": "codex"},
        runtime_probe=lambda cfg: True,
        local_host=HOST,
        observation_annotator=observer,
    )[0]
    # Assert
    assert (
        result["observation"]["authority"],
        result["observation"]["runtime"]["resident_state"],
        len(store.calls),
    ) == (authority.model_dump(), "working", 1)


def test_known_hermes_launch_remains_unsupported_without_ledger_or_provider_calls(
    native,
):
    # Arrange
    layout, authority = native
    compiled = json.loads(layout["birth"]["compiled_spec_json"])
    compiled["harness"] = "hermes"
    layout["birth"]["compiled_spec_json"] = json.dumps(compiled)
    # Act
    row, store = _batch(native)
    # Assert
    assert (
        row["observation"]["handshake"]["reason"],
        row["observation"]["runtime"]["state"],
        row["liveness"],
        store.calls,
    ) == ("capability_unknown", "unknown", {"verdict": "alive"}, [])


def test_tool_output_in_native_source_does_not_finalize_pending_ledger(native):
    # Arrange: real native source already has a completed tool lifecycle.
    layout, authority = native
    pending = _record(authority, code=202)
    operation = json.loads(pending["operation"])
    operation["source_identity"] = [
        layout["rollout"].stat().st_dev,
        layout["rollout"].stat().st_ino,
    ]
    pending["operation"] = json.dumps(operation)
    original = dict(pending)
    # Act
    row, store = _batch(native, records=[pending])
    # Assert
    assert (
        row["observation"]["handshake"]["current_exchange"]["phase"],
        row["observation"]["handshake"]["last_verified_reply"],
        row["observation"]["runtime"]["resident_state"],
        pending,
        len(store.calls),
    ) == ("pending", None, "working", original, 1)


@pytest.mark.parametrize("harness", ["codex", "hermes"])
@pytest.mark.parametrize("order", [1, -1])
def test_same_name_foreign_row_cannot_borrow_local_evidence(
    native, harness, order
):
    # Arrange: the local source has real native work and a verified ledger reply.
    layout, authority = native
    compiled = json.loads(layout["birth"]["compiled_spec_json"])
    compiled["harness"] = harness
    layout["birth"]["compiled_spec_json"] = json.dumps(compiled)
    record = _record(authority)
    operation = json.loads(record["operation"])
    operation["source_identity"] = [
        layout["rollout"].stat().st_dev,
        layout["rollout"].stat().st_ino,
    ]
    record["operation"] = json.dumps(operation)
    store = _ReadOnlyLedger([record])
    rows = [
        {"name": AGENT, "host": HOST, "liveness": {"verdict": "alive"}},
        {"name": AGENT, "host": "foreign-peer", "liveness": {"verdict": "busy"}},
    ]
    # Act
    result = annotate_observation_rows(
        rows[::order],
        active=[layout["record"]],
        births={AGENT: layout["birth"]},
        local_host=HOST,
        capture_fn=partial(capture_observation_authority, proc_root=layout["proc"]),
        state_dir_fn=lambda name: layout["state"],
        ledger_reader=partial(read_handshake_page, store_factory=lambda: store),
        clock=lambda: 106,
    )
    by_host = {row["host"]: row for row in result}
    local = by_host[HOST]["observation"]
    foreign = by_host["foreign-peer"]["observation"]
    # Assert: the foreign row borrows nothing — no authority, no work
    # verdict, no handshake — while the local row keeps its own.
    assert (
        local["authority"],
        local["runtime"]["resident_state"],
        local["handshake"]["reason"],
        foreign["authority"],
        foreign["runtime"]["state"],
        foreign["runtime"]["resident_state"],
        foreign["handshake"]["reason"],
        foreign["handshake"]["last_verified_reply"],
        by_host["foreign-peer"]["liveness"],
        len(store.calls),
    ) == (
        authority.model_dump() if harness == "codex" else None,
        "working" if harness == "codex" else None,
        "" if harness == "codex" else "capability_unknown",
        None,
        "unknown",
        None,
        "authority_unknown",
        None,
        {"verdict": "busy"},
        1 if harness == "codex" else 0,
    )
