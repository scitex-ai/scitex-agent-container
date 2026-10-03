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
    # Assert
    assert (
        evidence["authority"]["source"]["identity"],
        _cache(layout)["activity_source_id"],
        evidence["runtime"]["tools_started"],
        evidence["runtime"]["tools_completed"],
        evidence["runtime"]["progress_stale_s"],
        len(store.calls),
    ) == (authority.source.identity, authority.source.identity, 1, 1, None, 1)


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
    # Assert
    assert (result.state, result.tools_started, result.tools_completed) == (
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


def test_expired_work_lease_keeps_facts_and_counters_unknown(native):
    # Arrange
    layout, authority = native
    # Act
    result = runtime_observation(_cache(layout), authority, now=191)
    # Assert
    assert (
        result.state,
        result.age_s,
        result.heartbeat_lease_s,
        result.lease_expired,
        result.tools_started,
        result.lease_remaining_s,
    ) == ("unknown", 91, 90, True, None, -1)


@pytest.mark.parametrize(
    "threshold, stale", [(None, None), (96, False), (95, True), (0, True)]
)
def test_progress_uses_only_supplied_threshold_independently(native, threshold, stale):
    # Arrange
    layout, authority = native
    # Act
    result = runtime_observation(
        _cache(layout), authority, now=110, progress_stale_s=threshold
    )
    # Assert
    assert (
        result.progress_age_s,
        result.progress_stale_s,
        result.progress_is_stale,
        result.tools_completed,
    ) == (96, threshold, stale, 1)


def test_missing_native_source_stamp_keeps_work_unknown(native):
    # Arrange
    layout, authority = native
    heartbeat = _cache(layout)
    del heartbeat["activity_source_id"]
    # Act
    result = runtime_observation(heartbeat, authority, now=106)
    # Assert
    assert (result.state, result.authority, result.tools_started) == (
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
    # Assert
    assert (
        row["observation"]["handshake"]["reason"],
        row["observation"]["runtime"]["tools_completed"],
        row["liveness"],
    ) == ("ledger_unavailable", 1, {"verdict": "alive"})


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
        result["observation"]["runtime"]["tools_completed"],
        len(store.calls),
    ) == (authority.model_dump(), 1, 1)


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
        row["observation"]["runtime"]["tools_completed"],
        pending,
        len(store.calls),
    ) == ("pending", None, 1, original, 1)


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
    # Assert
    assert (
        local["authority"],
        local["runtime"]["tools_completed"],
        local["handshake"]["reason"],
        foreign["authority"],
        foreign["runtime"]["state"],
        foreign["runtime"]["tools_completed"],
        foreign["handshake"]["reason"],
        foreign["handshake"]["last_verified_reply"],
        by_host["foreign-peer"]["liveness"],
        len(store.calls),
    ) == (
        authority.model_dump() if harness == "codex" else None,
        1 if harness == "codex" else None,
        "" if harness == "codex" else "capability_unknown",
        None,
        "unknown",
        None,
        "authority_unknown",
        None,
        {"verdict": "busy"},
        1 if harness == "codex" else 0,
    )
