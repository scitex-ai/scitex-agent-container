"""The login-required pass: the RIGHT command, on the RIGHT agents, all logged.

NO MOCKS. ``sac`` is stood in for by a REAL executable shell script that records
the argv it was invoked with — so the assertion "we ran ``sac agents restart -y
<name>``" is a fact about a process that actually ran, not about a call recorded
against a stub. The history file, the log file and the fleet-registry directory
are all real files in ``tmp_path``.

That matters here more than usual. The whole reason this pass shells out instead
of calling ``agent_restart`` is that the operator has verified THAT invocation by
hand; a test that accepted a stub's word for the argv would be checking the one
thing it was built not to have to trust.
"""

from __future__ import annotations

import json
import os

import pytest

from scitex_agent_container._authheal._restart_pass import restart_login_required_pass
from scitex_agent_container._reconcile._budget import DEBOUNCE_S
from scitex_agent_container._reconcile._rule import Verdict

_CHROME = """\
────────────────────────────────────────────────
❯
────────────────────────────────────────────────
  Opus 4.8 | ctx:56% | 5h:49%
  ⏵⏵ bypass permissions on (shift+tab to cycle)
"""

# Wedged AND animating — the case the freeze-based detectors miss.
_WEDGED = (
    "⏺ Login expired · Please run /login\n  Retrying request in 47 seconds\n" + _CHROME
)

# Healthy, quoting the banner high in scrollback.
_QUOTING = (
    """\
⏺ I looked into it. The agent died with:

  Login expired · Please run /login

  That string is a 401, not an expiry. A sibling agent's OAuth refresh
  consumed the single-use refresh_token and revoked the token this one
  still held in memory, so nothing actually expired.

  The cure is a restart, not a /login — Claude never re-reads its
  credentials once it has started.

  I have carded it as sac-auth-401-incident, pinged the owner, and
  attached the pane capture to the incident notes.
"""
    + _CHROME
)

_NOW = 1_800_000_000.0


@pytest.fixture
def install_sac(tmp_path):
    """Install a REAL executable standing in for ``sac``; return its argv log.

    The script is genuinely executed by the production subprocess path, so what
    the assertions read back is the argv a real process really received.
    ``$SAC_BIN`` is set and restored here rather than via ``monkeypatch``, which
    is banned ecosystem-wide (STX-NM002) for encoding the author's assumptions
    about a collaborator instead of talking to the real one.
    """
    saved = os.environ.get("SAC_BIN")

    def install(*, exit_code: int = 0):
        record = tmp_path / "invocations.txt"
        script = tmp_path / "sac"
        script.write_text(
            "#!/bin/sh\n"
            f'printf "%s\\n" "$*" >> "{record}"\n'
            'echo "agent $4 restarted"\n'
            'echo "warning: overlay was stale" >&2\n'
            f"exit {exit_code}\n"
        )
        script.chmod(0o755)
        os.environ["SAC_BIN"] = str(script)
        return record

    yield install

    if saved is None:
        os.environ.pop("SAC_BIN", None)
    else:
        os.environ["SAC_BIN"] = saved


@pytest.fixture
def sac_bin(install_sac):
    return install_sac()


@pytest.fixture
def failing_sac_bin(install_sac):
    return install_sac(exit_code=3)


@pytest.fixture
def registry(tmp_path):
    """An empty-but-readable fleet registry, so the roster adds no extra names."""
    root = tmp_path / "agents"
    # exist_ok: a repo conftest already redirects the fleet agents dir into
    # tmp_path, so this directory can legitimately be there before we ask.
    root.mkdir(exist_ok=True)
    return root


@pytest.fixture
def log_file(tmp_path):
    return tmp_path / "login-required.log"


@pytest.fixture
def history_file(tmp_path):
    return tmp_path / "history.json"


def _run(*, registry, log_file, history_file, panes, apply, now=_NOW):
    return restart_login_required_pass(
        apply=apply,
        specs_dir=registry,
        history_file=history_file,
        log_file=log_file,
        alarm=False,  # never touch the real board from a test
        now=now,
        capture_fn=lambda: dict(panes),
    )


@pytest.fixture
def applied(registry, log_file, history_file, sac_bin):
    """One APPLY pass over a wedged agent, a quoting agent and an unreadable one."""
    outcome = _run(
        registry=registry,
        log_file=log_file,
        history_file=history_file,
        panes={"wedged": _WEDGED, "quoting": _QUOTING, "unreadable": None},
        apply=True,
    )
    return {
        "outcome": outcome,
        "log": log_file.read_text(),
        "invocations": sac_bin.read_text() if sac_bin.exists() else "",
        "verdicts": {r.name: r.verdict for r in outcome.reports},
    }


# ---------------------------------------------------------------------------
# (2) THE COMMAND — the operator's own, exactly.
# ---------------------------------------------------------------------------


def test_apply_runs_the_operator_verified_restart_command(applied):
    """`sac agents restart -y <name>` — the invocation he has verified by hand."""
    # Arrange
    invoked = applied["invocations"]
    # Act
    lines = invoked.split()
    # Assert
    assert lines == ["agents", "restart", "-y", "wedged"]


def test_apply_restarts_only_the_wedged_agent(applied):
    """The quoting agent keeps its context; the unreadable one is left alone."""
    # Arrange
    invoked = applied["invocations"]
    # Act
    names = [line.split()[-1] for line in invoked.splitlines() if line.strip()]
    # Assert
    assert names == ["wedged"]


def test_wedged_agent_is_reported_restarted(applied):
    # Arrange
    verdicts = applied["verdicts"]
    # Act
    verdict = verdicts["wedged"]
    # Assert
    assert verdict is Verdict.RESTARTED


def test_quoting_agent_produces_no_report_at_all(applied):
    """A healthy agent is not an event; it is logged, not reported."""
    # Arrange
    verdicts = applied["verdicts"]
    # Act
    reported = "quoting" in verdicts
    # Assert
    assert reported is False


# ---------------------------------------------------------------------------
# (3) THE LOG — every step, every byte.
# ---------------------------------------------------------------------------


def test_log_records_the_wedged_agents_verdict(applied):
    # Arrange
    log = applied["log"]
    # Act
    present = "agent=wedged verdict=LOGIN_REQUIRED" in log
    # Assert
    assert present is True


def test_log_records_WHY_the_wedged_agent_was_flagged(applied):
    """near-prompt / scrollback-only / pane-unreadable — the missing field."""
    # Arrange
    log = applied["log"]
    # Act
    present = "why=near-prompt" in log
    # Assert
    assert present is True


def test_log_records_why_the_healthy_agent_was_NOT_flagged(applied):
    """The question the deployed script left unanswerable."""
    # Arrange
    log = applied["log"]
    # Act
    present = "agent=quoting verdict=OK why=scrollback-only" in log
    # Assert
    assert present is True


def test_log_records_the_unreadable_pane_as_unknown(applied):
    """Tri-state: never healthy, never wedged."""
    # Arrange
    log = applied["log"]
    # Act
    present = "agent=unreadable verdict=UNKNOWN why=pane-unreadable" in log
    # Assert
    assert present is True


def test_log_keeps_the_raw_pane_capture(applied):
    """Keep the evidence, not just the conclusion drawn from it."""
    # Arrange
    log = applied["log"]
    # Act
    present = "Retrying request in 47 seconds" in log
    # Assert
    assert present is True


def test_log_records_the_exact_argv_executed(applied):
    # Arrange
    log = applied["log"]
    # Act
    present = "'agents', 'restart', '-y', 'wedged'" in log
    # Assert
    assert present is True


def test_log_records_the_restart_exit_code(applied):
    # Arrange
    log = applied["log"]
    # Act
    present = "rc=0" in log
    # Assert
    assert present is True


def test_log_keeps_the_restarts_full_stdout(applied):
    """The deployed script captured this and threw it away."""
    # Arrange
    log = applied["log"]
    # Act
    present = "agent wedged restarted" in log
    # Assert
    assert present is True


def test_log_keeps_the_restarts_full_stderr(applied):
    # Arrange
    log = applied["log"]
    # Act
    present = "warning: overlay was stale" in log
    # Assert
    assert present is True


def test_log_opens_with_the_discriminator_it_used(applied):
    """So a log read months later says which rule produced these verdicts."""
    # Arrange
    log = applied["log"]
    # Act
    present = "discriminator=near-prompt" in log
    # Assert
    assert present is True


def test_log_closes_with_a_pass_summary(applied):
    # Arrange
    log = applied["log"]
    # Act
    present = "PASS-END examined=3" in log
    # Assert
    assert present is True


# ---------------------------------------------------------------------------
# (1) DRY-RUN IS THE DEFAULT.
# ---------------------------------------------------------------------------


def test_check_mode_reports_the_wedged_agent(registry, log_file, history_file, sac_bin):
    # Arrange
    panes = {"wedged": _WEDGED, "quoting": _QUOTING}
    # Act
    outcome = _run(
        registry=registry,
        log_file=log_file,
        history_file=history_file,
        panes=panes,
        apply=False,
    )
    # Assert
    assert [r.verdict for r in outcome.reports] == [Verdict.WOULD_RESTART]


def test_check_mode_restarts_nothing(registry, log_file, history_file, sac_bin):
    # Arrange
    panes = {"wedged": _WEDGED, "quoting": _QUOTING}
    # Act
    _run(
        registry=registry,
        log_file=log_file,
        history_file=history_file,
        panes=panes,
        apply=False,
    )
    # Assert
    assert sac_bin.exists() is False


def test_check_mode_still_writes_the_full_log(
    registry, log_file, history_file, sac_bin
):
    """A dry run is where you look BEFORE acting, so it must be readable."""
    # Arrange
    panes = {"wedged": _WEDGED}
    # Act
    _run(
        registry=registry,
        log_file=log_file,
        history_file=history_file,
        panes=panes,
        apply=False,
    )
    # Assert
    assert "why=near-prompt" in log_file.read_text()


# ---------------------------------------------------------------------------
# A RESTART THAT FAILS must stay loud, and keep its output.
# ---------------------------------------------------------------------------


@pytest.fixture
def failed_apply(registry, log_file, history_file, failing_sac_bin):
    outcome = _run(
        registry=registry,
        log_file=log_file,
        history_file=history_file,
        panes={"wedged": _WEDGED},
        apply=True,
    )
    return {"outcome": outcome, "log": log_file.read_text()}


def test_nonzero_restart_is_reported_failed(failed_apply):
    # Arrange
    outcome = failed_apply["outcome"]
    # Act
    verdicts = [r.verdict for r in outcome.reports]
    # Assert
    assert verdicts == [Verdict.FAILED]


def test_nonzero_restart_records_its_exit_code(failed_apply):
    # Arrange
    log = failed_apply["log"]
    # Act
    present = "rc=3" in log
    # Assert
    assert present is True


def test_nonzero_restart_still_keeps_its_stderr(failed_apply):
    """The failing case is the one whose output is worth most."""
    # Arrange
    log = failed_apply["log"]
    # Act
    present = "warning: overlay was stale" in log
    # Assert
    assert present is True


def test_failed_restart_points_the_reader_at_the_log(failed_apply):
    # Arrange
    outcome = failed_apply["outcome"]
    # Act
    detail = outcome.reports[0].detail
    # Assert
    assert "login-required.log" in detail


# ---------------------------------------------------------------------------
# AN UNWRITABLE LOG MUST BLOCK THE RESTART.
# ---------------------------------------------------------------------------


@pytest.fixture
def unloggable(tmp_path, registry, history_file, sac_bin):
    """A log path that CANNOT be created: its parent is a regular file."""
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("i am a file")
    outcome = _run(
        registry=registry,
        log_file=blocker / "login-required.log",
        history_file=history_file,
        panes={"wedged": _WEDGED},
        apply=True,
    )
    return {"outcome": outcome, "sac_bin": sac_bin}


def test_unwritable_log_blocks_the_restart(unloggable):
    """An unauditable restart is the failure this pass exists to end."""
    # Arrange
    sac_bin = unloggable["sac_bin"]
    # Act
    was_invoked = sac_bin.exists()
    # Assert
    assert was_invoked is False


def test_unwritable_log_is_reported_not_swallowed(unloggable):
    # Arrange
    outcome = unloggable["outcome"]
    # Act
    reasons = [r.reason for r in outcome.reports]
    # Assert
    assert reasons == ["log-unwritable"]


def test_unwritable_log_cannot_be_reported_as_a_clean_pass(unloggable):
    # Arrange
    outcome = unloggable["outcome"]
    # Act
    code = outcome.exit_code()
    # Assert
    assert code == 2


# ---------------------------------------------------------------------------
# RATE LIMITS — reused wholesale from _reconcile._budget.
# ---------------------------------------------------------------------------


@pytest.fixture
def debounced(registry, log_file, history_file, sac_bin):
    """A wedged agent restarted 60s ago: inside the 30-minute debounce."""
    history_file.write_text(json.dumps({"wedged": [_NOW - 60.0]}))
    outcome = _run(
        registry=registry,
        log_file=log_file,
        history_file=history_file,
        panes={"wedged": _WEDGED},
        apply=True,
    )
    return {"outcome": outcome, "sac_bin": sac_bin}


def test_debounced_agent_is_not_restarted_again(debounced):
    # Arrange
    sac_bin = debounced["sac_bin"]
    # Act
    was_invoked = sac_bin.exists()
    # Assert
    assert was_invoked is False


def test_debounced_agent_is_reported_cooling_down(debounced):
    # Arrange
    outcome = debounced["outcome"]
    # Act
    verdicts = [r.verdict for r in outcome.reports]
    # Assert
    assert verdicts == [Verdict.COOLING_DOWN]


def test_restart_is_recorded_in_the_history_so_the_debounce_can_bite(applied, tmp_path):
    # Arrange
    history = json.loads((tmp_path / "history.json").read_text())
    # Act
    stamps = history.get("wedged", [])
    # Assert
    assert stamps == [_NOW]


def test_an_agent_restarted_long_ago_is_restartable_again(
    registry, log_file, history_file, sac_bin
):
    """Guard the debounce fixture: it must bite on RECENCY, not on presence."""
    # Arrange
    history_file.write_text(json.dumps({"wedged": [_NOW - DEBOUNCE_S - 60.0]}))
    # Act
    outcome = _run(
        registry=registry,
        log_file=log_file,
        history_file=history_file,
        panes={"wedged": _WEDGED},
        apply=True,
    )
    # Assert
    assert [r.verdict for r in outcome.reports] == [Verdict.RESTARTED]


# ---------------------------------------------------------------------------
# THE ROSTER — an agent with no session is UNOBSERVED, not healthy.
# ---------------------------------------------------------------------------


def test_registered_agent_with_no_live_session_is_unobserved(
    tmp_path, registry, log_file, history_file, sac_bin
):
    # Arrange
    spec_dir = registry / "ghost"
    spec_dir.mkdir()
    (spec_dir / "spec.yaml").write_text("name: ghost\n")
    # Act
    outcome = _run(
        registry=registry,
        log_file=log_file,
        history_file=history_file,
        panes={"wedged": _WEDGED},
        apply=False,
    )
    # Assert
    assert Verdict.UNOBSERVED in [r.verdict for r in outcome.reports]
