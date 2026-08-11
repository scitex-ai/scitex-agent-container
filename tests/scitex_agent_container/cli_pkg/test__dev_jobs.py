"""Tests for ``sac dev {service,timer,cron}`` — the kind-named job grammar.

WHY THIS FILE KEEPS ITS OWN POSTMORTEM

An earlier version installed a hand-rolled fake ``scitex_dev.jobs`` module
into ``sys.modules`` whose ``_Job`` dataclass defaulted to
``kind="systemd"``. No real JobSpec can have that kind: ``ALLOWED_KINDS``
is ``{service,timer,cron}`` and ``JobSpec.validate()`` raises on anything
else at construction. So the suite asserted, in green, that ``sac dev
systemd list`` shows ``sac.accounts-refresh`` — while in production that
command printed "No sac systemd-kind jobs." and exited 0, because the
group name was passed straight through as the kind filter and every sac
timer is ``kind="timer"``.

A fake whose shape no real object can have does not test the production
path; it tests the fake. These tests therefore drive the REAL
``scitex_dev.jobs`` with REAL ``JobSpec`` objects. If the contract is not
installed, the file skips — it does not invent a stand-in.

TWO STREAMS, AND THE TRAP BETWEEN THEM

click 8.4's ``Result.output`` is stdout **and stderr merged**;
``Result.stdout`` is stdout alone. Two tests here used to parse
``result.output`` as JSON, which is not the contract: it passed only
while nothing else wrote to stderr, and went red the moment a THIRD-PARTY
jobs provider failed to load and ``scitex_dev.jobs`` logged a warning —
correctly, on stderr. The real ``sac dev … --json`` stdout was clean the
whole time. Every JSON assertion below reads ``result.stdout``, and one
test proves the contract end-to-end in a REAL SUBPROCESS where the two
streams are genuinely separate files.

No mocks (PA-306). The one seam injected is ``_dev_jobs._delegate``,
which would otherwise shell out to a real ``scitex-dev`` and rewrite the
host's units and crontab; the delegation ARGUMENTS are what those tests
are about, and they are captured, not faked.

AAA marker comments; one assertion per test.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Iterator

import pytest
from click.testing import CliRunner

jobs_mod = pytest.importorskip(
    "scitex_dev.jobs",
    reason="installed scitex-dev predates the scitex_dev.jobs contract",
)

import scitex_agent_container  # noqa: E402
import scitex_agent_container.cli_pkg._dev_jobs as dj  # noqa: E402
from scitex_agent_container._jobs import _names  # noqa: E402
from scitex_agent_container._jobs._jobs_plugin import provide_jobs  # noqa: E402
from scitex_agent_container.cli_pkg._dev_jobs import (  # noqa: E402
    DEPRECATED_GROUPS,
    GROUP_KINDS,
    GROUP_VERBS,
    Deprecation,
)
from scitex_agent_container.cli_pkg.dev_group import dev_group  # noqa: E402


def _declared(kind: str) -> list[str]:
    """Canonical names of the jobs sac really declares for ``kind``."""
    return [j.name for j in provide_jobs() if j.kind == kind]


@contextmanager
def _jobs_absent() -> Iterator[None]:
    """Force ``import scitex_dev.jobs`` to raise ImportError."""
    saved = sys.modules.get("scitex_dev.jobs")
    sys.modules["scitex_dev.jobs"] = None  # type: ignore[assignment]
    try:
        yield
    finally:
        if saved is None:
            del sys.modules["scitex_dev.jobs"]
        else:
            sys.modules["scitex_dev.jobs"] = saved


@contextmanager
def _captured_delegations() -> Iterator[list[tuple]]:
    """Capture ``(kind, verb, name, yes)`` instead of shelling scitex-dev."""
    captured: list[tuple] = []
    original = dj._delegate
    dj._delegate = lambda *a, **k: captured.append(a) or 0  # type: ignore[assignment]
    try:
        yield captured
    finally:
        dj._delegate = original  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# the grammar: group name IS the kind
# ---------------------------------------------------------------------------


def test_there_is_a_group_per_jobspec_kind() -> None:
    # Arrange — the decided grammar: `dev {service,timer,cron} <verb>`.
    expected = set(jobs_mod.ALLOWED_KINDS)
    # Act
    present = {k for k in expected if k in dev_group.commands}
    # Assert
    assert present == expected


def test_every_kind_group_filters_on_exactly_its_own_name() -> None:
    # Arrange — the identity that makes the historical bug unrepresentable.
    kinds = set(jobs_mod.ALLOWED_KINDS)
    # Act
    mismatched = {g for g in kinds if GROUP_KINDS[g] != frozenset({g})}
    # Assert
    assert mismatched == set()


def test_job_groups_are_exactly_the_group_kinds_ssot() -> None:
    # Arrange — the CLI surface is derived from GROUP_KINDS, so a group can
    # never exist without a declared kind filter behind it.
    expected = set(GROUP_KINDS)
    # Act
    present = {g for g in expected if g in dev_group.commands}
    # Assert
    assert present == expected


def test_there_is_no_daemon_group() -> None:
    # Arrange — `sac dev daemon` was dead in BOTH halves: it filtered
    # kind="daemon" (never legal, so always zero jobs) AND delegated to
    # `scitex-dev ecosystem daemon`, which is not an ecosystem subcommand
    # at all. A long-running job is kind="service".
    groups = dev_group.commands
    # Act
    present = "daemon" in groups
    # Assert
    assert present is False


# ---------------------------------------------------------------------------
# list — NON-EMPTY against the real provider. Zero jobs was the bug.
# ---------------------------------------------------------------------------


def test_timer_list_is_not_empty() -> None:
    # Arrange — the regression that matters. A group that can only ever
    # return zero jobs is exactly what shipped for weeks, reporting
    # "No sac systemd-kind jobs." with exit 0.
    runner = CliRunner()
    # Act
    result = runner.invoke(dev_group, ["timer", "list"])
    # Assert
    assert "No sac timer jobs." not in result.stdout


def test_timer_list_shows_every_declared_timer() -> None:
    # Arrange — all of them, not just the one a pinning test happens to
    # name. sac declares nine timers today.
    expected = [_names.local(n) for n in _declared("timer")]
    runner = CliRunner()
    # Act
    result = runner.invoke(dev_group, ["timer", "list"])
    # Assert
    assert all(name in result.stdout for name in expected)


def test_timer_list_json_lists_every_declared_timer() -> None:
    # Arrange — the count is the assertion: a filter that matches nothing
    # also "succeeds".
    expected = set(_declared("timer"))
    runner = CliRunner()
    # Act
    result = runner.invoke(dev_group, ["timer", "list", "--json"])
    # Assert
    assert {j["name"] for j in json.loads(result.stdout)} == expected


def test_timer_list_exit_zero() -> None:
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(dev_group, ["timer", "list"])
    # Assert
    assert result.exit_code == 0


def test_timer_list_json_reports_the_real_kind() -> None:
    # Arrange — the JSON surfaces `kind` precisely so a future taxonomy
    # drift is visible rather than silently filtered to nothing.
    runner = CliRunner()
    # Act
    result = runner.invoke(dev_group, ["timer", "list", "--json"])
    # Assert
    assert {j["kind"] for j in json.loads(result.stdout)} == {"timer"}


def test_timer_list_filters_out_foreign_jobs() -> None:
    # Arrange — scitex-dev's and scitex-cards' own jobs are discoverable
    # too; only sac's may show here.
    runner = CliRunner()
    # Act
    result = runner.invoke(dev_group, ["timer", "list", "--json"])
    # Assert
    assert all(_names.is_ours(j["name"]) for j in json.loads(result.stdout))


def test_cron_list_is_empty_because_sac_declares_no_cron_jobs() -> None:
    # Arrange — a TRUE empty (sac has no kind="cron" job), unlike the
    # systemd group's old empty, which was a bug wearing the same output.
    runner = CliRunner()
    # Act
    result = runner.invoke(dev_group, ["cron", "list"])
    # Assert
    assert "No sac cron jobs." in result.stdout


def test_service_list_empty_is_consistent_with_what_sac_declares() -> None:
    # Arrange — sac declares no kind="service" job today (`sac listen` is
    # deliberately NOT federated; see _jobs_plugin). So this group's empty
    # must be a TRUE empty, and the test states which one it is rather
    # than pinning a hardcoded expectation.
    declared = _declared("service")
    runner = CliRunner()
    # Act
    result = runner.invoke(dev_group, ["service", "list"])
    # Assert
    assert ("No sac service jobs." in result.stdout) is (declared == [])


# ---------------------------------------------------------------------------
# stdout hygiene — --json must stay machine-parseable
# ---------------------------------------------------------------------------


def test_json_stdout_carries_no_deprecation_note() -> None:
    # Arrange — THE stdout-purity case: the deprecated alias prints its
    # notice on every invocation, including this one.
    runner = CliRunner()
    # Act
    result = runner.invoke(dev_group, ["systemd", "list", "--json"])
    # Assert — stdout ALONE parses; the note is not in it.
    assert isinstance(json.loads(result.stdout), list)


def test_deprecation_note_goes_to_stderr() -> None:
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(dev_group, ["systemd", "list", "--json"])
    # Assert
    assert "DEPRECATED" in result.stderr


def test_json_stdout_parses_in_a_real_subprocess() -> None:
    # Arrange — the only place the two streams are genuinely separate
    # files. CliRunner's `output` merges them, which is how a stdout test
    # can pass while stdout is filthy. PYTHONPATH pins the subprocess to
    # the SAME source tree this test imported, so a linked worktree is
    # never silently tested against the main checkout's installed code.
    src = str(Path(scitex_agent_container.__file__).resolve().parent.parent)
    env = dict(os.environ, PYTHONPATH=src)
    # Act
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "scitex_agent_container",
            "dev",
            "timer",
            "list",
            "--json",
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    # Assert
    assert len(json.loads(proc.stdout)) == len(_declared("timer"))


# ---------------------------------------------------------------------------
# deprecation — with an expiry, enforced
# ---------------------------------------------------------------------------


def test_systemd_is_the_only_deprecated_group() -> None:
    # Arrange
    expected = {"systemd"}
    # Act
    present = set(DEPRECATED_GROUPS)
    # Assert
    assert present == expected


def test_systemd_deprecation_carries_the_decided_dates() -> None:
    # Arrange — real values in code, not a vague "for now".
    dep = DEPRECATED_GROUPS["systemd"]
    # Act
    stamped = (dep.since, dep.remove_after)
    # Assert
    assert stamped == ("2026-08", "2026-10")


def test_the_deprecation_deadline_has_not_passed() -> None:
    # Arrange — THE ENFORCEMENT. A written date nobody checks is the same
    # as no date: this test turns the build red once the removal window
    # closes, so retiring `sac dev systemd` becomes a required action
    # rather than an intention.
    today = date.today().strftime("%Y-%m")
    # Act
    expired = [g for g, d in DEPRECATED_GROUPS.items() if d.is_expired(today)]
    # Assert
    assert not expired, (
        f"deprecation window closed for {expired} — delete these groups from "
        "GROUP_KINDS/GROUP_VERBS/DEPRECATED_GROUPS and their docs"
    )


def test_deprecation_rejects_a_vague_date() -> None:
    # Arrange — "2026" is how "for the time being" gets back in.
    kwargs = dict(since="2026-08", replacement="x")

    # Act
    def _build():
        return Deprecation(remove_after="2026", **kwargs)

    # Assert
    with pytest.raises(ValueError):
        _build()


def test_deprecation_rejects_a_removal_date_before_it_starts() -> None:
    # Arrange
    kwargs = dict(since="2026-10", replacement="x")

    # Act
    def _build():
        return Deprecation(remove_after="2026-08", **kwargs)

    # Assert
    with pytest.raises(ValueError):
        _build()


def test_deprecation_requires_a_replacement() -> None:
    # Arrange — a deprecation with nothing to move to is a complaint.
    kwargs = dict(since="2026-08", remove_after="2026-10")

    # Act
    def _build():
        return Deprecation(replacement="", **kwargs)

    # Assert
    with pytest.raises(ValueError):
        _build()


def test_the_alias_gains_no_new_verbs() -> None:
    # Arrange — the alias keeps EXACTLY its historical surface so nothing
    # new gets built on something with a removal date.
    frozen = {"list", "install", "uninstall"}
    # Act
    verbs = set(GROUP_VERBS["systemd"])
    # Assert
    assert verbs == frozen


# ---------------------------------------------------------------------------
# verb sets — per kind, and a verb that makes no sense does not exist
# ---------------------------------------------------------------------------


def _verbs_of(group: str) -> set[str]:
    """The leaf subcommand names exposed under ``sac dev <group>``."""
    grp = dev_group.commands[group]
    return set(grp.commands)  # type: ignore[attr-defined]


def test_declared_verbs_are_the_verbs_actually_wired() -> None:
    # Arrange — GROUP_VERBS is the SSOT; a declared verb that is not wired
    # is a declaration with no live counterpart.
    expected = {g: set(v) for g, v in GROUP_VERBS.items()}
    # Act
    wired = {g: _verbs_of(g) for g in GROUP_VERBS}
    # Assert
    assert wired == expected


def test_service_has_the_full_lifecycle() -> None:
    # Arrange — a service is a long-running unit.
    expected = {
        "list",
        "status",
        "start",
        "stop",
        "restart",
        "enable",
        "disable",
        "install",
        "uninstall",
    }
    # Act
    verbs = _verbs_of("service")
    # Assert
    assert verbs == expected


def test_timer_has_no_start_verb() -> None:
    # Arrange — `enable --now` is the systemd idiom for a timer; a second
    # spelling with different edge cases is worse than none.
    verbs = _verbs_of("timer")
    # Act
    present = "start" in verbs
    # Assert
    assert present is False


def test_cron_has_no_status_verb() -> None:
    # Arrange — a crontab line is present or commented out; there is no
    # runtime object to ask. The verb does not exist rather than existing
    # and erroring.
    verbs = _verbs_of("cron")
    # Act
    present = "status" in verbs
    # Assert
    assert present is False


# ---------------------------------------------------------------------------
# delegation — the KIND is what travels, never the group name
# ---------------------------------------------------------------------------


def test_install_delegates_once_per_declared_timer() -> None:
    # Arrange — the count is the point: a group that silently matched zero
    # jobs delegated zero times and still exited 0.
    expected = len(_declared("timer"))
    with _captured_delegations() as captured:
        runner = CliRunner()
        # Act
        runner.invoke(dev_group, ["timer", "install", "-y"])
    # Assert
    assert len(captured) == expected


def test_install_delegates_with_the_kind_not_the_group_name() -> None:
    # Arrange — THE historical bug, at the delegation boundary.
    with _captured_delegations() as captured:
        runner = CliRunner()
        # Act
        runner.invoke(dev_group, ["timer", "install", "-y"])
    # Assert
    assert captured and all(a[:2] == ("timer", "install") for a in captured)


def test_the_alias_delegates_on_a_kind_too_never_on_systemd() -> None:
    # Arrange — the deprecated group name must not reach the kind axis.
    with _captured_delegations() as captured:
        runner = CliRunner()
        # Act
        runner.invoke(dev_group, ["systemd", "install", "-y"])
    # Assert
    assert captured and all(a[0] in jobs_mod.ALLOWED_KINDS for a in captured)


def test_install_accepts_the_short_local_name() -> None:
    # Arrange — the operator types the local name inside sac's own CLI.
    with _captured_delegations() as captured:
        runner = CliRunner()
        # Act
        runner.invoke(dev_group, ["timer", "install", "accounts-refresh", "-y"])
    # Assert
    assert [a[2] for a in captured] == ["sac.accounts-refresh"]


def test_install_accepts_the_canonical_name_too() -> None:
    # Arrange — a name copied out of --json output or off a unit filename.
    with _captured_delegations() as captured:
        runner = CliRunner()
        # Act
        runner.invoke(dev_group, ["timer", "install", "sac.accounts-refresh", "-y"])
    # Assert
    assert [a[2] for a in captured] == ["sac.accounts-refresh"]


def test_an_unknown_job_name_exits_five() -> None:
    # Arrange — a verb that silently does nothing for a typo is how a job
    # quietly stops being scheduled.
    runner = CliRunner()
    # Act
    result = runner.invoke(dev_group, ["timer", "install", "no-such-job", "-y"])
    # Assert
    assert result.exit_code == 5


def test_an_unknown_job_name_lists_the_real_ones() -> None:
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(dev_group, ["timer", "install", "no-such-job", "-y"])
    # Assert
    assert "accounts-refresh" in result.stderr


# ---------------------------------------------------------------------------
# verbs the installed scitex-dev cannot serve yet
# ---------------------------------------------------------------------------


def test_a_verb_the_backend_cannot_serve_exits_four() -> None:
    # Arrange — `ecosystem timer` does not exist yet and `ecosystem
    # systemd` has no `status`, so this is a REAL unsupported path today,
    # measured rather than simulated. It must fail loudly, not pretend.
    runner = CliRunner()
    # Act
    result = runner.invoke(dev_group, ["timer", "status", "accounts-refresh"])
    # Assert
    assert result.exit_code == 4


def test_an_unsupported_verb_states_what_it_probed() -> None:
    # Arrange — a refusal with no evidence is a claim.
    runner = CliRunner()
    # Act
    result = runner.invoke(dev_group, ["timer", "status", "accounts-refresh"])
    # Assert
    assert "ecosystem timer status" in result.stderr


def test_an_unsupported_verb_offers_the_manual_command() -> None:
    # Arrange — reporting a command is not running it; sac still does not
    # own systemctl.
    runner = CliRunner()
    # Act
    result = runner.invoke(dev_group, ["timer", "status", "accounts-refresh"])
    # Assert
    assert "systemctl --user status sac.accounts-refresh.timer" in result.stderr


def test_an_unsupported_verb_writes_nothing_to_stdout() -> None:
    # Arrange — same stdout-purity rule as --json.
    runner = CliRunner()
    # Act
    result = runner.invoke(dev_group, ["timer", "status", "accounts-refresh"])
    # Assert
    assert result.stdout == ""


# ---------------------------------------------------------------------------
# degrade — scitex-dev too old
# ---------------------------------------------------------------------------


def test_list_degrades_with_upgrade_hint_when_jobs_absent() -> None:
    # Arrange
    with _jobs_absent():
        runner = CliRunner()
        # Act
        result = runner.invoke(dev_group, ["timer", "list"])
    # Assert — upgrade hint surfaces (no stack trace), on stderr.
    assert "requires scitex-dev>=" in result.stderr


def test_list_degrade_exit_code_is_three() -> None:
    # Arrange
    with _jobs_absent():
        runner = CliRunner()
        # Act
        result = runner.invoke(dev_group, ["timer", "list"])
    # Assert — clean non-zero exit, not an unhandled exception.
    assert result.exit_code == 3 and result.exception.__class__ is SystemExit


def test_degrade_writes_nothing_to_stdout() -> None:
    # Arrange — an upgrade hint on stdout would corrupt a --json consumer
    # exactly like a WARN does.
    with _jobs_absent():
        runner = CliRunner()
        # Act
        result = runner.invoke(dev_group, ["timer", "list", "--json"])
    # Assert
    assert result.stdout == ""


def test_install_degrades_when_jobs_absent() -> None:
    # Arrange
    with _jobs_absent():
        runner = CliRunner()
        # Act
        result = runner.invoke(dev_group, ["timer", "install", "-y"])
    # Assert
    assert "requires scitex-dev>=" in result.stderr
