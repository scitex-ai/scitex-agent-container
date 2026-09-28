"""Gateway-harness contract — one interface for server+TUI harnesses.

A gateway harness runs a loopback server as the session owner with the
official TUI attached as a second client. SAC talks to the server over
intent-level RPCs (submit/steer/abort/status) and never types into the
terminal. Hermes (``hermes-tui``) is implementation #1; the opencode
``serve`` API is the second driver this contract is written for
(probed 2026-09-28 on a temp ``serve`` port: ``POST message`` sync,
``POST prompt_async`` async-204, ``GET session/status`` busy map,
``POST abort`` true, client ``messageID`` replay returns the same run,
no duplicate).

PER-AGENT SPEC RULE. Every tunable lives in the agent's own spec under
``spec.available_harnesses.<name>`` (parsed at load into ``AgentConfig``
``<name>_`` fields, exactly like ``hermes_background_review`` /
``hermes_run_budget_seconds`` / ``hermes_compression``). Editing the
spec changes the derived profile; global/host config is never consulted
and operators never hand-edit the derived artifact. A harness block
under a non-selected harness is rejected at validation time.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ._gateway_types import GatewayHarness, GatewayHarnessError

__all__ = [
    "GATEWAY_HARNESSES",
    "GatewayHarness",
    "GatewayHarnessError",
    "get_gateway_harness",
    "heartbeat_observation_payload",
]


def heartbeat_observation_payload(
    states: dict[str, str],
    session_id: str,
    *,
    identity_fields: dict[str, object] | None = None,
) -> dict[str, Any]:
    """Build the gateway-observed liveness payload (pure, no I/O).

    ``states`` is :meth:`GatewayHarness.session_states` output. An
    observer beat carries process existence, never an incarnation id.
    """
    state = states.get(session_id, "unknown")
    payload: dict[str, Any] = {
        "session_id": session_id,
        "state": state,
        "writer": "gateway-observer",
    }
    if identity_fields:
        payload.update(identity_fields)
    return payload


class _HermesGatewayHarness:
    """Implementation #1: Hermes TUI gateway (delegates, no new logic)."""

    @property
    def name(self) -> str:
        return "hermes-tui"

    @property
    def spec_key(self) -> str:
        return "hermes"

    def owner_argv(self, config: Any, state_dir: Path) -> list[str]:
        from ..config._harness_callables import _hermes_tui_inner_argv

        del state_dir
        return _hermes_tui_inner_argv(config)

    def session_states(
        self, state_dir: Path, *, timeout_s: float = 10.0
    ) -> dict[str, str]:
        from . import _hermes_tui_rpc as rpc

        rows = rpc.active_sessions(state_dir, timeout_s=timeout_s)
        states: dict[str, str] = {}
        for row in rows:
            key = str(row.get("key") or row.get("id") or "")
            if key:
                states[key] = "busy"
        return states

    def submit_turn(
        self,
        state_dir: Path,
        agent_name: str,
        text: str,
        *,
        delivery_mode: str = "steer",
        timeout_s: float = 10.0,
        message_id: str | None = None,
    ) -> dict[str, Any]:
        from . import _hermes_tui_rpc as rpc

        del message_id  # Hermes owns idempotency at the bridge, not the RPC.
        receipt = rpc.submit_turn(
            state_dir,
            agent_name,
            text,
            timeout_s=timeout_s,
            delivery_mode=delivery_mode,
        )
        return dict(receipt.__dict__)

    def abort_turn(
        self, state_dir: Path, agent_name: str, *, timeout_s: float = 10.0
    ) -> bool:
        raise GatewayHarnessError(
            "Hermes has no run-level abort RPC; use `sac agents stop "
            f"{agent_name}` (process-level stop, writer=sac-hermes-stop)."
        )

    def parse_agent_options(self, config: Any) -> dict[str, Any]:
        compression = getattr(config, "hermes_compression", None)
        return {
            "session": str(getattr(getattr(config, "claude", None), "session", "")),
            "background_review": bool(
                getattr(config, "hermes_background_review", False)
            ),
            "run_budget_seconds": getattr(
                config, "hermes_run_budget_seconds", None
            ),
            "compression": (
                dict(compression.__dict__) if compression is not None else {}
            ),
            "engine": {
                "key": str(getattr(config, "engine_key", "") or ""),
                "model": str(getattr(config, "model", "") or ""),
            },
        }

    def compile_profile(
        self, plan: Any, *, workdir: str, options: dict[str, Any]
    ) -> dict[str, Any]:
        from ..config._hermes_config import compile_hermes_config
        from ..config._hermes_compression import HermesCompressionSpec

        compression = options.get("compression") or {}
        spec = (
            HermesCompressionSpec(**compression)
            if isinstance(compression, dict) and compression
            else HermesCompressionSpec()
        )
        return compile_hermes_config(
            plan,
            workdir=workdir,
            run_budget_seconds=options.get("run_budget_seconds"),
            background_review=bool(options.get("background_review", False)),
            compression=spec,
        )


HERMES_GATEWAY = _HermesGatewayHarness()

#: Registered gateway harnesses. ``opencode`` is implementation #2,
#: launched through its owner module + ``OpencodeTuiSessionRuntime``.
GATEWAY_HARNESSES: dict[str, GatewayHarness] = {
    HERMES_GATEWAY.spec_key: HERMES_GATEWAY,
}

from ._gateway_opencode import OPENCODE_GATEWAY  # noqa: E402

GATEWAY_HARNESSES[OPENCODE_GATEWAY.spec_key] = OPENCODE_GATEWAY


def get_gateway_harness(name: str) -> GatewayHarness:
    """Return the gateway driver for an ``available_harnesses`` key."""
    try:
        return GATEWAY_HARNESSES[str(name or "").strip().lower()]
    except KeyError:
        known = ", ".join(sorted(GATEWAY_HARNESSES))
        raise GatewayHarnessError(
            f"Unknown gateway harness {name!r}. Known: {known}. "
            "A terminal-emulated fallback is not offered."
        ) from None
