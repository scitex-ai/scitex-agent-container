"""Entitlement is a THIRD question, and it is three-valued.

INCIDENT 2026-08-25 (the reason this module exists). A cancelled
subscription went undetected because every gate asked whether the
token was FRESH, and a cancelled account's token refreshes perfectly
well. ``wyusuuke-gmail-com`` refreshed at 09:17 UTC with a new expiry
of 17:17 and read ``VALID`` everywhere, while a real request returned::

    403 permission_error - "OAuth authentication is currently not
    allowed for this organization"

:func:`test_a_fresh_token_can_still_be_forbidden` is that incident as
a test: it is the case where freshness and entitlement disagree, and
it is the one no existing test covered.

The other axis under test is the constitution's three-valued rule --
"true, false, and *unknown*. Collapsing unknown into either pole is
the most common bug we ship." Both collapses are pinned:

* collapsing UNKNOWN -> FORBIDDEN would evict the whole pool on a
  network blip (:func:`test_a_network_failure_is_unknown_not_forbidden`,
  :func:`test_unknown_does_not_block_use`);
* collapsing UNKNOWN -> ENTITLED would let us claim we checked when we
  did not (:func:`test_never_probed_reads_unknown_not_entitled`).

NO MOCKS of our own code (PA-306). The only substitution is the HTTP
transport -- an ``opener`` callable standing in for the network, so the
REAL classification logic runs against synthetic responses. Everything
else is a real file on a real tmp_path.
"""

from __future__ import annotations

import json
import socket
import urllib.error
from pathlib import Path

import pytest

from scitex_agent_container._creds._entitlement import (
    ENTITLED,
    FORBIDDEN,
    UNKNOWN,
    Entitlement,
    entitlement_path,
    probe_entitlement,
    read_entitlement,
    write_entitlement,
)

_FORBIDDEN_BODY = json.dumps(
    {
        "type": "error",
        "error": {
            "type": "permission_error",
            "message": (
                "OAuth authentication is currently not allowed for this "
                "organization."
            ),
            "details": {"error_code": "oauth_not_allowed_for_organization"},
        },
    }
).encode()


def _account(tmp_path: Path, name: str = "acct", token: str = "tok") -> Path:
    """A real account dir on disk, with a real credentials snapshot."""
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    (d / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": token}})
    )
    return d


class _Resp:
    """The shape urlopen returns that we actually read: ``.status``."""

    def __init__(self, status: int = 200):
        self.status = status


def _ok_opener(_req, timeout=None):
    return _Resp(200)


def _http_error_opener(code: int, body: bytes):
    def _open(_req, timeout=None):
        raise urllib.error.HTTPError(
            url="https://api.anthropic.com/v1/messages",
            code=code,
            msg="err",
            hdrs=None,
            fp=__import__("io").BytesIO(body),
        )

    return _open


def _boom_opener(exc: Exception):
    def _open(_req, timeout=None):
        raise exc

    return _open


# ---------------------------------------------------------------------------
# The incident
# ---------------------------------------------------------------------------


def test_a_fresh_token_can_still_be_forbidden(tmp_path):
    # Arrange: a perfectly readable, perfectly fresh credential -- the
    # exact state wyusuuke was in while unusable.
    d = _account(tmp_path)
    # Act
    verdict = probe_entitlement(
        "acct", d, opener=_http_error_opener(403, _FORBIDDEN_BODY)
    )
    # Assert
    assert verdict.state == FORBIDDEN


def test_the_forbidden_verdict_blocks_use(tmp_path):
    # Arrange
    d = _account(tmp_path)
    # Act
    verdict = probe_entitlement(
        "acct", d, opener=_http_error_opener(403, _FORBIDDEN_BODY)
    )
    # Assert
    assert verdict.blocks_use is True


def test_the_forbidden_verdict_carries_the_api_reason(tmp_path):
    # Arrange: an operator reading only the verdict must learn WHY.
    d = _account(tmp_path)
    # Act
    verdict = probe_entitlement(
        "acct", d, opener=_http_error_opener(403, _FORBIDDEN_BODY)
    )
    # Assert
    assert "not allowed for this organization" in verdict.detail


def test_a_working_account_is_entitled(tmp_path):
    # Arrange
    d = _account(tmp_path)
    # Act
    verdict = probe_entitlement("acct", d, opener=_ok_opener)
    # Assert
    assert verdict.state == ENTITLED


def test_an_entitled_account_does_not_block_use(tmp_path):
    # Arrange
    d = _account(tmp_path)
    # Act
    verdict = probe_entitlement("acct", d, opener=_ok_opener)
    # Assert
    assert verdict.blocks_use is False


# ---------------------------------------------------------------------------
# Three-valued: UNKNOWN must collapse into NEITHER pole
# ---------------------------------------------------------------------------


def test_a_network_failure_is_unknown_not_forbidden(tmp_path):
    # Arrange: the uplink is down. This is NOT a cancelled subscription,
    # and calling it one would evict every account at once.
    d = _account(tmp_path)
    # Act
    verdict = probe_entitlement(
        "acct", d, opener=_boom_opener(socket.timeout("timed out"))
    )
    # Assert
    assert verdict.state == UNKNOWN


def test_unknown_does_not_block_use(tmp_path):
    # Arrange: the load-bearing half of the rule -- not knowing must
    # never take an account out of service.
    d = _account(tmp_path)
    # Act
    verdict = probe_entitlement(
        "acct", d, opener=_boom_opener(socket.timeout("timed out"))
    )
    # Assert
    assert verdict.blocks_use is False


@pytest.mark.parametrize("code", [401, 429, 500, 503])
def test_other_http_errors_are_not_entitlement_verdicts(tmp_path, code):
    # Arrange: 401 is the freshness gate's question, 429 is the quota
    # ranker's, 5xx is nobody's. Answering a question we were not asked
    # is how a signal starts lying.
    d = _account(tmp_path)
    # Act
    verdict = probe_entitlement(
        "acct", d, opener=_http_error_opener(code, b'{"error":"x"}')
    )
    # Assert
    assert verdict.state == UNKNOWN


def test_a_403_without_an_oauth_reason_is_not_forbidden(tmp_path):
    # Arrange: 403 alone is not the verdict -- the body must name an
    # oauth/permission problem. A generic 403 stays UNKNOWN.
    d = _account(tmp_path)
    # Act
    verdict = probe_entitlement(
        "acct", d, opener=_http_error_opener(403, b'{"error":"rate stuff"}')
    )
    # Assert
    assert verdict.state == UNKNOWN


def test_a_missing_credential_is_unknown(tmp_path):
    # Arrange: an account dir with no snapshot. That is the freshness
    # gate's ABSENT case, not an entitlement denial.
    d = tmp_path / "empty"
    d.mkdir()
    # Act
    verdict = probe_entitlement("empty", d, opener=_ok_opener)
    # Assert
    assert verdict.state == UNKNOWN


# ---------------------------------------------------------------------------
# The cached read -- the ONLY thing a boot path calls
# ---------------------------------------------------------------------------


def test_never_probed_reads_unknown_not_entitled(tmp_path):
    # Arrange: no record at all. Must not read as "fine".
    d = _account(tmp_path)
    # Act
    verdict = read_entitlement("acct", d)
    # Assert
    assert verdict.state == UNKNOWN


def test_never_probed_says_so(tmp_path):
    # Arrange
    d = _account(tmp_path)
    # Act
    verdict = read_entitlement("acct", d)
    # Assert
    assert verdict.detail == "never probed"


def test_a_written_verdict_reads_back(tmp_path):
    # Arrange
    d = _account(tmp_path)
    write_entitlement(
        d, Entitlement("acct", FORBIDDEN, checked_at=1000.0, http_status=403)
    )
    # Act
    verdict = read_entitlement("acct", d, now=1100.0)
    # Assert
    assert verdict.state == FORBIDDEN


def test_a_stale_verdict_is_not_believed(tmp_path):
    # Arrange: a FORBIDDEN from long ago must not keep an account out
    # after the operator restored the subscription -- and the timer
    # that would have refreshed it is evidently not running.
    d = _account(tmp_path)
    write_entitlement(d, Entitlement("acct", FORBIDDEN, checked_at=0.0))
    # Act
    verdict = read_entitlement("acct", d, now=48 * 3600.0)
    # Assert
    assert verdict.state == UNKNOWN


def test_a_stale_verdict_explains_itself(tmp_path):
    # Arrange
    d = _account(tmp_path)
    write_entitlement(d, Entitlement("acct", FORBIDDEN, checked_at=0.0))
    # Act
    verdict = read_entitlement("acct", d, now=48 * 3600.0)
    # Assert
    assert "old" in verdict.detail


def test_a_fresh_verdict_inside_the_window_is_believed(tmp_path):
    # Arrange: the boundary the staleness rule is built on.
    d = _account(tmp_path)
    write_entitlement(d, Entitlement("acct", FORBIDDEN, checked_at=0.0))
    # Act
    verdict = read_entitlement("acct", d, now=23 * 3600.0)
    # Assert
    assert verdict.state == FORBIDDEN


def test_a_corrupt_record_is_unknown_not_a_crash(tmp_path):
    # Arrange: a half-written file must not take down an agent boot.
    d = _account(tmp_path)
    entitlement_path(d).write_text("{not json")
    # Act
    verdict = read_entitlement("acct", d)
    # Assert
    assert verdict.state == UNKNOWN


def test_an_unrecognised_state_is_unknown(tmp_path):
    # Arrange: a record from a future/older schema.
    d = _account(tmp_path)
    entitlement_path(d).write_text(
        json.dumps({"state": "SOMETHING_ELSE", "checked_at": 1.0})
    )
    # Act
    verdict = read_entitlement("acct", d, now=2.0)
    # Assert
    assert verdict.state == UNKNOWN


def test_a_record_without_a_timestamp_is_unknown(tmp_path):
    # Arrange: without checked_at we cannot judge staleness, so the
    # verdict cannot be trusted no matter what it claims.
    d = _account(tmp_path)
    entitlement_path(d).write_text(json.dumps({"state": FORBIDDEN}))
    # Act
    verdict = read_entitlement("acct", d)
    # Assert
    assert verdict.state == UNKNOWN


@pytest.fixture
def verdict_but_no_credential(tmp_path: Path) -> Path:
    """An account dir holding a verdict and NO ``.credentials.json``.

    With no token on disk a live probe is impossible, so anything this
    dir can still answer was answered from local disk alone. That is
    how the cache-only contract is proven here -- by construction
    rather than by patching ``urlopen`` (fixtures that rewrite
    production internals are banned ecosystem-wide, and would only
    encode our assumption about the transport anyway).
    """
    d = tmp_path / "cached-only"
    d.mkdir()
    write_entitlement(d, Entitlement("acct", ENTITLED, checked_at=1000.0))
    return d


def test_the_read_path_needs_no_credential_to_answer(verdict_but_no_credential):
    # Arrange: see the fixture -- there is no token to authenticate with.
    # Act
    verdict = read_entitlement("acct", verdict_but_no_credential, now=1001.0)
    # Assert
    assert verdict.state == ENTITLED


def test_the_cache_only_fixture_really_has_no_credential(
    verdict_but_no_credential,
):
    # Arrange: the premise the test above rests on. Pinned separately so
    # that if the fixture ever starts writing a credential, THIS fails
    # rather than the proof silently becoming vacuous.
    # Act
    exists = (verdict_but_no_credential / ".credentials.json").exists()
    # Assert
    assert exists is False


# ---------------------------------------------------------------------------
# Auto-heal: the operator's actual workflow
# ---------------------------------------------------------------------------


@pytest.fixture
def cancelled_account(tmp_path: Path) -> Path:
    """An account the timer has already probed and found FORBIDDEN.

    The operator's real workflow: cancel a subscription, restore it
    later. This is the state after the cancellation has been noticed.
    """
    d = _account(tmp_path)
    write_entitlement(
        d,
        probe_entitlement(
            "acct", d, opener=_http_error_opener(403, _FORBIDDEN_BODY)
        ),
    )
    return d


def test_a_cancelled_account_is_blocked(cancelled_account):
    # Arrange: see the fixture.
    # Act
    verdict = read_entitlement("acct", cancelled_account)
    # Assert
    assert verdict.blocks_use is True


def test_a_restored_subscription_heals_without_human_action(cancelled_account):
    # Arrange: the subscription comes back and the next timer pass
    # re-probes. Nothing else -- no spec edit, no symlink rename, no
    # human step -- may be required to return the account to service.
    write_entitlement(
        cancelled_account,
        probe_entitlement("acct", cancelled_account, opener=_ok_opener),
    )
    # Act
    verdict = read_entitlement("acct", cancelled_account)
    # Assert
    assert verdict.blocks_use is False


def test_write_never_raises_on_an_unwritable_dir(tmp_path):
    # Arrange: bookkeeping written from a timer. A read-only store must
    # not take the timer down.
    d = _account(tmp_path)
    d.chmod(0o500)
    try:
        # Act
        ok = write_entitlement(d, Entitlement("acct", ENTITLED, checked_at=1.0))
        # Assert
        assert ok is False
    finally:
        d.chmod(0o700)
