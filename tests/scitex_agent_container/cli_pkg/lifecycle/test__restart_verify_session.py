"""Restart verification must ASK THE OS, and abstain when it cannot.

P0, 2026-08-14, scitex-compute-04. ``sac agents restart`` printed::

    verified: agent 'scitex-agent-container' is a NEW run
              (c06d2fec-… -> 67068008-…)

while the agent's tmux session (``tui-scitex-agent-container``) had been
alive and untouched since 11:22 the PREVIOUS DAY. The check compared two
readings of ``<runtime-dir>/<agent>/instance_id`` — a marker written by the
very start path it was checking. That is not a witness, it is an ECHO: the
start path agreed with itself and the word "verified" was printed over a
process nothing had touched. ``stop`` and ``restart`` reported success four
times running, and the maintenance path then refused the overlay-venv
repair with ``agent_not_running``.

So a ``True`` now needs TWO independent witnesses — the ledger's
identity-of-run AND the OS's ``#{session_created}``, the one tmux stamp
that is constant for a session's life and different for the next one. And
where the second witness cannot be taken (``instances.screen`` NULL, a row
from another host, a wedged tmux, a caller inside a container), the honest
answer is "CANNOT VERIFY" — ``None``, never ``True`` and never ``False``.
Inventing a failure there is the exact mirror of the false success.

NO MOCKS. A REAL tmux server on a private socket with REAL sessions, plus
a REAL on-disk state.db; the production functions' documented injection
seams (``session_name_fn`` / ``snapshot_fn`` / ``in_sif_fn``) are passed
REAL callables.
"""

from __future__ import annotations

import contextlib
import importlib
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from scitex_agent_container.cli_pkg.lifecycle._restart_verify import (
    SessionObservation,
    read_session_identity,
    recorded_session_name,
    verify_cycled,
)

# A tmux socket is keyed by (user, socket-NAME), never by process, so a
# LITERAL name is a HOST-GLOBAL namespace shared with the operator's real
# fleet AND with our own concurrent CI legs (runners -01/-02/-03 are three
# registrations of ONE Spartan node and do overlap). Unique per PROCESS so
# neither can collide with us. Orphan servers self-reap: every session below
# runs `sleep`, and tmux exits with its last session.
SOCKET = f"sac-verify-tests-{os.getpid()}-{uuid.uuid4().hex[:8]}"

pytestmark = pytest.mark.skipif(
    shutil.which("tmux") is None, reason="tmux is not installed on this host"
)


def _tmux(*args: str) -> subprocess.CompletedProcess:
    # 30s: three CI legs share one node, so a `tmux` spawn competes for a
    # loaded box's CPU. A deadline tight enough to blow under load is a race,
    # and blowing it here raises inside a FIXTURE — reported as a broken test
    # rather than as a busy host.
    return subprocess.run(
        ["tmux", "-L", SOCKET, *args], capture_output=True, text=True, timeout=30
    )


@pytest.fixture()
def tmux_server():
    """A real tmux server on a socket only this process can name."""
    # SOCKET is per-process, so tests in one process share a server: this kill
    # is what isolates each test from the last one's sessions.
    _tmux("kill-server")
    yield _tmux
    with contextlib.suppress(subprocess.TimeoutExpired):
        _tmux("kill-server")


def _snapshot_on_socket(*, socket_name: str | None = None) -> dict | None:
    """The REAL production probe, aimed at this process's private socket.

    Deliberately NOT a re-implementation: a hand-copied parse in the test
    drifts from the code it claims to cover. ``socket_name`` is accepted and
    ignored because ``observed_session_snapshot`` passes it positionally to
    whatever ``snapshot_fn`` it is given.
    """
    del socket_name
    from scitex_agent_container._runners._tmux._tmux_probe import list_sessions_created

    return list_sessions_created(socket_name=SOCKET)


def _identity_of(agent: str, session: str) -> SessionObservation:
    """Read ``session``'s live identity through the production reader."""
    return read_session_identity(
        agent,
        session_name_fn=lambda _n: session,
        snapshot_fn=_snapshot_on_socket,
    )


@pytest.fixture
def db_path(tmp_path: Path):
    """Isolated state.db location, exported via env (explicit save/restore)."""
    p = tmp_path / "state.db"
    key = "SCITEX_AGENT_CONTAINER_STATE_DB"
    saved = os.environ.get(key)
    os.environ[key] = str(p)
    import scitex_agent_container._state.state_db as mod

    importlib.reload(mod)
    try:
        yield p
    finally:
        if saved is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = saved
        importlib.reload(mod)


def _on_this_host() -> str:
    from scitex_agent_container._state.state_db import _resolve_host

    return _resolve_host(None)


def _seed_row(name: str, *, screen: str | None, host: str | None = None) -> None:
    """Insert a REAL ``instances`` row, exactly as a start would."""
    from scitex_agent_container._state.state_db import record_instance_start

    record_instance_start(name=name, host=host or _on_this_host(), screen=screen)


# ---------------------------------------------------------------------------
# recorded_session_name — which rows are ours to probe at all
# ---------------------------------------------------------------------------


def test_a_row_with_screen_null_names_no_session(db_path) -> None:
    # Arrange — every locally-started row looked like this before the fix.
    _seed_row("null-screen", screen=None)
    # Act
    session = recorded_session_name("null-screen", in_sif_fn=lambda: False)
    # Assert — no handle to ask the OS about.
    assert session is None


def test_a_row_that_names_a_session_hands_it_over(db_path) -> None:
    # Arrange — a row written by the fixed start path, on THIS host.
    _seed_row("named-screen", screen="tui-named-screen")
    # Act
    session = recorded_session_name("named-screen", in_sif_fn=lambda: False)
    # Assert
    assert session == "tui-named-screen"


def test_an_unknown_agent_names_no_session(db_path) -> None:
    # Arrange — no row at all (an agent this host has never started).
    # Act
    session = recorded_session_name("never-seen", in_sif_fn=lambda: False)
    # Assert
    assert session is None


def test_a_caller_inside_a_container_refuses_to_probe_the_name(db_path) -> None:
    # Arrange — the row is perfectly good and on this host, but the CALLER is
    # in a SIF, whose tmux is a different mount namespace. Probing the name
    # here answers about the CONTAINER's tmux server; an answer from the wrong
    # server is not a weaker answer, it is not an answer.
    _seed_row("in-sif", screen="tui-in-sif")
    # Act
    session = recorded_session_name("in-sif", in_sif_fn=lambda: True)
    # Assert
    assert session is None


def test_a_row_written_on_another_host_refuses_to_probe_the_name(db_path) -> None:
    # Arrange — that session lives in a tmux server on a machine we cannot
    # reach; the same name here would answer about THIS machine.
    _seed_row("elsewhere", screen="tui-elsewhere", host="some-other-host")
    # Act
    session = recorded_session_name("elsewhere", in_sif_fn=lambda: False)
    # Assert
    assert session is None


# ---------------------------------------------------------------------------
# read_session_identity — blindness is reported AS blindness
# ---------------------------------------------------------------------------


def test_screen_null_reports_that_we_could_not_look(db_path) -> None:
    # Arrange — the P0's registry state, read through the production path.
    _seed_row("blind-agent", screen=None)
    # Act
    seen = read_session_identity(
        "blind-agent",
        session_name_fn=lambda n: recorded_session_name(n, in_sif_fn=lambda: False),
        snapshot_fn=_snapshot_on_socket,
    )
    # Assert — "I could not look", never "the session is not there".
    assert seen.observed is False


def test_screen_null_names_the_column_in_its_reason(db_path) -> None:
    # Arrange
    _seed_row("blind-why", screen=None)
    # Act
    seen = read_session_identity(
        "blind-why",
        session_name_fn=lambda n: recorded_session_name(n, in_sif_fn=lambda: False),
        snapshot_fn=_snapshot_on_socket,
    )
    # Assert — the operator is told WHICH fact is missing.
    assert "screen" in seen.blind_because


def test_a_live_session_is_observed_with_its_birthday(tmux_server, db_path) -> None:
    # Arrange — a REAL row naming a REAL tmux session.
    tmux_server("new-session", "-d", "-s", "tui-live-1", "sleep", "60")
    _seed_row("live-1", screen="tui-live-1")
    # Act — the full chain: state.db row -> name -> real tmux probe.
    seen = read_session_identity(
        "live-1",
        session_name_fn=lambda n: recorded_session_name(n, in_sif_fn=lambda: False),
        snapshot_fn=_snapshot_on_socket,
    )
    # Assert — a plausible unix epoch rides with the name, not a placeholder.
    assert int(seen.identity.rsplit("@", 1)[1]) > 1_600_000_000


def test_a_session_absent_from_a_good_probe_is_confirmed_absent(tmux_server) -> None:
    # Arrange — a live server holding OTHER sessions. The probe SUCCEEDED, so
    # this absence is real (unlike an empty/failed probe, which is unknown).
    tmux_server("new-session", "-d", "-s", "tui-somebody-else", "sleep", "60")
    # Act
    seen = _identity_of("gone", "tui-gone")
    # Assert — we DID look (observed), and there is nothing there (no identity).
    assert (seen.observed, seen.identity) == (True, None)


def test_a_wedged_probe_is_blindness_not_an_absent_session() -> None:
    # Arrange — a probe that cannot answer. Reading this as "the session is
    # gone" would turn a loaded host into a reported restart failure.
    def _wedged(*, socket_name: str | None = None):
        del socket_name
        return None

    # Act
    seen = read_session_identity(
        "wedged", session_name_fn=lambda _n: "tui-wedged", snapshot_fn=_wedged
    )
    # Assert
    assert seen.observed is False


# ---------------------------------------------------------------------------
# verify_cycled — the ledger alone can never reach True
# ---------------------------------------------------------------------------


def test_a_new_run_id_with_no_process_evidence_cannot_be_verified() -> None:
    # Arrange — THE regression guard, and the exact P0 input: the ledger says
    # NEW RUN and nothing looked at the OS. Pre-fix this printed "verified".
    # Act
    verdict = verify_cycled("echo-only", "run-1", "run-2")
    # Assert — abstention, not a pass.
    assert verdict.verified is None


def test_an_unverifiable_new_run_says_cannot_verify(tmux_server) -> None:
    # Arrange
    blind = SessionObservation()
    # Act
    verdict = verify_cycled(
        "echo-only", "run-1", "run-2", session_before=blind, session_after=blind
    )
    # Assert — the operator is told to confirm by hand, not told it passed.
    assert "CANNOT VERIFY" in verdict.reason


def test_a_new_run_id_over_an_untouched_session_is_a_failure(
    tmux_server, db_path
) -> None:
    # Arrange — THE P0, reproduced against real tmux: the ledger mints a new
    # run id while the session is never touched. Both readings are taken from
    # the SAME live session, exactly as a no-op restart would produce.
    tmux_server("new-session", "-d", "-s", "tui-untouched", "sleep", "60")
    _seed_row("untouched", screen="tui-untouched")
    before = _identity_of("untouched", "tui-untouched")
    after = _identity_of("untouched", "tui-untouched")
    # Act
    verdict = verify_cycled(
        "untouched", "run-1", "run-2", session_before=before, session_after=after
    )
    # Assert — the ledger is overruled by the OS.
    assert verdict.verified is False


def test_a_new_run_id_over_an_untouched_session_hands_over_the_kill_command(
    tmux_server,
) -> None:
    # Arrange — the only sequence that actually recovered the P0 was a
    # hand-run `tmux kill-session`, so the verdict must name it.
    tmux_server("new-session", "-d", "-s", "tui-untouched-2", "sleep", "60")
    seen = _identity_of("untouched-2", "tui-untouched-2")
    # Act
    verdict = verify_cycled(
        "untouched-2", "run-1", "run-2", session_before=seen, session_after=seen
    )
    # Assert
    assert "tmux kill-session -t tui-untouched-2" in verdict.reason


def test_a_genuinely_cycled_session_verifies_true(tmux_server) -> None:
    # Arrange — a REAL cycle: the session is killed and a new one created
    # under the same name, which is what a working restart does.
    tmux_server("new-session", "-d", "-s", "tui-cycled", "sleep", "60")
    before = _identity_of("cycled", "tui-cycled")
    # tmux reports #{session_created} in WHOLE SECONDS, so two sessions born
    # inside one second share a birthday. Crossing the boundary is what makes
    # the two readings distinguishable here; in production the gap is the
    # whole stop+start leg. (A real agent restarted within one second of
    # being started is the only shape this cannot separate.)
    time.sleep(1.1)
    tmux_server("kill-session", "-t", "tui-cycled")
    tmux_server("new-session", "-d", "-s", "tui-cycled", "sleep", "60")
    after = _identity_of("cycled", "tui-cycled")
    # Act
    verdict = verify_cycled(
        "cycled", "run-1", "run-2", session_before=before, session_after=after
    )
    # Assert — both witnesses agree, so this is the one path to a pass.
    assert verdict.verified is True


def test_a_new_run_id_with_no_session_at_all_is_a_failure(tmux_server) -> None:
    # Arrange — the ledger says it came back up; the OS says it did not. The
    # server is live and holds another session, so the absence is CONFIRMED.
    tmux_server("new-session", "-d", "-s", "tui-someone", "sleep", "60")
    before = _identity_of("down", "tui-down")
    after = _identity_of("down", "tui-down")
    # Act
    verdict = verify_cycled(
        "down", "run-1", "run-2", session_before=before, session_after=after
    )
    # Assert
    assert verdict.verified is False


def test_an_unchanged_run_id_stays_a_failure_without_any_session_evidence() -> None:
    # Arrange — the ledger's own definitive NO must not be weakened into an
    # abstention just because the OS could not be consulted.
    # Act
    verdict = verify_cycled("stuck", "run-1", "run-1")
    # Assert
    assert verdict.verified is False


def test_no_marker_either_side_stays_an_abstention() -> None:
    # Arrange — no evidence at all, from either witness.
    # Act
    verdict = verify_cycled("ghost", None, None)
    # Assert
    assert verdict.verified is None


# ---------------------------------------------------------------------------
# The evidence travels with the verdict, so --json can be re-checked
# ---------------------------------------------------------------------------


def test_the_verdict_carries_both_session_readings(tmux_server) -> None:
    # Arrange — a --json consumer must be able to re-check the OS-side
    # reasoning without re-running anything.
    tmux_server("new-session", "-d", "-s", "tui-evidence", "sleep", "60")
    seen = _identity_of("evidence", "tui-evidence")
    # Act
    payload = verify_cycled(
        "evidence", "run-1", "run-2", session_before=seen, session_after=seen
    ).as_dict()
    # Assert
    assert payload["session_before"] == payload["session_after"] == seen.identity


def test_the_default_observation_is_the_blind_one() -> None:
    # Arrange — a caller that forgets to pass an observation must get an
    # abstention, never a silently-unchecked pass. That default is the reason
    # the ledger-only case above cannot reach True.
    # Act
    seen = SessionObservation()
    # Assert
    assert seen.observed is False
