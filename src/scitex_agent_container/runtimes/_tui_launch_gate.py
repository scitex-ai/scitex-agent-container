"""The TUI launch gate — what runs AFTER the dry-run return, BEFORE tmux.

Extracted from :mod:`.tui_session` (which is at the 512-line per-file cap),
and one cohesive responsibility rather than an arbitrary cut: every step here
shares a single placement invariant. Each is a LAUNCH-time act — it writes to
the host, spawns a probe, or refuses to start — and each therefore belongs
past ``start``'s ``dry_run`` return and past its duplicate-session guard, never
inside ``build_run_argv``. ``sac agents explain`` and ``sac agents start
--dry-run`` both reach that argv builder and start nothing; a read-only
command must neither move files nor fail on a launch-time host condition.
``_apptainer_tmpfs.verify_tmpfs_headroom`` records what happens when that line
is crossed — a full disk made ``explain`` unusable on exactly the host it
would have diagnosed, and wired ~21 argv-building test modules to ambient free
disk. The SDK runtime (``_apptainer_runtime.start``) runs the same steps at
the same point; this module is the TUI half.

The order is load-bearing:

1. **``/uvwork`` on scratch** (ADR-0024) — create the bind source, or REFUSE
   when this host has no scratch root and no written decision to go without.
   First, because it is the only step that can refuse, and a refusal must not
   land after the overlay has already been rewritten.
2. **Overlay venv reconcile** — an image rebuild must invalidate the
   ``venv-sac`` slice of this agent's overlay or the stale site-packages
   shadow the new image forever (contract: ``_maintenance/
   _overlay_venv_model.py``). Before the probe below, so the probe measures
   the RECONCILED union rather than the stale one.
3. **Entry-point probe** — the console script must RUN in the union about to
   launch, not merely import in the image.

Steps 1 and 2 both derive their input FROM THE LAUNCH ARGV rather than
re-resolving it: the ``/uvwork`` bind source and the SIF are read back out of
the very list ``tmux`` will exec. A second resolution is free to drift from
the one that actually launches — reconciling against a DIFFERENT image than
the container mounts would stamp the overlay with an identity it never ran on,
which then reads as reconciled forever, and creating a directory the argv does
not mount would leave the launch to FATAL on a missing bind source. Deriving
makes both divergences unrepresentable.

Imports are function-local, as they were at the call site, so the module stays
cheap to import and each dependency is patchable at its own source module.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def run_launch_gate(config: Any, argv: list[str], *, state_dir: Path) -> None:
    """Run every launch-time gate for ``config``, in the order above.

    ``argv`` is the FINISHED launch argv and ``state_dir`` this agent's state
    directory. Raises rather than returning a verdict — a gate that declines
    is a launch that must not happen, and the caller has nothing useful to do
    with a soft ``False`` (see ``_state.host_scratch.ScratchRootError`` and
    ``_entry_point_gate``'s own error for the two that can fire).
    """
    from ._apptainer_scratch import ensure_uvwork_for_launch

    ensure_uvwork_for_launch(config, argv)

    from .._maintenance._overlay_venv_invalidate import (
        reconcile_overlay_venv_for_launch,
    )

    launch_sif = next((a for a in argv if str(a).endswith(".sif")), None)
    if launch_sif is not None:
        reconcile_overlay_venv_for_launch(config, launch_sif, state_dir)

    from ._entry_point_gate import assert_entry_point_runs

    assert_entry_point_runs(config.name, argv)


__all__ = ["run_launch_gate"]
