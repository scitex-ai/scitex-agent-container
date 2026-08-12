"""Tests for the job-name grammar (local short name vs canonical id).

The canonical name is not cosmetic: ``scitex_dev.jobs`` de-duplicates on
it and the systemd renderer derives the UNIT FILENAME from it verbatim.
So a resolver that silently double-prefixes, or that quietly returns
nothing for a typo, changes which unit a verb acts on — which is how a
scheduled job stops being scheduled while every command still exits 0.

No mocks (PA-306): these are pure functions over plain strings.
AAA marker comments; one assertion per test.
"""

from __future__ import annotations

import pytest

from scitex_agent_container._jobs import _names
from scitex_agent_container._jobs._jobs_plugin import provide_jobs


def test_a_local_name_becomes_canonical() -> None:
    # Arrange
    typed = "accounts-refresh"
    # Act
    got = _names.canonical(typed)
    # Assert
    assert got == "scitex-agent-container-accounts-refresh"


def test_canonicalising_is_idempotent() -> None:
    # Arrange — a name copied out of --json output or a unit filename must
    # resolve to itself, not to a double prefix.
    typed = "scitex-agent-container-accounts-refresh"
    # Act
    got = _names.canonical(typed)
    # Assert
    assert got == "scitex-agent-container-accounts-refresh"


def test_a_legacy_name_is_not_re_prefixed() -> None:
    # Arrange — THE dangerous direction. A legacy name still names a real
    # deployed unit; rewriting it here would point every verb at a unit
    # that does not exist, and for the held OAuth refresher that means
    # reporting the fleet's credential machinery as absent while it runs.
    typed = "sac.accounts-refresh"
    # Act
    got = _names.canonical(typed)
    # Assert
    assert got == "sac.accounts-refresh"


def test_a_legacy_name_is_recognised_as_ours() -> None:
    # Arrange — the ownership filter must not drop the held job, or
    # `sac dev timer list` stops showing the refresher entirely.
    typed = "sac.accounts-refresh"
    # Act
    got = _names.is_ours(typed)
    # Assert
    assert got is True


def test_local_strips_the_legacy_prefix_too() -> None:
    # Arrange
    typed = "sac.accounts-refresh"
    # Act
    got = _names.local(typed)
    # Assert
    assert got == "accounts-refresh"


def test_a_local_name_offers_the_canonical_form_first() -> None:
    # Arrange — resolution order decides which of two live prefixes wins
    # for a bare short name.
    typed = "worktree-gc"
    # Act
    got = _names.candidates(typed)
    # Assert
    assert got[0] == "scitex-agent-container-worktree-gc"


def test_a_local_name_still_offers_the_legacy_form() -> None:
    # Arrange — without this the held refresher is unreachable by its
    # short name, which is the name every runbook uses.
    typed = "accounts-refresh"
    # Act
    got = _names.candidates(typed)
    # Assert
    assert "sac.accounts-refresh" in got


def test_an_already_prefixed_name_is_unambiguous() -> None:
    # Arrange — a name that carries a prefix means exactly one thing.
    typed = "sac.worktree-gc"
    # Act
    got = _names.candidates(typed)
    # Assert
    assert got == ("sac.worktree-gc",)


def test_an_empty_name_is_rejected() -> None:
    # Arrange — an empty name would canonicalise to the bare prefix and
    # match nothing, silently.
    typed = ""

    # Act
    def _call():
        return _names.canonical(typed)

    # Assert
    with pytest.raises(ValueError):
        _call()


def test_local_strips_the_prefix() -> None:
    # Arrange
    canonical = "sac.worktree-gc"
    # Act
    got = _names.local(canonical)
    # Assert
    assert got == "worktree-gc"


def test_local_leaves_a_foreign_name_alone() -> None:
    # Arrange — another package's job must not be mangled into looking
    # like one of ours.
    foreign = "scitex-todo.dashboard"
    # Act
    got = _names.local(foreign)
    # Assert
    assert got == foreign


def test_is_ours_rejects_a_foreign_job() -> None:
    # Arrange — this predicate is the filter that keeps `sac dev … list`
    # from claiming other packages' jobs.
    foreign = "scitex-todo.dashboard"
    # Act
    got = _names.is_ours(foreign)
    # Assert
    assert got is False


def test_resolve_finds_a_real_declared_job_by_local_name() -> None:
    # Arrange — against the REAL declarations, not a hand-picked list.
    declared = [j.name for j in provide_jobs()]
    # Act
    got = _names.resolve("accounts-refresh", declared)
    # Assert
    assert got == "sac.accounts-refresh"


def test_resolve_raises_on_an_unknown_name() -> None:
    # Arrange
    declared = ["sac.accounts-refresh"]

    # Act
    def _call():
        return _names.resolve("typo", declared)

    # Assert
    with pytest.raises(KeyError):
        _call()


def test_the_unknown_name_error_lists_what_is_available() -> None:
    # Arrange — an error that does not say what WOULD have worked makes
    # the operator go read the source.
    declared = ["sac.accounts-refresh"]
    # Act
    try:
        _names.resolve("typo", declared)
        message = ""
    except KeyError as exc:
        message = str(exc.args[0])
    # Assert
    assert "accounts-refresh" in message


def test_every_declared_job_uses_the_declared_prefix() -> None:
    # Arrange — the prefix constant is the seam for the ecosystem rename;
    # a job that does not use it would be missed by that rename AND by
    # every `sac dev` verb's ownership filter.
    declared = [j.name for j in provide_jobs()]
    # Act
    strays = [n for n in declared if not _names.is_ours(n)]
    # Assert
    assert strays == []
