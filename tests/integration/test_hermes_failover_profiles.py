"""Exercise SAC's generated profiles with a real, explicitly selected Hermes checkout."""

import json
import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from scitex_agent_container.config import AgentConfig, ProviderSpec
from scitex_agent_container.config._engine_types import parse_engine_entry
from scitex_agent_container.config._hermes_config import compile_hermes_config
from scitex_agent_container.config._hermes_failover import HermesFailoverSpec
from scitex_agent_container.runtimes import _hermes_profile
from scitex_agent_container.runtimes._hermes_failover import (
    configure_failover,
    materialize_pools,
)

PROVIDERS = ("opencode-go", "custom:sac-command")


def _declared_env():
    return {
        "OPENCODE_GO_API_KEY": "fake-undeclared-base",
        "OPENCODE_GO_API_KEY_1": "fake-go-a",
        "OPENCODE_GO_API_KEY_2": "fake-go-b",
        "COMMANDCODE_API_KEY": "fake-undeclared-command",
        "COMMANDCODE_API_KEY_01": "fake-command-a",
        "COMMANDCODE_API_KEY_02": "fake-command-b",
    }


@contextmanager
def _hermes_lab(tmp_path, label):
    """Real Hermes resolver imports plus isolated env, restored afterwards."""
    source = _require_hermes_source(
        "Set SAC_HERMES_SOURCE_DIR to validate the real Hermes resolver"
    )
    sys.path.insert(0, source)
    profile = tmp_path / label
    mapping = {
        **_declared_env(),
        "HOME": str(tmp_path),
        "HERMES_HOME": str(profile),
        "HERMES_RUNTIME_DIR": str(profile / "runtime"),
    }
    previous = {name: os.environ.get(name) for name in mapping}
    os.environ.update(mapping)
    try:
        from agent.credential_pool import load_pool
        from hermes_cli.auth import primary_failure_wording
        from hermes_cli.runtime_provider import AuthError, resolve_runtime_provider

        yield SimpleNamespace(
            profile=profile,
            load_pool=load_pool,
            resolve=resolve_runtime_provider,
            auth_error=AuthError,
            wording=primary_failure_wording,
        )
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        sys.path.remove(source)


def _build():
    config = AgentConfig(name="infra", harness="hermes", runtime="tui", workdir="/work")
    config.engine_key, config.model = "go", "muse-spark-1.3-contributor"
    config.claude.provider = ProviderSpec(
        hermes_provider="opencode-go", auth_token_env="OPENCODE_GO_API_KEY"
    )
    config.engines["command"] = parse_engine_entry(
        "command",
        {
            "model": "meta/muse-spark-1.3-contributor",
            "provider": {
                "base_url": "https://api.commandcode.ai/provider/v1",
                "auth_token_env": "COMMANDCODE_API_KEY",
            },
        },
    )
    config.hermes_failover = HermesFailoverSpec(
        accounts={
            "go": ["OPENCODE_GO_API_KEY_1", "OPENCODE_GO_API_KEY_2"],
            "command": ["COMMANDCODE_API_KEY_01", "COMMANDCODE_API_KEY_02"],
        },
        engines=["command"],
    )
    rendered = compile_hermes_config(
        _hermes_profile._launch_plan(config, launch_mode="tui"), workdir="/work"
    )
    env, pools = configure_failover(config, rendered)
    return rendered, env, pools


def _materialize(profile, rendered, env, pools):
    profile.mkdir(exist_ok=True)
    (profile / "config.yaml").write_text(yaml.safe_dump(rendered))
    _hermes_profile._write_profile_env(profile / ".env", env)
    materialize_pools(profile, pools)


def _quota_context(message=True):
    context = {"reset_at": time.time() + 16 * 86400}
    if message:
        context["message"] = "Go usage limit exceeded"
    return context


@pytest.mark.parametrize("provider", PROVIDERS)
def test_generated_pool_holds_two_entries_per_provider(tmp_path, provider):
    # Arrange
    with _hermes_lab(tmp_path, "arc") as lab:
        rendered, env, pools = _build()
        _materialize(lab.profile, rendered, env, pools)
        # Act
        pool = lab.load_pool(provider)
    # Assert
    assert len(pool.entries()) == 2


@pytest.mark.parametrize("provider", PROVIDERS)
def test_quota_rotation_hands_over_to_the_declared_sibling(tmp_path, provider):
    # Arrange
    with _hermes_lab(tmp_path, "arc") as lab:
        rendered, env, pools = _build()
        _materialize(lab.profile, rendered, env, pools)
        runtime = lab.resolve(requested=provider)
        first = runtime["credential_pool"].select()
        # Act
        handed = runtime["credential_pool"].mark_exhausted_and_rotate(
            status_code=429,
            api_key_hint=first.runtime_api_key,
            error_context=_quota_context(),
        )
    # Assert
    assert (handed is not None, handed.label != first.label) == (True, True)


@pytest.mark.parametrize("provider", PROVIDERS)
def test_second_quota_rotation_leases_nothing(tmp_path, provider):
    # Arrange
    with _hermes_lab(tmp_path, "arc") as lab:
        rendered, env, pools = _build()
        _materialize(lab.profile, rendered, env, pools)
        runtime = lab.resolve(requested=provider)
        credential_pool = runtime["credential_pool"]
        first = credential_pool.select()
        handed = credential_pool.mark_exhausted_and_rotate(
            status_code=429,
            api_key_hint=first.runtime_api_key,
            error_context=_quota_context(),
        )
        # Act
        drained = credential_pool.mark_exhausted_and_rotate(
            status_code=429,
            api_key_hint=handed.runtime_api_key,
            error_context=_quota_context(message=False),
        )
    # Assert
    assert drained is None


@pytest.mark.parametrize("provider", PROVIDERS)
def test_exhausted_pool_refuses_with_next_reset(tmp_path, provider):
    # Arrange
    with _hermes_lab(tmp_path, "arc") as lab:
        rendered, env, pools = _build()
        _materialize(lab.profile, rendered, env, pools)
        runtime = lab.resolve(requested=provider)
        credential_pool = runtime["credential_pool"]
        first = credential_pool.select()
        handed = credential_pool.mark_exhausted_and_rotate(
            status_code=429,
            api_key_hint=first.runtime_api_key,
            error_context=_quota_context(),
        )
        credential_pool.mark_exhausted_and_rotate(
            status_code=429,
            api_key_hint=handed.runtime_api_key,
            error_context=_quota_context(message=False),
        )
        # Act
        call = lambda: lab.resolve(requested=provider)  # noqa: E731
        # Assert
        with pytest.raises(
            lab.auth_error, match="All credentials exhausted.*next reset"
        ):
            call()


@pytest.mark.parametrize("provider", PROVIDERS)
def test_exhaustion_wording_names_quota(tmp_path, provider):
    # Arrange
    with _hermes_lab(tmp_path, "arc") as lab:
        rendered, env, pools = _build()
        _materialize(lab.profile, rendered, env, pools)
        runtime = lab.resolve(requested=provider)
        credential_pool = runtime["credential_pool"]
        first = credential_pool.select()
        handed = credential_pool.mark_exhausted_and_rotate(
            status_code=429,
            api_key_hint=first.runtime_api_key,
            error_context=_quota_context(),
        )
        credential_pool.mark_exhausted_and_rotate(
            status_code=429,
            api_key_hint=handed.runtime_api_key,
            error_context=_quota_context(message=False),
        )
        # Act
        try:
            lab.resolve(requested=provider)
        except lab.auth_error as refused:
            wording = lab.wording(refused)
        else:
            wording = None
    # Assert
    assert wording is not None and wording[0] == "quota exhausted"


@pytest.mark.parametrize("provider", PROVIDERS)
def test_exhaustion_is_not_retryable(tmp_path, provider):
    # Arrange
    with _hermes_lab(tmp_path, "arc") as lab:
        rendered, env, pools = _build()
        _materialize(lab.profile, rendered, env, pools)
        runtime = lab.resolve(requested=provider)
        credential_pool = runtime["credential_pool"]
        first = credential_pool.select()
        handed = credential_pool.mark_exhausted_and_rotate(
            status_code=429,
            api_key_hint=first.runtime_api_key,
            error_context=_quota_context(),
        )
        credential_pool.mark_exhausted_and_rotate(
            status_code=429,
            api_key_hint=handed.runtime_api_key,
            error_context=_quota_context(message=False),
        )
        # Act
        try:
            lab.resolve(requested=provider)
        except lab.auth_error as refused:
            retryable = refused.retryable
        else:
            retryable = None
    # Assert
    assert retryable is False


def test_exhausted_pool_keeps_both_rows_for_audit(tmp_path):
    # Arrange
    with _hermes_lab(tmp_path, "arc") as lab:
        rendered, env, pools = _build()
        _materialize(lab.profile, rendered, env, pools)
        runtime = lab.resolve(requested="opencode-go")
        credential_pool = runtime["credential_pool"]
        first = credential_pool.select()
        handed = credential_pool.mark_exhausted_and_rotate(
            status_code=429,
            api_key_hint=first.runtime_api_key,
            error_context=_quota_context(),
        )
        credential_pool.mark_exhausted_and_rotate(
            status_code=429,
            api_key_hint=handed.runtime_api_key,
            error_context=_quota_context(message=False),
        )
        # Act
        rows = json.loads((lab.profile / "auth.json").read_text())["credential_pool"][
            "opencode-go"
        ]
    # Assert
    assert len(rows) == 2


def _diagnostic_pools():
    from scitex_agent_container.runtimes._hermes_failover import DeclaredPool

    rows = [
        {
            "id": f"sac-{index}",
            "label": f"KEY_{index}",
            "source": f"manual:sac:KEY_{index}",
            "auth_type": "api_key",
            "priority": index,
            "access_token": f"fake-key-{index}",
        }
        for index in range(2)
    ]
    return {"custom:sac-diagnostic": DeclaredPool(rows, [])}, rows


def test_rejected_static_key_rotates_to_its_sibling(tmp_path):
    # Arrange
    with _hermes_lab(tmp_path, "diagnostic") as lab:
        import agent.credential_pool as pool_module

        pools, _rows = _diagnostic_pools()
        materialize_pools(lab.profile, pools)
        pool = pool_module.load_pool("custom:sac-diagnostic")
        first = pool.select()
        # Act
        replacement = pool.mark_exhausted_and_rotate(
            status_code=401,
            credential_id=first.id,
            error_context={"message": "Invalid 'Authorization' header or token."},
            failure_reason="auth",
        )
    # Assert
    assert (replacement is not None, replacement.id != first.id) == (True, True)


def test_rejected_static_key_stays_dead_for_audit(tmp_path):
    # Arrange
    with _hermes_lab(tmp_path, "diagnostic") as lab:
        import agent.credential_pool as pool_module

        pools, _rows = _diagnostic_pools()
        materialize_pools(lab.profile, pools)
        pool = pool_module.load_pool("custom:sac-diagnostic")
        first = pool.select()
        # Act
        pool.mark_exhausted_and_rotate(
            status_code=401,
            credential_id=first.id,
            error_context={"message": "Invalid 'Authorization' header or token."},
            failure_reason="auth",
        )
    # Assert
    assert next(row for row in pool.entries() if row.id == first.id).last_status == (
        "dead"
    )


def test_reload_keeps_rejected_key_and_leases_sibling(tmp_path):
    # Arrange
    with _hermes_lab(tmp_path, "diagnostic") as lab:
        import agent.credential_pool as pool_module

        pools, _rows = _diagnostic_pools()
        materialize_pools(lab.profile, pools)
        pool = pool_module.load_pool("custom:sac-diagnostic")
        first = pool.select()
        replacement = pool.mark_exhausted_and_rotate(
            status_code=401,
            credential_id=first.id,
            error_context={"message": "Invalid 'Authorization' header or token."},
            failure_reason="auth",
        )
        # Act
        materialize_pools(lab.profile, pools)
        reloaded = pool_module.load_pool("custom:sac-diagnostic")
    # Assert
    assert (len(reloaded.entries()), reloaded.select().id) == (2, replacement.id)


def test_replaced_key_is_leased_without_old_rejection(tmp_path):
    # Arrange
    with _hermes_lab(tmp_path, "diagnostic") as lab:
        import agent.credential_pool as pool_module

        pools, rows = _diagnostic_pools()
        materialize_pools(lab.profile, pools)
        pool = pool_module.load_pool("custom:sac-diagnostic")
        first = pool.select()
        pool.mark_exhausted_and_rotate(
            status_code=401,
            credential_id=first.id,
            error_context={"message": "Invalid 'Authorization' header or token."},
            failure_reason="auth",
        )
        pools["custom:sac-diagnostic"].credentials[0] = {
            **rows[0],
            "id": "sac-replacement",
            "access_token": "fake-new-key",
        }
        # Act
        materialize_pools(lab.profile, pools)
        selected = pool_module.load_pool("custom:sac-diagnostic").select()
    # Assert
    assert selected.id == "sac-replacement"


def _require_hermes_source(reason):
    source = os.environ.get("SAC_HERMES_SOURCE_DIR")
    if not source:
        pytest.skip(reason)
    return source


def test_generated_profiles_cascade_through_real_http_errors():
    # Arrange
    import subprocess
    import sys

    _require_hermes_source("Set SAC_HERMES_SOURCE_DIR to validate a real Hermes turn")
    command = [
        sys.executable,
        str(Path(__file__).with_name("_hermes_cascade_probe.py")),
    ]
    # Act
    result = subprocess.run(command, capture_output=True, text=True, timeout=45)
    # Assert
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-3000:]
