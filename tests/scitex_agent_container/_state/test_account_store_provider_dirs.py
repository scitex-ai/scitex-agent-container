"""``list_accounts`` must distinguish PROVIDER directories from accounts.

The store holds two kinds of subdirectory side by side:

  accounts/anthropic/            <- PROVIDER: contains account dirs, no files
  accounts/openai/               <- PROVIDER
  accounts/wyusuuke-gmail-com/   <- ACCOUNT: account.json + .credentials.json
  accounts/_rotations/           <- store bookkeeping

The old test denylisted the single literal name ``"openai"`` and therefore
missed ``anthropic/``, which was enumerated as an account. Measured
2026-07-29: ``sac accounts refresh --all`` reported "anthropic FAILED —
credentials file not found" and exited 1 on every run, so systemd marked
``sac.accounts-refresh.service`` failed every 10 minutes — while the SAME run
reported all three real accounts "skipped; token still fresh". A unit doing
its job correctly reported failure indefinitely.

These lock the structural predicate, which cannot be forgotten the way a name
denylist can, and pin the one case a naive "has credentials" test would break.

PA-306: no mocks — real directories on disk, real ``list_accounts``.
AAA markers (TQ002); descriptive names; one assertion each (TQ007).
"""

from __future__ import annotations

import json
from pathlib import Path

from scitex_agent_container._state.account_store import list_accounts


def _account(store: Path, name: str, *, meta: bool = True, creds: bool = True) -> Path:
    d = store / name
    d.mkdir(parents=True, exist_ok=True)
    if meta:
        (d / "account.json").write_text(json.dumps({"name": name}), encoding="utf-8")
    if creds:
        (d / ".credentials.json").write_text("{}", encoding="utf-8")
    return d


def _provider(store: Path, name: str, *, holding: str) -> Path:
    """A provider dir: contains an account dir, holds no files of its own."""
    d = store / name
    (d / holding).mkdir(parents=True, exist_ok=True)
    return d


def _names(store: Path) -> list[str]:
    return [a["name"] for a in list_accounts(store_dir=store)]


def test_anthropic_provider_dir_is_not_listed_as_an_account(tmp_path: Path) -> None:
    # Arrange — the exact shape of the real store on 2026-07-29.
    _provider(tmp_path, "anthropic", holding="wyusuuke-gmail-com")
    _account(tmp_path, "wyusuuke-gmail-com")

    # Act
    names = _names(tmp_path)

    # Assert — this is the regression: 'anthropic' was reported as an account
    # and then failed for a credentials file it can never have.
    assert "anthropic" not in names


def test_openai_provider_dir_is_not_listed_as_an_account(tmp_path: Path) -> None:
    # Arrange — the name the OLD denylist happened to cover; it must stay
    # excluded once the test is structural rather than name-based.
    _provider(tmp_path, "openai", holding="some-account")
    _account(tmp_path, "wyusuuke-gmail-com")

    # Act
    names = _names(tmp_path)

    # Assert
    assert "openai" not in names


def test_an_unknown_future_provider_is_excluded_without_being_named(
    tmp_path: Path,
) -> None:
    # Arrange — the whole point of the structural test: a provider nobody has
    # added to any list. A denylist is silently wrong until someone remembers.
    _provider(tmp_path, "some-future-vendor", holding="acct-1")
    _account(tmp_path, "wyusuuke-gmail-com")

    # Act
    names = _names(tmp_path)

    # Assert
    assert "some-future-vendor" not in names


def test_a_real_account_is_still_listed(tmp_path: Path) -> None:
    # Arrange — positive control: the exclusion must not swallow real accounts.
    _provider(tmp_path, "anthropic", holding="wyusuuke-gmail-com")
    _account(tmp_path, "wyusuuke-gmail-com")

    # Act
    names = _names(tmp_path)

    # Assert
    assert "wyusuuke-gmail-com" in names


def test_all_real_accounts_are_listed_alongside_providers(tmp_path: Path) -> None:
    # Arrange
    _provider(tmp_path, "anthropic", holding="x")
    _provider(tmp_path, "openai", holding="y")
    for n in ("wyusuuke-gmail-com", "ywata1989-gmail-com", "ywatanabe-scitex-ai"):
        _account(tmp_path, n)

    # Act
    names = sorted(_names(tmp_path))

    # Assert
    assert names == [
        "wyusuuke-gmail-com",
        "ywata1989-gmail-com",
        "ywatanabe-scitex-ai",
    ]


def test_an_account_whose_credentials_snapshot_is_missing_is_still_listed(
    tmp_path: Path,
) -> None:
    # Arrange — a REAL account in a BROKEN state: metadata present, credentials
    # gone. A naive "has .credentials.json" predicate would hide exactly the
    # account an operator most needs to see, and the refresher exists partly to
    # report it. This is the case that keeps the fix from over-correcting.
    _account(tmp_path, "broken-acct", meta=True, creds=False)

    # Act
    names = _names(tmp_path)

    # Assert
    assert "broken-acct" in names


def test_an_account_with_credentials_but_no_metadata_is_still_listed(
    tmp_path: Path,
) -> None:
    # Arrange — the mirror case; list_accounts already tolerated absent
    # metadata (it defaults the name), so the new gate must not regress it.
    _account(tmp_path, "creds-only", meta=False, creds=True)

    # Act
    names = _names(tmp_path)

    # Assert
    assert "creds-only" in names


def test_a_bare_account_dir_with_no_files_at_all_is_still_listed(
    tmp_path: Path,
) -> None:
    # Arrange — the case my FIRST version of this fix wrongly excluded, caught
    # by CI via test_missing_snapshot_is_recorded_failure (assert 0 == 1). An
    # account dir with NO metadata and NO credentials is a real account whose
    # snapshot is gone; the refresher exists to report exactly that. It differs
    # from a provider dir by having no child DIRECTORIES, not by having files.
    (tmp_path / "a-gmail-com").mkdir()

    # Act
    names = _names(tmp_path)

    # Assert
    assert "a-gmail-com" in names


def test_a_provider_dir_is_told_from_a_bare_dir_by_its_child_dirs(
    tmp_path: Path,
) -> None:
    # Arrange — both hold zero files; only the child directory distinguishes
    # them. This is the discriminator the fix actually turns on.
    _provider(tmp_path, "anthropic", holding="nested-account")
    (tmp_path / "bare-acct").mkdir()

    # Act
    names = sorted(_names(tmp_path))

    # Assert
    assert names == ["bare-acct"]


def test_underscore_bookkeeping_dirs_stay_excluded(tmp_path: Path) -> None:
    # Arrange — `_rotations/` holds auth-rotation telemetry keyed by email, so
    # it can contain account-looking children.
    (tmp_path / "_rotations").mkdir()
    (tmp_path / "_backup").mkdir()
    _account(tmp_path, "wyusuuke-gmail-com")

    # Act
    names = _names(tmp_path)

    # Assert
    assert names == ["wyusuuke-gmail-com"]


def test_an_empty_store_lists_nothing(tmp_path: Path) -> None:
    # Arrange — vacuity guard: if this returned entries the tests above would
    # pass for the wrong reason. A GENUINELY empty store: no dirs at all.
    #
    # The first version of this test created a bare `anthropic/` and asserted
    # []. That was wrong once the discriminator became child DIRECTORIES: a
    # bare dir with no children is a broken ACCOUNT, not a provider, so it is
    # correctly listed. The test encoded my earlier misunderstanding, and
    # tightening the predicate is what exposed it.
    (tmp_path / "_rotations").mkdir()

    # Act
    names = _names(tmp_path)

    # Assert
    assert names == []
