"""Generic remote-launch script generator for the claude-session runner.

Sac stays generic; per-host quirks (module loads, env unsets, custom
PATH) live in the user's private ``~/.scitex/agent-container/hosts/<hostname>.sh``
which the generated script sources before exec'ing the runner. Examples
of what users put in that file:

* Spartan compute node:

  ```bash
  module load GCCcore/11.3.0 OpenSSL/1.1
  unset SCITEX_AGENT_CONTAINER_CI_ANTHROPIC_API_KEY
  ```

* Custom Python venv path:

  ```bash
  export PATH="$HOME/.env-3.11/bin:$PATH"
  ```

The package never writes to that file. New users without a host hook
just get a clean ``[no host hook]`` log line and bare-metal exec.
"""

from __future__ import annotations

import shlex
from pathlib import PurePosixPath

HOST_HOOK_DIR = "$HOME/.scitex/agent-container/hosts"


def render_remote_launch(
    *,
    runner_argv: list[str],
    agent_name: str,
    state_root: str | PurePosixPath | None = None,
    detach: bool = True,
    log_path: str | PurePosixPath | None = None,
) -> str:
    """Render the bash script that launches the runner on the remote host.

    The script:

    1. Sets ``SCITEX_AGENT_CONTAINER_RUNTIME_DIR`` if ``state_root`` given.
    2. Sources ``$HOME/.scitex/agent-container/hosts/$(hostname).sh`` if
       it exists (silent skip otherwise).
    3. Execs ``runner_argv`` either in foreground (``detach=False``) or
       detached via ``setsid nohup`` redirecting to ``log_path``.

    Use cases:

    * Generic users (no host hook): the source step is a no-op; runner
      exec's in whatever environment ssh's login shell provides.
    * Spartan / NAS / containerized hosts: drop a per-host hook to load
      modules, unset stale CI keys, prepend a venv to PATH, etc.

    The output is a single string suitable for ``ssh <host> "bash -s"``
    or ``ssh <host> 'bash -l -c <quoted>'``.
    """
    cmd = " ".join(shlex.quote(a) for a in runner_argv)
    log = (
        str(log_path)
        if log_path
        else f"$HOME/.scitex/agent-container/runtime/{agent_name}/runner.log"
    )

    lines: list[str] = [
        "#!/usr/bin/env bash",
        "set -e",
    ]
    if state_root is not None:
        lines.append(
            f"export SCITEX_AGENT_CONTAINER_RUNTIME_DIR={shlex.quote(str(state_root))}"
        )
    lines.extend(
        [
            # Per-host hook — silent skip if absent so generic users aren't
            # blocked.
            f'_sac_hook="{HOST_HOOK_DIR}/$(hostname).sh"',
            '[ -f "$_sac_hook" ] && . "$_sac_hook"',
        ]
    )
    if detach:
        lines.extend(
            [
                f'mkdir -p "$(dirname {shlex.quote(log)})"',
                # setsid + nohup so the runner survives SSH session close.
                f"setsid nohup {cmd} >>{shlex.quote(log)} 2>&1 < /dev/null &",
                "echo $!",
            ]
        )
    else:
        lines.append(f"exec {cmd}")
    return "\n".join(lines) + "\n"


__all__ = ["HOST_HOOK_DIR", "render_remote_launch"]
