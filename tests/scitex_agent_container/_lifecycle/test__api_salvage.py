"""One-shot recovery contracts exercised with real owned checkpoints and locks."""

from __future__ import annotations

import fcntl
import json
import multiprocessing
import os
import signal
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from scitex_agent_container._lifecycle._api_salvage import (
    RECOVERY_NOTICE,
    ApiRouteState,
    SalvageIdentity,
    SalvageProof,
    _restart,
    salvage_once,
)
from scitex_agent_container._lifecycle._selected_harness import SelectedRuntimeFence
from scitex_agent_container._runners._tmux._process_group import _identity
from scitex_agent_container._state.registry import Registry
from scitex_agent_container.config import AgentConfig, load_config
from scitex_agent_container.config._hermes_failover import HermesFailoverSpec
from scitex_agent_container.config._provider_types import ProviderSpec


@pytest.fixture
def recovery(tmp_path):
    config = AgentConfig(name="app", runtime="tui", harness="hermes")
    config.engine_key = "go-muse"
    config.model = "muse-spark-1.3-contributor"
    config.workdir = str(tmp_path)
    config.max_context_tokens = 1_048_576
    config.claude.provider = ProviderSpec(
        base_url="", auth_token_env="GO_ONE", hermes_provider="opencode-go"
    )
    config.hermes_failover = HermesFailoverSpec(
        accounts={"go-muse": ["GO_ONE", "GO_TWO"]}
    )
    identity = SalvageIdentity(
        agent="app",
        instance_id="instance-before",
        session_id="same-session",
        boot_id="boot-before",
        harness="hermes",
        engine="go-muse",
        provider="opencode-go",
        model=config.model,
    )
    proof = SalvageProof(
        identity=replace(identity, instance_id="instance-after", boot_id="boot-after"),
        account="GO_TWO",
        exchange_id="xch_20261003T040000Z_scitex-compute-03_ab1234",
        handshake_proven=True,
        tools_completed=1,
    )
    effects = []
    checkpoint = tmp_path / "api-salvage.json"
    history = tmp_path / "api-salvage-restart-history.json"

    def restart(selected, observed):
        effects.append(("restart", selected.model, observed.session_id))
        return True

    def verify(name, notice):
        effects.append(("verify", name, notice))
        return proof

    arguments = {
        "failure_kind": "quota",
        "failed_account": "GO_ONE",
        "failed_at": 1000.0,
        "accounts": (ApiRouteState("GO_TWO", "available", 1001.0),),
        "observe": lambda: identity,
        "verify": verify,
        "restart": restart,
        "checkpoint": checkpoint,
        "history": history,
        "now": 1002.0,
    }
    return config, identity, proof, arguments, effects


def invoke(recovery, **overrides):
    config, identity, _proof, arguments, _effects = recovery
    return salvage_once(config, identity, **{**arguments, **overrides})


def test_one_fenced_tool_and_nonce_recovers(recovery):
    # Arrange
    operation = recovery
    # Act
    result = invoke(operation)
    # Assert
    assert result["state"] == "recovered"


def test_completed_recovery_retains_session(recovery):
    # Arrange
    _config, identity, _proof, _arguments, effects = recovery
    # Act
    invoke(recovery)
    # Assert
    assert effects[0][2] == identity.session_id


def test_recovery_notice_requires_durable_work_recovery(recovery):
    # Arrange
    _config, _identity, _proof, _arguments, effects = recovery
    # Act
    invoke(recovery)
    # Assert
    assert effects[1][2] == RECOVERY_NOTICE


def test_checkpoint_exists_before_restart(recovery):
    # Arrange
    _config, _identity, _proof, arguments, _effects = recovery
    observed = []

    def restart(_config, _identity):
        observed.append(json.loads(arguments["checkpoint"].read_text())["state"])
        return False

    # Act
    invoke(recovery, restart=restart)
    # Assert
    assert observed == ["restart-attempted"]


def test_budget_is_spent_before_restart(recovery):
    # Arrange
    _config, _identity, _proof, arguments, _effects = recovery
    observed = []

    def restart(_config, _identity):
        observed.append(json.loads(arguments["history"].read_text()))
        return False

    # Act
    invoke(recovery, restart=restart)
    # Assert
    assert observed == [{"app": [1002.0]}]


@pytest.mark.parametrize(
    "status", ["capped", "cooldown", "auth-invalid", "unknown", "stale"]
)
def test_unavailable_account_never_restarts(recovery, status):
    # Arrange
    _config, _identity, _proof, _arguments, effects = recovery
    # Act
    invoke(recovery, accounts=(ApiRouteState("GO_TWO", status, 1001.0, 2000.0),))
    # Assert
    assert effects == []


def test_unavailable_account_leaves_durable_wait(recovery):
    # Arrange
    _config, _identity, _proof, arguments, _effects = recovery
    # Act
    invoke(recovery, accounts=(ApiRouteState("GO_TWO", "capped", 1001.0, 2000.0),))
    # Assert
    assert (
        json.loads(arguments["checkpoint"].read_text())["reason"]
        == "no-eligible-account"
    )


def test_pre_failure_quota_is_not_recovery_authority(recovery):
    # Arrange
    _config, _identity, _proof, _arguments, effects = recovery
    # Act
    invoke(recovery, accounts=(ApiRouteState("GO_TWO", "available", 999.0),))
    # Assert
    assert effects == []


def test_future_observation_is_not_recovery_authority(recovery):
    # Arrange
    _config, _identity, _proof, _arguments, effects = recovery
    # Act
    invoke(recovery, accounts=(ApiRouteState("GO_TWO", "available", 1003.0),))
    # Assert
    assert effects == []


def test_failed_account_is_not_immediately_retried(recovery):
    # Arrange
    _config, _identity, _proof, _arguments, effects = recovery
    # Act
    invoke(recovery, accounts=(ApiRouteState("GO_ONE", "available", 1001.0, 2000.0),))
    # Assert
    assert effects == []


def test_reset_account_requires_new_available_measurement(recovery):
    # Arrange
    _config, _identity, _proof, _arguments, effects = recovery
    # Act
    invoke(recovery, accounts=(ApiRouteState("GO_ONE", "capped", 1001.0, 1001.0),))
    # Assert
    assert effects == []


def test_auth_rejected_account_is_not_reused_at_reset(recovery):
    # Arrange
    _config, _identity, _proof, _arguments, effects = recovery
    # Act
    invoke(
        recovery,
        failure_kind="auth-rejected",
        accounts=(ApiRouteState("GO_ONE", "available", 1001.0, 1001.0),),
    )
    # Assert
    assert effects == []


def test_fresh_reset_account_can_be_attempted(recovery):
    # Arrange
    _config, _identity, proof, _arguments, _effects = recovery
    proof = replace(proof, account="GO_ONE")
    # Act
    result = invoke(
        recovery,
        accounts=(ApiRouteState("GO_ONE", "available", 1001.0, 1001.0),),
        verify=lambda *_: proof,
    )
    # Assert
    assert result["state"] == "recovered"


def test_native_subscription_is_refused(recovery):
    # Arrange
    config = recovery[0]
    config.harness = "codex"

    # Act
    def operation():
        invoke(recovery)

    # Assert
    with pytest.raises(ValueError, match="selected-api-harness-required"):
        operation()


def test_wrong_model_is_refused(recovery):
    # Arrange
    recovery[0].model = "another-model"

    # Act
    def operation():
        invoke(recovery)

    # Assert
    with pytest.raises(ValueError, match="selected-runtime-mismatch"):
        operation()


def test_undeclared_account_is_refused(recovery):
    # Arrange
    accounts = (ApiRouteState("ANOTHER_ACCOUNT", "available", 1001.0),)

    # Act
    def operation():
        invoke(recovery, accounts=accounts)

    # Assert
    with pytest.raises(ValueError, match="undeclared-account-refused"):
        operation()


def test_changed_incarnation_is_not_restarted(recovery):
    # Arrange
    identity = replace(recovery[1], boot_id="different-boot")
    # Act
    result = invoke(recovery, observe=lambda: identity)
    # Assert
    assert result["reason"] == "runtime-fence-changed"


@pytest.mark.parametrize(
    "proof_delta",
    [
        {"handshake_proven": None},
        {"handshake_proven": False},
        {"tools_completed": 0},
        {"tools_completed": True},
        {"account": "GO_ONE"},
        {"exchange_id": "HTTP202"},
    ],
)
def test_transport_or_unqualified_proof_is_wait(recovery, proof_delta):
    # Arrange
    proof = replace(recovery[2], **proof_delta)
    # Act
    result = invoke(recovery, verify=lambda *_: proof)
    # Assert
    assert result["reason"] == "recovery-unproven"


def test_old_boot_proof_is_wait(recovery):
    # Arrange
    proof = replace(recovery[2], identity=recovery[1])
    # Act
    result = invoke(recovery, verify=lambda *_: proof)
    # Assert
    assert result["reason"] == "recovery-unproven"


def test_second_invocation_reuses_persistent_restart_budget(recovery):
    # Arrange
    invoke(recovery, restart=lambda *_: False)
    # Act
    result = invoke(recovery, now=1003.0)
    # Assert
    assert result["reason"] == "debounce"


def test_unreadable_restart_memory_refuses(recovery):
    # Arrange
    history = recovery[3]["history"]
    history.write_text("broken JSON")
    # Act
    result = invoke(recovery)
    # Assert
    assert result["reason"] == "restart-budget-unknown"


def test_existing_real_lock_prevents_concurrent_restart(recovery):
    # Arrange
    lock = recovery[3]["history"].with_suffix(".lock")
    # Act
    with lock.open("a") as claimed:
        fcntl.flock(claimed, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = invoke(recovery)
    # Assert
    assert result["reason"] == "salvage-in-progress"


def test_restart_failure_leaves_wait(recovery):
    # Arrange
    def restart(*_):
        raise RuntimeError("synthetic-private-provider-body")

    # Act
    result = invoke(recovery, restart=restart)
    # Assert
    assert result["reason"] == "restart-failed"


def test_restart_failure_does_not_persist_private_error_body(recovery):
    # Arrange
    def restart(*_):
        raise RuntimeError("synthetic-private-provider-body")

    # Act
    invoke(recovery, restart=restart)
    checkpoint = recovery[3]["checkpoint"].read_text()
    # Assert
    assert "synthetic-private-provider-body" not in checkpoint


def test_verification_failure_does_not_retry_restart(recovery):
    # Arrange
    def verify(*_):
        raise OSError("synthetic-private-proof-body")

    # Act
    result = invoke(recovery, verify=verify)
    # Assert
    assert result["reason"] == "verification-failed"


def test_missing_proof_is_wait(recovery):
    # Arrange
    def verify(*_):
        return None

    # Act
    result = invoke(recovery, verify=verify)
    # Assert
    assert result["reason"] == "recovery-unproven"


def _crash_controller(config, identity, checkpoint, history):
    def kill_after_checkpoint(*_):
        os.kill(os.getpid(), signal.SIGKILL)

    salvage_once(
        config,
        identity,
        failure_kind="quota",
        failed_account="GO_ONE",
        failed_at=1000.0,
        accounts=(ApiRouteState("GO_TWO", "available", 1001.0),),
        observe=lambda: identity,
        verify=lambda *_: None,
        checkpoint=checkpoint,
        history=history,
        restart=kill_after_checkpoint,
        now=1002.0,
    )


@pytest.fixture
def killed_controller(recovery):
    config, identity, _proof, arguments, _effects = recovery
    process = multiprocessing.get_context("spawn").Process(
        target=_crash_controller,
        args=(config, identity, arguments["checkpoint"], arguments["history"]),
    )
    process.start()
    process.join(2)
    if process.is_alive():
        process.kill()
        process.join(2)
        raise RuntimeError("owned-test-controller-did-not-exit")
    return process.exitcode, invoke(recovery, now=1003.0), recovery[3]["checkpoint"]


def test_crashed_controller_is_real_sigkill(killed_controller):
    # Arrange
    exitcode, _result, _checkpoint = killed_controller
    # Act
    observed = exitcode
    # Assert
    assert observed == -signal.SIGKILL


def test_crashed_controller_does_not_spend_another_restart(killed_controller):
    # Arrange
    _exitcode, result, _checkpoint = killed_controller
    # Act
    reason = result["reason"]
    # Assert
    assert reason == "debounce"


def test_crashed_controller_retains_attempt_checkpoint(killed_controller):
    # Arrange
    _exitcode, _result, checkpoint = killed_controller
    # Act
    observed = json.loads(checkpoint.read_text())["state"]
    # Assert
    assert observed == "restart-attempted"


@pytest.fixture
def canonical_restart(tmp_path):
    from ..config.test__selected_harness import _write_declaration

    path, raw = _write_declaration(tmp_path)
    raw["spec"]["harness"] = "hermes"
    raw["spec"]["engine"] = "go-muse"
    path.write_text(yaml.safe_dump(raw))
    config = load_config(path)
    registry = Registry(tmp_path / "registry")
    registry.add(config.name, str(path), config.screen_name)
    effects = []

    def restart(name, owned_registry, **kwargs):
        effects.append((name, owned_registry.get(name)["config"], kwargs))
        return True

    identity = SalvageIdentity(
        config.name,
        "instance",
        "session",
        "boot",
        "hermes",
        "go-muse",
        "opencode-go",
        config.model,
    )
    process = _identity(os.getpid())
    fence = SelectedRuntimeFence(
        config.name,
        identity.instance_id,
        identity.session_id,
        identity.boot_id,
        identity.harness,
        identity.engine,
        identity.provider,
        identity.model,
        "TEST_GO_ONE",
        process.pid,
        process.start_time,
        process.uid,
        Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
    )
    return config, identity, registry, restart, effects, path, fence


def test_canonical_successor_reuses_ordinary_restart(canonical_restart):
    # Arrange
    config, identity, registry, restart, effects, _path, fence = canonical_restart
    # Act
    _restart(
        config,
        identity,
        registry=registry,
        restart=restart,
        expected_runtime=fence,
        observe_runtime=lambda _config: fence,
    )
    # Assert
    assert effects[0][2]["engine_override"] == "go-muse"


def test_canonical_codex_default_without_owned_fence_is_refused_before_effect(
    canonical_restart,
):
    # Arrange
    config, identity, registry, restart, _effects, path, _fence = canonical_restart
    raw = yaml.safe_load(path.read_text())
    raw["spec"]["harness"] = "codex"
    path.write_text(yaml.safe_dump(raw))

    # Act
    def operation():
        _restart(config, identity, registry=registry, restart=restart)

    # Assert
    with pytest.raises(ValueError, match="owned-runtime-required"):
        operation()


def test_canonical_refusal_never_calls_restart(canonical_restart):
    # Arrange
    config, identity, registry, restart, effects, path, fence = canonical_restart
    raw = yaml.safe_load(path.read_text())
    raw["spec"]["available_engines"]["go-muse"]["model"] = "another-model"
    path.write_text(yaml.safe_dump(raw))
    # Act
    try:
        _restart(
            config,
            identity,
            registry=registry,
            restart=restart,
            expected_runtime=fence,
            observe_runtime=lambda _config: fence,
        )
    except ValueError:
        pass
    # Assert
    assert effects == []
