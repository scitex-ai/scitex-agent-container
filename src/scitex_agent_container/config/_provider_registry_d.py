"""Operator-extensible provider overlay loader (``providers.d/*.yaml``).

Operator directive 2026-06-08: adding a new named backend used to
require editing :data:`config._provider_registry.PROVIDERS` in sac
source — that gate kept ad-hoc / site-local backends locked out of
the bare-string spec form. The overlay path lets the operator drop
a ``~/.scitex/agent-container/providers.d/<name>.yaml`` file per
backend and have sac merge it on top of the built-in registry at
config-load time.

File schema (one provider per file)::

    # ~/.scitex/agent-container/providers.d/qwen-spartan.yaml
    name: qwen-spartan          # required — keys the merged registry
    label: Qwen vLLM (Spartan)  # required — observability surface
    endpoint:                   # exactly base_url XOR tunnel
      tunnel:
        jump_host: spartan-login
        target_host: spartan-gpgpu171
        remote_port: 4000
    default_model: qwen36-35b-a3b
    auth_token_env: CLEW_VLLM_TOKEN

Loader contract (fail-loud, never silent skip)
----------------------------------------------

* Directory missing → silent OK (no-op). Operators on hosts that
  don't need overlays must not be forced to mkdir an empty dir.
* Each ``*.yaml`` is parsed with PyYAML. Malformed YAML / a
  missing required key raises :class:`ProviderRegistryDError` with
  the offending file path — silent skip would let an operator
  think their overlay is active when it isn't.
* Entry name conflicting with a built-in → operator entry WINS;
  a stderr notice names the file that overrode (so an unintended
  shadow doesn't silently change fleet behaviour).
* Two operator files declaring the same ``name`` → last-loaded
  wins, with a stderr warning naming BOTH files.

Test seam
---------

The overlay directory path is taken from (in order):

1. ``providers_d_dir`` keyword argument (highest — tests pass a
   ``tmp_path``).
2. ``$SAC_PROVIDERS_D_DIR`` env var (operator escape hatch — e.g.
   on a shared host where the per-user XDG path is wrong).
3. ``~/.scitex/agent-container/providers.d`` (default operator
   path; mirrors the rest of sac's per-host config layout).

The merged dict returned by :func:`load_merged_registry` is then
threaded through :func:`config._provider_registry.resolve_provider`
and :func:`config._provider_registry.list_providers` via their
``registry`` kwarg.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import yaml

from ._provider_registry import PROVIDERS


class ProviderRegistryDError(RuntimeError):
    """Raised when a ``providers.d/`` overlay file is malformed.

    Never thrown for an absent overlay directory — that is the
    "no overlay configured" baseline path.
    """


_REQUIRED_KEYS = ("name", "label", "endpoint", "default_model", "auth_token_env")
_ALLOWED_ENDPOINT_KEYS = {"base_url", "tunnel"}


def _resolve_overlay_dir(providers_d_dir: Path | str | None) -> Path:
    """Resolve the overlay directory path with the documented precedence.

    Returns the resolved :class:`Path` even when the directory doesn't
    exist — the existence check is the caller's job (so the caller can
    short-circuit silently per the loader contract).
    """
    if providers_d_dir is not None:
        return Path(providers_d_dir)
    env_path = os.environ.get("SAC_PROVIDERS_D_DIR", "")
    if env_path:
        return Path(env_path)
    return Path.home() / ".scitex" / "agent-container" / "providers.d"


def _validate_entry(entry: dict[str, Any], source: Path) -> None:
    """Loud-reject an overlay entry missing required keys or malformed.

    The validator runs before merge so a malformed entry never lands
    in the resolved registry. Each error names the source file so the
    operator can fix the right YAML without grepping.
    """
    if not isinstance(entry, dict):
        raise ProviderRegistryDError(
            f"{source}: top-level YAML must be a mapping, got {type(entry).__name__}"
        )
    missing = [k for k in _REQUIRED_KEYS if k not in entry]
    # ``default_model`` and ``auth_token_env`` may be None — they are
    # required to be DECLARED (so the operator sees what's unset) but
    # are allowed to carry an explicit null. Filter that out of the
    # "missing" list.
    if missing:
        raise ProviderRegistryDError(
            f"{source}: missing required key(s) {missing}. Each "
            f"providers.d/*.yaml must declare: {list(_REQUIRED_KEYS)}."
        )
    name = entry.get("name")
    if not isinstance(name, str) or not name:
        raise ProviderRegistryDError(
            f"{source}: 'name' must be a non-empty string, got {name!r}"
        )
    label = entry.get("label")
    if not isinstance(label, str) or not label:
        raise ProviderRegistryDError(
            f"{source}: 'label' must be a non-empty string, got {label!r}"
        )
    endpoint = entry.get("endpoint")
    if endpoint is None:
        # The "no override" sentinel — only the built-in "anthropic"
        # entry uses this shape; allow operator overlays to declare
        # it too for completeness.
        return
    if not isinstance(endpoint, dict):
        raise ProviderRegistryDError(
            f"{source}: 'endpoint' must be a mapping or null, "
            f"got {type(endpoint).__name__}"
        )
    keys_present = set(endpoint.keys()) & _ALLOWED_ENDPOINT_KEYS
    if len(keys_present) != 1:
        raise ProviderRegistryDError(
            f"{source}: 'endpoint' must declare exactly ONE of "
            f"{sorted(_ALLOWED_ENDPOINT_KEYS)}, got {sorted(endpoint.keys())}"
        )
    if "base_url" in endpoint:
        bu = endpoint["base_url"]
        if not isinstance(bu, str) or not bu:
            raise ProviderRegistryDError(
                f"{source}: 'endpoint.base_url' must be a non-empty string, got {bu!r}"
            )
    if "tunnel" in endpoint:
        t = endpoint["tunnel"]
        if not isinstance(t, dict):
            raise ProviderRegistryDError(
                f"{source}: 'endpoint.tunnel' must be a mapping, got {type(t).__name__}"
            )
        for tk in ("jump_host", "target_host", "remote_port"):
            if not t.get(tk):
                raise ProviderRegistryDError(
                    f"{source}: 'endpoint.tunnel.{tk}' is required and "
                    "must be non-empty (sac cannot stand up an ssh "
                    "ProxyJump without all three of jump_host, "
                    "target_host, remote_port)."
                )


def _load_one(path: Path) -> dict[str, Any]:
    """Read + parse one overlay YAML file. Raises on malformed input.

    The pyyaml ``SafeLoader`` path is used because operator overlay
    files are run-as-operator data; we never trust them with arbitrary
    Python object construction (``!!python/object``).
    """
    try:
        text = path.read_text()
    except OSError as exc:
        raise ProviderRegistryDError(
            f"{path}: could not read overlay file ({exc})"
        ) from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ProviderRegistryDError(f"{path}: malformed YAML ({exc})") from exc
    _validate_entry(data, source=path)
    return data


def load_merged_registry(
    providers_d_dir: Path | str | None = None,
    base_registry: dict[str, dict[str, Any]] | None = None,
    *,
    log_stream: Any = None,
) -> dict[str, dict[str, Any]]:
    """Return the built-in PROVIDERS merged with any overlay files.

    Loader behaviour (see module docstring for the rationale):

    * Overlay directory missing → return ``dict(base_registry)`` as-is.
    * Each ``*.yaml`` parsed and validated; malformed shape raises
      :class:`ProviderRegistryDError` naming the file.
    * Overlay entry conflicting with a built-in → overlay wins; a
      stderr notice names the overriding file.
    * Two overlay files declaring the same ``name`` → last-loaded
      wins (file iteration is sorted by name for determinism); a
      stderr warning names BOTH files.

    Args:
        providers_d_dir: OPTIONAL overlay directory. Precedence is the
            arg → ``$SAC_PROVIDERS_D_DIR`` → default
            ``~/.scitex/agent-container/providers.d``.
        base_registry: OPTIONAL base registry to overlay onto. Default
            uses :data:`_provider_registry.PROVIDERS`.
        log_stream: OPTIONAL stream for the override / dup notices.
            Default ``sys.stderr``; tests pass a ``io.StringIO``.
    """
    base = dict(base_registry if base_registry is not None else PROVIDERS)
    overlay_dir = _resolve_overlay_dir(providers_d_dir)
    if not overlay_dir.is_dir():
        return base

    stream = log_stream if log_stream is not None else sys.stderr
    # Sort by file name so two operators on the same fleet see the
    # same merge order — non-determinism here would surface as
    # different agents picking different overlays on different hosts.
    files = sorted(p for p in overlay_dir.iterdir() if p.suffix == ".yaml")
    # Track which file landed each name so the dup warning can name
    # both files when a second overlay clobbers the first.
    name_to_file: dict[str, Path] = {}
    for path in files:
        entry = _load_one(path)
        name = entry["name"]
        # Strip the ``name`` key out before merge — the registry dict
        # keys ARE the names, so leaving ``name`` in the value would
        # confuse downstream callers.
        record = {k: v for k, v in entry.items() if k != "name"}
        if name in name_to_file:
            print(
                f"[sac:providers.d] WARNING: '{name}' declared in both "
                f"{name_to_file[name]} and {path}; the latter wins. "
                "Delete one of the files to silence this warning.",
                file=stream,
            )
        elif name in base:
            print(
                f"[sac:providers.d] NOTICE: operator overlay {path} "
                f"overrides built-in provider '{name}'. Built-in entry "
                "metadata is shadowed for this run.",
                file=stream,
            )
        base[name] = record
        name_to_file[name] = path
    return base


__all__ = ["ProviderRegistryDError", "load_merged_registry"]
