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

from contextlib import contextmanager
from typing import Iterator

import click
import pytest

pytest.importorskip(
    "scitex_dev.jobs",
    reason="installed scitex-dev predates the scitex_dev.jobs contract",
)

from scitex_agent_container.cli_pkg import _dev_jobs_backend as backend  # noqa: E402

# ---------------------------------------------------------------------------
# Fixtures: REAL click trees shaped like the ecosystem versions we must
# survive. Not mocks — `_walk` is pointed at an actual Group and has to
# find its way down, exactly as it does against the installed package.
# ---------------------------------------------------------------------------


def _group(name: str, leaves: tuple[str, ...] = ()) -> click.Group:
    grp = click.Group(name)
    for leaf in leaves:
        grp.add_command(click.Command(leaf))
    return grp


def _tree_pre_move() -> click.Group:
    """scitex-dev <= 0.42: job groups at the TOP level of `ecosystem`."""
    eco = _group("ecosystem")
    eco.add_command(_group("cron", ("exec", "install", "list", "uninstall")))
    eco.add_command(_group("systemd", ("install", "list", "uninstall")))
    return eco


def _tree_moved_no_kinds() -> click.Group:
    """The MEASURED scitex-dev 0.43.1 shape.

    The job groups live under `dev`; the per-KIND groups do not exist
    yet; and the OLD top-level names survive as deprecated forwarding
    `Command` shims — plain Commands, not Groups, so they enumerate as
    zero verbs while `ecosystem systemd install` runs fine. Their help
    says "Removed in v0.50", so they are also a time bomb to target.
    """
    eco = _group("ecosystem")
    eco.add_command(
        click.Command("cron", help="(deprecated) Forwards to 'ecosystem dev cron'.")
    )
    eco.add_command(
        click.Command(
            "systemd", help="(deprecated) Forwards to 'ecosystem dev systemd'."
        )
    )
    dev = _group("dev")
    dev.add_command(_group("cron", ("exec", "install", "list", "uninstall")))
    dev.add_command(_group("systemd", ("install", "list", "uninstall")))
    eco.add_command(dev)
    return eco


def _tree_566() -> click.Group:
    """scitex-dev PR #566: per-KIND groups under `ecosystem dev`."""
    eco = _group("ecosystem")
    dev = _group("dev")
    dev.add_command(
        _group(
            "service",
            ("list", "status", "start", "stop", "restart", "install", "uninstall"),
        )
    )
    dev.add_command(
        _group(
            "timer",
            ("list", "status", "enable", "disable", "install", "uninstall"),
        )
    )
    dev.add_command(
        _group("cron", ("list", "enable", "disable", "install", "uninstall", "exec"))
    )
    eco.add_command(dev)
    return eco


@contextmanager
def _installed(tree: click.Group) -> Iterator[None]:
    """Point the probe at ``tree`` instead of the installed scitex-dev."""
    backend.reset_capability_cache()
    original = backend.ecosystem_verbs
    walked = backend._walk(tree)
    backend.ecosystem_verbs = lambda: walked  # type: ignore[assignment]
    try:
        yield
    finally:
        backend.ecosystem_verbs = original  # type: ignore[assignment]
        backend.reset_capability_cache()


def test_the_ecosystem_tree_is_readable() -> None:
    # Arrange — every capability answer below rests on this probe being
    # able to read the installed package at all.
    # Act
    verbs = backend.ecosystem_verbs()
    # Assert
    assert verbs is not None


def test_the_probe_reaches_a_job_surface_with_real_verbs() -> None:
    # Arrange — a POSITIVE CONTROL against the INSTALLED package. A probe
    # that reads nothing makes every "unsupported" verdict below
    # worthless, and "the group exists" is NOT enough evidence: measured
    # on scitex-dev 0.43.1, `ecosystem cron` and `ecosystem systemd` are
    # both PRESENT and both EMPTY, because the verbs moved under
    # `ecosystem dev`. So this asserts a path that actually carries verbs.
    tree = backend.ecosystem_verbs() or {}
    # Act
    with_verbs = {path for path, verbs in tree.items() if verbs}
    # Assert
    assert with_verbs, f"probe found no verbs anywhere; walked: {sorted(tree)}"


# --- the WALK: does it start at the node that will hold the kind groups? ---


def test_the_walk_descends_into_the_dev_subgroup() -> None:
    # Arrange — THE question. scitex-dev PR #566 mounts the kind groups at
    # `ecosystem dev <kind>`, one level DOWN. A probe enumerating only
    # `ecosystem`'s direct children reports "no service/timer surface"
    # forever — the silent-zero shape this whole card exists to kill.
    walked = backend._walk(_tree_566())
    # Act
    timer_verbs = walked.get(("dev", "timer"), frozenset())
    # Assert
    assert {"list", "status", "enable", "disable"} <= timer_verbs


def test_timer_status_resolves_against_the_566_tree() -> None:
    # Arrange — the verb that is unsupported today must become supported
    # the moment #566 is installed, with NO sac release.
    with _installed(_tree_566()):
        # Act
        got = backend.resolve("timer", "status")
    # Assert
    assert (got.path, got.supported) == (("dev", "timer"), True)


def test_service_restart_resolves_against_the_566_tree() -> None:
    # Arrange
    with _installed(_tree_566()):
        # Act
        got = backend.resolve("service", "restart")
    # Assert
    assert (got.path, got.supported) == (("dev", "service"), True)


def test_the_kind_group_wins_over_the_legacy_lump_group() -> None:
    # Arrange — preference order: the surface the ecosystem is moving TO.
    with _installed(_tree_566()):
        # Act
        got = backend.resolve("timer", "install")
    # Assert
    assert got.path == ("dev", "timer")


def test_the_moved_tree_resolves_to_dev_systemd_not_the_shim() -> None:
    # Arrange — THE MEASURED 0.43.1 shape. `ecosystem systemd` still
    # resolves, but it is a DEPRECATED forwarding shim whose own help
    # says "Removed in v0.50". Targeting it works today and breaks on a
    # scitex-dev upgrade, so the real group must win.
    with _installed(_tree_moved_no_kinds()):
        # Act
        got = backend.resolve("timer", "install")
    # Assert
    assert (got.path, got.supported) == (("dev", "systemd"), True)


def test_the_live_installed_tree_is_not_targeted_through_a_shim() -> None:
    # Arrange — against the INSTALLED scitex-dev, not a fixture. If this
    # resolves to the bare ("systemd",) shim, every mutating verb breaks
    # silently when scitex-dev reaches v0.50.
    # Act
    got = backend.resolve("timer", "install")
    # Assert
    assert got.path != ("systemd",), f"targeting the removed-in-v0.50 shim: {got.group}"


def test_the_pre_move_tree_still_resolves_at_the_top_level() -> None:
    # Arrange — an OLDER scitex-dev must keep working; the walk must not
    # require the `dev` subgroup to exist.
    with _installed(_tree_pre_move()):
        # Act
        got = backend.resolve("timer", "install")
    # Assert
    assert (got.path, got.supported) == (("systemd",), True)


def test_cron_install_resolves_to_a_supported_delegation() -> None:
    # Arrange — against the INSTALLED package, whichever level it mounts.
    # Act
    got = backend.resolve("cron", "install")
    # Assert
    assert got.supported is True


def test_timer_install_resolves_to_a_supported_delegation() -> None:
    # Arrange
    # Act
    got = backend.resolve("timer", "install")
    # Assert
    assert got.supported is True


# --- the THIRD STATE: dev present, kind children absent ---


def test_dev_present_with_kind_children_absent_is_cannot_tell() -> None:
    # Arrange — an older scitex-dev that has done the `dev` move but has
    # NOT split the kinds. `dev service`/`dev timer` report nothing.
    # Reading that as "verb unsupported" would refuse `install` on a
    # perfectly working install, so it must land in the third state and
    # be ATTEMPTED against the surface that does carry the verb.
    with _installed(_tree_moved_no_kinds()):
        # Act
        got = backend.resolve("service", "install")
    # Assert
    assert got.supported is True


def test_a_totally_empty_tree_is_cannot_tell_not_unsupported() -> None:
    # Arrange — every candidate path absent. Zero everywhere is not a
    # credible reading of a scitex-dev that has shipped these verbs since
    # 0.16.0; it is a probe that failed.
    with _installed(_group("ecosystem")):
        # Act
        got = backend.resolve("timer", "install")
    # Assert
    assert got.supported is True


def test_cannot_tell_prefers_dev_when_a_dev_subgroup_exists() -> None:
    # Arrange — if `dev` is there, the shipped verbs are behind it.
    eco = _group("ecosystem")
    eco.add_command(_group("dev"))
    with _installed(eco):
        # Act
        got = backend.resolve("timer", "install")
    # Assert
    assert got.path == ("dev", "systemd")


def test_a_verb_no_ecosystem_group_serves_is_unsupported() -> None:
    # Arrange — `status` exists in sac's grammar but on no surface the
    # pre-#566 tree serves, and that tree DOES report other verbs, so
    # this is genuine absence rather than an unreadable probe.
    with _installed(_tree_pre_move()):
        # Act
        got = backend.resolve("timer", "status")
    # Assert
    assert got.supported is False


def test_an_unsupported_verdict_names_every_path_it_probed() -> None:
    # Arrange — a refusal that cannot say what it looked for is a claim.
    with _installed(_tree_pre_move()):
        # Act
        got = backend.resolve("timer", "status")
    # Assert
    assert "ecosystem dev timer" in got.evidence


# --- the GATE: --dry-run / --yes must survive the pass-through ---


def test_the_pass_through_forwards_dry_run() -> None:
    # Arrange — scitex-dev's gate is load-bearing: `timer disable
    # sac.accounts-refresh` stops the fleet's SOLE OAuth refresher. A
    # pass-through that drops the flag turns a guarded command into an
    # unguarded one.
    delegation = backend.Delegation(
        path=("dev", "timer"), verb="disable", supported=True, evidence="fixture"
    )
    # Act
    argv = backend.build_argv(
        delegation, name="sac.accounts-refresh", yes=False, dry_run=True, exe="sd"
    )
    # Assert
    assert argv == [
        "sd",
        "ecosystem",
        "dev",
        "timer",
        "disable",
        "--name",
        "sac.accounts-refresh",
        "--dry-run",
    ]


def test_the_pass_through_forwards_yes() -> None:
    # Arrange
    delegation = backend.Delegation(
        path=("dev", "timer"), verb="install", supported=True, evidence="fixture"
    )
    # Act
    argv = backend.build_argv(delegation, name="sac.x", yes=True, exe="sd")
    # Assert
    assert argv[-1] == "--yes"


def test_the_pass_through_adds_neither_flag_unasked() -> None:
    # Arrange — sac must not decide confirmation on the operator's behalf
    # in either direction.
    delegation = backend.Delegation(
        path=("cron",), verb="install", supported=True, evidence="fixture"
    )
    # Act
    argv = backend.build_argv(delegation, name="sac.x", yes=False, exe="sd")
    # Assert
    assert argv == ["sd", "ecosystem", "cron", "install", "--name", "sac.x"]


def test_a_delegation_must_state_its_evidence() -> None:
    # Arrange — same doctrine as _jobs_audit.Finding.detail.
    kwargs = dict(path=("systemd",), verb="list", supported=True)

    # Act
    def _build():
        return backend.Delegation(evidence="", **kwargs)

    # Assert
    with pytest.raises(ValueError):
        _build()


def test_a_delegation_renders_a_nested_path_as_it_is_typed() -> None:
    # Arrange — the evidence and error messages quote this, and an
    # operator has to be able to paste it.
    delegation = backend.Delegation(
        path=("dev", "timer"), verb="status", supported=True, evidence="fixture"
    )
    # Act
    rendered = delegation.group
    # Assert
    assert rendered == "dev timer"


def test_invoking_an_unsupported_delegation_is_refused() -> None:
    # Arrange — the caller must translate "unsupported" into a message,
    # never shell out anyway.
    unsupported = backend.Delegation(
        path=("dev", "timer"),
        verb="status",
        supported=False,
        evidence="probed, absent",
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
