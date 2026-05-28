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

    Creates the scratch dir under ``<state_dir>/tmp-scratch`` and
    verifies the backing filesystem has at least ``tmpfs_size`` bytes
    free, raising :class:`TmpfsSpaceError` if not.
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
        return []

    # Operator already pinned a workdir → respect it, emit nothing.
    if any(a in ("-W", "--workdir") for a in raw_args):
        return []

    need_bytes = parse_tmpfs_size_bytes(size)

    scratch = state_dir.expanduser() / "tmp-scratch"
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

    return ["--workdir", str(scratch)]


__all__ = ["tmpfs_workdir_flags", "parse_tmpfs_size_bytes", "TmpfsSpaceError"]
