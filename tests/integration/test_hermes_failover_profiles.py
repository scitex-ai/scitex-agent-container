"""Exercise SAC's generated profiles with a real, explicitly selected Hermes checkout."""

import json
import os
import time
from pathlib import Path

import pytest
import yaml

from scitex_agent_container.config import AgentConfig, ProviderSpec
from scitex_agent_container.config._engine_types import parse_engine_entry
from scitex_agent_container.config._hermes_config import compile_hermes_config
from scitex_agent_container.config._hermes_failover import HermesFailoverSpec
from scitex_agent_container.runtimes import _apptainer_provider, _hermes_profile
from scitex_agent_container.runtimes._hermes_failover import (
    configure_failover,
    materialize_pools,
)


def test_generated_profiles_rotate_and_exhaust_only_declared_accounts(
    tmp_path, monkeypatch
):
    source = os.environ.get("SAC_HERMES_SOURCE_DIR")
    if not source:
        pytest.skip("Set SAC_HERMES_SOURCE_DIR to validate the real Hermes resolver")
    monkeypatch.syspath_prepend(source)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(_apptainer_provider, "load_dotenv", lambda **kwargs: None)
    for name, value in {
        "OPENCODE_GO_API_KEY": "fake-undeclared-base",
        "OPENCODE_GO_API_KEY_1": "fake-go-a",
        "OPENCODE_GO_API_KEY_2": "fake-go-b",
        "COMMANDCODE_API_KEY": "fake-undeclared-command",
        "COMMANDCODE_API_KEY_01": "fake-command-a",
        "COMMANDCODE_API_KEY_02": "fake-command-b",
    }.items():
        monkeypatch.setenv(name, value)
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
    from agent.credential_pool import load_pool
    from hermes_cli.runtime_provider import AuthError, resolve_runtime_provider

    for label in ("A", "B", "A"):
        profile = tmp_path / label
        profile.mkdir(exist_ok=True)
        monkeypatch.setenv("HERMES_HOME", str(profile))
        monkeypatch.setenv("HERMES_RUNTIME_DIR", str(profile / "runtime"))
        (profile / "config.yaml").write_text(yaml.safe_dump(rendered))
        _hermes_profile._write_profile_env(profile / ".env", env)
        materialize_pools(profile, pools)
        for provider in ("opencode-go", "custom:sac-command"):
            pool = load_pool(provider)
            # Counts and labels, rather than token comparisons, keep failures
            # safe even if an unexpected ambient secret entered the resolver.
            assert len(pool.entries()) == 2
            if label == "A" and pool.select() is None:
                with pytest.raises(AuthError, match="All credentials exhausted"):
                    resolve_runtime_provider(requested=provider)
                continue
            runtime = resolve_runtime_provider(requested=provider)
            first = runtime["credential_pool"].select()
            next_key = runtime["credential_pool"].mark_exhausted_and_rotate(
                status_code=429,
                api_key_hint=first.runtime_api_key,
                error_context={
                    "message": "Go usage limit exceeded",
                    "reset_at": time.time() + 16 * 86400,
                },
            )
            assert next_key is not None and next_key.label != first.label
            assert (
                runtime["credential_pool"].mark_exhausted_and_rotate(
                    status_code=429,
                    api_key_hint=next_key.runtime_api_key,
                    error_context={"reset_at": time.time() + 16 * 86400},
                )
                is None
            )
            with pytest.raises(
                AuthError, match="All credentials exhausted.*next reset"
            ) as raised:
                resolve_runtime_provider(requested=provider)
            from hermes_cli.auth import primary_failure_wording

            assert primary_failure_wording(raised.value)[0] == "quota exhausted"
            assert raised.value.retryable is False
        assert (
            len(
                json.loads((profile / "auth.json").read_text())["credential_pool"][
                    "opencode-go"
                ]
            )
            == 2
        )


def test_rejected_static_key_rotates_and_stays_skipped_across_reload(
    tmp_path, monkeypatch
):
    source = os.environ.get("SAC_HERMES_SOURCE_DIR")
    if not source:
        pytest.skip("Set SAC_HERMES_SOURCE_DIR to validate the real Hermes resolver")
    monkeypatch.syspath_prepend(source)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_RUNTIME_DIR", str(tmp_path / "runtime"))
    from agent import credential_pool as cp

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
    pools = {"custom:sac-diagnostic": DeclaredPool(rows, [])}
    materialize_pools(tmp_path, pools)
    pool = cp.load_pool("custom:sac-diagnostic")
    first = pool.select()
    replacement = pool.mark_exhausted_and_rotate(
        status_code=401,
        credential_id=first.id,
        error_context={"message": "Invalid 'Authorization' header or token."},
        failure_reason="auth",
    )
    assert replacement is not None and replacement.id != first.id
    assert (
        next(row for row in pool.entries() if row.id == first.id).last_status == "dead"
    )
    # A month later, reload and rematerialization still retain the rejected
    # account for audit while leasing only its healthy sibling.
    monkeypatch.setattr(cp.time, "time", lambda: time.time_ns() / 1e9 + 31 * 86400)
    materialize_pools(tmp_path, pools)
    reloaded = cp.load_pool("custom:sac-diagnostic")
    assert len(reloaded.entries()) == 2
    assert reloaded.select().id == replacement.id
    pools["custom:sac-diagnostic"].credentials[0] = {
        **rows[0],
        "id": "sac-replacement",
        "access_token": "fake-new-key",
    }
    materialize_pools(tmp_path, pools)
    assert cp.load_pool("custom:sac-diagnostic").select().id == "sac-replacement"
