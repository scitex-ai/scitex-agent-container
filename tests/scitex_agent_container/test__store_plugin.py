"""sac's leaf declaration for multi-host sync: what its state MEANS.

These tests deliberately EXERCISE ``scitex_dev.store.merge_field`` with real
values rather than asserting that a declaration contains a particular enum
member. Asserting ``policy.merge is MergeRule.MAX`` only proves this module
says MAX; running the merge proves a stale replica cannot roll a live host's
heartbeat backwards, which is the property anyone actually depends on.

No mocks (PA-306): the real primitive, the real policies, the real
entry-point metadata. AAA markers, one assertion per test.
"""

from __future__ import annotations

from importlib.metadata import entry_points

import pytest
from scitex_dev.store import HLC, MergeRule, merge_field

from scitex_agent_container._state.state_db import KNOWN_TABLES
from scitex_agent_container._store_plugin import (
    CLASSIFIED,
    INSTANCES,
    LINEAGE,
    NEVER_SYNCED,
    SOURCE_TABLE,
    Truth,
    provide,
)

_GROUP = "scitex_dev.store.plugins"


def _stamp(wall_us: int, node: str = "compute-04") -> HLC:
    return HLC(wall_us=wall_us, logical=0, node=node)


# ---------------------------------------------------------------------------
# Completeness — the anti-omission gate.
# ---------------------------------------------------------------------------


def test_every_known_table_has_an_explicit_sync_decision():
    # Arrange: the failure this guards is a NEW table added to the state DB
    # that nobody classified. It would then be silently absent from sync,
    # which looks identical to a table deliberately excluded. Forcing every
    # KNOWN_TABLES entry into exactly one of the two sets makes the omission
    # a red test instead of a quiet gap.
    decided = set(SOURCE_TABLE.values()) | set(NEVER_SYNCED)
    # Act
    undecided = sorted(set(KNOWN_TABLES) - decided)
    # Assert
    assert undecided == []


def test_no_table_is_both_replicated_and_never_synced():
    # Arrange: the two sets are a partition, not overlapping opinions.
    replicated = set(SOURCE_TABLE.values())
    # Act
    both = sorted(replicated & set(NEVER_SYNCED))
    # Assert
    assert both == []


def test_every_refusal_states_why():
    # Arrange: mirrors the _system_deps doctrine — the REASON is the point.
    # "channel_events: excluded" would satisfy a non-empty check and carry
    # nothing a future reader could act on.
    # Act
    thin = sorted(t for t, why in NEVER_SYNCED.items() if len(why.split()) < 12)
    # Assert
    assert thin == []


# ---------------------------------------------------------------------------
# PER-HOST TRUTH — two hosts disagreeing must not be a conflict at all.
# ---------------------------------------------------------------------------


def test_host_is_part_of_the_instance_identity():
    # Arrange: this single fact IS the per-host-truth mechanism. With host in
    # the key, compute-04's observation and spartan's observation are
    # different RECORDS, so no merge rule ever has to choose between two true
    # observations of two different processes.
    schema = INSTANCES
    # Act
    identity = schema.identity_fields
    # Assert
    assert "host" in identity


def test_the_field_the_per_host_identity_depends_on_is_actually_written(tmp_path):
    """A column declared in a schema and never populated is inert.

    This fleet found four such things in one night — scitex_dev.store with
    zero callers, spec.provider:openai with no runner, a freshness value
    hardcoded so it cannot fail, and scitex-cards' own ``origin_node``,
    declared at SCHEMA_VERSION 11 and written by nothing. Reading a schema
    tells you a field is PRESENT, never that it is FILLED.

    ``sac_instances`` puts ``host`` in the record identity, so if sac did not
    actually write that column the whole per-host classification would rest on
    an empty string and every host's rows would collide into one anonymous
    record. This exercises the real writer against a real (temporary) DB with
    no host argument, so it also proves the DEFAULT populates it — a value
    that only appears when a caller remembers to pass it is not an origin.
    """
    # Arrange
    from scitex_agent_container._state.state_db import record_instance_start
    from scitex_agent_container._state.state_db_export import export_state

    db = tmp_path / "state.db"
    record_instance_start("agent-under-test", db_path=db)
    # Act
    rows = export_state(db_path=db)["tables"]["instances"]
    # Assert
    assert (rows[0]["host"] or "").strip() != ""


def test_a_stale_replica_cannot_roll_a_heartbeat_backwards():
    # Arrange: the live host observed 12:00:05. An older sample (12:00:01)
    # arrives LATE, so it carries the HIGHER hlc — under LAST_WRITER_WINS it
    # would win and rewind a live agent. MAX makes the field a high-water
    # mark, so delivery order cannot rewrite observation order.
    policy = INSTANCES.fields["last_heartbeat_at"]
    # Act
    outcome = merge_field(
        "last_heartbeat_at",
        policy,
        current="2026-08-12T12:00:05Z",
        current_stamp=_stamp(1_000),
        incoming="2026-08-12T12:00:01Z",
        incoming_stamp=_stamp(9_999),
    )
    # Assert
    assert outcome.value == "2026-08-12T12:00:05Z"


def test_monotone_counters_never_decrease():
    # Arrange: iter_count is monotone; a late-delivered lower sample must not
    # subtract work the agent demonstrably did.
    policy = INSTANCES.fields["iter_count"]
    # Act
    outcome = merge_field(
        "iter_count",
        policy,
        current=412,
        current_stamp=_stamp(1_000),
        incoming=7,
        incoming_stamp=_stamp(9_999),
    )
    # Assert
    assert outcome.value == 412


def test_a_second_different_end_time_is_reported_not_believed():
    # Arrange: the 2026-08-11 incident — eleven rows stamped with one
    # now_iso() evaluated once per GC sweep, read by three separate readers
    # as a simultaneous mass kill when the agents had died 10h46m earlier. A
    # process ends ONCE, so a second differing end time is a contradiction,
    # and IMMUTABLE surfaces it as a conflict instead of overwriting.
    policy = INSTANCES.fields["ended_at"]
    # Act
    outcome = merge_field(
        "ended_at",
        policy,
        current="2026-08-11T07:08:00Z",
        current_stamp=_stamp(1_000),
        incoming="2026-08-11T17:54:26Z",
        incoming_stamp=_stamp(9_999),
    )
    # Assert
    assert outcome.conflict is not None


def test_the_original_end_time_survives_that_conflict():
    # Arrange: reporting a conflict is only half the guarantee — the first
    # observation must also still be the one presented.
    policy = INSTANCES.fields["ended_at"]
    # Act
    outcome = merge_field(
        "ended_at",
        policy,
        current="2026-08-11T07:08:00Z",
        current_stamp=_stamp(1_000),
        incoming="2026-08-11T17:54:26Z",
        incoming_stamp=_stamp(9_999),
    )
    # Assert
    assert outcome.value == "2026-08-11T07:08:00Z"


# ---------------------------------------------------------------------------
# HISTORY — merging must never lose a branch.
# ---------------------------------------------------------------------------


def test_two_hosts_claiming_different_parents_is_a_reported_conflict():
    # Arrange: the ACL derives group membership from lineage
    # (check_lineage_acl), so a silent rewrite of the family tree is a silent
    # privilege change. LAST_WRITER_WINS would take whichever host spoke
    # last; IMMUTABLE reports the contradiction carrying BOTH values.
    policy = LINEAGE.fields["parent_name"]
    # Act
    outcome = merge_field(
        "parent_name",
        policy,
        current="scitex-dev",
        current_stamp=_stamp(1_000),
        incoming="scitex-cards",
        incoming_stamp=_stamp(9_999),
    )
    # Assert
    assert outcome.conflict is not None


def test_the_rejected_parent_is_named_in_the_conflict():
    # Arrange: a conflict that says only "there was a conflict" cannot be
    # acted on. The report must carry what was dropped.
    policy = LINEAGE.fields["parent_name"]
    # Act
    outcome = merge_field(
        "parent_name",
        policy,
        current="scitex-dev",
        current_stamp=_stamp(1_000),
        incoming="scitex-cards",
        incoming_stamp=_stamp(9_999),
    )
    # Assert
    assert outcome.conflict.rejected == "scitex-cards"


def test_each_child_is_its_own_record_so_no_branch_can_collide():
    # Arrange: "merging must never lose a branch" reduces to keying the edge
    # by the child. Distinct children are then distinct records and a union
    # can drop none of them.
    schema = LINEAGE
    # Act
    identity = schema.identity_fields
    # Assert
    assert identity == ("child_name",)


# ---------------------------------------------------------------------------
# Refusals that are security-relevant.
# ---------------------------------------------------------------------------


def test_bearer_tokens_are_never_replicated():
    # Arrange: node_tokens is the authenticated-identity primitive. Copying
    # it to every host turns one host's compromise into the fleet's.
    # Act
    refused = "node_tokens" in NEVER_SYNCED
    # Assert
    assert refused


def test_the_sse_cursor_table_is_never_replicated():
    # Arrange: channel_events.id IS the Last-Event-ID a client resumes from.
    # Interleaving another host's numbering makes "resume from N" mean
    # something different, so a reconnecting client skips or replays frames
    # with no error raised anywhere.
    # Act
    refused = "channel_events" in NEVER_SYNCED
    # Assert
    assert refused


# ---------------------------------------------------------------------------
# The classification itself.
# ---------------------------------------------------------------------------


def test_instances_is_the_per_host_classification():
    # Arrange
    _schema, truth, _policy = CLASSIFIED["sac_instances"]
    # Act
    got = truth
    # Assert
    assert got is Truth.PER_HOST


def test_the_directory_is_fleet_truth():
    # Arrange: "agent X is reachable at host:port" is the same claim on every
    # host, so disagreement here IS a conflict and needs a stated rule.
    _schema, truth, _policy = CLASSIFIED["sac_comms_nodes"]
    # Act
    got = truth
    # Assert
    assert got is Truth.FLEET


def test_a_declared_identity_field_is_never_last_writer_wins():
    # Arrange: an identity field that could be overwritten would silently
    # re-key a record, which is indistinguishable from losing it.
    offenders = [
        f"{name}.{field}"
        for name, (schema, _t, _w) in CLASSIFIED.items()
        for field in schema.identity_fields
        if schema.fields[field].merge is MergeRule.LAST_WRITER_WINS
    ]
    # Act
    got = sorted(offenders)
    # Assert
    assert got == []


def test_the_tombstone_column_is_not_redeclared():
    # Arrange: comms_nodes.ended_at was a hand-rolled soft tombstone that
    # INSERT OR IGNORE could never propagate. The primitive's hide() is "the
    # ONLY removal" and replicates as an op, so keeping a second copy of the
    # concept would let the two drift.
    schema, _t, _w = CLASSIFIED["sac_comms_nodes"]
    # Act
    declared = sorted(schema.fields)
    # Assert
    assert "ended_at" not in declared


# ---------------------------------------------------------------------------
# Federation wiring.
# ---------------------------------------------------------------------------


@pytest.fixture
def sac_entry_point():
    """The installed entry point, or skip when the package is not installed."""
    eps = [ep for ep in entry_points(group=_GROUP) if ep.name == "scitex-agent-container"]
    if not eps:
        pytest.skip("scitex-agent-container entry point not installed in this env")
    return eps[0]


@pytest.fixture
def federation_available():
    """Skip unless scitex-dev has landed the StorePlugin federation."""
    try:
        from scitex_dev.store import StorePlugin  # noqa: F401
    except ImportError:
        pytest.skip("scitex_dev.store.StorePlugin not shipped yet")
    return True


def test_the_entry_point_is_registered(sac_entry_point):
    # Arrange: the module existing is not enough — discover_store_plugins
    # finds leaves through the entry-point group, so an unregistered provider
    # is invisible no matter how well it is written.
    ep = sac_entry_point
    # Act
    name = ep.name
    # Assert
    assert name == "scitex-agent-container"


def test_the_entry_point_resolves_to_this_provider(sac_entry_point):
    # Arrange: a registered name that loads something else is worse than an
    # absent one, because it looks correct in the metadata.
    ep = sac_entry_point
    # Act
    loaded = ep.load()
    # Assert
    assert loaded is provide


@pytest.fixture
def federation_absent():
    """Skip unless the StorePlugin federation is still unshipped."""
    try:
        from scitex_dev.store import StorePlugin  # noqa: F401
    except ImportError:
        return True
    pytest.skip("federation present; the absent-path contract is not exercisable")


def test_provide_raises_rather_than_returning_an_empty_list(federation_absent):
    # Arrange: scitex_dev.store 0.47.0 ships the primitive but NOT the
    # StorePlugin federation. A leaf that degraded to "no plugins" would be
    # indistinguishable from a leaf that declares nothing — the exact
    # success-shaped failure this work exists to remove. When the federation
    # is absent the answer must be an exception, never a quiet [].
    provider = provide
    # Act
    caught = pytest.raises(ImportError)
    # Assert
    with caught:
        provider()


def test_provide_declares_every_classified_schema(federation_available):
    # Arrange: the federation dedups on plugin name, so a leaf that silently
    # dropped one of its schemas would simply be absent from the merge with
    # nothing reporting it.
    plugins = provide()
    # Act
    names = sorted(p.name for p in plugins)
    # Assert
    assert names == sorted(CLASSIFIED)
