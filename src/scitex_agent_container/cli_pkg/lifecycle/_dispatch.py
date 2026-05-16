#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Cross-host dispatch for ``sac agents start``.

Step 3a (extraction) of the cross-host pipeline (see
``~/proj/scitex-lead/GITIGNORED/WORKING/remote-agent-pipeline.md``). The
routing branch in ``_start.py`` delegates to :func:`try_dispatch`, which
asks the pure resolver in ``_common._resolve_dispatch_peer`` whether a
remote handoff is required and, when so, calls
:func:`_dispatch_remote_start` to perform the drift check, rsync, and
(in step 4) the remote ``sac agents start`` invocation.

Keeping this code in a sibling module — rather than appending to
``_start.py`` — preserves the per-file 512-line cap and isolates the
dispatch logic for review. Extracted as a pure refactor (no behaviour
change) so the body changes (drift check + rsync) land in an isolated
follow-up diff.
"""

from __future__ import annotations

import shlex
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

import click

from ...config import AgentConfig
from ._common import _resolve_dispatch_peer

if TYPE_CHECKING:
    from ..._state.host_config import PeerSpec


def _is_first_launch_line(line: str) -> bool:
    """Return True when an rsync ``--itemize-changes`` row is a pure-new
    entry (head contains only ``+`` markers, no ``*`` deletion flag).

    Itemized format: ``YXcstpoguax <name>`` — the leading 11-char head
    is everything before the first space. First-launch markers are
    ``>f+++++++++`` / ``cd+++++++++``; drift uses letters like
    ``c.st....``; deletes use ``*deleting``.
    """
    head = line.split(" ", 1)[0]
    return "+++++++++" in head and "*" not in head


def _dispatch_remote_start(
    name: str,
    peer: str,
    *,
    dry_run: bool = False,
    force: bool = False,
) -> int:
    """Dispatch ``sac agents start <name>`` to a remote ``peer``.

    Step 3b implementation: locate the local spec dir, drift-check via
    ``rsync --dry-run --itemize-changes`` (content-checksum mode), and
    perform the real rsync when the drift gate allows it. The remote
    ``sac agents start`` invocation, JSON parse, and lead-side
    instances-row write are step 4 — this helper still raises
    ``NotImplementedError`` after rsync to keep the dispatch branch
    loudly half-built per the no-silent-stubs rule.

    Args:
        name: Agent name (used as both spec-dir basename and remote
            ``sac agents start`` argument).
        peer: ssh alias resolved from ``~/.ssh/config``. The matching
            ``ssh:`` field in ``~/.scitex/agent-container/config.yaml``
            is the source of this alias.
        dry_run: When True, print the planned change count and return
            0 without performing the actual rsync.
        force: When True, override the drift gate and rsync anyway.

    Note:
        The ``env_preamble`` peer field is NOT yet honoured by rsync —
        rsync uses the bare ``peer:path`` form and inherits the
        ``~/.ssh/config`` route. Module-load preambles become relevant
        in step 4 when ssh-hopped ``sac agents start`` lands.

    Raises:
        FileNotFoundError: When the local spec dir for ``name`` does
            not exist under ``~/.scitex/agent-container/agents/``.
        RuntimeError: When ``rsync --dry-run`` fails, when drift is
            detected without ``--force``, or when the real rsync fails.
        NotImplementedError: After a successful rsync — step 4
            (remote ``sac agents start`` invocation) is the next
            wiring step.
    """
    # 1. Locate the local spec dir.
    src_dir = Path.home() / ".scitex" / "agent-container" / "agents" / name
    if not src_dir.is_dir():
        raise FileNotFoundError(
            f"Spec dir for {name!r} not found on lead at {src_dir!s}. "
            f"Create the spec locally before dispatching to {peer!r}."
        )

    # 2. rsync --dry-run --itemize-changes (content-checksum mode).
    remote_target = f"{peer}:.scitex/agent-container/agents/{name}/"
    exclude_args = [
        "--exclude=runtime/",
        "--exclude=__pycache__/",
        "--exclude=.pytest_cache/",
        "--exclude=_sphinx_html/",
    ]
    rsync_dry_argv = [
        "rsync",
        "-acvn",  # archive + checksum + verbose + dry-run
        "--itemize-changes",
        "--delete",
        *exclude_args,
        f"{src_dir!s}/",
        remote_target,
    ]
    rsync_dry = subprocess.run(
        rsync_dry_argv,
        capture_output=True,
        text=True,
        check=False,
    )
    if rsync_dry.returncode != 0:
        raise RuntimeError(
            f"rsync --dry-run failed against {peer!r} (rc={rsync_dry.returncode}):\n"
            f"argv: {' '.join(shlex.quote(a) for a in rsync_dry_argv)}\n"
            f"stderr:\n{rsync_dry.stderr}"
        )

    # 3. Parse itemized output. Itemized rows have an 11-char "YXcstpoguax"
    # head; first-launch rows are all-plus; drift rows use letters; deletes
    # start with "*deleting". Filter the summary trailers that rsync emits.
    itemized = [
        line
        for line in rsync_dry.stdout.splitlines()
        if line and not line.startswith((" ", "sending", "sent", "total"))
    ]
    changes = [line for line in itemized if line and len(line) > 11]
    first_launch = bool(changes) and all(_is_first_launch_line(c) for c in changes)
    drift = bool(changes) and not first_launch

    # 4. Drift gate — error unless --force was passed.
    if drift and not force:
        raise RuntimeError(
            f"Spec drift between lead and {peer!r} for agent {name!r}:\n"
            + "\n".join(f"  {c}" for c in changes)
            + "\n\nResolve manually then re-run, "
            + "or pass --force to overwrite peer-side from lead. "
            + "See ~/proj/scitex-lead/GITIGNORED/WORKING/remote-agent-pipeline.md."
        )

    # 5. Dry-run mode: report the plan and return without rsyncing.
    if dry_run:
        if not changes:
            status = "no drift"
        elif first_launch:
            status = "first launch"
        else:
            status = "drift overridden by --force"
        click.echo(
            f"[dispatch] dry-run for {name!r} -> {peer!r}: "
            f"{status}; {len(changes)} file change(s) planned."
        )
        return 0

    # 6. Actual rsync (no --dry-run).
    rsync_real_argv = [
        "rsync",
        "-acv",
        "--delete",
        *exclude_args,
        f"{src_dir!s}/",
        remote_target,
    ]
    rsync_real = subprocess.run(
        rsync_real_argv,
        capture_output=True,
        text=True,
        check=False,
    )
    if rsync_real.returncode != 0:
        raise RuntimeError(
            f"rsync failed against {peer!r} (rc={rsync_real.returncode}):\n"
            f"argv: {' '.join(shlex.quote(a) for a in rsync_real_argv)}\n"
            f"stderr:\n{rsync_real.stderr}"
        )

    # 7. Step 4 stops here — ssh + remote sac start + JSON parse + lead-side
    # instances row is the next step. Loud failure preserves the no-silent-
    # stubs rule from feedback_no_silent_stubs memory.
    raise NotImplementedError(
        f"_dispatch_remote_start: rsync to {peer!r} complete for {name!r}, "
        f"but the remote `sac agents start {name}` invocation is step 4. "
        f"See ~/proj/scitex-lead/GITIGNORED/WORKING/remote-agent-pipeline.md."
    )


def try_dispatch(
    config: AgentConfig,
    current_host: str,
    peers: Mapping[str, "PeerSpec"],
    *,
    dry_run: bool,
    force: bool,
) -> bool:
    """Route ``config`` to a remote peer when its ``spec.host`` demands it.

    Returns ``True`` when the start was dispatched (the caller should
    ``continue`` the per-target loop).  Returns ``False`` when local
    execution should proceed — either because ``spec.host`` is unset,
    equals the current host, or names an unknown host (the resolver
    yields None and the singleton-skip logic downstream decides what
    to do).

    The body change (drift check + rsync + NotImplementedError("step 4"))
    lands in step 3b atop this extraction.
    """
    spec_host = config.hosts_spec.host
    if isinstance(spec_host, list):
        target_host = spec_host[0] if spec_host else None
    else:
        target_host = spec_host or None
    dispatch_peer = _resolve_dispatch_peer(
        target_host=target_host,
        current_host=current_host,
        peers=peers,
    )
    if dispatch_peer is None:
        return False
    _dispatch_remote_start(
        name=config.name,
        peer=dispatch_peer,
        dry_run=dry_run,
        force=force,
    )
    return True


__all__ = ["_dispatch_remote_start", "try_dispatch"]
