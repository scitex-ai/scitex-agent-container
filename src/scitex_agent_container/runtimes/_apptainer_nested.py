"""Nested apptainer build/pull from inside the agent container.

Pulled out of ``build_run_argv`` so the per-flag logic lives in one place
and the runtime file stays under sac's 512-line cap (mirrors
``_apptainer_iso_flags`` / ``_apptainer_tmpfs``).

Why
---
A solver agent should reproduce a research capsule's *pinned* environment
itself — instead of the harness pre-building it (which babysits the agent
and risks overfitting). CoreBench capsules ship a CodeOcean **published
image** that is anonymously pullable (``docker://registry.codeocean.com/
published/<uuid>:v1``); other capsules ship a ``Dockerfile``. With
``spec.apptainer.nested_build: true`` the agent can, from *inside* its own
SAC apptainer container, run::

    apptainer build cap.sif docker://.../published/<uuid>:v1     # pull
    # or, from a Dockerfile-derived def whose %post runs as root:
    apptainer build cap.sif capsule.def                          # build
    apptainer exec --bind data:/data --bind code:/code cap.sif bash -lc 'cd /code && bash run'

What this emits (all verified 2026-06-20 inside ``sac-scitex.sif``)
-------------------------------------------------------------------
* ``--bind /dev/fuse`` — ``--containall`` omits ``/dev/fuse``, but the
  squashfuse mount of a pulled/built SIF needs it. **Fail-loud** if the
  host lacks ``/dev/fuse`` rather than emitting a bind that FATALs at exec.
* empty-file binds over ``/etc/subuid`` + ``/etc/subgid`` — the SIF's
  ``newuidmap`` is owned by ``agent`` (not root), so the usual
  ``--fakeroot`` uid-mapping path FATALs ("newuidmap must be owned by the
  root user"). Masking subuid drops the agent user out of ``/etc/subuid``,
  which makes apptainer fall back to the **root-mapped namespace +
  ``fakeroot`` command** path ("User not listed in /etc/subuid, trying
  root-mapped namespace") — no setuid ``newuidmap`` needed. ``%post`` /
  ``RUN`` steps then execute as (faked) root unprivileged.
* ``APPTAINER_TMPDIR=/tmp`` + ``APPTAINER_CACHEDIR=/tmp/.apptainer-cache``
  — build scratch + OCI-layer cache on the **real-disk** ``/tmp`` (which
  ``tmpfs_workdir_flags`` relocates onto ``<state_dir>/tmp-scratch``). Must
  be ``/tmp`` (not a bespoke path like ``/opt/tmp``): apptainer propagates
  the outer binds into the ``%post`` sandbox, and a path the build base
  lacks FATALs "destination doesn't exist in container". Size the scratch
  with ``spec.apptainer.tmpfs_size`` (the 2G default is too small for a
  multi-GB capsule image — set e.g. ``"20G"``).

Security / scope
----------------
Adds **no host-FS bind** — only a device (``/dev/fuse``), two empty-file
masks, and env. So it composes with ``access: capsule``: the leak-safety
invariant (no host home, no sibling capsules, no answer key) is preserved.
Masking subuid yields a uid→root map **inside the build sandbox only**
(standard unprivileged-build mode), never host privilege.

Caveat
------
The subuid-mask bind requires the **build base image** to contain
``/etc/subuid`` (every real distro base — debian/ubuntu/miniconda/python —
does; ultra-minimal bases like ``busybox`` do not). A *pull* of a published
image has no ``%post`` and is unaffected.
"""

from __future__ import annotations

from pathlib import Path


def nested_build_flags(config, state_dir: Path) -> list[str]:
    """Return the nested-build enabling flags, or ``[]`` when off.

    No-op unless ``spec.apptainer.nested_build`` is true. Raises
    :class:`FileNotFoundError` (fail-loud) when nested_build is requested
    but the host has no ``/dev/fuse``.
    """
    ap = getattr(config, "apptainer", None)
    if ap is None or not getattr(ap, "nested_build", False):
        return []

    # /dev/fuse is required for the squashfuse mount of the pulled/built
    # SIF. Fail loud at argv-build time rather than emit a bind apptainer
    # rejects at exec ("/dev/fuse: no such file or directory").
    if not Path("/dev/fuse").exists():
        raise FileNotFoundError(
            "spec.apptainer.nested_build requires /dev/fuse on the host "
            "(squashfuse mount of the pulled/built SIF), but /dev/fuse is "
            "absent. Install the host 'fuse'/'fuse3' package, or unset "
            "spec.apptainer.nested_build."
        )

    # Empty file to mask /etc/subuid + /etc/subgid → root-mapped namespace
    # + fakeroot-command build path (no setuid newuidmap). Lives in the
    # per-agent state dir so it never collides across agents.
    nb_dir = state_dir.expanduser() / "nested-build"
    nb_dir.mkdir(parents=True, exist_ok=True)
    empty = nb_dir / "empty"
    if not empty.exists():
        empty.touch()

    return [
        "--bind",
        "/dev/fuse",
        "--bind",
        f"{empty}:/etc/subuid",
        "--bind",
        f"{empty}:/etc/subgid",
        # APPTAINER_* read by the agent's NESTED apptainer; the real-disk
        # /tmp comes from --workdir (tmpfs_workdir_flags). Emitted before
        # spec.env so an operator can still override.
        "--env",
        "APPTAINER_TMPDIR=/tmp",
        "--env",
        "APPTAINER_CACHEDIR=/tmp/.apptainer-cache",
    ]


__all__ = ["nested_build_flags"]
