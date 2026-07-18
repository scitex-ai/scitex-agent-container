"""Build REAL ``may-stop`` stand-in executables for the never-stop tests.

PA-306 no-mocks: these are not patched callables. Each helper writes a real
executable script to disk that a real :func:`subprocess.run` spawns, prints
to real stdout/stderr, and exits with a real code — the same interface the
production detector presents. The production code under test is exercised
end to end, including argv construction, pipe reading, and exit-code
interpretation.

``$SAC_MAY_STOP_CMD`` (a documented production knob, not a test-only hatch)
points the detector at the script.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

#: The tolerated store noise the REAL detector emits before any payload —
#: captured verbatim from a live ``scitex-cards`` run on 2026-07-18. Tests
#: prepend this to stderr so hint parsing is proved against the real stream
#: shape, not an idealised one.
REAL_STORE_WARNINGS = (
    "deprecated SCITEX_TODO_* environment names in use (SCITEX_TODO_AGENT_ID); "
    "rename them to SCITEX_CARDS_* — the old prefix is honoured for one "
    "transition window only\n"
    "[scitex-todo] TOLERATED (read-side): /home/agent/.scitex/todo/tasks.yaml: "
    "task 'ps214-215-new-vs-existing-severity' has unknown status 'in-progress'; "
    "this build knows ('goal', 'in_progress', 'blocked', 'done', 'deferred', "
    "'failed', 'cancelled').\n"
    "/opt/venv-sac/lib/python3.12/site-packages/scitex_cards/_model.py:116: "
    "UserWarning: [scitex-todo] TOLERATED (read-side): another warning line\n"
    "  _validate_tasks(tasks, source=str(path), strict=False)\n"
)


def write_detector(
    tmp_path: Path,
    *,
    returncode: int,
    stdout: str = "",
    stderr: str = "",
    name: str = "fake-may-stop",
) -> Path:
    """Write an executable script emitting exactly these streams + exit code."""
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


def runnable_payload(
    items: "list[tuple[str, str, str]]", *, agent: str = "test-agent", idle: int = 900
) -> str:
    """The one-line JSON stdout an exit-2 detector emits."""
    return json.dumps(
        {
            "agent": agent,
            "runnable": True,
            "items": [
                {"card_id": cid, "reason": reason, "next_action": action}
                for cid, reason, action in items
            ],
            "idle_seconds": idle,
        }
    )


def hint_block(
    items: "list[tuple[str, str, str]]", *, with_warnings: bool = True
) -> str:
    """The numbered stderr hints, optionally under the real store noise."""
    head = REAL_STORE_WARNINGS if with_warnings else ""
    head += "Runnable work remains for this agent:\n"
    lines = [
        f"{idx}. {cid} — {reason} — {action}"
        for idx, (cid, reason, action) in enumerate(items, start=1)
    ]
    return head + "\n".join(lines)


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
    from scitex_agent_container._never_stop._identity import IDENTITY_ENV_VARS

    for key in IDENTITY_ENV_VARS:
        env_save_restore.delete(key)


def detector_env(env_save_restore, script: "Path | str") -> None:
    """Point the production detector at ``script``."""
    env_save_restore.set("SAC_MAY_STOP_CMD", str(script))


def missing_detector(env_save_restore, tmp_path: Path) -> None:
    """Point the detector at a path that genuinely does not exist."""
    env_save_restore.set(
        "SAC_MAY_STOP_CMD", str(tmp_path / "no-such-binary" / os.sep.join(["gone"]))
    )
