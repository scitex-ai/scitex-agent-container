"""Deploy ``.claude/settings.json`` via a layered cascade deep-merge (ADR-0018).

The two-pass ``to_home`` walk plain-copies most files (last layer wins). That
is wrong for ``settings.json``: it lands at the container ``$HOME/.claude/`` at
USER scope and must COMPOSE the fleet-wide ``_shared`` baseline, the per-repo
``_shared`` baseline, and the per-agent file — not let the last one clobber the
rest. This module assembles it instead, lowest precedence first:

    user      ~/.scitex/agent-container/agents/_shared/to_home/.claude/settings.json
    project   <proj>/.scitex/agent-container/agents/_shared/to_home/.claude/settings.json
    per-agent <spec_dir>/to_home/.claude/settings.json

Each layer is deep-merged via :func:`_layer_merge.deep_merge_layers` (hooks and
lists additive; identical scalars idempotent; a cross-layer scalar conflict
raises :class:`LayerMergeConflict`). A layer may still ship the legacy name
``settings.local.json``; it is accepted as that layer's source (``settings.json``
wins if both exist in one layer). ``setup_settings_json`` later folds SAC's own
managed keys (skip-permissions / statusLine / event-ring hooks) on top.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from ._layer_merge import deep_merge_layers
from ._to_home_errors import WorkspaceSettingsMergeError

logger = logging.getLogger(__name__)

# Per layer, prefer the USER-scope name; accept the legacy project-scope name
# as a fallback source so un-renamed baselines still contribute.
_LAYER_FILENAMES = ("settings.json", "settings.local.json")


def _read_settings_layer(layer_dir: Path) -> dict | None:
    """Return the parsed ``.claude/settings.json`` (or legacy ``.local``) of a
    layer, or ``None`` when the layer ships neither. Bad JSON is fail-loud."""
    claude_dir = layer_dir / ".claude"
    for name in _LAYER_FILENAMES:
        path = claude_dir / name
        if not path.is_file():
            continue
        text = path.read_text()
        if not text.strip():
            return {}
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise WorkspaceSettingsMergeError(
                f"to_home: settings layer {path} is not valid JSON ({exc})"
            ) from exc
        if not isinstance(data, dict):
            raise WorkspaceSettingsMergeError(
                f"to_home: settings layer {path} is not a JSON object"
            )
        return data
    return None


def _collect_layers(
    layer_dirs: "list[tuple[str, Path | None]]",
) -> "list[tuple[str, dict]]":
    """Read each present layer's settings into ``(name, data)`` pairs."""
    layers: list[tuple[str, dict]] = []
    for name, layer_dir in layer_dirs:
        if layer_dir is None:
            continue
        data = _read_settings_layer(layer_dir)
        if data is not None:
            layers.append((name, data))
    return layers


def settings_cascade_provenance(
    layer_dirs: "list[tuple[str, Path | None]]",
) -> dict[str, str]:
    """Provenance map (dotted-key → owning layer) WITHOUT writing anything.

    Same merge as :func:`deploy_settings_cascade` (raises identically on
    conflict / bad JSON) but returns only the provenance — for
    ``sac agents explain`` to show WHERE each effective setting comes from.
    """
    layers = _collect_layers(layer_dirs)
    if not layers:
        return {}
    _, provenance = deep_merge_layers(layers)
    return provenance


def deploy_settings_cascade(
    dest: Path, layer_dirs: "list[tuple[str, Path | None]]"
) -> dict[str, str]:
    """Deep-merge each layer's ``.claude/settings.json`` into ``dest``.

    ``layer_dirs`` is ordered LOWEST precedence first as ``(name, dir)`` pairs;
    ``None`` dirs are caller-filtered. Writes ``dest/.claude/settings.json`` and
    returns the provenance map (dotted-key → owning layer name). No-op when no
    layer ships a settings file, so a non-settings deploy is untouched.

    Raises :class:`LayerMergeConflict` on a cross-layer scalar conflict and
    :class:`WorkspaceSettingsMergeError` on an unparseable layer file.
    """
    layers = _collect_layers(layer_dirs)
    if not layers:
        return {}

    merged, provenance = deep_merge_layers(layers)

    out = dest / ".claude" / "settings.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(merged, indent=2) + "\n")
    try:
        os.chmod(out, 0o644)
    except OSError as exc:  # stx-allow: fallback (reason: filesystem op failure)
        logger.warning("settings: failed to chmod 0644 on %s: %s", out, exc)
    logger.info(
        "settings: cascaded %d layer(s) into %s (%d keys)",
        len(layers),
        out,
        len(merged),
    )
    return provenance


__all__ = ["deploy_settings_cascade", "settings_cascade_provenance"]
