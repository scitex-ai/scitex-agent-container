"""Enforce the overlay-venv invalidation contract: observe, decide, MOVE ASIDE.

The contract, the measurement and the vocabulary live in
:mod:`._overlay_venv_model`; the pure decision lives in
:mod:`._overlay_venv_predicate`. This module is the I/O half — it gathers the
facts, asks the predicate, and performs the one mutation the rail is allowed to
make.

NOTHING IS EVER DELETED. The stale slice is renamed into
``<overlay>/.old/<timestamp>/upper/opt/venv-sac``. That is the standing fleet
rule and it is also what keeps the rail debuggable: if a prune is ever wrong,
the evidence is one ``mv`` away from being restored, and the path mirrors the
original so the restore is mechanical.

WHERE THE ARCHIVE LIVES, AND WHY NOT UNDER ``upper/``. ``.old/`` sits beside
``upper/``, not inside it. Inside, the archived tree would still be part of the
container's filesystem view — it would keep counting against the agent's disk,
keep showing up in every in-container scan, and (worst) a shadowed
``site-packages`` copy would still be reachable on a stray ``sys.path`` entry.
Outside, apptainer never sees it: apptainer reads ``upper/`` and ``work/`` and
nothing else under the overlay root. The same reasoning places the stamp file.

REFUSING MUST NOT BE SELF-ERASING. On a refusal the stamp is deliberately NOT
written. Writing it would record the current image as reconciled without having
reconciled anything, and the NEXT boot would then find the overlay "fresh" and
skip the work — a refusal that quietly converts itself into a pass.

WHY THIS NEVER RAISES INTO THE LAUNCH PATH. It is called from
``build_run_argv``, so an exception here would refuse every start on the host —
a guard more dangerous than the fault it guards, exactly as
:func:`..runtimes._entry_point_gate.assert_entry_point_runs` documents. A
reconcile that cannot run logs loudly and lets the launch proceed; the second
layer, the BOOT ASSERTION in :mod:`._venv_dist_assertion`, then refuses INSIDE
the container if the union really is incoherent. Try to repair host-side,
refuse to run broken in-container.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from .._drift.versions import DEFAULT_VENV
from ..runtimes._apptainer_overlay import (
    OVERLAY_UPPER_DIRNAME,
    OVERLAY_WORK_DIRNAME,
    is_image_overlay,
    resolve_overlay_declaration,
)
from ._overlay_venv_model import (
    ACTION_INVALIDATE,
    ACTION_REFUSE,
    InvalidationPlan,
    OverlayVenvFacts,
)
from ._overlay_venv_predicate import plan_invalidation

logger = logging.getLogger(__name__)

__all__ = [
    "ARCHIVE_DIRNAME",
    "STAMP_FILENAME",
    "agent_running_from_state_dir",
    "archive_dir_for",
    "inside_container",
    "observe_overlay",
    "read_stamp",
    "reconcile_overlay_venv",
    "reconcile_overlay_venv_for_launch",
    "sif_identity",
    "stamp_path",
    "upper_mounted_here",
    "venv_slice",
    "write_stamp",
]

#: Beside ``upper/`` and ``work/``, never inside them (see module docstring).
ARCHIVE_DIRNAME = ".old"
STAMP_FILENAME = ".sac-venv-sif-id"

#: Same stamp format the rest of the package archives with
#: (``cli_pkg._declare_a2a_host._archive_dir``), so ``.old/`` directories sort
#: and read identically wherever an operator meets one.
_ARCHIVE_STAMP_FMT = "%Y%m%dT%H%M%SZ"

_MOUNTINFO = Path("/proc/self/mountinfo")

#: Second, filesystem-level witness for "am I inside a container". Apptainer
#: creates this directory in every container it builds; it survives
#: ``--cleanenv``, which strips the environment witness.
_APPTAINER_MARKER_DIR = Path("/.singularity.d")


def sif_identity(sif_path: Path | str) -> str:
    """Identity of the image ``sif_path`` names, or ``""`` when unresolvable.

    THE FILENAME IS NOT THE KEY. ``containers/sac-base.sif`` is a STABLE
    SYMLINK onto a timestamped target::

        sac-base.sif -> .../sac-base/sac-base-2026-0810-195145.sif

    so its own name is identical before and after a rebuild. Keying on it
    yields a check that can never fail — worse than no check, because the
    config still lists it. We resolve the symlink and key on the TARGET.

    Size and mtime join the basename deliberately. A rebuild that reuses the
    same target filename (an in-place overwrite, or a layout without the
    timestamp) would be invisible to the name alone. The bias is intentional:
    a FALSE invalidation costs a re-install of genuinely overlay-only packages
    and moves nothing to the bin, while a FALSE NEGATIVE is the bug this rail
    exists to close. Over-sensitive is the safe direction here.
    """
    try:
        target = Path(sif_path).expanduser().resolve(strict=True)
        st = target.stat()
    except OSError as exc:
        logger.debug("overlay-venv: image %s did not resolve: %s", sif_path, exc)
        return ""
    return f"{target.name}:{st.st_size}:{st.st_mtime_ns}"


def stamp_path(overlay_root: Path | str) -> Path:
    """Where this overlay's reconciled-image identity is recorded.

    OUTSIDE ``upper/`` on purpose: the container's filesystem view never
    includes it, so an agent cannot clobber its own invalidation stamp from
    inside — accidentally or otherwise.
    """
    return Path(overlay_root) / STAMP_FILENAME


def read_stamp(overlay_root: Path | str) -> str | None:
    """The recorded identity, ``""`` when never stamped, ``None`` when unreadable.

    The three returns are three different facts and the caller treats them as
    such. In particular ``None`` is NOT ``""``: an unreadable stamp must not be
    mistaken for a never-stamped overlay, because the latter authorises a move.
    """
    path = stamp_path(overlay_root)
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""
    except OSError as exc:
        logger.warning("overlay-venv: stamp %s unreadable: %s", path, exc)
        return None


def write_stamp(overlay_root: Path | str, identity: str) -> bool:
    """Record ``identity`` as reconciled. ``False`` (loudly) when the write fails."""
    path = stamp_path(overlay_root)
    try:
        path.write_text(f"{identity}\n", encoding="utf-8")
    except OSError as exc:
        logger.warning("overlay-venv: could not write stamp %s: %s", path, exc)
        return False
    return True


def inside_container() -> bool:
    """Is THIS process running inside an apptainer container?

    Two independent witnesses, either decisive. The environment witness is
    :func:`.._lifecycle._in_sif_broker.is_in_sif` — reused rather than
    re-spelled so sac keeps ONE definition of "the env says SIF". The
    filesystem witness covers the case that one misses: ``--cleanenv`` strips
    ``APPTAINER_CONTAINER`` while ``/.singularity.d`` remains.

    Total by construction (an env read cannot fail and ``Path.exists`` swallows
    its own errors), so this never has to report UNKNOWN.
    """
    from .._lifecycle._in_sif_broker import is_in_sif

    if is_in_sif():
        return True
    return _APPTAINER_MARKER_DIR.is_dir()


def upper_mounted_here(overlay_root: Path | str) -> bool | None:
    """Does THIS process's mount table show an overlayfs using this overlay?

    ``None`` when the mount table could not be read — never ``False``. A
    corroborating witness only: a mount made by another process lives in that
    process's mount namespace and is invisible here, so a ``False`` proves that
    WE have not mounted it and nothing more. Liveness of the agent is what
    covers the other namespaces.
    """
    root = Path(overlay_root)
    upper = str(root / OVERLAY_UPPER_DIRNAME).rstrip("/")
    work = str(root / OVERLAY_WORK_DIRNAME).rstrip("/")
    try:
        raw = _MOUNTINFO.read_text(encoding="utf-8")
    except OSError as exc:
        logger.debug("overlay-venv: %s unreadable: %s", _MOUNTINFO, exc)
        return None
    for line in raw.splitlines():
        # mountinfo: "<mount fields> - <fstype> <source> <super options>".
        _, _, tail = line.partition(" - ")
        fields = tail.split()
        if len(fields) < 3 or fields[0] != "overlay":
            continue
        for option in fields[2].split(","):
            key, _, value = option.partition("=")
            if key == "upperdir" and value.rstrip("/") == upper:
                return True
            if key == "workdir" and value.rstrip("/") == work:
                return True
    return False


def _pid_alive(pid: int) -> bool:
    """``os.kill(pid, 0)`` — the same instrument sac's own verdict rail uses."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    return True


def agent_running_from_state_dir(state_dir: Path | str) -> bool | None:
    """Liveness from the agent's PID file. ``None`` when the file is unreadable.

    An ABSENT pid file reads as ``False``: nothing claims to be running, which
    is the state a clean stop leaves behind and the state a first start begins
    in. An UNREADABLE or malformed one reads as ``None``, because a file that
    exists and cannot be parsed is a question, not an answer.
    """
    pid_file = Path(state_dir) / "pid"
    try:
        raw = pid_file.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return False
    except OSError as exc:
        logger.warning("overlay-venv: pid file %s unreadable: %s", pid_file, exc)
        return None
    if not raw:
        return False
    try:
        pid = int(raw.splitlines()[0])
    except ValueError:
        logger.warning("overlay-venv: pid file %s is not a pid: %r", pid_file, raw)
        return None
    return _pid_alive(pid)


def venv_slice(overlay_root: Path | str, venv: str = DEFAULT_VENV) -> Path:
    """``<overlay>/upper/opt/venv-sac`` — the slice the contract invalidates."""
    return Path(overlay_root) / OVERLAY_UPPER_DIRNAME / venv.lstrip("/")


def observe_overlay(
    overlay_root: Path | str,
    sif_path: Path | str,
    *,
    agent_running: bool | None,
    venv: str = DEFAULT_VENV,
    inside_container_fn=None,
) -> OverlayVenvFacts:
    """Gather every fact the predicate needs. Reads only; mutates nothing.

    ``inside_container_fn`` is an injection seam (``() -> bool``), and it earns
    its keep the moment you try to test this: sac's own test suite RUNS INSIDE
    A CONTAINER, so the real :func:`inside_container` answers ``True`` there and
    every reconcile correctly refuses. Without the seam the acting path could
    only ever be exercised on a bare host — i.e. never in CI. The default is the
    real function, so production keeps the real guard.
    """
    slice_path = venv_slice(overlay_root, venv)
    try:
        slice_present: bool | None = slice_path.is_dir()
    except OSError as exc:  # stx-allow: fallback (reason: an unreadable parent must report UNKNOWN, never 'absent')
        logger.warning("overlay-venv: could not stat %s: %s", slice_path, exc)
        slice_present = None
    return OverlayVenvFacts(
        sif_identity=sif_identity(sif_path),
        recorded_identity=read_stamp(overlay_root),
        venv_slice_present=slice_present,
        inside_container=(inside_container_fn or inside_container)(),
        agent_running=agent_running,
        upper_mounted_here=upper_mounted_here(overlay_root),
    )


def archive_dir_for(overlay_root: Path | str, *, now: datetime | None = None) -> Path:
    """``<overlay>/.old/<timestamp>/upper/opt/venv-sac`` — the restore target.

    The archived path MIRRORS the original below ``.old/<ts>/`` so undoing an
    invalidation is one mechanical ``mv`` with no path arithmetic.
    """
    stamp = (now or datetime.now(timezone.utc)).strftime(_ARCHIVE_STAMP_FMT)
    return Path(overlay_root) / ARCHIVE_DIRNAME / stamp / OVERLAY_UPPER_DIRNAME


def _move_aside(
    agent: str,
    overlay_root: Path,
    plan: InvalidationPlan,
    venv: str,
    now: datetime | None = None,
) -> Path | None:
    """Rename the stale slice under ``.old/``; return where it landed."""
    source = venv_slice(overlay_root, venv)
    destination = archive_dir_for(overlay_root, now=now) / venv.lstrip("/")
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.rename(source, destination)
    # ONE line, naming BOTH image identities and the path that moved. Silence
    # is what cost scitex-hub a session: the agent read a broken venv as a
    # broken repo because nothing anywhere said the env had changed under it.
    # This line is part of the fix, not a nicety.
    logger.warning(
        "overlay-venv INVALIDATED for agent %s: image changed %s -> %s; "
        "moved %s -> %s (nothing deleted; restore with `mv %s %s`)",
        agent,
        plan.recorded_identity or "(never stamped)",
        plan.sif_identity,
        source,
        destination,
        destination,
        source,
    )
    return destination


def reconcile_overlay_venv(
    config,
    sif_path: Path | str,
    *,
    agent_running: bool | None,
    venv: str = DEFAULT_VENV,
    now: datetime | None = None,
    inside_container_fn=None,
) -> InvalidationPlan | None:
    """Enforce the contract for one agent. Returns the plan, or ``None``.

    ``None`` means the contract does not apply to this agent — no overlay is
    declared, or the overlay is a loopback IMAGE whose upper layer is not
    host-readable and therefore not host-invalidatable.

    ``agent_running`` is a REQUIRED keyword with no default so a caller cannot
    reach the mutation without having answered it. Pass ``None`` when you did
    not measure it; the plan then refuses, which is the correct answer.
    """
    root = resolve_overlay_declaration(config)
    name = getattr(config, "name", "") or "<unknown>"
    if root is None:
        return None

    ap = getattr(config, "apptainer", None)
    overlay_size = (getattr(ap, "overlay_size", "") or "") if ap is not None else ""
    if is_image_overlay(root, overlay_size):
        logger.info(
            "overlay-venv: agent %s uses a loopback image overlay (%s); its "
            "upper layer is not host-readable, so the venv slice cannot be "
            "invalidated from here. Recreate the image overlay to reset it.",
            name,
            root,
        )
        return None

    facts = observe_overlay(
        root,
        sif_path,
        agent_running=agent_running,
        venv=venv,
        inside_container_fn=inside_container_fn,
    )
    plan = plan_invalidation(agent=name, overlay_root=str(root), facts=facts)

    if plan.action == ACTION_REFUSE:
        # Loud, and deliberately NOT followed by a stamp write — see the module
        # docstring on why a refusal must not convert itself into a pass.
        logger.warning(
            "overlay-venv: REFUSING to invalidate %s for agent %s: %s",
            root,
            name,
            "; ".join(plan.blocking_reasons()),
        )
        return plan

    if plan.action == ACTION_INVALIDATE:
        try:
            _move_aside(name, root, plan, venv, now=now)
        except OSError as exc:  # stx-allow: fallback (reason: a failed archive must log loudly and leave the stamp unwritten, never brick the launch)
            logger.error(
                "overlay-venv: could not archive %s for agent %s: %s. The stale "
                "venv slice is UNCHANGED and the stamp is NOT advanced, so the "
                "next start retries. Move it aside by hand if this persists.",
                venv_slice(root, venv),
                name,
                exc,
            )
            return plan

    write_stamp(root, plan.sif_identity)
    return plan


def reconcile_overlay_venv_for_launch(
    config,
    sif_path: Path | str,
    state_dir: Path | str,
) -> InvalidationPlan | None:
    """``build_run_argv``'s entry point. Observes liveness; NEVER raises.

    Exists so the launch site stays one call and the never-raise contract lives
    HERE, next to the reasoning for it, rather than as an anonymous ``try`` in
    the middle of argv assembly.

    A reconcile that blows up must not refuse every start on the host — that is
    a guard more dangerous than the fault it guards. It logs loudly instead,
    and the in-container boot assertion
    (:func:`._venv_dist_assertion.assert_venv_distributions_unique`) is the
    backstop that refuses to RUN in a union this failed to repair.
    """
    try:
        return reconcile_overlay_venv(
            config,
            sif_path,
            agent_running=agent_running_from_state_dir(state_dir),
        )
    except Exception as exc:  # stx-allow: fallback (reason: a bug in this rail must never refuse every start on the host; the in-container boot assertion is the backstop)
        logger.error(
            "overlay-venv: reconcile FAILED for agent %r (%s: %s). The launch "
            "proceeds unreconciled; if this agent's venv is shadowed, the "
            "in-container boot assertion will refuse it.",
            getattr(config, "name", "<unknown>"),
            type(exc).__name__,
            exc,
        )
        return None
