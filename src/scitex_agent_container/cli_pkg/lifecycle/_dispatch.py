#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Cross-host dispatch for ``sac agents start``.

End-to-end implementation of the cross-host pipeline (see
``~/proj/scitex-lead/GITIGNORED/WORKING/remote-agent-pipeline.md``).
The routing branch in ``_start.py`` delegates to :func:`try_dispatch`,
which asks the pure resolver in ``_common.classify_dispatch_host`` to map
the concrete ``spec.host`` to local / remote / unknown. A ``remote``
classification calls :func:`_dispatch_remote_start` to:

  * drift-check by comparing content digests with the peer,
  * ship the spec dir and VERIFY it landed (see :mod:`._spec_handoff`,
    which documents why an rsync exit code is not evidence of delivery),
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
from ._dispatch_paths import local_spec_dir, remote_spec_dir
from ._spec_handoff import (
    local_manifest,
    plan_handoff,
    push_spec_dir,
    read_remote_manifest,
    ssh_runner,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Collection

    from ..._state.host_config import PeerSpec
    from ._host_chain import ReachabilityOracle


def _spawned_by() -> str:
    """Launching identity for the lineage edge (Rule B/D).

    The host that runs ``sac agents start`` and dispatches cross-host is
    the spawn parent. A parent AGENT shelling out carries ``SAC_NAME``
    in its env (recorded as ``spawned_by=<parent>``); a bare lead /
    operator dispatch has none and records ``"cli"``.
    """
    from ..._env import getenv

    return getenv("NAME") or "cli"


def _dispatch_remote_start(
    name: str,
    peer: str,
    *,
    dry_run: bool = False,
    force: bool = False,
) -> int:
    """Dispatch ``sac agents start <name>`` to a remote ``peer``.

    Step 4 implementation: locate the local spec dir, drift-check by
    comparing per-file digests with the peer, ship the spec when the
    drift gate allows it and verify it actually landed, then invoke
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
            0 without shipping anything.
        force: When True, override the drift gate and ship anyway.

    Raises:
        FileNotFoundError: When the local spec dir for ``name`` does
            not exist under ``~/.scitex/agent-container/agents/``.
        RuntimeError: When the peer's manifest cannot be read, when
            drift is detected without ``--force``, when the transfer
            fails OR SILENTLY MIS-DELIVERS (post-transfer digests do
            not match), when the remote ``sac agents start`` returns
            non-zero, or when its stdout is not valid JSON.
    """
    # 1. Locate the local spec dir ($SCITEX_DIR-aware — see _dispatch_paths).
    src_dir = local_spec_dir(name)
    if not src_dir.is_dir():
        raise FileNotFoundError(
            f"Spec dir for {name!r} not found on lead at {src_dir!s}. "
            f"Create the spec locally before dispatching to {peer!r}."
        )

    # 2. Compare content digests with the peer. The destination root comes
    # from the host REGISTRY (the SSOT port), not from the remote's
    # ``~/.scitex`` — see :mod:`._dispatch_paths` for the measured Spartan
    # incident that makes this mandatory.
    from ..._state.host_config import load as _load_host_config_for_root

    peers_for_root = _load_host_config_for_root().peers
    remote_dir = remote_spec_dir(name, peer, peers_for_root)
    peer_shell = ssh_runner(peer, peers_for_root)
    plan = plan_handoff(
        local_manifest(src_dir),
        read_remote_manifest(remote_dir, peer_shell),
    )

    # 3. Drift gate — error unless --force was passed. Only a file the peer
    # holds with DIFFERENT content is drift; peer-only files are reported at
    # step 5 and kept (see :mod:`._spec_handoff` on dropping ``--delete``).
    if plan.drift and not force:
        raise RuntimeError(
            f"Spec drift between lead and {peer!r} for agent {name!r} — the "
            f"peer's copy of these files differs from the lead's:\n"
            + "\n".join(f"  {rel}" for rel in plan.changed)
            + "\n\nResolve manually then re-run, "
            + "or pass --force to overwrite peer-side from lead. "
            + "See ~/proj/scitex-lead/GITIGNORED/WORKING/remote-agent-pipeline.md."
        )

    # 4. Dry-run mode: report the plan and return without shipping anything.
    if dry_run:
        if not plan.new and not plan.changed:
            status = "no drift"
        elif plan.first_launch:
            status = "first launch"
        elif plan.changed:
            status = "drift overridden by --force"
        else:
            status = "new files only"
        click.echo(
            f"[dispatch] dry-run for {name!r} -> {peer!r} at {remote_dir}: "
            f"{status}; {plan.summary()}."
        )
        return 0

    # 5. Deliver — and PROVE delivery by re-reading the peer's own digests.
    # An exit code is not evidence: a vendor-patched transport can exit 0
    # having written the spec somewhere nobody reads, after which the remote
    # `sac agents start` below would boot the agent from the STALE spec and
    # this dispatch would report success. push_spec_dir raises instead.
    push_spec_dir(src_dir, remote_dir, peer_shell, peer=peer)
    if plan.extra:
        click.echo(
            f"[dispatch] {len(plan.extra)} peer-only file(s) on {peer!r} kept "
            f"(the handoff never deletes): {', '.join(plan.extra)}"
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
    reachability: "ReachabilityOracle | None" = None,
    dispatcher: "Callable[..., int] | None" = None,
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
    * **unknown** — nothing in ``spec.host`` is usable: a name that resolves
      to neither this machine nor a peer, or a fallback CHAIN whose every
      candidate was rejected. Raises ``RuntimeError`` with the registered-peer
      list (the ``sac host list`` view) and the concrete fixes — operator
      directive 2026-07-10: a placement that cannot be routed is an ERROR,
      never a silent local start on the wrong machine. For a chain the message
      names EVERY candidate and why it was rejected (unreachable vs unknown
      name), because those have different fixes. The historical fall-through
      survives in two explicit forms: ``--no-redispatch`` skips this
      dispatcher entirely (the documented force-local escape, which also
      disarms the singleton skip), and a chain whose tail names THIS machine
      still classifies local. The negative-safety guarantee is unchanged — an
      unknown host never becomes an ssh target.

    A LIST ``spec.host`` is walked in priority order with each remote
    candidate probed by one bounded ssh round-trip, so a head answering
    ``Permission denied (publickey)`` degrades to the next entry instead of
    taking the agent down. A plain STRING is never probed and routes exactly
    as it always has. The walk itself lives in
    :func:`._host_routing.resolve_start_dispatch_peer`.

    ``local_names`` defaults to :func:`_local_host_names`, ``reachability`` to
    the real ssh prober (chains only), ``dispatcher`` to
    :func:`_dispatch_remote_start` — injection seams so tests exercise the
    routing decision hermetically, with no network and no PATH shim.

    Raises:
        RuntimeError: ``spec.host`` resolves nowhere usable (message body from
            ``_host_routing.format_route_error``).
    """
    from ._host_routing import resolve_start_dispatch_peer

    if local_names is None:
        local_names = _local_host_names(current_host)
    # ONLY a resolved remote peer triggers ssh dispatch; None means local, so
    # the caller proceeds with the unchanged local launch.
    dispatch_peer = resolve_start_dispatch_peer(
        config.name,
        config.hosts_spec.host,
        current_host,
        peers,
        local_names=local_names,
        reachability=reachability,
    )
    if dispatch_peer is None:
        return False
    try:
        (dispatcher or _dispatch_remote_start)(
            name=config.name,
            peer=dispatch_peer,
            dry_run=dry_run,
            force=force,
        )
    except Exception:
        # The failure is re-raised UNCHANGED — this only adds the sentence the
        # operator needs to attribute it. See :func:`_explain_pinned_hop_failure`.
        _explain_pinned_hop_failure(
            config.name, config.hosts_spec.host, dispatch_peer
        )
        raise
    return True


def _explain_pinned_hop_failure(
    name: str,
    spec_host: str | list[str] | None,
    peer: str,
) -> None:
    """Name ``spec.host`` when a dispatch driven by it fails.

    MEASURED 2026-08-09: specs across the fleet carried ``host: ywata-note-win``
    after the laptop was retired. The lifecycle verbs dispatched there and TWELVE
    agents died with ``Permission denied (publickey)`` — a message that names the
    AGENT and never the field that chose the destination. Attributing it took
    days, because nothing in the output connected "this agent will not start" to
    "a line in its spec points at a machine that is gone".

    Why this is a message and not a probe: ``_host_chain.resolve_host_chain``
    deliberately never probes a STRING ``host:`` — only a LIST is probed, where
    the verdict CHOOSES among alternatives. A pin has no alternatives, so a probe
    there could only REFUSE, and refusing an explicit pin on a prober's say-so is
    a worse failure than the one being fixed ("never a licence to reject a host
    the operator asked for"). Probing pre-emptively would also add an ssh
    round-trip to every remote start, to say something the imminent hop is about
    to establish for free.

    So the pin is still obeyed and the error still propagates untouched. What
    changes is that the operator is no longer left to guess WHY this peer.
    """
    if not isinstance(spec_host, str) or not spec_host:
        # A LIST was already walked and probed candidate-by-candidate, and its
        # own error accounts for every entry; an empty pin never routes remote.
        return
    from .._helpers._console import system_msg

    system_msg(
        f"{name}: this hop was chosen by `host: {spec_host}` in the agent's "
        f"spec — sac dispatched to peer {peer!r} because of that line, and the "
        "hop failed (the error below is the peer's, unmodified). A plain "
        "`host:` pin is never reachability-probed, so a pin at a machine that "
        "is retired, asleep, or no longer accepting this key fails HERE, with "
        "a message that names the agent rather than the spec. If "
        f"{spec_host} is not where this agent should run, correct or remove "
        "`host:` — an absent `host:` means 'start on whichever machine runs "
        "the command'.",
        style="warn",
    )


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
