"""Resolve WHICH ``to_home`` directories apply to an agent (ADR-0006/0018).

Pure path-resolution helpers, split out of :mod:`._to_home` (which kept
growing past the line cap). No I/O beyond ``Path.is_dir`` probes; no
deploy side effects. :mod:`._to_home` imports and re-exports these so the
legacy ``from ...runtimes._to_home import resolve_baseline_to_home_dir``
contract (and ``sac agents explain``) keeps resolving unchanged.

Three baseline layers feed the settings/.envrc/.mcp cascade, lowest
precedence first:

    user      ~/.scitex/agent-container/agents/_shared/to_home
    project   <proj>/.scitex/agent-container/agents/_shared/to_home
    per-agent <spec_dir>/to_home

Each layer has an explicit env override / opt-out so an agent can pin its
home by spec alone (see the two ``*_ENV_VAR`` constants).
"""

from __future__ import annotations

import os
from pathlib import Path

from ..config import AgentConfig

# Env var: explicit override for the shared/common baseline to_home dir.
# Absolute path. When unset we fall back to ``<agents_dir>/_shared/to_home``
# (or the legacy ``_base`` sibling).
_BASELINE_ENV_VAR = "SAC_TO_HOME_BASELINE"

# Env var: explicit override / opt-out for the USER-level shared baseline
# (``~/.scitex/agent-container/agents/_shared/to_home``). Mirrors
# ``_BASELINE_ENV_VAR`` exactly: set to an absolute dir → use it; set to a
# NON-dir (e.g. ``/nonexistent``) → the user baseline is ABSENT (opt-out);
# unset → the default ``~`` search. This lets a sandboxed agent pin its home
# by spec ALONE — excluding the fleet-wide user baseline whose
# coordinator-discipline hooks (e.g. force_background_bash) are wrong for a
# long-foreground benchmark solver and, worse, would be an uncontrolled
# confound between runs of the same experimental arm (a solver started before
# the baseline landed vs after). The spec-relative baseline already had this
# escape hatch via ``_BASELINE_ENV_VAR``; the user layer lacked it. (2026-07-06)
_USER_BASELINE_ENV_VAR = "SAC_USER_TO_HOME_BASELINE"

# Names of the sibling dir (under the agents root) that holds the common
# baseline. Agents live at ``<agents_dir>/<name>/``, so the agents root
# is the spec dir's parent and the baseline is ``<parent>/_shared/to_home``.
# ``_shared`` is the current name; ``_base`` is retained as a
# backward-compat fallback for hosts/fleets not yet renamed (first match
# under the agents root wins, in declared order).
_BASELINE_DIR_NAMES = ("_shared", "_base")


def resolve_to_home_dir(config: AgentConfig) -> Path | None:
    """Resolve ``spec.to_home`` to an absolute directory.

    Resolution order:
      1. Absolute path: use as-is.
      2. Relative path: resolve against the directory containing
         ``spec.yaml``.
      3. Empty: auto-discover ``./to_home`` next to ``spec.yaml``.

    Returns ``None`` if no directory can be resolved (legacy specs
    without a to_home/ dir simply skip materialization).
    """
    spec_dir = _spec_dir(config)
    raw = (getattr(config, "to_home", "") or "").strip()
    if not raw:
        if spec_dir is not None and (spec_dir / "to_home").is_dir():
            return spec_dir / "to_home"
        return None
    p = Path(raw).expanduser()
    if not p.is_absolute():
        if spec_dir is None:
            return None
        p = spec_dir / p
    return p if p.is_dir() else None


def resolve_baseline_to_home_dir(spec_dir: Path | None) -> Path | None:
    """Resolve the shared/common baseline ``to_home/`` directory.

    Resolution order:
      1. ``$SAC_TO_HOME_BASELINE`` (absolute dir) — explicit override.
      2. ``<agents_dir>/_shared/to_home`` — a sibling ``_shared`` dir
         under the agents root (``_base`` accepted as a backward-compat
         fallback). Agents live at ``<agents_dir>/<name>/``, so the
         agents root is ``spec_dir.parent``.

    Returns ``None`` when no baseline dir can be resolved (no baseline =
    current behavior; fully backward compatible).
    """
    override = (os.environ.get(_BASELINE_ENV_VAR, "") or "").strip()
    if override:
        p = Path(override).expanduser()
        return p if p.is_dir() else None
    if spec_dir is None:
        return None
    for name in _BASELINE_DIR_NAMES:
        p = spec_dir.parent / name / "to_home"
        if p.is_dir():
            return p
    return None


def _user_baseline_to_home_dir() -> Path | None:
    """The USER-level shared baseline ``to_home`` — applies to every agent
    regardless of where its spec lives: ``~/.scitex/agent-container/agents/
    {_shared,_base}/to_home`` (first match wins). Returns ``None`` when absent.

    Honors ``$SAC_USER_TO_HOME_BASELINE`` (mirrors ``resolve_baseline_to_home_dir``'s
    ``$SAC_TO_HOME_BASELINE``): set to an absolute dir → use it; set to a NON-dir
    → the user baseline is ABSENT (opt-out); unset → the default ``~`` search.
    A sandboxed solver sets it to a non-dir to exclude the fleet-wide baseline
    and keep its home spec-pinned (arm-consistency across benchmark runs).

    Distinct from :func:`resolve_baseline_to_home_dir`, which resolves the
    baseline *relative to the spec's* agents root (project-local for a
    project-local spec). The ``.envrc`` cascade sources BOTH so a user-global
    default and a project ``_shared`` both apply, lowest precedence first.
    """
    override = (os.environ.get(_USER_BASELINE_ENV_VAR, "") or "").strip()
    if override:
        p = Path(override).expanduser()
        return p if p.is_dir() else None
    base = Path("~/.scitex/agent-container/agents").expanduser()
    for name in _BASELINE_DIR_NAMES:
        p = base / name / "to_home"
        if p.is_dir():
            return p
    return None


def settings_layer_dirs(config: AgentConfig) -> "list[tuple[str, Path | None]]":
    """The ordered settings.json cascade layers (lowest precedence first).

    ``(name, dir)`` pairs for the user-level ``_shared`` baseline, the spec's
    ``_shared`` baseline, and the per-agent ``to_home`` — the inputs to
    :func:`_to_home_settings.deploy_settings_cascade` /
    :func:`_to_home_settings.settings_cascade_provenance`. Shared by
    ``deploy_to_home`` and ``sac agents explain`` so both resolve identically.
    """
    return [
        ("user-shared", _user_baseline_to_home_dir()),
        ("project-shared", resolve_baseline_to_home_dir(_spec_dir(config))),
        ("per-agent", resolve_to_home_dir(config)),
    ]


def _spec_dir(config: AgentConfig) -> Path | None:
    if not getattr(config, "config_path", ""):
        return None
    return Path(config.config_path).parent


__all__ = [
    "resolve_to_home_dir",
    "resolve_baseline_to_home_dir",
    "settings_layer_dirs",
]
