"""Hook execution helpers for the lifecycle layer.

Extracted from the former monolithic ``lifecycle.py`` (split for the
512-line module limit). ``lifecycle`` re-exports ``_run_hooks``,
``_fire_forget_hook`` and ``run_hook`` so existing call sites and tests
(``lc._run_hooks`` / ``lc.run_hook``) are unchanged.
"""

from __future__ import annotations

import logging
import subprocess
from typing import Any, Callable

from ..hooks import run_hook

# Module logger (stdlib, mirroring ._stop_escalate) so hook failures are
# emitted at a proper WARNING LEVEL — rendered as scitex-logging's coloured
# ``WARN:`` prefix by the root handler in production — instead of a bare
# ``print`` with the severity baked into the message text as ``[WARN]``
# (operator 2026-07-19: severity is DATA, not text). Stdlib ``getLogger`` is
# free, so this does not tax the CLI import budget the way a top-level
# ``scitex_logging`` import would (see config._config_logger).
logger = logging.getLogger(__name__)


def _fire_forget_hook(
    agent_name: str,
    hook_name: str,
    commands: list[str],
    context: dict | None = None,
) -> None:
    """Invoke ``run_hook`` (non-blocking, handles URL + shell entries).

    Called alongside the legacy synchronous ``_run_hooks`` path so
    existing YAML pipes/redirects keep working unchanged while
    external tools (orochi etc.) can additionally plug in via
    ``http(s)://`` URLs. The legacy path filters out URL entries to
    avoid double-dispatch of the same side-effect.
    """
    # stx-allow: fallback (reason: hook dispatch is fire-and-forget; a URL hook failing must never crash the caller)
    try:
        run_hook(agent_name, hook_name, list(commands or []), context=context)
    except Exception:  # pragma: no cover  # stx-allow: fallback (reason: hook dispatch safety net — hook crashes must not propagate to caller)
        logger.warning("run_hook %s dispatch failed for %s", hook_name, agent_name)


def _run_hooks(
    hooks: list[str],
    extra_env: dict[str, str] | None = None,
    *,
    runner: Callable[..., Any] = subprocess.run,
) -> None:
    """Execute a list of shell hook commands.

    Args:
        hooks: Shell commands to execute.
        extra_env: Additional env vars passed to hook subprocesses
            (e.g., SCITEX_AGENT_CONTAINER_CONFIG_PATH, SCITEX_AGENT_CONTAINER_SCREEN_NAME, SCITEX_AGENT_CONTAINER_NAME).
        runner: Injectable subprocess runner (real callable; default
            :func:`subprocess.run`). The runner is invoked with the
            exact ``subprocess.run`` keyword surface and must return an
            object with ``returncode`` and ``stderr`` attributes.
    """
    import os

    env = {**os.environ, **(extra_env or {})}
    for hook in hooks:
        if not hook:
            continue
        # URL entries are handled by the new fire-and-forget path
        # (see _fire_forget_hook / hooks.run_hook). Skip them here to
        # avoid trying to ``sh -c "https://..."``.
        if isinstance(hook, str) and hook.startswith(("http://", "https://")):
            continue
        result = runner(hook, shell=True, capture_output=True, text=True, env=env)
        if result.returncode != 0:
            # Log but don't fail — at WARNING level (severity as data), and
            # the captured stderr as its own indented follow-on line rather
            # than a run-on.
            logger.warning("Hook failed (rc=%s): %s", result.returncode, hook)
            if result.stderr:
                logger.warning("    %s", result.stderr.strip())
