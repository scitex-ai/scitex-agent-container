"""Dependency-inversion hook execution (todo#286).

The container declares hook points (pre_start/post_start/pre_stop/
post_stop/on_compact/on_restart/on_diff) and runs entries opaquely.
Each entry is EITHER an ``http(s)://`` URL (POSTed as JSON) OR a shell
command (``shlex.split`` + ``subprocess.run``). Stdlib only.

All invocations are fire-and-forget via a module-level thread pool;
``run_hook`` never blocks the caller and never raises — errors are
logged and swallowed. This lets scitex-agent-container stay agnostic
of telegram / any specific fleet comms: external tools plug
in via YAML and the container runs them opaquely.

Trust boundary: entries in a spec.hooks.* list execute arbitrary shell
commands or POST to arbitrary URLs with container-scoped privileges.
Treat agent YAML files as an RCE-equivalent trust boundary: never load a
YAML you don't control, and never inherit hooks from an untrusted source.
The container does not sandbox hook execution — that is by design so that
operators can wire in system-level integrations (systemctl, cloudflared,
notify-send, etc.).
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Mapping
from urllib import error as urlerror
from urllib import request as urlrequest

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT_S = 5.0
_SHELL_TIMEOUT_S = 10.0

# Module-level pool — cheap, never grows, shared across callers.
_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="scitex-hook")


def _flatten_ctx_env(context: Mapping[str, Any] | None) -> dict[str, str]:
    """Flatten a context dict into SCITEX_HOOK_CTX_* env vars."""
    if not context:
        return {}
    out: dict[str, str] = {}
    for k, v in context.items():
        key = f"SCITEX_HOOK_CTX_{str(k).upper()}"
        if isinstance(v, (dict, list)):
            out[key] = json.dumps(v, default=str)
        else:
            out[key] = "" if v is None else str(v)
    return out


def _dispatch_http(
    url: str,
    agent_name: str,
    hook_name: str,
    context: Mapping[str, Any] | None,
) -> None:
    body = json.dumps(
        {"agent": agent_name, "hook": hook_name, "context": dict(context or {})},
        default=str,
    ).encode("utf-8")
    req = urlrequest.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urlrequest.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:
            resp.read()
    except (
        urlerror.URLError,
        urlerror.HTTPError,
        OSError,
        ValueError,
    ) as exc:  # stx-allow: fallback (reason: file system operation failure)
        logger.warning(
            "hook[%s/%s] HTTP POST %s failed: %s",
            agent_name,
            hook_name,
            url,
            exc,
        )


def _dispatch_shell(
    cmd: str,
    agent_name: str,
    hook_name: str,
    context: Mapping[str, Any] | None,
) -> None:
    try:
        argv = shlex.split(cmd)
    except (
        ValueError
    ) as exc:  # stx-allow: fallback (reason: type coercion or format mismatch)
        logger.warning(
            "hook[%s/%s] shlex split failed for %r: %s",
            agent_name,
            hook_name,
            cmd,
            exc,
        )
        return
    if not argv:
        return
    env = {
        **os.environ,
        "SAC_NAME": agent_name,
        "SCITEX_HOOK": hook_name,
        **_flatten_ctx_env(context),
    }
    try:
        subprocess.run(
            argv,
            shell=False,
            env=env,
            timeout=_SHELL_TIMEOUT_S,
            capture_output=True,
            text=True,
            check=True,
        )
    except (
        FileNotFoundError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        OSError,
    ) as exc:
        logger.warning(
            "hook[%s/%s] shell %r failed: %s",
            agent_name,
            hook_name,
            cmd,
            exc,
        )


def _run_one(
    entry: str,
    agent_name: str,
    hook_name: str,
    context: Mapping[str, Any] | None,
) -> None:
    entry = (entry or "").strip()
    if not entry:
        return
    # stx-allow: fallback (reason: hook dispatch is fire-and-forget; a crashed hook must not propagate and disrupt the calling agent)
    try:
        if entry.startswith(("http://", "https://")):
            _dispatch_http(entry, agent_name, hook_name, context)
        else:
            _dispatch_shell(entry, agent_name, hook_name, context)
    except Exception:  # pragma: no cover  # stx-allow: fallback (reason: ultimate hook dispatch safety net — hook crashes must not break agent startup)
        logger.exception("hook[%s/%s] dispatch crashed", agent_name, hook_name)


def run_hook(
    agent_name: str,
    hook_name: str,
    commands: list[str] | None,
    context: Mapping[str, Any] | None = None,
    pool: Any = None,
) -> None:
    """Fire all entries in ``commands`` for ``hook_name``, non-blocking.

    Returns immediately. Each entry runs in the shared thread pool.
    Errors are logged and swallowed.

    Parameters
    ----------
    pool:
        Optional executor exposing ``submit(fn, *args, **kwargs)``.
        Defaults to the module-level shared pool. Callers (and tests)
        that want a private, joinable executor — so they can block on
        completion via ``pool.shutdown(wait=True)`` instead of polling
        side-effects — can pass a real
        ``concurrent.futures.ThreadPoolExecutor`` here. Behaviour is
        otherwise identical: same fire-and-forget dispatch, same error
        swallowing.
    """
    if not commands:
        return
    executor = pool if pool is not None else _POOL
    for entry in commands:
        try:
            executor.submit(_run_one, entry, agent_name, hook_name, context)
        except (
            RuntimeError
        ):  # stx-allow: fallback (reason: runtime state error — handled gracefully)
            # Pool shut down (interpreter exit) — run inline as a
            # last-ditch effort so tests / shutdown paths still work.
            _run_one(entry, agent_name, hook_name, context)
