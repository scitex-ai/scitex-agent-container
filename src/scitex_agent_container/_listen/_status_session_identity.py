"""Read-only status session evidence through the canonical runtime owner seam."""

from __future__ import annotations

import asyncio
from pathlib import Path

from .._lifecycle._off_loop import run_blocking
from .._lifecycle._runtime_select import _get_runtime
from .._lifecycle._session_identity import read_session_id_for
from .._runners._hermes_owned_session import SessionEvidenceError


async def status_session_identity(config, state_dir: Path) -> tuple[str | None, dict]:
    """Retain SDK precedence and runtime proof without hiding unrelated status.

    Path-only Hermes proof cannot validate a container's host bind alias. The
    lifecycle owner seam verifies that alias and the authenticated live pair.
    An unavailable identity never authorizes a fallback session or a fresh run.
    """
    def read_owned_identity():
        return read_session_id_for(config, _get_runtime(config), state_dir)

    try:
        session_id = await run_blocking(read_owned_identity, timeout_s=3.0)
    except SessionEvidenceError:
        return None, {"state": "unknown", "reason": "session_ownership_unproven"}
    except asyncio.TimeoutError:
        return None, {"state": "unknown", "reason": "session_identity_timeout"}
    return session_id, {
        "state": "observed" if session_id is not None else "absent",
        "reason": "",
    }
