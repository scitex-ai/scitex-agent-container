"""tmux ``-t`` targets must match EXACTLY — never by prefix.

Live incident 2026-08-14 (card ``sac-tmux-prefix-match-false-alive-20260814``):
tmux resolves a bare ``-t <name>`` by PREFIX when no exact match exists, so
``tmux has-session -t tui-scitex-cards`` matched the SIBLING session
``tui-scitex-cards-gui``. ``TmuxManager.exists`` reported the cards agent
ALIVE off the GUI agent's pane, ``sac agents start scitex-cards`` exited 0
having launched NOTHING, and a stop/restart would have prefix-match KILLED
the innocent sibling.

The fix is :func:`scitex_agent_container._runners._tmux._target.exact_target`
— every ``-t`` that passes a session name goes through it. The form it emits
is ``=name:`` (exact-match ``=`` + trailing ``:`` marking a session target),
because the BARE ``=name`` form is NOT uniformly accepted: measured on tmux
3.4, target-pane subcommands (``capture-pane``, ``send-keys``) reject it with
``can't find pane: =name`` while ``has-session`` accepts it. The
per-subcommand acceptance tests below re-prove that ``=name:`` parses
everywhere, against a REAL tmux (same pattern as ``test_tmux_pane_probe``).

No mocks — pure-function cases, a recording-runner seam, and real ``tmux``.
"""

from __future__ import annotations

import shutil
import subprocess
import time
import uuid
from typing import Iterator

import pytest

from scitex_agent_container._runners._tmux._target import exact_target
from scitex_agent_container._runners._tmux.tmux import TmuxManager

requires_tmux = pytest.mark.skipif(
    shutil.which("tmux") is None, reason="tmux binary not on PATH"
)


# ---------------------------------------------------------------------------
# exact_target — the pure rendering rule
# ---------------------------------------------------------------------------


class TestExactTargetRendering:
    def test_bare_session_name_gains_equals_and_trailing_colon(self) -> None:
        # Arrange
        name = "tui-scitex-cards"
        # Act
        target = exact_target(name)
        # Assert — the ONE universal exact form (``=name`` alone is rejected
        # by capture-pane / send-keys; measured, tmux 3.4).
        assert target == "=tui-scitex-cards:"

    def test_already_exact_target_is_not_double_prefixed(self) -> None:
        # Arrange
        name = "=tui-x:"
        # Act
        target = exact_target(name)
        # Assert
        assert target == "=tui-x:"

    @pytest.mark.parametrize("target_id", ["%5", "@2", "$3"])
    def test_pane_window_session_ids_pass_through_untouched(
        self, target_id: str
    ) -> None:
        # Arrange — ids are exact by construction; wrapping would corrupt them.
        # Act
        target = exact_target(target_id)
        # Assert
        assert target == target_id

    def test_target_with_window_part_keeps_it_and_gains_only_equals(self) -> None:
        # Arrange — a caller that already picked a window must keep it.
        name = "tui-x:0"
        # Act
        target = exact_target(name)
        # Assert
        assert target == "=tui-x:0"

    def test_empty_target_passes_through(self) -> None:
        # Arrange — tmux reads an empty -t as "current session"; wrapping
        # would turn that deliberate default into a parse error.
        name = ""
        # Act
        target = exact_target(name)
        # Assert
        assert target == ""


# ---------------------------------------------------------------------------
# The seam-level proof: TmuxManager renders the exact form into its argv
# ---------------------------------------------------------------------------


class _RunnerRecorder:
    def __init__(self) -> None:
        self.argvs: list[list[str]] = []

    def __call__(self, argv: list[str], **_kwargs: object) -> None:
        self.argvs.append(list(argv))


def test_send_text_literal_targets_the_exact_session_form() -> None:
    # Arrange
    runner = _RunnerRecorder()
    # Act
    TmuxManager.send_text_literal("tui-x", "hello", runner=runner)
    # Assert — the argv carries "=tui-x:", so a prefix can never land the
    # text in a sibling's pane.
    assert runner.argvs[0][:4] == ["tmux", "send-keys", "-t", "=tui-x:"]


# ---------------------------------------------------------------------------
# Real-tmux regression: the incident shape, both halves
# ---------------------------------------------------------------------------


def _new_session(name: str, command: str = "sleep 600") -> None:
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", name, *command.split()],
        check=True,
        capture_output=True,
    )


def _kill_session(name: str) -> None:
    subprocess.run(
        ["tmux", "kill-session", "-t", f"={name}:"],
        check=False,
        capture_output=True,
    )


@pytest.fixture()
def sibling_only() -> Iterator[str]:
    """A running ``<base>-gui`` session with NO ``<base>`` session.

    Yields ``base`` — the name whose liveness must NOT be vouched for by
    the sibling. This is the incident's exact shape
    (``tui-scitex-cards`` vs ``tui-scitex-cards-gui``).
    """
    base = f"tui-exact-{uuid.uuid4().hex[:8]}"
    _new_session(f"{base}-gui")
    try:
        yield base
    finally:
        _kill_session(f"{base}-gui")


@requires_tmux
def test_exists_is_false_when_only_a_prefix_sibling_runs(sibling_only: str) -> None:
    # Arrange — fixture: only "<base>-gui" is running.
    base = sibling_only
    # Act
    alive = TmuxManager.exists(base)
    # Assert — the incident regression: a bare -t here prefix-matched the
    # sibling and reported the dead agent ALIVE, which silently no-op'd
    # `sac agents start`.
    assert alive is False


@requires_tmux
def test_exists_still_finds_the_exact_session(sibling_only: str) -> None:
    # Arrange — both the sibling AND the exact session running.
    base = sibling_only
    _new_session(base)
    try:
        # Act
        alive = TmuxManager.exists(base)
        # Assert — exact matching must not throw away true positives.
        assert alive is True
    finally:
        _kill_session(base)


@requires_tmux
def test_stop_never_prefix_kills_the_sibling(sibling_only: str) -> None:
    # Arrange — only "<base>-gui" runs; stopping "<base>" must be a no-op.
    base = sibling_only
    # Act — the destructive half of the incident: a bare kill-session here
    # would have torn down the innocent sibling.
    TmuxManager.stop(base)
    sibling_alive = TmuxManager.exists(f"{base}-gui")
    # Assert
    assert sibling_alive is True


@requires_tmux
def test_stop_kills_the_exact_session(sibling_only: str) -> None:
    # Arrange — both sessions running; stop(base) must take exactly one.
    base = sibling_only
    _new_session(base)
    try:
        # Act
        stopped = TmuxManager.stop(base)
        # Assert
        assert stopped is True and TmuxManager.exists(base) is False
    finally:
        _kill_session(base)


# ---------------------------------------------------------------------------
# Per-subcommand acceptance: the ``=name:`` form parses EVERYWHERE
# ---------------------------------------------------------------------------
#
# This is the guard the coordinator's field data demanded: on the live host,
# ``capture-pane -t =name`` FAILED ("can't find pane") while ``has-session -t
# =name`` passed — an exists() made exact at the price of a broken capture /
# send would be worse than the bug. Each case runs the REAL subcommand against
# a REAL session using exactly what ``exact_target`` emits.


@pytest.fixture()
def live_session() -> Iterator[str]:
    name = f"tui-exact-{uuid.uuid4().hex[:8]}"
    _new_session(name)
    try:
        yield name
    finally:
        _kill_session(name)


@requires_tmux
@pytest.mark.parametrize(
    "argv_for",
    [
        pytest.param(lambda t: ["has-session", "-t", t], id="has-session"),
        pytest.param(lambda t: ["capture-pane", "-p", "-t", t], id="capture-pane"),
        pytest.param(lambda t: ["send-keys", "-t", t, ""], id="send-keys"),
        pytest.param(
            lambda t: ["list-panes", "-t", t, "-F", "#{pane_pid}"], id="list-panes"
        ),
        pytest.param(
            lambda t: ["display-message", "-p", "-t", t, "#{pane_pid}"],
            id="display-message",
        ),
        pytest.param(
            lambda t: ["resize-window", "-t", t, "-x", "200", "-y", "50"],
            id="resize-window",
        ),
    ],
)
def test_exact_form_is_accepted_by_subcommand(live_session: str, argv_for) -> None:
    # Arrange
    target = exact_target(live_session)
    # Act
    result = subprocess.run(
        ["tmux", *argv_for(target)], capture_output=True, text=True
    )
    # Assert — rc 0 with no "can't find" refusal: the exact form parses.
    assert result.returncode == 0, result.stderr


@requires_tmux
def test_exact_form_is_accepted_by_rename_session(live_session: str) -> None:
    # Arrange
    renamed = f"{live_session}-renamed"
    # Act — rename away and back, both legs on the exact form.
    away = subprocess.run(
        ["tmux", "rename-session", "-t", exact_target(live_session), renamed],
        capture_output=True,
        text=True,
    )
    back = subprocess.run(
        ["tmux", "rename-session", "-t", exact_target(renamed), live_session],
        capture_output=True,
        text=True,
    )
    # Assert
    assert (away.returncode, back.returncode) == (0, 0), (
        away.stderr,
        back.stderr,
    )


@requires_tmux
def test_capture_content_reads_the_exact_sessions_pane(live_session: str) -> None:
    """End-to-end through TmuxManager: capture must return REAL content.

    ``capture_content`` returns ``""`` both for "no such session" and for a
    failed parse, so asserting non-failure needs an observable marker in the
    pane — proving the exact-form target reached the RIGHT pane, not merely
    that nothing crashed.
    """
    # Arrange — a second session whose pane prints a marker.
    name = f"tui-exact-{uuid.uuid4().hex[:8]}"
    marker = f"MARK-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", name, "bash", "-c", f"echo {marker}; sleep 600"],
        check=True,
        capture_output=True,
    )
    try:
        # Act — poll briefly: the pane needs a beat to render the echo.
        content = ""
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            content = TmuxManager.capture_content(name)
            if marker in content:
                break
            time.sleep(0.1)
        # Assert
        assert marker in content
    finally:
        _kill_session(name)
