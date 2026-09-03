"""Where does ``/uvwork`` live on THIS host? — the scratch-root resolver.

ADR-0024. Every agent spec points ``TMPDIR``, ``UV_CACHE_DIR``,
``UV_INSTALL_DIR`` and the agent venv at ``/uvwork``, a directory the base
image creates and NOTHING binds. So all of it lands in the apptainer overlay
upper — ``overlays/<agent>/upper/uvwork`` — which sits on the host's ROOT
volume. Measured on ``scitex-compute-04`` on 2026-09-03: 11.7 GB (sac),
3.3 GB (scitex-dev), 3.0 GB (scitex-hub), 2.5 GB (scitex-cards), 1.9 GB
(scitex-storage); the root LV filled to 0 four times on 2026-09-02. Meanwhile
``/scratch`` is a separate 3.0 TB LV with 2.8 TB free on that host, and on
compute-03 (3.0 TB) and compute-01 (295 GB) alike.

This module answers ONE question, once per start: which host directory backs
``/uvwork``. The answer is a fixed shape (:class:`ScratchRoot`) whose
``source`` says how it was reached, so the argv record and the CLI can both
state it:

  ``config``   ``config.yaml`` declares ``scratch_root: /abs/path``
  ``default``  no declaration; ``/scratch`` is a mount point or directory here
  ``none``     ``config.yaml`` declares ``scratch_root: none`` WITH a reason —
               this host keeps ``/uvwork`` in the overlay, on purpose

There is no fourth state. A host that declares nothing and has no ``/scratch``
gets :class:`ScratchRootError` naming the missing path, the config key, and
both fixes — because the silent alternative is exactly the overlay upper on the
root volume this exists to stop, and compute-01 / compute-03 have NO
``config.yaml`` at all, so the default has to work without one.

ONE DISH: the only knob is ``scratch_root:`` in ``config.yaml``. No env var
selects the root; the ``probe`` parameter below is a seam for tests to point the
DEFAULT probe at a temporary path (a host that has ``/scratch`` could not
otherwise exercise the "default absent" row hermetically), and no production
caller passes it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .host_config import Config, _default_config_path
from .host_config import load as _load_host_config

#: The path probed when ``config.yaml`` declares no ``scratch_root:``.
DEFAULT_SCRATCH_ROOT = Path("/scratch")

#: The closed set of ways a root can be reached.
SCRATCH_SOURCES = ("config", "default", "none")

#: The config key, spelled once so every message names the same thing.
CONFIG_KEY = "scratch_root"


class ScratchRootError(RuntimeError):
    """No scratch root could be honoured on this host — refuse to start.

    Carries the path that was missing, the ``config.yaml`` that was read and
    both fixes, so the operator's next action is a mount or a one-line edit,
    never a guess.
    """


@dataclass(frozen=True)
class ScratchRoot:
    """The resolved answer: ``root`` is ``None`` exactly when ``source`` is
    ``"none"`` (the written decision to keep ``/uvwork`` in the overlay)."""

    root: Path | None
    source: str
    reason: str

    def __post_init__(self) -> None:
        if self.source not in SCRATCH_SOURCES:
            raise ValueError(
                f"ScratchRoot.source must be one of {SCRATCH_SOURCES}, "
                f"got {self.source!r}"
            )
        if (self.root is None) != (self.source == "none"):
            raise ValueError(
                f"ScratchRoot: root={self.root!r} is inconsistent with "
                f"source={self.source!r} (root is None iff source is 'none')"
            )


def resolve_scratch_root(
    config: Config | None = None,
    *,
    probe: Path = DEFAULT_SCRATCH_ROOT,
) -> ScratchRoot:
    """Resolve this host's scratch root from ``config.yaml``, else the default.

    ``config`` defaults to :func:`host_config.load` (the same cached parse
    every other config reader uses). ``probe`` is the default candidate —
    :data:`DEFAULT_SCRATCH_ROOT` in production; tests pass a tmp path.

    Raises :class:`ScratchRootError` when a declared path is not a directory
    on this host (a declaration that cannot be honoured is a refusal, not a
    silent detour into the overlay), and when nothing is declared and the
    probe path is neither a mount point nor a directory.
    """
    cfg = config if config is not None else _load_host_config()
    where = cfg.source_path if cfg.source_path is not None else _default_config_path()
    declared = cfg.scratch
    if declared is not None:
        if declared.is_none:
            return ScratchRoot(root=None, source="none", reason=declared.reason)
        root = Path(declared.root)
        if not root.is_dir():
            raise ScratchRootError(
                f"config.yaml at {where} declares {CONFIG_KEY}: {root}, but "
                f"that is not a directory on this host, so /uvwork has nowhere "
                f"to live off the root volume. Fix ONE of: mount or create "
                f"{root}; or change the '{CONFIG_KEY}:' line in {where} to a "
                f"directory that exists (or to 'none' with a "
                f"'scratch_root_reason:' if this host is to keep /uvwork in "
                f"the overlay upper on its root volume)."
            )
        return ScratchRoot(
            root=root, source="config", reason=f"{where} declares {CONFIG_KEY}: {root}"
        )
    if probe.is_dir():
        how = "a mount point" if probe.is_mount() else "an existing directory"
        return ScratchRoot(
            root=probe,
            source="default",
            reason=f"{probe} is {how} on this host and {where} declares no {CONFIG_KEY}:",
        )
    raise ScratchRootError(
        f"cannot resolve where /uvwork lives on this host: {probe} is neither "
        f"a mount point nor a directory, and {where} declares no "
        f"'{CONFIG_KEY}:'. Without a scratch root, every agent's /uvwork (uv, "
        f"the agent venv, TMPDIR, the uv cache) lands in the apptainer overlay "
        f"upper on the ROOT volume — the volume that filled to 0 four times on "
        f"2026-09-02. Fix ONE of: (1) mount or create {probe} on this host; "
        f"(2) add '{CONFIG_KEY}: /abs/path' to {where}; or, as a written "
        f"decision, add '{CONFIG_KEY}: none' together with "
        f"'scratch_root_reason: <why this host keeps /uvwork in the overlay>'."
    )


__all__ = [
    "CONFIG_KEY",
    "DEFAULT_SCRATCH_ROOT",
    "SCRATCH_SOURCES",
    "ScratchRoot",
    "ScratchRootError",
    "resolve_scratch_root",
]
