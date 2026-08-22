#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for lifecycle host IDENTITY — "which machine is which".

The regression is concrete and was MEASURED on ``scitex-nas-03``
(2026-08-14, while restoring scitex-hub during a live outage): that
machine's ``hostname -s`` is the appliance's factory name
``DXP480TPLUS-994``, every spec pins ``host: scitex-nas-03``, and the
machine has no sac ``config.yaml`` at all (its path is a dangling symlink),
so ``sac agents start scitex-hub`` ON THAT MACHINE refused with

    spec.host 'scitex-nas-03' is neither this machine nor a registered peer

and proceeded only with ``--no-redispatch``, typed by hand, every time.

NO MOCKS. Every test drives the real seam: a real ``hosts.yaml`` written to
a real ``$SCITEX_DIR/dev/`` (the ecosystem local-state cascade the registry
port itself resolves), a real absent ``config.yaml`` via
``$SCITEX_AGENT_CONTAINER_CONFIG``, and this test process's REAL
``socket.gethostname()`` standing in for the factory name — the same device
``test__common.py::TestLocalHostNames`` already uses.

Both halves are covered on purpose, because a fix to one that breaks the
other is the dangerous outcome: a pin naming THIS machine under another
name must resolve LOCAL, and a pin naming a machine that is neither this
one nor a peer must still fail LOUD.
"""

from __future__ import annotations

import os
import socket
import textwrap
from collections.abc import Iterator
from pathlib import Path

import pytest

from scitex_agent_container._state.host_registry import registry_local_names
from scitex_agent_container.cli_pkg.lifecycle._host_identity import (
    _local_host_names,
    classify_dispatch_host,
)
from scitex_agent_container.cli_pkg.lifecycle._host_routing import (
    UnknownSpecHostError,
    resolve_start_dispatch_peer,
)

#: This machine's raw short hostname — the stand-in for ``DXP480TPLUS-994``.
FACTORY_NAME = socket.gethostname().split(".")[0]

#: The fleet name the ledger gives that machine.
FLEET_NAME = "scitex-nas-03"


def _hosts_yaml(aliases: str) -> str:
    return textwrap.dedent(
        f"""\
        hosts:
          {FLEET_NAME}:
            kind: storage
            ssh_alias: {FLEET_NAME}
            aliases: [{aliases}]
            scitex_root: "~/.scitex"
          mba:
            kind: workstation
            ssh_alias: mba
            scitex_root: "~/.scitex"
        """
    )


def _install_registry(tmp_path: Path, body: str) -> Iterator[None]:
    """Install ``body`` as the real hosts.yaml and point $SCITEX_DIR at it."""
    hosts_dir = tmp_path / "dev"
    hosts_dir.mkdir(parents=True, exist_ok=True)
    (hosts_dir / "hosts.yaml").write_text(body)
    sentinel = object()
    previous: object = os.environ.get("SCITEX_DIR", sentinel)
    os.environ["SCITEX_DIR"] = str(tmp_path)
    try:
        yield
    finally:
        if previous is sentinel:
            os.environ.pop("SCITEX_DIR", None)
        else:
            os.environ["SCITEX_DIR"] = str(previous)


@pytest.fixture()
def ledger_knows_this_machine(tmp_path: Path, env_save_restore) -> Iterator[None]:
    """The nas-03 shape: the ledger claims this machine's factory hostname.

    The sac-local ``config.yaml`` is pointed at a path that does not exist —
    exactly nas-03's state, where it is a dangling symlink — so the ONLY
    authority that can connect the two names is the registry.
    """
    env_save_restore.set(
        "SCITEX_AGENT_CONTAINER_CONFIG", str(tmp_path / "absent.yaml")
    )
    yield from _install_registry(tmp_path, _hosts_yaml(f"nas-03, {FACTORY_NAME}"))


@pytest.fixture()
def ledger_does_not_know_this_machine(
    tmp_path: Path, env_save_restore
) -> Iterator[None]:
    """A registry that exists but claims nothing about this machine."""
    env_save_restore.set(
        "SCITEX_AGENT_CONTAINER_CONFIG", str(tmp_path / "absent.yaml")
    )
    yield from _install_registry(tmp_path, _hosts_yaml("nas-03"))


@pytest.fixture()
def ledger_claims_this_machine_twice(
    tmp_path: Path, env_save_restore
) -> Iterator[None]:
    """An INCONSISTENT ledger: two rows claim this machine's hostname."""
    env_save_restore.set(
        "SCITEX_AGENT_CONTAINER_CONFIG", str(tmp_path / "absent.yaml")
    )
    body = textwrap.dedent(
        f"""\
        hosts:
          box-a:
            kind: storage
            ssh_alias: box-a
            aliases: [{FACTORY_NAME}]
            scitex_root: "~/.scitex"
          box-b:
            kind: storage
            ssh_alias: box-b
            aliases: [{FACTORY_NAME}]
            scitex_root: "~/.scitex"
        """
    )
    yield from _install_registry(tmp_path, body)


# ---------------------------------------------------------------------------
# registry_local_names — the ledger pivot, in isolation.
# ---------------------------------------------------------------------------


def test_registry_pivots_from_the_factory_name_to_the_fleet_name(
    ledger_knows_this_machine,
) -> None:
    # Arrange — the ledger row claims this machine's factory hostname.
    spellings = {FACTORY_NAME}
    # Act
    names = registry_local_names(spellings)
    # Assert
    assert FLEET_NAME in names


def test_registry_also_returns_the_rows_other_aliases(
    ledger_knows_this_machine,
) -> None:
    # Arrange
    spellings = {FACTORY_NAME}
    # Act
    names = registry_local_names(spellings)
    # Assert — every spelling of that row denotes this machine.
    assert "nas-03" in names


def test_registry_says_nothing_about_an_unclaimed_machine(
    ledger_does_not_know_this_machine,
) -> None:
    # Arrange
    spellings = {FACTORY_NAME}
    # Act
    names = registry_local_names(spellings)
    # Assert — no row claims this machine, so the ledger adds no identity.
    assert names == set()


def test_registry_refuses_to_guess_when_two_rows_claim_this_machine(
    ledger_claims_this_machine_twice,
) -> None:
    # Arrange — a machine is ONE host; two claimants means answering would
    # pick a fleet identity by coin-flip.
    spellings = {FACTORY_NAME}
    # Act
    names = registry_local_names(spellings)
    # Assert
    assert names == set()


def test_registry_never_claims_identity_from_a_route(
    ledger_does_not_know_this_machine,
) -> None:
    # Arrange — being able to REACH a name is not being that name, so a
    # spelling that only matches an ssh_alias must contribute nothing.
    spellings = {"mba"}
    # Act
    names = registry_local_names(spellings) - {"mba"}
    # Assert
    assert names == set()


def test_registry_answers_nothing_for_an_empty_question(
    ledger_knows_this_machine,
) -> None:
    # Arrange
    spellings: set[str] = set()
    # Act
    names = registry_local_names(spellings)
    # Assert
    assert names == set()


# ---------------------------------------------------------------------------
# _local_host_names — the union, with the ledger as third authority.
# ---------------------------------------------------------------------------


def test_local_names_include_the_fleet_name_from_the_ledger(
    ledger_knows_this_machine,
) -> None:
    # Arrange — sac's own config.yaml is absent; only the ledger can say it.
    expected = FLEET_NAME
    # Act
    names = _local_host_names()
    # Assert
    assert expected in names


def test_local_names_still_include_the_raw_hostname(
    ledger_knows_this_machine,
) -> None:
    # Arrange
    expected = FACTORY_NAME
    # Act
    names = _local_host_names()
    # Assert
    assert expected in names


def test_local_names_unchanged_when_the_ledger_claims_nothing(
    ledger_does_not_know_this_machine,
) -> None:
    # Arrange — a host absent from the ledger must behave exactly as before.
    unexpected = FLEET_NAME
    # Act
    names = _local_host_names()
    # Assert
    assert unexpected not in names


# ---------------------------------------------------------------------------
# The measured failure, end to end: start on the machine its spec pins.
# ---------------------------------------------------------------------------


def test_pin_naming_this_machine_under_its_fleet_name_is_local(
    ledger_knows_this_machine,
) -> None:
    # Arrange — nas-03's exact situation: no peers registered anywhere.
    peers: dict = {}
    # Act
    classification = classify_dispatch_host(
        FLEET_NAME,
        FACTORY_NAME,
        peers,
        local_names=_local_host_names(FACTORY_NAME),
    )
    # Assert
    assert classification == ("local", None)


def test_start_dispatch_runs_locally_without_no_redispatch(
    ledger_knows_this_machine,
) -> None:
    # Arrange — before the fix this RAISED, and the only way through was the
    # hand-typed --no-redispatch escape, on every single start.
    local_names = _local_host_names(FACTORY_NAME)
    # Act
    peer = resolve_start_dispatch_peer(
        "scitex-hub", FLEET_NAME, FACTORY_NAME, {}, local_names=local_names
    )
    # Assert — None means "run here".
    assert peer is None


def test_a_pin_to_a_machine_that_is_not_this_one_still_fails_loud(
    ledger_knows_this_machine,
) -> None:
    # Arrange — the case that MUST keep failing: a host that is neither this
    # machine (the ledger does not claim it for us) nor a registered peer.
    local_names = _local_host_names(FACTORY_NAME)
    # Act
    # Assert
    with pytest.raises(UnknownSpecHostError):
        resolve_start_dispatch_peer(
            "scitex-hub",
            "some-other-box",
            FACTORY_NAME,
            {},
            local_names=local_names,
        )


def test_the_loud_refusal_names_the_identities_it_checked(
    ledger_knows_this_machine,
) -> None:
    # Arrange — "neither this machine nor a registered peer" is true and
    # unhelpful when the machine IS the pinned host under another name, so
    # the refusal must show what this machine does answer to.
    local_names = _local_host_names(FACTORY_NAME)
    # Act
    try:
        resolve_start_dispatch_peer(
            "scitex-hub",
            "some-other-box",
            FACTORY_NAME,
            {},
            local_names=local_names,
        )
        message = ""
    except UnknownSpecHostError as exc:
        message = str(exc)
    # Assert
    assert f"This machine answers to: " in message  # noqa: F541


def test_the_loud_refusal_lists_the_fleet_name_among_them(
    ledger_knows_this_machine,
) -> None:
    # Arrange
    local_names = _local_host_names(FACTORY_NAME)
    # Act
    try:
        resolve_start_dispatch_peer(
            "scitex-hub",
            "some-other-box",
            FACTORY_NAME,
            {},
            local_names=local_names,
        )
        message = ""
    except UnknownSpecHostError as exc:
        message = str(exc)
    # Assert
    assert FLEET_NAME in message


def test_the_loud_refusal_points_at_the_registry_as_the_fix(
    ledger_does_not_know_this_machine,
) -> None:
    # Arrange — the operator standing on nas-03 needs to be told WHERE to
    # record "this machine is scitex-nas-03", not just that it is not known.
    local_names = _local_host_names(FACTORY_NAME)
    # Act
    try:
        resolve_start_dispatch_peer(
            "scitex-hub", FLEET_NAME, FACTORY_NAME, {}, local_names=local_names
        )
        message = ""
    except UnknownSpecHostError as exc:
        message = str(exc)
    # Assert
    assert "hosts.yaml" in message


# EOF
