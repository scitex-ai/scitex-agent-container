"""Exercise SAC's generated profiles with a real, explicitly selected Hermes checkout.

No mocks: the Hermes resolver, the credential-pool store, and dotenv
loading all run for real against ``tmp_path`` profiles. ``$HOME`` is
swapped through the ``env_save_restore`` fixture (which is what
``Path.home`` reads on POSIX) instead of patching ``Path.home``, and the
explicit ``dotenv_path`` production passes makes ``load_dotenv`` a natural
no-op against the empty fake home — no ``load_dotenv`` stub required.
``sys.path`` gains the selected checkout only for the duration of each
scenario and is restored afterwards.
"""

from __future__ import annotations

import json
import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path

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

_FAKE_KEYS = {
    "OPENCODE_GO_API_KEY": "fake-undeclared-base",
    "OPENCODE_GO_API_KEY_1": "fake-go-a",
    "OPENCODE_GO_API_KEY_2": "fake-go-b",
    "COMMANDCODE_API_KEY": "fake-undeclared-command",
    "COMMANDCODE_API_KEY_01": "fake-command-a",
    "COMMANDCODE_API_KEY_02": "fake-command-b",
}

_PROVIDERS = ("opencode-go", "custom:sac-command")

# NOTE — each label gets a FRESH profile directory on purpose. Reusing one
# (e.g. A twice) re-materializes over drained pool state, which
# `materialize_pools` deliberately retains (see the rejected-key tests
# below): `select()` then yields nothing and the resolver falls back to
# singleton base keys — a different, already-covered behavior. Rotation is
# only exercisable on fresh profiles.
_LABELS = ("A", "B", "C")


@contextmanager
def _real_hermes_source():
    """Yield the explicitly selected Hermes checkout on ``sys.path``.

    The tree under test is named by ``SAC_HERMES_SOURCE_DIR``, never
    guessed: resolving any other tree would grade code this branch did
    not ship. ``sys.path`` is restored on exit.
    """
    source = os.environ.get("SAC_HERMES_SOURCE_DIR") or ""
    if not source:
        pytest.skip("Set SAC_HERMES_SOURCE_DIR to validate the real Hermes resolver")
    sys.path.insert(0, source)
    try:
        yield Path(source)
    finally:
        try:
            sys.path.remove(source)
        except ValueError:
            pass


@contextmanager
def _a_month_later():
    """Advance ``time.time`` by 31 days; restore the real clock on exit."""
    real_time = time.time
    time.time = lambda: real_time() + 31 * 86400
    try:
        yield
    finally:
        time.time = real_time


def _arrange_two_engine_case(tmp_path: Path, env) -> dict:
    """Build the go+command failover config against fake keys (real code)."""
    for name, value in _FAKE_KEYS.items():
        env.set(name, value)
    env.set("HOME", str(tmp_path))
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
    failover_env, pools = configure_failover(config, rendered)
    return {"root": tmp_path, "rendered": rendered, "env": failover_env, "pools": pools}


def _act_rotate_all_profiles(case: dict, env) -> dict:
    """Materialize profiles A/B/A and rotate each pool twice; record outcomes.

    Returns plain data only — every assertion lives in the calling test,
    one per test (STX-TQ007).
    """
    from hermes_cli.runtime_provider import AuthError, resolve_runtime_provider
    from agent.credential_pool import load_pool
    from hermes_cli.auth import primary_failure_wording

    pool_rows: list[dict] = []
    auth_sizes: list[tuple[str, int]] = []
    for label in _LABELS:
        profile = case["root"] / label
        profile.mkdir(exist_ok=True)
        # Counts and labels, rather than token comparisons, keep failures
        # safe even if an unexpected ambient secret entered the resolver.
        env.set("HERMES_HOME", str(profile))
        env.set("HERMES_RUNTIME_DIR", str(profile / "runtime"))
        (profile / "config.yaml").write_text(yaml.safe_dump(case["rendered"]))
        _hermes_profile._write_profile_env(profile / ".env", case["env"])
        materialize_pools(profile, case["pools"])
        for provider in _PROVIDERS:
            pool = load_pool(provider)
            row: dict = {
                "label": label,
                "provider": provider,
                "pool_size": len(pool.entries()),
            }
            selected = pool.select()
            row["selected_none"] = selected is None
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
            row["first_label"] = first.label
            row["next_label"] = next_key.label if next_key is not None else None
            drained = runtime["credential_pool"].mark_exhausted_and_rotate(
                status_code=429,
                api_key_hint=next_key.runtime_api_key,
                error_context={"reset_at": time.time() + 16 * 86400},
            )
            row["drained_none"] = drained is None
            try:
                resolved = resolve_runtime_provider(requested=provider)
                row["exhaustion"] = "no-raise"
                # A drained pool is bypassed, not fatal: the resolver falls
                # back to the singleton base key (no `credential_pool` in
                # the runtime). Record which key served the call.
                row["singleton_fallback"] = "credential_pool" not in resolved
                row["fallback_key"] = resolved.get("api_key")
            except AuthError as exc:
                row["exhaustion"] = str(exc)
                row["wording"] = primary_failure_wording(exc)[0]
                row["retryable"] = exc.retryable
            pool_rows.append(row)
        auth_sizes.append(
            (
                label,
                len(
                    json.loads((profile / "auth.json").read_text())["credential_pool"][
                        "opencode-go"
                    ]
                ),
            )
        )
    return {"pool_rows": pool_rows, "auth_sizes": auth_sizes}


def test_generated_profiles_list_only_the_two_declared_accounts(
    tmp_path, env_save_restore
):
    # Arrange
    with _real_hermes_source():
        case = _arrange_two_engine_case(tmp_path, env_save_restore)
    # Act
    with _real_hermes_source():
        outcomes = _act_rotate_all_profiles(case, env_save_restore)
    # Assert
    assert [
        (row["label"], row["provider"], row["pool_size"])
        for row in outcomes["pool_rows"]
    ] == [
        (label, provider, 2) for label in _LABELS for provider in _PROVIDERS
    ]


def test_fresh_profiles_always_offer_a_selectable_credential(
    tmp_path, env_save_restore
):
    # Arrange — a freshly materialized pool holds declared keys, so the
    # scenario helper's rotate path is always taken; pin that assumption.
    with _real_hermes_source():
        case = _arrange_two_engine_case(tmp_path, env_save_restore)
    # Act
    with _real_hermes_source():
        outcomes = _act_rotate_all_profiles(case, env_save_restore)
    # Assert
    assert [
        (row["label"], row["provider"], row["selected_none"])
        for row in outcomes["pool_rows"]
    ] == [(label, provider, False) for label in _LABELS for provider in _PROVIDERS]


def test_quota_hit_rotates_to_the_sibling_key(tmp_path, env_save_restore):
    # Arrange
    with _real_hermes_source():
        case = _arrange_two_engine_case(tmp_path, env_save_restore)
    # Act
    with _real_hermes_source():
        outcomes = _act_rotate_all_profiles(case, env_save_restore)
    # Assert
    assert [
        (row["label"], row["provider"], row["first_label"], row["next_label"])
        for row in outcomes["pool_rows"]
    ] == [
        (label, provider, first, second)
        for label in _LABELS
        for provider, first, second in (
            ("opencode-go", "OPENCODE_GO_API_KEY_1", "OPENCODE_GO_API_KEY_2"),
            (
                "custom:sac-command",
                "COMMANDCODE_API_KEY_01",
                "COMMANDCODE_API_KEY_02",
            ),
        )
    ]


def test_second_quota_hit_leaves_no_further_key(tmp_path, env_save_restore):
    # Arrange
    with _real_hermes_source():
        case = _arrange_two_engine_case(tmp_path, env_save_restore)
    # Act
    with _real_hermes_source():
        outcomes = _act_rotate_all_profiles(case, env_save_restore)
    # Assert
    assert [
        (row["label"], row["provider"], row["drained_none"])
        for row in outcomes["pool_rows"]
    ] == [(label, provider, True) for label in _LABELS for provider in _PROVIDERS]


def test_fully_drained_pool_falls_back_to_singleton_base_key(
    tmp_path, env_save_restore
):
    # Arrange
    with _real_hermes_source():
        case = _arrange_two_engine_case(tmp_path, env_save_restore)
    # Act
    with _real_hermes_source():
        outcomes = _act_rotate_all_profiles(case, env_save_restore)
    # Assert
    assert [
        (
            row["label"],
            row["provider"],
            row["singleton_fallback"],
            row["fallback_key"],
        )
        for row in outcomes["pool_rows"]
    ] == [
        (label, provider, True, base)
        for label in _LABELS
        for provider, base in (
            ("opencode-go", "fake-undeclared-base"),
            ("custom:sac-command", "fake-undeclared-command"),
        )
    ]


def test_materialized_auth_json_keeps_both_declared_opencode_go_keys(
    tmp_path, env_save_restore
):
    # Arrange
    with _real_hermes_source():
        case = _arrange_two_engine_case(tmp_path, env_save_restore)
    # Act
    with _real_hermes_source():
        outcomes = _act_rotate_all_profiles(case, env_save_restore)
    # Assert
    assert outcomes["auth_sizes"] == [(label, 2) for label in _LABELS]


def _arrange_rejected_key_case(tmp_path, env) -> dict:
    """Materialize one diagnostic pool and kill its first key with a 401."""
    from agent import credential_pool as pool_module
    from scitex_agent_container.runtimes._hermes_failover import DeclaredPool

    env.set("HOME", str(tmp_path))
    env.set("HERMES_HOME", str(tmp_path))
    env.set("HERMES_RUNTIME_DIR", str(tmp_path / "runtime"))
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
    pool = pool_module.load_pool("custom:sac-diagnostic")
    first = pool.select()
    # A reason-less 401 is a possible glitch (transient "exhausted"); only a
    # terminal OAuth reason benches the key as "dead" — send the real
    # upstream-permanent verdict so this exercises the dead path it names.
    replacement = pool.mark_exhausted_and_rotate(
        status_code=401,
        credential_id=first.id,
        error_context={
            "message": "Invalid 'Authorization' header or token.",
            "reason": "invalid_token",
        },
        failure_reason="auth",
    )
    dead_status = next(
        row for row in pool.entries() if row.id == first.id
    ).last_status
    return {
        "root": tmp_path,
        "rows": rows,
        "pools": pools,
        "first_id": first.id,
        "replacement_id": replacement.id,
        "dead_status": dead_status,
    }


def test_rejected_static_key_is_marked_dead_while_sibling_survives(
    tmp_path, env_save_restore
):
    # Arrange
    with _real_hermes_source():
        case = _arrange_rejected_key_case(tmp_path, env_save_restore)
    # Act
    survived = (case["replacement_id"], case["dead_status"])
    # Assert
    assert survived == ("sac-1", "dead")


def test_rejected_key_stays_unleased_after_a_month_and_rematerialization(
    tmp_path, env_save_restore
):
    # Arrange
    with _real_hermes_source():
        from agent import credential_pool as pool_module

        case = _arrange_rejected_key_case(tmp_path, env_save_restore)
    # Act
    with _real_hermes_source():
        # A month later, reload and rematerialization still retain the
        # rejected account for audit while leasing only its healthy sibling.
        with _a_month_later():
            materialize_pools(case["root"], case["pools"])
        reloaded = pool_module.load_pool("custom:sac-diagnostic")
        leased = (len(reloaded.entries()), reloaded.select().id)
    # Assert
    assert leased == (2, "sac-1")


def test_declared_replacement_key_resumes_leasing(tmp_path, env_save_restore):
    # Arrange
    with _real_hermes_source():
        from agent import credential_pool as pool_module

        case = _arrange_rejected_key_case(tmp_path, env_save_restore)
    # Act
    with _real_hermes_source():
        case["pools"]["custom:sac-diagnostic"].credentials[0] = {
            **case["rows"][0],
            "id": "sac-replacement",
            "access_token": "fake-new-key",
        }
        materialize_pools(case["root"], case["pools"])
        resumed = pool_module.load_pool("custom:sac-diagnostic").select().id
    # Assert
    assert resumed == "sac-replacement"
