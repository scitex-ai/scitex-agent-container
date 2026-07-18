"""Build REAL hook executables for the never-stop-when-task-remains tests.

PA-306 no-mocks: these are not patched callables. Each helper writes a real
executable to disk that a real :func:`subprocess.run` spawns, prints to real
stdout/stderr, and exits with a real code — the same interface the
scitex-cards executable presents. ``$SAC_MAY_STOP_CMD`` (a documented
production knob, not a test-only hatch) points sac at the script.

Note what is NOT here any more: there is no fixture reproducing
scitex-cards' payload schema or its numbered hint lines. sac no longer
parses either, so a fixture encoding their format would be testing a
coupling we deliberately removed — and would quietly re-establish it, since
a fixture is a claim about someone else's output.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path


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
