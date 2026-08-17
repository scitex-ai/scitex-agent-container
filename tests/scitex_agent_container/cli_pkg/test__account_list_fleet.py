"""Tests for the fleet-wide ``sac accounts list``.

The one that matters most is at the top: a listing MUST NOT rotate a
credential. It is asserted BEHAVIOURALLY — the credential file's bytes before
and after — rather than by watching for a call, because the property the fleet
needs is "the file did not change", and only the file can testify to that.

No mocks and no ``monkeypatch``: ``$HOME`` is redirected with the documented
env var (``Path.home()`` reads it on POSIX), and every fan-out seam is a real
callable.
"""

from __future__ import annotations

import json

import pytest

from scitex_agent_container.cli_pkg._account_list_build import usage_for_account
from scitex_agent_container.cli_pkg._account_list_fleet import rows_from_stored
from scitex_agent_container.cli_pkg._account_list_render import (
    render_stored_table_to_str,
)
from scitex_agent_container.cli_pkg._helpers._agent_list_fleet_model import (
    SAC_TOO_OLD,
    HostTarget,
)
from scitex_agent_container.cli_pkg._helpers._agent_list_fleet_probe import (
    ssh_json_probe,
)

_EXPIRED_MS = 1_000_000_000_000  # long past, so a live path WOULD refresh


@pytest.fixture
def sandbox_home(tmp_path, env_save_restore):
    """Redirect ``$HOME`` so ``Path.home()`` resolves inside ``tmp_path``."""
    home = tmp_path / "home"
    home.mkdir()
    env_save_restore.set("HOME", str(home))
    return home


@pytest.fixture
def expired_account(sandbox_home):
    """An account whose token is EXPIRED — the state that triggers a refresh.

    Returns the credential path. Its bytes are what the passivity tests watch:
    the live path rewrites this file in place when it refreshes.
    """
    acct = sandbox_home / ".scitex" / "agent-container" / "accounts" / "acct-a"
    acct.mkdir(parents=True)
    (acct / "account.json").write_text(json.dumps({"name": "acct-a"}))
    (acct / ".credentials.json").write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "sk-ant-EXPIRED",
                    "refreshToken": "refresh-single-use",
                    "clientId": "client-1",
                    "expiresAt": _EXPIRED_MS,
                }
            }
        )
    )
    (acct / "usage.json").write_text(json.dumps({"used_pct_5h": 12.0}))
    return acct / ".credentials.json"


# ===========================================================================
# A LISTING MUST NOT ROTATE A CREDENTIAL
# ===========================================================================


def test_passive_read_leaves_the_credential_file_byte_identical(expired_account):
    # Arrange -- an EXPIRED token: exactly the state the live path refreshes,
    # and a refresh rewrites this file and invalidates the single-use refresh
    # token every other host is still holding (INCIDENT 2026-08-09).
    before = expired_account.read_bytes()
    # Act
    usage_for_account({"name": "acct-a"}, passive=True)
    # Assert
    assert expired_account.read_bytes() == before


def test_passive_read_returns_the_cached_usage(expired_account):
    # Arrange
    del expired_account
    # Act
    usage = usage_for_account({"name": "acct-a"}, passive=True)
    # Assert
    assert usage == {"used_pct_5h": 12.0}


def test_passive_read_returns_none_when_there_is_no_cache(sandbox_home):
    # Arrange -- no usage.json; passive must not go and fetch one.
    acct = sandbox_home / ".scitex" / "agent-container" / "accounts" / "acct-b"
    acct.mkdir(parents=True)
    (acct / "account.json").write_text(json.dumps({"name": "acct-b"}))
    # Act
    usage = usage_for_account({"name": "acct-b"}, passive=True)
    # Assert
    assert usage is None


def test_the_fleet_leg_asks_every_peer_to_be_passive():
    # Arrange
    from scitex_agent_container.cli_pkg._account_list_fleet import _REMOTE_ARGV

    # Act
    argv = list(_REMOTE_ARGV)
    # Assert -- without this the fan-out would rotate a token on every machine.
    assert "--passive" in argv


def test_the_fleet_leg_also_carries_the_recursion_guard():
    # Arrange
    from scitex_agent_container.cli_pkg._account_list_fleet import _REMOTE_ARGV

    # Act
    argv = list(_REMOTE_ARGV)
    # Assert
    assert "--no-fanout" in argv


# ===========================================================================
# A peer too old to answer SAFELY is reported, never re-asked unsafely
# ===========================================================================


class _Peer:
    def __init__(self, ssh: str) -> None:
        self.name = ssh
        self.ssh = ssh
        self.via: tuple[str, ...] = ()

    def jump_chain(self, peers):
        return []

    def joined_preamble(self) -> str:
        return ""


class _Proc:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


_TARGET = HostTarget(name="peer", ssh="peer")
_PEERS = {"peer": _Peer("peer")}


def _runner(*results, record: list | None = None):
    queue = list(results)

    def run(argv, **kwargs):
        if record is not None:
            record.append(list(argv))
        return queue.pop(0)

    return run


def _old_peer_probe(record: list | None = None):
    return ssh_json_probe(
        _TARGET,
        8.0,
        argv=["sac", "accounts", "list", "--json", "--passive", "--no-fanout"],
        envelope_key="stored",
        required_flags=("--passive",),
        peers=_PEERS,
        runner=_runner(
            _Proc(2, "", "Usage: sac accounts list\nError: No such option: --passive"),
            _Proc(0, '{"stored": [{"name": "leaked"}]}'),
            record=record,
        ),
    )


def test_a_peer_that_rejects_the_safety_flag_reads_too_old():
    # Arrange
    # Act
    report, _ = _old_peer_probe()
    # Assert -- not "unreachable": we got there, and the remedy is an upgrade.
    assert report.status == SAC_TOO_OLD


def test_the_too_old_report_names_the_flag_and_the_remedy():
    # Arrange
    # Act
    report, _ = _old_peer_probe()
    # Assert
    assert "--passive" in report.detail and "upgrade sac" in report.detail


def test_a_peer_that_rejects_the_safety_flag_is_never_re_asked_without_it():
    # Arrange -- the retry that rescues a merely-stale peer is forbidden here:
    # re-asking without --passive would do the exact damage the flag prevents.
    seen: list = []
    # Act
    _old_peer_probe(record=seen)
    # Assert
    assert len(seen) == 1


def test_a_too_old_peer_contributes_no_rows():
    # Arrange
    # Act
    _, rows = _old_peer_probe()
    # Assert
    assert rows == []


# ===========================================================================
# Reading a peer's payload: by NAME, and honouring its own usage gate
# ===========================================================================


def _entry(**over) -> dict:
    base = {
        "name": "acct-a",
        "provider": "claude-code",
        "freshness": "VALID",
        "freshness_hours": 4.9,
        "usage": {"used_pct_5h": 30.0, "used_pct_7d": 40.0},
        "usage_state": "known",
        "identity": {"state": "verified", "verified_email": "a@example.com"},
    }
    base.update(over)
    return base


def test_a_peer_row_is_stamped_with_its_host():
    # Arrange
    stored = [_entry()]
    # Act
    rows = rows_from_stored(stored, "nas-03")
    # Assert
    assert rows[0].host == "nas-03"


def test_a_peer_row_carries_its_own_freshness_hours():
    # Arrange -- the whole point: the SAME account differs host to host.
    stored = [_entry(freshness_hours=-1.1, freshness="EXPIRED")]
    # Act
    rows = rows_from_stored(stored, "nas-03")
    # Assert
    assert (rows[0].freshness_state, rows[0].freshness_hours) == ("EXPIRED", -1.1)


def test_a_percentage_is_kept_when_the_peer_vouches_for_it():
    # Arrange
    stored = [_entry()]
    # Act
    rows = rows_from_stored(stored, "h")
    # Assert
    assert rows[0].used_pct_5h == 30.0


def test_a_percentage_is_dropped_when_the_peer_does_not_vouch_for_it():
    # Arrange -- a figure read with a credential that may belong to another
    # account is not this account's usage, however well it survived the ssh hop.
    stored = [_entry(usage_state="unknown")]
    # Act
    rows = rows_from_stored(stored, "h")
    # Assert
    assert rows[0].used_pct_5h is None


def test_an_unknown_key_in_a_peer_payload_does_not_ride_along():
    # Arrange -- keys are read BY NAME, so a hand-edited account.json cannot
    # smuggle a field (token material included) into the table.
    stored = [_entry(accessToken="sk-ant-SECRET")]
    # Act
    rows = rows_from_stored(stored, "h")
    # Assert
    assert not hasattr(rows[0], "accessToken")


def test_a_secret_in_a_peer_payload_never_reaches_the_rendered_table():
    # Arrange
    rows = rows_from_stored([_entry(accessToken="sk-ant-SECRET")], "h")
    # Act
    rendered = render_stored_table_to_str(rows, width=200)
    # Assert
    assert "sk-ant-SECRET" not in rendered


def test_a_malformed_peer_entry_is_skipped_rather_than_crashing():
    # Arrange
    stored = [{"no": "name"}, _entry()]
    # Act
    rows = rows_from_stored(stored, "h")
    # Assert
    assert [r.name for r in rows] == ["acct-a"]


# ===========================================================================
# The Host column appears exactly when it says something
# ===========================================================================


def test_the_table_gains_a_host_column_for_a_fleet_listing():
    # Arrange
    rows = rows_from_stored([_entry()], "nas-03")
    # Act
    rendered = render_stored_table_to_str(rows, width=200)
    # Assert
    assert "Host" in rendered


def test_the_host_cell_names_the_machine():
    # Arrange
    rows = rows_from_stored([_entry()], "nas-03")
    # Act
    rendered = render_stored_table_to_str(rows, width=200)
    # Assert
    assert "nas-03" in rendered


def test_a_single_host_listing_keeps_its_columns_unchanged():
    # Arrange -- a column that always says the same thing teaches the eye to
    # skip the place where the answer lives.
    rows = rows_from_stored([_entry()], "")
    # Act
    rendered = render_stored_table_to_str(rows, width=200)
    # Assert
    assert "Host" not in rendered
