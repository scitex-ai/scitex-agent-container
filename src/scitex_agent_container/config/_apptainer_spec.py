"""``ApptainerSpec`` dataclass — extracted from :mod:`config._types`.

F-CS18 — apptainer-specific extension hook.

Apptainer reads OCI images natively (`apptainer build sif docker://...`),
so for the no-extras case spec.image alone is enough — sac just
`apptainer build`s the SIF and runs it. For HPC-specific layering
(extra pip packages, system libs, env vars), the operator can either:

  * declare `spec.apptainer.post` — sac synthesises a `.def` with
    `Bootstrap: docker` + `%post` + `%environment` and builds from it.
  * declare `spec.apptainer.def_file` — sac runs `apptainer build`
    against the operator's hand-written `.def` (full control).

All fields are optional; an `apptainer:` block with no fields set is
equivalent to none at all.

Pulled out of ``_types.py`` so that module stays under sac's 512-line
per-file cap. Re-exported from ``config._types`` for back-compat — any
``from ...config._types import ApptainerSpec`` keeps resolving.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ApptainerSpec:
    """Apptainer-specific image-build extensions (F-CS18)."""

    # v3-realign: apptainer-engine-scoped knobs promoted from top-level.
    image: str = ""
    """SIF path or docker:// URL — promoted from top-level spec.image (§3).
    Empty = fall back to the default sac-scitex SIF."""

    binds: list[str] = field(default_factory=list)
    """Bind mounts as ``host:container[:mode]`` strings — promoted from
    top-level spec.mounts (§3)."""

    env: dict[str, str] = field(default_factory=dict)
    """Env vars exported into the container — promoted from top-level
    spec.env (§3)."""

    raw_args: list[str] = field(default_factory=list)
    """v3 escape hatch (§1 invariant): appended verbatim to the
    ``apptainer exec`` argv after all curated args. Lets operators bolt
    on flags sac doesn't model."""

    container_workdir: str = "/work"
    """Path inside the container where ``spec.workdir`` gets bind-mounted
    (and where the runner's ``--pwd`` lands). Default ``/work``.
    Override when the SIF expects a different mount point (e.g. a
    pre-baked ``WORKDIR`` in the .def file)."""

    post: str = ""
    """Shell snippet run inside the SIF build (apptainer's `%post`).
    Lines are concatenated verbatim. Empty = no extension."""

    environment: dict = field(default_factory=dict)
    """Env vars baked into the SIF (apptainer's `%environment`). Same
    shape as ``spec.env`` — KEY: VALUE pairs."""

    def_file: str = ""
    """Path to a hand-authored ``.def`` file (apptainer's native
    build language). Mutually exclusive with `post`/`environment`:
    when set, sac uses this file verbatim and ignores `post`."""

    nv: bool = False
    """Forward host NVIDIA driver/libs into the container (apptainer's
    ``--nv``). Required for CUDA workloads on GPU nodes; harmless on
    CPU-only hosts but only set when needed."""

    rocm: bool = False
    """Forward host AMD ROCm libs (apptainer's ``--rocm``). Mutually
    exclusive with ``nv`` in practice (no host has both)."""

    overlay: str = ""
    """Writable apptainer overlay image (``--overlay <file>``). Empty =
    no overlay (tmpfs writable layer). Non-absolute paths resolve
    against ``spec.workdir``. See ``docs/isolation.md`` §7."""

    overlay_size: str = ""
    """When set together with ``overlay``, sac auto-creates the overlay
    image with the given size if it doesn't exist before launching.
    Accepts apptainer-style sizes with units M/MB/G/GB only (e.g.
    ``"5G"``, ``"500M"``, ``"1024MB"``). K/KB are explicitly rejected —
    apptainer's ``overlay create --size`` takes integer MB so sub-MB
    granularity makes no sense. Empty = no auto-create (default;
    missing overlay raises FileNotFoundError at launch with a clear
    message). See ``docs/isolation.md`` §7."""

    overlay_create_if_missing: bool = True
    """When True (default) AND ``overlay_size`` is set AND the overlay
    path does not exist, sac runs ``apptainer overlay create --size
    <MB> <path>`` before launching. When False, sac never creates
    overlays even if size is given (operator must pre-create — sac
    raises FileNotFoundError instead). See ``docs/isolation.md`` §7."""

    tmpfs_size: str = "2G"
    """Minimum free-space guarantee for the container's ``/tmp`` (and
    ``/var/tmp``). A ``--containall`` apptainer container otherwise gets
    a 64 MB session tmpfs at ``/tmp`` — too small for the full test
    suite, coverage XML generation, or pytest fixtures with file IO,
    which fill it mid-run with "No space left on device".

    sac emits ``--workdir <state_dir>/tmp-scratch`` to relocate ``/tmp``
    onto the host filesystem (capacity >> 64 MB) and verifies that
    filesystem has at least this many bytes free before launch, failing
    loud otherwise. Accepts apptainer-style sizes with units M/MB/G/GB
    only (e.g. ``"2G"``, ``"512M"``, ``"2048MB"``); K/KB rejected.

    NOT a hard cap — an unprivileged apptainer user cannot mount a
    size-capped tmpfs (``--mount type=tmpfs`` is unsupported), so the
    contract is "at least this much room", backed by host disk. Set to
    ``""`` to opt out entirely (legacy 64 MB tmpfs). See
    ``runtimes/_apptainer_tmpfs.py``."""

    relaxed: bool = False
    """Opt out of sac's hardened defaults (auto-prepended
    ``--containall``/``--cleanenv``/``--writable-tmpfs``/``--home``).
    See ``docs/isolation.md``."""

    fakeroot: bool = False
    """Apptainer ``--fakeroot`` — uid 0 inside via user-namespace
    remapping; operator uid on host. Pairs with the D5 preflight's
    ``/proc/self/uid_map`` detection (see ``docs/isolation.md``)."""

    jail: bool = False
    """Opt in to the JAILED-capsule mount-boundary assert (security
    guardrail — ``runtimes/_apptainer_jail.py``). When true (or when the
    spec resolves to the ``solver`` named group, which turns this on
    automatically and non-bypassably), the apptainer launch layer FORCES
    ``--containall`` and REFUSES to launch if any operator-controlled bind
    source (``binds`` / ``raw_args`` ``--bind`` / ``APPTAINER_BIND*`` env)
    or the ``--pwd`` realpath-resolves under a forbidden shared-filesystem
    prefix (``/data/gpfs`` / ``/data/scratch`` / ``/home``). Fail-loud,
    before exec; the spec cannot opt it back off for a solver."""

    nested_build: bool = False
    """Enable NESTED apptainer build/pull from inside the agent container
    (``runtimes/_apptainer_nested.py``): binds ``/dev/fuse``, masks
    ``/etc/subuid``+``/etc/subgid`` (→ root-mapped + ``fakeroot``-command
    build path, no setuid ``newuidmap`` needed), and points
    ``APPTAINER_TMPDIR``/``CACHEDIR`` at the real-disk ``/tmp``. Lets a
    solver reproduce a capsule's pinned env — pull a published
    ``docker://`` image, or build a Dockerfile-derived def whose ``%post``
    runs as root — then exec it. Composes with ``access: capsule`` (adds
    no host-FS bind). Size the build scratch via ``tmpfs_size`` (2G default
    is too small for a multi-GB image). Verified 2026-06-20 in
    sac-scitex.sif."""


__all__ = ["ApptainerSpec"]
