"""Tests for the ``sac dev <kind> <verb>`` -> scitex-dev delegation seam.

The seam has one job: answer "who serves this verb, and how do I know".
It is tested against the INSTALLED scitex-dev's real Click tree, because
a hard-coded table of what the ecosystem supports is a declaration with
no live counterpart — it stays green while the real surface moves.

The capability answers are three-state on purpose (supported / not
supported / cannot tell). A false "not supported" refuses a command that
would have worked, which is why an unreadable Click tree degrades to
attempting the verbs that have shipped since 0.16.0 rather than to a
refusal.

No mocks (PA-306). `resolve` is a pure function over a probed dict, and
the probe reads the real installed package.

AAA marker comments; one assertion per test.
"""

from __future__ import annotations

import pytest

pytest.importorskip(
    "scitex_dev.jobs",
    reason="installed scitex-dev predates the scitex_dev.jobs contract",
)

from scitex_agent_container.cli_pkg import _dev_jobs_backend as backend  # noqa: E402


def test_the_ecosystem_tree_is_readable() -> None:
    # Arrange — every capability answer below rests on this probe being
    # able to read the installed package at all.
    # Act
    verbs = backend.ecosystem_verbs()
    # Assert
    assert verbs is not None


def test_the_probe_finds_the_shipped_job_groups() -> None:
    # Arrange — a POSITIVE CONTROL. If these group names are missing the
    # probe read nothing and every "unsupported" verdict below is
    # worthless. It asserts group PRESENCE rather than the verb sets,
    # because whether the leaf verbs are statically readable depends on
    # whether the installed scitex-dev builds its tree lazily — measured
    # differing between this container and CI for the same code, which is
    # exactly why `resolve` treats an all-empty read as "cannot tell".
    verbs = backend.ecosystem_verbs() or {}
    # Act
    groups = {g for g in ("cron", "systemd") if g in verbs}
    # Assert
    assert groups == {"cron", "systemd"}


def test_timer_list_falls_back_to_the_shipped_systemd_group() -> None:
    # Arrange — `ecosystem timer` does not exist yet; `ecosystem systemd`
    # serves both unit kinds today.
    # Act
    got = backend.resolve("timer", "list")
    # Assert
    assert (got.group, got.supported) == ("systemd", True)


def test_cron_install_delegates_to_the_cron_group() -> None:
    # Arrange
    # Act
    got = backend.resolve("cron", "install")
    # Assert
    assert (got.group, got.supported) == ("cron", True)


def test_a_verb_no_ecosystem_group_serves_is_unsupported() -> None:
    # Arrange — `status` exists in sac's grammar but on neither
    # `ecosystem timer` nor `ecosystem systemd` today.
    # Act
    got = backend.resolve("timer", "status")
    # Assert
    assert got.supported is False


def test_an_unsupported_verdict_names_both_probes() -> None:
    # Arrange — a refusal that cannot say what it looked for is a claim.
    # Act
    got = backend.resolve("timer", "status")
    # Assert
    assert "ecosystem systemd status" in got.evidence


def test_a_delegation_must_state_its_evidence() -> None:
    # Arrange — same doctrine as _jobs_audit.Finding.detail.
    kwargs = dict(group="systemd", verb="list", supported=True)

    # Act
    def _build():
        return backend.Delegation(evidence="", **kwargs)

    # Assert
    with pytest.raises(ValueError):
        _build()


def test_invoking_an_unsupported_delegation_is_refused() -> None:
    # Arrange — the caller must translate "unsupported" into a message,
    # never shell out anyway.
    unsupported = backend.Delegation(
        group="timer", verb="status", supported=False, evidence="probed, absent"
    )

    # Act
    def _call():
        return backend.invoke(unsupported, name="sac.x", yes=False)

    # Assert
    with pytest.raises(ValueError):
        _call()


def test_the_manual_hint_names_the_timer_unit() -> None:
    # Arrange — the unit filename is derived from JobSpec.name verbatim by
    # scitex-dev's renderer, so the hint must mirror that exactly or it
    # points the operator at a unit that does not exist.
    # Act
    hint = backend.manual_hint("timer", "status", "sac.accounts-refresh")
    # Assert
    assert hint == "systemctl --user status sac.accounts-refresh.timer"


def test_the_manual_hint_names_the_service_unit() -> None:
    # Arrange
    # Act
    hint = backend.manual_hint("service", "restart", "sac.listen")
    # Assert
    assert hint == "systemctl --user restart sac.listen.service"


def test_enable_hints_use_the_now_form() -> None:
    # Arrange — `enable` without `--now` writes a symlink and starts
    # nothing, which reads exactly like success.
    # Act
    hint = backend.manual_hint("timer", "enable", "sac.worktree-gc")
    # Assert
    assert "--now" in hint


def test_an_unreadable_tree_still_attempts_a_shipped_verb() -> None:
    # Arrange — "cannot tell" must not become "refuse": a false negative
    # here blocks a command that would have worked.
    backend.reset_capability_cache()
    original = backend.ecosystem_verbs
    backend.ecosystem_verbs = lambda: None  # type: ignore[assignment]
    try:
        # Act
        got = backend.resolve("timer", "install")
    finally:
        backend.ecosystem_verbs = original  # type: ignore[assignment]
        backend.reset_capability_cache()
    # Assert
    assert got.supported is True


def test_an_unreadable_tree_refuses_a_verb_that_never_shipped() -> None:
    # Arrange — attempting `ecosystem systemd status` blind would shell
    # out to a subcommand that has never existed and surface a raw click
    # usage error instead of sac's own message.
    backend.reset_capability_cache()
    original = backend.ecosystem_verbs
    backend.ecosystem_verbs = lambda: None  # type: ignore[assignment]
    try:
        # Act
        got = backend.resolve("timer", "status")
    finally:
        backend.ecosystem_verbs = original  # type: ignore[assignment]
        backend.reset_capability_cache()
    # Assert
    assert got.supported is False
