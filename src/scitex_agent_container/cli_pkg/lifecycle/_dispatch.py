#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Cross-host dispatch for ``sac agents start``.

End-to-end implementation of the cross-host pipeline (see
``~/proj/scitex-lead/GITIGNORED/WORKING/remote-agent-pipeline.md``).
The routing branch in ``_start.py`` delegates to :func:`try_dispatch`,
which asks the pure resolver in ``_common.classify_dispatch_host`` to map
the concrete ``spec.host`` to local / remote / unknown. A ``remote``
classification calls :func:`_dispatch_remote_start` to:

  * drift-check via ``rsync --dry-run --itemize-changes``,
  * rsync the spec dir to the peer,
  * invoke ``sac agents start <name> --no-redispatch --json`` over ssh
    (env_preamble-aware via :func:`build_ssh_argv`), and
  * write a lead-side ``state.db.instances`` row so cross-host
    listings see the remote agent immediately.

Keeping this code in a sibling module — rather than appending to
``_start.py`` — preserves the per-file 512-line cap and isolates the
dispatch logic for review.
"""

from __future__ import annotations

import shlex
import subprocess
from collections.abc import Mapping
from typing import TYPE_CHECKING

import click

from ...config import AgentConfig
from ._common import _local_host_names
from ._dispatch_paths import local_spec_dir, remote_spec_target

if TYPE_CHECKING:
    from collections.abc import Collection

    from ..._state.host_config import PeerSpec


def _spawned_by() -> str:
    """Launching identity for the lineage edge (Rule B/D).

    The host that runs ``sac agents start`` and dispatches cross-host is
    the spawn parent. A parent AGENT shelling out carries ``SAC_NAME``
    in its env (recorded as ``spawned_by=<parent>``); a bare lead /
    operator dispatch has none and records ``"cli"``.
    """
    from ..._env import getenv

    return getenv("NAME") or "cli"


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

    Step 4 implementation: locate the local spec dir, drift-check via
    ``rsync --dry-run --itemize-changes`` (content-checksum mode),
    rsync the spec when the drift gate allows it, then invoke
    ``sac agents start <name> --no-redispatch --json`` on the peer
    over ssh (env_preamble-aware via :func:`build_ssh_argv`), parse
    the resulting JSON, and write a lead-side ``state.db.instances``
    row so cross-host listings see the new agent immediately.

    Args:
        name: Agent name (used as both spec-dir basename and remote
            ``sac agents start`` argument).
        peer: ssh alias resolved from ``~/.ssh/config``. The matching
            ``ssh:`` field in ``~/.scitex/agent-container/config.yaml``
            is the source of this alias.
        dry_run: When True, print the planned change count and return
            0 without performing the actual rsync.
        force: When True, override the drift gate and rsync anyway.

    Raises:
        FileNotFoundError: When the local spec dir for ``name`` does
            not exist under ``~/.scitex/agent-container/agents/``.
        RuntimeError: When ``rsync --dry-run`` fails, when drift is
            detected without ``--force``, when the real rsync fails,
            when the remote ``sac agents start`` returns non-zero,
            or when its stdout is not valid JSON.
    """
    # 1. Locate the local spec dir ($SCITEX_DIR-aware — see _dispatch_paths).
    src_dir = local_spec_dir(name)
    if not src_dir.is_dir():
        raise FileNotFoundError(
            f"Spec dir for {name!r} not found on lead at {src_dir!s}. "
            f"Create the spec locally before dispatching to {peer!r}."
        )

    # 2. rsync --dry-run --itemize-changes (content-checksum mode).
    # The destination root comes from the host REGISTRY (the SSOT port), not
    # from the remote's ``~/.scitex`` — see :mod:`._dispatch_paths` for the
    # measured Spartan incident that makes this mandatory.
    from ..._state.host_config import load as _load_host_config_for_root

    remote_target = remote_spec_target(name, peer, _load_host_config_for_root().peers)
    exclude_args = [
        "--exclude=runtime/",
        "--exclude=__pycache__/",
        "--exclude=.pytest_cache/",
        "--exclude=_sphinx_html/",
    ]
    # Drive rsync's ssh transport with the same TOFU policy we use for
    # bare ssh (build_ssh_argv): accept-new lets the first-touch peer
    # be added to known_hosts, but rejects any later key change. Without
    # ``-e``, rsync would invoke ssh with whatever the user's defaults
    # are, which on a fresh peer surfaces as a silent rc-1 from rsync.
    rsync_ssh_opt = "ssh -o StrictHostKeyChecking=accept-new -o BatchMode=yes"
    rsync_dry_argv = [
        "rsync",
        "-acvn",  # archive + checksum + verbose + dry-run
        "-e",
        rsync_ssh_opt,
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
    # head (position 0 is one of "<>ch*."; position 1 is one of "fdLD");
    # first-launch rows are all-plus; drift rows use letters; deletes start
    # with "*deleting".  rsync also emits informational lines that are NOT
    # itemize records — "sending incremental file list" (header), "created
    # directory <path>" (top-level dir creation notice), "sent N bytes ..."
    # (footer), "total size is ..." (footer).  We must keep only true
    # itemize records.
    _ITEMIZE_OP_CHARS = set("<>ch*.")
    itemized = []
    for line in rsync_dry.stdout.splitlines():
        if not line or line.startswith((" ", "sending", "sent", "total", "created")):
            continue
        # A true itemize record starts with one of <>ch*. — informational
        # lines (e.g. "Number of files: ...") will not match.
        if len(line) < 11 or line[0] not in _ITEMIZE_OP_CHARS:
            continue
        itemized.append(line)
    changes = itemized
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

    # 6. Actual rsync (no --dry-run). Same TOFU ssh transport as the
    # dry-run above so the real handoff also accepts a first-touch
    # peer key.
    rsync_real_argv = [
        "rsync",
        "-acv",
        "-e",
        rsync_ssh_opt,
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

    # 7. Step 4: invoke remote-side `sac agents start --no-redispatch --json`
    # over ssh, parse the JSON, write the lead-side instances row.
    # env_preamble is honoured by build_ssh_argv (bash -lc wrapper).
    import json as _json

    from ..._state.host_config import build_ssh_argv
    from ..._state._remote_sac_hint import remote_sac_not_found_hint
    from ..._state.host_config import load as _load_host_config
    from ..._state.state_db import record_instance_start

    peers_map = _load_host_config().peers
    # NOTE: the remote process's state root (``SCITEX_DIR=<registry root>``)
    # is pinned inside ``build_ssh_argv`` — the single choke point every
    # remote-sac invocation funnels through — so it is NOT injected here.
    # See ``_state/_host_ssh._scitex_dir_prefix``.
    ssh_argv = build_ssh_argv(
        peer,
        ["sac", "agents", "start", name, "--no-redispatch", "--json"],
        peers_map,
    )
    ssh_result = subprocess.run(
        ssh_argv,
        capture_output=True,
        text=True,
        check=False,
    )
    if ssh_result.returncode != 0:
        raise RuntimeError(
            f"Remote `sac agents start {name}` failed on {peer!r} "
            f"(rc={ssh_result.returncode}):\n"
            f"argv: {' '.join(shlex.quote(a) for a in ssh_argv)}\n"
            f"stdout:\n{ssh_result.stdout}\n"
            f"stderr:\n{ssh_result.stderr}"
            + remote_sac_not_found_hint(
                peer, ssh_result.returncode, ssh_result.stderr, peers_map
            )
        )
    try:
        peer_state = _json.loads(ssh_result.stdout)
    except _json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Remote `sac agents start {name}` on {peer!r} returned "
            f"non-JSON stdout:\n"
            f"stdout (first 500 chars):\n{ssh_result.stdout[:500]}\n"
            f"json error: {exc}"
        ) from exc

    # Lead-side instances row — mirrors the canonical
    # record_instance_start API used elsewhere. host=peer so cross-host
    # listings see the remote agent; a2a_port comes from the peer's
    # --json output (None when sidecar is disabled). This is the BOUND
    # port the peer's allocator resolved — the crux of the remote-port
    # gap fix: the peer's ``--json`` ``a2a_port`` field carries the
    # concrete int the peer's port allocator claimed (auto -> int
    # happens BEFORE the runtime builds argv, see
    # ``_lifecycle/_a2a_port.py``). Recorded as both ``a2a_port`` (legacy
    # readers) and ``bound_port`` (new readers). ``remote=True`` marks
    # the cross-host locality so ``resolve_peer_url`` / ``agent_status``
    # know to reach the agent on ``peer``. ``spawned_by`` is the
    # launching identity (Rule B/D lineage edge).
    bound = peer_state.get("a2a_port")
    record_instance_start(
        name=name,
        host=peer,
        a2a_port=bound,
        bound_port=bound,
        remote=True,
        spawned_by=_spawned_by(),
    )
    # ADR-0014 Stage 1 — paired comms_nodes row for the cross-host
    # agent so peers resolving via the federated graph (not just the
    # local instances table) see the new placement after the next sync.
    if bound is not None:
        try:
            from ..._state.state_db_nodes import register_comms_node

            register_comms_node(
                name=name,
                host=peer,
                a2a_port=int(bound),
                source_host=None,
            )
        except (
            Exception
        ):  # stx-allow: fallback (reason: never block dispatch on registry write)
            pass
    click.echo(
        f"[dispatch] {name!r} started on {peer!r} "
        f"(a2a_port={peer_state.get('a2a_port')!s}, "
        f"started_at={peer_state.get('started_at')!s})."
    )
    return 0


def try_dispatch(
    config: AgentConfig,
    current_host: str,
    peers: Mapping[str, "PeerSpec"],
    *,
    dry_run: bool,
    force: bool,
    local_names: "Collection[str] | None" = None,
) -> bool:
    """Route ``config`` to a remote peer when its ``spec.host`` demands it.

    Resolves the concrete ``spec.host`` into exactly one of three outcomes
    via :func:`classify_dispatch_host`:

    * **local** — ``spec.host`` is empty/absent, equals the current host, or
      is any spelling in ``local_names`` (the canonical name + aliases
      denoting THIS machine — a ``host: ${HOSTNAME}`` placement already
      resolved to the concrete name at load time). Returns ``False`` so the
      caller proceeds with the UNCHANGED local launch. Because the local
      check precedes the peer table, a machine that is also registered as a
      peer (``ssh: localhost``) is never ssh-dispatched to itself.
    * **remote** — ``spec.host`` names a known peer distinct from this
      machine. Calls :func:`_dispatch_remote_start` for the end-to-end
      handoff (drift-check + rsync + remote ``sac agents start
      --no-redispatch --json`` + lead-side ``state.db.instances`` row) and
      returns ``True``. Only this branch ever reaches ssh.
    * **unknown** — ``spec.host`` names neither this machine nor a peer.
      Raises ``RuntimeError`` with the registered-peer list (the
      ``sac host list`` view) and the concrete fixes — operator directive
      2026-07-10: a placement that cannot be routed is an ERROR, never a
      silent local start on the wrong machine. The historical fall-through
      survives in two explicit forms: ``--no-redispatch`` skips this
      dispatcher entirely (the documented force-local escape, which also
      disarms the singleton skip), and a fallback CHAIN whose tail names
      THIS machine still classifies local (``classify_spec_host_route``).
      The negative-safety guarantee is unchanged — an unknown host never
      becomes an ssh target.

    ``local_names`` defaults to :func:`_local_host_names` (the union of both
    hostname authorities); tests inject an explicit set to keep the routing
    decision pure and hermetic.

    Raises:
        RuntimeError: ``spec.host`` resolves to neither this machine nor a
            registered peer (message body from
            ``_host_routing.format_unknown_host_error``).
    """
    from ._host_routing import classify_spec_host_route, format_unknown_host_error

    spec_host = config.hosts_spec.host
    if local_names is None:
        local_names = _local_host_names(current_host)
    kind, dispatch_peer = classify_spec_host_route(
        spec_host,
        current_host,
        peers,
        local_names=local_names,
    )
    if kind == "unknown":
        head = spec_host[0] if isinstance(spec_host, list) else spec_host
        raise RuntimeError(
            format_unknown_host_error(config.name, str(head), peers, verb="start")
        )
    # ONLY a resolved remote peer triggers ssh dispatch; "local" returns
    # False so the caller proceeds with the unchanged local launch.
    if kind != "remote" or dispatch_peer is None:
        return False
    _dispatch_remote_start(
        name=config.name,
        peer=dispatch_peer,
        dry_run=dry_run,
        force=force,
    )
    return True


def lookup_remote_peer(name: str) -> tuple[str, dict] | None:
    """Look up the active instance row for ``name``; return (peer, row) when remote.

    Returns:
        * ``None`` when no active row exists, or the row lives on the
          current host (caller proceeds locally).
        * ``(peer_name, row_dict)`` when the row's ``host`` differs from
          the current canonical hostname — the verb-specific caller is
          expected to dispatch via ssh to ``peer_name``.

    The row dict mirrors the ``instances`` table columns (id, name,
    host, a2a_port, started_at, ended_at, ...). Callers care about
    ``host`` and ``a2a_port`` mostly.

    Resolution chain for current_host matches ``state_db._resolve_host``
    (env override → ``host.canonical`` → ``host.aliases`` → ``hostname -s``),
    so a row written under one alias is matched when the same alias is
    set on this run.

    Failure modes (state.db missing, schema not yet created) raise
    ``RuntimeError`` rather than degrading silently — a missing
    instances row is a legitimate "not running" signal, but a missing
    database when one was expected is a configuration error.
    """
    from ..._state.state_db import _resolve_host, list_active_instances

    rows = list_active_instances()
    matching = [r for r in rows if r.get("name") == name]
    if not matching:
        return None
    # Latest row wins (started_at DESC order from list_active_instances).
    row = matching[0]
    peer = str(row.get("host") or "")
    if not peer:
        return None
    current_host = _resolve_host(None)
    if peer == current_host:
        return None
    return peer, row


def try_dispatch_remote(
    name: str,
    verb: str,
    peers: Mapping[str, "PeerSpec"],
    *,
    handler,
) -> bool:
    """Generic cross-host dispatcher driven by ``state.db.instances``.

    Used by ``stop`` / ``tail`` / ``send`` / ``delete`` to route a
    lifecycle command to the peer that holds the agent's active
    instance row. ``start`` uses the sibling :func:`try_dispatch`
    helper because its routing is driven by ``spec.host`` (the row
    doesn't exist yet at start time).

    Args:
        name: Agent name to look up in ``state.db.instances``.
        verb: Human-readable verb used in error messages (e.g. ``"stop"``).
        peers: Result of ``host_config.load().peers``. The looked-up
            peer name MUST appear in this mapping; otherwise the
            handoff fails loudly (operator's ``peers.yaml`` is missing
            an entry).
        handler: Callable ``handler(peer_name, row_dict, peers)`` that
            performs the actual ssh / HTTP work. Called only when the
            row is remote; raises propagate.

    Returns:
        * ``False`` when no active row exists for ``name`` OR the row
          lives on the current host — caller proceeds with local
          handling.
        * ``True`` when ``handler`` was dispatched successfully.

    Raises:
        RuntimeError: When the resolved peer is not in ``peers``
            (the lead's peers.yaml needs the entry).
    """
    found = lookup_remote_peer(name)
    if found is None:
        return False
    peer, row = found
    if peer not in peers:
        raise RuntimeError(
            f"Agent {name!r} active on peer {peer!r} per state.db, but "
            f"{peer!r} is NOT in ~/.scitex/agent-container/config.yaml's "
            f"peers: section. Cannot {verb} cross-host without an ssh "
            f"target. Add the peer entry and retry."
        )
    handler(peer, row, peers)
    return True


__all__ = [
    "_dispatch_remote_start",
    "lookup_remote_peer",
    "try_dispatch",
    "try_dispatch_remote",
]
