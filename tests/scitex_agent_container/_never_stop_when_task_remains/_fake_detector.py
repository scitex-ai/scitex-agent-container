"""Build REAL hook executables for the never-stop-when-task-remains tests.

PA-306 no-mocks: these are not patched callables. Each helper writes a real
executable to disk that a real :func:`subprocess.run` spawns, prints to real
stdout/stderr, and exits with a real code — the same interface the
scitex-cards executable presents. ``$SAC_MAY_STOP_CMD`` (a documented
production knob, not a test-only hatch) points sac at the script.

A fixture is a CLAIM ABOUT SOMEONE ELSE'S OUTPUT, so the two below are not
invented — both were captured from real runs on this host:

* :data:`CLICK_USAGE_ERROR` is what ``scitex-cards`` really prints (and the
  code it really exits with) for a verb it does not have.
* :func:`runnable_verdict` mirrors a real ``may-stop --agent ...`` answer.

An earlier revision deliberately shipped NO fixture of the detector's
payload, to avoid coupling sac to scitex-cards' schema. That purity is what
let the live defect through: with only hook-protocol JSON in the suite,
nothing ever exercised the shape the detector ACTUALLY emits, and the
never-stop gate was tested exclusively against output it never receives. We
now assert the minimum needed to tell an ANSWER from a FAILURE TO ANSWER —
the ``runnable`` flag — and nothing more.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

#: Exactly what a host whose scitex-cards predates ``may-stop`` emits: the
#: usage text on STDERR, an EMPTY stdout, and exit 2 — the same code the
#: protocol uses for "work remains". Captured from a real run.
CLICK_USAGE_ERROR = (
    "Usage: scitex-cards [OPTIONS] [COMMAND] [ARGS]...\n"
    "Try 'scitex-cards --help' for help.\n"
    "\n"
    "Error: No such command 'may-stop'."
)


def write_detector(
    tmp_path: Path,
    *,
    returncode: int,
    stdout: str = "",
    stderr: str = "",
    name: str = "fake-hook-exe",
) -> Path:
    """Write an executable emitting exactly these streams + exit code."""
    script = tmp_path / name
    script.write_text(
        "#!/bin/sh\n"
        "cat >/dev/null 2>&1 || true\n"  # drain the hook payload like the real one
        f"cat <<'SAC_EOF_OUT'\n{stdout}\nSAC_EOF_OUT\n"
        f"cat >&2 <<'SAC_EOF_ERR'\n{stderr}\nSAC_EOF_ERR\n"
        f"exit {returncode}\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return script


def stale_cards_detector(tmp_path: Path, *, name="stale-cards") -> Path:
    """A detector from BEFORE ``may-stop`` existed — the fleet's steady state.

    Exits 2 (which the protocol reads as "work remains") while stdout is
    empty and stderr holds click's usage error.
    """
    return write_detector(
        tmp_path, returncode=2, stdout="", stderr=CLICK_USAGE_ERROR, name=name
    )


def runnable_verdict(
    *, agent: str = "a", items: "list[dict] | None" = None, idle_seconds: int = 1084
) -> str:
    """One line of JSON shaped like a real ``may-stop`` "work remains" answer.

    Note it carries NO ``decision`` key — the detector answers in its own
    verdict schema, not in Claude Code's hook protocol.
    """
    if items is None:
        items = [
            {
                "card_id": "card-1",
                "reason": "in_progress card",
                "next_action": "work it, update it, or close it",
            }
        ]
    return json.dumps(
        {
            "agent": agent,
            "runnable": True,
            "items": items,
            "idle_seconds": idle_seconds,
        }
    )


def isolate_runtime(env_save_restore, tmp_path: Path) -> Path:
    """Point the loop-guard state tree at ``tmp_path``.

    ``runtime_base_dir()`` reads its env var at CALL time, so this really
    redirects the state — there is no import-time constant to defeat it.
    """
    root = tmp_path / "sac-runtime"
    env_save_restore.set("SCITEX_AGENT_CONTAINER_RUNTIME_DIR", str(root))
    return root


def clear_identity(env_save_restore) -> None:
    """Remove every identity var so a test controls resolution completely."""
    from scitex_agent_container._never_stop_when_task_remains._identity import (
        IDENTITY_ENV_VARS,
    )

    for key in IDENTITY_ENV_VARS:
        env_save_restore.delete(key)


def detector_env(env_save_restore, script: "Path | str") -> None:
    """Point sac at ``script`` as the registered hook executable."""
    env_save_restore.set("SAC_MAY_STOP_CMD", str(script))


def missing_detector(env_save_restore, tmp_path: Path) -> None:
    """Point sac at a path that genuinely does not exist."""
    env_save_restore.set(
        "SAC_MAY_STOP_CMD", str(tmp_path / "no-such-binary" / os.sep.join(["gone"]))
    )
