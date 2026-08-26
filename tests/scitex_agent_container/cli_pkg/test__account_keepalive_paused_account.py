"""A PAUSED account must not make the credential alarm red. Ever.

THIS IS THE OPERATOR'S ACTUAL REQUIREMENT, 2026-08-26::

    …また アカウント復活させるので…クオーターを見ながら無駄遣いを
    しないように止めたり再開したりしてるんですけど、なのでその休止の
    間も失敗しないようにしてほしいんですよ。

"…so I'd like it not to FAIL during the pause either." Everything else
in this change is machinery; this file is the requirement.

WHAT IT REPLACES. ``sac accounts send-credentials --all`` enumerates
every account this host holds refresh material for. A rested account
still holds refresh material, so it was still enumerated, and minting
from it raised ``MintError`` -> ``KeepaliveError`` on every peer, which
set the run's single ``failed`` boolean, which exited 1. Measured
2026-08-26: ``wyusuuke-gmail-com`` reads FORBIDDEN (403,
``oauth_not_allowed_for_organization``) and the unit had been failing
on it on every pass. One account the operator had deliberately stopped
was pinning the fleet's credential alarm red permanently — and a
signal that is always red is one nobody reads. That is the same
sentence ``--optional-peer`` was written for, one axis over: that flag
forgives an intermittent PEER; nothing forgave an ACCOUNT whose absence
is intended.

HOW THE GATE IS MADE ABLE TO FAIL, and what it honestly cannot show
-------------------------------------------------------------------
Every push in this file fails, because no peer here is reachable: the
target is a name absent from ``config.yaml``, which
``resolve_peer_transport`` refuses with ``UnknownPeerError`` before any
ssh is attempted. That keeps the tests offline and deterministic, and
it constrains what the EXIT CODE can prove:

* WHEN EVERY ACCOUNT IS PAUSED there is nothing left to push, so the
  exit code is the whole answer:
  :func:`test_pausing_every_account_exits_zero` versus its control
  :func:`test_without_the_pauses_the_same_store_exits_one`. Same store,
  same argv, the pause files the only difference — 0 against 1.

* WHEN ONE ACCOUNT SURVIVES the run still exits 1, because the
  SURVIVOR's push has nowhere to go. An exit-0-with-a-survivor test
  cannot be written offline: it would need a peer that accepts a real
  credential. So the mechanism is measured where it actually lives
  instead — the paused account must contribute NOTHING to the failure
  tally, which is asserted as: it is never attempted, it produces no
  FAILED line, and the run's FAILED lines number one rather than two.
  Each has a control that reverses when the pause file is removed.
  This is stated rather than papered over: no assertion here claims
  the exit code proves the survivor case.

An earlier draft declared the unreachable peer with
``--optional-peer`` so that everything was forgiven and the run exited
0 with a survivor. Its control — the same store with no pause — ALSO
exited 0, which is how the false gate was caught: the 0 was the
optional-peer tolerance, not the pause. The flag is gone for that
reason.

NO MOCKS (PA-306) and no ``monkeypatch``: real account directories with
real credential snapshots on a real ``tmp_path``, and click's own
``CliRunner(env=...)`` isolation to point the store at them.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from scitex_agent_container._creds._pause import Pause, write_pause

ALPHA = "alpha-example-com"
BETA = "beta-example-com"
_ABSENT_PEER = "peer-not-in-config"


def _write_account(store: Path, name: str) -> Path:
    """A real account dir holding REFRESH material, so ``--all`` enumerates it."""
    account_dir = store / name
    account_dir.mkdir(parents=True, exist_ok=True)
    (account_dir / ".credentials.json").write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "access-not-a-real-token",
                    "refreshToken": "refresh-not-a-real-token",
                    "expiresAt": int((time.time() + 8 * 3600) * 1000),
                }
            }
        )
    )
    return account_dir


def _write_pause(account_dir: Path, name: str, reason: str, *, ago: float = 0.0):
    written = write_pause(
        account_dir,
        Pause(
            name=name,
            active=True,
            reason=reason,
            since=time.time() - ago,
            by="tester@test-host",
        ),
    )
    assert written, "fixture failed to write the pause record"


@pytest.fixture
def keepalive_cli() -> click.Group:
    """A bare group with the real command registered onto it."""
    from scitex_agent_container.cli_pkg._account_keepalive import (
        register_keepalive_command,
    )

    @click.group()
    def group():
        pass

    register_keepalive_command(group)
    return group


@pytest.fixture
def store(tmp_path: Path) -> Path:
    path = tmp_path / ".scitex" / "agent-container" / "accounts"
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    """Point the store cascade at the tmp_path — click's own env isolation."""
    return {"HOME": str(tmp_path), "SCITEX_DIR": str(tmp_path / ".scitex")}


def _run(cli: click.Group, env: dict[str, str], *extra: str):
    return CliRunner().invoke(
        cli,
        ["send-credentials", "--all", "--to", _ABSENT_PEER, *extra],
        env=env,
    )


@pytest.fixture
def two_accounts(store: Path) -> Path:
    _write_account(store, ALPHA)
    _write_account(store, BETA)
    return store


# ---------------------------------------------------------------------------
# EVERY account rested — where the exit code is the whole answer
# ---------------------------------------------------------------------------


@pytest.fixture
def result_with_everything_paused(keepalive_cli, env, two_accounts: Path):
    _write_pause(two_accounts / ALPHA, ALPHA, "resting alpha")
    _write_pause(two_accounts / BETA, BETA, "resting beta")
    return _run(keepalive_cli, env)


@pytest.fixture
def result_with_nothing_paused(keepalive_cli, env, two_accounts: Path):
    return _run(keepalive_cli, env)


def test_pausing_every_account_exits_zero(result_with_everything_paused):
    """THE REQUIREMENT. 「その休止の間も失敗しないようにしてほしい」."""
    # Arrange
    result = result_with_everything_paused
    # Act
    exit_code = result.exit_code
    # Assert
    assert exit_code == 0


def test_without_the_pauses_the_same_store_exits_one(result_with_nothing_paused):
    """The gate can fail. Same store, same argv, no pause files — red.

    Without this, a change that exited 0 unconditionally would pass the
    test above and look exactly like the feature working.
    """
    # Arrange
    result = result_with_nothing_paused
    # Act
    exit_code = result.exit_code
    # Assert
    assert exit_code == 1


def test_pausing_every_account_says_that_is_the_intended_state(
    result_with_everything_paused,
):
    """An empty list because the operator emptied it is not a failure, and the
    run has to say which of the two empties this is."""
    # Arrange
    output = result_with_everything_paused.output
    # Act
    said = "are PAUSED — nothing to push" in output
    # Assert
    assert said is True


@pytest.fixture
def result_on_a_host_that_is_not_the_origin(keepalive_cli, env, store: Path):
    """An account store with no refresh material anywhere in it."""
    return _run(keepalive_cli, env)


def test_a_host_that_is_not_the_origin_still_exits_one(
    result_on_a_host_that_is_not_the_origin,
):
    """The pair that stops "empty means success" from swallowing the guard.

    Two empty lists, opposite verdicts: a host that holds refresh
    material for nobody cannot keep any peer alive and must say so,
    while a host whose accounts are all paused is doing exactly what it
    was told. Collapsing them would re-create the always-red bug in the
    one case where the operator has paused everything.
    """
    # Arrange
    result = result_on_a_host_that_is_not_the_origin
    # Act
    exit_code = result.exit_code
    # Assert
    assert exit_code == 1


def test_a_host_that_is_not_the_origin_says_why(
    result_on_a_host_that_is_not_the_origin,
):
    # Arrange
    output = result_on_a_host_that_is_not_the_origin.output
    # Act
    said = "not the origin" in output
    # Assert
    assert said is True


# ---------------------------------------------------------------------------
# ONE account rested, one still running — the partition itself
# ---------------------------------------------------------------------------


@pytest.fixture
def result_with_beta_paused(keepalive_cli, env, two_accounts: Path):
    _write_pause(two_accounts / BETA, BETA, "quota rest", ago=3 * 86400)
    return _run(keepalive_cli, env)


def test_a_paused_account_is_never_attempted(result_with_beta_paused):
    """Skip, not tolerate. No mint, no ssh, no FAILED line — nothing at all.

    Tolerating would still open one connection and print one FAILED line
    per peer per pass, which is the 115 MB journal the operator is
    looking at. Tolerance is the right verb for a peer that MIGHT be up;
    skip is the right verb for an account he has decided is down.
    """
    # Arrange
    output = result_with_beta_paused.output
    # Act
    attempted = f"{BETA}: pushing" in output
    # Assert
    assert attempted is False


def test_without_the_pause_that_account_is_attempted(result_with_nothing_paused):
    """The control for the assertion above."""
    # Arrange
    output = result_with_nothing_paused.output
    # Act
    attempted = f"{BETA}: pushing" in output
    # Assert
    assert attempted is True


def test_a_paused_account_contributes_no_failure(result_with_beta_paused):
    """The failure tally is what `failed` is computed from, so this is the
    closest an offline test gets to the exit code itself."""
    # Arrange
    output = result_with_beta_paused.output
    # Act
    failures = [line for line in output.splitlines() if ": FAILED" in line]
    # Assert
    assert len(failures) == 1


def test_without_the_pause_both_accounts_contribute_failures(
    result_with_nothing_paused,
):
    """The control: two accounts, two failures, which is today's red unit."""
    # Arrange
    output = result_with_nothing_paused.output
    # Act
    failures = [line for line in output.splitlines() if ": FAILED" in line]
    # Assert
    assert len(failures) == 2


def test_the_surviving_account_is_still_attempted(result_with_beta_paused):
    """Without this, a bug that skipped EVERY account would pass the rest."""
    # Arrange
    output = result_with_beta_paused.output
    # Act
    attempted = f"{ALPHA}: pushing" in output
    # Assert
    assert attempted is True


# ---------------------------------------------------------------------------
# The skip must be LOUD — he intends to bring these accounts back
# ---------------------------------------------------------------------------


def test_the_run_names_the_skipped_account(result_with_beta_paused):
    # Arrange
    output = result_with_beta_paused.output
    # Act
    named = f"{BETA}: SKIPPED — paused" in output
    # Assert
    assert named is True


def test_the_run_quotes_the_operators_reason(result_with_beta_paused):
    # Arrange
    output = result_with_beta_paused.output
    # Act
    quoted = "quota rest" in output
    # Assert
    assert quoted is True


def test_the_run_prints_how_long_the_pause_has_stood(result_with_beta_paused):
    """The age stands in for the expiry this record deliberately lacks.

    Nothing will ever lift a pause on its own, so the only thing between
    a deliberate rest and a forgotten account is that every run says how
    old it is.
    """
    # Arrange
    output = result_with_beta_paused.output
    # Act
    aged = "paused 3d ago" in output
    # Assert
    assert aged is True


def test_the_run_prints_the_command_that_lifts_it(result_with_beta_paused):
    # Arrange
    output = result_with_beta_paused.output
    # Act
    told = f"sac accounts resume {BETA}" in output
    # Assert
    assert told is True


def test_the_run_says_the_skip_was_not_a_failure(result_with_beta_paused):
    """A line the operator scans must not READ like a soft error."""
    # Arrange
    output = result_with_beta_paused.output
    # Act
    said = "Nothing pushed, nothing failed." in output
    # Assert
    assert said is True


# ---------------------------------------------------------------------------
# The machine-readable record
# ---------------------------------------------------------------------------


@pytest.fixture
def json_records(keepalive_cli, env, two_accounts: Path):
    _write_pause(two_accounts / BETA, BETA, "quota rest")
    result = _run(keepalive_cli, env, "--json")
    return json.loads(result.stdout)


def test_the_json_record_marks_the_skip_as_a_pause(json_records):
    """``skipped`` names WHY, so a consumer need not parse prose."""
    # Arrange
    records = json_records
    # Act
    skipped = [r["account"] for r in records if r.get("skipped") == "paused"]
    # Assert
    assert skipped == [BETA]


def test_the_json_record_does_not_call_a_skip_a_failure(json_records):
    """The run did the right thing; ``ok`` must say so."""
    # Arrange
    records = json_records
    # Act
    skip = next(r for r in records if r.get("skipped") == "paused")
    # Assert
    assert skip["ok"] is True


def test_the_json_record_carries_the_reason(json_records):
    # Arrange
    records = json_records
    # Act
    skip = next(r for r in records if r.get("skipped") == "paused")
    # Assert
    assert skip["reason"] == "quota rest"
