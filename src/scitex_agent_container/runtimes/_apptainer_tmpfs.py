"""Sized ``/tmp`` scratch + ``TMPDIR=/opt/tmp`` redirect for apptainer.

Why this exists
---------------
A ``--containall`` apptainer container mounts a **64 MB tmpfs** at
``/tmp`` (governed by ``sessiondir max size`` in apptainer.conf, which
sac cannot change per-run and which is not env-overridable). Agents
running the full test suite, generating coverage XML, or using
pytest fixtures with file IO routinely fill that 64 MB tmpfs mid-run
and fail with "No space left on device" (the symptom that triggered
this feature — see the FUTURE note ``sac-container-tmpfs-size.md``).

Heavy ML capsules (TF/Keras tokenizer caches, BERT preprocessing) hit
the SAME wall through a slightly different path: ``tempfile.gettempdir()``
honours ``$TMPDIR`` before falling through to ``/tmp``. If the workload
writes a multi-GB dataset to its tempdir under ``--writable-tmpfs``,
the writable-tmpfs overlay (also a small in-memory tmpfs) fills and
the process wedges silently — the symptom proj-paper-scitex-clew hit
on cohort-A capsule-0238624 (a2a ``d346a786``, 2026-06-07).

What we do
----------
TWO independent measures, both default-on:

1. **``--workdir <state_dir>/tmp-scratch``** (function
   :func:`tmpfs_workdir_flags`). Apptainer's ``-W/--workdir <dir>``
   flag relocates the in-container ``/tmp`` and ``/var/tmp`` onto a
   host directory, replacing the tiny session tmpfs with the host
   filesystem's capacity. ``spec.apptainer.tmpfs_size`` (default
   ``"2G"``) is the minimum free-space guarantee: before launch we
   verify the host filesystem backing the scratch dir has at least
   that many bytes free, raising :class:`TmpfsSpaceError` if not.

2. **``--bind <state_dir>/opt-tmp:/opt/tmp`` + ``--env TMPDIR=/opt/tmp``**
   (function :func:`opt_tmp_flags`). Apps that write to ``$TMPDIR``
   instead of literal ``/tmp`` (Python's ``tempfile``, sqlite3
   wal-shm, TF/Keras caches) land on the bound per-agent host
   scratch dir rather than the writable-tmpfs overlay. Per #50
   lead-decision option 4 (a2a ``7d14d69b``, 2026-06-07), confirmed
   live by proj-paper-scitex-clew capsule-0238624 (submission.json
   T+7min after the TMPDIR=/opt/tmp fix landed). No host-/tmp leak
   (per-agent scratch dir, not host ``/tmp``).

Both measures key on ``spec.apptainer.tmpfs_size`` — setting it to
``""`` opts out of BOTH (legacy 64 MB tmpfs everywhere). Operator
escape hatches (their own ``--workdir`` in ``raw_args``; their own
``TMPDIR`` in ``apptainer.env``; their own bind to ``/opt/tmp``) all
win — sac never overrides an explicit operator choice.

Compatibility
-------------
Both flag sets compose cleanly with the hardened default flags
(``--containall --cleanenv --writable-tmpfs --home /home/agent``) and
with PR #183's overlay upper-home bind — verified end-to-end.
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


def opt_tmp_flags(config, state_dir: Path) -> list[str]:
    """Return ``--bind`` + ``--env`` flags to redirect ``$TMPDIR`` onto
    a per-agent host scratch dir at ``/opt/tmp`` inside the container.

    Default emission (under the default ``ApptainerSpec``):

    .. code-block:: text

        --bind <state_dir>/opt-tmp:/opt/tmp --env TMPDIR=/opt/tmp

    The bind makes ``/opt/tmp`` inside the container point at a host
    directory (so writes hit real disk, not the writable-tmpfs overlay
    or the 64 MB session tmpfs). The ``TMPDIR`` env redirects Python's
    ``tempfile.gettempdir()``, sqlite3 wal-shm files, and most C-lib
    tempdir lookups to that path. Together these stop heavy ML
    capsules (TF/Keras tokenizer caches, BERT preprocessing, multi-GB
    pickles) from wedging on a small in-memory tmpfs.

    Per #50 lead-decision option 4 (a2a ``7d14d69b``, 2026-06-07);
    confirmed live by proj-paper-scitex-clew capsule-0238624
    (a2a ``d346a786``).

    Returns ``[]`` (no-op) when:
      * ``spec.apptainer.tmpfs_size`` is empty (operator opt-out,
        consistent with :func:`tmpfs_workdir_flags`).

    Operator overrides are respected:
      * If ``spec.apptainer.env.TMPDIR`` is set (any value), sac does
        not append its own ``--env TMPDIR=...`` (last-wins would have
        won, but emitting our own would be wasted argv noise and
        confusing in logs).
      * If ``spec.apptainer.binds`` already includes any entry whose
        container side is ``/opt/tmp``, sac skips its own bind to
        avoid a duplicate-mount error.

    Creates ``<state_dir>/opt-tmp`` if it doesn't exist (mode 0o700
    via apptainer's default umask). Does NOT preflight free space —
    the user-facing knob for that is ``spec.apptainer.tmpfs_size``,
    which :func:`tmpfs_workdir_flags` already polices.
    """
    ap = getattr(config, "apptainer", None)
    if ap is None:
        size = "2G"
        env: dict = {}
        binds: list = []
    else:
        size = getattr(ap, "tmpfs_size", "2G")
        env = dict(getattr(ap, "env", None) or {})
        binds = list(getattr(ap, "binds", None) or [])

    if not size:
        # Operator opted out of sac's tmpfs management; don't
        # surreptitiously inject /opt/tmp bind+env either.
        return []

    flags: list[str] = []

    # Skip the bind if the operator already mapped something to
    # /opt/tmp. The container side is the LAST colon-separated field
    # (possibly followed by an optional :ro/:rw mode), so we check
    # whether any bind entry mentions ``:/opt/tmp`` as its container
    # path. This is a substring check by design — bind grammar is
    # ``host:container[:mode]``, container paths never contain colons,
    # so ``:/opt/tmp`` (or ``:/opt/tmp:``) is unambiguous.
    operator_owns_opt_tmp_bind = any(
        ":/opt/tmp" in b and (b.endswith(":/opt/tmp") or ":/opt/tmp:" in b)
        for b in binds
    )
    if not operator_owns_opt_tmp_bind:
        opt_scratch = state_dir.expanduser() / "opt-tmp"
        opt_scratch.mkdir(parents=True, exist_ok=True)
        flags += ["--bind", f"{opt_scratch}:/opt/tmp"]

    # Skip the TMPDIR env if the operator already set it (any value).
    if "TMPDIR" not in env:
        flags += ["--env", "TMPDIR=/opt/tmp"]

    return flags


__all__ = [
    "tmpfs_workdir_flags",
    "opt_tmp_flags",
    "parse_tmpfs_size_bytes",
    "TmpfsSpaceError",
]
