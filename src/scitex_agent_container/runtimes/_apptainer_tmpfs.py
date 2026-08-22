"""Sized ``/tmp`` scratch for the apptainer runtime.

Why this exists
---------------
A ``--containall`` apptainer container mounts a **64 MB tmpfs** at
``/tmp`` (governed by ``sessiondir max size`` in apptainer.conf, which
sac cannot change per-run and which is not env-overridable). Agents
running the full test suite, generating coverage XML, or using
pytest fixtures with file IO routinely fill that 64 MB tmpfs mid-run
and fail with "No space left on device" (the symptom that triggered
this feature — see the FUTURE note ``sac-container-tmpfs-size.md``).

What we do
----------
Apptainer's ``-W/--workdir <dir>`` flag relocates the in-container
``/tmp`` and ``/var/tmp`` (and ``$HOME`` under ``--contain``, but sac
pins ``$HOME`` via ``--home`` so that part is moot) onto a host
directory, replacing the tiny session tmpfs with the host filesystem's
capacity. We create a per-agent scratch dir under
``<state_dir>/tmp-scratch`` and emit ``--workdir <dir>``.

``spec.apptainer.tmpfs_size`` (default ``"2G"``) is the **minimum
free-space guarantee**: before launch we verify the host filesystem
backing the scratch dir has at least that many bytes free, and fail
loud (``TmpfsSpaceError``) if not — rather than letting the agent
discover the shortfall mid-run with an opaque ENOSPC. It is NOT a hard
cap: an unprivileged apptainer user cannot mount a size-capped tmpfs
(``--mount type=tmpfs`` is rejected by apptainer 1.4 — only ``bind`` is
supported), so the honest contract is "at least this much room",
backed by the host disk. Setting ``tmpfs_size: ""`` opts out entirely
(no ``--workdir``; the legacy 64 MB tmpfs is used).

Compatibility
-------------
``--workdir`` composes cleanly with the hardened default flags
(``--containall --cleanenv --writable-tmpfs --home /home/agent``) and
with PR #183's overlay upper-home bind — verified end-to-end. When the
operator already declares their own ``-W``/``--workdir`` in
``apptainer.raw_args`` (relaxed escape-hatch), sac skips its own to
avoid a duplicate/conflicting flag.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

# apptainer-style size string: integer + M/MB/G/GB (K/KB rejected —
# sub-MB scratch makes no sense for the test-suite workload this serves).
# Mirrors the overlay_size grammar in _apptainer_build._create_overlay_image.
_SIZE_RE = re.compile(r"^\s*(\d+)\s*([MG]B?)\s*$", re.IGNORECASE)
_UNIT_BYTES = {"M": 1024**2, "MB": 1024**2, "G": 1024**3, "GB": 1024**3}


class TmpfsSpaceError(RuntimeError):
    """Host filesystem backing the scratch dir has less free space than
    ``spec.apptainer.tmpfs_size`` requires."""


def parse_tmpfs_size_bytes(size: str) -> int:
    """Parse an apptainer-style size string into bytes.

    Accepts ``"2G"``, ``"512M"``, ``"2048MB"`` etc. (units M/MB/G/GB
    only). Raises ``ValueError`` for unparseable / sub-MB / unsupported
    sizes — fail loud rather than silently coercing to a default.
    """
    m = _SIZE_RE.match(size)
    if not m:
        raise ValueError(
            f"tmpfs_size {size!r} unparseable; use '2G', '512M', '2048MB' "
            "etc. (units M/MB/G/GB only — K/KB rejected because sub-MB "
            "scratch makes no sense for this workload)."
        )
    n = int(m.group(1))
    unit = m.group(2).upper()
    nbytes = n * _UNIT_BYTES[unit]
    if nbytes < _UNIT_BYTES["M"]:
        raise ValueError(f"tmpfs_size {size!r} resolves to <1MB")
    return nbytes


def tmpfs_workdir_flags(config, state_dir: Path) -> list[str]:
    """Return ``["--workdir", <dir>]`` to give the container a larger
    ``/tmp`` than the default 64 MB session tmpfs, or ``[]``.

    Returns ``[]`` (no-op) when:
      * ``spec.apptainer.tmpfs_size`` is empty (operator opt-out), OR
      * the operator already declared ``-W``/``--workdir`` in
        ``apptainer.raw_args`` (relaxed escape-hatch — don't duplicate).

    Creates the scratch dir under ``<state_dir>/tmp-scratch``.

    Does NOT check free space — that is :func:`verify_tmpfs_headroom`,
    called on the real launch path only. This function is reached by
    ``sac agents explain`` and by ``run(dry_run=True)``, which start
    nothing and must not fail on a host condition they do not depend on.
    """
    resolved = _resolve_scratch(config, state_dir)
    if resolved is None:
        return []

    scratch, _size, _need_bytes = resolved
    scratch.mkdir(parents=True, exist_ok=True)
    return ["--workdir", str(scratch)]


def _resolve_scratch(config, state_dir: Path) -> tuple[Path, str, int] | None:
    """Resolve ``(scratch_dir, size_str, need_bytes)``, or ``None`` when sac
    does not manage this agent's workdir (operator opt-out, or an explicit
    ``-W``/``--workdir`` in ``raw_args``).

    Shared by :func:`tmpfs_workdir_flags` and :func:`verify_tmpfs_headroom`
    so the two can never disagree about WHICH directory is being sized —
    the flag and the check must describe the same path or the guarantee is
    about a directory the container does not use.

    ``parse_tmpfs_size_bytes`` is called HERE, so an unparseable
    ``tmpfs_size`` still fails loud at argv-construction time. That is
    deliberate: a bad size string is a CONFIG error, present on every host
    regardless of disk, and it should surface the moment the spec is read.
    Only the free-space check — a RESOURCE condition, true on one host at
    one moment — belongs at launch.
    """
    ap = getattr(config, "apptainer", None)
    if ap is None:
        # No apptainer block → use the dataclass default ("2G") so a
        # bare ``runtime: apptainer`` agent still gets the larger /tmp.
        size = "2G"
        raw_args: list[str] = []
    else:
        size = getattr(ap, "tmpfs_size", "2G")
        raw_args = list(getattr(ap, "raw_args", None) or [])

    if not size:
        return None

    # Operator already pinned a workdir → respect it, emit nothing.
    if any(a in ("-W", "--workdir") for a in raw_args):
        return None

    return state_dir.expanduser() / "tmp-scratch", size, parse_tmpfs_size_bytes(size)


def verify_tmpfs_headroom(config, state_dir: Path) -> None:
    """Raise :class:`TmpfsSpaceError` unless the filesystem backing the
    scratch dir has at least ``spec.apptainer.tmpfs_size`` bytes free.

    CALL THIS ONLY ON A REAL LAUNCH PATH. It asks a question about the host
    RIGHT NOW, so it is only meaningful immediately before starting a
    container — and it is actively wrong anywhere else.

    Why it is not in ``tmpfs_workdir_flags`` (which is where it used to
    live): that function is also reached by ``sac agents explain`` and by
    ``run(dry_run=True)``, neither of which starts anything. A full disk
    therefore made two READ-ONLY commands fail — so an operator diagnosing
    a full host could not run ``explain`` at exactly the moment it was
    most useful. It also wired ~21 argv-building test files' verdicts to
    ambient free disk they neither control nor test, which on a 91%-full
    shared CI runner (2026-08-19) produced failures carrying unrelated
    TEST NAMES and sent readers to their own diffs for hours.

    This mirrors the placement ``_apptainer_runtime`` already chose for
    ``reconcile_overlay_venv_for_launch`` — past the ``dry_run`` return,
    for the same stated reason: a read-only command must not do
    launch-time work.
    """
    resolved = _resolve_scratch(config, state_dir)
    if resolved is None:
        return

    scratch, size, need_bytes = resolved
    scratch.mkdir(parents=True, exist_ok=True)

    usage = shutil.disk_usage(scratch)
    if usage.free < need_bytes:
        raise TmpfsSpaceError(
            f"apptainer.tmpfs_size requests {size} "
            f"({need_bytes} bytes) of /tmp headroom but the filesystem "
            f"backing {scratch} has only {usage.free} bytes free. "
            "Free space, point spec.workdir at a roomier filesystem, or "
            "lower spec.apptainer.tmpfs_size."
        )


__all__ = [
    "tmpfs_workdir_flags",
    "verify_tmpfs_headroom",
    "parse_tmpfs_size_bytes",
    "TmpfsSpaceError",
]
