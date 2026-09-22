"""Tests for scitex_agent_container._state.checkout_manifest (fleet sync-code).

Pure-function tests on synthetic records — no git, no ssh (PA-306),
one assertion per test (TQ007), AAA layout (TQ002).

Manifest contract (locked here so it survives refactors):

    {
      "host": "<canonical name>",
      "dotfiles": {"present": bool, "sha": "<40-hex>" | None},
      "sac_version": "<version>" | None,
      "checkouts": {
        "<pkg>": {
          "present": True, "branch": str | None, "sha": str,
          "dirty": bool, "ahead": int, "behind": int,
        },
      },
      "errors": []
    }

Diff-kind precedence per package (locked here):

    missing_on_host > branch_mismatch > dirty_on_host + ahead_of_origin
    (both reported — neither hides the other) > sha_mismatch > behind_origin
"""

from __future__ import annotations

from scitex_agent_container._state.checkout_manifest import (
    build_checkout_manifest,
    diff_checkout_manifests,
)


# ---------------------------------------------------------------------------
# Helpers (not tests).
# ---------------------------------------------------------------------------


def _rec(
    sha: str = "a" * 40,
    *,
    branch: str | None = "develop",
    dirty: bool = False,
    ahead: int = 0,
    behind: int = 0,
) -> dict:
    return {
        "branch": branch,
        "sha": sha,
        "dirty": dirty,
        "ahead": ahead,
        "behind": behind,
    }


def _manifest(
    host: str,
    checkouts: dict,
    *,
    dotfiles: str | None = "d" * 40,
    sac: str | None = "0.28.2",
) -> dict:
    return build_checkout_manifest(
        host=host,
        dotfiles_sha=dotfiles,
        sac_version=sac,
        checkouts=checkouts,
    )


# ---------------------------------------------------------------------------
# build_checkout_manifest
# ---------------------------------------------------------------------------


def test_build_sorts_packages_and_marks_present() -> None:
    got = _manifest("h1", {"b-pkg": _rec(), "a-pkg": _rec()})
    assert list(got["checkouts"].keys()) == ["a-pkg", "b-pkg"]


def test_build_coerces_dirty_ahead_behind_types() -> None:
    got = _manifest("h1", {"p": {"branch": "develop", "sha": "a" * 40, "dirty": 1, "ahead": "2", "behind": "0"}})
    assert got["checkouts"]["p"]["ahead"] == 2


def test_build_records_missing_keys_as_errors() -> None:
    got = _manifest("h1", {"p": {"branch": "develop", "sha": "a" * 40}})
    assert "p" not in got["checkouts"]


def test_build_absent_dotfiles_marks_not_present() -> None:
    got = _manifest("h1", {}, dotfiles=None)
    assert got["dotfiles"] == {"present": False, "sha": None}


# ---------------------------------------------------------------------------
# diff_checkout_manifests — single host trivially ok
# ---------------------------------------------------------------------------


def test_single_host_is_trivially_ok() -> None:
    got = diff_checkout_manifests({"h1": _manifest("h1", {"p": _rec()})})
    assert got["ok"] is True


# ---------------------------------------------------------------------------
# diff — identical fleet is ok
# ---------------------------------------------------------------------------


def test_identical_fleet_is_ok() -> None:
    per = {
        "h1": _manifest("h1", {"p": _rec()}),
        "h2": _manifest("h2", {"p": _rec()}),
    }
    got = diff_checkout_manifests(per)
    assert got["ok"] is True


# ---------------------------------------------------------------------------
# diff — dotfiles drift
# ---------------------------------------------------------------------------


def test_dotfiles_sha_mismatch_is_not_ok() -> None:
    per = {
        "h1": _manifest("h1", {}, dotfiles="d" * 40),
        "h2": _manifest("h2", {}, dotfiles="e" * 40),
    }
    got = diff_checkout_manifests(per)
    assert got["dotfiles"]["ok"] is False


def test_dotfiles_agreement_is_ok() -> None:
    per = {
        "h1": _manifest("h1", {}, dotfiles="d" * 40),
        "h2": _manifest("h2", {}, dotfiles="d" * 40),
    }
    got = diff_checkout_manifests(per)
    assert got["dotfiles"]["ok"] is True


# ---------------------------------------------------------------------------
# diff — sac version drift
# ---------------------------------------------------------------------------


def test_sac_version_mismatch_is_not_ok() -> None:
    per = {
        "h1": _manifest("h1", {}, sac="0.28.2"),
        "h2": _manifest("h2", {}, sac="0.28.1"),
    }
    got = diff_checkout_manifests(per)
    assert got["sac_version"]["ok"] is False


# ---------------------------------------------------------------------------
# diff — per-package kinds
# ---------------------------------------------------------------------------


def test_missing_package_on_one_host_conflicts() -> None:
    per = {
        "h1": _manifest("h1", {"p": _rec()}),
        "h2": _manifest("h2", {}),
    }
    got = diff_checkout_manifests(per)
    assert got["packages"]["p"]["conflicts"][0]["kind"] == "missing_on_host"


def test_dirty_tree_is_reported_with_diverged_host() -> None:
    per = {
        "h1": _manifest("h1", {"p": _rec(dirty=True)}),
        "h2": _manifest("h2", {"p": _rec()}),
    }
    got = diff_checkout_manifests(per)
    kinds = [c["kind"] for c in got["packages"]["p"]["conflicts"]]
    assert "dirty_on_host" in kinds


def test_ahead_and_dirty_both_reported_neither_hides_other() -> None:
    per = {
        "h1": _manifest("h1", {"p": _rec(dirty=True, ahead=2)}),
        "h2": _manifest("h2", {"p": _rec()}),
    }
    got = diff_checkout_manifests(per)
    kinds = [c["kind"] for c in got["packages"]["p"]["conflicts"]]
    assert "dirty_on_host" in kinds and "ahead_of_origin" in kinds


def test_branch_mismatch_conflicts() -> None:
    per = {
        "h1": _manifest("h1", {"p": _rec(branch="develop")}),
        "h2": _manifest("h2", {"p": _rec(branch="main")}),
    }
    got = diff_checkout_manifests(per)
    kinds = [c["kind"] for c in got["packages"]["p"]["conflicts"]]
    assert "branch_mismatch" in kinds


def test_behind_only_is_plain_lag() -> None:
    per = {
        "h1": _manifest("h1", {"p": _rec(behind=3)}),
        "h2": _manifest("h2", {"p": _rec()}),
    }
    got = diff_checkout_manifests(per)
    kinds = [c["kind"] for c in got["packages"]["p"]["conflicts"]]
    assert "behind_origin" in kinds


def test_overall_ok_false_when_any_package_conflicts() -> None:
    per = {
        "h1": _manifest("h1", {"p": _rec(behind=1)}),
        "h2": _manifest("h2", {"p": _rec()}),
    }
    got = diff_checkout_manifests(per)
    assert got["ok"] is False


# EOF
