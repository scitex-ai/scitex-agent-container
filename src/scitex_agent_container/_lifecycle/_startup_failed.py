"""STARTUP_FAILED lifecycle marker for stillborn agents.

A stillborn agent is one whose container fails during creation —
apptainer FATAL on a missing bind source is the canonical example,
but the marker covers any failure between ``sac agents start <name>``
returning and the SDK runner writing its first heartbeat. The
clew-cohort-a-capsule-0201225 incident on 2026-06-02 was the
motivating case:

  * ``POST /agents`` returned HTTP 200 because the subprocess to
    ``sac agents start`` exited 0 after the apptainer wrapper itself
    handed off to the next stage,
  * but the apptainer container creation FATAL'd with "mount source
    /work/... doesn't exist",
  * the SDK runner never started → no pidfile, no session.jsonl, no
    heartbeat,
  * ``GET /agents/<name>/status`` returned ``session_id: null``,
    ``DELETE /agents/<name>`` returned 404 "no pid file", ``POST
    /agents/<name>/send`` returned 400 "no live session".

The host had silently lost a request and the operator had three
different shaped failures to triage. This module adds a single
on-disk marker (``runtime_dir/STARTUP_FAILED``) so:

  * the marker IS the record of the stillborn lifecycle event,
  * ``DELETE`` returns ``410 Gone`` (resource existed, has been removed)
    with the failure detail in the body instead of ``404 Not Found``,
  * ``STATUS`` reports ``status=startup_failed`` with the same body so
    the operator and any orchestrator see a unified shape,
  * future automation can drive an off-host ``forget`` of any agent
    whose state-dir has only ``STARTUP_FAILED`` + no pidfile (= it
    never started, never will, safe to GC).

The on-disk shape is intentionally minimal JSON with a schema version
so we can extend without breaking the existing readers:

.. code-block:: json

   {
     "schema_version": 1,
     "started_at":   "<ISO-8601 UTC>",
     "failed_at":    "<ISO-8601 UTC>",
     "phase":        "container_creation",
     "kind":         "apptainer_mount_failed",
     "exit_code":    255,
     "runtime_dir":  "<host abs path to per-instance state dir>",
     "stdout_tail":  "<last N lines>",
     "stderr_tail":  "<last N lines>",
     "remediation_hint": "..."
   }

The ``runtime_dir`` field is the host-absolute path to the per-instance
state directory carrying this marker (and any peer ``stdout.log`` /
``stderr.log`` if the spawn captured them). Echoing it in the JSON
spares wire-shape consumers (clew launcher, ``sac agents delete``
410 body) having to recompute or guess the path — the marker is
self-describing.

Tail capture is bounded so a runaway log doesn't bloat the marker.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Schema version. Bump when the shape changes; readers should
# downgrade gracefully on a higher value.
SCHEMA_VERSION = 1

# Bound on how much of stdout / stderr we copy into the marker.
# Generous enough to capture a FATAL block + surrounding context;
# small enough that a `runtime_dir/STARTUP_FAILED` stays a single
# struct-readable file.
_TAIL_BYTES_LIMIT = 8 * 1024  # 8 KiB

# Marker filename. Capital-letters convention mirrors ``CHANGELOG.md``-
# style sentinel files — easy to grep, hard to mistake for runtime
# state the agent owns.
MARKER_FILENAME = "STARTUP_FAILED"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _tail_text(text: str, limit: int = _TAIL_BYTES_LIMIT) -> str:
    """Return the last ``limit`` bytes of ``text`` (decoded as-is).

    Avoids splitting in the middle of a multi-byte UTF-8 sequence by
    re-encoding to bytes, slicing, then decoding with ``errors="replace"``
    so an arbitrary truncation point doesn't raise.
    """
    if not text:
        return ""
    raw = text.encode("utf-8", errors="replace")
    if len(raw) <= limit:
        return text
    return raw[-limit:].decode("utf-8", errors="replace")


def classify_apptainer_failure(stdout: str, stderr: str) -> tuple[str, str]:
    """Best-effort ``(kind, remediation_hint)`` for apptainer FATAL stdout.

    Pattern-matches the well-known failure shapes so the marker carries
    an actionable kind tag rather than a free-form blob. Falls back to
    ``"container_creation_unknown"`` with a generic hint if the trace
    isn't recognised.

    Recognised today:

      * ``"mount source ... doesn't exist"`` → ``"apptainer_mount_failed"``
        — the historical clew case + most SAC-from-SAC bind-rewrite gaps.
      * ``"failed to open overlay image"`` → ``"overlay_missing"``
        — the agent's per-agent directory overlay does not exist on the
        host. sac now provisions it before every launch
        (``runtimes/_apptainer_overlay.ensure_overlay_dirs``), so this
        should be unreachable; if it fires anyway the overlay path is
        outside sac's control (e.g. a hand-edited raw_arg pointing at an
        unwritable location) and the hint names the fix.
      * ``"image ... is not a valid SIF image"`` → ``"sif_invalid"``
        — the host SIF path is wrong / partial download / wrong arch.
      * ``"No space left on device"`` → ``"disk_full"``.

    The detection list is intentionally short — adding a new shape
    later only needs a regex + hint pair.
    """
    blob = (stdout or "") + "\n" + (stderr or "")
    # Checked BEFORE disk_full: an overlay FATAL can mention the overlay
    # *image* path while the real cause is the missing directory.
    if "failed to open overlay image" in blob or "while loading overlay images" in blob:
        return (
            "overlay_missing",
            "the agent's apptainer overlay directory does not exist (or is "
            "not readable) on the host. Apptainer creates <overlay>/upper "
            "and <overlay>/work itself, but NEVER the overlay root — that "
            "must exist before `apptainer exec`. sac auto-provisions it at "
            "launch (runtimes/_apptainer_overlay.ensure_overlay_dirs); if "
            "you are seeing this, create the path named in the FATAL line by "
            "hand — `mkdir -p <overlay>/upper <overlay>/work` — or repoint "
            "spec.apptainer.overlay (or the --overlay raw_arg) at a path "
            "this user can write.",
        )
    if "mount source" in blob and "doesn't exist" in blob:
        return (
            "apptainer_mount_failed",
            "one or more bind sources do not exist on the host; "
            "rewrite the offending path(s) or have the parent expose "
            "them via a host-visible bind. The inline-spec POST /agents "
            "preflight (PR-1) catches this when the spec is submitted; "
            "if you saw this marker, the failure escaped the preflight "
            "(e.g. host-side spec was edited after preflight).",
        )
    if "is not a valid SIF image" in blob or "Failed to load SIF" in blob:
        return (
            "sif_invalid",
            "the apptainer SIF at spec.apptainer.image is missing or "
            "corrupted; rebuild or rsync a fresh copy and re-spawn.",
        )
    if "No space left on device" in blob or "ENOSPC" in blob:
        return (
            "disk_full",
            "the host filesystem hosting the runtime dir / overlay is "
            "out of space; free up disk and re-spawn.",
        )
    return (
        "container_creation_unknown",
        "container creation failed for an unrecognised reason — see "
        "stdout_tail / stderr_tail and the runtime_dir's stdout.log "
        "for full context.",
    )


def write_marker(
    runtime_dir: Path,
    *,
    started_at: str,
    phase: str,
    exit_code: int,
    stdout: str,
    stderr: str,
    kind_override: str | None = None,
) -> Path:
    """Write ``runtime_dir/STARTUP_FAILED`` and return its path.

    ``runtime_dir`` is the agent's per-instance state directory
    (``~/.scitex/agent-container/runtime/<name>/``). Created if it
    doesn't exist — the failure may have happened so early that the
    directory itself isn't there yet.

    Args:
        runtime_dir: Target state directory.
        started_at: ISO-8601 timestamp of the spawn attempt.
        phase: Lifecycle phase the failure happened in (e.g.
            ``"container_creation"``, ``"sdk_init"``,
            ``"to_home_deploy"``). Free-form string the lifecycle
            owner picks; downstream consumers branch on ``kind`` rather
            than ``phase``.
        exit_code: Subprocess exit code if known (apptainer returns
            255 on FATAL; -1 / 0 acceptable for "unknown").
        stdout / stderr: The subprocess's captured streams. Tail-clipped
            to :data:`_TAIL_BYTES_LIMIT` before write.
        kind_override: Skip the automatic ``classify_apptainer_failure``
            pattern match and use the given ``kind``. Useful for callers
            that already know the cause (e.g. an explicit ``sdk_init``
            failure that doesn't look like an apptainer FATAL).

    The write is best-effort atomic: written to a ``.tmp`` sibling
    and ``os.replace``-d into place so partial markers aren't visible
    to ``DELETE`` / ``STATUS`` readers.
    """
    runtime_dir.mkdir(parents=True, exist_ok=True)
    if kind_override is not None:
        kind = kind_override
        # No automatic hint when caller pinned the kind — they have
        # better context than the regex matcher.
        hint = ""
    else:
        kind, hint = classify_apptainer_failure(stdout, stderr)
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "started_at": started_at,
        "failed_at": _now_iso(),
        "phase": phase,
        "kind": kind,
        "exit_code": int(exit_code),
        # Host-absolute path to the per-instance state directory so the
        # marker is self-describing. The DELETE 410 / STATUS bodies
        # echo this verbatim so a clew launcher (or human operator)
        # can `cat <runtime_dir>/stderr.log` without recomputing the
        # path from <name>.
        "runtime_dir": str(runtime_dir.resolve()),
        "stdout_tail": _tail_text(stdout),
        "stderr_tail": _tail_text(stderr),
        "remediation_hint": hint,
    }
    target = runtime_dir / MARKER_FILENAME
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(target)
    return target


def read_marker(runtime_dir: Path) -> dict[str, Any] | None:
    """Return the parsed marker dict, or ``None`` if absent/unreadable.

    Best-effort: a partially-written / hand-corrupted marker collapses
    to ``None`` so callers fall back to their existing not-found path.
    """
    target = runtime_dir / MARKER_FILENAME
    if not target.is_file():
        return None
    # stx-allow: fallback (reason: an externally-corrupted marker should
    # NOT crash status/delete; collapse to None and the caller treats
    # it as a stillborn-with-no-record case)
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):  # stx-allow: fallback (reason: see inline comment)
        return None


def is_stillborn(runtime_dir: Path) -> bool:
    """True iff the runtime_dir carries a ``STARTUP_FAILED`` marker."""
    return (runtime_dir / MARKER_FILENAME).is_file()


__all__ = [
    "MARKER_FILENAME",
    "SCHEMA_VERSION",
    "classify_apptainer_failure",
    "is_stillborn",
    "read_marker",
    "write_marker",
]
