"""Native subscription admission before a production TUI replacement."""

from __future__ import annotations

from ..config import AgentConfig


def preflight_native_tui(config: AgentConfig, *, production: bool) -> None:
    """Refuse an unusable native successor while its old process is alive.

    Injected runtime factories and command builders own their test launch
    contracts. Other harnesses and headless runtimes retain their existing
    admission paths.
    """
    if not production:
        return
    from ..config._harness_registry import CODEX_TUI, resolve_harness_key

    if resolve_harness_key(config) != CODEX_TUI:
        return
    from ._apptainer_codex_env import preflight_subscription
    from .tui_session import state_dir_for_config

    preflight_subscription(config, state_dir_for_config(config))


def preflight_native_tui_from_config_path(
    config_path: str, *, engine_override: str | None = None,
) -> None:
    """Resolve exactly the restart's successor before entering its stop leg."""
    from .._lifecycle._engine_select import select_engine_at_start
    from ..config import load_config

    config = load_config(config_path)
    select_engine_at_start(config, engine_override, probe=False, log=False)
    preflight_native_tui(config, production=True)
