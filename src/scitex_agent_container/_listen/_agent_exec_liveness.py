"""Post-ack liveness probe for brokered spawns (extracted from _agent_exec).

Layer-3 fail-loud (clew dogfood repro 2026-06-06, lead msg 57f1632a): after
``sac agents start <child>`` returns rc=0, the listen waits a grace window for
the real apptainer instance to come up before accepting the spawn as healthy —
a SIF that comes up and dies silently must not be reported as SUCC.

Split out of :mod:`scitex_agent_container._listen._agent_exec` to keep that
module under the per-file line cap. ``_agent_exec`` re-imports
``_probe_post_ack_liveness`` so its behavior is unchanged.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

__all__ = [
    "_POST_ACK_LIVENESS_TIMEOUT_S",
    "_POST_ACK_LIVENESS_POLL_INTERVAL_S",
    "_pid_alive",
    "_probe_post_ack_liveness",
]

# Grace window the listen waits AFTER `sac agents start <child>` returns rc=0
# before it accepts the spawn as "running and healthy". Long enough for the
# real apptainer instance to come up on a SLURM-allocation host under typical
# load; short enough that a stillborn instance is caught before the operator's
# recv path treats SUCC as truth.
_POST_ACK_LIVENESS_TIMEOUT_S = 5.0

# How often to re-check the apptainer_pid file inside the grace window.
_POST_ACK_LIVENESS_POLL_INTERVAL_S = 0.1


def _pid_alive(pid: int) -> bool:
    """``kill -0`` style liveness check. Returns False iff pid is reaped.

    ``PermissionError`` means the kernel refused us (e.g. pid owned by
    another uid), but the process exists — we treat that as alive.
    Only ``ProcessLookupError`` (and the equivalent ``OSError(ESRCH)``)
    counts as dead. ``pid <= 0`` is treated as dead defensively (the
    file may have been written truncated / partial).
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _probe_post_ack_liveness(
    runtime_dir: Path,
    *,
    timeout_s: float = _POST_ACK_LIVENESS_TIMEOUT_S,
    poll_interval_s: float = _POST_ACK_LIVENESS_POLL_INTERVAL_S,
) -> tuple[str, str] | None:
    """Return ``None`` if the spawned instance is live by deadline.

    Returns ``(kind, hint)`` tuple on loud failure, suitable for the
    :func:`_lifecycle._startup_failed.write_marker` ``kind_override``
    + the operator-facing diagnostic hint:

    * ``"post_ack_no_apptainer_pid"`` — the child subprocess returned
      0 but never wrote ``<runtime_dir>/apptainer_pid``. The wrapper
      bypassed the apptainer runtime path entirely (broken config,
      missing image, runtime mis-resolved, etc.).
    * ``"post_ack_apptainer_pid_dead"`` — ``apptainer_pid`` was
      written but the process is dead (or never was alive). The SIF
      instance came up and immediately exited; ``stderr.log`` in the
      runtime dir usually has the actual cause.

    Implementation: poll for the file to appear; once it does, check
    liveness via ``os.kill(pid, 0)``. The check repeats until either
    deadline OR a non-alive pid is observed, whichever comes first.
    """
    if timeout_s <= 0.0:
        # Test escape hatch: zero/negative timeout skips the probe. The
        # listen handler honours SAC_LISTEN_POST_ACK_LIVENESS_TIMEOUT_S
        # to set this from the env so legacy tests that don't simulate
        # the apptainer-pid write can opt out (the probe's contract is
        # asserted by the dedicated probe-tests below).
        return None
    pid_file = runtime_dir / "apptainer_pid"
    deadline = time.monotonic() + max(0.0, timeout_s)
    saw_pid_file = False
    last_seen_pid: int | None = None

    while True:
        if pid_file.is_file():
            saw_pid_file = True
            try:
                pid = int(pid_file.read_text(encoding="utf-8").strip())
            except (OSError, ValueError):
                pid = -1
            last_seen_pid = pid
            if pid > 0 and _pid_alive(pid):
                return None  # live → happy path
        if time.monotonic() >= deadline:
            break
        time.sleep(poll_interval_s)

    if not saw_pid_file:
        return (
            "post_ack_no_apptainer_pid",
            f"`sac agents start` returned rc=0 but no apptainer_pid "
            f"file appeared in {runtime_dir} within "
            f"{timeout_s:.1f}s. The child subprocess bypassed the "
            "apptainer runtime path (broken wrapper / missing config / "
            "runtime mis-resolved). Inspect the runtime_dir for any "
            "stdout/stderr the child captured, and verify the agent's "
            "spec.apptainer block resolves to a real SIF on disk.",
        )
    return (
        "post_ack_apptainer_pid_dead",
        f"apptainer_pid={last_seen_pid} in {runtime_dir} was reaped "
        f"within {timeout_s:.1f}s of the spawn-ack. The SIF instance "
        "came up and exited silently — check the runtime_dir's "
        "stderr.log for the apptainer FATAL line. Common causes: "
        "missing bind source on host, SIF binary missing on host, "
        "in-SIF entrypoint crashing on a startup-prompt eval.",
    )
