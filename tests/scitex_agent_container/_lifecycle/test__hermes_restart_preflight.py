"""Hermes successor auth is proved before any process or profile is replaced."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from types import SimpleNamespace

import pytest

from scitex_agent_container._lifecycle import _hermes_restart_preflight as gate
from scitex_agent_container._lifecycle._restart_preflight import RestartPreflightAbort
from scitex_agent_container.config import AgentConfig
from scitex_agent_container.config._engine_types import EngineSpec
from scitex_agent_container.config._hermes_config import compile_hermes_config
from scitex_agent_container.config._hermes_failover import HermesFailoverSpec
from scitex_agent_container.config._hermes_goals import HermesGoalSpec
from scitex_agent_container.config._provider_types import ProviderSpec
from scitex_agent_container.runtimes import _hermes_profile as profile
from scitex_agent_container.runtimes._apptainer_provider import ProviderEnvError

MODEL = "muse-spark-1.3-contributor"
TOKENS = {"SAC_TEST_A": "synthetic-primary-a", "SAC_TEST_B": "synthetic-primary-b"}


@pytest.fixture(autouse=True)
def isolated_health(tmp_path, monkeypatch):
    from scitex_agent_container.runtimes import tui_session

    gate.reset_probe_cache()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(
        tui_session, "state_dir_for_config", lambda c: tmp_path / c.name
    )
    yield
    gate.reset_probe_cache()


def config(name="alpha", *, fallback_model=None, judge=False):
    value = AgentConfig(name=name, harness="hermes", runtime="tui")
    value.workdir = "/work"
    value.engine_key = "muse"
    value.model = MODEL
    value.claude.session = "continue"
    value.claude.provider = ProviderSpec(
        base_url="https://opencode.ai/zen/go/v1/responses",
        auth_token_env="SAC_TEST_A",
    )
    value.hermes_failover = HermesFailoverSpec(accounts={"muse": list(TOKENS)})
    if fallback_model:
        value.engines["backup"] = EngineSpec(
            key="backup",
            model=fallback_model,
            provider=ProviderSpec(
                base_url="https://api.commandcode.ai/provider/v1",
                auth_token_env="SAC_TEST_C",
            ),
        )
        value.hermes_failover.engines = ["backup"]
        value.hermes_failover.accounts["backup"] = ["SAC_TEST_C"]
    if judge:
        value.hermes_goals = HermesGoalSpec(max_turns=99999, judge_engine="muse")
    return value


def resolve(tokens):
    def read(value):
        slot = value.claude.provider.auth_token_env
        if slot not in tokens:
            raise ProviderEnvError(f"Unresolved credential slot {slot}")
        return tokens[slot]

    return read


def probe_result(code, now=1000):
    if code == 200:
        return {"last_status": "ok", "last_status_at": now}
    return {
        "last_status": "dead" if code == 401 else "exhausted",
        "last_status_at": now,
        "last_error_code": code,
        "last_error_reset_at": now + 3600,
    }


def prepare(value, *, codes=None, tokens=None, calls=None, **kwargs):
    calls = [] if calls is None else calls
    codes = {} if codes is None else codes

    def native(spec, token):
        calls.append((spec["provider"], token))
        return probe_result(
            401 if token == gate._CONTROL_TOKEN else codes.get(token, 200)
        )

    return gate.prepare_hermes_successor(
        value,
        probe=native,
        resolver=resolve(TOKENS if tokens is None else tokens),
        pool_reader=lambda: SimpleNamespace(env={}),
        now=lambda: 1000,
        **kwargs,
    )


def test_nonhermes_successor_keeps_existing_guard():
    assert gate.prepare_hermes_successor(AgentConfig(name="native")) is None


def test_healthy_sibling_is_selected_without_touching_auth_file(tmp_path):
    value = config()
    before = deepcopy(value)
    auth = tmp_path / "alpha/home/.hermes/auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text('{"version": 1, "credential_pool": {}}')
    original = auth.read_bytes()
    proof = prepare(value, codes={TOKENS["SAC_TEST_A"]: 429})
    rows = next(iter(proof.pools.values())).credentials
    assert (
        proof.provider_key,
        [r["last_status"] for r in rows],
        auth.read_bytes(),
        value,
    ) == (
        TOKENS["SAC_TEST_B"],
        ["exhausted", "ok"],
        original,
        before,
    )


def test_missing_slot_is_skipped_and_canonical_pool_can_supply_daemon_key(monkeypatch):
    value = config()
    value.hermes_failover.accounts["muse"] = ["SAC_TEST_MISSING", "SAC_TEST_B"]
    monkeypatch.delenv("SAC_TEST_B", raising=False)
    proof = gate.prepare_hermes_successor(
        value,
        resolver=resolve({}),
        pool_reader=lambda: SimpleNamespace(env={"SAC_TEST_B": TOKENS["SAC_TEST_B"]}),
        probe=lambda _spec, token: probe_result(
            401 if token == gate._CONTROL_TOKEN else 200
        ),
        now=lambda: 1000,
    )
    assert (proof.provider_key, list(proof.env)) == (
        TOKENS["SAC_TEST_B"],
        ["SAC_TEST_B", "SAC_TEST_A"],
    )


@pytest.mark.parametrize(
    "tokens,codes",
    [
        ({}, {}),
        (TOKENS, {t: 401 for t in TOKENS.values()}),
        (TOKENS, {t: None for t in TOKENS.values()}),
    ],
)
def test_all_missing_rejected_or_unknown_refuses_without_creating_home(
    tmp_path, tokens, codes
):
    with pytest.raises(RestartPreflightAbort, match="No declared Hermes credential"):
        prepare(config(), tokens=tokens, codes=codes)
    assert not (tmp_path / "alpha").exists()


def test_public_get_style_acceptance_of_bad_key_is_not_auth_proof():
    with pytest.raises(RestartPreflightAbort, match="invalid credential control"):
        gate.prepare_hermes_successor(
            config(),
            resolver=resolve(TOKENS),
            pool_reader=lambda: SimpleNamespace(env={}),
            probe=lambda *_args: probe_result(200),
        )


def test_probe_exception_never_echoes_a_key():
    sentinel = TOKENS["SAC_TEST_A"]

    def broken(_spec, _token):
        raise ValueError("upstream echoed authorization " + sentinel)

    with pytest.raises(RestartPreflightAbort) as error:
        gate.prepare_hermes_successor(config(), resolver=resolve(TOKENS), probe=broken)
    assert sentinel not in str(error.value)


def test_dead_and_future_quota_cooldown_are_preserved_without_reprobe(tmp_path):
    value = config()
    initial = prepare(value)
    pool = next(iter(initial.pools.values()))
    pool.credentials[0].update(probe_result(401))
    pool.credentials[1].update(probe_result(429))
    auth = tmp_path / "alpha/home/.hermes/auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text(
        json.dumps(
            {
                "credential_pool": {
                    initial.selection["model"]["provider"]: pool.credentials
                }
            }
        )
    )
    gate.reset_probe_cache()
    calls = []
    with pytest.raises(RestartPreflightAbort):
        prepare(value, calls=calls)
    assert [token for _, token in calls] == [gate._CONTROL_TOKEN]


def test_expired_quota_cooldown_is_rechecked(tmp_path):
    value = config()
    initial = prepare(value)
    provider = initial.selection["model"]["provider"]
    rows = initial.pools[provider].credentials
    for row in rows:
        row.update(probe_result(429))
        row["last_error_reset_at"] = 900
    auth = tmp_path / "alpha/home/.hermes/auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text(json.dumps({"credential_pool": {provider: rows}}))
    gate.reset_probe_cache()
    calls = []
    proof = prepare(value, calls=calls)
    assert (len(calls), proof.provider_key) == (3, TOKENS["SAC_TEST_A"])


def test_changed_token_cannot_inherit_dead_metadata_under_stale_same_id(tmp_path):
    value = config()
    initial = prepare(value)
    provider = initial.selection["model"]["provider"]
    rows = initial.pools[provider].credentials
    for row in rows:
        row["access_token"] = "retired-synthetic-token"
        row.update(probe_result(401))
    auth = tmp_path / "alpha/home/.hermes/auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text(json.dumps({"credential_pool": {provider: rows}}))
    gate.reset_probe_cache()
    calls = []
    proof = prepare(value, calls=calls)
    assert (len(calls), proof.provider_key) == (3, TOKENS["SAC_TEST_A"])


def test_healthy_primary_survives_unreachable_backup_control():
    value = config(fallback_model="meta/" + MODEL, judge=True)

    def native(spec, token):
        if spec["provider"] == "custom:sac-backup":
            raise TimeoutError("synthetic-upstream-secret-echo")
        return probe_result(401 if token == gate._CONTROL_TOKEN else 200)

    proof = gate.prepare_hermes_successor(
        value,
        resolver=resolve({**TOKENS, "SAC_TEST_C": "synthetic-backup"}),
        probe=native,
        now=lambda: 1000,
    )
    assert (
        proof.selection["model"]["provider"],
        proof.selection["fallback_providers"],
        proof.pools["custom:sac-backup"].credentials[0]["last_status"],
    ) == (
        "custom:sac-muse",
        [],
        "exhausted",
    )


def test_auth_discriminating_backup_with_failed_real_probe_does_not_ground_primary():
    value = config(fallback_model="meta/" + MODEL, judge=True)

    def native(spec, token):
        if spec["provider"] == "custom:sac-backup" and token != gate._CONTROL_TOKEN:
            raise TimeoutError("synthetic-upstream-secret-echo")
        return probe_result(401 if token == gate._CONTROL_TOKEN else 200)

    proof = gate.prepare_hermes_successor(
        value,
        resolver=resolve({**TOKENS, "SAC_TEST_C": "synthetic-backup"}),
        probe=native,
    )
    assert (
        proof.selection["model"]["provider"],
        proof.selection["fallback_providers"],
    ) == ("custom:sac-muse", [])


def test_missing_primary_and_unverified_backup_control_refuses_without_writes(tmp_path):
    value = config(fallback_model="meta/" + MODEL, judge=True)
    with pytest.raises(RestartPreflightAbort, match="No declared Hermes credential"):
        gate.prepare_hermes_successor(
            value,
            resolver=resolve({"SAC_TEST_C": "synthetic-backup"}),
            pool_reader=lambda: SimpleNamespace(env={}),
            probe=lambda *_args: probe_result(200),
        )
    assert not (tmp_path / "alpha").exists()


def test_bulk_concurrent_agents_share_native_health_for_identical_vendor_keys():
    calls = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(
                lambda name: prepare(config(name), calls=calls),
                ["alpha", "beta", "gamma", "delta"],
            )
        )
    assert (len(calls), len(results)) == (3, 4)


def test_probe_cache_expires_and_drops_old_key_material():
    stamp = [1000.0]
    calls = []
    spec = {
        "url": "https://opencode.ai/zen/go/v1/responses",
        "protocol": "openai-responses",
        "model": MODEL,
        "headers": {},
    }

    def native(_spec, token):
        calls.append(token)
        return probe_result(200)

    gate._cached_probe(spec, "retired-synthetic", native, lambda: stamp[0])
    stamp[0] += 89
    gate._cached_probe(spec, "retired-synthetic", native, lambda: stamp[0])
    stamp[0] += 2
    gate._cached_probe(spec, "new-synthetic", native, lambda: stamp[0])
    assert (calls, any("retired-synthetic" in key for key in gate._CACHE)) == (
        ["retired-synthetic", "new-synthetic"],
        False,
    )


def test_arbitrary_gateway_identity_headers_are_not_cache_equivalent():
    calls = []
    spec = {
        "url": "https://private.example/v1/responses",
        "protocol": "openai-responses",
        "model": MODEL,
        "headers": {"X-SciTeX-Agent-ID": "alpha"},
    }

    def native(_spec, token):
        calls.append(token)
        return probe_result(200)

    gate._cached_probe(spec, "synthetic", native, lambda: 1000)
    gate._cached_probe(
        {**spec, "headers": {"X-SciTeX-Agent-ID": "beta"}},
        "synthetic",
        native,
        lambda: 1000,
    )
    assert len(calls) == 2


def test_missing_primary_can_use_verified_same_muse_backup_with_same_judge():
    value = config(fallback_model="meta/" + MODEL, judge=True)
    proof = prepare(value, tokens={"SAC_TEST_C": "synthetic-backup"})
    assert (
        proof.selection["model"]["provider"],
        proof.selection["model"]["default"],
        proof.provider_key,
        proof.selection["fallback_providers"],
    ) == (
        "custom:sac-backup",
        "meta/" + MODEL,
        "synthetic-backup",
        [],
    )


def test_unrelated_model_backup_refuses_authored_judge_intent():
    value = config(fallback_model="deepseek-chat", judge=True)
    with pytest.raises(RestartPreflightAbort, match="authored goal judge model"):
        prepare(value, tokens={"SAC_TEST_C": "synthetic-backup"})


def test_unknown_fallback_is_not_skipped_as_an_unresolved_account():
    value = config()
    value.hermes_failover.engines = ["not-declared"]
    with pytest.raises(ValueError, match="not available"):
        prepare(value)


def test_prepared_route_cannot_be_rebound_to_different_pool_policy():
    value = config()
    proof = prepare(value)
    value.hermes_failover.accounts["muse"].reverse()
    with pytest.raises(RestartPreflightAbort, match="configuration changed"):
        gate.attach_prepared_route(value, proof)


def test_spec_edit_between_probe_and_stop_refuses(tmp_path):
    value = config()
    path = tmp_path / "spec.yaml"
    path.write_text("first")
    value.config_path = str(path)
    proof = prepare(value)
    path.write_text("second-version")
    with pytest.raises(RestartPreflightAbort, match="spec changed"):
        gate.assert_prepared_source_current(proof)


def test_handoff_consumes_exact_proof_once_without_any_new_probe_or_resolver(
    tmp_path, monkeypatch
):
    value = config(judge=True)
    calls = []
    proof = prepare(value, calls=calls)
    gate.attach_prepared_route(value, proof)
    value.workdir = "/work/.worktrees/owned-topic"

    def forbidden(*_args, **_kwargs):
        pytest.fail("prepared route must not re-probe or resolve a different key")

    monkeypatch.setattr(profile, "preflight_pools", forbidden)
    monkeypatch.setattr(profile, "resolve_provider_api_key", forbidden)
    plan, selection, env, pools = profile._verified_route(
        value, tmp_path / "home", launch_mode="tui"
    )
    key = profile._profile_primary_key(value)
    rendered = compile_hermes_config(
        plan, workdir=value.workdir, goals=value.hermes_goals
    )
    profile._apply_verified_route(rendered, selection)
    assert (
        plan is proof.plan,
        env is proof.env,
        pools is proof.pools,
        key,
        len(calls),
        getattr(value, "_hermes_prepared_route", None),
        rendered["providers"]["sac-muse"]["key_env"],
        rendered["auxiliary"]["goal_judge"],
        all(token not in repr(proof) for token in TOKENS.values()),
    ) == (
        True,
        True,
        True,
        TOKENS["SAC_TEST_A"],
        3,
        None,
        "",
        {"provider": "main", "model": MODEL},
        True,
    )


def test_same_muse_fallback_updates_goal_judge_wire_name():
    value = config(fallback_model="meta/" + MODEL, judge=True)
    proof = prepare(value, tokens={"SAC_TEST_C": "synthetic-backup"})
    rendered = compile_hermes_config(
        proof.plan, workdir=value.workdir, goals=value.hermes_goals
    )
    profile._apply_verified_route(rendered, proof.selection)
    assert rendered["auxiliary"]["goal_judge"] == {
        "provider": "main",
        "model": "meta/" + MODEL,
    }
