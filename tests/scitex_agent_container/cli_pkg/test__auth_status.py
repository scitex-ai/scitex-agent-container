"""`sac agents auth-status` — the pure `evaluate_agents` mapper, the PERSIST
step, plus command registration/help.

`evaluate_agents` turns each agent's two captured panes into an OK / AUTH-FAILED
verdict via the near-prompt + distance-frozen matcher (the matcher itself is
exercised in depth in ``_runners/_tmux/test_auth_status.py``); here we confirm
the command-level real-vs-prose separation, and the half that makes the whole
feature work: this command is the WRITER, so its verdicts must actually land in
state.db for ``sac agents list`` to read back.

No mocks: pure calls, a real Click invocation, and a real PostgreSQL schema
via the shared ``pg_schema`` fixture (the store moved off SQLite 2026-08-24,
so there is no ``state.db`` to point at any more).
"""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from scitex_agent_container.cli_pkg._auth_status import (
    evaluate_agents,
    persist_verdicts,
)
from scitex_agent_container.cli_pkg.agent_group import agent_group

# Wedged: banner directly above the prompt, identical on both reads → frozen.
_STUCK = "● Login expired · Please run /login\n────────\n❯\n────────\n  ctx:1%\n"
# Healthy: no banner.
_OK = "  continuing the task now\n────────\n❯\n────────\n  ctx:1%\n"


def test_evaluate_agents_flags_frozen_banner_as_auth_failed():
    # Arrange
    captures = {"scitex-hpc": (_STUCK, _STUCK)}
    # Act
    row = evaluate_agents(captures)[0]
    # Assert
    assert row["verdict"] == "auth_failed"


def test_evaluate_agents_marks_clean_pane_ok():
    # Arrange
    captures = {"figrecipe": (_OK, _OK)}
    # Act
    row = evaluate_agents(captures)[0]
    # Assert
    assert row["verdict"] == "ok"


def test_evaluate_agents_uncapturable_agent_is_UNKNOWN_never_ok():
    """A pane we could not READ is UNKNOWN — absence of evidence, not health.

    This test previously asserted ``("ok", False)`` — it encoded the bug as the
    expected behaviour and stayed green while `sac agents auth-status` printed OK
    for agents it had never observed. An instrument reporting good news about a
    thing it did not look at is the same false-green that let a wedged agent read
    ALIVE; the verdict must be the third state.
    """
    # Arrange
    captures = {"gone": (None, None)}
    # Act
    row = evaluate_agents(captures)[0]
    # Assert
    assert (row["verdict"], row["captured"]) == ("unknown", False)


def test_evaluate_agents_unreadable_second_pane_is_UNKNOWN():
    """Corroboration needs the DECISIVE read: run 1 alone cannot yield a verdict.

    A session that vanishes between the two captures produced one pane and then
    nothing. Without the second read there is no frozen/moving judgement to make,
    so the honest answer is UNKNOWN rather than a verdict invented from run 1.
    """
    # Arrange
    captures = {"vanished": (_OK, None)}
    # Act
    row = evaluate_agents(captures)[0]
    # Assert
    assert (row["verdict"], row["captured"]) == ("unknown", False)


def test_unknown_row_note_says_no_evidence_not_health():
    """The row must carry WHY it is unknown, not just that it is."""
    # Arrange
    captures = {"gone": (None, None)}
    # Act
    note = evaluate_agents(captures)[0]["note"]
    # Assert
    assert "could not be read" in note


def test_evaluate_agents_sorts_rows_by_agent_name():
    # Arrange
    captures = {"zeta": (_OK, _OK), "alpha": (_OK, _OK)}
    # Act
    names = [r["agent"] for r in evaluate_agents(captures)]
    # Assert
    assert names == ["alpha", "zeta"]


def test_auth_status_command_registered_under_agents_group():
    # Arrange
    group = agent_group
    # Act
    registered = "auth-status" in group.commands
    # Assert
    assert registered is True


def test_auth_status_help_renders_interval_option():
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(agent_group, ["auth-status", "--help"])
    # Assert
    assert result.exit_code == 0 and "--interval" in result.output


# ---------------------------------------------------------------------------
# persist_verdicts — the WRITE half: what `sac agents list` reads back
# ---------------------------------------------------------------------------


def test_persisted_failing_verdict_is_readable_by_the_list(pg_schema: str):
    # Arrange — the contract that makes the feature work: the watchdog writes,
    # the list reads. A real sqlite file, a real row, no mocks.
    from scitex_agent_container._state.auth_state import list_auth_states
    rows = evaluate_agents({"scitex-hpc": (_STUCK, _STUCK)})
    # Act
    persist_verdicts(rows)
    # Assert
    assert list_auth_states()["scitex-hpc"]["auth_failed"] is True


def test_persisted_healthy_verdict_is_readable_by_the_list(pg_schema: str):
    # Arrange
    from scitex_agent_container._state.auth_state import list_auth_states
    rows = evaluate_agents({"figrecipe": (_OK, _OK)})
    # Act
    persist_verdicts(rows)
    # Assert
    assert list_auth_states()["figrecipe"]["auth_failed"] is False


def test_uncapturable_agent_is_not_recorded_as_healthy(tmp_path: Path):
    # Arrange — an agent we could not READ produced NO evidence. Writing
    # "auth is fine" for it would manufacture exactly the false green this
    # feature exists to abolish, so it must not be written at all.
    from scitex_agent_container._state.auth_state import list_auth_states
    rows = evaluate_agents({"gone": (None, None)})
    # Act
    persist_verdicts(rows)
    # Assert
    assert list_auth_states() == {}


def test_persist_stamps_checked_at_so_staleness_can_be_judged(pg_schema: str):
    # Arrange — a verdict with no timestamp could never be aged, and a cache that
    # cannot be aged gets presented as fresh truth forever.
    from scitex_agent_container._state.auth_state import list_auth_states
    rows = evaluate_agents({"scitex-hpc": (_STUCK, _STUCK)})
    persist_verdicts(rows)
    # Act
    checked_at = list_auth_states()["scitex-hpc"]["checked_at"]
    # Assert
    assert checked_at.endswith("Z")
