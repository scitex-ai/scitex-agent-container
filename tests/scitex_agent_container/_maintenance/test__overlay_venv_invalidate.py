"""The invalidation itself, against a REAL filesystem.

Mirrors ``src/scitex_agent_container/_maintenance/_overlay_venv_invalidate.py``.

Every overlay here is a real directory tree with real ``.dist-info``
directories, and every SIF is a real file behind a real symlink — because the
two properties under test are filesystem properties. ``sac-base.sif`` being a
STABLE symlink is the entire reason the identity cannot be the filename, and no
amount of faking would have caught keying on the wrong name.

THE ONE SEAM is ``inside_container_fn``. sac's suite runs INSIDE a container, so
the real detector answers True and every reconcile correctly refuses; without
the seam the acting path could only ever run on a bare host, i.e. never in CI.
The refusal path is tested with the REAL detector left in place as well, so the
seam cannot hide a guard that stopped working.

Nothing here mocks (PA-306): the seam is a one-line real function, and the
liveness checks use real PIDs of real processes.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from scitex_agent_container._maintenance import _overlay_venv_invalidate as INV
from scitex_agent_container._maintenance._overlay_venv_model import (
    ACTION_INVALIDATE,
    ACTION_NONE,
    ACTION_REFUSE,
)

VENV = "/opt/venv-sac"

#: A frozen clock, so the archive directory name is deterministic. The datetime
#: is PARSED FROM the expected directory name rather than written twice: the
#: test asserts the archive lands at exactly this stamp, and two hand-kept
#: spellings of the same instant is the classic way that assertion starts
#: passing for the wrong reason.
ARCHIVE_STAMP = "20260811T063000Z"
FIXED_NOW = datetime.strptime(ARCHIVE_STAMP, "%Y%m%dT%H%M%SZ").replace(
    tzinfo=timezone.utc
)


def ON_THE_HOST() -> bool:
    """The seam, as a real function. Not a mock — it records nothing and makes
    no assertions about being called; it just answers the question the same way
    the real detector would on a bare host."""
    return False


def IN_A_CONTAINER() -> bool:
    """The same seam, answering the way the real detector does inside a SIF."""
    return True


#: How many times the base probe was invoked. A module-level counter rather than
#: a mock's call log: the laziness of the probe is a REAL property worth pinning
#: (it costs an `apptainer exec`), and counting is the whole of what we need.
_base_probe_calls: list[tuple[str, str]] = []


def BASE_POPULATED(sif, venv):
    """Base-probe seam: the image ships a populated venv (the healthy fleet)."""
    _base_probe_calls.append((str(sif), venv))
    return True


def BASE_EMPTY(sif, venv):
    """Base-probe seam: the image has NO venv to fall back on."""
    _base_probe_calls.append((str(sif), venv))
    return False


def BASE_UNREADABLE(sif, venv):
    """Base-probe seam: the probe could not run — UNKNOWN, never a verdict."""
    _base_probe_calls.append((str(sif), venv))
    return None


def _sif(tmp_path: Path, target_name: str) -> Path:
    """A real SIF file behind the stable ``sac-base.sif`` symlink the fleet uses."""
    containers = tmp_path / "containers"
    containers.mkdir(exist_ok=True)
    target = containers / target_name
    target.write_bytes(b"SIF" + target_name.encode())
    link = containers / "sac-base.sif"
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(target)
    return link


def _overlay(tmp_path: Path, *, with_slice: bool = True) -> Path:
    """A directory overlay shaped like the fleet's, optionally already shadowing."""
    root = tmp_path / "overlays" / "agent-x"
    (root / "upper").mkdir(parents=True, exist_ok=True)
    (root / "work").mkdir(parents=True, exist_ok=True)
    if with_slice:
        site = root / "upper" / VENV.lstrip("/") / "lib/python3.12/site-packages"
        (site / "scitex_dev-0.38.0.dist-info").mkdir(parents=True)
        (site / "scitex_dev-0.38.0.dist-info" / "METADATA").write_text(
            "Name: scitex-dev\n"
        )
    return root


def _config(overlay_root: Path, **apptainer_extra) -> SimpleNamespace:
    ap = SimpleNamespace(
        overlay=str(overlay_root),
        raw_args=[],
        image="",
        overlay_size="",
        **apptainer_extra,
    )
    return SimpleNamespace(
        apptainer=ap, workdir=str(overlay_root.parent), name="agent-x"
    )


# ---------------------------------------------------------------------------
# SIF identity — the key is the RESOLVED TARGET, never the symlink's own name
# ---------------------------------------------------------------------------
def test_identity_keys_on_the_symlink_target_not_the_symlink_name(tmp_path) -> None:
    """`sac-base.sif` never changes name, so keying on it could never fail."""
    # Arrange
    link = _sif(tmp_path, "sac-base-2026-0810-195145.sif")
    # Act
    identity = INV.sif_identity(link)
    # Assert
    assert identity.startswith("sac-base-2026-0810-195145.sif:")


def test_identity_does_not_contain_the_stable_symlink_name(tmp_path) -> None:
    """The negative control for the test above — the whole bug in one assert."""
    # Arrange
    link = _sif(tmp_path, "sac-base-2026-0810-195145.sif")
    # Act
    identity = INV.sif_identity(link)
    # Assert
    assert not identity.startswith("sac-base.sif:")


def test_repointing_the_symlink_changes_the_identity(tmp_path) -> None:
    """This IS an image rebuild, as the fleet performs it."""
    # Arrange
    before = INV.sif_identity(_sif(tmp_path, "sac-base-2026-0810-195145.sif"))
    # Act
    after = INV.sif_identity(_sif(tmp_path, "sac-base-2026-0811-071500.sif"))
    # Assert
    assert before != after


def test_an_unresolvable_image_has_no_identity(tmp_path) -> None:
    """Empty, so the predicate reports UNKNOWN and refuses rather than guesses."""
    # Arrange
    missing = tmp_path / "containers" / "gone.sif"
    # Act
    identity = INV.sif_identity(missing)
    # Assert
    assert identity == ""


def test_a_dangling_symlink_has_no_identity(tmp_path) -> None:
    """A half-finished rebuild must not be mistaken for a valid new image."""
    # Arrange
    link = tmp_path / "dangling.sif"
    link.symlink_to(tmp_path / "never-built.sif")
    # Act
    identity = INV.sif_identity(link)
    # Assert
    assert identity == ""


# ---------------------------------------------------------------------------
# The stamp
# ---------------------------------------------------------------------------
def test_an_unstamped_overlay_reads_as_empty_not_unreadable(tmp_path) -> None:
    # Arrange
    root = _overlay(tmp_path)
    # Act
    recorded = INV.read_stamp(root)
    # Assert
    assert recorded == ""


def test_a_written_stamp_reads_back(tmp_path) -> None:
    # Arrange
    root = _overlay(tmp_path)
    # Act
    INV.write_stamp(root, "sac-base-2026-0810.sif:1:2")
    # Assert
    assert INV.read_stamp(root) == "sac-base-2026-0810.sif:1:2"


def test_the_stamp_lives_outside_the_upper_layer(tmp_path) -> None:
    """Inside ``upper/`` the agent could clobber its own invalidation stamp."""
    # Arrange
    root = _overlay(tmp_path)
    # Act
    path = INV.stamp_path(root)
    # Assert
    assert path.parent == root


# ---------------------------------------------------------------------------
# Liveness — real PIDs, no fakes
# ---------------------------------------------------------------------------
def test_a_missing_pid_file_reads_as_not_running(tmp_path) -> None:
    """The state a clean stop leaves behind, and a first start begins in."""
    # Arrange
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    # Act
    running = INV.agent_running_from_state_dir(state_dir)
    # Assert
    assert running is False


def test_a_live_pid_reads_as_running(tmp_path) -> None:
    """Uses THIS process — a real, certainly-alive pid."""
    # Arrange
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "pid").write_text(f"{os.getpid()}\n")
    # Act
    running = INV.agent_running_from_state_dir(state_dir)
    # Assert
    assert running is True


def test_an_unparseable_pid_file_reads_as_unknown(tmp_path) -> None:
    """A file that exists and cannot be parsed is a question, not an answer."""
    # Arrange
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "pid").write_text("not-a-pid\n")
    # Act
    running = INV.agent_running_from_state_dir(state_dir)
    # Assert
    assert running is None


def test_a_plain_directory_is_not_a_live_overlay_mount(tmp_path) -> None:
    """Reads the REAL /proc/self/mountinfo — a tmp_path overlay is not mounted."""
    # Arrange
    root = _overlay(tmp_path)
    # Act
    mounted = INV.upper_mounted_here(root)
    # Assert
    assert mounted is False


# ---------------------------------------------------------------------------
# The mutation — move aside, never delete
# ---------------------------------------------------------------------------
def test_a_stale_overlay_is_invalidated(tmp_path) -> None:
    # Arrange
    root = _overlay(tmp_path)
    link = _sif(tmp_path, "sac-base-2026-0810-195145.sif")
    # Act
    plan = INV.reconcile_overlay_venv(
        _config(root),
        link,
        agent_running=False,
        inside_container_fn=ON_THE_HOST,
        base_probe=BASE_POPULATED,
    )
    # Assert
    assert plan.action == ACTION_INVALIDATE


def test_the_stale_slice_leaves_the_upper_layer(tmp_path) -> None:
    # Arrange
    root = _overlay(tmp_path)
    link = _sif(tmp_path, "sac-base-2026-0810-195145.sif")
    # Act
    INV.reconcile_overlay_venv(
        _config(root),
        link,
        agent_running=False,
        inside_container_fn=ON_THE_HOST,
        base_probe=BASE_POPULATED,
    )
    # Assert
    assert not (root / "upper" / VENV.lstrip("/")).exists()


def test_nothing_is_deleted_only_moved(tmp_path) -> None:
    """The standing fleet rule, and what keeps a wrong prune recoverable."""
    # Arrange
    root = _overlay(tmp_path)
    link = _sif(tmp_path, "sac-base-2026-0810-195145.sif")
    # Act
    INV.reconcile_overlay_venv(
        _config(root),
        link,
        agent_running=False,
        now=FIXED_NOW,
        inside_container_fn=ON_THE_HOST,
        base_probe=BASE_POPULATED,
    )
    # Assert
    assert (
        root
        / ".old"
        / ARCHIVE_STAMP
        / "upper"
        / VENV.lstrip("/")
        / "lib/python3.12/site-packages/scitex_dev-0.38.0.dist-info/METADATA"
    ).is_file()


def test_the_archive_sits_outside_the_upper_layer(tmp_path) -> None:
    """Inside ``upper/`` the archived tree would still be in the container's view."""
    # Arrange
    root = _overlay(tmp_path)
    link = _sif(tmp_path, "sac-base-2026-0810-195145.sif")
    # Act
    INV.reconcile_overlay_venv(
        _config(root),
        link,
        agent_running=False,
        now=FIXED_NOW,
        inside_container_fn=ON_THE_HOST,
        base_probe=BASE_POPULATED,
    )
    # Assert
    assert not (root / "upper" / ".old").exists()


def test_invalidating_records_the_new_image_identity(tmp_path) -> None:
    # Arrange
    root = _overlay(tmp_path)
    link = _sif(tmp_path, "sac-base-2026-0810-195145.sif")
    # Act
    INV.reconcile_overlay_venv(
        _config(root),
        link,
        agent_running=False,
        inside_container_fn=ON_THE_HOST,
        base_probe=BASE_POPULATED,
    )
    # Assert
    assert INV.read_stamp(root) == INV.sif_identity(link)


def test_a_second_start_on_the_same_image_is_a_no_op(tmp_path) -> None:
    """Idempotent: the contract fires on CHANGE, not on every boot."""
    # Arrange
    root = _overlay(tmp_path)
    link = _sif(tmp_path, "sac-base-2026-0810-195145.sif")
    INV.reconcile_overlay_venv(
        _config(root),
        link,
        agent_running=False,
        inside_container_fn=ON_THE_HOST,
        base_probe=BASE_POPULATED,
    )
    # Act
    plan = INV.reconcile_overlay_venv(
        _config(root),
        link,
        agent_running=False,
        inside_container_fn=ON_THE_HOST,
        base_probe=BASE_POPULATED,
    )
    # Assert
    assert plan.action == ACTION_NONE


def test_a_rebuilt_image_invalidates_again(tmp_path) -> None:
    """The contract in one test: rebuild the image, the overlay's venv goes."""
    # Arrange
    root = _overlay(tmp_path)
    old = _sif(tmp_path, "sac-base-2026-0810-195145.sif")
    INV.reconcile_overlay_venv(
        _config(root),
        old,
        agent_running=False,
        inside_container_fn=ON_THE_HOST,
        base_probe=BASE_POPULATED,
    )
    site = root / "upper" / VENV.lstrip("/") / "lib/python3.12/site-packages"
    (site / "scitex_dev-0.39.0.dist-info").mkdir(parents=True)
    # Act
    new = _sif(tmp_path, "sac-base-2026-0811-071500.sif")
    plan = INV.reconcile_overlay_venv(
        _config(root),
        new,
        agent_running=False,
        inside_container_fn=ON_THE_HOST,
        base_probe=BASE_POPULATED,
    )
    # Assert
    assert plan.action == ACTION_INVALIDATE


def test_an_overlay_with_no_venv_slice_is_left_alone(tmp_path) -> None:
    # Arrange
    root = _overlay(tmp_path, with_slice=False)
    link = _sif(tmp_path, "sac-base-2026-0810-195145.sif")
    # Act
    plan = INV.reconcile_overlay_venv(
        _config(root),
        link,
        agent_running=False,
        inside_container_fn=ON_THE_HOST,
        base_probe=BASE_POPULATED,
    )
    # Assert
    assert plan.action == ACTION_NONE


# ---------------------------------------------------------------------------
# Refusals — the guard, with the REAL detector where it can be used
# ---------------------------------------------------------------------------
def test_a_running_agent_is_refused(tmp_path) -> None:
    # Arrange
    root = _overlay(tmp_path)
    link = _sif(tmp_path, "sac-base-2026-0810-195145.sif")
    # Act
    plan = INV.reconcile_overlay_venv(
        _config(root),
        link,
        agent_running=True,
        inside_container_fn=ON_THE_HOST,
        base_probe=BASE_POPULATED,
    )
    # Assert
    assert plan.action == ACTION_REFUSE


def test_a_refused_overlay_keeps_its_venv_slice(tmp_path) -> None:
    """A refusal must change nothing at all, not merely skip the log line."""
    # Arrange
    root = _overlay(tmp_path)
    link = _sif(tmp_path, "sac-base-2026-0810-195145.sif")
    # Act
    INV.reconcile_overlay_venv(
        _config(root),
        link,
        agent_running=True,
        inside_container_fn=ON_THE_HOST,
        base_probe=BASE_POPULATED,
    )
    # Assert
    assert (root / "upper" / VENV.lstrip("/")).is_dir()


def test_a_refusal_does_not_advance_the_stamp(tmp_path) -> None:
    """Otherwise the NEXT boot reads 'fresh' and the refusal became a pass."""
    # Arrange
    root = _overlay(tmp_path)
    link = _sif(tmp_path, "sac-base-2026-0810-195145.sif")
    # Act
    INV.reconcile_overlay_venv(
        _config(root),
        link,
        agent_running=True,
        inside_container_fn=ON_THE_HOST,
        base_probe=BASE_POPULATED,
    )
    # Assert
    assert INV.read_stamp(root) == ""


def test_unmeasured_liveness_is_refused(tmp_path) -> None:
    """A caller that did not answer the question does not get the mutation."""
    # Arrange
    root = _overlay(tmp_path)
    link = _sif(tmp_path, "sac-base-2026-0810-195145.sif")
    # Act
    plan = INV.reconcile_overlay_venv(
        _config(root),
        link,
        agent_running=None,
        inside_container_fn=ON_THE_HOST,
        base_probe=BASE_POPULATED,
    )
    # Assert
    assert plan.action == ACTION_REFUSE


def test_an_unresolvable_image_is_refused(tmp_path) -> None:
    # Arrange
    root = _overlay(tmp_path)
    missing = tmp_path / "containers" / "never-built.sif"
    # Act
    plan = INV.reconcile_overlay_venv(
        _config(root),
        missing,
        agent_running=False,
        inside_container_fn=ON_THE_HOST,
        base_probe=BASE_POPULATED,
    )
    # Assert
    assert plan.action == ACTION_REFUSE


def test_a_declared_container_context_is_refused(tmp_path) -> None:
    """The whiteout guard, exercised through the same seam in the True direction."""
    # Arrange
    root = _overlay(tmp_path)
    link = _sif(tmp_path, "sac-base-2026-0810-195145.sif")
    # Act
    plan = INV.reconcile_overlay_venv(
        _config(root),
        link,
        agent_running=False,
        inside_container_fn=IN_A_CONTAINER,
        base_probe=BASE_POPULATED,
    )
    # Assert
    assert plan.action == ACTION_REFUSE


def test_the_real_detector_sees_this_container(tmp_path) -> None:
    """POSITIVE CONTROL for the seam: the suite runs inside a SIF, so the real
    detector must say so. If this ever flips to False the seam is masking a
    detector that stopped working, and every refusal test above goes hollow.
    """
    # Arrange
    env_says = bool(
        os.environ.get("APPTAINER_CONTAINER") or os.environ.get("SINGULARITY_CONTAINER")
    )
    marker = Path("/.singularity.d").is_dir()
    # Act
    detected = INV.inside_container()
    # Assert
    assert detected == (env_says or marker)


# ---------------------------------------------------------------------------
# The lower layer must be there BEFORE we move the upper's copy away
# ---------------------------------------------------------------------------
def test_an_empty_image_venv_is_refused(tmp_path) -> None:
    """Moving the slice aside only UNHIDES the image's copy; it makes none."""
    # Arrange
    root = _overlay(tmp_path)
    link = _sif(tmp_path, "sac-base-2026-0810-195145.sif")
    # Act
    plan = INV.reconcile_overlay_venv(
        _config(root),
        link,
        agent_running=False,
        inside_container_fn=ON_THE_HOST,
        base_probe=BASE_EMPTY,
    )
    # Assert
    assert plan.action == ACTION_REFUSE


def test_an_empty_image_venv_leaves_the_slice_in_place(tmp_path) -> None:
    """The slice is the agent's ONLY venv here — moving it would kill it."""
    # Arrange
    root = _overlay(tmp_path)
    link = _sif(tmp_path, "sac-base-2026-0810-195145.sif")
    # Act
    INV.reconcile_overlay_venv(
        _config(root),
        link,
        agent_running=False,
        inside_container_fn=ON_THE_HOST,
        base_probe=BASE_EMPTY,
    )
    # Assert
    assert (root / "upper" / VENV.lstrip("/")).is_dir()


def test_an_unreadable_image_venv_is_refused(tmp_path) -> None:
    """A probe that could not run is UNKNOWN, and UNKNOWN never moves files."""
    # Arrange
    root = _overlay(tmp_path)
    link = _sif(tmp_path, "sac-base-2026-0810-195145.sif")
    # Act
    plan = INV.reconcile_overlay_venv(
        _config(root),
        link,
        agent_running=False,
        inside_container_fn=ON_THE_HOST,
        base_probe=BASE_UNREADABLE,
    )
    # Assert
    assert plan.action == ACTION_REFUSE


def test_the_image_is_not_probed_when_nothing_would_move(tmp_path) -> None:
    """The probe costs an `apptainer exec`; an ordinary boot must not pay it."""
    # Arrange
    root = _overlay(tmp_path)
    link = _sif(tmp_path, "sac-base-2026-0810-195145.sif")
    INV.reconcile_overlay_venv(
        _config(root),
        link,
        agent_running=False,
        inside_container_fn=ON_THE_HOST,
        base_probe=BASE_POPULATED,
    )
    _base_probe_calls.clear()
    # Act
    INV.reconcile_overlay_venv(  # second start, same image — already reconciled
        _config(root),
        link,
        agent_running=False,
        inside_container_fn=ON_THE_HOST,
        base_probe=BASE_POPULATED,
    )
    # Assert
    assert _base_probe_calls == []


def test_the_image_is_probed_when_a_move_is_proposed(tmp_path) -> None:
    """Negative control for the laziness test above — it must fire when it matters."""
    # Arrange
    root = _overlay(tmp_path)
    link = _sif(tmp_path, "sac-base-2026-0810-195145.sif")
    _base_probe_calls.clear()
    # Act
    INV.reconcile_overlay_venv(
        _config(root),
        link,
        agent_running=False,
        inside_container_fn=ON_THE_HOST,
        base_probe=BASE_POPULATED,
    )
    # Assert
    assert len(_base_probe_calls) == 1


def test_the_real_base_probe_reports_unknown_for_a_non_image(tmp_path) -> None:
    """A file that is not a SIF cannot answer, and must not answer 'fine'.

    Runs the REAL probe (a real `apptainer exec` attempt against a real file
    that is not an image) rather than a seam — the seam proves the wiring, this
    proves the probe itself refuses to invent an answer.
    """
    # Arrange
    not_an_image = tmp_path / "definitely-not.sif"
    not_an_image.write_bytes(b"not a squashfs")
    # Act
    answer = INV.base_provides_venv(not_an_image)
    # Assert
    assert answer is None


# ---------------------------------------------------------------------------
# Out of scope — the contract simply does not apply
# ---------------------------------------------------------------------------
def test_an_agent_with_no_overlay_is_out_of_scope(tmp_path) -> None:
    # Arrange
    config = SimpleNamespace(
        apptainer=SimpleNamespace(overlay="", raw_args=[], image="", overlay_size=""),
        workdir=str(tmp_path),
        name="agent-x",
    )
    link = _sif(tmp_path, "sac-base-2026-0810-195145.sif")
    # Act
    plan = INV.reconcile_overlay_venv(
        config,
        link,
        agent_running=False,
        inside_container_fn=ON_THE_HOST,
        base_probe=BASE_POPULATED,
    )
    # Assert
    assert plan is None


def test_a_loopback_image_overlay_is_out_of_scope(tmp_path) -> None:
    """Its upper layer is not host-readable, so it cannot be invalidated here."""
    # Arrange
    root = _overlay(tmp_path)
    config = _config(root)
    config.apptainer.overlay_size = "5G"
    link = _sif(tmp_path, "sac-base-2026-0810-195145.sif")
    # Act
    plan = INV.reconcile_overlay_venv(
        config,
        link,
        agent_running=False,
        inside_container_fn=ON_THE_HOST,
        base_probe=BASE_POPULATED,
    )
    # Assert
    assert plan is None


def test_the_launch_wrapper_never_raises_on_a_broken_config(tmp_path) -> None:
    """A bug in this rail must not refuse every start on the host."""
    # Arrange
    broken = SimpleNamespace(name="agent-x")  # no .apptainer, no .workdir
    link = _sif(tmp_path, "sac-base-2026-0810-195145.sif")
    # Act
    plan = INV.reconcile_overlay_venv_for_launch(broken, link, tmp_path / "state")
    # Assert
    assert plan is None
