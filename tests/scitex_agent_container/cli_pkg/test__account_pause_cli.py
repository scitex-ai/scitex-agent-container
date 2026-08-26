"""``sac accounts pause`` / ``resume`` — the operator's own switch.

OPERATOR REQUEST 2026-08-26. He offered 「除外」 (exclusion) and rejected
it, in the same sentence, for 「休止」 — a PAUSE. These two verbs are
that word, and 「また復活させる」 is why ``resume`` exists at all: he
intends to bring the accounts back, so stopping one must cost one
command and lifting it must cost one command.

WHAT THESE TESTS PIN, beyond "the file gets written":

* ``--reason`` IS REQUIRED, whitespace included. This record has no
  expiry, so nothing will ever tidy up a pause he has forgotten; the
  reason is the only thing that distinguishes, months later, a
  deliberate rest from an abandoned account. The fleet already ruled
  this way on ``scitex-cards``' ``parked`` field, in nearly the same
  words: "a park with no stated reason is exactly the abandonment the
  sweep should still catch."

* THE ``--yes`` GATE CAN FAIL, AND CAN SUCCEED. Pausing the last VALID
  account leaves the picker with nothing and fails every agent boot, so
  it is refused; ``--yes`` proceeds. Both directions are tested,
  because a gate that only ever passes is not a gate.

* ``resume`` STATES THE RESULT RATHER THAN IMPLYING IT. Lifting a pause
  from an account whose subscription is still cancelled gives you a
  FORBIDDEN account, not a working one. An operator told "resumed" who
  then watches it keep failing has been answered about the wrong
  question.

NO MOCKS (PA-306), no ``monkeypatch``: real account directories on a
real ``tmp_path``, and click's own ``CliRunner(env=...)`` isolation to
point the store cascade at them.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from scitex_agent_container._creds._entitlement import (
    FORBIDDEN,
    Entitlement,
    write_entitlement,
)
from scitex_agent_container._creds._pause import pause_path, read_pause

ALPHA = "alpha-example-com"
BETA = "beta-example-com"


def _write_account(store: Path, name: str, *, hours_left: float = 8.0) -> Path:
    account_dir = store / name
    account_dir.mkdir(parents=True, exist_ok=True)
    (account_dir / ".credentials.json").write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "access-not-a-real-token",
                    "refreshToken": "refresh-not-a-real-token",
                    "expiresAt": int((time.time() + hours_left * 3600) * 1000),
                }
            }
        )
    )
    (account_dir / "account.json").write_text(json.dumps({"name": name}))
    return account_dir


@pytest.fixture
def pause_cli() -> click.Group:
    from scitex_agent_container.cli_pkg._account_pause import register_pause_commands

    @click.group()
    def group():
        pass

    register_pause_commands(group)
    return group


@pytest.fixture
def store(tmp_path: Path) -> Path:
    path = tmp_path / ".scitex" / "agent-container" / "accounts"
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    return {"HOME": str(tmp_path), "SCITEX_DIR": str(tmp_path / ".scitex")}


@pytest.fixture
def two_accounts(store: Path) -> Path:
    _write_account(store, ALPHA)
    _write_account(store, BETA)
    return store


@pytest.fixture
def one_account(store: Path) -> Path:
    _write_account(store, ALPHA)
    return store


# ---------------------------------------------------------------------------
# The reason is required
# ---------------------------------------------------------------------------


def test_pausing_without_a_reason_is_refused(pause_cli, env, two_accounts):
    """A pause with no reason is the same file on disk as an abandonment."""
    # Arrange
    args = ["pause", ALPHA]
    # Act
    result = CliRunner().invoke(pause_cli, args, env=env)
    # Assert
    assert result.exit_code != 0


def test_pausing_with_a_whitespace_reason_is_refused(pause_cli, env, two_accounts):
    """``--reason "   "`` satisfies click and says nothing. Refuse it here."""
    # Arrange
    args = ["pause", ALPHA, "--reason", "   "]
    # Act
    result = CliRunner().invoke(pause_cli, args, env=env)
    # Assert
    assert result.exit_code != 0


def test_a_whitespace_reason_leaves_no_record_behind(
    pause_cli, env, two_accounts: Path
):
    """The refusal must be total: no half-written pause on disk."""
    # Arrange
    args = ["pause", ALPHA, "--reason", "   "]
    # Act
    CliRunner().invoke(pause_cli, args, env=env)
    # Assert
    assert pause_path(two_accounts / ALPHA).exists() is False


# ---------------------------------------------------------------------------
# Pausing a real account
# ---------------------------------------------------------------------------


@pytest.fixture
def paused_result(pause_cli, env, two_accounts: Path):
    return CliRunner().invoke(
        pause_cli, ["pause", BETA, "--reason", "quota rest"], env=env
    )


def test_pausing_a_real_account_exits_zero(paused_result):
    # Arrange
    result = paused_result
    # Act
    exit_code = result.exit_code
    # Assert
    assert exit_code == 0


def test_pausing_records_the_reason_on_disk(paused_result, two_accounts: Path):
    # Arrange
    account_dir = two_accounts / BETA
    # Act
    stored = read_pause(BETA, account_dir)
    # Assert
    assert stored.reason == "quota rest"


def test_pausing_marks_the_account_paused_on_disk(paused_result, two_accounts: Path):
    # Arrange
    account_dir = two_accounts / BETA
    # Act
    stored = read_pause(BETA, account_dir)
    # Assert
    assert stored.active is True


def test_pausing_prints_the_path_it_wrote(paused_result, two_accounts: Path):
    """It is a PER-HOST decision, so the operator must be told which host's
    file this is — the path says it without a sentence about hosts."""
    # Arrange
    expected = str(pause_path(two_accounts / BETA))
    # Act
    output = paused_result.output
    # Assert
    assert expected in output


def test_pausing_names_the_command_that_lifts_it(paused_result):
    # Arrange
    output = paused_result.output
    # Act
    named = f"sac accounts resume {BETA}" in output
    # Assert
    assert named is True


def test_pausing_touches_no_credential(paused_result, two_accounts: Path):
    """Nothing is deleted and no token is rewritten — that is the whole point."""
    # Arrange
    creds = two_accounts / BETA / ".credentials.json"
    # Act
    payload = json.loads(creds.read_text())
    # Assert
    assert payload["claudeAiOauth"]["refreshToken"] == "refresh-not-a-real-token"


# ---------------------------------------------------------------------------
# Unknown names are refused
# ---------------------------------------------------------------------------


@pytest.fixture
def unknown_name_result(pause_cli, env, two_accounts):
    return CliRunner().invoke(
        pause_cli, ["pause", "not-an-account", "--reason", "x"], env=env
    )


def test_an_unknown_account_is_refused(unknown_name_result):
    """The store carries symlink aliases, so a plausible-but-unlisted second
    spelling of a real account exists. Refusing anything the enumerator does
    not name keeps the CLI and the listing agreeing."""
    # Arrange
    result = unknown_name_result
    # Act
    exit_code = result.exit_code
    # Assert
    assert exit_code != 0


def test_the_refusal_lists_the_accounts_that_do_exist(unknown_name_result):
    # Arrange
    output = unknown_name_result.output
    # Act
    listed = ALPHA in output and BETA in output
    # Assert
    assert listed is True


# ---------------------------------------------------------------------------
# The --yes gate, in both directions
# ---------------------------------------------------------------------------


@pytest.fixture
def last_account_without_yes(pause_cli, env, one_account: Path):
    return CliRunner().invoke(
        pause_cli, ["pause", ALPHA, "--reason", "resting everything"], env=env
    )


@pytest.fixture
def last_account_with_yes(pause_cli, env, one_account: Path):
    return CliRunner().invoke(
        pause_cli,
        ["pause", ALPHA, "--reason", "resting everything", "--yes"],
        env=env,
    )


def test_pausing_the_last_valid_account_is_refused(last_account_without_yes):
    """An all-paused store fails every agent boot. Say so before it happens."""
    # Arrange
    result = last_account_without_yes
    # Act
    exit_code = result.exit_code
    # Assert
    assert exit_code != 0


def test_the_refusal_explains_what_would_break(last_account_without_yes):
    # Arrange
    output = last_account_without_yes.output
    # Act
    explained = "last stored account" in output
    # Assert
    assert explained is True


def test_the_refusal_writes_nothing(last_account_without_yes, one_account: Path):
    # Arrange
    account_dir = one_account / ALPHA
    # Act
    exists = pause_path(account_dir).exists()
    # Assert
    assert exists is False


def test_yes_pauses_the_last_valid_account_anyway(last_account_with_yes):
    """The other half of the pair: a gate that cannot succeed is not a gate."""
    # Arrange
    result = last_account_with_yes
    # Act
    exit_code = result.exit_code
    # Assert
    assert exit_code == 0


def test_yes_actually_writes_the_record(last_account_with_yes, one_account: Path):
    # Arrange
    account_dir = one_account / ALPHA
    # Act
    stored = read_pause(ALPHA, account_dir)
    # Assert
    assert stored.active is True


# ---------------------------------------------------------------------------
# Resume
# ---------------------------------------------------------------------------


@pytest.fixture
def resumed_result(pause_cli, env, two_accounts: Path):
    CliRunner().invoke(
        pause_cli, ["pause", BETA, "--reason", "quota rest"], env=env
    )
    return CliRunner().invoke(pause_cli, ["resume", BETA], env=env)


def test_resuming_a_paused_account_exits_zero(resumed_result):
    # Arrange
    result = resumed_result
    # Act
    exit_code = result.exit_code
    # Assert
    assert exit_code == 0


def test_resuming_removes_the_record(resumed_result, two_accounts: Path):
    """Absence IS "not paused", so deletion is the whole of resume."""
    # Arrange
    account_dir = two_accounts / BETA
    # Act
    exists = pause_path(account_dir).exists()
    # Assert
    assert exists is False


def test_resuming_quotes_the_reason_it_lifted(resumed_result):
    """So the operator can see WHICH decision he just reversed."""
    # Arrange
    output = resumed_result.output
    # Act
    quoted = "lifted: quota rest" in output
    # Assert
    assert quoted is True


def test_resuming_states_the_health_that_results(resumed_result):
    # Arrange
    output = resumed_result.output
    # Act
    stated = "health now: VALID" in output
    # Assert
    assert stated is True


@pytest.fixture
def resumed_still_forbidden_result(pause_cli, env, two_accounts: Path):
    """Resume an account whose SUBSCRIPTION is still cancelled underneath."""
    CliRunner().invoke(
        pause_cli, ["pause", BETA, "--reason", "sub is off"], env=env
    )
    write_entitlement(
        two_accounts / BETA,
        Entitlement(
            BETA,
            FORBIDDEN,
            checked_at=time.time(),
            http_status=403,
            detail="oauth_not_allowed_for_organization",
        ),
    )
    return CliRunner().invoke(pause_cli, ["resume", BETA], env=env)


def test_resuming_a_cancelled_account_reports_forbidden(
    resumed_still_forbidden_result,
):
    """"Resumed" must not be heard as "working". The pause was one reason the
    account was out of service; it may not have been the only one."""
    # Arrange
    output = resumed_still_forbidden_result.output
    # Act
    stated = "health now: FORBIDDEN" in output
    # Assert
    assert stated is True


def test_resuming_a_cancelled_account_names_the_real_remedy(
    resumed_still_forbidden_result,
):
    # Arrange
    output = resumed_still_forbidden_result.output
    # Act
    named = "Restore the subscription" in output
    # Assert
    assert named is True


@pytest.fixture
def resume_of_a_running_account(pause_cli, env, two_accounts):
    return CliRunner().invoke(pause_cli, ["resume", BETA], env=env)


def test_resuming_an_unpaused_account_is_not_an_error(resume_of_a_running_account):
    """A no-op the operator is allowed to make."""
    # Arrange
    result = resume_of_a_running_account
    # Act
    exit_code = result.exit_code
    # Assert
    assert exit_code == 0


def test_resuming_an_unpaused_account_says_there_was_nothing_to_lift(
    resume_of_a_running_account,
):
    # Arrange
    output = resume_of_a_running_account.output
    # Act
    said = "was not paused" in output
    # Assert
    assert said is True
