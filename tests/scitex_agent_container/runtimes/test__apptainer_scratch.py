"""``/uvwork`` is bound from the host scratch volume — the launch half.

ADR-0024. Nothing bound ``/uvwork``, so uv, the uv cache, ``TMPDIR`` and the
agent venv all landed in ``overlays/<agent>/upper/uvwork`` on the host's ROOT
volume: 11.7 GB for sac alone, measured on scitex-compute-04 on 2026-09-03,
on the volume that filled to 0 four times the day before.

What these pin, and why each is a property rather than an implementation
detail:

* the bind exists AT ALL in the argv a real ``build_run_argv`` produces —
  with the RED CONTROL right beside it: the same spec on a host that decided
  ``scratch_root: none`` emits no ``/uvwork`` bind, so a green here cannot
  come from some other layer happening to mention the path;
* the source is ``<root>/sac/agents/<agent>/uvwork``, one directory per
  agent, so two agents can never share a venv;
* that directory is created 0700 — an agent's venv, cache and TMPDIR are its
  private working set — and an EXISTING one is left alone, mode included, so
  restarts never overrule an operator who tightened or loosened it; and
* a spec that binds ``/uvwork`` itself WINS, because apptainer keeps the
  first bind to a destination and this default must behave like every other
  fleet default.

Emitting the bind is READ-ONLY — one row here pins that no directory appears
under the scratch root — and the rest of that split (the launch hook that
creates the source and REFUSES on a host with nowhere to put it, and what a
host with no scratch root at all does to each surface) lives in
``test__apptainer_scratch_launch.py`` beside this file.

No mocks (PA-306): real spec files, the real loader, real directories, the
real argv builder. The only injected value is ``ScratchRoot`` — the
resolver's own answer type, passed through the documented ``scratch``
parameter so a test need not own the host's ``/scratch``.
STX-TQ002 AAA markers; one fact per test (PA-307).
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
    ensure_scratch_uvwork,
    scratch_uvwork_dir,
    uvwork_bind_flags,
)
from tests.scitex_agent_container._helpers.scratch_agent import (
    load_uvwork_agent,
    uvwork_binds,
)


@pytest.fixture
def agent_config(tmp_path: Path):
    """A real, loadable spec named ``agt`` (the directory name is the name)."""
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


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


# ---------------------------------------------------------------------------
# The per-agent layout
# ---------------------------------------------------------------------------


def test_the_host_directory_is_per_agent_under_sac_agents(tmp_path: Path) -> None:
    # Arrange
    root = tmp_path / "scratch"
    # Act
    target = scratch_uvwork_dir(root, "agt")
    # Assert
    assert target == root / "sac" / "agents" / "agt" / "uvwork"


def test_two_agents_never_share_a_uvwork_directory(tmp_path: Path) -> None:
    # Arrange — the venv and uv cache are per-agent state.
    root = tmp_path / "scratch"
    # Act
    first, second = scratch_uvwork_dir(root, "a"), scratch_uvwork_dir(root, "b")
    # Assert
    assert first != second


def test_an_agent_name_with_a_separator_is_refused(tmp_path: Path) -> None:
    # Arrange — a name is ONE path component; "../x" must not escape.
    root = tmp_path / "scratch"
    # Act
    raised = pytest.raises(ValueError, match="path component")
    # Assert
    with raised:
        scratch_uvwork_dir(root, "../elsewhere")


def test_an_empty_agent_name_is_refused(tmp_path: Path) -> None:
    # Arrange
    root = tmp_path / "scratch"
    # Act
    raised = pytest.raises(ValueError, match="path component")
    # Assert
    with raised:
        scratch_uvwork_dir(root, "")


# ---------------------------------------------------------------------------
# Creating the bind source
# ---------------------------------------------------------------------------


def test_the_bind_source_directory_is_created(tmp_path: Path) -> None:
    # Arrange — apptainer needs the source to exist before exec.
    root = tmp_path / "scratch"
    root.mkdir()
    # Act
    target = ensure_scratch_uvwork(root, "agt")
    # Assert
    assert target.is_dir()


def test_the_created_directory_is_owner_only(tmp_path: Path) -> None:
    # Arrange
    root = tmp_path / "scratch"
    root.mkdir()
    # Act
    target = ensure_scratch_uvwork(root, "agt")
    # Assert
    assert _mode(target) == UVWORK_DIR_MODE


def test_the_owner_only_mode_is_0700(tmp_path: Path) -> None:
    # Arrange — spelled out so a silent widening of the constant fails here.
    expected = 0o700
    # Act
    mode = UVWORK_DIR_MODE
    # Assert
    assert mode == expected


def test_an_existing_directory_keeps_the_mode_it_had(tmp_path: Path) -> None:
    # Arrange — restarts must not overrule an operator's own chmod.
    root = tmp_path / "scratch"
    existing = root / "sac" / "agents" / "agt" / "uvwork"
    existing.mkdir(parents=True)
    os.chmod(existing, 0o755)
    # Act
    ensure_scratch_uvwork(root, "agt")
    # Assert
    assert _mode(existing) == 0o755


def test_an_existing_directorys_contents_survive_a_restart(tmp_path: Path) -> None:
    # Arrange — the venv already built there must still be there.
    root = tmp_path / "scratch"
    existing = root / "sac" / "agents" / "agt" / "uvwork"
    existing.mkdir(parents=True)
    (existing / "marker").write_text("venv", encoding="utf-8")
    # Act
    ensure_scratch_uvwork(root, "agt")
    # Assert
    assert (existing / "marker").read_text(encoding="utf-8") == "venv"


def test_a_file_where_the_directory_belongs_is_refused(tmp_path: Path) -> None:
    # Arrange — apptainer's own message for this is far less useful.
    root = tmp_path / "scratch"
    target = root / "sac" / "agents" / "agt" / "uvwork"
    target.parent.mkdir(parents=True)
    target.write_text("not a directory", encoding="utf-8")
    # Act
    raised = pytest.raises(ScratchRootError, match="not a directory")
    # Assert
    with raised:
        ensure_scratch_uvwork(root, "agt")


# ---------------------------------------------------------------------------
# The emitted flags
# ---------------------------------------------------------------------------


def test_the_flags_bind_the_per_agent_directory_read_write(
    agent_config, scratch: ScratchRoot
) -> None:
    # Arrange
    expected = f"{scratch.root}/sac/agents/agt/uvwork:{UVWORK_CONTAINER_PATH}:rw"
    # Act
    flags = uvwork_bind_flags(agent_config, [], scratch=scratch)
    # Assert
    assert flags == ["--bind", expected]


def test_a_written_none_decision_emits_no_bind(
    agent_config, no_scratch: ScratchRoot
) -> None:
    # Arrange — the host chose the overlay, in writing.
    # Act
    flags = uvwork_bind_flags(agent_config, [], scratch=no_scratch)
    # Assert
    assert flags == []


def test_a_spec_bind_to_uvwork_wins_over_the_default(
    agent_config, scratch: ScratchRoot
) -> None:
    # Arrange — apptainer keeps the FIRST bind to a destination.
    argv = ["--bind", f"/somewhere/else:{UVWORK_CONTAINER_PATH}"]
    # Act
    flags = uvwork_bind_flags(agent_config, argv, scratch=scratch)
    # Assert
    assert flags == []


def test_a_spec_bind_to_uvwork_with_a_mode_also_wins(
    agent_config, scratch: ScratchRoot
) -> None:
    # Arrange — the three-field spelling is the same declaration.
    argv = ["--bind", f"/somewhere/else:{UVWORK_CONTAINER_PATH}:ro"]
    # Act
    flags = uvwork_bind_flags(agent_config, argv, scratch=scratch)
    # Assert
    assert flags == []


def test_an_unrelated_bind_does_not_suppress_the_default(
    agent_config, scratch: ScratchRoot
) -> None:
    # Arrange — the positive control for the two rows above.
    argv = ["--bind", "/scratch:/scratch:rw", "--bind", "/data:/data"]
    # Act
    flags = uvwork_bind_flags(agent_config, argv, scratch=scratch)
    # Assert
    assert flags[0] == "--bind"


def test_a_bind_whose_SOURCE_is_uvwork_does_not_suppress_the_default(
    agent_config, scratch: ScratchRoot
) -> None:
    # Arrange — only the DESTINATION decides; a source named /uvwork is
    # a different bind entirely.
    argv = ["--bind", f"{UVWORK_CONTAINER_PATH}:/elsewhere"]
    # Act
    flags = uvwork_bind_flags(agent_config, argv, scratch=scratch)
    # Assert
    assert flags[0] == "--bind"


# ---------------------------------------------------------------------------
# End to end — the argv a real launch would receive
# ---------------------------------------------------------------------------


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
def host_declares_none(tmp_path: Path, env_save_restore) -> Path:
    """A real config.yaml recording the decision to stay in the overlay."""
    cfg = tmp_path / "config-none.yaml"
    cfg.write_text(
        "scratch_root: none\nscratch_root_reason: red control\n", encoding="utf-8"
    )
    env_save_restore.set("SCITEX_AGENT_CONTAINER_CONFIG", str(cfg))
    return cfg


def test_build_run_argv_binds_uvwork_from_the_host_scratch_root(
    agent_config, tmp_path: Path, host_declares_scratch: Path
) -> None:
    # Arrange
    expected = f"{host_declares_scratch}/sac/agents/agt/uvwork:{UVWORK_CONTAINER_PATH}:rw"
    # Act
    argv = build_run_argv(
        agent_config,
        state_dir=tmp_path / "state",
        sif_path=Path("/img/sac.sif"),
        tui=True,
    )
    # Assert
    assert uvwork_binds(argv) == [expected]


def test_build_run_argv_emits_no_uvwork_bind_on_a_none_host(
    agent_config, tmp_path: Path, host_declares_none: Path
) -> None:
    # Arrange — the RED CONTROL for the row above: same spec, same builder,
    # only the host's written decision differs.
    # Act
    argv = build_run_argv(
        agent_config,
        state_dir=tmp_path / "state2",
        sif_path=Path("/img/sac.sif"),
        tui=True,
    )
    # Assert
    assert uvwork_binds(argv) == []


def test_build_run_argv_creates_nothing_under_the_scratch_root(
    agent_config, tmp_path: Path, host_declares_scratch: Path
) -> None:
    # Arrange — `sac agents explain` and `sac agents start --dry-run` both
    # call this builder and start nothing. The rest of that split, and the
    # host with no scratch root at all, are in
    # ``test__apptainer_scratch_launch.py`` beside this file.
    # Act
    build_run_argv(
        agent_config,
        state_dir=tmp_path / "state3",
        sif_path=Path("/img/sac.sif"),
        tui=True,
    )
    # Assert
    assert list(host_declares_scratch.rglob("uvwork")) == []
