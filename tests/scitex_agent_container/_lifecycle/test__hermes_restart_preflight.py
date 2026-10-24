"""Skip-dead Hermes failover: preflight skips dead/exhausted slots.

:mod:`scitex_agent_container._lifecycle._hermes_restart_preflight`
proves one declared route before a restart replaces its live process.
Slots already known ``dead`` — or ``exhausted`` with an unexpired
cooldown — are NOT re-probed; the successor starts on a live slot and
the preflight aborts (leaving the current process running) only when
zero slots prove inference access.

No network: a stub ``probe`` answers for each token, a stub resolver
maps declared slot names to synthetic tokens, and previous pool state
is pre-seeded under an isolated tmp profile. Token values are obvious
test sentinels, never secret-shaped.
"""

from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from scitex_agent_container._lifecycle import _hermes_restart_preflight as gate
from scitex_agent_container._lifecycle._hermes_restart_preflight import (
    prepare_hermes_successor,
    reset_probe_cache,
    same_goal_model,
)
from scitex_agent_container._lifecycle._restart_preflight import (
    RestartPreflightAbort,
)
from scitex_agent_container.config import AgentConfig
from scitex_agent_container.config._engine_types import EngineSpec
from scitex_agent_container.config._hermes_config import compile_hermes_config
from scitex_agent_container.config._hermes_failover import HermesFailoverSpec
from scitex_agent_container.config._hermes_goals import HermesGoalSpec
from scitex_agent_container.config._provider_types import ProviderSpec
from scitex_agent_container.runtimes import _hermes_profile as profile
from scitex_agent_container.runtimes._apptainer_provider import ProviderEnvError

_CONTROL = "sac-preflight-control-not-a-valid-key"
_NOW = 1_700_000_000.0

_LIVE = "synthetic-live-token"
_DEAD = "synthetic-dead-token"
_OTHER = "synthetic-other-token"


@pytest.fixture(autouse=True)
def _fresh_probe_cache():
    reset_probe_cache()
    yield
    reset_probe_cache()


def _config(slot_names: list[str]) -> AgentConfig:
    config = AgentConfig(name="future", harness="hermes", runtime="tui")
    config.workdir = "/work"
    config.engine_key = "muse"
    config.model = "muse-spark-1.3-contributor"
    config.claude.provider = ProviderSpec(
        hermes_provider="opencode-go", auth_token_env="OPENCODE_GO_API_KEY"
    )
    config.hermes_failover = HermesFailoverSpec(
        accounts={"muse": slot_names},
        strategy="round_robin",
    )
    return config


def _resolver(tokens: dict[str, str]):
    def resolve(route) -> str:
        name = route.claude.provider.auth_token_env
        if name not in tokens:
            raise ProviderEnvError(f"no credential for slot {name}")
        return tokens[name]

    return resolve


def _probe(live: set[str], calls: list[str]):
    def probe(spec: dict, token: str) -> dict:
        calls.append(token)
        if token == _CONTROL:
            return {
                "last_status": "exhausted",
                "last_status_at": _NOW,
                "last_error_code": 401,
                "last_error_reason": "http:401",
                "last_error_reset_at": _NOW + 60,
                "failure_reason": "auth",
            }
        if token in live:
            return {"last_status": "ok", "last_status_at": _NOW}
        return {
            "last_status": "dead",
            "last_status_at": _NOW,
            "last_error_code": 401,
            "last_error_reason": "http:401",
            "last_error_reset_at": _NOW + 60,
            "failure_reason": "auth",
        }

    return probe


def _declared_id(provider: str, token: str) -> str:
    return "sac-" + hashlib.sha256((provider + "\0" + token).encode()).hexdigest()[:20]


def _seed_previous(profile: Path, provider: str, rows: list[dict]) -> None:
    profile.mkdir(parents=True, exist_ok=True)
    (profile / "auth.json").write_text(json.dumps({"credential_pool": {provider: rows}}))


def _prepare(config, tokens, live, tmp_path, previous_rows=(), now=_NOW):
    calls: list[str] = []
    profile = tmp_path / ".hermes"
    _seed_previous(profile, "opencode-go", list(previous_rows))
    return (
        prepare_hermes_successor(
            config,
            probe=_probe(set(live), calls),
            resolver=_resolver(tokens),
            pool_reader=lambda: SimpleNamespace(env={}),
            profiles=[profile],
            now=lambda: now,
        ),
        calls,
    )


def test_preflight_starts_on_the_live_slot(tmp_path):
    # Arrange
    config = _config(["GO_DEAD", "GO_LIVE"])
    tokens = {"GO_DEAD": _DEAD, "GO_LIVE": _LIVE}

    # Act
    prepared, _ = _prepare(config, tokens, live={_LIVE}, tmp_path=tmp_path)

    # Assert
    assert prepared.provider_key == _LIVE


def test_preflight_does_not_reprobe_a_known_dead_slot(tmp_path):
    # Arrange
    config = _config(["GO_DEAD", "GO_LIVE"])
    tokens = {"GO_DEAD": _DEAD, "GO_LIVE": _LIVE}
    dead_row = {
        "id": _declared_id("opencode-go", _DEAD),
        "access_token": _DEAD,
        "last_status": "dead",
        "last_status_at": _NOW - 10,
    }

    # Act
    prepared, calls = _prepare(config, tokens, live={_LIVE}, tmp_path=tmp_path, previous_rows=[dead_row])

    # Assert
    assert (_DEAD not in calls, prepared.provider_key) == (True, _LIVE)


def test_preflight_does_not_reprobe_an_unexpired_exhausted_slot(tmp_path):
    # Arrange
    config = _config(["GO_SPENT", "GO_LIVE"])
    tokens = {"GO_SPENT": _OTHER, "GO_LIVE": _LIVE}
    spent_row = {
        "id": _declared_id("opencode-go", _OTHER),
        "access_token": _OTHER,
        "last_status": "exhausted",
        "last_status_at": _NOW - 10,
        "last_error_reset_at": _NOW + 3500,
    }

    # Act
    prepared, calls = _prepare(config, tokens, live={_LIVE}, tmp_path=tmp_path, previous_rows=[spent_row])

    # Assert
    assert (_OTHER not in calls, prepared.provider_key) == (True, _LIVE)


def test_preflight_reprobes_an_exhausted_slot_past_its_cooldown(tmp_path):
    # Arrange
    config = _config(["GO_SPENT", "GO_LIVE"])
    tokens = {"GO_SPENT": _OTHER, "GO_LIVE": _LIVE}
    spent_row = {
        "id": _declared_id("opencode-go", _OTHER),
        "access_token": _OTHER,
        "last_status": "exhausted",
        "last_status_at": _NOW - 7200,
        "last_error_reset_at": _NOW - 3600,
    }

    # Act
    _, calls = _prepare(config, tokens, live={_LIVE, _OTHER}, tmp_path=tmp_path, previous_rows=[spent_row])

    # Assert
    assert _OTHER in calls


def test_preflight_aborts_only_when_no_slot_is_live(tmp_path):
    # Arrange
    config = _config(["GO_DEAD", "GO_SPENT"])
    tokens = {"GO_DEAD": _DEAD, "GO_SPENT": _OTHER}

    # Act
    # Assert
    with pytest.raises(RestartPreflightAbort):
        _prepare(config, tokens, live=set(), tmp_path=tmp_path)


def test_preflight_abort_marks_cooldown_gated_routes_retryable(tmp_path):
    # Arrange
    config = _config(["GO_DEAD", "GO_SPENT"])
    tokens = {"GO_DEAD": _DEAD, "GO_SPENT": _OTHER}
    spent_row = {
        "id": _declared_id("opencode-go", _OTHER),
        "access_token": _OTHER,
        "last_status": "exhausted",
        "last_status_at": _NOW,
        "last_error_reset_at": _NOW + 3600,
    }

    # Act
    try:
        _prepare(config, tokens, live=set(), tmp_path=tmp_path, previous_rows=[spent_row])
        abort = None
    except RestartPreflightAbort as exc:
        abort = exc

    # Assert
    assert abort is not None and abort.retryable is True


def test_preflight_abort_marks_all_dead_routes_not_retryable(tmp_path):
    # Arrange
    config = _config(["GO_DEAD"])
    tokens = {"GO_DEAD": _DEAD}

    # Act
    try:
        _prepare(config, tokens, live=set(), tmp_path=tmp_path)
        abort = None
    except RestartPreflightAbort as exc:
        abort = exc

    # Assert
    assert abort is not None and abort.retryable is False


def test_preflight_skips_a_slot_without_an_installed_secret(tmp_path):
    # Arrange
    config = _config(["GO_MISSING", "GO_LIVE"])
    tokens = {"GO_LIVE": _LIVE}

    # Act
    prepared, _ = _prepare(config, tokens, live={_LIVE}, tmp_path=tmp_path)

    # Assert
    assert prepared.provider_key == _LIVE


def test_same_goal_model_accepts_identical_names() -> None:
    # Arrange
    name = "meta/muse-spark-1.3-contributor"

    # Act
    verdict = same_goal_model(name, name)

    # Assert
    assert verdict is True


def test_same_goal_model_accepts_wire_and_bare_alias() -> None:
    # Arrange
    pair = ("meta/muse-spark-1.3-contributor", "muse-spark-1.3-contributor")

    # Act
    verdict = same_goal_model(*pair)

    # Assert
    assert verdict is True


def test_same_goal_model_accepts_wire_and_named_custom_alias() -> None:
    # Arrange
    pair = ("meta/muse-spark-1.3-contributor", "meta-muse-spark-1.3-contributor")

    # Act
    verdict = same_goal_model(*pair)

    # Assert
    assert verdict is True


def test_same_goal_model_accepts_bare_and_named_custom_alias() -> None:
    # Arrange
    pair = ("muse-spark-1.3-contributor", "meta-muse-spark-1.3-contributor")

    # Act
    verdict = same_goal_model(*pair)

    # Assert
    assert verdict is True


def test_same_goal_model_refuses_different_models() -> None:
    # Arrange
    pair = ("meta/muse-spark-1.3-contributor", "meta/muse-spark-2.0-contributor")

    # Act
    verdict = same_goal_model(*pair)

    # Assert
    assert verdict is False


def test_same_goal_model_refuses_non_muse_base() -> None:
    # Arrange
    pair = ("meta/other-model-1.0", "other-model-1.0")

    # Act
    verdict = same_goal_model(*pair)

    # Assert
    assert verdict is False


# ---- Merged successor-auth proof tests (feature/remove-force-option) ----
# Union with the skip-dead failover suite above; test names are disjoint.
# (Imports hoisted to the top of this file.)

MODEL = "muse-spark-1.3-contributor"
TOKENS = {"SAC_TEST_A": "synthetic-primary-a", "SAC_TEST_B": "synthetic-primary-b"}


@pytest.fixture(autouse=True)
def isolated_health(tmp_path):
    import os

    from scitex_agent_container.runtimes import tui_session

    gate.reset_probe_cache()
    previous_home = os.environ.get("HOME")
    os.environ["HOME"] = str(tmp_path)
    previous_state_dir = tui_session.state_dir_for_config
    tui_session.state_dir_for_config = lambda c: tmp_path / c.name
    try:
        yield
    finally:
        if previous_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = previous_home
        tui_session.state_dir_for_config = previous_state_dir
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
    # Arrange
    # Act
    # Assert
    assert gate.prepare_hermes_successor(AgentConfig(name="native")) is None


def test_healthy_sibling_is_selected_without_touching_auth_file(tmp_path):
    # Arrange
    # Act
    value = config()
    before = deepcopy(value)
    auth = tmp_path / "alpha/home/.hermes/auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text('{"version": 1, "credential_pool": {}}')
    original = auth.read_bytes()
    proof = prepare(value, codes={TOKENS["SAC_TEST_A"]: 429})
    rows = next(iter(proof.pools.values())).credentials
    # Assert
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


def test_missing_slot_is_skipped_and_canonical_pool_can_supply_daemon_key():
    # Arrange
    import os

    # Act
    value = config()
    value.hermes_failover.accounts["muse"] = ["SAC_TEST_MISSING", "SAC_TEST_B"]
    previous_missing = os.environ.pop("SAC_TEST_B", None)
    try:
        proof = gate.prepare_hermes_successor(
            value,
            resolver=resolve({}),
            pool_reader=lambda: SimpleNamespace(env={"SAC_TEST_B": TOKENS["SAC_TEST_B"]}),
            probe=lambda _spec, token: probe_result(
                401 if token == gate._CONTROL_TOKEN else 200
            ),
            now=lambda: 1000,
        )
    finally:
        if previous_missing is not None:
            os.environ["SAC_TEST_B"] = previous_missing
    # Assert
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
def test_all_missing_rejected_or_unknown_refuses_without_creating_home_checks_pytest_raises_restartpreflightabort_match(tmp_path, tokens, codes):
    # Arrange
    # Act
    # Assert
    with pytest.raises(RestartPreflightAbort, match="No declared Hermes credential"):
        prepare(config(), tokens=tokens, codes=codes)


@pytest.mark.parametrize(
    "tokens,codes",
    [
        ({}, {}),
        (TOKENS, {t: 401 for t in TOKENS.values()}),
        (TOKENS, {t: None for t in TOKENS.values()}),
    ],
)
def test_all_missing_rejected_or_unknown_refuses_without_creating_home_checks_tmp_path_alpha_exists(tmp_path, tokens, codes):
    # Arrange
    # Act
    # Assert
    assert not (tmp_path / "alpha").exists()



def test_public_get_style_acceptance_of_bad_key_is_not_auth_proof():
    # Arrange
    # Act
    # Assert
    with pytest.raises(RestartPreflightAbort, match="invalid credential control"):
        gate.prepare_hermes_successor(
            config(),
            resolver=resolve(TOKENS),
            pool_reader=lambda: SimpleNamespace(env={}),
            probe=lambda *_args: probe_result(200),
        )


def test_probe_exception_refuses_without_echoing_a_key():
    # Arrange
    sentinel = TOKENS["SAC_TEST_A"]

    def broken(_spec, _token):
        raise ValueError("upstream echoed authorization " + sentinel)

    # Act
    try:
        gate.prepare_hermes_successor(config(), resolver=resolve(TOKENS), probe=broken)
    except RestartPreflightAbort as error:
        observed = str(error)
    else:
        observed = None
    # Assert
    assert observed is not None and sentinel not in observed


def test_dead_and_future_quota_cooldown_refuse_without_reprobe(tmp_path):
    # Arrange
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
    # Act
    # Assert
    with pytest.raises(RestartPreflightAbort):
        prepare(value, calls=calls)


def test_dead_and_future_quota_cooldown_probe_only_control_token(tmp_path):
    # Arrange
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
    # Act
    try:
        prepare(value, calls=calls)
    except RestartPreflightAbort:
        refused = True
    else:
        refused = False
    # Assert
    assert (refused, [token for _, token in calls]) == (True, [gate._CONTROL_TOKEN])


def test_expired_quota_cooldown_is_rechecked(tmp_path):
    # Arrange
    # Act
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
    # Assert
    assert (len(calls), proof.provider_key) == (3, TOKENS["SAC_TEST_A"])


def test_changed_token_cannot_inherit_dead_metadata_under_stale_same_id(tmp_path):
    # Arrange
    # Act
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
    # Assert
    assert (len(calls), proof.provider_key) == (3, TOKENS["SAC_TEST_A"])


def test_healthy_primary_survives_unreachable_backup_control():
    # Arrange
    # Act
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
    # Assert
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
    # Arrange
    # Act
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
    # Assert
    assert (
        proof.selection["model"]["provider"],
        proof.selection["fallback_providers"],
    ) == ("custom:sac-muse", [])


def test_missing_primary_and_unverified_backup_control_refuses_without_writes_checks_pytest_raises_restartpreflightabort_match(tmp_path):
    # Arrange
    value = config(fallback_model="meta/" + MODEL, judge=True)
    # Act
    # Assert
    with pytest.raises(RestartPreflightAbort, match="No declared Hermes credential"):
        gate.prepare_hermes_successor(
            value,
            resolver=resolve({"SAC_TEST_C": "synthetic-backup"}),
            pool_reader=lambda: SimpleNamespace(env={}),
            probe=lambda *_args: probe_result(200),
        )


def test_missing_primary_and_unverified_backup_control_refuses_without_writes_checks_tmp_path_alpha_exists(tmp_path):
    # Arrange
    value = config(fallback_model="meta/" + MODEL, judge=True)
    # Act
    # Assert
    assert not (tmp_path / "alpha").exists()



def test_bulk_concurrent_agents_share_native_health_for_identical_vendor_keys():
    # Arrange
    # Act
    calls = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(
                lambda name: prepare(config(name), calls=calls),
                ["alpha", "beta", "gamma", "delta"],
            )
        )
    # Assert
    assert (len(calls), len(results)) == (3, 4)


def test_probe_cache_expires_and_drops_old_key_material():
    # Arrange
    stamp = [1000.0]
    # Act
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
    # Assert
    assert (calls, any("retired-synthetic" in key for key in gate._CACHE)) == (
        ["retired-synthetic", "new-synthetic"],
        False,
    )


def test_arbitrary_gateway_identity_headers_are_not_cache_equivalent():
    # Arrange
    # Act
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
    # Assert
    assert len(calls) == 2


def test_missing_primary_can_use_verified_same_muse_backup_with_same_judge():
    # Arrange
    # Act
    value = config(fallback_model="meta/" + MODEL, judge=True)
    proof = prepare(value, tokens={"SAC_TEST_C": "synthetic-backup"})
    # Assert
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
    # Arrange
    # Act
    value = config(fallback_model="deepseek-chat", judge=True)
    # Assert
    with pytest.raises(RestartPreflightAbort, match="authored goal judge model"):
        prepare(value, tokens={"SAC_TEST_C": "synthetic-backup"})


def test_unknown_fallback_is_not_skipped_as_an_unresolved_account():
    # Arrange
    # Act
    value = config()
    value.hermes_failover.engines = ["not-declared"]
    # Assert
    with pytest.raises(ValueError, match="not available"):
        prepare(value)


def test_prepared_route_cannot_be_rebound_to_different_pool_policy():
    # Arrange
    # Act
    value = config()
    proof = prepare(value)
    value.hermes_failover.accounts["muse"].reverse()
    # Assert
    with pytest.raises(RestartPreflightAbort, match="configuration changed"):
        gate.attach_prepared_route(value, proof)


def test_spec_edit_between_probe_and_stop_refuses(tmp_path):
    # Arrange
    # Act
    value = config()
    path = tmp_path / "spec.yaml"
    path.write_text("first")
    value.config_path = str(path)
    proof = prepare(value)
    path.write_text("second-version")
    # Assert
    with pytest.raises(RestartPreflightAbort, match="spec changed"):
        gate.assert_prepared_source_current(proof)


def test_handoff_consumes_exact_proof_once_without_any_new_probe_or_resolver(
    # Arrange
    tmp_path,
):
    # Act
    value = config(judge=True)
    calls = []
    proof = prepare(value, calls=calls)
    gate.attach_prepared_route(value, proof)
    value.workdir = "/work/.worktrees/owned-topic"

    def forbidden(*_args, **_kwargs):
        forbidden_calls.append((_args, _kwargs))

    forbidden_calls = []
    previous_pools = profile.preflight_pools
    previous_resolve = profile.resolve_provider_api_key
    profile.preflight_pools = forbidden
    profile.resolve_provider_api_key = forbidden
    try:
        plan, selection, env, pools = profile._verified_route(
            value, tmp_path / "home", launch_mode="tui"
        )
    finally:
        profile.preflight_pools = previous_pools
        profile.resolve_provider_api_key = previous_resolve
    key = profile._profile_primary_key(value)
    rendered = compile_hermes_config(
        plan, workdir=value.workdir, goals=value.hermes_goals
    )
    profile._apply_verified_route(rendered, selection)
    # Assert
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
        tuple(forbidden_calls),
    ) == (
        True,
        True,
        True,
        TOKENS["SAC_TEST_A"],
        3,
        None,
        "",
        {
            "provider": "custom:sac-muse",
            "model": "meta-muse-spark-1.3-contributor",
            "reasoning_effort": "xhigh",
            "timeout": 60,
            "max_tokens": 4096,
        },
        True,
        (),
    )


def test_same_muse_fallback_updates_goal_judge_wire_name():
    # Arrange
    # Act
    value = config(fallback_model="meta/" + MODEL, judge=True)
    proof = prepare(value, tokens={"SAC_TEST_C": "synthetic-backup"})
    rendered = compile_hermes_config(
        proof.plan, workdir=value.workdir, goals=value.hermes_goals
    )
    profile._apply_verified_route(rendered, proof.selection)
    # Assert
    assert rendered["auxiliary"]["goal_judge"] == {
        "provider": "custom:sac-muse",
        "model": "meta/" + MODEL,
        "reasoning_effort": "xhigh",
        "timeout": 60,
        "max_tokens": 4096,
    }
