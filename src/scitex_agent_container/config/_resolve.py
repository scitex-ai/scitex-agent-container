"""Resolve agent name or path to a config file path."""

from __future__ import annotations

import os
from pathlib import Path
from typing import List, Tuple

_ENV_VAR = "SCITEX_AGENT_CONTAINER_YAML_DIRS"

# Operator-facing disambiguator for the project-local-vs-fleet registry
# ambiguity. ``user`` → search ONLY the user-scope fleet registry;
# ``project`` → search ONLY the project-local registry; unset/empty/any
# other value → auto (and fail loud if BOTH registries exist — see
# ``AmbiguousRegistryScope`` and ``_apply_scope``).
_SCOPE_ENV_VAR = "SAC_AGENT_SCOPE"

# Sentinel ``primary`` used when ``SAC_AGENT_SCOPE=project`` scopes the
# fleet registry out. It is a guaranteed-absent path so the existing
# ``is_dir`` / ``exists`` guards skip it, and the not-found message
# renders a clear "scoped out" line instead of pointing at the real
# fleet dir the operator asked to ignore.
_SCOPED_OUT_FLEET = Path("<fleet scoped out by SAC_AGENT_SCOPE=project>")


def _read_scope() -> str | None:
    """Return the requested registry scope: ``"user"``, ``"project"``, or None.

    Reads ``$SAC_AGENT_SCOPE`` case-insensitively and trimmed. Anything
    that is not exactly ``user`` / ``project`` (including empty / unset /
    a typo) maps to ``None`` = "auto / unset" — the caller then applies
    the fail-loud-on-ambiguity rule. Kept deliberately strict + simple so
    the behaviour is predictable: only the two documented values switch
    scope; everything else is "unset".
    """
    raw = os.environ.get(_SCOPE_ENV_VAR, "").strip().lower()
    if raw in ("user", "project"):
        return raw
    return None


class AmbiguousRegistryScope(LookupError):
    """Raised when project-local and fleet registries both exist + scope is unset.

    A fleet-management op run from inside a repo that ships its own
    project-local ``.scitex/agent-container/agents/`` would otherwise
    silently resolve the project-local registry and never see the
    user-scope fleet (the "sac-from-sac" breakage). Rather than guess,
    sac fails loud and tells the operator how to disambiguate via
    ``$SAC_AGENT_SCOPE``.
    """

    def __init__(self, project_dir: Path, fleet_dir: Path):
        self.project_dir = project_dir
        self.fleet_dir = fleet_dir
        super().__init__(
            f"Registry scope is ambiguous: project-local {project_dir} "
            f"shadows the fleet registry {fleet_dir}. "
            f"Set {_SCOPE_ENV_VAR}=user to target the fleet, or "
            f"{_SCOPE_ENV_VAR}=project for this repo's local agents."
        )


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

    When ``scitex-config`` is not installed (it is an optional dep), a
    self-contained native walk-up provides the SAME discovery so the
    project-local scope still works — sac does not silently lose
    project-local agents just because the optional package is absent.
    """
    try:
        from scitex_config._ecosystem import local_state
    except Exception:  # stx-allow: fallback (reason: scitex-config is an optional dep — fall through to the native git-walk below rather than break sac install)
        scope = _native_project_scope(start)
    else:
        scope = local_state.find_project_scope("agent-container", start=start)
    if scope is None:
        return []
    agents = scope / "agents"
    return [agents] if agents.is_dir() else []


def _native_project_scope(start: Path | None = None) -> Path | None:
    """``scitex-config``-free fallback for project-scope discovery.

    Walks upward from ``start`` (default: cwd) looking for a directory
    that is a git repo (``.git`` present) AND carries a
    ``.scitex/agent-container/`` subtree; returns that subtree (the
    "scope" dir, parent of ``agents/``) or None. Matches the
    ecosystem-wide ``find_project_scope`` contract closely enough for
    sac's single consumer here.
    """
    here = Path(start) if start is not None else Path.cwd()
    here = here.resolve()
    for d in (here, *here.parents):
        scope = d / ".scitex" / "agent-container"
        if (d / ".git").exists() and scope.is_dir():
            return scope
    return None


def _operator_env_dirs() -> List[Path]:
    """Return the operator-supplied ``$SCITEX_AGENT_CONTAINER_YAML_DIRS`` dirs.

    Colon-separated, ``~`` expanded, empty segments dropped. This is the
    explicit plugin-port extension and is NEVER part of the
    project-local-vs-fleet ambiguity — it is honoured as-is regardless of
    ``$SAC_AGENT_SCOPE``.
    """
    env_raw = os.environ.get(_ENV_VAR, "")
    return [Path(os.path.expanduser(p)) for p in env_raw.split(":") if p.strip()]


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

    Scope (``$SAC_AGENT_SCOPE``): when BOTH the project-local registry
    dir AND the user-scope fleet registry dir exist and the scope is
    unset, this raises :class:`AmbiguousRegistryScope` rather than
    silently preferring project-local. See :func:`_apply_scope`. The
    operator-supplied ``$SCITEX_AGENT_CONTAINER_YAML_DIRS`` extension is
    NOT part of the ambiguous pair — it is always honoured as-is.
    """
    home = Path(os.path.expanduser("~"))
    primary = home / ".scitex" / "agent-container" / "agents"
    operator_env_dirs = _operator_env_dirs()
    project_local = _project_local_dirs()

    # Apply $SAC_AGENT_SCOPE: drop one side per the explicit scope, or
    # fail loud when both registries exist and the scope is unset.
    primary, project_local = _apply_scope(primary, project_local)

    # Project-local takes priority OVER the home install root: prepend
    # it as the highest-priority entry. Returned via the env_dirs slot
    # but inserted before the existing primary check at the call site
    # below by ordering primary AFTER it in resolve_config(). The
    # operator extension follows project-local.
    env_dirs = project_local + operator_env_dirs
    fleet_dirs: list[Path] = []  # always empty; preserved for back-compat tuple shape
    return primary, env_dirs, fleet_dirs


def _apply_scope(
    primary: Path, project_local: List[Path]
) -> Tuple[Path, List[Path]]:
    """Resolve the project-local-vs-fleet ambiguity per ``$SAC_AGENT_SCOPE``.

    Rule (simple + predictable):

    * ``SAC_AGENT_SCOPE=user``    → search ONLY the user-scope fleet
      ``primary``; drop project-local entirely.
    * ``SAC_AGENT_SCOPE=project`` → search ONLY project-local; signal
      "skip primary" by returning a sentinel non-existent fleet dir so
      the existing call-site loops never hit it.
    * unset/auto: if BOTH the project-local registry dir AND the fleet
      registry dir exist → raise :class:`AmbiguousRegistryScope`. If only
      one exists (the common case — a fresh CI runner has no
      ``~/.scitex/agent-container/agents`` fleet dir) → no ambiguity,
      both are returned untouched and the lone present dir wins silently.

    The "both present" test keys purely on directory existence, NOT on
    which scope a specific agent name lives in — the operator wants the
    scope made explicit, not guessed per-name.
    """
    scope = _read_scope()
    if scope == "user":
        # Fleet only: ignore any project-local registry.
        return primary, []
    if scope == "project":
        # Project-local only: neutralise the fleet primary with the
        # sentinel below so the call-site loops + the not-found message
        # both skip / render it as scoped-out (rather than searching the
        # real fleet dir).
        return _SCOPED_OUT_FLEET, project_local

    # scope unset → fail loud iff BOTH registry dirs are present.
    project_present = bool(project_local) and project_local[0].is_dir()
    fleet_present = primary.is_dir()
    if project_present and fleet_present:
        raise AmbiguousRegistryScope(project_local[0], primary)
    return primary, project_local


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

    Honours ``$SAC_AGENT_SCOPE`` via the shared ``_search_dirs`` seam:
    raises :class:`AmbiguousRegistryScope` when both the project-local
    and fleet registries exist and the scope is unset (same rule as
    :func:`resolve_config`).
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

    Scope (``$SAC_AGENT_SCOPE``): if BOTH the project-local registry dir
    AND the user-scope fleet registry dir exist and the scope is unset,
    this raises :class:`AmbiguousRegistryScope` instead of silently
    preferring project-local. ``SAC_AGENT_SCOPE=user`` searches only the
    fleet; ``=project`` searches only project-local. A single present
    registry resolves with no error.

    Pass an explicit path (with / or .yaml/.yml) to bypass the search entirely.
    """
    p = Path(name_or_path)
    if "/" in name_or_path or name_or_path.endswith((".yaml", ".yml")):
        if p.exists():
            return str(p)
        raise FileNotFoundError(f"Config file not found: {name_or_path}")

    primary, env_dirs, fleet_dirs = _search_dirs()
    # Split env_dirs into the project-local prefix (which ``_search_dirs``
    # may have dropped per ``$SAC_AGENT_SCOPE``) and the operator-supplied
    # ``$SCITEX_AGENT_CONTAINER_YAML_DIRS`` extension. Derive the split
    # from the operator count (scope-invariant) so it stays correct even
    # when the project-local prefix was scoped out.
    operator_env_dirs = _operator_env_dirs()
    split = len(env_dirs) - len(operator_env_dirs)
    project_local_dirs = env_dirs[:split]

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
    primary_line = (
        f"  {primary}"
        if primary == _SCOPED_OUT_FLEET
        else f"  {primary}/{name_or_path}/spec.yaml"
    )
    raise FileNotFoundError(
        f"Agent '{name_or_path}' not found. Searched:\n"
        + (project_lines + "\n" if project_lines else "")
        + f"{primary_line}\n"
        f"{env_line}\n"
        f"{fleet_lines}"
    )
