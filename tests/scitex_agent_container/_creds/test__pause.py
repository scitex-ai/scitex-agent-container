"""A pause is a DECISION, and no probe may write it, lift it, or expire it.

OPERATOR REQUEST 2026-08-26. He stops and restarts Anthropic
subscriptions while watching quota, and asked for exactly one property
of the gap between: 「その休止の間も失敗しないようにしてほしい」 --
while an account is paused, nothing must fail because of it.

The design that delivers that has one load-bearing claim, and
:func:`test_a_pause_survives_the_entitlement_probe` is that claim as a
test. Both obvious places to store the flag are REWRITTEN WHOLESALE by
something that runs on a timer: ``account.json`` by ``save_account`` on
every ``sync-live``, and ``entitlement.json`` by the ``*/30``
entitlement probe. A pause put in either would silently lift itself
within half an hour, which is precisely the failure the operator asked
us to prevent. That test calls the REAL ``write_entitlement`` -- the
exact function the probe calls -- against a real account dir and
asserts the pause is untouched afterwards.

The other axis is the CONFLATION these two records must not undergo:

* PAUSE outranks FORBIDDEN and never renders as it, because one is
  authored and one is measured, and only the authored one is
  actionable by the person reading it;
* they occupy SEPARATE fields on the health record, so a probe's
  sentence and a human's sentence can never be mistaken for each other
  at the point of reading;
* a pause does not decay the way an entitlement verdict does -- a
  measurement whose prober stopped running must stop counting, but a
  decision does not go stale because nobody re-asserted it.

NO MOCKS (PA-306), and no ``monkeypatch`` either: every account here is
a real directory with a real credentials snapshot on a real
``tmp_path``, and every function under test already takes the
``store_dir`` / ``home`` seam that the boot picker, the minter and the
keepalive push all take.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from scitex_agent_container._creds._account_health import (
    NoHealthyAccountError,
    account_health,
)
from scitex_agent_container._creds._entitlement import (
    ENTITLED,
    FORBIDDEN,
    Entitlement,
    write_entitlement,
)
from scitex_agent_container._creds._pause import (
    Pause,
    clear_pause,
    format_age,
    pause_path,
    read_pause,
    write_pause,
)
from scitex_agent_container._creds._pick_healthy import pick_healthy_account

_HOUR = 3600.0
_DAY = 86400.0
ALPHA = "alpha-example-com"
BETA = "beta-example-com"


def _write_account(store: Path, name: str, *, hours_left: float = 8.0) -> Path:
    """A real account dir: a credentials snapshot with a real expiry, on disk."""
    account_dir = store / name
    account_dir.mkdir(parents=True, exist_ok=True)
    (account_dir / ".credentials.json").write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "access-not-a-real-token",
                    "refreshToken": "refresh-not-a-real-token",
                    "expiresAt": int((time.time() + hours_left * _HOUR) * 1000),
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
def store(tmp_path: Path) -> Path:
    """An empty account store on a real tmp_path."""
    path = tmp_path / ".scitex" / "agent-container" / "accounts"
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture
def fresh_account(store: Path) -> Path:
    """One account with a token that is hours from expiry."""
    return _write_account(store, ALPHA)


@pytest.fixture
def paused_account(fresh_account: Path) -> Path:
    """...which the operator has since paused."""
    _write_pause(fresh_account, ALPHA, "quota rest")
    return fresh_account


# ---------------------------------------------------------------------------
# THE ONE THE DESIGN STANDS ON: a probe cannot erase a decision
# ---------------------------------------------------------------------------


@pytest.fixture
def pause_after_a_probe_wrote_its_verdict(fresh_account: Path) -> Pause:
    """Pause the account, then run the probe's OWN write path over it."""
    _write_pause(fresh_account, ALPHA, "quota rest - restarting it later")
    write_entitlement(
        fresh_account, Entitlement(ALPHA, ENTITLED, checked_at=time.time())
    )
    return read_pause(ALPHA, fresh_account)


def test_a_pause_survives_the_entitlement_probe(
    pause_after_a_probe_wrote_its_verdict: Pause,
):
    """Had the flag lived in entitlement.json, this would fail in 30 minutes."""
    # Arrange
    pause = pause_after_a_probe_wrote_its_verdict
    # Act
    active = pause.active
    # Assert
    assert active is True


def test_the_probe_does_not_alter_the_operators_reason(
    pause_after_a_probe_wrote_its_verdict: Pause,
):
    """Not merely still-paused: still paused for the SAME stated reason."""
    # Arrange
    pause = pause_after_a_probe_wrote_its_verdict
    # Act
    reason = pause.reason
    # Assert
    assert reason == "quota rest - restarting it later"


def test_a_paused_account_is_still_probed_underneath(
    store: Path, tmp_path: Path, paused_account: Path
):
    """The pause hides the account from the pool, never from the prober.

    Deliberate: the verdict must already be current the moment the
    operator resumes, so a restored subscription needs no second step.
    """
    # Arrange
    write_entitlement(paused_account, Entitlement(ALPHA, ENTITLED, checked_at=time.time()))
    # Act
    state = account_health(ALPHA, store_dir=store, home=tmp_path).state
    # Assert
    assert state == "PAUSED"


def test_resuming_reveals_the_verdict_recorded_during_the_pause(
    store: Path, tmp_path: Path, paused_account: Path
):
    # Arrange
    write_entitlement(paused_account, Entitlement(ALPHA, ENTITLED, checked_at=time.time()))
    clear_pause(paused_account)
    # Act
    state = account_health(ALPHA, store_dir=store, home=tmp_path).state
    # Assert
    assert state == "VALID"


# ---------------------------------------------------------------------------
# Health state
# ---------------------------------------------------------------------------


def test_an_unpaused_fresh_account_reads_valid(
    store: Path, tmp_path: Path, fresh_account: Path
):
    """The control. Without it, PAUSED could be coming from the fixture."""
    # Arrange
    name = ALPHA
    # Act
    state = account_health(name, store_dir=store, home=tmp_path).state
    # Assert
    assert state == "VALID"


def test_a_paused_account_reads_paused(
    store: Path, tmp_path: Path, paused_account: Path
):
    # Arrange
    name = ALPHA
    # Act
    state = account_health(name, store_dir=store, home=tmp_path).state
    # Assert
    assert state == "PAUSED"


def test_a_paused_account_is_not_healthy(
    store: Path, tmp_path: Path, paused_account: Path
):
    """``is_healthy`` is the ONE predicate the picker and the minter gate on."""
    # Arrange
    name = ALPHA
    # Act
    healthy = account_health(name, store_dir=store, home=tmp_path).is_healthy
    # Assert
    assert healthy is False


def test_a_paused_health_record_carries_the_reason(
    store: Path, tmp_path: Path, paused_account: Path
):
    # Arrange
    name = ALPHA
    # Act
    reason = account_health(name, store_dir=store, home=tmp_path).pause_reason
    # Assert
    assert reason == "quota rest"


# ---------------------------------------------------------------------------
# Precedence: a decision outranks a measurement, and never borrows its field
# ---------------------------------------------------------------------------


@pytest.fixture
def forbidden_account(fresh_account: Path) -> Path:
    """A fresh token whose subscription the API has measured as refused."""
    write_entitlement(
        fresh_account,
        Entitlement(
            ALPHA,
            FORBIDDEN,
            checked_at=time.time(),
            http_status=403,
            detail="oauth_not_allowed_for_organization",
        ),
    )
    return fresh_account


def test_without_a_pause_a_cancelled_account_reads_forbidden(
    store: Path, tmp_path: Path, forbidden_account: Path
):
    """The control for the precedence tests below."""
    # Arrange
    name = ALPHA
    # Act
    state = account_health(name, store_dir=store, home=tmp_path).state
    # Assert
    assert state == "FORBIDDEN"


def test_a_pause_outranks_a_forbidden_verdict(
    store: Path, tmp_path: Path, forbidden_account: Path
):
    """The case the operator will actually hit.

    The accounts he wants to rest are the ones whose subscriptions he
    cancelled, so their verdict already reads FORBIDDEN. If the measured
    denial won, pausing would change nothing visible and he could not
    tell "I stopped this" from "this broke".
    """
    # Arrange
    _write_pause(forbidden_account, ALPHA, "resting it while the sub is off")
    # Act
    state = account_health(ALPHA, store_dir=store, home=tmp_path).state
    # Assert
    assert state == "PAUSED"


def test_a_paused_record_does_not_carry_the_apis_words(
    store: Path, tmp_path: Path, forbidden_account: Path
):
    """Two fields, never one. A probe's sentence is not a human's sentence."""
    # Arrange
    _write_pause(forbidden_account, ALPHA, "resting it while the sub is off")
    # Act
    detail = account_health(ALPHA, store_dir=store, home=tmp_path).entitlement_detail
    # Assert
    assert detail == ""


def test_a_paused_record_carries_the_operators_words(
    store: Path, tmp_path: Path, forbidden_account: Path
):
    # Arrange
    _write_pause(forbidden_account, ALPHA, "resting it while the sub is off")
    # Act
    reason = account_health(ALPHA, store_dir=store, home=tmp_path).pause_reason
    # Assert
    assert reason == "resting it while the sub is off"


# ---------------------------------------------------------------------------
# The decision is true whatever the credential is doing
# ---------------------------------------------------------------------------


@pytest.fixture
def account_with_no_snapshot(store: Path) -> Path:
    """A real account directory whose credential file is simply not there."""
    account_dir = store / ALPHA
    account_dir.mkdir(parents=True, exist_ok=True)
    return account_dir


def test_without_a_pause_a_missing_snapshot_reads_absent(
    store: Path, tmp_path: Path, account_with_no_snapshot: Path
):
    """The control: ABSENT is what this account says when nobody paused it."""
    # Arrange
    name = ALPHA
    # Act
    state = account_health(name, store_dir=store, home=tmp_path).state
    # Assert
    assert state == "ABSENT"


def test_a_paused_account_with_no_snapshot_still_reads_paused(
    store: Path, tmp_path: Path, account_with_no_snapshot: Path
):
    """Report the fact the operator can act on, not a fault he did not cause."""
    # Arrange
    _write_pause(account_with_no_snapshot, ALPHA, "resting")
    # Act
    state = account_health(ALPHA, store_dir=store, home=tmp_path).state
    # Assert
    assert state == "PAUSED"


# ---------------------------------------------------------------------------
# No age decay
# ---------------------------------------------------------------------------


@pytest.fixture
def ancient_pause(fresh_account: Path) -> Pause:
    """A pause written 400 days ago and never re-asserted."""
    _write_pause(fresh_account, ALPHA, "long rest", ago=400 * _DAY)
    return read_pause(ALPHA, fresh_account)


def test_a_400_day_old_pause_is_still_a_pause(ancient_pause: Pause):
    """An entitlement verdict would have decayed to UNKNOWN after 24 hours.

    Copying that here would un-pause an account behind the operator's
    back -- the same conflation, from the other side.
    """
    # Arrange
    pause = ancient_pause
    # Act
    active = pause.active
    # Assert
    assert active is True


def test_a_long_pause_renders_its_age_in_days(ancient_pause: Pause):
    """The age is what stands in for the expiry this record refuses to have."""
    # Arrange
    pause = ancient_pause
    # Act
    rendered = pause.age_human()
    # Assert
    assert rendered == "400d"


# ---------------------------------------------------------------------------
# Degrade towards VISIBLE, never towards silent
# ---------------------------------------------------------------------------

_UNUSABLE_RECORDS = [
    pytest.param("not json at all", id="not-json"),
    pytest.param(json.dumps({"reason": "   "}), id="whitespace-reason"),
    pytest.param(json.dumps({"since": 1.0, "by": "x"}), id="no-reason-key"),
    pytest.param(json.dumps(["a", "list"]), id="not-an-object"),
]


@pytest.mark.parametrize("body", _UNUSABLE_RECORDS)
def test_an_unusable_pause_record_does_not_pause(fresh_account: Path, body: str):
    """Chosen direction: back in the pool, loudly, rather than out of service
    silently and forever."""
    # Arrange
    pause_path(fresh_account).write_text(body)
    # Act
    active = read_pause(ALPHA, fresh_account).active
    # Assert
    assert active is False


@pytest.mark.parametrize("body", _UNUSABLE_RECORDS)
def test_an_unusable_pause_record_states_its_problem(fresh_account: Path, body: str):
    """``problem`` is what makes an unusable record visible rather than inert."""
    # Arrange
    pause_path(fresh_account).write_text(body)
    # Act
    problem = read_pause(ALPHA, fresh_account).problem
    # Assert
    assert problem != ""


def test_an_unusable_pause_record_leaves_the_account_usable(
    store: Path, tmp_path: Path, fresh_account: Path
):
    # Arrange
    pause_path(fresh_account).write_text("not json at all")
    # Act
    state = account_health(ALPHA, store_dir=store, home=tmp_path).state
    # Assert
    assert state == "VALID"


def test_an_absent_record_is_not_a_pause(fresh_account: Path):
    # Arrange
    name = ALPHA
    # Act
    active = read_pause(name, fresh_account).active
    # Assert
    assert active is False


def test_an_absent_record_reports_no_problem(fresh_account: Path):
    """Absence is the NORMAL not-paused, and must not look like damage."""
    # Arrange
    name = ALPHA
    # Act
    problem = read_pause(name, fresh_account).problem
    # Assert
    assert problem == ""


# ---------------------------------------------------------------------------
# Lifting one
# ---------------------------------------------------------------------------


def test_clear_pause_reports_that_it_removed_a_record(paused_account: Path):
    # Arrange
    account_dir = paused_account
    # Act
    removed = clear_pause(account_dir)
    # Assert
    assert removed is True


def test_clear_pause_reports_nothing_to_lift_the_second_time(paused_account: Path):
    """Resuming an already-running account is a no-op, not an error."""
    # Arrange
    clear_pause(paused_account)
    # Act
    removed = clear_pause(paused_account)
    # Assert
    assert removed is False


def test_lifting_a_pause_makes_the_account_usable_again(
    store: Path, tmp_path: Path, paused_account: Path
):
    """One command, no spec edit, no rename — 「また復活させる」."""
    # Arrange
    clear_pause(paused_account)
    # Act
    healthy = account_health(ALPHA, store_dir=store, home=tmp_path).is_healthy
    # Assert
    assert healthy is True


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(90, "1m"), (4 * _HOUR, "4h"), (40 * _DAY, "40d"), (None, "an unknown time")],
)
def test_format_age_is_coarse_on_purpose(seconds, expected):
    """A multi-week decision reported to the minute would be noise."""
    # Arrange
    value = seconds
    # Act
    rendered = format_age(value)
    # Assert
    assert rendered == expected


# ---------------------------------------------------------------------------
# The picker
# ---------------------------------------------------------------------------


@pytest.fixture
def one_paused_one_running(store: Path) -> Path:
    """Two real accounts; the operator has rested exactly one of them."""
    paused_dir = _write_account(store, ALPHA)
    _write_account(store, BETA)
    _write_pause(paused_dir, ALPHA, "quota rest")
    return store


def test_the_picker_skips_a_paused_account(
    tmp_path: Path, one_paused_one_running: Path
):
    # Arrange
    store_dir = one_paused_one_running
    # Act
    picked = pick_healthy_account(None, store_dir=store_dir, home=tmp_path)
    # Assert
    assert picked == BETA


def test_the_picker_rotates_off_a_pinned_paused_account(
    tmp_path: Path, one_paused_one_running: Path
):
    """The case that actually matters.

    A spec that PINS the paused account is exactly how an agent would
    otherwise keep booting onto the account the operator is resting --
    the waste 「無駄遣いをしないように」 names.
    """
    # Arrange
    store_dir = one_paused_one_running
    # Act
    picked = pick_healthy_account(ALPHA, store_dir=store_dir, home=tmp_path)
    # Assert
    assert picked == BETA


@pytest.fixture
def boot_error_with_everything_paused(store: Path, tmp_path: Path) -> str:
    """The message an agent boot renders when every account is rested."""
    alpha_dir = _write_account(store, ALPHA)
    beta_dir = _write_account(store, BETA)
    _write_pause(alpha_dir, ALPHA, "resting alpha for quota")
    _write_pause(beta_dir, BETA, "beta subscription is off")
    with pytest.raises(NoHealthyAccountError) as exc:
        pick_healthy_account(None, store_dir=store, home=tmp_path)
    return str(exc.value)


def test_the_boot_error_names_the_paused_state(
    boot_error_with_everything_paused: str,
):
    # Arrange
    message = boot_error_with_everything_paused
    # Act
    mentions_state = "PAUSED" in message
    # Assert
    assert mentions_state is True


def test_the_boot_error_quotes_the_first_pauses_reason(
    boot_error_with_everything_paused: str,
):
    """Without the reason the message offers ``claude /login`` — advice that
    cannot work, for a condition one ``sac accounts resume`` lifts."""
    # Arrange
    message = boot_error_with_everything_paused
    # Act
    quoted = "paused: resting alpha for quota" in message
    # Assert
    assert quoted is True


def test_the_boot_error_quotes_the_second_pauses_reason(
    boot_error_with_everything_paused: str,
):
    # Arrange
    message = boot_error_with_everything_paused
    # Act
    quoted = "paused: beta subscription is off" in message
    # Assert
    assert quoted is True
