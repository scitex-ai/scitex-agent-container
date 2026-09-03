"""Bind ``/uvwork`` from the host scratch volume — the launch half of ADR-0024.

``/uvwork`` is where every spec's ``startup_commands`` put uv, the uv cache,
``TMPDIR`` and the agent venv (``/uvwork/venv-agent`` — see
``_apptainer_listen_env``'s ``UV_PROJECT_ENVIRONMENT``). The base image creates
the directory (``containers/apptainer-base.def``) and until this module nothing
bound it, so every byte written there went to the apptainer overlay upper on
the host's root volume. The resolver in :mod:`.._state.host_scratch` says which
host directory should back it instead; this module decides the bind and, on a
real launch only, makes that directory exist.

Per agent: ``<scratch_root>/sac/agents/<agent>/uvwork``, created 0700 (the
venv and cache are one agent's private working set; nothing else on the host
needs to read them), bound read-write at ``/uvwork``. Idempotent across
restarts — an existing directory is left exactly as it is, mode included, so an
operator who tightened or loosened it is not silently overruled. The spec's
own ``[ -x /uvwork/bin/uv ] || curl ...`` / ``[ -x /uvwork/venv-agent/bin/python
] || uv venv ...`` steps then run against scratch unchanged: on the first start
after this lands they rebuild there once, unless ``sac agents scratch-migrate``
moved the overlay copy across first.

ONE derivation of the per-agent directory. :func:`scratch_uvwork_dir_for` is
the ONLY place the agent's identity becomes a path component, and both halves
of ADR-0024 call it: the launch bind here, and ``sac agents scratch-migrate``
(:mod:`..._maintenance._scratch_migrate`) when it picks a destination. They
used to spell it independently — the migration keyed on the spec DIRECTORY
name, the bind on ``config.name`` — which are the SAME string for a ``host:``
spec and DIFFERENT for a ``hosts:`` one, whose effective id carries a
``-<hostname>`` suffix (``config._loaders.compose_effective_name``). For every
multi-instance agent the migration therefore copied gigabytes to a path no
launch would ever mount, verified the copy, deleted the overlay original, and
reported success. Deriving both from one function makes that divergence
unrepresentable.

TWO PHASES, because ``build_run_argv`` is not a launch.
:func:`uvwork_bind_flags` runs during argv assembly, which ``sac agents
explain`` and ``sac agents start --dry-run`` also reach; it therefore READS
ONLY — it creates nothing, and a host with no resolvable scratch root gets a
visible ``WARNING`` and an argv without the bind, not an exception. The
refusal that is the point of ADR-0024 lives in
:func:`ensure_uvwork_for_launch`, called from the two real launch paths
(``_apptainer_runtime.start`` / ``tui_session.start``) PAST their ``dry_run``
return, where it re-raises that same :class:`ScratchRootError` and creates the
bind source. This is the placement ``_apptainer_tmpfs`` already chose for
``verify_tmpfs_headroom`` and ``_apptainer_runtime`` for
``reconcile_overlay_venv_for_launch``, for the reason stated there: a
READ-ONLY command must neither move files nor fail on a launch-time host
condition. ``verify_tmpfs_headroom`` records what happens otherwise — a full
disk made ``explain`` unusable on exactly the host it would have diagnosed.

The bind is emitted from :func:`._apptainer_argv_finalize.finalize_flag_argv`
after every spec-declared bind, so that a spec which binds ``/uvwork``
explicitly wins: apptainer keeps the FIRST bind to a destination and skips
later duplicates ("already in mount point list"), and the rule this package
already applies to its fleet-default binds (``_p3a_default_binds``) is that an
explicit spec entry to the same destination overrides the default. That case
is logged, never silent.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from .._state.host_scratch import ScratchRoot, ScratchRootError, resolve_scratch_root

logger = logging.getLogger(__name__)

#: The in-container path every spec's uv / venv / TMPDIR wiring targets.
UVWORK_CONTAINER_PATH = "/uvwork"

#: Owner-only: one agent's venv, uv cache and TMPDIR are its private working set.
UVWORK_DIR_MODE = 0o700

#: ``<scratch_root>/sac/agents/<agent>/uvwork`` — the per-agent layout.
SCRATCH_AGENTS_SUBDIR = ("sac", "agents")


def uvwork_agent_key(config) -> str:
    """The agent identity that becomes a scratch path component for ``config``.

    The EFFECTIVE id (``config.name``), not the spec directory name — they
    differ for a ``hosts:`` spec, and the effective id is what every other
    per-agent runtime path already keys on. Spelled once, here, so the launch
    bind and the migration cannot pick different answers.
    """
    return getattr(config, "name", "") or ""


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


def scratch_uvwork_dir_for(root: Path, config) -> Path:
    """The host directory that backs ``/uvwork`` for ``config`` under ``root``.

    THE single derivation — see this module's docstring. Every caller that
    needs "where does this agent's /uvwork live on scratch" calls this and
    never composes the path itself.
    """
    return scratch_uvwork_dir(root, uvwork_agent_key(config))


def _ensure_uvwork_dir(target: Path, agent: str) -> Path:
    """Create ``target`` 0700 if missing; return it. WRITES — launch only."""
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


def ensure_scratch_uvwork(root: Path, agent: str) -> Path:
    """Create ``<root>/sac/agents/<agent>/uvwork`` (0700) if missing; return it.

    Idempotent: an existing directory is returned untouched. A path that
    exists but is not a directory is a refusal naming the path — a file
    sitting where the bind source must be would make apptainer FATAL with a
    far less useful message.
    """
    return _ensure_uvwork_dir(scratch_uvwork_dir(root, agent), agent)


def _argv_uvwork_bind(argv: list[str]) -> tuple[str, str] | None:
    """``(source, whole bind spec)`` of the first ``--bind`` whose DESTINATION
    is ``/uvwork``, or ``None``."""
    for i, arg in enumerate(argv):
        if arg != "--bind" or i + 1 >= len(argv):
            continue
        spec = argv[i + 1]
        parts = spec.split(":")
        dst = parts[1] if len(parts) > 1 else parts[0]
        if dst.rstrip("/") == UVWORK_CONTAINER_PATH:
            return parts[0], spec
    return None


def _argv_already_binds_uvwork(argv: list[str]) -> str | None:
    """The earlier ``--bind`` spec string targeting ``/uvwork``, if any."""
    found = _argv_uvwork_bind(argv)
    return None if found is None else found[1]


@dataclass(frozen=True)
class UvworkBind:
    """What ``/uvwork`` resolves to for one agent on this host — a VALUE.

    Built without touching the filesystem, so the read-only surfaces can state
    the outcome and the launch path can act on it, from the same decision.

    ``host_dir`` is set exactly when sac emits its own bind; it is ``None`` for
    the written ``scratch_root: none`` decision, for a spec that binds
    ``/uvwork`` itself, and when ``error`` holds the resolver's refusal.
    ``reason`` always says which of those it was.
    """

    agent: str
    host_dir: Path | None
    flags: tuple[str, ...]
    reason: str
    error: ScratchRootError | None = None

    @property
    def refused(self) -> bool:
        """True when a real launch must refuse rather than start."""
        return self.error is not None


def plan_uvwork_bind(
    config,
    argv: list[str] | None = None,
    *,
    scratch: ScratchRoot | None = None,
) -> UvworkBind:
    """Decide ``/uvwork`` for ``config`` WITHOUT writing anything.

    Never raises for a HOST condition: a scratch root that cannot be resolved
    comes back in ``error`` so a read-only caller can print it and a launch
    caller can raise it. A malformed AGENT NAME still raises ``ValueError``
    immediately — that is a config fault, true on every host, and it should
    surface the moment the spec is read (the same line
    ``_apptainer_tmpfs._resolve_scratch`` draws for an unparseable
    ``tmpfs_size``).

    ``scratch`` lets a caller that already resolved the host root pass it in.
    """
    agent = uvwork_agent_key(config)
    if scratch is not None:
        resolved: ScratchRoot = scratch
    else:
        try:
            resolved = resolve_scratch_root()
        except ScratchRootError as exc:
            return UvworkBind(
                agent=agent,
                host_dir=None,
                flags=(),
                reason=f"no scratch root on this host: {exc}",
                error=exc,
            )
    if resolved.root is None:
        return UvworkBind(
            agent=agent,
            host_dir=None,
            flags=(),
            reason=(
                f"kept in the overlay by a written decision "
                f"(scratch_root: none — {resolved.reason})"
            ),
        )
    if argv is not None:
        explicit = _argv_already_binds_uvwork(argv)
        if explicit is not None:
            return UvworkBind(
                agent=agent,
                host_dir=None,
                flags=(),
                reason=(
                    f"the spec binds {UVWORK_CONTAINER_PATH} itself ({explicit}); "
                    f"the spec wins and the scratch bind is not emitted"
                ),
            )
    host_dir = scratch_uvwork_dir_for(resolved.root, config)
    return UvworkBind(
        agent=agent,
        host_dir=host_dir,
        flags=("--bind", f"{host_dir}:{UVWORK_CONTAINER_PATH}:rw"),
        reason=f"{host_dir} (source={resolved.source}: {resolved.reason})",
    )


def uvwork_bind_flags(
    config,
    argv: list[str] | None = None,
    *,
    scratch: ScratchRoot | None = None,
) -> list[str]:
    """``["--bind", "<host>:/uvwork:rw"]`` for ``config``, or ``[]``.

    READ-ONLY — it creates no directory and raises no host-condition error,
    because ``sac agents explain`` and ``sac agents start --dry-run`` reach it
    (see this module's docstring). ``[]`` in exactly three cases, each stated:
    the host's resolved source is ``none``, ``argv`` already carries a
    ``--bind`` to ``/uvwork`` from the spec, or no scratch root resolves at all
    — the last logged at ``WARNING`` so it reaches stderr through Python's
    last-resort handler even with no logging configured, rather than being a
    silently absent bind. The real launch turns that third case into a refusal
    in :func:`ensure_uvwork_for_launch`.
    """
    plan = plan_uvwork_bind(config, argv, scratch=scratch)
    if plan.refused:
        logger.warning(
            "uvwork: agent %s would REFUSE to start — %s", plan.agent, plan.reason
        )
    else:
        logger.info("uvwork: agent %s -> %s", plan.agent, plan.reason)
    return list(plan.flags)


def ensure_uvwork_for_launch(
    config,
    argv: list[str],
    *,
    scratch: ScratchRoot | None = None,
) -> Path | None:
    """Make the ``/uvwork`` bind source exist, or REFUSE the launch. Writes.

    CALL THIS ONLY ON A REAL LAUNCH PATH, past the ``dry_run`` return — it
    creates a directory on the host and raises :class:`ScratchRootError` when
    this host has nowhere to put ``/uvwork``, which is the whole point of
    ADR-0024 and is exactly wrong on a read-only surface.

    ``argv`` is the FINISHED launch argv, and the directory created is the one
    that argv will actually mount: the bind source is read back out of it and
    created only when it matches the source this host resolves to. Same
    reasoning as ``tui_session`` reading the SIF out of the launch argv rather
    than re-resolving it — deriving from what launches makes divergence
    impossible, and it keeps sac from creating a path some SPEC declared and
    sac does not own. Returns the directory, or ``None`` when no bind of ours
    is in play (written ``none`` decision, or the spec's own bind won).
    """
    plan = plan_uvwork_bind(config, scratch=scratch)
    if plan.error is not None:
        raise plan.error
    if plan.host_dir is None:
        logger.info("uvwork: agent %s -> %s", plan.agent, plan.reason)
        return None
    bound = _argv_uvwork_bind(argv)
    if bound is None or bound[0] != str(plan.host_dir):
        logger.info(
            "uvwork: agent %s launches with %s, not sac's %s; leaving it to "
            "whoever declared it",
            plan.agent,
            "no /uvwork bind" if bound is None else bound[1],
            plan.host_dir,
        )
        return None
    return _ensure_uvwork_dir(plan.host_dir, plan.agent)


__all__ = [
    "SCRATCH_AGENTS_SUBDIR",
    "UVWORK_CONTAINER_PATH",
    "UVWORK_DIR_MODE",
    "UvworkBind",
    "ensure_scratch_uvwork",
    "ensure_uvwork_for_launch",
    "plan_uvwork_bind",
    "scratch_uvwork_dir",
    "scratch_uvwork_dir_for",
    "uvwork_agent_key",
    "uvwork_bind_flags",
]
