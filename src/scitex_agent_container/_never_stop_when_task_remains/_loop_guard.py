"""Stop re-driving an agent onto work it cannot move — alarm instead.

Blocking a stop is only useful if the block CHANGES something. If the same
block reason comes back unchanged turn after turn, the agent is not being
driven into work, it is being driven into a wall: the session becomes
unstoppable, every turn burns tokens, and the real problem (a wedged card, a
check that cannot see the agent's progress) stays invisible because the loop
looks like activity.

WHY THIS LIVES IN SAC
---------------------
The guard needs to know how many times THIS SESSION has already been
blocked. That is session state, and the hook executable — owned by
scitex-cards — has no view of it. Everything else about the decision is
theirs; the counting is ours.

The signature is a digest of the executable's OPAQUE block text, never of
card ids extracted from it. Extracting ids would mean knowing their output
format, which is exactly the coupling this boundary removes. Any change in
what they say counts as progress.

Why N = 3
---------
The signature already absorbs genuine progress, so N counts only IDENTICAL
re-drives:

* **1** is the normal, healthy case — the agent is handed its next item and
  takes it.
* **2** is still plausibly healthy: the agent may be mid-task and simply has
  not written its progress back to the board yet, so the reason has not
  moved.
* **3** is the first point at which "this agent is being re-driven onto work
  it cannot move" is better supported than "it is still working". Three
  identical verdicts is enough evidence to stop guessing.

Past that, further blocks buy nothing and cost the thing the fail-open rule
exists to protect: an agent that can never end its turn. So on the trip we
ALLOW the stop and raise an alarm — a loud, visible failure beats an
invisible infinite loop.

The counter lives beside the rest of the per-agent runtime state, under
:func:`~.._runtime_paths.runtime_base_dir`, which is read at CALL time — so
relocating the runtime tree (or a test's ``tmp_path``) takes effect without
an import-time constant to work around.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path

from .._runtime_paths import runtime_base_dir

logger = logging.getLogger(__name__)

#: Consecutive IDENTICAL blocks tolerated before we alarm instead of blocking.
#: See the module docstring for the justification of this value.
MAX_CONSECUTIVE_BLOCKS = 3

_STATE_SUBDIR = "never_stop_when_task_remains"
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_name(agent: str) -> str:
    """A filesystem-safe stem for ``agent`` that cannot escape the state dir."""
    stem = _UNSAFE.sub("-", agent).strip("-") or "unknown-agent"
    return stem[:120]


def state_path(agent: str) -> Path:
    """Where ``agent``'s consecutive-block counter lives."""
    return runtime_base_dir() / _STATE_SUBDIR / f"{_safe_name(agent)}.json"


def signature(block_text: str) -> str:
    """A stable digest of the executable's OPAQUE block text.

    Hashed so the state file stays bounded regardless of how much the
    executable says, and treated as an opaque blob — we never look inside
    it, because its structure is scitex-cards' to change. Surrounding
    whitespace is stripped so trivial formatting drift does not read as
    progress. Empty text gets its own stable signature: a contentless block
    repeating is still a repeat.
    """
    normalized = (block_text or "").strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (
        OSError,
        json.JSONDecodeError,
    ):  # stx-allow: fallback (reason: a missing or corrupt counter must not break the hook; treat it as "no history")
        return {}
    return data if isinstance(data, dict) else {}


def _write(path: Path, payload: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2) + "\n")
    except OSError as exc:  # stx-allow: fallback (reason: an unwritable state dir must not break the hook; the guard degrades to counting from 1)
        logger.warning(
            "never-stop-when-task-remains: could not persist loop-guard state: %s", exc
        )


def record_block(agent: str, block_text: str) -> "tuple[int, bool]":
    """Register a block attempt; return ``(consecutive_count, tripped)``.

    ``consecutive_count`` counts blocks whose text matched the previous one,
    this attempt included. ``tripped`` is ``True`` once that count EXCEEDS
    :data:`MAX_CONSECUTIVE_BLOCKS`, meaning the caller must alarm and allow
    the stop rather than block again.

    Tripping also RESETS the counter, so the alarm fires once and the next
    turn starts from a clean slate instead of alarming forever.
    """
    path = state_path(agent)
    sig = signature(block_text)
    prev = _read(path)
    count = (
        int(prev.get("consecutive_blocks") or 0) if prev.get("signature") == sig else 0
    )
    count += 1

    if count > MAX_CONSECUTIVE_BLOCKS:
        clear_blocks(agent)
        return count, True

    _write(
        path,
        {
            "agent": agent,
            "signature": sig,
            "consecutive_blocks": count,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
    )
    return count, False


def clear_blocks(agent: str) -> None:
    """Forget ``agent``'s block history — called whenever a stop is allowed."""
    try:
        os.unlink(state_path(agent))
    except OSError:  # stx-allow: fallback (reason: nothing to clear is the normal case; a failed unlink must not break the hook)
        pass


__all__ = [
    "MAX_CONSECUTIVE_BLOCKS",
    "clear_blocks",
    "record_block",
    "signature",
    "state_path",
]
