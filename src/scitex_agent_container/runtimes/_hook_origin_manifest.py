"""Record WHICH to_home layer armed each of an agent's hooks, on disk.

An agent's effective ``.claude/settings.json`` is assembled from a cascade of
``to_home`` layers (``_layer_merge.deep_merge_layers``). Reading the deployed
file afterwards tells you which hooks are armed but NOT where any of them came
from — the layers are flattened into one block and the origin is gone. So the
operator's question when a guard fires unexpectedly, "where is this hook coming
from?", had no answer that did not involve re-deriving the whole cascade by
hand.

This module answers it from a file. At deploy time — where the provenance is
still known — every armed hook command is written out next to the layer that
armed it:

    <runtime>/logs/<agent>/hook-origins.json

``<runtime>`` is :func:`_runtime_paths.runtime_base_dir`, so this lands beside
the existing ``runtime/logs/host_exec.log`` and honours the same relocation env
var. It is RUNTIME state, deliberately outside version control: it describes
what one host deployed at one moment, which is exactly the fact a committed
file cannot carry.

Scope, stated plainly: this records what was ARMED, not what FIRED. It is
derived from the deploy, so it cannot drift from the deployed settings, and it
touches nothing on the hook execution path. A record of individual firings
needs each hook's invocation wrapped, which puts code between Claude and the
guard's exit code — a different risk class, kept out of this module on purpose.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from .._runtime_paths import runtime_base_dir

logger = logging.getLogger(__name__)

_MANIFEST_NAME = "hook-origins.json"
_HOOKS_PREFIX = "hooks."


def manifest_path(agent_name: str) -> Path:
    """Where ``agent_name``'s hook-origin manifest lives."""
    return runtime_base_dir() / "logs" / agent_name / _MANIFEST_NAME


def hook_origins(provenance: "dict[str, str]") -> "dict[str, dict[str, str]]":
    """Reshape a cascade provenance map into ``{event: {command: layer}}``.

    ``provenance`` keys are dotted paths; hooks are recorded as
    ``hooks.<Event>.<command>`` (see ``_layer_merge._record_hook_commands``).
    A command may itself contain dots, so the split is bounded to 2 — the
    remainder is the command verbatim. Non-hook keys are ignored.
    """
    origins: dict[str, dict[str, str]] = {}
    for path, layer in provenance.items():
        if not path.startswith(_HOOKS_PREFIX):
            continue
        parts = path.split(".", 2)
        if len(parts) != 3:
            continue
        _, event, command = parts
        if not event or not command:
            continue
        origins.setdefault(event, {})[command] = layer
    return origins


def write_hook_manifest(agent_name: str, provenance: "dict[str, str]") -> Path | None:
    """Write ``agent_name``'s hook-origin manifest; return its path.

    Returns ``None`` when the cascade armed no hooks — an agent with no guards
    gets no file, rather than an empty one that reads like a wiped manifest.

    Never raises: a manifest is an observability aid, and failing a deploy
    because a log could not be written would trade a real capability for a
    diagnostic one. A write failure is logged at WARNING with the path, which
    is the actionable half.
    """
    origins = hook_origins(provenance)
    if not origins:
        return None

    out = manifest_path(agent_name)
    payload = {
        "agent": agent_name,
        "hook_count": sum(len(cmds) for cmds in origins.values()),
        "layers": sorted(
            {layer for cmds in origins.values() for layer in cmds.values()}
        ),
        "events": {
            ev: dict(sorted(cmds.items())) for ev, cmds in sorted(origins.items())
        },
    }
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, indent=2) + "\n")
    except OSError as exc:  # stx-allow: fallback (observability must not fail a deploy)
        logger.warning("hook manifest: failed to write %s: %s", out, exc)
        return None
    logger.info(
        "hook manifest: %d hook(s) from %d layer(s) -> %s",
        payload["hook_count"],
        len(payload["layers"]),
        out,
    )
    return out


__all__ = ["hook_origins", "manifest_path", "write_hook_manifest"]
