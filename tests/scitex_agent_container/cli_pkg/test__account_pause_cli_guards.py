"""``sac accounts pause`` — the four refusals, and what a refusal must not say.

THE COMPANION TO ``test__account_pause_cli.py``, split off it because
that file passed the repo's 512-line cap. It holds the guards rather
than the happy path, and every one of them was added or repaired on
2026-08-26 after review found the same shape twice: a check that was
RIGHT ABOUT THE FEATURE and wrong about the population it ran on.

* THE LAST-VALID GUARD asked only "is anything ELSE VALID?", so it
  fired on a target that was not VALID either and then said, as its
  reason, that the target "currently reads VALID". That is exactly the
  account class this feature exists for — his FORBIDDEN account is the
  one he wants to rest — so the real workflow walked into a refusal
  built for a different situation and had to be talked past with
  ``--yes``.

* ``_resolve_account_dir`` validated with ``is_dir()``, which is true
  of the provider dir, the bookkeeping dirs and editor litter. All
  three paused successfully, wrote a record nothing reads, and left the
  timer failing while the operator had a command that said "paused".

* ``resume`` called ``clear_pause`` bare. Its refusal to absorb a
  non-FileNotFoundError is correct — the pause is STILL THERE — but
  the rendering was missing, so the one verb whose whole promise is
  that 「また復活させる」 costs one command ended in a stack trace.

* NOTHING PINNED THE ON-DISK KEY NAMES, so a lockstep rename of
  ``write_pause`` and ``read_pause`` would have kept every test green
  while every pause file written before the rename became unreadable —
  and unreadable degrades to "not paused", silently un-pausing every
  account he had rested.

NO MOCKS (PA-306), no ``monkeypatch``: real account directories on a
real ``tmp_path``, and click's own ``CliRunner(env=...)`` isolation.
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


# ---------------------------------------------------------------------------
# The guard asks about the TARGET, not only about its neighbours
# ---------------------------------------------------------------------------
#
# Reviewed 2026-08-26. The guard originally asked one question — "is
# anything ELSE currently VALID?" — and refused whenever the answer was
# no. So it fired on a target that was not VALID either, and stated as
# its reason that the target "currently reads VALID". Measured on a
# store holding one EXPIRED and one FORBIDDEN account: pausing the
# FORBIDDEN one was refused, both clauses of the refusal false, and the
# operator had to reach for --yes to do something that could not have
# broken anything.
#
# That is precisely the class this feature exists for. His FORBIDDEN
# account is the one he wants to rest, and the fleet's others read
# EXPIRED between keepalive passes, so the real workflow walked
# straight into a refusal written for a different situation. The
# existing pair above proves the guard on a VALID target, which is the
# half that was already right.


@pytest.fixture
def only_a_forbidden_account(store: Path) -> Path:
    """One account, fresh token, and a measured 403 underneath it.

    The operator's own 2026-08-26 case: ``wyusuuke-gmail-com``'s
    credential is fine and the API refuses it. Nothing here reads VALID,
    so pausing it removes nothing from the picker.
    """
    account_dir = _write_account(store, ALPHA)
    write_entitlement(
        account_dir,
        Entitlement(
            name=ALPHA,
            state=FORBIDDEN,
            checked_at=time.time(),
            http_status=403,
            detail="Your organization has disabled Claude Code",
        ),
    )
    return store


@pytest.fixture
def only_an_expired_account(store: Path) -> Path:
    """The same shape by the other route — a stale token, no entitlement file."""
    _write_account(store, ALPHA, hours_left=-5.0)
    return store


def test_pausing_a_forbidden_last_account_is_allowed_without_yes(
    pause_cli, env, only_a_forbidden_account
):
    """It was never pickable, so pausing it strands nothing."""
    # Arrange
    args = ["pause", ALPHA, "--reason", "the subscription is stopped"]
    # Act
    result = CliRunner().invoke(pause_cli, args, env=env)
    # Assert
    assert result.exit_code == 0, result.output


def test_pausing_a_forbidden_last_account_records_the_pause(
    pause_cli, env, only_a_forbidden_account
):
    """Exit 0 is not enough — the decision has to be on disk."""
    # Arrange
    args = ["pause", ALPHA, "--reason", "the subscription is stopped"]
    # Act
    CliRunner().invoke(pause_cli, args, env=env)
    # Assert
    assert read_pause(ALPHA, only_a_forbidden_account / ALPHA).active is True


def test_pausing_an_expired_last_account_is_allowed_without_yes(
    pause_cli, env, only_an_expired_account
):
    """The other route to the same state: stale token, no verdict file."""
    # Arrange
    args = ["pause", ALPHA, "--reason", "resting it while it is stale"]
    # Act
    result = CliRunner().invoke(pause_cli, args, env=env)
    # Assert
    assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# Only things the ENUMERATOR names may be paused
# ---------------------------------------------------------------------------
#
# Reviewed 2026-08-26. ``_resolve_account_dir`` validated with
# ``is_dir()``, which is true of things that are not accounts. Measured
# against a store carrying the real shape, all three exited 0 saying
# "paused": the PROVIDER dir ``anthropic/``, the bookkeeping dir
# ``_backup/``, and editor litter ``.swap-backup-20260815/``. Each wrote
# a ``pause.json`` where nothing would ever read it — ``list_accounts``
# skips ``_``/``.`` names and classifies a provider dir as a provider —
# so the very next keepalive run still enumerated the real accounts and
# still failed, while the operator had a command that had said "paused".
#
# It is reachable in the real store rather than hypothetical: the
# accounts are symlinked short names, so ``anthropic`` sits among the
# account names in an ``ls``. The pre-existing refusal test used a name
# with NO directory at all, which ``is_dir()`` already caught — so it
# proved the wrong half of the claim its own docstring made.


@pytest.fixture
def store_with_non_accounts(store: Path) -> Path:
    """A real account beside the three shapes that are not accounts."""
    _write_account(store, ALPHA)
    (store / "anthropic" / "some-account").mkdir(parents=True)
    (store / "_backup").mkdir()
    (store / ".swap-backup-20260815").mkdir()
    return store


@pytest.mark.parametrize("name", ["anthropic", "_backup", ".swap-backup-20260815"])
def test_pausing_something_that_is_not_an_account_is_refused(
    pause_cli, env, store_with_non_accounts, name
):
    """A decision recorded where nothing reads it is worse than a refusal."""
    # Arrange
    args = ["pause", name, "--reason", "audit probe"]
    # Act
    result = CliRunner().invoke(pause_cli, args, env=env)
    # Assert
    assert result.exit_code != 0, result.output


@pytest.mark.parametrize("name", ["anthropic", "_backup", ".swap-backup-20260815"])
def test_a_refused_non_account_gets_no_pause_file(
    pause_cli, env, store_with_non_accounts, name
):
    """Exit non-zero is not enough; nothing may be left behind either."""
    # Arrange
    args = ["pause", name, "--reason", "audit probe"]
    # Act
    CliRunner().invoke(pause_cli, args, env=env)
    # Assert
    assert not pause_path(store_with_non_accounts / name).exists()


def test_a_real_account_in_the_same_store_is_still_pausable(
    pause_cli, env, store_with_non_accounts
):
    """The reversing control: the membership check must not refuse everything."""
    # Arrange
    args = ["pause", ALPHA, "--reason", "quota rest", "--yes"]
    # Act
    result = CliRunner().invoke(pause_cli, args, env=env)
    # Assert
    assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# A store that refuses the unlink
# ---------------------------------------------------------------------------
#
# :func:`._creds._pause.clear_pause` absorbs FileNotFoundError and NOTHING
# else, deliberately: a permission problem means the pause is STILL THERE,
# and reporting that as "nothing to lift" would tell the operator he had
# resumed an account that stays paused. That decision is right and its
# RENDERING was missing — the call was bare, so the one verb whose whole
# promise is that 「また復活させる」 costs one command ended in a Python
# traceback instead of a sentence.
#
# The unremovable record here is a DIRECTORY named ``pause.json``, not a
# chmod: ``Path.unlink`` raises IsADirectoryError, which is an OSError and
# not FileNotFoundError, on every uid. A permission-based fixture would
# quietly succeed for root and turn this into a gate that cannot fail on
# exactly the hosts where it matters least to notice.


@pytest.fixture
def unremovable_record(pause_cli, env, two_accounts: Path) -> Path:
    """A ``pause.json`` that exists and cannot be unlinked."""
    pause_path(two_accounts / BETA).mkdir()
    return two_accounts


def test_resume_reports_an_unremovable_record_instead_of_crashing(
    pause_cli, env, unremovable_record
):
    """A ClickException, not a stack trace: exit 1 with a sentence."""
    # Arrange
    args = ["resume", BETA]
    # Act
    result = CliRunner().invoke(pause_cli, args, env=env)
    # Assert
    assert result.exit_code == 1, result.output


def test_resume_says_the_account_is_still_paused(pause_cli, env, unremovable_record):
    """The one thing he must not be left believing is that it worked."""
    # Arrange
    args = ["resume", BETA]
    # Act
    result = CliRunner().invoke(pause_cli, args, env=env)
    # Assert
    assert "STILL PAUSED" in result.output, result.output


def test_resume_does_not_raise_a_bare_oserror(pause_cli, env, unremovable_record):
    """The failure this replaces: an unhandled IsADirectoryError."""
    # Arrange
    args = ["resume", BETA]
    # Act
    result = CliRunner().invoke(pause_cli, args, env=env)
    # Assert
    assert not isinstance(result.exception, OSError)


def test_a_removable_record_is_still_removed(pause_cli, env, two_accounts: Path):
    """The reversing control: resume must still resume."""
    # Arrange
    CliRunner().invoke(pause_cli, ["pause", BETA, "--reason", "quota rest"], env=env)
    # Act
    CliRunner().invoke(pause_cli, ["resume", BETA], env=env)
    # Assert
    assert not pause_path(two_accounts / BETA).exists()


# ---------------------------------------------------------------------------
# The ON-DISK shape, pinned literally
# ---------------------------------------------------------------------------
#
# Every other test in this file produces a pause with ``write_pause`` and
# reads it back with ``read_pause``, so renaming the payload key in BOTH
# would keep them all green while every pause file written before the
# rename became unreadable — and an unreadable record degrades to "not
# paused", which silently un-pauses every account the operator had
# rested. That is the exact failure :mod:`._creds._pause` says it exists
# to prevent, arriving by the one route the round-trip cannot watch. This
# is the only assertion here that survives a lockstep rename.


def test_the_pause_file_holds_exactly_the_documented_keys(
    pause_cli, env, two_accounts: Path
):
    # Arrange
    CliRunner().invoke(pause_cli, ["pause", BETA, "--reason", "quota rest"], env=env)
    # Act
    payload = json.loads(pause_path(two_accounts / BETA).read_text())
    # Assert
    assert set(payload) == {"reason", "since", "by"}


def test_the_pause_file_stores_the_reason_under_the_key_reason(
    pause_cli, env, two_accounts: Path
):
    # Arrange
    CliRunner().invoke(pause_cli, ["pause", BETA, "--reason", "quota rest"], env=env)
    # Act
    payload = json.loads(pause_path(two_accounts / BETA).read_text())
    # Assert
    assert payload["reason"] == "quota rest"


def test_the_audit_field_names_a_real_user_not_the_word_unknown(
    pause_cli, env, two_accounts: Path
):
    """With no expiry, ``by`` and the reason are all that survive the months.

    THE FIXTURE IS THE POINT: every name-carrying environment variable
    is blanked, which is the state anything launched from systemd, cron
    or a bare container shell actually runs in. The first cut read only
    ``$USER`` / ``$LOGNAME`` and fell to the literal string "unknown"
    there — reliably, and most reliably in exactly the non-interactive
    contexts where nobody is watching. It now falls through
    ``getpass.getuser``, which consults ``pwd`` when the environment is
    silent.

    Without the blanking this test would pass on any interactive shell
    whatever the code did, i.e. it would be a gate that cannot fail.
    """
    # Arrange — the non-interactive environment, made explicit.
    blind_env = {**env, "USER": "", "LOGNAME": "", "LNAME": "", "USERNAME": ""}
    CliRunner().invoke(
        pause_cli, ["pause", BETA, "--reason", "quota rest"], env=blind_env
    )
    # Act
    payload = json.loads(pause_path(two_accounts / BETA).read_text())
    # Assert
    assert not payload["by"].startswith("unknown@")
