"""Gates for the account-identity verifier (INCIDENT 2026-08-12).

The incident: ``accounts/anthropic/ywatanabe-scitex-ai/`` held a credential
belonging to ``ywata1989@gmail.com``. One Anthropic account was rendered as
two rows, and the fleet appeared to have headroom it did not have. Nothing
in the store could catch it — a stored credential carries no identity claim
at all — so the directory name was the only thing naming the account, and it
was wrong.

Each test below drives a state sac must not mistake for a healthy one.
"""

from __future__ import annotations

import io
import json
from datetime import datetime, timedelta, timezone

from scitex_agent_container._account.account_verify import (
    MISMATCH,
    UNVERIFIED,
    VERIFIED,
    AccountIdentity,
    mark_duplicates,
    verify_account,
)

NOW = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _profile_opener(email, uuid="uuid-1"):
    def _opener(req, *a, **kw):
        return _Resp(json.dumps({"account": {"email": email, "uuid": uuid}}).encode())

    return _opener


def _dead_opener(req, *a, **kw):
    raise OSError("network down")


def _write_creds(path, token="tok-a"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": token,
                    "refreshToken": "r-" + token,
                    "expiresAt": 9_999_999_999_000,
                }
            }
        )
    )
    return path


# ---------------------------------------------------------------------------
# A credential that matches its directory
# ---------------------------------------------------------------------------


def test_matching_email_verifies(tmp_path):
    # Arrange
    creds = _write_creds(tmp_path / "acct" / ".credentials.json")
    # Act
    ident = verify_account(
        "acct",
        creds,
        claimed_email="a@example.com",
        opener=_profile_opener("a@example.com"),
        now=NOW,
    )
    # Assert
    assert ident.state == VERIFIED


def test_matching_email_records_the_verified_address(tmp_path):
    # Arrange
    creds = _write_creds(tmp_path / "acct" / ".credentials.json")
    # Act
    ident = verify_account(
        "acct",
        creds,
        claimed_email="a@example.com",
        opener=_profile_opener("a@example.com"),
        now=NOW,
    )
    # Assert
    assert ident.verified_email == "a@example.com"


def test_matching_email_is_trustworthy(tmp_path):
    # Arrange
    creds = _write_creds(tmp_path / "acct" / ".credentials.json")
    # Act
    ident = verify_account(
        "acct",
        creds,
        claimed_email="a@example.com",
        opener=_profile_opener("a@example.com"),
        now=NOW,
    )
    # Assert
    assert ident.trustworthy


# ---------------------------------------------------------------------------
# The incident: directory says one account, token proves another
# ---------------------------------------------------------------------------


def test_credential_belonging_to_another_account_is_a_mismatch(tmp_path):
    # Arrange
    creds = _write_creds(tmp_path / "ywatanabe-scitex-ai" / ".credentials.json")
    # Act
    ident = verify_account(
        "ywatanabe-scitex-ai",
        creds,
        claimed_email="ywatanabe@scitex.ai",
        opener=_profile_opener("ywata1989@gmail.com"),
        now=NOW,
    )
    # Assert
    assert ident.state == MISMATCH


def test_mismatch_names_the_real_owner(tmp_path):
    # Arrange — the operator needs the NAME to act; "wrong" alone is unusable.
    creds = _write_creds(tmp_path / "ywatanabe-scitex-ai" / ".credentials.json")
    # Act
    ident = verify_account(
        "ywatanabe-scitex-ai",
        creds,
        claimed_email="ywatanabe@scitex.ai",
        opener=_profile_opener("ywata1989@gmail.com"),
        now=NOW,
    )
    # Assert
    assert ident.verified_email == "ywata1989@gmail.com"


def test_mismatch_is_not_trustworthy(tmp_path):
    # Arrange — every figure gathered with this credential is someone else's.
    creds = _write_creds(tmp_path / "ywatanabe-scitex-ai" / ".credentials.json")
    # Act
    ident = verify_account(
        "ywatanabe-scitex-ai",
        creds,
        claimed_email="ywatanabe@scitex.ai",
        opener=_profile_opener("ywata1989@gmail.com"),
        now=NOW,
    )
    # Assert
    assert not ident.trustworthy


# ---------------------------------------------------------------------------
# "Could not check" is its own state, never folded into "checked and fine"
# ---------------------------------------------------------------------------


def test_unreachable_endpoint_reports_unverified_not_verified(tmp_path):
    # Arrange
    creds = _write_creds(tmp_path / "acct" / ".credentials.json")
    # Act
    ident = verify_account(
        "acct", creds, claimed_email="a@example.com", opener=_dead_opener, now=NOW
    )
    # Assert
    assert ident.state == UNVERIFIED


def test_unverified_is_not_trustworthy(tmp_path):
    # Arrange
    creds = _write_creds(tmp_path / "acct" / ".credentials.json")
    # Act
    ident = verify_account(
        "acct", creds, claimed_email="a@example.com", opener=_dead_opener, now=NOW
    )
    # Assert
    assert not ident.trustworthy


def test_missing_token_is_unverified(tmp_path):
    # Arrange
    creds = tmp_path / "acct" / ".credentials.json"
    creds.parent.mkdir(parents=True)
    creds.write_text(json.dumps({"claudeAiOauth": {}}))
    # Act
    ident = verify_account("acct", creds, claimed_email="a@example.com", now=NOW)
    # Assert
    assert ident.state == UNVERIFIED


# ---------------------------------------------------------------------------
# Cache validity is bound to the token, not only to a clock
# ---------------------------------------------------------------------------


def test_cached_verdict_is_reused_for_the_same_token(tmp_path):
    # Arrange
    creds = _write_creds(tmp_path / "acct" / ".credentials.json")
    verify_account(
        "acct",
        creds,
        claimed_email="a@example.com",
        opener=_profile_opener("a@example.com"),
        now=NOW,
    )
    # Act — endpoint now dead; the verdict still applies to THIS token.
    ident = verify_account(
        "acct", creds, claimed_email="a@example.com", opener=_dead_opener, now=NOW
    )
    # Assert
    assert ident.state == VERIFIED


def test_relogin_invalidates_the_cached_verdict(tmp_path):
    """A `/login` swaps the token; the old verdict must stop applying AT ONCE.

    This is the operator's actual scenario. A plain TTL would keep asserting
    the previous identity for hours after the credential changed underneath
    it — the cache would then be the very thing hiding the swap it exists to
    detect.
    """
    # Arrange — verify, then replace the credential as a re-login would.
    creds = _write_creds(tmp_path / "acct" / ".credentials.json", token="tok-old")
    verify_account(
        "acct",
        creds,
        claimed_email="a@example.com",
        opener=_profile_opener("a@example.com"),
        now=NOW,
    )
    _write_creds(creds, token="tok-new-after-login")
    # Act — endpoint unreachable, so nothing can re-verify the new token.
    ident = verify_account(
        "acct", creds, claimed_email="a@example.com", opener=_dead_opener, now=NOW
    )
    # Assert — NOT the stale VERIFIED verdict.
    assert ident.state == UNVERIFIED


def test_expired_verdict_is_not_reused(tmp_path):
    # Arrange
    creds = _write_creds(tmp_path / "acct" / ".credentials.json")
    verify_account(
        "acct",
        creds,
        claimed_email="a@example.com",
        opener=_profile_opener("a@example.com"),
        now=NOW,
    )
    # Act — well past the TTL, with no way to re-check.
    ident = verify_account(
        "acct",
        creds,
        claimed_email="a@example.com",
        opener=_dead_opener,
        now=NOW + timedelta(hours=7),
    )
    # Assert
    assert ident.state == UNVERIFIED


# ---------------------------------------------------------------------------
# Two directories, one Anthropic account
# ---------------------------------------------------------------------------


def _one_account_two_directories():
    """Two DIFFERENT tokens that are one account — the shape that fooled us.

    Comparing credential files (or token fingerprints) reports "distinct"
    here, which is how the duplication survived inspection. Only the account
    UUID answers the question.
    """
    return [
        AccountIdentity(
            name="ywata1989-gmail-com",
            state=VERIFIED,
            verified_email="ywata1989@gmail.com",
            verified_uuid="a84bd9dd",
        ),
        AccountIdentity(
            name="ywatanabe-scitex-ai",
            state=MISMATCH,
            claimed_email="ywatanabe@scitex.ai",
            verified_email="ywata1989@gmail.com",
            verified_uuid="a84bd9dd",
        ),
    ]


def test_first_of_a_duplicate_pair_keeps_its_identity():
    # Arrange
    idents = _one_account_two_directories()
    # Act
    marked = mark_duplicates(idents)
    # Assert
    assert marked[0].duplicate_of is None


def test_second_directory_is_marked_a_duplicate_of_the_first():
    # Arrange
    idents = _one_account_two_directories()
    # Act
    marked = mark_duplicates(idents)
    # Assert
    assert marked[1].duplicate_of == "ywata1989-gmail-com"


def test_duplicate_is_not_trustworthy():
    # Arrange — counting it again would double the fleet's apparent capacity.
    idents = _one_account_two_directories()
    # Act
    marked = mark_duplicates(idents)
    # Assert
    assert not marked[1].trustworthy


def test_the_verified_member_owns_the_group_regardless_of_order():
    """Directory order must not decide which row keeps its usage figure.

    Reversed, the MISLABELLED directory comes first. If ownership went by
    order it would claim the group, and the correctly-named account would be
    the one whose numbers got suppressed — the incident inverted.
    """
    # Arrange
    idents = list(reversed(_one_account_two_directories()))
    # Act
    marked = mark_duplicates(idents)
    # Assert — the mismatched row (now first) is still the duplicate.
    assert marked[0].duplicate_of == "ywata1989-gmail-com"


def test_the_correctly_labelled_account_keeps_its_figures():
    # Arrange
    idents = list(reversed(_one_account_two_directories()))
    # Act
    marked = mark_duplicates(idents)
    # Assert
    assert marked[1].duplicate_of is None


def test_unverified_accounts_are_never_grouped_as_duplicates():
    # Arrange — two unknowns are not evidence of sameness.
    idents = [
        AccountIdentity(name="a", state=UNVERIFIED),
        AccountIdentity(name="b", state=UNVERIFIED),
    ]
    # Act
    marked = mark_duplicates(idents)
    # Assert
    assert [m.duplicate_of for m in marked] == [None, None]
