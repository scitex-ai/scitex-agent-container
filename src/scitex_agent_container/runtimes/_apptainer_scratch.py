"""Bind ``/uvwork`` from the host scratch volume — the launch half of ADR-0024.

``/uvwork`` is where every spec's ``startup_commands`` put uv, the uv cache,
``TMPDIR`` and the agent venv (``/uvwork/venv-agent`` — see
``_apptainer_listen_env``'s ``UV_PROJECT_ENVIRONMENT``). The base image creates
the directory (``containers/apptainer-base.def``) and until this module nothing
bound it, so every byte written there went to the apptainer overlay upper on
the host's root volume. The resolver in :mod:`.._state.host_scratch` says which
host directory should back it instead; this module makes that directory exist
and emits the bind.

Per agent: ``<scratch_root>/sac/agents/<agent>/uvwork``, created 0700 (the
venv and cache are one agent's private working set; nothing else on the host
needs to read them), bound read-write at ``/uvwork``. Idempotent across
restarts — an existing directory is left exactly as it is, mode included, so an
operator who tightened or loosened it is not silently overruled. The spec's
own ``[ -x /uvwork/bin/uv ] || curl ...`` / ``[ -x /uvwork/venv-agent/bin/python
] || uv venv ...`` steps then run against scratch unchanged: on the first start
after this lands they rebuild there once, unless ``sac agents scratch-migrate``
moved the overlay copy across first.

Called from :func:`._apptainer_argv_finalize.finalize_flag_argv`, after every
spec-declared bind, so that a spec which binds ``/uvwork`` explicitly wins:
apptainer keeps the FIRST bind to a destination and skips later duplicates
("already in mount point list"), and the rule this package already applies to
its fleet-default binds (``_p3a_default_binds``) is that an explicit spec entry
to the same destination overrides the default. That case is logged, never
silent.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from .._state.host_scratch import ScratchRoot, ScratchRootError, resolve_scratch_root

logger = logging.getLogger(__name__)

#: The in-container path every spec's uv / venv / TMPDIR wiring targets.
UVWORK_CONTAINER_PATH = "/uvwork"

#: Owner-only: one agent's venv, uv cache and TMPDIR are its private working set.
UVWORK_DIR_MODE = 0o700

#: ``<scratch_root>/sac/agents/<agent>/uvwork`` — the per-agent layout.
SCRATCH_AGENTS_SUBDIR = ("sac", "agents")


def scratch_uvwork_dir(root: Path, agent: str) -> Path:
    """The host directory that backs ``/uvwork`` for ``agent`` under ``root``.

    Pure — no filesystem access. ``agent`` becomes ONE path component, so a
    name that is empty, ``.``/``..`` or carries a separator is refused here
    rather than resolving to somebody else's directory.
    """
    if not agent or agent in (".", "..") or "/" in agent or os.sep in agent:
        raise ValueError(
            f"agent name {agent!r} cannot be a scratch path component under {root}"
        )
    return root.joinpath(*SCRATCH_AGENTS_SUBDIR, agent, "uvwork")


def ensure_scratch_uvwork(root: Path, agent: str) -> Path:
    """Create ``<root>/sac/agents/<agent>/uvwork`` (0700) if missing; return it.

    Idempotent: an existing directory is returned untouched. A path that
    exists but is not a directory is a refusal naming the path — a file
    sitting where the bind source must be would make apptainer FATAL with a
    far less useful message.
    """
    target = scratch_uvwork_dir(root, agent)
    if target.is_dir():
        return target
    if target.exists():
        raise ScratchRootError(
            f"{target} exists and is not a directory; it must be the directory "
            f"that backs /uvwork for agent {agent!r}. Move it aside or delete it."
        )
    target.mkdir(parents=True, exist_ok=True)
    os.chmod(target, UVWORK_DIR_MODE)
    return target


def _argv_already_binds_uvwork(argv: list[str]) -> str | None:
    """The earlier ``--bind`` spec string targeting ``/uvwork``, if any."""
    for i, arg in enumerate(argv):
        if arg != "--bind" or i + 1 >= len(argv):
            continue
        spec = argv[i + 1]
        parts = spec.split(":")
        dst = parts[1] if len(parts) > 1 else parts[0]
        if dst.rstrip("/") == UVWORK_CONTAINER_PATH:
            return spec
    return None


def uvwork_bind_flags(
    config,
    argv: list[str] | None = None,
    *,
    scratch: ScratchRoot | None = None,
) -> list[str]:
    """``["--bind", "<host>:/uvwork:rw"]`` for ``config``, or ``[]``.

    ``[]`` in exactly two cases, both stated: the host's resolved source is
    ``none`` (a written ``scratch_root: none``), or ``argv`` already carries a
    ``--bind`` to ``/uvwork`` from the spec (logged, the spec wins). Any other
    outcome is a bind whose source directory this call has just made exist,
    or a :class:`ScratchRootError` from the resolver — never a silent skip.

    ``scratch`` lets a caller that already resolved the host root pass it in;
    the runtime does not, and resolves once per start here.
    """
    resolved = scratch if scratch is not None else resolve_scratch_root()
    agent = getattr(config, "name", "") or ""
    if resolved.root is None:
        logger.info(
            "uvwork: agent %s keeps /uvwork in the overlay (scratch_root: none — %s)",
            agent,
            resolved.reason,
        )
        return []
    if argv is not None:
        explicit = _argv_already_binds_uvwork(argv)
        if explicit is not None:
            logger.info(
                "uvwork: agent %s declares its own bind to %s (%s); the spec "
                "wins and the scratch bind is not emitted",
                agent,
                UVWORK_CONTAINER_PATH,
                explicit,
            )
            return []
    host_dir = ensure_scratch_uvwork(resolved.root, agent)
    logger.info(
        "uvwork: agent %s -> %s (source=%s: %s)",
        agent,
        host_dir,
        resolved.source,
        resolved.reason,
    )
    return ["--bind", f"{host_dir}:{UVWORK_CONTAINER_PATH}:rw"]


__all__ = [
    "SCRATCH_AGENTS_SUBDIR",
    "UVWORK_CONTAINER_PATH",
    "UVWORK_DIR_MODE",
    "ensure_scratch_uvwork",
    "scratch_uvwork_dir",
    "uvwork_bind_flags",
]
