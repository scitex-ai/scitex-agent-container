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

import logging
import os
from pathlib import Path

from ..config import AgentConfig
from ._to_home_errors import UnknownToHomeLayer

logger = logging.getLogger(__name__)

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


def _collapse_duplicate_paths(
    layers: "list[tuple[str, Path | None]]",
) -> "list[tuple[str, Path | None]]":
    """Drop any layer whose directory an EARLIER layer already contributes.

    ``user-shared`` and ``project-shared`` both search
    ``<agents root>/_shared/to_home``. For a spec living under the USER agents
    root those two roots are the same directory, so the cascade merges one
    directory into itself. Measured 2026-08-09: this is the case for ALL 102
    registered specs — the "three layer cascade" is really two.

    Merging a directory with itself cannot contribute anything, so today this
    is invisible: equal scalars are idempotent, hook groups de-dupe on
    equality, list merges append uniques, ``_comment`` keeps the first. Every
    one of those has to KEEP being true. One non-idempotent merge rule — a
    counter, an append-always list, a timestamp — and the duplicate silently
    doubles it for every agent at once.

    Collapsing here removes that standing trap and makes the layer list honest:
    what it reports is what actually contributes. The EARLIER (lower
    precedence) layer keeps the path, which matches the cascade's own
    first-layer-owns rule, so provenance attribution is unchanged.

    A ``None`` layer is left alone — absence is not a duplicate.
    """
    seen: set[Path] = set()
    collapsed: list[tuple[str, Path | None]] = []
    for name, path in layers:
        if path is None:
            collapsed.append((name, None))
            continue
        try:
            key = path.resolve()
        except OSError:  # stx-allow: fallback (unresolvable path -> compare raw)
            key = path
        if key in seen:
            logger.debug(
                "to_home: layer %r resolves to %s, already contributed by an "
                "earlier layer — collapsed",
                name,
                key,
            )
            collapsed.append((name, None))
            continue
        seen.add(key)
        collapsed.append((name, path))
    return collapsed


def settings_layer_dirs(config: AgentConfig) -> "list[tuple[str, Path | None]]":
    """The ordered settings.json cascade layers (lowest precedence first).

    ``(name, dir)`` pairs for the user-level ``_shared`` baseline, the spec's
    ``_shared`` baseline, and the per-agent ``to_home`` — the inputs to
    :func:`_to_home_settings.deploy_settings_cascade` /
    :func:`_to_home_settings.settings_cascade_provenance`. Shared by
    ``deploy_to_home`` and ``sac agents explain`` so both resolve identically.

    When the spec DECLARES ``to_home_layers``, only the named layers are
    resolved; every other layer is dropped to ``None`` and contributes nothing.
    Declaring the empty list therefore inherits nothing, which is a sandboxed
    agent's legitimate way of pinning its home to its own spec.

    When the spec declares NOTHING, every layer applies — today's behaviour —
    and this function says nothing about it. It used to log a WARNING here, and
    that was the wrong place: this is a PURE resolver, and a single start calls
    it TWICE (workspace home, then the apptainer overlay upper — see
    ``_to_home_overlay.deploy_to_home_overlay``), so one agent produced two
    identical paragraphs. The missing declaration is a property of the SPEC,
    decided once per launch, so the complaint — and, once the fleet's specs are
    migrated, the REFUSAL — lives in
    :func:`.._lifecycle._layers_preflight.check_to_home_layers_at_launch`,
    which ``agent_start`` calls exactly once.
    """
    resolved = _collapse_duplicate_paths(
        [
            ("user-shared", _user_baseline_to_home_dir()),
            ("project-shared", resolve_baseline_to_home_dir(_spec_dir(config))),
            ("per-agent", resolve_to_home_dir(config)),
        ]
    )

    declared = getattr(config, "to_home_layers", None)
    if declared is None:
        return resolved

    wanted = set(declared)
    unknown = wanted - {name for name, _ in resolved}
    if unknown:
        raise UnknownToHomeLayer(
            f"spec.to_home_layers names unknown layer(s) {sorted(unknown)!r} "
            f"for agent {getattr(config, 'name', '<unnamed>')!r}. Valid names: "
            f"{[name for name, _ in resolved]!r}. A misspelt layer would "
            f"silently inherit nothing, so this is refused rather than ignored."
        )
    return [(name, path if name in wanted else None) for name, path in resolved]


def _spec_dir(config: AgentConfig) -> Path | None:
    if not getattr(config, "config_path", ""):
        return None
    return Path(config.config_path).parent


__all__ = [
    "resolve_to_home_dir",
    "resolve_baseline_to_home_dir",
    "settings_layer_dirs",
]
