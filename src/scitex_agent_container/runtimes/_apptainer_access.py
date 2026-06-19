"""``spec.access`` — host-access posture helpers (operator directive
2026-06-19, ``feedback_sac_dev_agent_bind_policy``).

Dev agents see the whole host BY DEFAULT; capsule restriction is an
opt-in knob:

* ``access: full`` (DEFAULT) — bind the operator's whole home
  (``/home/<user>:/home/<user>:rw``) so the agent reaches every project
  + config at its CANONICAL host path, and open the TUI at the workdir's
  canonical path (not the ``/work`` alias). The agent's own
  ``$HOME=/home/agent`` (credentials_file / to_home / overlay wiring)
  is UNTOUCHED — ``full`` only emits one ADDITIONAL whole-home bind +
  switches the workdir bind target / ``--pwd`` to the canonical path.

* ``access: capsule`` — ONLY the binds explicitly listed in the spec
  (the pre-2026-06-19 behaviour). The workdir mounts at the spec's
  ``container_workdir`` (default ``/work``) and ``--pwd`` points there.
  For leak-prevention agents that must not see the rest of the host.

Why a separate module: keeps ``_apptainer_build_argv.build_run_argv``
under the 512-line per-file cap and makes the access-posture logic
independently unit-testable (it's a pure function of the config).

Watch-outs (verified against the live figrecipe / neurovista / clew
specs + the apptainer mount model):

  * The whole-home bind targets ``/home/<user>`` (e.g.
    ``/home/ywatanabe``), which is a DIFFERENT path from the agent's
    ``$HOME=/home/agent``. So it never shadows the ``/home/agent`` bind,
    the overlay's upper-home bind, or the writable credentials_file bind
    (all of which target ``/home/agent/...``).
  * The overlay (``--overlay <file>``) and credentials_file SOURCE both
    live UNDER ``/home/<user>``; binding ``/home/<user>`` rw makes those
    host paths visible at ``/home/<user>`` inside the container, but the
    overlay is applied at the rootfs layer (independent of binds) and
    the credentials_file binds to the ``/home/agent`` TARGET — no double
    mount, no shadow. (Binding the operator home while the overlay +
    credentials_file paths live under it is known to work at the
    apptainer level — operator memory ``project_tui_in_apptainer``.)
  * When a ``full`` agent's workdir is itself UNDER the operator home
    (the common case, e.g. ``/home/ywatanabe/proj/figrecipe``) the
    canonical workdir bind is nested inside the whole-home bind with an
    IDENTICAL source==target mapping — apptainer handles nested binds
    fine (harmless redundancy). When the workdir is OUTSIDE the home
    (e.g. ``/tmp/...``) the explicit canonical bind is still required, so
    we always emit it for ``full``.

  * BACK-COMPAT — ``full`` agents ALSO keep the legacy ``/work`` mount.
    The live figrecipe / neurovista / clew specs (and their
    ``startup_commands`` / ``startup_prompts``) reference ``/work``
    directly (``ln -sfn /work $HOME/proj/<pkg>``, "Repo at /work"), so
    dropping it would break them. ``full`` therefore binds the workdir at
    BOTH its canonical path AND ``/work``; only ``--pwd`` moves to the
    canonical path (the directive's "prefer canonical over the alias").
    ``capsule`` keeps the single ``/work`` mount + ``--pwd /work``.
"""

from __future__ import annotations

from pathlib import Path

from ..config import AgentConfig

__all__ = [
    "ACCESS_CAPSULE",
    "ACCESS_FULL",
    "WORK_ALIAS",
    "full_home_bind_flags",
    "is_full_access",
    "resolve_pwd",
    "workdir_bind_targets",
]

ACCESS_FULL = "full"
ACCESS_CAPSULE = "capsule"

# Legacy in-container workdir alias kept for back-compat under ``full``
# (specs + prompts hardcode ``/work``). The spec's own
# ``apptainer.container_workdir`` is the actual alias value — this is just
# the historical default it carries when unset.
WORK_ALIAS = "/work"


def is_full_access(config: AgentConfig) -> bool:
    """True when the agent's posture is whole-home access.

    Absent / empty / unrecognised ``access`` defaults to ``full`` — the
    dev-agent default (operator directive). Only the explicit string
    ``"capsule"`` opts OUT of the whole-home bind. Defaulting unknowns to
    ``full`` is safe because the loader normalises ``access`` and the
    validator rejects anything other than ``full``/``capsule`` before we
    ever reach here.
    """
    return str(getattr(config, "access", ACCESS_FULL) or ACCESS_FULL) != ACCESS_CAPSULE


def _operator_home() -> Path:
    """The operator's canonical host home (``Path.home()``).

    ``build_run_argv`` runs on the host that will launch the agent (or
    on the target host under the right ``$HOME`` when dispatched), so
    ``Path.home()`` is the canonical home to expose. This matches what
    the existing per-bind specs hardcode as ``/home/ywatanabe``.
    """
    return Path.home()


def full_home_bind_flags(config: AgentConfig) -> list[str]:
    """``["--bind", "/home/<user>:/home/<user>:rw"]`` for ``full``; else [].

    The whole-home bind that gives a ``full`` (default) agent reach over
    every project + config at the operator's canonical path. No-op for
    ``capsule`` agents (they get only their explicit binds).
    """
    if not is_full_access(config):
        return []
    home = _operator_home()
    return ["--bind", f"{home}:{home}:rw"]


def _container_workdir_alias(config: AgentConfig) -> str:
    """The spec's in-container workdir alias (``apptainer.container_workdir``,
    default ``/work``)."""
    ap = getattr(config, "apptainer", None)
    return str(getattr(ap, "container_workdir", WORK_ALIAS) or WORK_ALIAS)


def workdir_bind_targets(config: AgentConfig) -> list[str]:
    """Container-side path(s) the workdir mounts at, de-duped, in order.

    * ``capsule`` → ``[<container_workdir>]`` (default ``[/work]``) — the
      single legacy mount, byte-identical to pre-2026-06-19.
    * ``full`` → ``[<canonical-workdir-path>, <container_workdir>]`` — the
      workdir is reachable at BOTH the operator's canonical path (so the
      ``--pwd`` lands there even when the workdir is OUTSIDE the home bind,
      e.g. ``/tmp/...``) AND the legacy ``/work`` alias (back-compat for
      specs/prompts that hardcode ``/work``). When the canonical path
      EQUALS the alias (an operator already set ``container_workdir`` to
      the canonical path) the duplicate collapses to one.

    Each returned target gets one ``--bind <workdir-src>:<target>`` in
    ``build_run_argv``. Order matters: the canonical path is emitted first
    so a later identical bind never demotes it.
    """
    alias = _container_workdir_alias(config)
    if not is_full_access(config):
        return [alias]
    canonical = str(Path(config.workdir).expanduser())
    targets = [canonical]
    if alias != canonical:
        targets.append(alias)
    return targets


def resolve_pwd(config: AgentConfig) -> str:
    """The ``--pwd`` the inner process opens at.

    * ``full`` → the CANONICAL workdir path (the directive's "prefer the
      canonical path over the ``/work`` alias"), reachable via the
      whole-home bind and/or the canonical workdir bind.
    * ``capsule`` → the ``/work`` alias (unchanged).

    Workdir is the STARTING cwd, never a jail: a ``full`` agent opens in
    its project but the whole-home bind lets it reach the rest of the host.
    """
    if is_full_access(config):
        return str(Path(config.workdir).expanduser())
    return _container_workdir_alias(config)
