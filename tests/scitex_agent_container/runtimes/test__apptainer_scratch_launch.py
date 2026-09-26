"""``/uvwork``: assembling the argv is not launching — the ADR-0024 split.

``build_run_argv`` is reached by ``sac agents explain`` and by ``sac agents
start --dry-run``, neither of which starts anything. So the ADR-0024 work
divides in two, and these rows pin BOTH halves on BOTH kinds of host, because
either half alone passes just as well while the other behaviour is exactly
inverted:

* **Emitting the bind writes nothing and refuses nothing.** No directory is
  created on the host, and a host with no resolvable scratch root gets a
  ``WARNING`` plus an argv without the bind — never an exception. A refusal
  here made two READ-ONLY commands fail on a host condition, which is how
  ``verify_tmpfs_headroom`` came to carry its own warning: a full disk made
  ``explain`` unusable on exactly the host it would have diagnosed.
* **The real launch creates the source and refuses.** ``ensure_uvwork_for_
  launch`` — called from ``_apptainer_runtime.start`` and ``tui_session.start``
  past their ``dry_run`` return — makes the bind source exist 0700 and raises
  ``ScratchRootError`` naming the missing path. That refusal is the point of
  ADR-0024 and these rows exist so it cannot be softened by accident.
* **`explain` says which of those it is.** The plan renders binds by walking
  the argv, so the one outcome it could not otherwise show is the one that
  emits no bind — precisely the case where a start WILL refuse.

The bind's shape, the per-agent layout and the spec-wins rule live in
``test__apptainer_scratch.py`` beside this file; the shared spec is
``_helpers/scratch_agent.py`` so the two suites cannot drift apart.

No mocks (PA-306): real spec files, the real loader, real config.yaml files,
real directories, the real argv builder. STX-TQ002 AAA markers; one fact per
test (PA-307).
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from scitex_agent_container._state.host_scratch import ScratchRoot, ScratchRootError
from scitex_agent_container.runtimes._apptainer_build_argv import build_run_argv
from scitex_agent_container.runtimes._apptainer_scratch import (
    UVWORK_CONTAINER_PATH,
    UVWORK_DIR_MODE,
    ensure_uvwork_for_launch,
    uvwork_bind_flags,
)
from tests.scitex_agent_container._helpers.scratch_agent import (
    load_uvwork_agent,
    uvwork_binds,
)


@pytest.fixture
def agent_config(tmp_path: Path):
    """A real, loadable spec named ``agt``."""
    return load_uvwork_agent(tmp_path)


@pytest.fixture
def scratch(tmp_path: Path) -> ScratchRoot:
    """A resolved root standing in for this host's ``/scratch``."""
    root = tmp_path / "scratch"
    root.mkdir()
    return ScratchRoot(root=root, source="config", reason="test config declares it")


@pytest.fixture
def no_scratch() -> ScratchRoot:
    """The written decision: this host keeps ``/uvwork`` in the overlay."""
    return ScratchRoot(root=None, source="none", reason="root LV is 8T")


@pytest.fixture
def host_declares_scratch(tmp_path: Path, env_save_restore) -> Path:
    """A real config.yaml declaring a real scratch root for THIS host."""
    root = tmp_path / "host-scratch"
    root.mkdir()
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"scratch_root: {root}\n", encoding="utf-8")
    env_save_restore.set("SCITEX_AGENT_CONTAINER_CONFIG", str(cfg))
    return root


@pytest.fixture
def host_declares_a_missing_root(tmp_path: Path, env_save_restore) -> Path:
    """A real config.yaml naming a scratch root that is not on this host."""
    missing = tmp_path / "not-mounted"
    cfg = tmp_path / "config-missing.yaml"
    cfg.write_text(f"scratch_root: {missing}\n", encoding="utf-8")
    env_save_restore.set("SCITEX_AGENT_CONTAINER_CONFIG", str(cfg))
    return missing


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


# ---------------------------------------------------------------------------
# Half one — assembling the argv writes nothing
# ---------------------------------------------------------------------------


def test_emitting_the_flags_creates_NOTHING(
    agent_config, scratch: ScratchRoot
) -> None:
    # Arrange — this call is on `build_run_argv`, which `sac agents explain`
    # and `--dry-run` also reach; a read-only command must not write.
    target = scratch.root / "sac" / "agents" / "agt" / "uvwork"
    # Act
    uvwork_bind_flags(agent_config, [], scratch=scratch)
    # Assert
    assert not target.exists()


def test_build_run_argv_creates_nothing_under_the_scratch_root(
    agent_config, tmp_path: Path, host_declares_scratch: Path
) -> None:
    # Arrange — the same property through the REAL builder, on a real host
    # config, because that is the surface `explain` actually calls.
    # Act
    build_run_argv(
        agent_config,
        state_dir=tmp_path / "state",
        sif_path=Path("/img/sac.sif"),
        tui=True,
    )
    # Assert
    assert list(host_declares_scratch.rglob("uvwork")) == []


# ---------------------------------------------------------------------------
# Half one, continued — a host with NO scratch root states it, never raises
# ---------------------------------------------------------------------------


def test_build_run_argv_does_not_raise_when_no_scratch_root_resolves(
    agent_config, tmp_path: Path, host_declares_a_missing_root: Path
) -> None:
    # Arrange — a read-only command must not fail on a launch-time host
    # condition. A raise here is the regression this row exists to catch.
    # Act
    argv = build_run_argv(
        agent_config,
        state_dir=tmp_path / "state",
        sif_path=Path("/img/sac.sif"),
        tui=True,
    )
    # Assert
    assert uvwork_binds(argv) == []


# The instrument here is the process's own STDERR, read back at the file
# descriptor (``capfd``) — not ``caplog`` and not a handler this test attaches
# to the logger by name. Measured 2026-09-03, twice: both of those reported an
# EMPTY record list in the full suite while pytest's own "Captured stderr" and
# "Captured log call" sections printed the very WARNING they claimed was
# absent. An instrument that says "nothing was emitted" about a message it is
# simultaneously displaying is the one thing a visibility test must not use.
# Stderr is also the property being claimed: the diagnostic must REACH A HUMAN
# running `sac agents explain` on a host with no scratch root, which is a
# statement about the terminal, not about a logging record.


def test_an_unresolvable_root_reaches_stderr_not_a_silent_absence(
    agent_config, host_declares_a_missing_root: Path, capfd
) -> None:
    # Arrange — logged at WARNING so it lands on stderr through the handler
    # sac installs, and through Python's last-resort handler without one.
    uvwork_bind_flags(agent_config, [])
    # Act
    err = capfd.readouterr().err
    # Assert
    assert "/uvwork" in err


def test_the_stderr_diagnostic_names_the_agent_whose_start_would_refuse(
    agent_config, host_declares_a_missing_root: Path, capfd
) -> None:
    # Arrange — a diagnostic that does not say WHICH agent is no diagnostic
    # on a host running ninety of them.
    uvwork_bind_flags(agent_config, [])
    # Act
    err = capfd.readouterr().err
    # Assert
    assert agent_config.name in err


def test_the_stderr_diagnostic_names_the_path_that_is_missing(
    agent_config, host_declares_a_missing_root: Path, capfd
) -> None:
    # Arrange — the operator's next action is a mount, and the message has
    # to say of what.
    uvwork_bind_flags(agent_config, [])
    # Act
    err = capfd.readouterr().err
    # Assert
    assert str(host_declares_a_missing_root) in err


def test_a_resolvable_root_raises_no_stderr_alarm(
    agent_config, host_declares_scratch: Path, capfd
) -> None:
    # Arrange — the positive control: the same call on a healthy host must
    # NOT print a refusal, or the alarm means nothing when it does fire.
    uvwork_bind_flags(agent_config, [])
    # Act
    err = capfd.readouterr().err
    # Assert
    assert "REFUSE" not in err


def test_the_explain_line_names_the_refusal_a_start_would_hit(
    agent_config, host_declares_a_missing_root: Path
) -> None:
    # Arrange — `explain` renders binds by walking the argv, so the one
    # outcome it cannot show is the one that emits no bind. It says so.
    from scitex_agent_container.cli_pkg._explain import _uvwork_line

    # Act
    line = _uvwork_line(agent_config)
    # Assert
    assert "REFUSE" in line


def test_the_explain_line_names_the_source_when_the_root_resolves(
    agent_config, host_declares_scratch: Path
) -> None:
    # Arrange — the positive control for the row above: same call, a host
    # that CAN resolve, and the line names where /uvwork comes from.
    from scitex_agent_container.cli_pkg._explain import _uvwork_line

    # Act
    line = _uvwork_line(agent_config)
    # Assert
    assert str(host_declares_scratch) in line


def test_the_explain_line_cries_REFUSE_only_when_it_would(
    agent_config, host_declares_scratch: Path
) -> None:
    # Arrange — the alarm must not fire on a healthy host, or it says
    # nothing on an unhealthy one.
    from scitex_agent_container.cli_pkg._explain import _uvwork_line

    # Act
    line = _uvwork_line(agent_config)
    # Assert
    assert "REFUSE" not in line


# ---------------------------------------------------------------------------
# Half two — the real launch creates the source, and refuses
# ---------------------------------------------------------------------------


def test_the_launch_hook_creates_the_source(
    agent_config, scratch: ScratchRoot
) -> None:
    # Arrange — a bind whose source does not exist is a FATAL at exec, so
    # the real launch path makes it exist.
    argv = uvwork_bind_flags(agent_config, [], scratch=scratch)
    target = scratch.root / "sac" / "agents" / "agt" / "uvwork"
    # Act
    ensure_uvwork_for_launch(agent_config, argv, scratch=scratch)
    # Assert
    assert target.is_dir()


def test_the_launch_hook_creates_the_source_owner_only(
    agent_config, scratch: ScratchRoot
) -> None:
    # Arrange
    argv = uvwork_bind_flags(agent_config, [], scratch=scratch)
    # Act
    target = ensure_uvwork_for_launch(agent_config, argv, scratch=scratch)
    # Assert
    assert _mode(target) == UVWORK_DIR_MODE


def test_the_launch_hook_leaves_a_spec_declared_bind_to_its_owner(
    agent_config, scratch: ScratchRoot, tmp_path: Path
) -> None:
    # Arrange — the spec bound /uvwork itself and won, so sac emitted
    # nothing; creating its source is not sac's to do.
    argv = ["--bind", f"{tmp_path}/theirs:{UVWORK_CONTAINER_PATH}:rw"]
    # Act
    created = ensure_uvwork_for_launch(agent_config, argv, scratch=scratch)
    # Assert
    assert created is None


def test_the_launch_hook_creates_nothing_on_a_written_none_decision(
    agent_config, no_scratch: ScratchRoot
) -> None:
    # Arrange — the host keeps /uvwork in the overlay, in writing.
    # Act
    created = ensure_uvwork_for_launch(agent_config, [], scratch=no_scratch)
    # Assert
    assert created is None


def test_the_launch_hook_refuses_when_no_scratch_root_resolves(
    agent_config, host_declares_a_missing_root: Path
) -> None:
    # Arrange — undiminished: the start still refuses.
    argv: list[str] = []
    # Act
    raised = pytest.raises(ScratchRootError)
    # Assert
    with raised:
        ensure_uvwork_for_launch(agent_config, argv)


def test_the_launch_refusal_names_the_missing_path(
    agent_config, host_declares_a_missing_root: Path
) -> None:
    # Arrange — an operator's next action must be a mount, not a guess.
    missing = str(host_declares_a_missing_root)
    # Act
    try:
        ensure_uvwork_for_launch(agent_config, [])
        message = "<no refusal was raised>"
    except ScratchRootError as exc:
        message = str(exc)
    # Assert
    assert missing in message
