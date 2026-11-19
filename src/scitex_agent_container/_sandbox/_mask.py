"""The control-plane mask around a sealed run's agent tree (pure argv helpers).

Every sealed TUI pane starts what the agent can drive — its
``startup_commands`` and the harness, and therefore every tool child —
through :mod:`._hide`, with :func:`control_plane_paths` covered:

* Claude Code and Codex panes run the harness directly, so the mask is the
  pane's inner command itself: ``python3 -I /run/sac-sandbox/bin/hide.py
  --hide ... -- /bin/bash -lc "<startup_commands>; exec <harness>"`` —
  the mask exists before the first startup command runs, and the startup
  shell's exports still reach the harness.
* Hermes and OpenCode panes have an in-container SAC owner that must keep
  ``/state/<name>``; it starts the harness processes under the mask itself
  (``--hide``). Their ``startup_commands`` run first, in their OWN masked
  shell (filesystem effects persist; exported variables do not reach the
  harness), and the owner is started only when that shell succeeded.

The mask launcher is the run's read-only copy of :mod:`._hide`, run BY PATH
under ``-I`` on the image's ``python3`` (like the egress gateway), so no SAC
installation needs to be visible to the agent tree for it to start. SAC
modules that do run in-container (the owners) start through
:func:`isolated_module_argv`, never ``python -m``: the working directory and
``PYTHONPATH`` are agent-influenced and must not shadow SAC or the stdlib.
"""

from __future__ import annotations

import os
import shlex
import shutil
import sys
import sysconfig
from pathlib import Path
from typing import Any, Sequence

from ._layout import C_HIDE, C_SOCK

__all__ = [
    "IMAGE_SAC_SOURCE",
    "ISOLATED_MODULE_BOOTSTRAP",
    "SAC_IMPORT_ROOT",
    "control_plane_paths",
    "isolated_module_argv",
    "launching_sac_binary",
    "masked_argv",
    "sealed_tui_inner_argv",
]

#: The ``scitex_agent_container`` package directory of this installation.
SAC_PACKAGE_DIR = Path(__file__).resolve().parents[1]
#: The directory that makes exactly this installation importable.
SAC_IMPORT_ROOT = SAC_PACKAGE_DIR.parent
#: SAC's source tree some images carry (importable with a PYTHONPATH).
IMAGE_SAC_SOURCE = "/opt/scitex-agent-container-src"

#: ``python -I -c`` bootstrap: run ``module`` as ``__main__`` from exactly
#: the SAC installation at ``root``. ``-I`` keeps the working directory,
#: ``PYTHONPATH`` and user site off ``sys.path``; ``runpy`` is imported
#: before ``root`` is prepended, so nothing at ``root`` can shadow it.
ISOLATED_MODULE_BOOTSTRAP = """\
import sys
import runpy
root, module = sys.argv[1], sys.argv[2]
sys.argv = [module, *sys.argv[3:]]
sys.path.insert(0, root)
runpy.run_module(module, run_name="__main__", alter_sys=True)
"""


def isolated_module_argv(
    python: str | Path, module: str, args: Sequence[str] = ()
) -> list[str]:
    """``python -m module args`` without the working directory or PYTHONPATH."""
    return [
        str(python),
        "-I",
        "-c",
        ISOLATED_MODULE_BOOTSTRAP,
        str(SAC_IMPORT_ROOT),
        module,
        *map(str, args),
    ]


def launching_sac_binary() -> Path | None:
    """The launching installation's ``sac`` (scripts dir first, then PATH)."""
    candidates = [Path(sysconfig.get_path("scripts")) / "sac"]
    located = shutil.which("sac")
    if located:
        candidates.append(Path(located))
    return next((c for c in candidates if c.is_file() and os.access(c, os.X_OK)), None)


def control_plane_paths(config: Any) -> list[str]:
    """What a sealed agent tree must not see, for every harness.

    ``/state/<name>`` (secret env, tokens, logs, the owner's state), the
    launching installation (its whole environment when it is a venv, else
    its package and ``sac`` script), SAC's in-image venv as a whole, every
    other in-image SAC console script (the launcher also covers their
    environments' package), the image's SAC source tree, and the run's
    proxy/ingress socket directory (only the egress gateway, which runs
    outside the mask, uses it). Absent paths are skipped by the launcher.
    """
    from ..runtimes._apptainer_inner_argv_tui import (
        _SAC_BIN_IN_SIF_CANDIDATES,
        _SAC_BIN_IN_SIF_DEFAULT,
    )

    prefix, base_prefix = Path(sys.prefix), Path(sys.base_prefix)
    paths = [f"/state/{config.name}"]
    paths.append(str(prefix if prefix != base_prefix else SAC_PACKAGE_DIR))
    binary = launching_sac_binary()
    if binary is not None and not binary.is_relative_to(prefix):
        paths.append(str(binary))
    paths.append(str(Path(_SAC_BIN_IN_SIF_DEFAULT).parents[1]))
    for candidate in _SAC_BIN_IN_SIF_CANDIDATES:
        paths += [candidate, str(Path(candidate).with_name("scitex-agent-container"))]
    paths += [IMAGE_SAC_SOURCE, C_SOCK]
    return list(dict.fromkeys(paths))


def masked_argv(config: Any, argv: Sequence[str]) -> list[str]:
    """``argv`` started in-container under the mask (refused, exit 125, if
    the mask cannot be established or a ``sac`` stays runnable)."""
    out = ["python3", "-I", C_HIDE]
    for path in control_plane_paths(config):
        out += ["--hide", path]
    return [*out, "--", *argv]


def sealed_tui_inner_argv(
    config: Any,
    *,
    alias_steps: Sequence[str],
    startup_steps: Sequence[str],
    runner_tail: Sequence[str],
    owner_masks_harness: bool,
) -> list[str]:
    """The sealed pane's inner command; see the module docstring."""
    runner = " ".join(shlex.quote(p) for p in runner_tail)
    if not owner_masks_harness:
        inline = "; ".join([*alias_steps, *startup_steps, f"exec {runner}"])
        return masked_argv(config, ["/bin/bash", "-lc", inline])
    steps = list(alias_steps)
    if startup_steps:
        startup = masked_argv(config, ["/bin/bash", "-lc", "; ".join(startup_steps)])
        steps += ["set -e", " ".join(shlex.quote(a) for a in startup)]
    return ["/bin/bash", "-lc", "; ".join([*steps, f"exec {runner}"])]
