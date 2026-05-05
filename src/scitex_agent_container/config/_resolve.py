"""Resolve agent name or path to a config file path."""

from __future__ import annotations

import os
from pathlib import Path
from typing import List, Tuple

_ENV_VAR = "SCITEX_AGENT_CONTAINER_YAML_DIRS"

# Retained for ``runtimes/claude_session.py``; project-local discovery
# itself delegates to ``scitex_config._ecosystem.local_state``.
_PROJECT_LOCAL_MAX_DEPTH = 10


def _project_local_dirs(start: Path | None = None) -> List[Path]:
    """Return ``[<scope>/agents]`` for the closest project scope, or [].

    Uses ``scitex_config._ecosystem.local_state.find_project_scope`` —
    walks upward from ``start`` (default: cwd) looking for a git repo
    that contains ``.scitex/agent-container/``. This matches the
    ecosystem-wide convention (slurm scripts, hpc reservations, etc.
    all use the same scope resolution) so per-package state lands in
    one predictable place per repo.

    The git boundary is deliberate: it provides an unambiguous
    "this is a project" marker. Users who want project scope without
    git can simply ``git init``.
    """
    try:
        from scitex_config._ecosystem import local_state
    except Exception:  # stx-allow: fallback (reason: scitex-config is an optional dep — degrade to no project-local discovery rather than break sac install)
        return []
    scope = local_state.find_project_scope("agent-container", start=start)
    if scope is None:
        return []
    agents = scope / "agents"
    return [agents] if agents.is_dir() else []


def _resolve_host() -> str:
    """Return hostname using SCITEX_AGENT_CONTAINER_HOSTNAME or SCITEX_OROCHI_HOSTNAME or gethostname."""
    env = os.environ.get("SCITEX_AGENT_CONTAINER_HOSTNAME", "").strip()
    if env:
        return env
    env = os.environ.get("SCITEX_OROCHI_HOSTNAME", "").strip()
    if env:
        return env
    try:
        import socket

        hn = socket.gethostname()
        return hn.split(".", 1)[0] if hn else ""
    except Exception:
        return ""


def _search_dirs() -> Tuple[Path, List[Path], List[Path]]:
    """Return (primary_dir, env_dirs, fleet_dirs) with ~ expansion.

    Search order (highest priority first):
      0. **Project-local** — first ``.scitex/agent-container/agents/``
         found by walking upward from cwd. Surfaces in ``env_dirs``
         (prepended) so checked-in test agents and CI fixtures win
         over stale globals.
      1. ``~/.scitex/agent-container/agents/`` — sac's own install root.
      2. ``$SCITEX_AGENT_CONTAINER_YAML_DIRS`` — plugin port for external
         orchestrators to extend the search scope without touching sac.
      3. Fleet layout — for each root in
         (~/.scitex/orochi, ~/.dotfiles/src/.scitex/orochi):
             a. ``<root>/<HOST>/agents/`` (host-specific override)
             b. ``<root>/shared/agents/`` (shared default)
             c. ``<root>/agents/`` (legacy flat layout)
    """
    home = Path(os.path.expanduser("~"))
    primary = home / ".scitex" / "agent-container" / "agents"
    env_raw = os.environ.get(_ENV_VAR, "")
    env_dirs = [Path(os.path.expanduser(p)) for p in env_raw.split(":") if p.strip()]
    # Project-local takes priority OVER the home install root: prepend
    # it as the highest-priority entry. Returned via the env_dirs slot
    # but inserted before the existing primary check at the call site
    # below by ordering primary AFTER it in resolve_config().
    env_dirs = _project_local_dirs() + env_dirs

    host = _resolve_host()
    fleet_roots = [
        home / ".scitex" / "orochi",
        home / ".dotfiles" / "src" / ".scitex" / "orochi",
    ]
    fleet_dirs: list[Path] = []
    for root in fleet_roots:
        if host:
            fleet_dirs.append(root / host / "agents")
        fleet_dirs.append(root / "shared" / "agents")
        fleet_dirs.append(root / "agents")
    return primary, env_dirs, fleet_dirs


def _try_dir(base: Path, name: str) -> str | None:
    """Try <base>/<name>.yaml|yml and <base>/<name>/<name>.yaml|yml."""
    for ext in (".yaml", ".yml"):
        cand = base / f"{name}{ext}"
        if cand.exists():
            return str(cand)
        cand = base / name / f"{name}{ext}"
        if cand.exists():
            return str(cand)
    return None


# F-CS10 — smart name resolution
class AmbiguousAgent(LookupError):
    """Raised when a prefix matches multiple agent names."""

    def __init__(self, prefix: str, matches: list[str]):
        self.prefix = prefix
        self.matches = matches
        super().__init__(
            f"agent name '{prefix}' is ambiguous: {', '.join(sorted(matches))}"
        )


def enumerate_agent_names() -> list[str]:
    """Return every agent name discoverable through the standard search.

    A name is recorded when ``<dir>/<name>/<name>.yaml`` exists OR
    ``<dir>/<name>.yaml`` exists for any directory in the search
    chain. Duplicates are collapsed; ordering is alphabetical.
    """
    primary, env_dirs, fleet_dirs = _search_dirs()
    seen: set[str] = set()
    for base in [primary, *env_dirs, *fleet_dirs]:
        if not base.is_dir():
            continue
        # <base>/<name>.yaml form
        for ext in (".yaml", ".yml"):
            for f in base.glob(f"*{ext}"):
                name = f.stem
                if name and not name.startswith("."):
                    seen.add(name)
        # <base>/<name>/<name>.yaml form
        for sub in base.iterdir():
            if not sub.is_dir() or sub.name.startswith("."):
                continue
            for ext in (".yaml", ".yml"):
                if (sub / f"{sub.name}{ext}").is_file():
                    seen.add(sub.name)
                    break
    return sorted(seen)


def resolve_with_prefix(name: str) -> str:
    """Like :func:`resolve_config` but with smart prefix fallback.

    Behaviour (F-CS10):
      1. Exact match wins (delegates to resolve_config).
      2. If no exact hit, look for agent names starting with ``name``.
         - 1 match → use it. Emit a single stderr line so the user
           knows we expanded the input.
         - 2+ matches → raise :class:`AmbiguousAgent` with the list.
         - 0 matches → re-raise the original FileNotFoundError so
           the existing 'Agent not found. Searched: ...' help fires.

    Path arguments (containing '/' or .yaml/.yml) bypass the entire
    chain — they're already explicit.
    """
    if "/" in name or name.endswith((".yaml", ".yml")):
        return resolve_config(name)
    try:
        return resolve_config(name)
    except FileNotFoundError as exact_miss:
        matches = [n for n in enumerate_agent_names() if n.startswith(name)]
        if len(matches) == 1:
            import sys

            print(
                f"resolved '{name}' → '{matches[0]}' (prefix match)",
                file=sys.stderr,
                flush=True,
            )
            return resolve_config(matches[0])
        if len(matches) > 1:
            raise AmbiguousAgent(name, matches) from exact_miss
        raise


def resolve_config(name_or_path: str) -> str:
    """Resolve agent name or path to a config file path.

    Search order for short names (no slash, no .yaml/.yml suffix):
      0. **Project-local** — first ``.scitex/agent-container/agents/``
         found walking upward from cwd. Highest priority so checked-in
         test agents and CI fixtures override globals.
      1. ~/.scitex/agent-container/agents/<name>.yaml  (sac install root)
      2. $SCITEX_AGENT_CONTAINER_YAML_DIRS (colon-separated extra dirs)
      3. Fleet layout — for each root in
         (~/.scitex/orochi, ~/.dotfiles/src/.scitex/orochi):
             a. ``<root>/<HOST>/agents/<name>/<name>.yaml`` (host override)
             b. ``<root>/shared/agents/<name>/<name>.yaml`` (shared default)

    Pass an explicit path (with / or .yaml/.yml) to bypass the search entirely.
    """
    p = Path(name_or_path)
    if "/" in name_or_path or name_or_path.endswith((".yaml", ".yml")):
        if p.exists():
            return str(p)
        raise FileNotFoundError(f"Config file not found: {name_or_path}")

    primary, env_dirs, fleet_dirs = _search_dirs()
    # Split env_dirs into the project-local prefix (added by
    # ``_search_dirs``) and the operator-supplied extension.
    project_local_dirs = _project_local_dirs()
    operator_env_dirs = env_dirs[len(project_local_dirs) :]

    # Order: project-local → primary (~/.scitex…) → env → fleet.
    for d in project_local_dirs:
        hit = _try_dir(d, name_or_path)
        if hit:
            return hit
    hit = _try_dir(primary, name_or_path)
    if hit:
        return hit
    for d in operator_env_dirs:
        hit = _try_dir(d, name_or_path)
        if hit:
            return hit
    for d in fleet_dirs:
        hit = _try_dir(d, name_or_path)
        if hit:
            return hit

    project_lines = "\n".join(
        f"  {d}/{name_or_path}/{name_or_path}.yaml" for d in project_local_dirs
    )
    env_line = (
        f"  (env ${_ENV_VAR}: "
        f"{', '.join(str(d) for d in operator_env_dirs) if operator_env_dirs else '<unset>'})"
    )
    fleet_lines = "\n".join(
        f"  {d}/{name_or_path}/{name_or_path}.yaml" for d in fleet_dirs
    )
    raise FileNotFoundError(
        f"Agent '{name_or_path}' not found. Searched:\n"
        + (project_lines + "\n" if project_lines else "")
        + f"  {primary}/{name_or_path}.yaml\n"
        f"{env_line}\n"
        f"{fleet_lines}"
    )
