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
from pathlib import Path
from types import SimpleNamespace

import pytest

from scitex_agent_container._lifecycle._hermes_restart_preflight import (
    prepare_hermes_successor,
    reset_probe_cache,
    same_goal_model,
)
from scitex_agent_container._lifecycle._restart_preflight import (
    RestartPreflightAbort,
)
from scitex_agent_container.config import AgentConfig
from scitex_agent_container.config._hermes_failover import HermesFailoverSpec
from scitex_agent_container.config._provider_types import ProviderSpec
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
