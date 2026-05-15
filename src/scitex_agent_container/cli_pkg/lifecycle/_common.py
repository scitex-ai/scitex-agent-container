#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shared helpers for lifecycle commands.

Holds module-level helpers used by more than one verb in the
``lifecycle/`` package — agent discovery, singleton host-skip logic,
and the foreground-tail multiplexer.
"""

from __future__ import annotations

from pathlib import Path

import click

from ...config import AgentConfig

_SKIP_DIR_NAMES = {"legacy-agents", "shared", "GITIGNORED"}


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
         for downstream orchestrators (e.g. orochi) to inject extra paths
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
    "_singleton_skip_reason",
    "_iter_agent_yamls",
    "_discover_all_agents",
    "_multiplex_foreground_tails",
]
