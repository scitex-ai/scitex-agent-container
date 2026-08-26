"""A pause on DISK must reach the screen the operator actually types.

WHY THIS FILE EXISTS AT ALL. ``test__account_list_paused_row.py`` proves
the RENDERER can print PAUSED, by hand-rolling an ``AccountRow`` and
pushing it through the table. That is a good test and it is not this
one: it never asks whether anything READS a ``pause.json`` and fills
that row in. Reviewed 2026-08-26, three separate ways of severing the
wiring left all of the new tests green:

* ``build_stored_rows`` dropping ``pause_reason`` — the local builder
  stops carrying the pause, and a paused account renders ``VALID
  +6h12m``.
* ``build_stored_json`` dropping the ``paused`` key — the documented
  ``--json`` sibling disappears for every downstream consumer.
* ``rows_from_stored`` never reading that key — the SHIPPED defect. The
  data crossed the wire correctly and was thrown away at the renderer,
  so the DEFAULT ``sac accounts list`` (which goes through the fleet
  path, for this machine's own accounts too) showed ``VALID +7h59m``
  while ``sac accounts list --refresh`` showed ``PAUSED``. Only the
  second is a command he runs.

That matters more here than it would elsewhere: a pause never expires
and nothing nags him about one, so 「また復活させる」 depends on the
listing BEING the standing reminder. A reminder that renders only under
a flag he does not type is not one.

Every test below therefore starts from a real ``pause.json`` on a real
``tmp_path`` store and ends at a rendered cell, and every one has a
control with the pause file absent — without the control, a bug that
printed PAUSED on every row would pass. NO MOCKS (PA-306): no
``monkeypatch``, no substituted reader; ``$HOME`` is redirected with the
suite's own ``env_save_restore`` fixture, which is the same real seam
the neighbouring list tests use.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from scitex_agent_container._creds._pause import Pause, write_pause
from scitex_agent_container._state.account_store import save_account
from scitex_agent_container.cli_pkg._account_list_build import (
    build_stored_json,
    build_stored_rows,
)
from scitex_agent_container.cli_pkg._account_list_collapse import (
    AccountGroup,
    _fmt_status_cell,
)
from scitex_agent_container.cli_pkg._account_list_fleet import rows_from_stored
from scitex_agent_container.cli_pkg._account_list_render import (
    AccountRow,
    render_stored_table_to_str,
)
from scitex_agent_container.cli_pkg.account_group import account

NAME = "work"
REASON = "quota rest — restarting the subscription later"
#: Token TTL on the hand-rolled fleet rows. Named so the stability test can
#: build the expected cell rather than hard-coding a rendered TTL.
_HOURS = 7.9


@pytest.fixture(autouse=True)
def sandbox_home(tmp_path, env_save_restore) -> Path:
    """Redirect ``$HOME`` so the account-store cascade lands in ``tmp_path``.

    Same shape as ``test__account_list_render.py::sandbox_home``. Click's
    ``CliRunner`` does not isolate ``Path.home()``, and the store
    resolves through it, so this is what keeps the real fleet store
    untouched.
    """
    home = tmp_path / "home"
    home.mkdir()
    env_save_restore.set("HOME", str(home))
    env_save_restore.delete("TZ")
    env_save_restore.delete("SCITEX_AGENT_CONTAINER_TZ")
    return home


def _seed_account(home: Path) -> Path:
    """A real stored account with a token that is fresh for eight hours."""
    save_account(NAME, {"email_address": "w@example.com"}, home=home)
    account_dir = home / ".scitex" / "agent-container" / "accounts" / NAME
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


@pytest.fixture
def paused_account(sandbox_home) -> Path:
    """The stored account, rested one day ago, with the record on disk."""
    account_dir = _seed_account(sandbox_home)
    write_pause(
        account_dir,
        Pause(
            name=NAME,
            active=True,
            reason=REASON,
            since=time.time() - 86400,
            by="operator@test-host",
        ),
    )
    return account_dir


@pytest.fixture
def running_account(sandbox_home) -> Path:
    """The identical account with NO pause file — the control for every case."""
    return _seed_account(sandbox_home)


_ACCOUNTS = [{"name": NAME, "email_address": "w@example.com"}]


# ---------------------------------------------------------------------------
# The local builder: does a file on disk reach the row?
# ---------------------------------------------------------------------------


def test_build_stored_rows_carries_the_reason_from_disk(paused_account):
    """Severing this makes a paused account render as ``VALID +6h12m``."""
    # Arrange
    accounts = list(_ACCOUNTS)
    # Act
    rows = build_stored_rows(accounts, passive=True)
    # Assert
    assert rows[0].pause_reason == REASON


def test_build_stored_rows_carries_no_reason_without_a_pause(running_account):
    """The control: the reason must come from the file, not from the builder."""
    # Arrange
    accounts = list(_ACCOUNTS)
    # Act
    rows = build_stored_rows(accounts, passive=True)
    # Assert
    assert rows[0].pause_reason == ""


def test_build_stored_rows_carries_the_decision_timestamp(paused_account):
    """Without ``since`` the cell can print PAUSED but not how long for."""
    # Arrange
    accounts = list(_ACCOUNTS)
    # Act
    rows = build_stored_rows(accounts, passive=True)
    # Assert
    assert rows[0].pause_since is not None


def test_build_stored_json_emits_the_documented_paused_key(paused_account):
    """``--json`` consumers read this key; renaming it must go red."""
    # Arrange
    accounts = list(_ACCOUNTS)
    # Act
    entries = build_stored_json(accounts, passive=True)
    # Assert
    assert entries[0]["paused"]["reason"] == REASON


def test_build_stored_json_says_null_when_not_paused(running_account):
    """The control, and the on-the-wire spelling: ``None``, never ``false``."""
    # Arrange
    accounts = list(_ACCOUNTS)
    # Act
    entries = build_stored_json(accounts, passive=True)
    # Assert
    assert entries[0]["paused"] is None


# ---------------------------------------------------------------------------
# The fleet boundary: does the key survive being turned back into a row?
# ---------------------------------------------------------------------------


def test_rows_from_stored_reads_the_paused_key(paused_account):
    """THE SHIPPED DEFECT: the key crossed the wire and was dropped here."""
    # Arrange
    entries = build_stored_json(_ACCOUNTS, passive=True)
    # Act
    rows = rows_from_stored(entries, "some-host")
    # Assert
    assert rows[0].pause_reason == REASON


def test_rows_from_stored_carries_no_reason_without_a_pause(running_account):
    """The control for the assertion above."""
    # Arrange
    entries = build_stored_json(_ACCOUNTS, passive=True)
    # Act
    rows = rows_from_stored(entries, "some-host")
    # Assert
    assert rows[0].pause_reason == ""


def test_a_peer_running_an_older_sac_omits_the_key_and_is_not_paused():
    """An older peer sends no ``paused`` at all; that is "not paused"."""
    # Arrange — exactly the entry shape a pre-pause sac emits.
    entries = [{"name": NAME, "freshness": "VALID", "freshness_hours": 7.9}]
    # Act
    rows = rows_from_stored(entries, "old-peer")
    # Assert
    assert rows[0].pause_reason == ""


# ---------------------------------------------------------------------------
# The collapsed cell: the DEFAULT table's own formatter
# ---------------------------------------------------------------------------


def _group(*rows: AccountRow) -> AccountGroup:
    return AccountGroup(provider="claude-code", name=NAME, rows=list(rows))


def _row(host: str, **overrides) -> AccountRow:
    base = dict(
        host=host,
        name=NAME,
        freshness_state="VALID",
        freshness_hours=_HOURS,
        used_pct_5h=None,
        used_pct_7d=None,
        snapshot_as_of=None,
    )
    base.update(overrides)
    return AccountRow(**base)


def test_the_collapsed_status_cell_says_paused():
    """It called ``_fmt_status`` POSITIONALLY and dropped the pause kwargs."""
    # Arrange
    group = _group(_row("h1", pause_reason=REASON, pause_since=time.time() - 86400))
    # Act
    cell = _fmt_status_cell(group)
    # Assert
    assert cell.startswith("PAUSED")


def test_the_collapsed_status_cell_says_valid_without_a_pause():
    """The control for the assertion above."""
    # Arrange
    group = _group(_row("h1"))
    # Act
    cell = _fmt_status_cell(group)
    # Assert
    assert cell.startswith("VALID")


def test_one_host_pausing_and_another_not_is_shown_as_a_disagreement():
    """A pause is a per-host FILE, so hosts CAN disagree — and must not be averaged.

    This column's whole job is that an odd host is named rather than
    absorbed. Folding the pause into the agreement key is what makes
    that true of a pause as well as of an expiry.
    """
    # Arrange
    group = _group(
        _row("h1"),
        _row("h2", pause_reason=REASON, pause_since=time.time() - 86400),
    )
    # Act
    cell = _fmt_status_cell(group)
    # Assert
    assert "PAUSED on h2" in cell


def test_a_one_to_one_disagreement_renders_the_same_way_every_run():
    """Found by this suite failing intermittently, and it is a real defect.

    ``max`` over a SET of equally-common states returns whichever the
    set yields first, and Python randomises string hashing per process.
    So the SAME two hosts rendered ``VALID x1; PAUSED on h2`` in one run
    and ``PAUSED x1; VALID on h1`` in the next. Both sentences are true,
    which is why it went unnoticed; on a screen refreshed every few
    seconds it reads as the fleet flapping between two states.

    First-seen row order is now the tie-break, so this asserts the
    stable answer rather than merely that SOMETHING was said.

    HONEST LIMIT: the twenty iterations run in ONE process and
    therefore share one hash seed, so they do not sample the variation
    they describe — the loop only pins that nothing else is
    non-deterministic. What catches a regression is the exact expected
    string: without the tie-break it is wrong on roughly half of
    Python's hash seeds, which is how this surfaced (green on its first
    runs, red later on identical code). A gate that fails half the time
    is a gate; one that reads "something was printed" is not.
    """
    # Arrange
    group = _group(
        _row("h1"),
        _row("h2", pause_reason=REASON, pause_since=time.time() - 86400),
    )
    # Act
    cells = {_fmt_status_cell(group) for _ in range(20)}
    # Assert
    assert cells == {"VALID x1; PAUSED on h2"}


# ---------------------------------------------------------------------------
# End to end: the command he actually types
# ---------------------------------------------------------------------------


def test_the_default_list_shows_paused(paused_account):
    """``sac accounts list`` — no --refresh, which is the whole point."""
    # Arrange
    args = ["list", "--no-fanout"]
    # Act
    result = CliRunner().invoke(account, args)
    # Assert
    assert "PAUSED" in result.output, result.output


def test_the_default_list_does_not_show_paused_without_a_pause(running_account):
    """The control: PAUSED must come from the file, not from the table."""
    # Arrange
    args = ["list", "--no-fanout"]
    # Act
    result = CliRunner().invoke(account, args)
    # Assert
    assert "PAUSED" not in result.output, result.output


def test_the_default_lists_status_cell_quotes_the_operators_own_reason(
    paused_account,
):
    """"PAUSED" alone leaves him wondering which decision this was.

    ASSERTED ON THE CELL, NOT ON THE TABLE, and that is not a dodge.
    ``rich`` word-wraps a Status cell that is wider than the column, so
    the reason genuinely appears in the rendered table split across
    three lines — a substring assertion there would be measuring the
    terminal width rather than anything this code decides, which is the
    same defect that made the first truncation test unable to fail. The
    cell is the widest thing the application actually controls, so this
    is where the claim belongs. The CLI test above owns the other half:
    that the cell reaches the default screen at all.
    """
    # Arrange — the real row the default table is built from.
    rows = build_stored_rows(list(_ACCOUNTS), passive=True)
    # Act
    cell = _fmt_status_cell(_group(rows[0]))
    # Assert
    assert "quota rest" in cell


def test_the_by_host_list_shows_paused(paused_account):
    """The other fleet shape reaches the renderer through the same builder."""
    # Arrange
    args = ["list", "--no-fanout", "--by-host"]
    # Act
    result = CliRunner().invoke(account, args)
    # Assert
    assert "PAUSED" in result.output, result.output


# ---------------------------------------------------------------------------
# The rendered table, from a real file rather than a hand-rolled row
# ---------------------------------------------------------------------------


def test_a_real_pause_file_reaches_the_rendered_table(paused_account):
    """Store → builder → renderer, with nothing hand-rolled in between."""
    # Arrange
    rows = build_stored_rows(list(_ACCOUNTS), passive=True)
    # Act
    table = render_stored_table_to_str(rows, width=160)
    # Assert
    assert "PAUSED 1d" in table


def test_without_the_file_the_same_table_shows_the_token_ttl(running_account):
    """The control: the same account, same builder, no pause file."""
    # Arrange
    rows = build_stored_rows(list(_ACCOUNTS), passive=True)
    # Act
    table = render_stored_table_to_str(rows, width=160)
    # Assert
    assert "PAUSED" not in table
