"""The OTHER account timer must honour the pause, or the pause half-works.

THERE ARE TWO TIMERS AND THEY SHARE AN ACCOUNT SET.
``sac accounts send-credentials`` DISTRIBUTES a credential to peers;
``sac accounts refresh --all --include-active --sync-active-login``
RENEWS one, on its own schedule. Reviewed 2026-08-26: the first cut of
the pause change taught the distributor to skip a rested account and
left the renewer enumerating straight from ``list_accounts``. Measured
by reading it: no ``read_pause`` anywhere in ``_account_refresh.py``.

Two consequences, and both are the operator's own words:

* A paused account is by construction one whose token is no longer
  kept alive, so within hours it stops being "skipped, still fresh"
  and starts being ATTEMPTED on every pass. Each failure feeds
  ``alert_failed_refreshes``; when he has paused EVERYTHING, every
  attempt fails and ``all_attempted_failed`` exits 1 on every pass.
  That is 「休止の間も失敗しないようにしてほしい」 — the requirement
  the whole change exists for — one timer over from the one that was
  fixed.
* Each attempt is a network round-trip against a subscription he asked
  us to stop touching, which is 「無駄遣いをしないように」 whatever the
  exit code says.

A half-honoured pause is worse than none, because it teaches him the
pause works.

WHAT THESE TESTS CAN AND CANNOT MEASURE OFFLINE, stated plainly. A
real refresh POSTs to Anthropic's token endpoint, which this suite must
not do, so no test here drives a refresh ATTEMPT of any kind. What
every test below measures instead is the TARGET SET and the lines the
run prints about it — which is where the defect was and where a future
edit would put it back.

THE EXIT CODE IS NOT ASSERTED, and the reason is worth recording
because the first draft did assert it. ``exit_code == 0`` on an
all-paused store passed, and its control — the same store without the
pause files — passed too, both at 0: a token that is still fresh is
held back by the TTL gate and never attempted, so the 0 was measuring
the fixture rather than the pause. Reversing that needs STALE tokens,
and a stale account really does reach for the network. The claim is
therefore made one step earlier, at the attempt set, where it is both
true and falsifiable: ``all_attempted_failed`` reads ``bool(attempted)``
and an empty list is False, which is the whole of why the timer stays
green through a total pause.

NO MOCKS (PA-306): real account dirs on a real ``tmp_path``, real
``pause.json`` written by :func:`._creds._pause.write_pause`, and
click's own ``CliRunner(env=...)`` isolation.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from scitex_agent_container._creds._pause import Pause, write_pause
from scitex_agent_container.cli_pkg._account_keepalive_pause import (
    refresh_targets_and_notes,
)

ALPHA = "alpha-example-com"
BETA = "beta-example-com"
REASON = "quota rest — subscription stopped for now"


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


def _pause(account_dir: Path, name: str) -> None:
    write_pause(
        account_dir,
        Pause(
            name=name,
            active=True,
            reason=REASON,
            since=time.time() - 86400,
            by="operator@test-host",
        ),
    )


@pytest.fixture
def store(tmp_path: Path) -> Path:
    path = tmp_path / ".scitex" / "agent-container" / "accounts"
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    return {"HOME": str(tmp_path), "SCITEX_DIR": str(tmp_path / ".scitex")}


@pytest.fixture
def one_paused(store: Path, tmp_path: Path) -> Path:
    """Two accounts, beta rested. The shape the operator actually runs."""
    _write_account(store, ALPHA)
    _pause(_write_account(store, BETA), BETA)
    return tmp_path


@pytest.fixture
def none_paused(store: Path, tmp_path: Path) -> Path:
    """The identical store with no pause files — the control for every case."""
    _write_account(store, ALPHA)
    _write_account(store, BETA)
    return tmp_path


# ---------------------------------------------------------------------------
# The partition itself
# ---------------------------------------------------------------------------


def test_a_paused_account_is_dropped_from_the_all_target_set(one_paused):
    """Every network round-trip this run would have made against it."""
    # Arrange
    home = one_paused
    # Act
    targets, _ = refresh_targets_and_notes(
        [ALPHA, BETA], all_accounts=True, home=home
    )
    # Assert
    assert targets == [ALPHA]


def test_without_the_pause_both_accounts_stay_in_the_target_set(none_paused):
    """The reversing control: the drop must come from the file."""
    # Arrange
    home = none_paused
    # Act
    targets, _ = refresh_targets_and_notes(
        [ALPHA, BETA], all_accounts=True, home=home
    )
    # Assert
    assert targets == [ALPHA, BETA]


def test_the_dropped_account_gets_a_skipped_line(one_paused):
    """A silent skip is indistinguishable from a target list that lost a name."""
    # Arrange
    home = one_paused
    # Act
    _, notes = refresh_targets_and_notes([ALPHA, BETA], all_accounts=True, home=home)
    # Assert
    assert "SKIPPED — paused" in notes[0]


def test_the_skipped_line_quotes_the_reason(one_paused):
    """The reason is what makes a months-old pause liftable."""
    # Arrange
    home = one_paused
    # Act
    _, notes = refresh_targets_and_notes([ALPHA, BETA], all_accounts=True, home=home)
    # Assert
    assert REASON in notes[0]


def test_nothing_is_announced_when_nothing_is_paused(none_paused):
    """The control: a quiet run must stay quiet."""
    # Arrange
    home = none_paused
    # Act
    _, notes = refresh_targets_and_notes([ALPHA, BETA], all_accounts=True, home=home)
    # Assert
    assert notes == []


def test_pausing_everything_leaves_nothing_to_attempt(one_paused, store):
    """Nothing attempted is nothing failed — that IS the exit-0 mechanism."""
    # Arrange
    _pause(store / ALPHA, ALPHA)
    # Act
    targets, _ = refresh_targets_and_notes(
        [ALPHA, BETA], all_accounts=True, home=one_paused
    )
    # Assert
    assert targets == []


# ---------------------------------------------------------------------------
# An explicitly named account is a different question
# ---------------------------------------------------------------------------


def test_naming_a_paused_account_still_refreshes_it(one_paused):
    """A pause silences what enumerates on its own, not what he types."""
    # Arrange
    home = one_paused
    # Act
    targets, _ = refresh_targets_and_notes([BETA], all_accounts=False, home=home)
    # Assert
    assert targets == [BETA]


def test_naming_a_paused_account_says_so(one_paused):
    """Otherwise the two surfaces disagree in silence, which is worse."""
    # Arrange
    home = one_paused
    # Act
    _, notes = refresh_targets_and_notes([BETA], all_accounts=False, home=home)
    # Assert
    assert "is PAUSED" in notes[0]


def test_naming_an_unpaused_account_says_nothing(one_paused):
    """The control for the note above."""
    # Arrange
    home = one_paused
    # Act
    _, notes = refresh_targets_and_notes([ALPHA], all_accounts=False, home=home)
    # Assert
    assert notes == []


# ---------------------------------------------------------------------------
# Through the CLI, which is where the timer meets it
# ---------------------------------------------------------------------------


def _invoke_refresh(env: dict[str, str], *args: str):
    from scitex_agent_container.cli_pkg.account_group import account

    return CliRunner().invoke(account, ["refresh", *args], env=env)


def test_the_refresh_command_announces_the_skip(one_paused, env):
    """``journalctl`` has to say the same thing the other timer's journal says."""
    # Arrange
    args = ("--all",)
    # Act
    result = _invoke_refresh(env, *args)
    # Assert
    assert f"{BETA}: SKIPPED — paused" in result.output, result.output


def test_the_refresh_command_says_nothing_when_nothing_is_paused(none_paused, env):
    """The reversing control, same store shape, same argv."""
    # Arrange
    args = ("--all",)
    # Act
    result = _invoke_refresh(env, *args)
    # Assert
    assert "SKIPPED — paused" not in result.output, result.output


def test_the_refresh_command_leaves_nothing_to_attempt_when_all_are_paused(
    one_paused, store, env
):
    """The operator's requirement, measured where it can honestly be measured.

    THE EXIT CODE IS NOT ASSERTED HERE, AND THAT IS DELIBERATE. The
    first draft of this test asserted ``exit_code == 0`` with an
    ``exit_code != 0`` control on the same store minus the pause files.
    The control came back GREEN-when-it-should-have-been-RED: both runs
    exited 0, because a token that is still fresh is skipped by the TTL
    gate and never attempted at all, so the 0 was measuring the fixture,
    not the pause. Reversing it needs accounts whose tokens are STALE —
    and a stale account really does POST to Anthropic's token endpoint,
    which a test may not do.

    So the claim is made one step earlier, where it is both true and
    falsifiable: the run reports that it has NOTHING to attempt. An
    empty attempt set is what ``all_attempted_failed`` reads, and
    ``bool([])`` is False, which is the whole of why the timer stays
    green through a total pause. The reversing control is
    :func:`test_without_the_pause_both_accounts_stay_in_the_target_set`,
    which shows the emptying comes from the pause files.
    """
    # Arrange
    _pause(store / ALPHA, ALPHA)
    # Act
    result = _invoke_refresh(env, "--all")
    # Assert
    assert f"{ALPHA}: SKIPPED — paused" in result.output, result.output


def test_an_all_paused_refresh_attempts_no_account(one_paused, store, env):
    """The other half: no account line survives the partition.

    ``skipped; token still fresh`` is what an ATTEMPT-less-but-present
    target prints, so its absence is how we know the target list was
    emptied rather than merely quiet.
    """
    # Arrange
    _pause(store / ALPHA, ALPHA)
    # Act
    result = _invoke_refresh(env, "--all")
    # Assert
    assert "token still fresh" not in result.output, result.output


def test_without_the_pauses_the_same_store_does_attempt_them(none_paused, env):
    """The reversing control for the assertion above, same argv, same store."""
    # Arrange
    args = ("--all",)
    # Act
    result = _invoke_refresh(env, *args)
    # Assert
    assert "token still fresh" in result.output, result.output
