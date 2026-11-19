"""Which environment variable NAMES may enter a sealed run's container.

The rule (``spec.sandbox.env``):

* the names in ``env.allow`` (explicit),
* the keys of ``spec.apptainer.env`` (explicit in the same spec),
* the ONE model key ``env.model_key``,
* :data:`SAC_WIRING` — the few non-secret variables SAC itself sets so the
  harness can start (paths inside the container, the engine/model name).

Anything else — the fleet-default layer, SAC's host-side identity/discovery
variables, operator secrets, a failover pool's other keys — is dropped, and
the DROPPED NAMES (never values) are recorded in the run's outcome. A
secret-shaped name passes only when it is the model key or listed in
``env.allow`` by name: SAC wiring never carries a secret.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

__all__ = [
    "SAC_WIRING",
    "allowed_env_names",
    "filter_env",
    "seal_profile_env",
    "validate_sandbox_config",
]

#: Non-secret launch wiring SAC emits for its harnesses (container-side
#: paths and names only; no host path, no fleet identity, no credential).
#: Nothing that points the agent at SAC itself (``PREPEND_PATH``,
#: ``SAC_BIN``, ``SAC_BIN_IN_SIF``): a sealed agent never sees SAC.
SAC_WIRING = frozenset(
    {
        "HERMES_HOME",
        "SAC_ENGINE",
        "SAC_NAME",
        "SCITEX_AGENT_CONTAINER_MODEL",
        "UV_PROJECT_ENVIRONMENT",
        "CLAUDE_CONFIG_DIR",
        "CLAUDE_DISABLE_AUTO_UPDATE",
    }
)


def _is_secret(name: str) -> bool:
    from ..runtimes._apptainer_secret_env import is_secret_env_key

    return is_secret_env_key(name)


def allowed_env_names(config: Any) -> frozenset[str]:
    """The explicit allow set for ``config`` (empty when not sandboxed)."""
    sb = getattr(config, "sandbox", None)
    if sb is None:
        return frozenset()
    ap = getattr(config, "apptainer", None)
    spec_env = set((getattr(ap, "env", None) or {}).keys()) if ap is not None else set()
    explicit = (
        set(sb.env_allow) | spec_env | ({sb.model_key} if sb.model_key else set())
    )
    wiring = {n for n in SAC_WIRING if not _is_secret(n)}
    return frozenset(explicit | wiring)


def filter_env(
    pairs: Mapping[str, str] | Iterable[tuple[str, str]], allowed: frozenset[str]
) -> tuple[dict[str, str], list[str]]:
    """Split ``pairs`` into (kept mapping, sorted dropped NAMES)."""
    items = pairs.items() if isinstance(pairs, Mapping) else pairs
    kept: dict[str, str] = {}
    dropped: set[str] = set()
    for key, value in items:
        if key in allowed:
            kept[key] = value
        else:
            dropped.add(key)
    return kept, sorted(dropped - set(kept))


def validate_sandbox_config(config: Any) -> None:
    """Cross-field checks a sealed run needs; raises ``ValueError`` loudly."""
    sb = getattr(config, "sandbox", None)
    if sb is None:
        return
    name = getattr(config, "name", "?")
    ap = getattr(config, "apptainer", None)
    if ap is not None and str(getattr(ap, "overlay", "") or ""):
        raise ValueError(
            f"{name}: spec.sandbox owns the writable layer (a fresh "
            f"{sb.run_size_mb} MB overlay per run); remove spec.apptainer.overlay"
        )
    if ap is not None and bool(getattr(ap, "relaxed", False)):
        raise ValueError(
            f"{name}: spec.sandbox cannot run with spec.apptainer.relaxed: true"
        )
    raw = (
        [str(a) for a in (getattr(ap, "raw_args", None) or [])]
        if ap is not None
        else []
    )
    for flag in (
        "--net",
        "--network",
        "--overlay",
        "--workdir",
        "--writable",
        "--no-home",
    ):
        if any(a == flag or a.startswith(flag + "=") for a in raw):
            raise ValueError(
                f"{name}: spec.apptainer.raw_args sets {flag}, which spec.sandbox controls"
            )
    _require_tui_runtime(config, name)
    failover = getattr(config, "hermes_failover", None)
    accounts = getattr(failover, "accounts", None) or {}
    pool = sorted({str(k) for keys in accounts.values() for k in (keys or [])})
    extra = [k for k in pool if k != sb.model_key]
    if extra:
        raise ValueError(
            f"{name}: spec.sandbox admits ONE model key ({sb.model_key}); the "
            f"Hermes failover pool also lists {extra}. Reduce every failover "
            f"account list to [{sb.model_key}] for a sealed run."
        )


def seal_profile_env(
    config: Any, env: Mapping[str, str], *, auth_env: str
) -> dict[str, str]:
    """A harness profile's own env file (e.g. ``$HERMES_HOME/.env``), sealed.

    The harness reads that file itself, so the container-env seal cannot
    reach it: it is filtered with the same allow set here, at write time.
    Refuses when the engine's key variable is not the declared model key.
    """
    sb = getattr(config, "sandbox", None)
    if sb is None:
        return dict(env)
    if auth_env and auth_env != sb.model_key:
        raise ValueError(
            f"{getattr(config, 'name', '?')}: the selected engine reads its key "
            f"from {auth_env}, but spec.sandbox.env.model_key is {sb.model_key}"
        )
    kept, _ = filter_env(env, allowed_env_names(config))
    return kept


def _require_tui_runtime(config: Any, name: str) -> None:
    """Sealed runs are supervised from the tmux launch (TuiSessionRuntime).

    Any other runtime would render the sealed argv without ``begin_run`` or
    the supervisor (no proxy, no env seal) — refuse instead of degrading.
    Configs without a resolvable runtime (unit-test stand-ins) are skipped.
    """
    if not hasattr(config, "runtime"):
        return
    try:
        from .._lifecycle._runtime_select import _get_runtime
        from ..runtimes.tui_session import TuiSessionRuntime

        runtime = _get_runtime(config)
    except (ImportError, ValueError, RuntimeError):
        return
    if not isinstance(runtime, TuiSessionRuntime):
        raise ValueError(
            f"{name}: spec.sandbox needs a tmux-supervised TUI runtime "
            f"(spec.runtime: tui); {type(runtime).__name__} is not supported"
        )
