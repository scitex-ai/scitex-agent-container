"""Idempotent provisioning of an agent's apptainer DIRECTORY overlay.

Root cause this module closes (2026-07-13): creating + starting a BRAND-NEW
agent died in the ``container_creation`` phase with a raw apptainer FATAL::

    FATAL: while loading overlay images: failed to open overlay image
    <...>/containers/overlays/<agent>/: failed to retrieve path for <...>:
    lstat <...>: no such file or directory

Apptainer's directory-overlay contract: it creates the ``upper/`` + ``work/``
subdirectories itself, but it ``lstat()``s the overlay ROOT and refuses to
create that — a missing root is a hard, non-recoverable FATAL.

Until now NOTHING in sac ensured the root existed. The fleet's overlays came
into being only as an INCIDENTAL side-effect of
:func:`._to_home_overlay.deploy_to_home_overlay`, whose
``<overlay>/upper/<container_home>/`` ``mkdir(parents=True)`` happens to create
the root on the way down. That side-effect is gated on
:func:`._to_home_overlay._resolve_overlay_dir`, which recognises an overlay
only as ``spec.apptainer.overlay`` or as the SPACE-SEPARATED raw_args pair
``["--overlay", "<path>"]``. Every fleet spec authored by hand happened to use
that spelling, so the accident held.

An agent scaffolded from the ``_template_python_developer`` dir-template
instead declares the ``=``-JOINED spelling ``--overlay=<path>`` — which
apptainer accepts identically. sac's resolver saw nothing, the mkdir
side-effect never fired, the overlay root was never created, and the launch
was stillborn.

This module makes the precondition EXPLICIT, form-agnostic and idempotent:
:func:`ensure_overlay_dirs` is called from ``build_run_argv`` — the single
choke point EVERY apptainer launch passes through (SDK runner and TUI alike) —
and creates ``<overlay>/``, ``<overlay>/upper/`` and ``<overlay>/work/`` with
the same layout and permissions the live fleet's overlays carry. It reads BOTH
raw_args spellings, so no future spec shape can silently regress to a
stillborn start.

Scope: DIRECTORY overlays only. A sized loopback IMAGE overlay
(``spec.apptainer.overlay_size``, or an ``.img`` / ``.ext3`` / ``.sif`` path)
remains the business of :func:`._apptainer_build._create_overlay_image` —
``mkdir``-ing a directory at an image path would break that path outright.

Fail-loud: an overlay we cannot create raises :class:`OverlayProvisionError`
naming the exact path and the exact command to create it by hand, rather than
letting apptainer die with a FATAL that sac's lifecycle classifier can only
label ``container_creation_unknown``.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# Apptainer's directory-overlay layout: the writable layer lives in ``upper/``
# and the overlayfs scratch area in ``work/`` (``apptainer overlay create``).
OVERLAY_UPPER_DIRNAME = "upper"
OVERLAY_WORK_DIRNAME = "work"

# Permissions the live fleet's overlays actually carry — verified on
# scitex-scholar / figrecipe / scitex-clew (root, upper and work are all
# 0755). Provisioning to the same mode keeps a sac-created overlay
# indistinguishable from the ones apptainer created in place.
OVERLAY_DIR_MODE = 0o755

# Path suffixes that mark a loopback IMAGE overlay — never a directory.
_IMAGE_SUFFIXES = (".img", ".ext3", ".sif")

# The apptainer flag whose value is the overlay path.
_OVERLAY_FLAG = "--overlay"


class OverlayProvisionError(RuntimeError):
    """A declared directory overlay could not be provisioned.

    Carries the offending path and the exact remediation command — the
    constitution's fail-fast/fail-loud/actionable-hint contract. Raised
    from :func:`ensure_overlay_dirs` before ``apptainer exec`` is ever
    reached, so the operator sees THIS instead of a raw apptainer FATAL.
    """


def raw_arg_value(raw_args, flag: str) -> str:
    """Return the value of ``flag`` in an apptainer argv list.

    Handles BOTH spellings apptainer itself accepts:

    * space-separated — ``["--overlay", "/path"]``
    * ``=``-joined    — ``["--overlay=/path"]``

    Returns ``""`` when the flag is absent or carries no value. sac read only
    the first spelling for years; the second silently resolved to "no overlay
    declared" and cost the fleet a stillborn agent (see the module docstring).
    First occurrence wins, mirroring a left-to-right argv scan.
    """
    raw = [str(a) for a in (raw_args or [])]
    prefix = f"{flag}="
    for i, arg in enumerate(raw):
        if arg == flag:
            return raw[i + 1].strip() if i + 1 < len(raw) else ""
        if arg.startswith(prefix):
            return arg[len(prefix) :].strip()
    return ""


def resolve_overlay_declaration(config) -> Path | None:
    """Resolve the overlay path apptainer will actually receive, or ``None``.

    Form-agnostic — mirrors apptainer's own argv parsing:

      1. ``spec.apptainer.overlay`` (the modeled field), else
      2. ``--overlay <path>`` / ``--overlay=<path>`` in
         ``spec.apptainer.raw_args`` (the escape hatch relaxed specs use).

    Relative paths resolve against ``spec.workdir``, matching how
    ``build_run_argv`` resolves the modeled field. Returns ``None`` when no
    overlay is declared at all.

    Distinct from :func:`._to_home_overlay._resolve_overlay_dir`, which answers
    a different question — "does sac back the container ``$HOME`` with this
    overlay's upper layer?" — and is deliberately narrower (see that module).
    THIS function answers "what path does apptainer get?", so it must accept
    every spelling apptainer does.
    """
    ap = getattr(config, "apptainer", None)
    if ap is None:
        return None
    declared = (getattr(ap, "overlay", "") or "").strip()
    if not declared:
        declared = raw_arg_value(getattr(ap, "raw_args", None), _OVERLAY_FLAG)
    if not declared:
        return None
    path = Path(declared).expanduser()
    if not path.is_absolute():
        workdir = (getattr(config, "workdir", "") or "").strip()
        if not workdir:
            return None
        path = Path(workdir).expanduser() / path
    return path


def is_image_overlay(path: Path, overlay_size: str = "") -> bool:
    """True iff ``path`` denotes a loopback IMAGE overlay, not a directory.

    An image overlay is built by ``apptainer overlay create --size`` (see
    :func:`._apptainer_build._create_overlay_image`) and must NEVER be
    ``mkdir``-ed — doing so would shadow the image path with a directory and
    break that auto-create path. Three independent signals, any one decisive:

      * the path exists and is NOT a directory (a real image file already),
      * ``spec.apptainer.overlay_size`` is set — the explicit "this is a sized
        image, auto-create it" contract, or
      * the path carries an image suffix (``.img`` / ``.ext3`` / ``.sif``).
    """
    if path.exists() and not path.is_dir():
        return True
    if (overlay_size or "").strip():
        return True
    return path.suffix.lower() in _IMAGE_SUFFIXES


def ensure_overlay_dirs(config) -> Path | None:
    """Idempotently provision the agent's DIRECTORY overlay; return its root.

    Creates ``<overlay>/``, ``<overlay>/upper/`` and ``<overlay>/work/`` when
    missing. A fully-provisioned overlay is left completely untouched — no
    ``chmod`` of an existing directory, so an operator (or the agent itself)
    may legitimately have tightened it. Returns ``None`` when the spec declares
    no overlay, or declares an IMAGE overlay (not ours to create).

    Called from ``build_run_argv`` so EVERY apptainer launch establishes the
    precondition immediately before ``apptainer exec``, regardless of runtime
    (SDK / TUI) and regardless of which spelling the spec used.

    Raises:
        OverlayProvisionError: the overlay could not be created — names the
            exact path and the exact ``mkdir -p`` command that fixes it.
    """
    root = resolve_overlay_declaration(config)
    if root is None:
        return None

    ap = getattr(config, "apptainer", None)
    overlay_size = (getattr(ap, "overlay_size", "") or "") if ap is not None else ""
    if is_image_overlay(root, overlay_size):
        return None

    upper = root / OVERLAY_UPPER_DIRNAME
    work = root / OVERLAY_WORK_DIRNAME
    name = getattr(config, "name", "") or "<unknown>"

    created: list[str] = []
    try:
        for target in (root, upper, work):
            if target.is_dir():
                continue
            target.mkdir(parents=True, exist_ok=True)
            os.chmod(target, OVERLAY_DIR_MODE)
            created.append(str(target))
    except OSError as exc:
        raise OverlayProvisionError(
            f"apptainer overlay {root} could not be provisioned ({exc}). "
            f"Agent {name!r} declares this DIRECTORY overlay, and apptainer "
            "FATALs at container creation ('failed to open overlay image ... "
            "no such file or directory') unless the directory EXISTS before "
            "`apptainer exec`. Fix ONE of: (1) create it by hand — "
            f"`mkdir -p {upper} {work}` — or (2) repoint the overlay "
            "(spec.apptainer.overlay, or the --overlay raw_arg) at a path "
            "this user can write."
        ) from exc

    if created:
        logger.info(
            "overlay: provisioned directory overlay %s for agent %s (created: %s)",
            root,
            name,
            ", ".join(created),
        )
    return root


def overlay_flags(config) -> list[str]:
    """Return the curated ``--overlay`` argv flags for the MODELED spec field.

    Extracted verbatim from ``build_run_argv`` (which crossed the 512-line cap)
    so that every overlay concern — path resolution, directory provisioning,
    sized-image auto-create and argv emission — lives in ONE module. Mirrors
    the sibling ``tmpfs_workdir_flags`` / ``nested_build_flags`` / ``auth_argv``
    extraction pattern that ``build_run_argv`` already follows.

    A writable overlay lets the agent install packages, write caches and
    persist state while the base SIF stays immutable. Resolution: an absolute
    path is used as-is; a relative path resolves against ``spec.workdir``.

    Declarative auto-create (see ``docs/isolation.md`` §7): for a sized IMAGE
    overlay, ``spec.apptainer.overlay_size`` plus the default
    ``overlay_create_if_missing=True`` drives ``apptainer overlay create
    --size <MB> <path>`` when the image file is missing. Without
    ``overlay_size`` we fail loudly (``FileNotFoundError``) rather than let
    apptainer error cryptically at exec time.

    DIRECTORY overlays never reach the missing-path branch: they are
    provisioned up-front by :func:`ensure_overlay_dirs`, which
    ``build_run_argv`` calls before it assembles any flags.

    Returns ``[]`` when ``spec.apptainer.overlay`` is unset — a raw_args
    overlay (``--overlay`` / ``--overlay=``) is passed through verbatim by
    ``build_run_argv`` and needs no curated flag here.
    """
    ap = getattr(config, "apptainer", None)
    if ap is None:
        return []
    overlay = getattr(ap, "overlay", "") or ""
    if not overlay:
        return []

    overlay_p = Path(overlay).expanduser()
    if not overlay_p.is_absolute():
        overlay_p = Path(config.workdir).expanduser() / overlay_p

    if not overlay_p.exists():
        overlay_size = getattr(ap, "overlay_size", "") or ""
        create_ok = getattr(ap, "overlay_create_if_missing", True)
        if overlay_size and create_ok:
            from ._apptainer_build import _create_overlay_image

            _create_overlay_image(overlay_p, overlay_size)
        elif overlay_size:
            raise FileNotFoundError(
                f"overlay {overlay_p} missing and "
                "overlay_create_if_missing=false; pre-create with "
                "`apptainer overlay create --size <MB> <path>` or "
                "flip overlay_create_if_missing back to true."
            )
        else:
            raise FileNotFoundError(
                f"overlay {overlay_p} missing; set "
                "spec.apptainer.overlay_size (e.g. '5G') for "
                "declarative auto-create, or pre-create with "
                "`apptainer overlay create`."
            )
    return ["--overlay", str(overlay_p)]


__all__ = [
    "OVERLAY_DIR_MODE",
    "OVERLAY_UPPER_DIRNAME",
    "OVERLAY_WORK_DIRNAME",
    "OverlayProvisionError",
    "ensure_overlay_dirs",
    "is_image_overlay",
    "overlay_flags",
    "raw_arg_value",
    "resolve_overlay_declaration",
]

# EOF
