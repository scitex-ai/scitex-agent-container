"""One API-quota recovery using the ordinary restart and its existing budget.

The owning controller supplies fenced route observations and one real handshake
reader. This is a one-shot operation, never a scheduler or a provider probe.
Native subscription harnesses are refused. Unknown quota, a failed restart or
an unproven handshake leaves a durable WAIT result instead of another restart.
"""

from __future__ import annotations

import fcntl
import json
import math
import os
import re
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from .._reconcile._budget import Budget, read_history, save_history
from ._selected_harness import SelectedRuntimeFence, require_selected_runtime

RECOVERY_NOTICE = (
    "You were restarted after an API account failure. Ephemeral subagents may "
    "have disappeared; their files and Cards remain. Read your operator inbox "
    "and card notifications, recover outstanding work from durable files and "
    "Cards, and recreate subagents only when needed. Preserve your assigned "
    "scope. If the approved accounts are exhausted, report the reset and wait."
)
_LABEL = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


@dataclass(frozen=True)
class ApiRouteState:
    """Public, measured account state; contains no credentials or private body."""

    account: str
    status: str
    observed_at: float
    reset_at: float | None = None


@dataclass(frozen=True)
class SalvageIdentity:
    """Exact controller-owned runtime fence, including its selected API route."""

    agent: str
    instance_id: str
    session_id: str
    boot_id: str
    harness: str
    engine: str
    provider: str
    model: str


@dataclass(frozen=True)
class SalvageProof:
    """One independently observed completed-tool and server-nonce result."""

    identity: SalvageIdentity
    account: str
    exchange_id: str
    handshake_proven: bool | None
    tools_completed: int


def _write_checkpoint(path: Path, values: dict) -> None:
    """Durably replace the owned checkpoint before any restart is attempted."""
    descriptor, temporary = tempfile.mkstemp(prefix=".salvage-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(values, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _selected_identity(config):
    from ..config._hermes_config import compile_hermes_config
    from ..runtimes._hermes_profile import _launch_plan

    rendered = compile_hermes_config(
        _launch_plan(config, launch_mode="tui"), workdir=str(config.workdir)
    )
    return (
        config.name,
        config.harness,
        config.engine_key,
        rendered["model"]["provider"],
        config.model,
    )


def _restart(
    config,
    identity: SalvageIdentity,
    *,
    registry=None,
    restart=None,
    expected_runtime: SelectedRuntimeFence | None = None,
    observe_runtime: Callable[[object], SelectedRuntimeFence] | None = None,
):
    """Resolve the real successor before entering the ordinary restart seam.

    The optional registry and restart callable are the same owned filesystem
    and effect boundary used by the existing lifecycle tests.
    """
    from .._state.registry import Registry
    from ..config import load_config, resolve_config
    from ..config._engine_types import apply_engine, select_engine
    from ._stop import agent_restart

    registry = registry if registry is not None else Registry()
    entry = registry.get(identity.agent)
    path = entry["config"] if entry is not None else resolve_config(identity.agent)
    successor = load_config(path)
    selection = {}
    if successor.harness != identity.harness:
        if expected_runtime is None or observe_runtime is None:
            raise ValueError("salvage-canonical-successor-mismatch")
        successor = load_config(
            path, harness_override=identity.harness, engine_override=identity.engine
        )
        selection["harness_override"] = identity.harness
    engine = select_engine(successor.engines, identity.engine)
    if engine is not None:
        apply_engine(successor, engine)
    # A per-call projection requires a real owned observation. A mere in-memory
    # override still cannot stop the canonical native runtime.
    if successor.harness != "hermes" or _selected_identity(
        successor
    ) != _selected_identity(config):
        raise ValueError("salvage-canonical-successor-mismatch")
    if expected_runtime is not None or observe_runtime is not None:
        require_selected_runtime(successor, expected_runtime, observe_runtime)
        selection.update(
            expected_runtime=expected_runtime, observe_runtime=observe_runtime
        )
    restart = restart if restart is not None else agent_restart
    return restart(
        identity.agent,
        registry,
        engine_override=identity.engine,
        probe_engine=False,
        **selection,
    )


def salvage_once(
    config,
    identity: SalvageIdentity,
    *,
    failure_kind: str,
    failed_account: str,
    failed_at: float,
    accounts: tuple[ApiRouteState, ...],
    observe: Callable[[], SalvageIdentity],
    verify: Callable[[str, str], SalvageProof],
    checkpoint: Path | None = None,
    history: Path | None = None,
    restart: Callable[[object, SalvageIdentity], object] = _restart,
    now: float | None = None,
    expected_runtime: SelectedRuntimeFence | None = None,
    observe_runtime: Callable[[object], SelectedRuntimeFence] | None = None,
) -> dict:
    """Restart at most once, then require a real fenced tool/nonce observation.

    The selected declaration must match the live API harness and engine. A
    default-Codex spec requires the owning controller's complete runtime fence
    and observer for an explicit, already-declared Hermes projection.
    ``verify`` delivers RECOVERY_NOTICE through the existing owned route and
    observes one real Hermes RPC and Cards/tool nonce checkpoint. Its failure
    is WAIT, never a retry; Codex rollout capture cannot prove a Hermes turn.
    The normal restart still owns provider admission, teardown, bot/session
    preservation, native pool selection, and the target namespace Python gate.
    """
    now = time.time() if now is None else now
    if (
        failure_kind not in {"quota", "rate-limit", "auth-rejected"}
        or not math.isfinite(now)
        or not math.isfinite(failed_at)
        or failed_at > now
        or now - failed_at > 300
        or not _LABEL.fullmatch(failed_account)
    ):
        raise ValueError("salvage-failure-evidence-refused")
    if config.harness != "hermes" or config.subscription_provider:
        raise ValueError("salvage-selected-api-harness-required")
    selected = _selected_identity(config)
    expected = (
        identity.agent,
        identity.harness,
        identity.engine,
        identity.provider,
        identity.model,
    )
    if (
        selected != expected
        or not identity.instance_id
        or not identity.session_id
        or not identity.boot_id
    ):
        raise ValueError("salvage-selected-runtime-mismatch")
    if expected_runtime is not None and (
        not isinstance(expected_runtime, SelectedRuntimeFence)
        or (
            expected_runtime.agent,
            expected_runtime.instance_id,
            expected_runtime.session_id,
            expected_runtime.boot_id,
            expected_runtime.harness,
            expected_runtime.engine,
            expected_runtime.provider,
            expected_runtime.model,
            expected_runtime.account,
        )
        != (
            identity.agent,
            identity.instance_id,
            identity.session_id,
            identity.boot_id,
            identity.harness,
            identity.engine,
            identity.provider,
            identity.model,
            failed_account,
        )
    ):
        raise ValueError("salvage-owned-route-mismatch")
    declared = config.hermes_failover.accounts.get(config.engine_key, [])
    aliases = [row.account for row in accounts]
    if (
        not declared
        or failed_account not in declared
        or len(set(aliases)) != len(aliases)
        or any(
            not _LABEL.fullmatch(alias) or alias not in declared for alias in aliases
        )
    ):
        raise ValueError("salvage-undeclared-account-refused")
    eligible = [
        row.account
        for row in accounts
        if row.status == "available"
        and math.isfinite(row.observed_at)
        and failed_at <= row.observed_at <= now
        and (
            row.account != failed_account
            or (
                failure_kind != "auth-rejected"
                and row.reset_at is not None
                and math.isfinite(row.reset_at)
                and failed_at < row.reset_at <= now
            )
        )
    ]
    result = {
        "state": "wait",
        "reason": "no-eligible-account",
        "restarted": False,
        "identity": asdict(identity),
        "eligible_accounts": eligible,
        "failure_kind": failure_kind,
        "failed_at": failed_at,
    }
    from .._state.state_paths import runtime_root

    checkpoint = (
        checkpoint
        if checkpoint is not None
        else runtime_root() / identity.agent / "api-salvage.json"
    )
    history = (
        history
        if history is not None
        else runtime_root() / "api-salvage-restart-history.json"
    )
    checkpoint.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    history.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # A second local controller may inspect while the first is verifying. It
    # must not bounce the same runtime concurrently or forget a spent attempt.
    with history.with_suffix(".lock").open("a") as claim:
        try:
            fcntl.flock(claim, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {**result, "reason": "salvage-in-progress"}
        if observe() != identity:
            return {**result, "reason": "runtime-fence-changed"}
        if not eligible:
            _write_checkpoint(checkpoint, result)
            return result
        memory = read_history(history)
        if not memory.enforceable:
            return {**result, "reason": "restart-budget-unknown"}
        budget = Budget(memory.history, pass_cap=1)
        decision = budget.check(identity.agent, now)
        if not decision.allowed:
            return {**result, "reason": decision.reason}
        budget.record(identity.agent, now)
        save_history(history, budget.history, now=now)
        _write_checkpoint(checkpoint, {**result, "state": "restart-attempted"})
        # The budget is spent before the call, including a killed controller.
        # stx-allow: fallback (reason: a restart/verification failure becomes a
        # fixed WAIT checkpoint at runtime/<agent>/api-salvage.json; private
        # exception text is never persisted and the spent budget is retained)
        try:
            if restart is _restart:
                restarted = _restart(
                    config,
                    identity,
                    expected_runtime=expected_runtime,
                    observe_runtime=observe_runtime,
                )
            else:
                restarted = restart(config, identity)
        except Exception:
            result["reason"] = "restart-failed"
            _write_checkpoint(checkpoint, result)
            return result
        if restarted is not True:
            result["reason"] = "restart-unproven"
        else:
            result["restarted"] = True
            # stx-allow: fallback (reason: a failed owned proof reader becomes
            # a fixed WAIT checkpoint; no exception body or false recovery)
            try:
                proof = verify(identity.agent, RECOVERY_NOTICE)
            except Exception:
                result["reason"] = "verification-failed"
                _write_checkpoint(checkpoint, result)
                return result
            if not isinstance(proof, SalvageProof) or not isinstance(
                proof.identity, SalvageIdentity
            ):
                result["reason"] = "recovery-unproven"
                _write_checkpoint(checkpoint, result)
                return result
            after = proof.identity
            stable = (
                after.agent,
                after.session_id,
                after.harness,
                after.engine,
                after.provider,
                after.model,
            )
            before = (
                identity.agent,
                identity.session_id,
                identity.harness,
                identity.engine,
                identity.provider,
                identity.model,
            )
            from scitex_dev.status import is_exchange_id

            if (
                stable == before
                and after.boot_id != identity.boot_id
                and after.boot_id
                and after.instance_id
                and proof.account in eligible
                and proof.handshake_proven is True
                and is_exchange_id(proof.exchange_id)
                and type(proof.tools_completed) is int
                and proof.tools_completed > 0
            ):
                result.update(
                    state="recovered",
                    reason="tool-and-nonce-proven",
                    account=proof.account,
                    exchange_id=proof.exchange_id,
                )
            else:
                result["reason"] = "recovery-unproven"
        _write_checkpoint(checkpoint, result)
        return result
