"""``sac agents restart-login-expired`` — command wiring, help, arg validation.

The command is a thin shell over :func:`.._authheal.auth_heal_pass` (whose
behaviour is exercised in depth, with injected panes and a real temp store, in
``tests/scitex_agent_container/_authheal/``). Here we pin only the CLI surface:
it is registered, its help documents the two modes AND the deploy gate, and the
contradictory-flags guard fires BEFORE any pass runs (so these tests never touch
real tmux or the real board).

No mocks: a real Click invocation against the real ``agents`` group.
"""

from __future__ import annotations

from click.testing import CliRunner

from scitex_agent_container._reconcile._rule import Verdict
from scitex_agent_container.cli_pkg._agents_restart_login_expired import (
    already_summarised_by_count,
)
from scitex_agent_container.cli_pkg.agent_group import agent_group


def test_command_registered_under_agents_group():
    # Arrange
    group = agent_group
    # Act
    registered = "restart-login-expired" in group.commands
    # Assert
    assert registered is True


def test_help_renders_apply_option():
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(agent_group, ["restart-login-expired", "--help"])
    # Assert
    assert "--apply" in result.output


def test_help_renders_check_option():
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(agent_group, ["restart-login-expired", "--help"])
    # Assert
    assert "--check" in result.output


def test_help_warns_about_the_deploy_gate():
    # Arrange — the double-supervisor hazard must be impossible to miss.
    runner = CliRunner()
    # Act
    result = runner.invoke(agent_group, ["restart-login-expired", "--help"])
    # Assert
    assert "DEPLOY GATE" in result.output


def test_apply_and_check_are_contradictory():
    # Arrange — the guard must fire before any pass runs, so this never touches
    # tmux or the board.
    runner = CliRunner()
    # Act
    result = runner.invoke(agent_group, ["restart-login-expired", "--apply", "--check"])
    # Assert
    assert result.exit_code != 0


def test_apply_and_check_error_explains_why():
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(agent_group, ["restart-login-expired", "--apply", "--check"])
    # Assert
    assert "contradictory" in result.output


# ---------------------------------------------------------------------------
# already_summarised_by_count — which reports the per-agent listing may skip.
#
# The suppression exists because a pass printed ~105 no-session agents EVERY
# five minutes on top of the count line that already said the same thing:
# 93,778 such lines in a 32 MB timer log, measured on compute-04 2026-08-18.
#
# The risk of a suppression is always that it hides the thing you needed, so
# the INDETERMINATE case is pinned separately and deliberately: those reports
# mean "nothing was learned", and silencing them would convert a loud gap into
# a quiet one.


class _FakeReport:
    """Minimal stand-in carrying only the fields the predicate reads.

    Not a mock: no behaviour is faked, no call is recorded. It is a value with
    two attributes, which is exactly what the predicate consumes.
    """

    def __init__(self, verdict, reason: str) -> None:
        self.verdict = verdict
        self.reason = reason


def test_no_session_unobserved_is_summarised_by_count():
    # Arrange — the determinate population, already reported once as a total.
    report = _FakeReport(Verdict.UNOBSERVED, "no-session")
    # Act
    skipped = already_summarised_by_count(report)
    # Assert
    assert skipped is True


def test_indeterminate_unobserved_is_still_printed_per_agent():
    # Arrange — "nothing was learned about this agent" must stay individually
    # visible; it is why a pass cannot report a clean fleet.
    report = _FakeReport(Verdict.UNOBSERVED, "pane-unreadable")
    # Act
    skipped = already_summarised_by_count(report)
    # Assert
    assert skipped is False


def test_a_restarted_agent_is_never_suppressed():
    # Arrange — a non-UNOBSERVED verdict must be unaffected even if some other
    # code path ever gives it a no-session reason.
    report = _FakeReport(Verdict.RESTARTED, "no-session")
    # Act
    skipped = already_summarised_by_count(report)
    # Assert
    assert skipped is False
