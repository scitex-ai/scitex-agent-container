#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for spec-sourced group authority.

This file used to be built around an ``identical_from_both_stores``
pair — one caller, one spec, two genuinely different SQLite stores, one
answer. It was written for the 2026-08-11 relocation incident (nine
``403 ACL deny`` probes at once, because the target host held no
``node_comms_policy`` row for the caller) and the 2026-08-09 escalation
(three agents read an empty per-agent shard and concluded the fleet
registry had been wiped, while the host DB was healthy).

THAT SHAPE IS GONE, and this docstring no longer claims otherwise. Both
incidents were caused by the per-agent SQLite shard, and moving
``node_comms_policy`` to one shared PostgreSQL store removes the shard
rather than testing around it. There is no longer a second store for a
caller to disagree with, so the equality the old headline asserted is
now true BY CONSTRUCTION and cannot be tested — a test for it can only
call the same function twice.

The old docstring promised the opposite: that "two tests below assert
that the stores really do still differ, so the equality tests can never
quietly decay into comparing one store with itself and proving
nothing." That guard is exactly what decayed. The tests it protected
became ``x == x``, and one of them — a container store holding no row —
asserted an emptiness that the shared store cannot produce, so it was
green only where nothing could write. They are deleted below rather
than left passing under names that promise coverage this file no longer
provides.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import pytest

from scitex_agent_container._state.state_db_acl_policy import (
    read_comms_policy,
    record_comms_policy,
)
from scitex_agent_container._state.state_db_groups import (
    is_developer,
    resolve_group_name,
    resolve_group_names,
)
from scitex_agent_container.config._group_authority import (
    group_name_from_spec,
    group_names_from_spec,
    spec_labels_for,
)

_YAML_DIRS_ENV = "SCITEX_AGENT_CONTAINER_YAML_DIRS"

SPEC_TEMPLATE = """\
apiVersion: sac/v1
kind: Agent
metadata:
  name: {name}
  labels:
    role: {role}
    groups: [{groups}]
spec:
  workdir: /tmp
"""


def _write_spec(root: Path, name: str, groups: str, role: str = "worker") -> None:
    """Author a real ``spec.yaml`` under ``root/<name>/``."""
    agent_dir = root / name
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "spec.yaml").write_text(
        SPEC_TEMPLATE.format(name=name, groups=groups, role=role)
    )


def _write_raw_spec(root: Path, name: str, text: str) -> None:
    agent_dir = root / name
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "spec.yaml").write_text(text)


@pytest.fixture()
def spec_root(tmp_path):
    """A real spec search dir, wired in through the documented env port.

    Sets ``$SCITEX_AGENT_CONTAINER_YAML_DIRS`` directly (restoring the
    prior value on teardown) rather than patching the resolver: that env
    var is precisely the mechanism that makes specs resolvable inside a
    container, so exercising it IS exercising the production path.
    """
    root = tmp_path / "agents"
    root.mkdir()
    previous = os.environ.get(_YAML_DIRS_ENV)
    os.environ[_YAML_DIRS_ENV] = str(root)
    yield root
    if previous is None:
        os.environ.pop(_YAML_DIRS_ENV, None)
    else:
        os.environ[_YAML_DIRS_ENV] = previous


@dataclass(frozen=True)
class RelocateCase:
    """One agent and its spec.

    It carried ``host_db`` and ``container_db`` until the policy table
    moved to PostgreSQL. Nothing read them after that -- ``record_comms_policy``
    stopped taking a ``db_path`` -- but they kept the file LOOKING like it
    still exercised two stores, which is how three tests went on claiming a
    container/host comparison they no longer made.
    """

    name: str


@pytest.fixture()
def relocate_case(spec_root, tmp_path) -> RelocateCase:
    """An agent whose spec is visible and whose policy row is published.

    It used to build two SQLite files: a populated one standing for the bare
    host's ``~/.scitex/agent-container/runtime/state.db``, and an empty one
    standing for the private ``/state/<agent>/state.db`` shard a SIF gets --
    the exact shape of the relocation 403. The shard is what caused that
    incident, and it no longer exists: the policy row lives in one shared
    PostgreSQL store that every caller reads. The two paths are not built any
    more because nothing could read them.
    """
    name = "t-auth-relocate"
    _write_spec(spec_root, name, groups="developer, infra")
    record_comms_policy(
        name=name,
        outbound_siblings="allow",
        outbound_parent="allow",
        inbound_siblings="allow",
        inbound_parent="allow",
        lineage_group="",
        may_spawn=True,
        group_name="developer",
        group_names=frozenset({"developer", "infra"}),
    )
    return RelocateCase(name=name)


# ---------------------------------------------------------------------------
# the published row is readable
#
# This section asserted the FAULT CONDITION: that a populated host store and
# an empty container shard really did disagree, so the equality tests further
# down could not decay into comparing one store with itself.
#
# `test_container_store_holds_no_policy_row` is deleted. It made the IDENTICAL
# call as the test below -- `read_comms_policy(name=case.name)`, no store
# argument, because there is no longer one to pass -- and asserted the
# OPPOSITE result. Against the shared store that row exists, so the read
# returns "developer" and the assertion of "" is simply false; it passed only
# where nothing could write, which made it green here and red on any writable
# primary. Two tests cannot both be right about one call, and the one that is
# right is the one that is kept.
# ---------------------------------------------------------------------------


def test_the_published_policy_row_is_readable(pg_schema: str, relocate_case):
    # Arrange
    case = relocate_case
    # Act
    policy = read_comms_policy(name=case.name)
    # Assert
    assert policy["group_name"] == "developer"


# ---------------------------------------------------------------------------
# the headline — one caller, two stores, one answer
# ---------------------------------------------------------------------------


def test_group_set_from_the_container_is_the_spec_answer(pg_schema: str, relocate_case):
    # Arrange
    case = relocate_case
    # Act
    from_container = resolve_group_names(name=case.name)
    # Assert
    assert from_container == frozenset({"developer", "infra"})


def test_developer_authority_holds_from_the_container_store(pg_schema: str, relocate_case):
    """The gate itself agrees — this is what the 403 actually turned on."""
    # Arrange
    case = relocate_case
    # Act
    allowed = is_developer(name=case.name)
    # Assert
    assert allowed is True


# ---------------------------------------------------------------------------
# tri-state: "no spec here" is never the same fact as "spec says none"
# ---------------------------------------------------------------------------


def test_absent_spec_yields_none_labels(spec_root):
    # Arrange
    name = "t-auth-absent"
    # Act
    labels = spec_labels_for(name)
    # Assert
    assert labels is None


def test_absent_spec_yields_none_group_set(spec_root):
    # Arrange
    name = "t-auth-absent"
    # Act
    groups = group_names_from_spec(name)
    # Assert
    assert groups is None


def test_absent_spec_yields_none_primary_group(spec_root):
    # Arrange
    name = "t-auth-absent"
    # Act
    primary = group_name_from_spec(name)
    # Assert
    assert primary is None


def test_spec_without_labels_yields_empty_group_set(spec_root):
    # Arrange
    name = "t-auth-bare"
    _write_raw_spec(
        spec_root,
        name,
        "apiVersion: sac/v1\nkind: Agent\nmetadata:\n  name: t-auth-bare\n"
        "spec:\n  workdir: /tmp\n",
    )
    # Act
    groups = group_names_from_spec(name)
    # Assert
    assert groups == frozenset()


def test_spec_without_labels_yields_empty_primary_group(spec_root):
    # Arrange
    name = "t-auth-bare2"
    _write_raw_spec(
        spec_root,
        name,
        "apiVersion: sac/v1\nkind: Agent\nmetadata:\n  name: t-auth-bare2\n"
        "spec:\n  workdir: /tmp\n",
    )
    # Act
    primary = group_name_from_spec(name)
    # Assert
    assert primary == ""


# ---------------------------------------------------------------------------
# label interpretation is unchanged — still the pure resolver's job
# ---------------------------------------------------------------------------


def test_every_authored_group_is_returned(spec_root):
    # Arrange
    name = "t-auth-multi"
    _write_spec(spec_root, name, groups="generalist, privileged, developer")
    # Act
    groups = group_names_from_spec(name)
    # Assert
    assert groups == frozenset({"generalist", "privileged", "developer"})


def test_primary_group_stays_first_of_the_authored_list(spec_root):
    """First-of, so an isolated solver stays OUT of the fleet mesh."""
    # Arrange
    name = "t-auth-multi2"
    _write_spec(spec_root, name, groups="generalist, privileged, developer")
    # Act
    primary = group_name_from_spec(name)
    # Assert
    assert primary == "generalist"


def test_role_derivation_still_applies_when_no_groups_authored(spec_root):
    # Arrange
    name = "t-auth-role"
    _write_raw_spec(
        spec_root,
        name,
        "apiVersion: sac/v1\nkind: Agent\nmetadata:\n  name: t-auth-role\n"
        "  labels:\n    role: project-maintainer\nspec:\n  workdir: /tmp\n",
    )
    # Act
    primary = group_name_from_spec(name)
    # Assert
    assert primary == "developer"


def test_spec_outside_the_fleet_dirs_is_not_consulted(spec_root, tmp_path):
    """Authority reads fleet scope ONLY.

    A spec that exists on disk but in a directory the operator did not
    put on the fleet search path must not grant anything. This is what
    keeps a project-local ``.scitex/agent-container/agents/<self>/spec.yaml``
    — a file inside a repository the agent itself edits — from deciding
    that agent's own ACL groups.
    """
    # Arrange
    elsewhere = tmp_path / "not-the-fleet-dir"
    elsewhere.mkdir()
    _write_spec(elsewhere, "t-auth-elsewhere", groups="developer")
    # Act
    groups = group_names_from_spec("t-auth-elsewhere")
    # Assert
    assert groups is None


def test_malformed_spec_falls_back_instead_of_raising(spec_root):
    """A broken spec must never raise into an ACL path nor invent a grant."""
    # Arrange
    name = "t-auth-broken"
    _write_raw_spec(spec_root, name, "metadata: [this is not: a mapping\n")
    # Act
    groups = group_names_from_spec(name)
    # Assert
    assert groups is None


# ---------------------------------------------------------------------------
# precedence — spec replaces the row; foreign nodes still use the row
# ---------------------------------------------------------------------------


@pytest.fixture()
def stale_row_case(spec_root, tmp_path) -> RelocateCase:
    """A spec that has DROPPED developer, over a row that still grants it."""
    name = "t-auth-revoked"
    _write_spec(spec_root, name, groups="generalist")
    record_comms_policy(
        name=name,
        outbound_siblings="allow",
        outbound_parent="allow",
        inbound_siblings="allow",
        inbound_parent="allow",
        lineage_group="",
        may_spawn=True,
        group_name="developer",
        group_names=frozenset({"developer"}),
    )
    return RelocateCase(name=name)


def test_stale_row_still_records_the_revoked_group(pg_schema: str, stale_row_case):
    # Arrange
    case = stale_row_case
    # Act
    policy = read_comms_policy(name=case.name)
    # Assert
    assert policy["group_name"] == "developer"


def test_spec_revocation_beats_the_stale_row(pg_schema: str, stale_row_case):
    """Deleting a group from the spec actually revokes it."""
    # Arrange
    case = stale_row_case
    # Act
    groups = resolve_group_names(name=case.name)
    # Assert
    assert groups == frozenset({"generalist"})


def test_revoked_developer_authority_is_actually_denied(pg_schema: str, stale_row_case):
    # Arrange
    case = stale_row_case
    # Act
    allowed = is_developer(name=case.name)
    # Assert
    assert allowed is False


@pytest.fixture()
def foreign_node_case(spec_root, tmp_path) -> RelocateCase:
    """A node with a policy row but NO spec — a remote / federated peer."""
    name = "t-auth-foreign"
    record_comms_policy(
        name=name,
        outbound_siblings="allow",
        outbound_parent="allow",
        inbound_siblings="allow",
        inbound_parent="allow",
        lineage_group="",
        may_spawn=True,
        group_name="researcher",
        group_names=frozenset({"researcher"}),
    )
    return RelocateCase(name=name)


def test_foreign_node_has_no_visible_spec(pg_schema: str, foreign_node_case):
    # Arrange
    case = foreign_node_case
    # Act
    groups = group_names_from_spec(case.name)
    # Assert
    assert groups is None


def test_foreign_node_falls_back_to_its_policy_row(pg_schema: str, foreign_node_case):
    # Arrange
    case = foreign_node_case
    # Act
    groups = resolve_group_names(name=case.name)
    # Assert
    assert groups == frozenset({"researcher"})


def test_foreign_node_primary_group_falls_back_to_its_row(pg_schema: str, foreign_node_case):
    # Arrange
    case = foreign_node_case
    # Act
    primary = resolve_group_name(name=case.name)
    # Assert
    assert primary == "researcher"
