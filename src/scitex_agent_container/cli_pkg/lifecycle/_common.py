#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shared helpers for lifecycle commands.

Holds module-level helpers used by more than one verb in the
``lifecycle/`` package — agent discovery, singleton host-skip logic,
and the foreground-tail multiplexer.
"""

from __future__ import annotations

import socket
from collections.abc import Collection, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Callable

import click

from ...config import AgentConfig

if TYPE_CHECKING:
    from ..._state.host_config import PeerSpec

# (name, host) -> True iff the registry holds an active instances row
# for ``name`` on ``host``. Used by :func:`_resolve_singleton_skip` to
# liveness-gate the spec-host preference.
BoundHostLivenessOracle = Callable[[str, str], bool]

_SKIP_DIR_NAMES = {"legacy-agents", "shared", "GITIGNORED"}


def classify_dispatch_host(
    target_host: str | None,
    current_host: str,
    peers: Mapping[str, "PeerSpec"],
    *,
    local_names: Collection[str] = (),
) -> tuple[str, str | None]:
    """Classify a concrete ``spec.host`` into local / remote / unknown.

    Pure resolver — never raises, never logs, never reads files (the
    caller supplies ``local_names`` and ``peers``). This is the operator's
    "resolution layer": a concrete canonical hostname is mapped to WHERE
    that host is, so ``host: <this-machine>`` launches locally and
    ``host: <peer>`` dispatches over ssh.

    Returns a ``(kind, peer)`` tuple:

    * ``("local", None)``  — run on the caller. Fires when ``target_host``
      is unset (empty ``host:`` / absent, normalized to ``""`` → ``None``),
      equals ``current_host``, or is any spelling in ``local_names``
      (the canonical name + aliases that denote THIS machine per
      ``host_config``). LOCAL is checked BEFORE the peer table so a machine
      that is ALSO registered as a peer (e.g. ``ywata-note-win: {ssh:
      localhost}`` so remote hosts can reach it) is never ssh-dispatched
      to itself.
    * ``("remote", <peer>)`` — dispatch to that peer over ssh. Fires when
      ``target_host`` is a known peer key distinct from the local machine
      (glob peer entries like ``spartan-*`` match here via ``PeersMap``).
    * ``("unknown", None)`` — ``target_host`` names neither the local
      machine nor a peer. This classifier stays a PURE resolver and never
      raises; the REACTION is the caller's. Since operator directive
      2026-07-10 the lifecycle dispatchers fail LOUD on it
      (``_host_routing.format_unknown_host_error`` — peer list + fixes)
      instead of silently falling through to a local start; either way an
      unknown host is never routed to ssh.
    """
    if target_host is None:
        return ("local", None)
    if target_host == current_host or target_host in local_names:
        return ("local", None)
    if target_host in peers:
        return ("remote", target_host)
    return ("unknown", None)


def _resolve_dispatch_peer(
    target_host: str | None,
    current_host: str,
    peers: Mapping[str, "PeerSpec"],
    *,
    local_names: Collection[str] = (),
) -> str | None:
    """Return the peer name to dispatch to, or None for local execution.

    Thin wrapper over :func:`classify_dispatch_host` preserving the historic
    "peer-name-or-None" contract used by :func:`try_dispatch`. Returns a peer
    name only for a ``remote`` classification; both ``local`` and ``unknown``
    yield ``None`` (the caller decides what an unknown host means). With the
    default empty ``local_names`` the behaviour is byte-identical to the
    pre-hardening resolver — the alias-of-self short-circuit only engages
    when the caller passes the machine's local spellings.
    """
    _kind, peer = classify_dispatch_host(
        target_host, current_host, peers, local_names=local_names
    )
    return peer


def _local_host_names(current_host: str | None = None) -> set[str]:
    """Return every host spelling that denotes THIS machine.

    Unions the two hostname authorities so ``host: <canonical-or-alias>``
    resolves to a LOCAL launch regardless of which registry the operator
    configured, and — critically — regardless of drift between them:

      * ``host_config`` (F-CS12 ``~/.scitex/agent-container/config.yaml``):
        ``canonical_host()`` plus the ``host.aliases`` entry keyed by this
        machine's short hostname.
      * ``config/_host.resolve_hostname()`` (the value dispatch already
        passes as ``current_host``) and the bare ``socket`` short hostname.

    Only the alias entry for THIS machine's short hostname is included, so a
    peer machine's alias is never mistaken for local. Best-effort — a
    missing / broken config degrades to the short hostname (plus
    ``current_host`` when supplied); it never raises.
    """
    names: set[str] = set()
    if current_host:
        names.add(current_host)
    short = socket.gethostname().split(".")[0]
    if short:
        names.add(short)
    # config/_host resolver (env override → spec.hostname_aliases → short).
    try:
        from ...config._host import resolve_hostname

        rn = resolve_hostname()
        if rn:
            names.add(rn)
    except Exception:  # stx-allow: fallback (reason: hostname resolution must never block dispatch; short hostname already captured)
        pass
    # host_config F-CS12 registry (env override → host.canonical → aliases).
    try:
        from ..._state.host_config import load as _load_host_config

        cfg = _load_host_config()
        canonical = cfg.canonical_host()
        if canonical:
            names.add(canonical)
        alias = cfg.host.aliases.get(short)
        if alias:
            names.add(alias)
    except Exception:  # stx-allow: fallback (reason: absent/malformed config.yaml must not break the local-vs-remote decision; the two hostname sources above suffice)
        pass
    return {n for n in names if n}


def _bound_host(config: AgentConfig) -> str | None:
    """Return the spec-bound preferred host for a singleton config.

    Mirrors :func:`_singleton_skip_reason`'s head-of-chain selection —
    the v3 ``spec.host`` (str or first element of list) wins, then the
    v2 ``scheduling.preferred_host`` fallback. Returns None when the
    config is multi-instance or unpinned (no skip would fire and no
    liveness check is meaningful).
    """
    spec = config.hosts_spec
    if spec.hosts:
        return None
    host = spec.host
    if host:
        if isinstance(host, str):
            return host or None
        return host[0] if host else None
    sched = config.scheduling
    if sched.mode != "singleton":
        return None
    return sched.preferred_host or None


def _registry_active_on(name: str, host: str) -> bool:
    """Default bound-host liveness oracle: True iff the lead-side
    ``instances`` table holds an active row for ``name`` on ``host``.

    Reads :func:`state_db.list_active_instances` (host-unfiltered) and
    matches name+host exactly. Any failure (state.db missing, schema
    mismatch, OS error) yields False — "no evidence of liveness" —
    which is the conservative answer for the caller
    (:func:`_resolve_singleton_skip` will treat the spec-host pin as
    stale and fall through to a local start, breaking the stale-binding
    dead end the lead's bm025 repro identified).
    """
    # stx-allow: fallback (reason: state.db read failure is treated as
    # "no liveness evidence" so the stale spec-host binding is released
    # and the start path proceeds; this is the conservative answer that
    # never blocks)
    try:
        from ..._state.state_db import list_active_instances

        rows = list_active_instances(host=None)
    except Exception:
        return False
    for row in rows or ():
        if row.get("name") == name and row.get("host") == host:
            return True
    return False


def _resolve_singleton_skip(
    config: AgentConfig,
    hostname: str,
    *,
    no_redispatch: bool,
    liveness_oracle: BoundHostLivenessOracle | None = None,
) -> str | None:
    """Liveness-gated wrapper around :func:`_singleton_skip_reason`.

    Returns ``None`` (no skip — start locally) in any of:

      1. ``no_redispatch=True`` — operator explicitly disabled the
         redispatch chain (e.g. via ``sac --on <peer>`` which propagates
         ``--no-redispatch`` to the remote ``sac agents start``). With
         redispatch off there is nowhere else for the skip to defer to,
         so honouring it would produce a silent no-op the propagator
         then drops on the floor. (PR #252 / Bug 1.)
      2. The singleton check itself yields no skip (host matches, or
         multi-instance config).
      3. The singleton check WOULD fire, but the liveness oracle reports
         no active instance row on the bound host — the spec-host pin
         is stale. The lead's bm025 repro (clew pinned to a dead prior
         host with no live row + ``pam_slurm_adopt`` rejecting any ssh
         to it) hung clew because the skip kept deferring to a host
         that had nothing live and was unreachable. Falling through
         lets the operator's actual target take over.

    Returns the human-readable skip reason ONLY when the singleton
    check fires AND the agent has a verified-live row on the bound
    host (the legitimate "it IS running over there, defer" case).

    Args:
        config: Agent spec.
        hostname: Current host's resolved canonical name.
        no_redispatch: Operator's ``--no-redispatch`` flag (when True,
            never skip).
        liveness_oracle: Optional ``(name, host) -> bool`` seam for
            checking whether the agent is already active on its bound
            host. Defaults to :func:`_registry_active_on` (real state.db
            read).
    """
    if no_redispatch:
        return None
    reason = _singleton_skip_reason(config, hostname)
    if reason is None:
        return None
    bound = _bound_host(config)
    if bound is None:
        # No explicit pin → no liveness check can apply; preserve
        # behaviour.
        return reason
    oracle = liveness_oracle if liveness_oracle is not None else _registry_active_on
    if not oracle(config.name, bound):
        # Spec says "should be on X" but the registry has no live row
        # on X. Skipping would leave the agent unlaunched anywhere —
        # fall through and start locally instead. This is the lead's
        # bm025 stale-binding repro.
        return None
    return reason


def _singleton_skip_reason(config: AgentConfig, hostname: str) -> str | None:
    """Return a human-readable skip reason if ``config`` is a singleton on
    the wrong host, else None.

    Multi-instance (``hosts:`` set or per-host scheduling) never skips.
    Singleton with no host preference launches anywhere. Singleton with
    ``host:``/``preferred-host`` set skips when the current host doesn't match.
    """
    # v3 config: use hosts_spec
    spec = config.hosts_spec
    if spec.hosts:  # multi-instance
        return None
    host = spec.host
    if host:
        chain = [host] if isinstance(host, str) else list(host)
        if not chain or hostname == chain[0]:
            return None
        if hostname in chain[1:]:
            return None
        fallback_str = (
            f" (fallback-hosts: {', '.join(chain[1:])})" if len(chain) > 1 else ""
        )
        return f"singleton prefers '{chain[0]}', current host is '{hostname}'{fallback_str}"
    # v2 config: use scheduling spec
    sched = config.scheduling
    if sched.mode != "singleton":
        return None
    if not sched.preferred_host:
        return None
    if sched.preferred_host == hostname:
        return None
    fallback = (
        f" (fallback-hosts: {', '.join(sched.fallback_hosts)})"
        if sched.fallback_hosts
        else ""
    )
    return (
        f"singleton pinned to '{sched.preferred_host}', "
        f"current host is '{hostname}'{fallback}"
    )


def _iter_agent_yamls(agents_dir: "Path") -> "list[tuple[str, str]]":
    """Yield ``(name, yaml_path)`` for each agent subdir in ``agents_dir``.

    Skips hidden dirs (``.`` / ``_``) and reserved names. Uses the
    ``<agent>/<agent>.yaml`` convention; ``.yml`` is also accepted.
    """
    results: list[tuple[str, str]] = []
    if not agents_dir.exists():
        return results
    for d in sorted(agents_dir.iterdir()):
        if not d.is_dir():
            continue
        if d.name.startswith((".", "_")):
            continue
        if d.name in _SKIP_DIR_NAMES:
            continue
        for ext in (".yaml", ".yml"):
            candidate = d / f"{d.name}{ext}"
            if candidate.exists():
                results.append((d.name, str(candidate)))
                break
    return results


def _discover_all_agents(project_local_dirs=None) -> list[str]:
    """Find all agent YAML files via sac's standard search chain.

    Search locations (earlier wins on name collision):
      0. **Project-local** — first ``.scitex/agent-container/agents/``
         found walking upward from cwd. Highest priority so checked-in
         test agents and CI fixtures override globals.
      1. ``~/.scitex/agent-container/agents/<name>/spec.yaml`` — sac's own root.
      2. ``$SCITEX_AGENT_CONTAINER_YAML_DIRS`` — plugin port (colon-separated)
         for downstream orchestrators to inject extra paths
         without sac knowing about them.

    sac is standalone: it never reads from any other scitex package's
    state directory. Returned paths are sorted by agent name for stable
    ordering.
    """
    from pathlib import Path

    from ..._env import getenv as _sac_env

    if project_local_dirs is None:
        from ...config._resolve import _project_local_dirs as _default_local

        project_local_dirs = _default_local

    # name -> yaml path; later writes are ignored (earlier = higher priority).
    found: dict[str, str] = {}

    home = Path.home()
    primary = home / ".scitex" / "agent-container" / "agents"
    # Project-local first (so an in-repo test agent wins over a stale
    # global with the same name), then home root, then env-port.
    search_dirs: list[Path] = list(project_local_dirs())
    search_dirs.append(primary)

    env_raw = _sac_env("YAML_DIRS", "")
    for p in env_raw.split(":"):
        p = p.strip()
        if p:
            search_dirs.append(Path(p).expanduser())

    for src_dir in search_dirs:
        for name, yaml_path in _iter_agent_yamls(src_dir):
            if name not in found:
                found[name] = yaml_path

    return [found[name] for name in sorted(found)]


def _multiplex_foreground_tails(names, sleeper=None):
    """Tail each agent's session.jsonl with a ``[<name>]`` line-prefix
    until every heartbeat reports "stopping" (or Ctrl-C).

    Best-effort: missing files are tolerated (the runner may not have
    written session.jsonl yet); any IO error swallowed so one agent's
    failure doesn't kill the multiplexer.
    """
    import json as _json
    import time as _time
    from pathlib import Path as _Path

    if sleeper is None:
        sleeper = _time.sleep
    root = _Path.home() / ".scitex" / "agent-container" / "runtime"
    # Start at end-of-file for each agent so we tail only NEW turns —
    # otherwise every re-start replays the whole historical session.jsonl
    # and the operator sees the same assistant reply N times.
    offsets: dict = {}
    for n in names:
        p = root / n / "session.jsonl"
        offsets[n] = p.stat().st_size if p.is_file() else 0
    done = {n: False for n in names}

    def _is_stopping(n: str) -> bool:
        # stx-allow: fallback (best-effort heartbeat check; missing file = not stopping)
        try:
            data = _json.loads((root / n / "heartbeat.json").read_text())
            return data.get("state") == "stopping"
        except Exception:
            return False

    try:
        while not all(done.values()):
            any_progress = False
            for n in names:
                if done[n]:
                    continue
                path = root / n / "session.jsonl"
                if not path.is_file():
                    if _is_stopping(n):
                        done[n] = True
                    continue
                # stx-allow: fallback (transcript reads must not crash the multiplexer)
                try:
                    with path.open(encoding="utf-8", errors="replace") as fh:
                        fh.seek(offsets[n])
                        chunk = fh.read()
                        offsets[n] = fh.tell()
                except OSError:
                    chunk = ""
                for line in chunk.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    any_progress = True
                    # stx-allow: fallback (malformed JSONL line — show raw)
                    try:
                        rec = _json.loads(line)
                        kind = rec.get("type", "?")
                        if kind == "assistant":
                            text = (rec.get("text") or rec.get("raw") or "")[:300]
                            click.echo(f"[{n}] [assistant] {text}")
                        elif kind == "result":
                            click.echo(f"[{n}] [result] (turn complete)")
                        elif kind == "error":
                            click.echo(f"[{n}] [error] {rec.get('detail', '')}")
                    except _json.JSONDecodeError:
                        click.echo(f"[{n}] {line[:300]}")
                if _is_stopping(n):
                    done[n] = True
                    click.echo(f"[{n}] (stopped)")
            if not any_progress:
                sleeper(0.5)
    except KeyboardInterrupt:
        click.echo("\n[foreground] interrupted; agents keep running in background.")


__all__ = [
    "_SKIP_DIR_NAMES",
    "BoundHostLivenessOracle",
    "classify_dispatch_host",
    "_local_host_names",
    "_bound_host",
    "_registry_active_on",
    "_resolve_dispatch_peer",
    "_resolve_singleton_skip",
    "_singleton_skip_reason",
    "_iter_agent_yamls",
    "_discover_all_agents",
    "_multiplex_foreground_tails",
]
