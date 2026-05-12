"""Resolve agent name or path to a config file path."""

from __future__ import annotations

import os
from pathlib import Path
from typing import List, Tuple

_ENV_VAR = "SCITEX_AGENT_CONTAINER_YAML_DIRS"


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


def _search_dirs() -> Tuple[Path, List[Path], List[Path]]:
    """Return (primary_dir, env_dirs, fleet_dirs) with ~ expansion.

    Search order (highest priority first):
      0. **Project-local** — first ``.scitex/agent-container/agents/``
         found by walking upward from cwd. Surfaces in ``env_dirs``
         (prepended) so checked-in test agents and CI fixtures win
         over stale globals.
      1. ``~/.scitex/agent-container/agents/`` — sac's own install root.
      2. ``$SCITEX_AGENT_CONTAINER_YAML_DIRS`` — plugin port for external
         orchestrators (e.g. orochi) to extend the search scope without
         sac knowing about them. Colon-separated; each path treated as
         a base dir holding ``<name>/spec.yaml`` agents.

    sac is standalone and does not read from any other scitex package's
    state directory. Downstream orchestrators that want sac to discover
    their agent specs must set ``$SCITEX_AGENT_CONTAINER_YAML_DIRS``,
    e.g. in their startup script:

        export SCITEX_AGENT_CONTAINER_YAML_DIRS=\\
            ~/.scitex/orochi/$(hostname -s)/agents:\\
            ~/.scitex/orochi/shared/agents
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
    fleet_dirs: list[Path] = []  # always empty; preserved for back-compat tuple shape
    return primary, env_dirs, fleet_dirs


def _try_dir(base: Path, name: str) -> str | None:
    """Locate ``<base>/<name>/spec.yaml|yml``.

    The flat-form ``<base>/<name>.yaml`` and the legacy
    ``<base>/<name>/<name>.yaml`` are no longer accepted — every agent
    lives in its own directory and the config is named ``spec.yaml``.
    """
    for ext in (".yaml", ".yml"):
        cand = base / name / f"spec{ext}"
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

    A name is recorded when ``<dir>/<name>/spec.yaml`` (or .yml) exists
    in any search-chain directory. The flat-form ``<dir>/<name>.yaml``
    and the legacy ``<dir>/<name>/<name>.yaml`` no longer count.
    Duplicates are collapsed; ordering is alphabetical.
    """
    primary, env_dirs, fleet_dirs = _search_dirs()
    seen: set[str] = set()
    for base in [primary, *env_dirs, *fleet_dirs]:
        if not base.is_dir():
            continue
        for sub in base.iterdir():
            if not sub.is_dir() or sub.name.startswith("."):
                continue
            for ext in (".yaml", ".yml"):
                if (sub / f"spec{ext}").is_file():
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

    Every agent lives in its own directory; the config file is always
    named ``spec.yaml`` (or ``spec.yml``). The flat-form
    ``<base>/<name>.yaml`` and the legacy ``<base>/<name>/<name>.yaml``
    are no longer accepted — sac is dir-as-SSoT.

    Search order for short names (no slash, no .yaml/.yml suffix):

    0. **Project-local** — first ``.scitex/agent-container/agents/`` found walking upward from cwd. Highest priority so checked-in test agents and CI fixtures override globals.
    1. ``~/.scitex/agent-container/agents/<name>/spec.yaml`` (sac install root).
    2. ``$SCITEX_AGENT_CONTAINER_YAML_DIRS`` (colon-separated extra dirs, each searched as ``<dir>/<name>/spec.yaml``). Plugin port for downstream orchestrators to inject extra paths without sac knowing about them.

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
        f"  {d}/{name_or_path}/spec.yaml" for d in project_local_dirs
    )
    env_line = (
        f"  (env ${_ENV_VAR}: "
        f"{', '.join(str(d) for d in operator_env_dirs) if operator_env_dirs else '<unset>'})"
    )
    fleet_lines = "\n".join(f"  {d}/{name_or_path}/spec.yaml" for d in fleet_dirs)
    raise FileNotFoundError(
        f"Agent '{name_or_path}' not found. Searched:\n"
        + (project_lines + "\n" if project_lines else "")
        + f"  {primary}/{name_or_path}/spec.yaml\n"
        f"{env_line}\n"
        f"{fleet_lines}"
    )
